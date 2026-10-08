#!/usr/bin/env python3
"""LLM 判决的解析、校验、降级与叠加（纯函数，不碰网络、不碰报告渲染）。

三条设计要点：

1. **不信任模型的自报。** `disagrees_with_rule` 由服务端按 `verdict_class != 规则桶` 现算，
   模型自报的只留作诊断 —— 自报字段会被「迎合」污染。

2. **引用行号是第一道幻觉闸门。** 模型可以编，但编出来的行号不在证据窗口里就过不了校验。
   这比「请勿编造」这类祈使句有效得多，而且它的失效率本身就是可观测指标（幻觉率）。

3. **降级必须显式。** 任何一步失败都退回规则判决，且把原因具名写进 `verdict.llm.fallback_reason`：
   静默退回等于把「AI 判的」和「规则判的」混成一种东西，报告读者无法分辨，比不接 LLM 更糟。

4. **桶是事后归类，不是判决入口。** `verdict_class` 选填：闭集里没有贴合的就留空。
   强迫模型在定性**之前**先落一个桶，等于把规则层「首个命中正则胜出」原样搬进模型 ——
   实测那正是 60% 的真值根本不在 32 桶里、而模型仍被逼着挑一个最接近的成因。
   投影结果只用于统计/去重/派活，**不进任何给人读的结论行**。
"""
import json
import re

# 改了 prompt（含纪律条款、输出契约）必须改这里：它进判决缓存的键与报告，
# 否则改完 prompt 会静默复用旧口径的判决，「评测证明改进了」而线上还是旧的。
PROMPT_VERSION = "v2"

OWNER_ENUM = ("infra", "code", "mixed", "unknown")
CONFIDENCE_ENUM = ("high", "medium", "low")
# verdict_class 的闭集由调用方传入（桶表在流水线脚本里，测试无法 import 那个脚本），
# 这里只放两个兜底类，避免调用方漏传时把闸门悄悄放宽。
FALLBACK_CLASSES = ("其他", "未分类")

MAX_ROOT_CAUSE_CHARS = 600

# 置信度映射：与规则层的措辞同前缀（既有测试断言的是前缀词），LLM 那档必须同框
# 出现在结论行里 —— 「算出来的置信度」与「模板拼出来的结论」语气不匹配正是现状的病根。
CONFIDENCE_TEXT = {
    "high": "高（LLM 判决：引用证据行已给出判据）",
    "medium": "中（LLM 判决，建议人工过一眼）",
    "low": "低（LLM 判决：缺决定性判据，勿直接派活）",
}
DEGRADED_CONFIDENCE = "低（LLM 判决不可用，退回规则）"
DEGRADED_BASIS_PREFIX = "LLM 判决不可用（原因："


class VerdictParseError(Exception):
    """reason 是机器可读的（进 fallback_reason 与指标），detail 供人排查。"""

    def __init__(self, reason, detail=""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def extract_json(text):
    """从模型输出里取出 JSON 对象；剥 ```json 围栏，容忍前后有解释性文字。"""
    if text is None or not text.strip():
        raise VerdictParseError("empty_content", "模型返回了空内容")
    stripped = text.strip()
    fenced = re.search(r'```(?:json)?\s*(.*?)```', stripped, re.S)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        parsed = json.loads(stripped)
    except Exception:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            raise VerdictParseError("not_json", stripped[:200])
        try:
            parsed = json.loads(stripped[start:end + 1])
        except Exception as exc:
            raise VerdictParseError("not_json", f"{exc}: {stripped[:200]}")
    if not isinstance(parsed, dict):
        raise VerdictParseError("not_json", f"顶层不是对象：{type(parsed).__name__}")
    return parsed


def _require(parsed, field, types):
    if field not in parsed:
        raise VerdictParseError(f"missing_field:{field}")
    value = parsed[field]
    if not isinstance(value, types) or isinstance(value, bool):
        raise VerdictParseError(f"bad_type:{field}", f"{type(value).__name__}")
    return value


def parse_llm_verdict(text, *, allowed_classes):
    """解析并校验模型输出。字段缺失/类型错/枚举越界一律抛 VerdictParseError（→ 降级）。

    必填：root_cause / owner / confidence / evidence_lines
    选填：verdict_class / phenomenon / decisive_line / disagrees_with_rule / missing_evidence
      —— 诊断性字段不参与判决，缺了不该让整条判决作废（那会白白抬高降级率）。

    `verdict_class` 是**事后归类**（见模块 docstring 第 4 条），因此选填、默认空串，且
    **取值越界不再作废整条判决**：把「模型挑了个闭集里没有的桶」升级成「整条判决不可用」，
    代价（丢掉一条本来可用的自由文本判决）与收益完全不成比例。越界与否记在
    `verdict_class_in_closed_set` 里，供指标读取 —— 它本身就是闭集覆盖不足的读数。
    """
    parsed = extract_json(text)

    root_cause = _require(parsed, "root_cause", str).strip()
    if not root_cause:
        raise VerdictParseError("missing_field:root_cause")
    owner = _require(parsed, "owner", str)
    if owner not in OWNER_ENUM:
        raise VerdictParseError("bad_enum:owner", owner)
    confidence = _require(parsed, "confidence", str)
    if confidence not in CONFIDENCE_ENUM:
        raise VerdictParseError("bad_enum:confidence", confidence)
    raw_class = parsed.get("verdict_class")
    if raw_class is None:
        verdict_class = ""
    elif not isinstance(raw_class, str):
        raise VerdictParseError("bad_type:verdict_class", type(raw_class).__name__)
    else:
        verdict_class = raw_class.strip()

    evidence_lines = _require(parsed, "evidence_lines", list)
    if not evidence_lines:
        raise VerdictParseError("missing_field:evidence_lines", "引用证据行为空")
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in evidence_lines):
        raise VerdictParseError("bad_type:evidence_lines", str(evidence_lines)[:120])

    decisive_line = parsed.get("decisive_line")
    if decisive_line is not None and not isinstance(decisive_line, int):
        raise VerdictParseError("bad_type:decisive_line", str(decisive_line)[:60])

    return {
        "root_cause": root_cause[:MAX_ROOT_CAUSE_CHARS],
        "owner": owner,
        "confidence": confidence,
        "verdict_class": verdict_class,
        # 空串（模型没归类）与「归了个闭集外的名字」都记 False —— 两者对指标的含义不同，
        # 靠 verdict_class 是否为空串区分，不靠这个布尔值。
        "verdict_class_in_closed_set": verdict_class in tuple(allowed_classes) + FALLBACK_CLASSES,
        "phenomenon": str(parsed.get("phenomenon") or "").strip(),
        "evidence_lines": list(evidence_lines),
        "decisive_line": decisive_line,
        "disagrees_with_rule": parsed.get("disagrees_with_rule"),
        "missing_evidence": str(parsed.get("missing_evidence") or "").strip(),
    }


def validate_citations(parsed, allowed_lines):
    """返回引用行号的问题列表（空列表 = 全部落在证据窗口内）。

    这是抑制幻觉的核心闸门：模型可以编，编的行号过不了。返回的是问题码而不是抛异常，
    因为「软」问题（没给决定性行）只该记指标、不该作废判决 —— 作废与否由调用方按
    EMPTY/硬问题决定。
    """
    allowed = set(allowed_lines)
    problems = []
    for number in parsed.get("evidence_lines") or []:
        if number not in allowed:
            problems.append(f"cited_line_not_in_evidence:{number}")
    decisive = parsed.get("decisive_line")
    if decisive is not None and decisive not in allowed:
        problems.append(f"decisive_line_not_in_evidence:{decisive}")
    if decisive is not None and decisive not in (parsed.get("evidence_lines") or []):
        problems.append("decisive_line_not_cited")
    return problems


def hard_citation_problems(problems):
    """返回必须触发降级的问题码（引用了证据窗口里不存在的行号）；其余是软问题，只记指标。"""
    return [p for p in problems if "not_in_evidence" in p]


def weak_decisive_line(decisive_text, confidence):
    """决定性行是不是一条 WARNING/INFO —— 实测那正是规则层误判的病征，要能被统计。"""
    if confidence == "low":
        return False
    if not decisive_text:
        return True
    return bool(re.search(r'\bWARNING\b|\bWARN\b|\bINFO\b', decisive_text))


class LLMOutcome:
    """一次判决尝试的结果：成功（parsed 有值）或降级（fallback_reason 具名）。"""

    def __init__(self, used, parsed=None, fallback_reason=None, meta=None):
        self.used = used
        self.parsed = parsed
        self.fallback_reason = fallback_reason
        self.meta = meta or {}

    @classmethod
    def disabled(cls):
        return cls(False, fallback_reason="disabled", meta={})

    @classmethod
    def degraded(cls, reason, meta=None):
        return cls(False, fallback_reason=reason, meta=meta or {})

    def as_dict(self):
        block = {"used": self.used, "fallback_reason": self.fallback_reason}
        block.update(self.meta)
        if self.parsed:
            block.update({k: v for k, v in self.parsed.items()})
        return block


def apply_llm_verdict(rule_verdict, outcome, rule_bucket=None):
    """把 LLM 判决叠加到规则 verdict 上，返回新 verdict。

    与规则层的边界（每一条都有对应测试）：
      - `owner_from_cluster` 为真时 LLM **不得覆盖 owner**（集群侧实证比模型推断硬，
        与既有 synthesize 的纪律一致），只加一条 conflict 并降置信度；
      - `basis` 只**追加**不插队（既有测试断言 basis[0] 的前缀）；
      - `needs_human` 是**单向棘轮**：LLM 只能置真，不能清除规则/集群侧已判出的人工复核标记；
      - `official_leaf` / `precedent` / `owner_from_cluster` / `suggestions` 原样保留。
    """
    merged = dict(rule_verdict or {})
    basis = list(merged.get("basis") or [])
    conflicts = list(merged.get("conflicts") or [])
    hints = list(merged.get("hints_requiring_human") or [])
    merged["confidence_rule"] = merged.get("confidence")

    if not outcome.used or not outcome.parsed:
        reason = outcome.fallback_reason or "unknown"
        merged["llm"] = outcome.as_dict()
        merged["confidence"] = DEGRADED_CONFIDENCE
        merged["needs_human"] = True
        basis.append(f"{DEGRADED_BASIS_PREFIX}{reason}），已退回规则判决："
                     f"{merged.get('root_cause')}")
        merged["basis"] = basis
        return merged

    parsed = outcome.parsed
    merged["llm"] = outcome.as_dict()
    rule_owner = merged.get("owner")
    owner_from_cluster = bool(merged.get("owner_from_cluster"))
    if owner_from_cluster and parsed["owner"] != rule_owner:
        conflicts.append(f"集群侧实证判 owner={rule_owner}，LLM 判 {parsed['owner']}；"
                         f"以集群侧为准（模型推断不得覆盖实证）")
        merged["confidence"] = CONFIDENCE_TEXT["low"]
    else:
        merged["owner"] = parsed["owner"]
        merged["confidence"] = CONFIDENCE_TEXT[parsed["confidence"]]
    merged["root_cause"] = parsed["root_cause"]

    if rule_bucket and parsed["verdict_class"] != rule_bucket:
        conflicts.append(f"规则桶【{rule_bucket}】与 LLM 判决【{parsed['verdict_class']}】不一致"
                         f"（规则桶是正则首个命中，非最终结论）")
    basis.append(f"LLM 判决（{parsed['verdict_class']}）：{parsed['root_cause']}")
    if parsed["evidence_lines"]:
        cited = "、".join(f"L{n}" for n in parsed["evidence_lines"])
        basis.append(f"LLM 引用的证据行：{cited}")
    if parsed["missing_evidence"]:
        hints.append(f"补齐以下证据才能定性（LLM 判定）：{parsed['missing_evidence']}")
    merged["basis"] = basis
    merged["conflicts"] = conflicts
    merged["hints_requiring_human"] = hints
    # 单向棘轮：只能置真
    merged["needs_human"] = bool(merged.get("needs_human")) or parsed["confidence"] != "high" \
        or bool(parsed["missing_evidence"])
    return merged
