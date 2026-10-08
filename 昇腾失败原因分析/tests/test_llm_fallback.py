#!/usr/bin/env python3
"""LLM 判决降级与叠加的回归测试。

为什么守这些（每一条都对应一个真实的坏结果）：
  - **降级必须逐字保留规则结论**：半个模型结论混进规则判决，读者无法分辨谁说的；
  - **置信度必须封顶**：LLM 挂了却仍标「高」，等于把不可用伪装成高可信；
  - **basis 只追加不插队**：`test_peer_logs.py` 断言 `basis[0]` 的前缀，插队会连带把它弄红；
  - **集群侧实证不得被模型覆盖**：实证比推断硬，这是既有 synthesize 的纪律；
  - **needs_human 是单向棘轮**：模型的高置信不能用来消除人工复核标记 ——
    本仓 99.7% 的 case 是低/中置信，人工复核是安全网，不能被「AI 说它确定」拆掉。

运行：python3 tests/test_llm_fallback.py      （无需 pytest，也兼容 pytest）
"""
import pathlib
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
sys.path.insert(0, str(BASE_DIR))

from forensics import llm_verdict as lv    # noqa: E402

# 取自 synthesize() 的真实形态（含既有各个键，用来验证「没被误伤」）
RULE_VERDICT = {
    "root_cause": "依赖/安装(ImportError)",
    "owner": "code",
    "owner_from_cluster": False,
    "confidence": "中（仅日志侧正则定性）",
    "basis": ["日志侧：node0 失败步骤时间窗命中桶【依赖/安装(ImportError)】"],
    "conflicts": [],
    "hints_requiring_human": [],
    "needs_human": False,
    "official_leaf": "依赖问题",
    "suggestions": ["检查镜像内是否缺该模块"],
    "precedent": None,
}

PARSED = {
    "root_cause": "业务侧用例真失败：test_single_node 断言精度不达标",
    "owner": "code",
    "confidence": "high",
    "verdict_class": "测试用例失败(pytest ret=1)",
    "phenomenon": "用例真失败（精度/逻辑）",
    "evidence_lines": [10, 11],
    "decisive_line": 10,
    "disagrees_with_rule": True,
    "missing_evidence": "",
}

ALL_REASONS = [
    "disabled", "timeout", "http_429", "http_5xx", "http_4xx", "network",
    "empty_content", "not_json", "missing_field:owner", "bad_enum:verdict_class",
    "cited_line_not_in_evidence:9999", "evidence_unavailable", "budget_exhausted",
]


def _used(**overrides):
    parsed = dict(PARSED)
    parsed.update(overrides)
    return lv.LLMOutcome(True, parsed=parsed, meta={"model": "deepseek-flash",
                                                    "prompt_version": lv.PROMPT_VERSION})


# ---------------- 降级路径 ----------------

def test_every_failure_mode_is_named_and_preserves_the_rule_verdict():
    for reason in ALL_REASONS:
        outcome = lv.LLMOutcome.disabled() if reason == "disabled" \
            else lv.LLMOutcome.degraded(reason)
        merged = lv.apply_llm_verdict(RULE_VERDICT, outcome)
        assert merged["root_cause"] == RULE_VERDICT["root_cause"], reason
        assert merged["owner"] == RULE_VERDICT["owner"], reason
        assert merged["confidence"] == lv.DEGRADED_CONFIDENCE, reason
        assert merged["needs_human"] is True, reason
        assert merged["llm"]["used"] is False
        assert merged["llm"]["fallback_reason"] == reason, merged["llm"]
        assert any(lv.DEGRADED_BASIS_PREFIX in line and reason in line
                   for line in merged["basis"]), f"{reason} 没在依据里留下说明行"


def test_degraded_confidence_is_capped_regardless_of_rule_confidence():
    for rule_confidence in ("高（集群侧实证）", "中（仅日志侧正则定性）", "低（无判据）"):
        rule = dict(RULE_VERDICT, confidence=rule_confidence)
        merged = lv.apply_llm_verdict(rule, lv.LLMOutcome.degraded("timeout"))
        assert merged["confidence"] == lv.DEGRADED_CONFIDENCE, rule_confidence


def test_degraded_keeps_rule_confidence_for_comparison():
    merged = lv.apply_llm_verdict(RULE_VERDICT, lv.LLMOutcome.degraded("timeout"))
    assert merged["confidence_rule"] == RULE_VERDICT["confidence"]


# ---------------- 正常路径 ----------------

def test_used_outcome_overrides_root_cause_and_owner():
    merged = lv.apply_llm_verdict(RULE_VERDICT, _used(), rule_bucket="依赖/安装(ImportError)")
    assert merged["root_cause"] == PARSED["root_cause"]
    assert merged["owner"] == "code"
    assert merged["confidence"] == lv.CONFIDENCE_TEXT["high"]
    assert merged["confidence_rule"] == RULE_VERDICT["confidence"], "规则那档要留着对照"
    assert merged["llm"]["verdict_class"] == PARSED["verdict_class"]


def test_basis_is_appended_never_prepended():
    merged = lv.apply_llm_verdict(RULE_VERDICT, _used())
    assert merged["basis"][0] == RULE_VERDICT["basis"][0], \
        "LLM 依据插到了 basis[0] —— test_peer_logs 断言的就是 basis[0] 的前缀"
    assert len(merged["basis"]) > len(RULE_VERDICT["basis"])
    assert any("LLM 引用的证据行" in line for line in merged["basis"])


def test_rule_bucket_disagreement_is_recorded_as_conflict():
    merged = lv.apply_llm_verdict(RULE_VERDICT, _used(), rule_bucket="依赖/安装(ImportError)")
    assert any("依赖/安装(ImportError)" in line and "不一致" in line
               for line in merged["conflicts"]), merged["conflicts"]


def test_agreement_adds_no_conflict():
    merged = lv.apply_llm_verdict(RULE_VERDICT, _used(),
                                 rule_bucket="测试用例失败(pytest ret=1)")
    assert merged["conflicts"] == []


def test_cluster_evidence_is_not_overridden_by_the_model():
    rule = dict(RULE_VERDICT, owner="infra", owner_from_cluster=True,
                confidence="高（集群侧实证：容器 OOMKilled）")
    merged = lv.apply_llm_verdict(rule, _used(owner="code"),
                                 rule_bucket="依赖/安装(ImportError)")
    assert merged["owner"] == "infra", "集群侧实证被模型推断覆盖了"
    assert any("集群侧为准" in line for line in merged["conflicts"]), merged["conflicts"]
    assert merged["confidence"] == lv.CONFIDENCE_TEXT["low"], "分歧必须降置信度"


def test_needs_human_is_a_one_way_ratchet():
    rule = dict(RULE_VERDICT, needs_human=True)
    merged = lv.apply_llm_verdict(rule, _used(confidence="high"))
    assert merged["needs_human"] is True, "模型的高置信不能清除已判出的人工复核标记"


def test_medium_or_low_confidence_still_requires_human():
    for level in ("medium", "low"):
        merged = lv.apply_llm_verdict(RULE_VERDICT, _used(confidence=level))
        assert merged["needs_human"] is True, level


def test_missing_evidence_is_surfaced_to_human_hints():
    merged = lv.apply_llm_verdict(RULE_VERDICT, _used(missing_evidence="缺用例级 traceback"))
    assert any("缺用例级 traceback" in line for line in merged["hints_requiring_human"])
    assert merged["needs_human"] is True


def test_untouched_fields_are_preserved():
    merged = lv.apply_llm_verdict(RULE_VERDICT, _used())
    for field in ("official_leaf", "precedent", "owner_from_cluster", "suggestions"):
        assert merged[field] == RULE_VERDICT[field], field


def test_rule_verdict_dict_is_not_mutated():
    before = {k: (list(v) if isinstance(v, list) else v) for k, v in RULE_VERDICT.items()}
    lv.apply_llm_verdict(RULE_VERDICT, _used(), rule_bucket="依赖/安装(ImportError)")
    assert RULE_VERDICT == before, "叠加判决改了传进来的规则 verdict —— 调用方可能还要用"


def test_llm_block_is_json_serializable():
    import json
    merged = lv.apply_llm_verdict(RULE_VERDICT, _used())
    json.dumps(merged["llm"], ensure_ascii=False)


def main():
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = []
    for name, func in tests:
        try:
            func()
            print(f"✅ {name}")
        except Exception as exc:
            # 连 AssertionError 以外的异常也计为失败：崩在某个用例上会**掩盖后面所有用例**，
            # 「套件整体崩溃」在证伪里看起来像「没红」，比一条失败危险得多。
            failed.append(name)
            detail = str(exc) if isinstance(exc, AssertionError) else f"{type(exc).__name__}: {exc}"
            print(f"❌ {name}: {detail}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} 通过" + (f"，失败：{failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
