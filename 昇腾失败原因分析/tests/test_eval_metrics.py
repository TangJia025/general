#!/usr/bin/env python3
"""评测指标的回归测试：**手算值对照** + 口径守卫 + 冻结集 sha 守卫。

为什么每条都要守（都是「数字看起来对但其实错」的形态）：
  - **未裁定不当成对或错**：把没裁定的算进分母，准确率会凭空变一个数，看不出来；
  - **幻觉率的分母是「引用行」**：换成日志总行数，这个数字会永远接近 0，等于没测；
  - **弱决定性行率只看真正用了 LLM 的 case**：把降级的也算进去，降级率高时它反而变好；
  - **bootstrap 给定 seed 必须确定**：不确定的话，两次评测的区间不同，谁也没法复核；
  - **fixture sha 守卫**：冻结集被改一字节而 sha 没更新，两次评测的数字就不可比，
    而报告里完全看不出来。

运行：python3 tests/test_eval_metrics.py      （无需 pytest，也兼容 pytest）
"""
import gzip
import pathlib
import sys
import tempfile

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
sys.path.insert(0, str(BASE_DIR))

from forensics import eval_metrics as em    # noqa: E402

# ---------------- 手算样本 ----------------

# 4 条已裁定 + 1 条未裁定；3 对 1 错
PAIRS = [("A", "A"), ("A", "A"), ("B", "B"), ("A", "B"), ("C", None)]


def _record(job_id="j1", pred="A", truth="A", owner=("code", "code"), stratum="normal",
            cited=(1, 2), allowed=(1, 2, 3), used=True, weak=False, reason=None,
            usage=None, elapsed=0.0, expressible=None):
    return {"job_id": job_id, "arm": "llm", "pred_class": pred, "truth_class": truth,
            "pred_owner": owner[0], "truth_owner": owner[1], "stratum": stratum,
            "truth_expressible": expressible,
            "cited": list(cited), "allowed": list(allowed), "used": used,
            "weak_decisive": weak, "fallback_reason": reason,
            "usage": usage if usage is not None else {"prompt_tokens": 100,
                                                      "completion_tokens": 20},
            "elapsed": elapsed}


# ---------------- 一致率 ----------------

def test_unadjudicated_cases_are_excluded_not_counted():
    detail = em.agreement_detail(PAIRS)
    assert detail == {"n": 4, "agree": 3, "rate": 0.75, "unadjudicated": 1}, detail


def test_agreement_of_nothing_is_none_not_zero():
    assert em.agreement([("A", None)]) is None, "「没数据」不是「全错」"
    assert em.agreement([]) is None


def test_confusion_counts_truth_to_prediction():
    assert em.confusion(PAIRS) == {"A=>A": 2, "B=>B": 1, "B=>A": 1}
    assert em.owner_confusion([("infra", "code"), ("infra", "code"), ("code", "code")]) \
        == {"code=>infra": 2, "code=>code": 1}


# ---------------- 幻觉率与弱决定性行 ----------------

def test_hallucination_rate_counts_cited_lines_not_log_lines():
    records = [_record(cited=(1, 2, 9999), allowed=(1, 2, 3)),
               _record(cited=(3,), allowed=(1, 2, 3))]
    assert em.hallucination_rate(records) == 0.25, "1 条窗口外的引用 / 4 条引用"


def test_hallucination_rate_is_none_when_nothing_was_cited():
    assert em.hallucination_rate([_record(cited=(), used=False, reason="timeout")]) is None


def test_weak_decisive_rate_ignores_degraded_records():
    records = [_record(weak=True), _record(weak=False), _record(weak=False),
               _record(used=False, weak=True, reason="timeout")]
    assert em.weak_decisive_rate(records) == 1 / 3, \
        "降级的 case 没有决定性行可言，把它算进分母会让降级率高时这个指标反而变好"
    assert em.weak_decisive_rate([_record(used=False, weak=True, reason="timeout")]) is None


def test_degrade_rate_and_reasons():
    records = [_record(), _record(used=False, reason="timeout"),
               _record(used=False, reason="timeout"),
               _record(used=False, reason="cited_line_not_in_evidence:9999")]
    assert em.degrade_rate(records) == 0.75
    assert em.fallback_reasons(records) == {
        "timeout": 2, "cited_line_not_in_evidence:9999": 1}


# ---------------- Kappa：准确率的天花板 ----------------

def test_kappa_hand_computed():
    pairs = [("A", "A"), ("A", "A"), ("B", "B"), ("B", "A"), ("A", "B")]
    # po=0.6；pe=0.6*0.6+0.4*0.4=0.52；(0.6-0.52)/(1-0.52)=0.1666…
    assert abs(em.cohens_kappa(pairs) - 0.08 / 0.48) < 1e-9


def test_kappa_perfect_and_undefined_cases():
    assert em.cohens_kappa([("A", "A"), ("B", "B")]) == 1.0
    assert em.cohens_kappa([("A", "B"), ("A", "B")]) == 0.0, \
        "机会一致率为 1 时 Kappa 无定义，不能除零崩掉"
    assert em.cohens_kappa([]) is None


# ---------------- 区间 ----------------

def test_bootstrap_ci_is_deterministic_for_a_seed():
    # 用**连续**取值：0/1 数据的自助分布是量化的，分位数会落在同一格上，
    # 于是「不设 seed」也照样同值 —— 那样这条测试什么也没守住（证伪时才发现）。
    values = [0.13, 0.71, 0.42, 0.90, 0.05, 0.60, 0.33, 0.88, 0.21, 0.55]
    first = em.bootstrap_ci(values, n=500, seed=7)
    assert first == em.bootstrap_ci(values, n=500, seed=7), "同一 seed 两次跑出不同区间"
    assert first != em.bootstrap_ci(values, n=500, seed=8), \
        "换个 seed 结果完全一样 —— 说明 seed 根本没接进去，上面那条也就没测到东西"


def test_bootstrap_ci_brackets_the_estimate_and_stays_in_range():
    values = [1, 1, 0, 1, 0, 1, 1, 1, 0, 1]
    low, high = em.bootstrap_ci(values, n=1000, seed=0)
    assert 0.0 <= low <= 0.8 <= high <= 1.0
    assert em.bootstrap_ci([1, 1, 1], n=200, seed=0) == (1.0, 1.0)
    assert em.bootstrap_ci([]) == (None, None)


def test_agreement_ci_uses_only_scored_pairs():
    low, high = em.agreement_ci([("A", "A"), ("A", "A"), ("B", "B"), ("A", "B"),
                                 ("C", None)], n=500, seed=1)
    assert 0.0 <= low <= 0.75 <= high <= 1.0
    assert em.agreement_ci([("A", None)], n=10) == (None, None)


def test_percentile_hand_computed():
    assert em.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert em.percentile([1, 2, 3, 4], 0.0) == 1.0
    assert em.percentile([1, 2, 3, 4], 1.0) == 4.0
    assert em.percentile([], 0.5) is None


# ---------------- 分层 ----------------

def test_by_stratum_separates_defect_from_normal():
    rows = [_record(stratum="defect", pred="A", truth="B"),
            _record(stratum="defect", pred="A", truth="A"),
            _record(stratum="normal", pred="A", truth="A")]
    strata = em.by_stratum(rows)
    assert strata["defect"]["n"] == 2 and strata["defect"]["rate"] == 0.5
    assert strata["normal"]["rate"] == 1.0


def test_by_stratum_keeps_unadjudicated_in_n_but_not_in_rate():
    rows = [_record(pred="A", truth=None, stratum="defect")]
    assert em.by_stratum(rows)["defect"] == {"n": 1, "scored": 0, "agree": 0,
                                             "rate": None, "unadjudicated": 1}, \
        "「这一层有几条」与「其中算了几条」必须是两个数，合并会让未裁定悄悄进分母"


# ---------------- 覆盖度分半：可表达 vs 不可表达 ----------------

def test_coverage_split_separates_ordering_defects_from_coverage_gaps():
    """可表达 3 对 1 错；不可表达全错 —— 两半必须各算各的。

    不分半的话整体是 3/7 = 42.9%，读起来像「规则只有四成准」；
    分开才看得出「闭集里有正确桶的那一半是 75%，没有正确桶的那一半是 0%」——
    后者换判决器也修不好，得先加桶。
    """
    records = [
        _record("e1", pred="A", truth="A", expressible=True),
        _record("e2", pred="B", truth="B", expressible=True),
        _record("e3", pred="A", truth="A", expressible=True),
        _record("e4", pred="B", truth="A", expressible=True),
        _record("n1", pred="C", truth="性能未达标(benchmark)", expressible=False),
        _record("n2", pred="C", truth="性能未达标(benchmark)", expressible=False),
        _record("n3", pred="C", truth="性能未达标(benchmark)", expressible=False),
    ]
    split = em.coverage_split(records)
    assert split["expressible"] == {"n": 4, "agree": 3, "rate": 0.75,
                                    "unadjudicated": 0}, split["expressible"]
    assert split["not_expressible"] == {"n": 3, "agree": 0, "rate": 0.0,
                                        "unadjudicated": 0}, split["not_expressible"]
    assert split["unmarked"]["n"] == 0


def test_coverage_split_never_files_an_unmarked_record_as_expressible():
    """没标 closed_set_expressible 的记录必须落在 unmarked 里。

    若默认归到「可表达」，覆盖缺口会被算成排序缺陷 —— 正是我们要分清的那两件事。
    """
    split = em.coverage_split([_record("x", pred="A", truth="A")])
    assert split["expressible"]["n"] == 0
    assert split["unmarked"] == {"n": 1, "agree": 1, "rate": 1.0, "unadjudicated": 0}


# ---------------- 成本 ----------------

def test_cost_summary_sums_tokens_and_reports_tail_latency():
    records = [_record(usage={"prompt_tokens": 100, "completion_tokens": 10}, elapsed=1.0),
               _record(usage={"prompt_tokens": 300, "completion_tokens": 30}, elapsed=3.0),
               _record(used=False, reason="timeout", usage={}, elapsed=5.0)]
    cost = em.cost_summary(records)
    assert cost["calls"] == 2, "降级的调用没有 usage，不该算进 token 成本"
    assert cost["prompt_tokens"] == 400 and cost["total_tokens"] == 440
    assert cost["elapsed_p50"] == 3.0 and cost["elapsed_p95"] == 4.8, cost
    assert cost["elapsed_total"] == 9.0


# ---------------- 整臂与渲染 ----------------

def test_arm_metrics_has_every_documented_key():
    records = [_record(stratum="defect", pred="A", truth="B"),
               _record(cited=(9,), stratum="defect")]   # 引用 3 行，其中 1 行在窗口外
    metrics = em.arm_metrics(records, n=100, seed=0)
    for key in ("n", "agreement", "agreement_ci", "owner_agreement", "owner_ci",
                "confusion", "owner_confusion", "hallucination_rate",
                "weak_decisive_rate", "degrade_rate", "fallback_reasons",
                "by_stratum", "coverage_split", "cost"):
        assert key in metrics, key
    assert abs(metrics["hallucination_rate"] - 1 / 3) < 1e-9
    assert metrics["coverage_split"]["unmarked"]["n"] == 2, \
        "没标可表达性的样本必须落在 unmarked，不能默认归到可表达"


def test_arm_metrics_output_is_json_serializable():
    import json
    json.dumps(em.arm_metrics([_record()]), ensure_ascii=False)


def test_render_arm_table_shows_all_arms_and_marks_missing_truth():
    arms = {"规则（基线）": em.arm_metrics([_record(pred="A", truth=None)], n=50, seed=0),
            "deepseek-flash": em.arm_metrics([_record(pred="A", truth="A")], n=50, seed=0)}
    table = em.render_arm_table(arms)
    assert "规则（基线）" in table and "deepseek-flash" in table
    assert "—（无真值）" in table
    assert "100.0%" in table


# ---------------- 冻结集 sha 守卫 ----------------

def test_fixture_sha_guard_catches_a_tampered_fixture():
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp, "111.txt.gz")
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write("L1|原始窗口文本")
        case = {"job_id": "111", "scan_sha256": em.sha256_text("L1|原始窗口文本")}
        assert em.verify_fixtures([case], tmp) == []

        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write("L1|被改动过的文本")
        problems = em.verify_fixtures([case], tmp)
        assert len(problems) == 1 and "被改动" in problems[0]


def test_fixture_sha_guard_reports_missing_files():
    with tempfile.TemporaryDirectory() as tmp:
        problems = em.verify_fixtures([{"job_id": "404", "scan_sha256": "x"}], tmp)
        assert problems == ["缺 fixture：404"]


def test_sha_is_stable_across_processes_for_the_same_text():
    assert em.sha256_text("abc") == em.sha256_text("abc")
    assert em.sha256_text("abc") != em.sha256_text("abd")


# ---------------- 日志格式闸门 ----------------

# 线上取法（gh api jobs/{id}/logs）：逐行带 GHA 前缀，首行还带 BOM
PRODUCTION_FORMAT = ("﻿2026-10-08T08:10:57.6240163Z Current runner version: '2.337.0'\n"
                     "2026-10-08T08:10:57.6247420Z Runner name: 'linux-aarch64'\n"
                     "2026-10-08T08:12:09.1234567Z [INFO] start\n")
# 另一种格式（harness 自带时间戳）：只有首行有 GHA 前缀
OTHER_FORMAT = ("﻿2026-09-30T17:09:39.2778938Z Current runner version: '2.337.0'\n"
                "Runner name: 'linux-aarch64'\n"
                "[2026-09-30 17:20:03] [INFO] External DP server command node=1 rank=11\n")


def test_timestamps_per_line_tells_the_two_log_formats_apart():
    assert em.timestamps_per_line(PRODUCTION_FORMAT) == 1.0
    assert em.timestamps_per_line(OTHER_FORMAT) == 1 / 3, \
        "另一种格式只有首行带前缀，按它切不出步骤时间窗"
    assert em.timestamps_per_line("") == 0.0


def test_log_format_usable_rejects_the_other_format():
    assert em.log_format_usable(PRODUCTION_FORMAT)[0] is True
    assert em.log_format_usable(OTHER_FORMAT)[0] is False, \
        "放行另一种格式 → 窗口静默退化成全局尾部窗口，评测输入与线上不是同一个东西"
    assert em.log_format_usable("")[0] is False


# ---------------- 规则层的弱决定性行（整行判，不看片段） ----------------

BUCKETS = [(r'No module named\s+\S+', "依赖/安装(ImportError)", "code"),
           (r'=+[^\n]*\d+ failed[^\n]*=+', "测试用例失败(pytest ret=1)", "code")]

# 真实形态：GHA 每行带 31 字符时间戳前缀，WARNING 在命中处**之前** 60 多字符的位置
RETRY_LINE = ("2026-10-08T08:12:09.1234567Z WARNING Failed to import the extension: "
              "No module named 'vllm._deepselect_C'")


def test_rule_weak_decisive_judges_the_whole_line_not_the_snippet():
    label, weak = em.rule_weak_decisive(RETRY_LINE, BUCKETS)
    assert label == "依赖/安装(ImportError)" and weak is True, \
        "拿 ±30 字片段判会落在时间戳里，「WARNING」看不到，指标会恒为 0"


def test_rule_weak_decisive_is_false_for_a_real_verdict_line():
    text = ("2026-10-08T08:12:09.1234567Z ============ 3 failed, 12 passed in 45.2s ============\n"
            "2026-10-08T08:12:09.1234567Z FAILED tests/test_x.py::test_y - AssertionError")
    label, weak = em.rule_weak_decisive(text, BUCKETS)
    assert label == "测试用例失败(pytest ret=1)" and weak is False


def test_first_match_line_returns_none_when_nothing_matches():
    assert em.first_match_line("2026-10-08T08:12:09.1Z all good", BUCKETS) == (None, "")


# ---------------- 分层代理（只用于采样） ----------------

def test_stratum_of_flags_rule_misses_as_the_defect_layer():
    assert em.stratum_of("测试用例失败(pytest ret=1)", "依赖/安装(ImportError)") == "defect"
    assert em.stratum_of("断言失败", "断言失败") == "normal"
    assert em.stratum_of(None, "断言失败") == "undetermined", \
        "没有终局行的 case 分不了层，不能塞进缺陷层充数"


def test_read_jsonl_tolerates_missing_file_and_blank_lines():
    with tempfile.TemporaryDirectory() as tmp:
        assert em.read_jsonl(pathlib.Path(tmp, "nope.jsonl")) == []
        path = pathlib.Path(tmp, "a.jsonl")
        path.write_text('{"a": 1}\n\n{"b": 2}\n', encoding="utf-8")
        assert em.read_jsonl(path) == [{"a": 1}, {"b": 2}]


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
