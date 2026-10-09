#!/usr/bin/env python3
"""`eval/run_eval.py` 把判决写进 record 的口径（指标的唯一上游）。

为什么单独守这个文件：`llm_verdict.projected_class` 自己是对的（`test_llm_fallback.py` 守着），
但**投影有没有真的接到 record 上**是另一回事 —— 实测把那一行换回裸 `parsed["verdict_class"]`：
空串原样进了 `pred_class`（既不是桶也不是 `其他`）、`verdict_class_source` 被误标成 `"llm"`、
`declined_rate` 从 1.0 变成 0.0、`other_clusters` 直接空掉 —— **一条红测试都没有**。
也就是说「闭集覆盖不足」这个读数会静默消失，报表上看着一切正常。

三条口径（每条都有对应测试）：
  1. 空串 / 闭集外的归类 → `pred_class = 其他`、`source = "none"`（进留空率）；
  2. 闭集内的归类 → `source = "llm"`；
  3. **降级（`used=False`）退回规则桶的那条口径一字不改** —— 它等于「LLM 挂了线上会怎样」，
     改了这条，报表里就再也没有「退回规则」这个读数了。

运行：python3 tests/test_eval_wiring.py      （无需 pytest，也兼容 pytest）
"""
import pathlib
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
sys.path.insert(0, str(BASE_DIR))

from forensics import llm_verdict as lv              # noqa: E402
from eval import run_eval                            # noqa: E402

CASE = {"job_id": "a", "rule_bucket": "HCCL 集合通信失败", "rule_owner": "infra"}
CLOSED_SET = ("测试用例失败(pytest ret=1)", "依赖/安装(ImportError)")


def _records(parsed=None, *, used=True, reason=None, rule_bucket=None):
    """用一次判决跑通 `llm_records` —— 只换 `judge_case`，其余（窗口、record 组装）走真代码。"""
    case = dict(CASE)
    if rule_bucket is not None:
        case["rule_bucket"] = rule_bucket
    outcome = lv.LLMOutcome(used, parsed=parsed, fallback_reason=reason,
                            meta={"cited_lines": [1], "usage": {}, "elapsed": 0.0})

    saved = (run_eval.judge_case, run_eval.read_evidence_text, run_eval.build_evidence)
    run_eval.judge_case = lambda text, **kw: outcome
    run_eval.read_evidence_text = lambda path: pathlib.Path(path).name.split(".")[0]
    run_eval.build_evidence = lambda text, **kw: _LineNumbers()
    try:
        return run_eval.llm_records([case], {}, lambda _case: None, fixtures_dir="x",
                                    allowed_classes=CLOSED_SET, arm_name="flash",
                                    budget_tokens=1, max_tokens=1, timeout=1)
    finally:
        (run_eval.judge_case, run_eval.read_evidence_text,
         run_eval.build_evidence) = saved


class _LineNumbers:
    def line_numbers(self):
        return {1}


def _parsed(**overrides):
    parsed = {"root_cause": "性能未达标", "owner": "code", "confidence": "high",
              "verdict_class": "", "verdict_class_in_closed_set": False,
              "phenomenon": "性能未达标", "evidence_lines": [1]}
    parsed.update(overrides)
    return parsed


# ---------------- 投影必须真的接到 record 上 ----------------

def test_blank_class_becomes_other_and_counts_as_declined():
    """模型留空 = 闭集里没有贴合的格子。空串若原样进 `pred_class`，这个读数就消失了。"""
    record, = _records(_parsed(verdict_class=""))
    assert record["pred_class"] == lv.OTHER_CLASS, record["pred_class"]
    assert record["verdict_class_source"] == "none"
    assert run_eval.arm_metrics([record])["declined_rate"] == 1.0


def test_out_of_set_class_is_projected_not_passed_through():
    record, = _records(_parsed(verdict_class="性能未达标(benchmark)",
                               verdict_class_in_closed_set=False))
    assert record["pred_class"] == lv.OTHER_CLASS, "闭集外的名字不许冒充归类"
    assert record["verdict_class_source"] == "none"


def test_in_set_class_is_labelled_as_an_llm_class():
    record, = _records(_parsed(verdict_class="依赖/安装(ImportError)",
                               verdict_class_in_closed_set=True))
    assert record["pred_class"] == "依赖/安装(ImportError)"
    assert record["verdict_class_source"] == "llm"
    assert run_eval.arm_metrics([record])["declined_rate"] == 0.0


def test_phenomenon_and_root_cause_reach_the_record():
    """聚类与「结论样例」都读这两个键；不落进 record，`other_clusters` 就是空表。"""
    record, = _records(_parsed(verdict_class=""))
    assert record["phenomenon"] == "性能未达标"
    assert record["root_cause"] == "性能未达标"
    clusters = run_eval.arm_metrics([record])["other_clusters"]
    assert clusters and clusters[0]["phenomenon"] == "性能未达标"


# ---------------- 降级口径一字不改 ----------------

def test_degraded_records_still_score_against_the_rule_bucket():
    """降级 = 线上退回规则判决。这条口径改了，「上线后会怎样」这个读数就没了。"""
    record, = _records(None, used=False, reason="timeout")
    assert record["pred_class"] == CASE["rule_bucket"], "降级必须退回规则桶"
    assert record["pred_owner"] == CASE["rule_owner"]
    assert record["verdict_class_source"] == "rule_fallback"
    assert record["fallback_reason"] == "timeout"
    assert record["phenomenon"] == "" and record["root_cause"] == "", \
        "降级没有 LLM 结论可记，不许拿规则桶冒充"


def test_degraded_records_stay_out_of_the_declined_denominator():
    """停摆的调用没机会留空。算进去，留空率会被降级率稀释成一个无意义的数。"""
    metrics = run_eval.arm_metrics(_records(None, used=False, reason="timeout") +
                                   _records(_parsed(verdict_class="")))
    assert metrics["declined_rate"] == 1.0, metrics["declined_rate"]


# ---------------- 规则臂 ----------------

def test_rule_arm_never_reports_a_declined_rate():
    """规则臂没跑 LLM，报 0.0% 会被读成「模型从不留空」。"""
    record = run_eval._record(CASE, run_eval.RULE_ARM, pred_class=CASE["rule_bucket"],
                              pred_owner=CASE["rule_owner"], truths={})
    assert record["verdict_class_source"] == "rule", \
        "规则臂的 record 默认必须落在 `rule` 上，否则它会被算进留空率的分母"
    assert run_eval.em.arm_metrics([record])["declined_rate"] is None


def main():
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = []
    for name, func in tests:
        try:
            func()
            print(f"✅ {name}")
        except Exception as exc:
            failed.append(name)
            detail = str(exc) if isinstance(exc, AssertionError) else f"{type(exc).__name__}: {exc}"
            print(f"❌ {name}: {detail}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} 通过" + (f"，失败：{failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
