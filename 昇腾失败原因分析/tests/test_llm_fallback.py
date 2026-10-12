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

# 取自 synthesize() 的真实形态（含既有各个键，用来验证「没被误伤」）。
#
# ⚠️ 这是**手工维护**的快照，synthesize 改措辞它不会自动跟着变 —— 于是就烂过一次：
# `root_cause` 曾经是 `依赖/安装(ImportError)`（桶名直接当结论），而 synthesize 早已把
# 这档删掉（桶不是结论，见 report.py 根因描述处）；`confidence` 也少了个「，未经集群侧验证」。
# 下面 `test_rule_verdict_snapshot_still_matches_synthesize` 就是防这件事的。
RULE_VERDICT = {
    "root_cause": "未能定性（仅有日志侧归类，需人工介入）",
    "owner": "code",
    "owner_from_cluster": False,
    "confidence": "中（仅日志侧正则定性，未经集群侧验证）",
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

# 每个 reason 都必须是**当前真的可能产生**的：`bad_enum:verdict_class` 已随
# 「归类降为选填」消失，留着它只会让这份清单变成一串没人验证的字符串。
ALL_REASONS = [
    "disabled", "timeout", "http_429", "http_5xx", "http_4xx", "network",
    "empty_content", "not_json", "missing_field:owner", "bad_enum:owner",
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


def test_rule_verdict_snapshot_still_matches_synthesize():
    """RULE_VERDICT 是手工快照，会腐烂（已经烂过一次，见文件头注释）。

    这里就地把它的 `root_cause` / `confidence` 与 synthesize 的真实输出对一遍：措辞一改，
    这条红，逼着同步 —— 否则整个文件是在用一份不存在的输入形态做验证，绿得没有意义。
    """
    from forensics.report import synthesize

    case = {
        "job_name": "Nightly-A2 调度失败", "workflow": "schedule_nightly_test_a2.yaml",
        "link": "https://example.invalid/job/1", "step": "Run tests", "chip": "a2",
        "labels": [], "runner_name": None, "bucket": "依赖/安装(ImportError)",
        "owner": "code", "sig": "No module named 'xxx'",
        "cluster": {"skipped": False, "skip_reason": None, "cluster_name": "EXAMPLE-CLUSTER",
                    "kubeconfig_path": "/tmp/example.kubeconfig", "pod_evidence": None,
                    "availability": None, "candidates": [], "not_obtained": [], "logs": []},
        "history": [], "related_issues": [],
    }
    verdict = synthesize(case)
    assert verdict["root_cause"] == RULE_VERDICT["root_cause"], \
        f"synthesize 现在输出 `{verdict['root_cause']}`，快照还停在 `{RULE_VERDICT['root_cause']}`"
    assert verdict["confidence"] == RULE_VERDICT["confidence"], \
        f"synthesize 现在输出 `{verdict['confidence']}`，快照还停在 `{RULE_VERDICT['confidence']}`"
    assert verdict["owner"] == RULE_VERDICT["owner"]


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


# ---------------- 归类是投影，不是结论 ----------------

def test_basis_never_carries_the_class_label():
    """给人读的那一行只有自由文本：桶曾经就是结论本身，那正是误判的来源。"""
    merged = lv.apply_llm_verdict(RULE_VERDICT, _used(), rule_bucket="依赖/安装(ImportError)")
    judged = [line for line in merged["basis"] if line.startswith("LLM 判决：")]
    assert judged == [f"LLM 判决：{PARSED['root_cause']}"], judged


def test_blank_class_projects_to_other():
    """模型留空 = 闭集里没有贴合的格子，落 `其他`（它是「该新增哪个桶」的候选来源）。"""
    merged = lv.apply_llm_verdict(RULE_VERDICT, _used(verdict_class=""), rule_bucket="依赖/安装(ImportError)")
    assert merged["llm_class"] == lv.OTHER_CLASS
    assert merged["llm"]["verdict_class"] == "", "投影不覆盖模型原话，两者都要留"


def test_out_of_set_class_projects_to_other_not_a_fallback():
    merged = lv.apply_llm_verdict(RULE_VERDICT,
                                  _used(verdict_class="模型自创的桶",
                                        verdict_class_in_closed_set=False),
                                  rule_bucket="依赖/安装(ImportError)")
    assert merged["llm_class"] == lv.OTHER_CLASS
    assert merged["root_cause"] == PARSED["root_cause"], "越界只影响归类，不许作废判决"


def test_other_class_is_not_a_conflict():
    """`其他` 不是「与规则桶冲突」，是「闭集里没有这一格」——

    把每条 `其他` 都报成证据冲突，冲突段就被噪声淹没，而它的价值就是「出现即要人看」。
    """
    merged = lv.apply_llm_verdict(RULE_VERDICT, _used(verdict_class=""),
                                  rule_bucket="依赖/安装(ImportError)")
    assert merged["conflicts"] == [], merged["conflicts"]


def test_missing_closed_set_flag_still_trusts_a_non_empty_label():
    """手工构造的 parsed（早于该字段引入）没有 `verdict_class_in_closed_set`。

    此时按「非空即算数」处理：否则所有老调用点会静默地把归类降级成 `其他`，
    冲突检测跟着一起失效 —— 那是无声的行为变更，比显式报错危险。
    """
    parsed = {k: v for k, v in PARSED.items()}
    assert "verdict_class_in_closed_set" not in parsed
    assert lv.projected_class(parsed) == PARSED["verdict_class"]


def test_projection_never_invents_a_class_for_an_unknown_case():
    """闭集覆盖不到时唯一的出路是 `其他`：不挑「最接近的」桶。"""
    assert lv.projected_class({"verdict_class": "性能未达标(benchmark)",
                               "verdict_class_in_closed_set": False}) == lv.OTHER_CLASS


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
