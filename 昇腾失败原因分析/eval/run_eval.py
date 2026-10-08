#!/usr/bin/env python3
"""三臂评测：规则（基线） / deepseek-flash / deepseek-v4-pro。

三臂走**同一个指标函数**（`eval_metrics.arm_metrics`）。口径差一点，比出来的差值就没有意义。

关键纪律：
  1. **先冻结真值、再跑 LLM**。`eval/truths.jsonl` 由人工裁定，裁定者不看 LLM 输出；
     本脚本只读它。真值缺失的 case 记为「未裁定」，**不计入准确率**（不是算错）。
  2. **规则臂必须在同一批样本上复现出 ~48%**。复现不出就先停下查窗口口径与样本构成，
     不要往下比 —— 基线变了，后面的差值全是假的。
  3. **降级按线上行为计分**（退回规则判决）：这样「LLM 臂 − 规则臂」就是纯粹的改进量，
     降级率高时不会靠排除样本虚假变好。同时另出一列 `llm_only`（只用真正判了的 case）
     供对照，两列都在报告里。
  4. `verdicts.jsonl` **必须存原始响应文本**：事后才分得清「模型判错」与「解析器判错」。

用法：
  python3 eval/run_eval.py --dry-run                    # ReplayLLMClient 空跑：验 harness 与指标，$0
  python3 eval/run_eval.py --arms rule,deepseek-flash    # 真跑（需 DEEPSEEK_API_KEY）
  python3 eval/run_eval.py --replay runs/<ts>/verdicts.jsonl   # 复用上次的原始响应重算指标，$0
"""
import argparse
import datetime
import json
import os
import pathlib
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from forensics import eval_metrics as em                       # noqa: E402
from forensics.llm_client import DeepSeekClient, ReplayLLMClient   # noqa: E402
from forensics.llm_evidence import build_evidence              # noqa: E402
from forensics.llm_judge import (judge_case, read_evidence_text,  # noqa: E402
                                 render_summary_line, summarize_outcomes)
from forensics import llm_verdict as lv                        # noqa: E402
from forensics.llm_verdict import PROMPT_VERSION, weak_decisive_line   # noqa: E402

RULE_ARM = "rule"
FAKE_ARM = "fake"          # --dry-run 且无 --replay 时的空回放臂（全部降级）


def load_cases(path, *, strata=None, limit=0, job_ids=None):
    cases = em.read_jsonl(path)
    if strata:
        cases = [case for case in cases if case.get("stratum") in strata]
    if job_ids:
        wanted = {str(item) for item in job_ids}
        cases = [case for case in cases if str(case.get("job_id")) in wanted]
    return cases[:limit] if limit else cases


def load_truths(path):
    """人工裁定表 → {job_id: 真值}。文件不存在时返回空（全部记为未裁定，而不是造 0 个对）。"""
    truths = {}
    for row in em.read_jsonl(path):
        truths[str(row.get("job_id"))] = row
    return truths


def truth_of(truths, job_id):
    row = truths.get(str(job_id))
    if not row:
        return None, None
    return row.get("truth_class"), row.get("truth_owner")


def truth_expressible(truths, job_id):
    """该案例的真值在闭集桶表里有没有正确的那只桶（True/False/None=未标注）。

    取自真值行的 `closed_set_expressible`。**必须与真值行同名读**：字段名对不上时
    （例如真值行写 `owner`、这里读 `truth_owner`）会静默取到 None，
    于是 owner 准确率整列算在 None 上 —— 数字看着是出来了，但量的是空气。
    """
    row = truths.get(str(job_id))
    return None if not row else row.get("closed_set_expressible")


def _record(case, arm, *, pred_class, pred_owner, truths, cited=(), allowed=(),
            used=True, weak=False, reason=None, usage=None, elapsed=None,
            pred_class_llm_only=None, raw_response=None,
            phenomenon="", verdict_class_source="rule", root_cause=""):
    """`verdict_class_source` 的取值口径（进指标，别随手改）：

      - `"llm"`：LLM 判了，且给了闭集内的归类；
      - `"none"`：LLM 判了，但**留空**（或填了闭集外的名字）→ 投影成 `其他`。
        这一档进 `declined_rate`，是「桶表覆盖不足」的读数，与下面那档不是一回事；
      - `"rule_fallback"`：LLM 整条不可用，退回规则桶（`used=False`）；
      - `"rule"`：规则臂，根本没跑 LLM。
    """
    truth_class, truth_owner = truth_of(truths, case["job_id"])
    return {
        "job_id": str(case["job_id"]), "arm": arm,
        "rule_bucket": case.get("rule_bucket"), "rule_owner": case.get("rule_owner"),
        "pred_class": pred_class, "pred_owner": pred_owner,
        "pred_class_llm_only": pred_class_llm_only,
        "truth_class": truth_class, "truth_owner": truth_owner,
        "truth_expressible": truth_expressible(truths, case["job_id"]),
        "stratum": case.get("stratum"), "chip": case.get("chip"),
        "step": case.get("step"),
        "cited": list(cited), "allowed": list(allowed),
        "used": used, "weak_decisive": weak, "fallback_reason": reason,
        "phenomenon": phenomenon, "verdict_class_source": verdict_class_source,
        "root_cause": root_cause,
        "usage": usage or {}, "elapsed": elapsed,
        "raw_response": raw_response,
    }


def rule_records(cases, truths, *, fixtures_dir):
    """规则臂：**从 fixture 现算**（用生产代码自己的 classify_text），不用记下来的副本。

    顺带校验：现算的桶必须等于 cases.jsonl 里记的那个 —— 不等就说明冻结集与当初构建时
    的桶表/窗口已经对不上，此时任何「LLM 比规则强」的结论都不成立。
    """
    from eval.production import production
    ns = production()
    records, drift = [], []
    for case in cases:
        # 这里的比对与 cases.jsonl 里的 `rule_drift`（handoff 记录 vs 现算）不是一回事：
        #   - rule_drift 是**桶表演进**的信号，记录在案、不阻断（新例优先重建冻结集即可）；
        #   - 这里的比对是**冻结集是否被改动**：fixture 现算 ≠ cases.jsonl 记录，说明
        #     冻结集与它自己当初的构建对不上，此时任何跨臂比较都不成立，必须停下。
        scan_text = read_evidence_text(pathlib.Path(fixtures_dir) / f"{case['job_id']}.txt.gz")
        bucket, sig = ns["classify_text"](scan_text)
        if bucket != case.get("rule_bucket"):
            drift.append(f"{case['job_id']}: 现算 {bucket} ≠ 记录 {case.get('rule_bucket')}")
        # 规则层的「决定性证据」是它命中的那一行。用整行判「是不是 WARNING/INFO 抢中」，
        # 两臂的这个指标才可比（拿 ±30 字片段判会恒为 0，见 eval_metrics.rule_weak_decisive）。
        _label, weak = em.rule_weak_decisive(scan_text, ns["BUCKETS"])
        records.append(_record(case, RULE_ARM, pred_class=bucket,
                               pred_owner=ns["BUCKET_OWNER"].get(bucket), truths=truths,
                               weak=weak))
    if drift:
        raise SystemExit("❌ 规则桶与冻结集记录不一致，评测必须停下：\n   - "
                         + "\n   - ".join(drift[:5]))
    return records


def llm_records(cases, truths, client_for, *, fixtures_dir, allowed_classes, arm_name,
                budget_tokens, max_tokens, timeout):
    """`client_for(case) -> client`：真跑时三种臂共用一个客户端；回放时按 job 各取一份
    原始响应（$0 重算指标，且输入逐字节可复现）。"""
    records = []
    for case in cases:
        scan_text = read_evidence_text(pathlib.Path(fixtures_dir) / f"{case['job_id']}.txt.gz")
        outcome = judge_case(scan_text, client=client_for(case),
                             allowed_classes=allowed_classes,
                             rule_hint=case.get("rule_bucket"), job_id=case["job_id"],
                             job_name=case.get("job_name"), step_name=case.get("step"),
                             chip=case.get("chip"), budget_tokens=budget_tokens,
                             max_tokens=max_tokens, timeout=timeout)
        allowed = sorted(build_evidence(scan_text, budget_tokens=budget_tokens).line_numbers())
        parsed = outcome.parsed or {}
        # 降级 = 线上退回规则判决：按那个口径计分，差值才是「上线后会发生什么」。
        # **这一档的口径一字不改**：改了就等于把「LLM 挂了会怎样」从报表里抹掉。
        if outcome.used:
            # `verdict_class` 现在可能是空串（闭集里没有贴合的那一格）；空串与越界都投影成
            # `其他`，不硬塞最近桶 —— 投影逻辑与 `llm_verdict.projected_class` 同源。
            predicted = lv.projected_class(parsed)
            class_source = "llm" if predicted != lv.OTHER_CLASS else "none"
        else:
            predicted = case.get("rule_bucket")
            class_source = "rule_fallback"
        owner = parsed.get("owner") if outcome.used else case.get("rule_owner")
        records.append(_record(
            case, arm_name, pred_class=predicted, pred_owner=owner, truths=truths,
            cited=outcome.meta.get("cited_lines") or [], allowed=allowed,
            used=outcome.used, weak=bool(outcome.meta.get("weak_decisive_line")),
            reason=outcome.fallback_reason, usage=outcome.meta.get("usage"),
            elapsed=outcome.meta.get("elapsed"),
            pred_class_llm_only=predicted if outcome.used else None,
            phenomenon=parsed.get("phenomenon") or "",
            verdict_class_source=class_source,
            root_cause=parsed.get("root_cause") or "",
            raw_response=outcome.meta.get("raw_response")))
    return records


def arm_metrics(records):
    metrics = em.arm_metrics(records)
    # 第二个口径：只用真正判了的 case（降级被排除而不是退回规则）。两个口径都给，
    # 免得读者按自己以为的那个去解读 —— 「退回规则」看上线效果，「只看判了的」看模型能力。
    pure = [(record.get("pred_class_llm_only"), record.get("truth_class"))
            for record in records]
    metrics["agreement_pure_llm"] = em.agreement_detail(pure)
    return metrics


def write_report(path, *, arms, summary, env):
    lines = ["# 根因判决评测（规则 vs LLM）", "",
             f"- 生成时间：{env['generated_at']}",
             f"- prompt 版本：{env['prompt_version']}；模型：{env['models']}",
             f"- 样本：{env['n_cases']} 例（分层 {env['strata']}）；真值裁定：{env['n_truths']} 例",
             f"- 冻结集 sha 校验：{'通过' if not env['fixture_problems'] else env['fixture_problems']}",
             "", "## 各臂指标", "", em.render_arm_table(arms), ""]
    lines.append("## 判决可用性")
    lines.append("")
    lines.append(f"- {render_summary_line(summary)}")
    lines.append("")
    lines.append("## 口径说明")
    lines.append("")
    lines.append("- **真值为 None 的 case 不计入准确率**（未裁定 ≠ 判错）。")
    lines.append("- LLM 臂的降级按**线上行为**计分（退回规则判决）；`agreement` 因此等于"
                 "「上线后实际会得到什么」。纯 LLM 能力见 metrics.json 的 "
                 "`agreement_pure_llm`。")
    lines.append("- **幻觉率**的分母是模型引用的行号；经过引用闸门后它应恒为 0，"
                 "大于 0 说明闸门被绕过。闸门真正的触发次数看 `fallback_reasons` 里的 "
                 "`cited_line_not_in_evidence:*`。")
    lines.append("- 未裁定样本上的一切数字**不成立**，只作为 harness 自检。")
    lines.append("- **降级率与留空率是两件事**：降级是「LLM 整条不可用，退回规则桶」，"
                 "留空是「LLM 给了结论，只是闭集里没有贴合的那一格」。"
                 "后者才是**该往桶表里加桶**的信号，别把它读成模型不好用。")
    lines.append("")
    clusters = em.render_cluster_candidates(arms)
    if clusters:
        lines.append("## 归类为「其他」的现象簇 —— 「该新增哪个桶」的候选")
        lines.append("")
        lines.append("闭集覆盖不到时唯一的出路是显式记成「其他」（不挑最接近的桶冒充结论）；"
                     "下面每簇就是一条**新增归类的候选**，是否新增由人看结论样例裁定。")
        lines.append("")
        lines.append(clusters)
        lines.append("")
    lines.append("## 规则基线复现")
    lines.append("")
    rule = arms.get(RULE_ARM) or {}
    rate = (rule.get("agreement") or {}).get("rate")
    lines.append(f"- 规则臂一致率：{'—' if rate is None else f'{rate:.1%}'}"
                 f"（n={(rule.get('agreement') or {}).get('n')}）")
    lines.append("- 该数字必须与线上实测基线（约 48%）同量级；差得远就先查窗口口径与样本构成，"
                 "**不要往下比**。")
    pathlib.Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="根因判决评测（规则 / LLM）")
    parser.add_argument("--cases", default=str(BASE_DIR / "eval/cases.jsonl"))
    parser.add_argument("--fixtures", default=str(BASE_DIR / "eval/fixtures"))
    parser.add_argument("--truths", default=str(BASE_DIR / "eval/truths.jsonl"))
    parser.add_argument("--runs-dir", default=str(BASE_DIR / "eval/runs"))
    parser.add_argument("--arms", default="rule", help="逗号分隔：rule,deepseek-flash,…")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--stratum", action="append",
                        help="只看指定分层（defect/normal/undetermined，可重复）")
    parser.add_argument("--job-id", action="append")
    parser.add_argument("--dry-run", action="store_true",
                        help="不出网：空回放（全部降级），用于验证 harness 与指标")
    parser.add_argument("--replay", help="复用上次 runs/<ts>/verdicts.jsonl 里的原始响应")
    parser.add_argument("--base-url", default=os.environ.get("DEEPSEEK_BASE_URL",
                                                             "https://api.deepseek.com/v1"))
    parser.add_argument("--temperature", type=float, default=0.0,
                        help="判决要可复现，默认 0；调它等于换口径，env.json 会记下")
    parser.add_argument("--max-tokens", type=int, default=4000)
    parser.add_argument("--budget-tokens", type=int, default=60_000)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    cases = load_cases(args.cases, strata=args.stratum, limit=args.limit,
                       job_ids=args.job_id)
    truths = load_truths(args.truths)
    if not cases:
        print(f"没有可评的 case（{args.cases}）—— 先跑 eval/build_fixtures.py")
        return 1

    problems = em.verify_fixtures(cases, args.fixtures)
    if problems:
        print("❌ 冻结集校验失败，评测必须停下：")
        for problem in problems[:10]:
            print(f"   - {problem}")
        return 1

    from eval.production import allowed_classes
    classes = allowed_classes()
    run_dir = pathlib.Path(args.runs_dir,
                           datetime.datetime.now().strftime("%Y%m%dT%H%M%S"))
    run_dir.mkdir(parents=True, exist_ok=True)

    arms = {}
    all_verdicts, summary = [], {"total": 0, "used": 0, "degraded": 0, "reasons": {}}
    replay_responses = {}
    if args.replay:
        replay_responses = {row["job_id"]: row.get("raw_response")
                            for row in em.read_jsonl(args.replay)}

    for arm in [name.strip() for name in args.arms.split(",") if name.strip()]:
        if arm == RULE_ARM:
            records = rule_records(cases, truths, fixtures_dir=args.fixtures)
        else:
            if args.dry_run or args.replay:
                client_for = (lambda case: _OneShotReplay(
                    replay_responses.get(str(case["job_id"]))))
            else:
                api_key = os.environ.get("DEEPSEEK_API_KEY")
                if not api_key:
                    print("缺少 DEEPSEEK_API_KEY（凭据由环境变量注入，本仓不落盘）")
                    return 1
                shared = DeepSeekClient(api_key=api_key, base_url=args.base_url,
                                        model=arm, temperature=args.temperature)
                client_for = lambda case: shared
            records = llm_records(cases, truths, client_for,
                                  fixtures_dir=args.fixtures,
                                  allowed_classes=classes, arm_name=arm,
                                  budget_tokens=args.budget_tokens,
                                  max_tokens=args.max_tokens, timeout=args.timeout)
            all_verdicts.extend(records)
            if (args.dry_run or args.replay) and not replay_responses:
                # harness 自检：全部降级 → 每条都退回规则判决，于是这一臂必须**逐条等于**
                # 规则臂。不等就是计分或降级链路有 bug，此时任何真跑的结论都不可信。
                mismatch = [record["job_id"] for record in records
                            if record["used"] or record["pred_class"] != record["rule_bucket"]]
                if mismatch:
                    raise SystemExit(f"❌ 空回放自检失败：{len(mismatch)} 例没有退回规则判决"
                                     f"（例：{mismatch[:3]}）")
                print(f"✅ 自检：{arm} 空回放 {len(records)} 例全部降级并退回规则判决")
        arms[arm] = arm_metrics(records)
        agreement = arms[arm]["agreement"]
        print(f"{arm}: 一致率={agreement['rate']}（n={agreement['n']}，"
              f"未裁定={agreement['unadjudicated']}）降级率={arms[arm]['degrade_rate']}")

    if all_verdicts:
        summary = summarize_outcomes([
            _OutcomeView(record) for record in all_verdicts])
    env = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "prompt_version": PROMPT_VERSION, "models": args.arms,
        "n_cases": len(cases), "n_truths": len(truths),
        "strata": {name: sum(1 for case in cases if case.get("stratum") == name)
                   for name in sorted({case.get("stratum") for case in cases})},
        "fixture_problems": problems,
        "temperature": args.temperature, "max_tokens": args.max_tokens,
        "budget_tokens": args.budget_tokens, "endpoint": args.base_url,
        "python": sys.version.split()[0], "dry_run": bool(args.dry_run),
        "cases_sha256": em.sha256_text(
            "\n".join(f"{case['job_id']}:{case['scan_sha256']}" for case in cases)),
    }
    with open(run_dir / "verdicts.jsonl", "w", encoding="utf-8") as handle:
        for record in all_verdicts:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    (run_dir / "metrics.json").write_text(
        json.dumps({"arms": arms, "env": env}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    (run_dir / "env.json").write_text(json.dumps(env, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    write_report(run_dir / "report.md", arms=arms, summary=summary, env=env)
    print(f"\n产出：{run_dir}（verdicts.jsonl / metrics.json / report.md / env.json）")
    return 0


class _OneShotReplay:
    """回放一个 case 的**已落盘原始响应**。

    `text=None` 时不报错、而是返回 empty_content：这正是 `--dry-run` 没有回放数据时的行为
    —— 每个 case 都降级，于是 LLM 臂的一致率必须**正好等于**规则臂（降级按线上行为退回规则）。
    这个恒等式是 harness 的自检：不等就说明计分或降级链路有 bug。
    """

    def __init__(self, text):
        self.text = text
        self.model = "replay"

    def judge(self, system, user, *, max_tokens=None, timeout=None):
        from forensics.llm_client import LLMResult
        if self.text:
            return LLMResult(True, text=self.text, attempts=1, elapsed=0.0)
        return LLMResult(False, error="empty_content", attempts=1, elapsed=0.0)


class _OutcomeView:
    """把记录视图成 summarize_outcomes 要的形状（它只关心 used / fallback_reason）。"""

    def __init__(self, record):
        self.used = record["used"]
        self.fallback_reason = record["fallback_reason"]


if __name__ == "__main__":
    sys.exit(main())
