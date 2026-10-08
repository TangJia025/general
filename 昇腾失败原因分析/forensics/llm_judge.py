#!/usr/bin/env python3
"""LLM 判决的编排：证据打包 → 组装 prompt → 调客户端 → 解析 → 引用校验 → LLMOutcome。

这一层只做**串起来**，不自己做任何判断：每一步失败都翻译成一个具名的降级原因，
交给 `llm_verdict.LLMOutcome`。所以本文件里没有 `try: … except: pass` ——
静默吞掉失败，等于把「AI 判的」和「规则判的」混成一种东西，报告读者无法分辨，
那比不接 LLM 更糟（本方案的每一条降级都必须留下可统计的名字）。

**为什么降级原因要这么细分**：降级率是线上健康指标，而不同原因的动作完全不同 ——
`http_429` 要退避、`http_4xx` 是 prompt 或凭据的 bug（重试纯烧钱）、
`cited_line_not_in_evidence` 是模型幻觉（要改 prompt）、`budget_exhausted` 是评测预算不够。
打成一句「LLM 不可用」这四种就都看不见了。

第一阶段：**无缓存、不写盘、不改任何生产脚本**。这一层只被评测脚本调用。
"""
import gzip
import pathlib
import time

from forensics.llm_client import DEFAULT_MAX_TOKENS
from forensics.llm_evidence import (DEFAULT_BUDGET_TOKENS, build_evidence,
                                    estimate_tokens, render_evidence_block)
from forensics.llm_prompt import build_system_prompt, build_user_message
from forensics.llm_verdict import (PROMPT_VERSION, LLMOutcome, VerdictParseError,
                                   hard_citation_problems, parse_llm_verdict,
                                   validate_citations, weak_decisive_line)

DEFAULT_TIMEOUT = 120.0

# 案件元信息字段：从 case 字典里挑出来传给 prompt，其余键（job_id 等）不进 prompt。
CASE_FIELDS = ("job_name", "step_name", "chip", "failed_step", "extra_note")


def read_evidence_text(path):
    """读证据文本（`.gz` 自动解压）。评测与第二阶段都用同一个入口，避免两处解压口径不同。"""
    path = pathlib.Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as handle:
            return handle.read()
    return path.read_text(encoding="utf-8", errors="ignore")


def judge_case(scan_text, *, client, allowed_classes, rule_hint=None,
               budget_tokens=DEFAULT_BUDGET_TOKENS, max_tokens=DEFAULT_MAX_TOKENS,
               timeout=DEFAULT_TIMEOUT, **case_meta):
    """判一个 case。任何失败都返回 `LLMOutcome.degraded(<具名原因>)`，绝不抛异常。

    返回的 outcome.meta 里带齐了评测要用的量：证据摘要、token 估算、耗时、用量、
    引用问题的**软**部分（未自洽不算失败，但要能统计）。
    """
    meta = {"prompt_version": PROMPT_VERSION,
            "model": getattr(client, "model", type(client).__name__),
            "job_id": case_meta.get("job_id")}

    evidence = build_evidence(scan_text or "", budget_tokens=budget_tokens)
    if not evidence.lines:
        return LLMOutcome.degraded("evidence_unavailable", meta)
    meta.update({
        "evidence_sha256": evidence.sha256,
        "evidence_lines": len(evidence.lines),
        "evidence_total_lines": evidence.total_lines,
        "evidence_truncated": bool(evidence.truncated()),
        "terminal_lines": list(evidence.terminal_lines),
        "estimate_tokens": estimate_tokens(scan_text or ""),
    })

    system = build_system_prompt(allowed_classes)
    user = build_user_message(
        render_evidence_block(evidence),
        rule_hint=rule_hint,
        **{key: case_meta.get(key) for key in CASE_FIELDS})

    result = client.judge(system, user, max_tokens=max_tokens, timeout=timeout)
    meta.update({"attempts": result.attempts, "elapsed": round(result.elapsed, 3),
                 "usage": dict(result.usage or {}),
                 # 原始响应必须留下：事后才分得清「模型判错」与「解析器判错」，
                 # 也是 `--replay` 能 $0 重算指标的前提。
                 "raw_response": result.text})
    if not result.ok:
        return LLMOutcome.degraded(result.error or "network", meta)

    try:
        parsed = parse_llm_verdict(result.text, allowed_classes=allowed_classes)
    except VerdictParseError as exc:
        meta["parse_detail"] = exc.detail[:400]
        return LLMOutcome.degraded(exc.reason, meta)

    problems = validate_citations(parsed, evidence.line_numbers())
    hard = hard_citation_problems(problems)
    meta["citation_problems"] = problems
    # 引用行与判决类在这里就记下（**包括**因引用越界而降级的那种）：幻觉率的分母是
    # 「模型引用的行」，如果只在成功时记录，被闸门拦下的那些就消失了 ——
    # 而那恰恰是幻觉率的全部来源，指标会变成恒等于 0 的摆设。
    meta["cited_lines"] = list(parsed["evidence_lines"])
    meta["raw_verdict_class"] = parsed["verdict_class"]
    if hard:
        # 引用窗口外的行号 = 幻觉。这是 prompt 纪律是否生效的直接读数，必须具名落到原因里。
        return LLMOutcome.degraded(hard[0], meta)

    lines_by_number = dict(evidence.lines)
    meta["weak_decisive_line"] = weak_decisive_line(
        lines_by_number.get(parsed.get("decisive_line"), ""), parsed["confidence"])
    return LLMOutcome(True, parsed=parsed, meta=meta)


def judge_cases(cases, *, client, allowed_classes, budget_seconds=None,
                clock=time.monotonic, **kwargs):
    """批量判决，可选墙钟预算。

    预算耗尽后**不静默跳过**剩下的 case —— 每个都记一条 `budget_exhausted` 降级。
    否则「跑了 10 条就超预算」在报告里会变成「30 条都判了」，成本与覆盖率全错。
    """
    started = clock()
    outcomes = []
    for case in cases:
        if budget_seconds is not None and clock() - started > budget_seconds:
            outcomes.append(LLMOutcome.degraded(
                "budget_exhausted", {"job_id": case.get("job_id"),
                                     "model": getattr(client, "model", None)}))
            continue
        call = {key: case.get(key) for key in CASE_FIELDS if case.get(key) is not None}
        outcomes.append(judge_case(
            case.get("scan_text", ""), client=client, allowed_classes=allowed_classes,
            rule_hint=case.get("rule_hint"), job_id=case.get("job_id"), **call, **kwargs))
    return outcomes


def summarize_outcomes(outcomes):
    """`N 成功 / M 降级（原因分布…）` —— 报告摘要那一行的数据源，也是线上健康指标。"""
    used = sum(1 for out in outcomes if out.used)
    reasons = {}
    for out in outcomes:
        if not out.used:
            reasons[out.fallback_reason] = reasons.get(out.fallback_reason, 0) + 1
    ordered = dict(sorted(reasons.items(), key=lambda item: (-item[1], item[0])))
    return {"total": len(outcomes), "used": used, "degraded": len(outcomes) - used,
            "reasons": ordered}

def render_summary_line(summary):
    """压成一行中文摘要，给报告与命令行看。"""
    if not summary["total"]:
        return "LLM 判决：本批无 case"
    detail = "、".join(f"{reason}×{count}" for reason, count in summary["reasons"].items())
    suffix = f"（{detail}）" if detail else ""
    return (f"LLM 判决：{summary['used']} 成功 / {summary['degraded']} 降级"
            f"（共 {summary['total']} 例）{suffix}")
