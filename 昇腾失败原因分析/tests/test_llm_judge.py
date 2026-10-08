#!/usr/bin/env python3
"""判决编排的回归测试（**用假客户端，不出网**）。

编排层只做串接，所以这里守的是「每一步失败都翻译成一个具名原因」：
  - **缺证据时不许调模型**：白花一次 40K token 去判一段空日志，纯浪费；
  - **幻觉引用要落到原因里**（`cited_line_not_in_evidence:<n>`）—— 这是 prompt 纪律
    是否生效的直接读数，混进「判决成功」里就看不见了；
  - **未自洽（决定性行没在引用里）只记软问题**，不该作废整条判决：那是模型表述不严谨，
    不是幻觉，作废它反而抬高了降级率、用降级掩盖了真问题；
  - **超预算的 case 必须逐条记为 `budget_exhausted`**，不能静默丢掉 ——
    否则「跑了 10 条就没钱了」在报告里会变成「30 条都判了」，成本与覆盖率全错。

运行：python3 tests/test_llm_judge.py      （无需 pytest，也兼容 pytest）
"""
import gzip
import json
import pathlib
import sys
import tempfile

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
sys.path.insert(0, str(BASE_DIR))

from forensics import llm_judge as lj      # noqa: E402
from forensics.llm_client import FakeLLMClient, LLMResult    # noqa: E402

ALLOWED = ("依赖/安装(ImportError)", "测试用例失败(pytest ret=1)")

SCAN = "\n".join([
    "WARNING Failed to import the vllm._deepselect_C extension",       # L1 良性噪声
    "collecting ...",                                                  # L2
    "=== 1 failed, 12 passed in 45.2s ===",                            # L3 终局计数行
    "FAILED tests/test_precision.py::test_single_node - AssertionError",  # L4 终局明细
    "pytest exit code: ret=1",                                         # L5
])


def _payload(**overrides):
    data = {"root_cause": "业务侧用例真失败：精度不达标", "owner": "code",
            "confidence": "high", "verdict_class": "测试用例失败(pytest ret=1)",
            "phenomenon": "用例真失败", "decisive_line": 4, "evidence_lines": [3, 4, 5]}
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def _run(text=_payload(), scan=SCAN, **kwargs):
    client = FakeLLMClient(text=text)
    outcome = lj.judge_case(scan, client=client, allowed_classes=ALLOWED,
                            job_id="job-1", job_name="job-1", **kwargs)
    return outcome, client


# ---------------- 正常路径 ----------------

def test_success_carries_everything_the_eval_needs():
    outcome, client = _run()
    assert outcome.used and outcome.parsed["verdict_class"] == "测试用例失败(pytest ret=1)"
    meta = outcome.meta
    assert len(meta["evidence_sha256"]) == 64
    assert meta["prompt_version"] and meta["attempts"] == 1
    assert meta["usage"]["prompt_tokens"] == 1
    assert meta["evidence_lines"] == 5 and meta["evidence_total_lines"] == 5
    assert meta["evidence_truncated"] is False
    assert meta["citation_problems"] == [] and meta["job_id"] == "job-1"
    assert client.call_count == 1


def test_decision_line_on_a_warning_is_flagged_weak():
    """把 WARNING 当决定性行正是规则层误判的病征 —— 要能被统计，不能只看准确率。"""
    outcome, _ = _run(_payload(decisive_line=1, evidence_lines=[1, 3]))
    assert outcome.used and outcome.meta["weak_decisive_line"] is True


def test_decision_line_on_the_terminal_line_is_not_weak():
    outcome, _ = _run()
    assert outcome.meta["weak_decisive_line"] is False


def test_soft_problems_do_not_kill_the_verdict():
    outcome, _ = _run(_payload(decisive_line=5, evidence_lines=[3, 4]))
    assert outcome.used, "决定性行没在引用里只是表述不严谨，不该作废整条判决"
    assert "decisive_line_not_cited" in outcome.meta["citation_problems"]


def test_rule_hint_and_case_meta_reach_the_prompt():
    _, client = _run(rule_hint="依赖/安装(ImportError)")
    user = client.calls[0]["user"]
    assert "依赖/安装(ImportError)" in user and "job-1" in user
    assert "L3|=== 1 failed, 12 passed in 45.2s ===" in user, "证据块没有回显行号与原文"


# ---------------- 失败路径：每一条都具名 ----------------

def test_empty_evidence_does_not_call_the_model():
    outcome, client = _run(scan="")
    assert outcome.fallback_reason == "evidence_unavailable"
    assert client.call_count == 0, "空日志也调了模型 —— 白烧一次上下文"


def test_client_error_is_propagated_by_name():
    client = FakeLLMClient(error="http_429")
    outcome = lj.judge_case(SCAN, client=client, allowed_classes=ALLOWED)
    assert outcome.fallback_reason == "http_429" and client.call_count == 1


def test_unparseable_output_is_named():
    for text, reason in (("模型说这是依赖问题", "not_json"),
                         ("", "empty_content"),
                         ('{"root_cause":"x"}', "missing_field:owner")):
        outcome, _ = _run(text)
        assert outcome.fallback_reason == reason, text


def test_hallucinated_citation_is_a_named_fallback():
    outcome, _ = _run(_payload(evidence_lines=[3, 9999]))
    assert outcome.fallback_reason == "cited_line_not_in_evidence:9999"
    assert any("9999" in problem for problem in outcome.meta["citation_problems"])


def test_cited_lines_are_kept_even_when_the_gate_rejects_them():
    """幻觉率的分母是「模型引用的行」。被闸门拦下的那些如果只在成功时记录，
    幻觉率的全部来源就消失了，指标会变成恒等于 0 的摆设。"""
    outcome, _ = _run(_payload(evidence_lines=[3, 9999]))
    assert outcome.used is False
    assert 9999 in outcome.meta["cited_lines"]
    assert outcome.meta["raw_verdict_class"] == "测试用例失败(pytest ret=1)"


def test_raw_response_is_kept_for_replay():
    """`--replay` 靠它 $0 重算指标；不留原始文本，事后也分不清模型判错还是解析器判错。"""
    outcome, _ = _run()
    assert outcome.meta["raw_response"] == _payload()


def test_raw_response_is_kept_when_parsing_fails():
    outcome, _ = _run("模型说这是依赖问题")
    assert outcome.meta["raw_response"] == "模型说这是依赖问题"


def test_enum_violation_is_named():
    outcome, _ = _run(_payload(verdict_class="模型自创的桶"))
    assert outcome.fallback_reason == "bad_enum:verdict_class"


def test_failed_outcome_has_no_parsed_payload():
    outcome, _ = _run("乱码")
    assert outcome.parsed is None and outcome.used is False


# ---------------- 截断与终局行 ----------------

def _big_scan(terminal_line=1500, total=3000):
    lines = [f"INFO step {number} ok" for number in range(1, total + 1)]
    lines[terminal_line - 1] = "Performance verification failed"
    return "\n".join(lines)


def test_terminal_line_survives_truncation_and_reaches_the_model():
    client = FakeLLMClient(text=_payload(verdict_class="测试用例失败(pytest ret=1)",
                                         decisive_line=1500, evidence_lines=[1500]))
    outcome = lj.judge_case(_big_scan(), client=client, allowed_classes=ALLOWED,
                            budget_tokens=200)
    user = client.calls[0]["user"]
    assert "Performance verification failed" in user, \
        "超预算截断把终局行切掉了 —— 判据没了，模型只能去抓噪声行"
    assert "[省略 " in user, "截断处没有留下省略标记"
    assert outcome.used and outcome.meta["evidence_truncated"] is True


# ---------------- 批量与预算 ----------------

class Clock:
    def __init__(self, values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


def test_budget_exhaustion_is_recorded_per_case_not_dropped():
    cases = [{"job_id": f"job-{number}", "scan_text": SCAN} for number in range(3)]
    client = FakeLLMClient(text=_payload())
    outcomes = lj.judge_cases(cases, client=client, allowed_classes=ALLOWED,
                              budget_seconds=6, clock=Clock([0, 5, 8, 11]))
    assert len(outcomes) == 3, "超预算的 case 被静默丢掉了"
    assert outcomes[0].used is True
    assert [out.fallback_reason for out in outcomes[1:]] == ["budget_exhausted"] * 2
    assert client.call_count == 1, "超预算之后还在调模型"


def test_batch_forwards_case_metadata():
    cases = [{"job_id": "job-9", "scan_text": SCAN, "job_name": "nightly-A2",
              "chip": "A2", "rule_hint": "依赖/安装(ImportError)"}]
    client = FakeLLMClient(text=_payload())
    lj.judge_cases(cases, client=client, allowed_classes=ALLOWED)
    user = client.calls[0]["user"]
    assert "nightly-A2" in user and "A2" in user and "依赖/安装(ImportError)" in user


def test_no_budget_means_run_everything():
    cases = [{"job_id": str(number), "scan_text": SCAN} for number in range(4)]
    outcomes = lj.judge_cases(cases, client=FakeLLMClient(text=_payload()),
                              allowed_classes=ALLOWED)
    assert len(outcomes) == 4 and all(out.used for out in outcomes)


# ---------------- 摘要与读取 ----------------

def test_summary_counts_are_ordered_by_frequency():
    from forensics.llm_verdict import LLMOutcome
    outcomes = [LLMOutcome(True, parsed={}, meta={}), LLMOutcome.degraded("timeout"),
                LLMOutcome.degraded("timeout"), LLMOutcome.degraded("http_429"),
                LLMOutcome.disabled()]
    summary = lj.summarize_outcomes(outcomes)
    assert summary == {"total": 5, "used": 1, "degraded": 4,
                       "reasons": {"timeout": 2, "http_429": 1, "disabled": 1}}
    line = lj.render_summary_line(summary)
    assert "1 成功 / 4 降级" in line and "timeout×2" in line


def test_summary_line_on_empty_batch():
    assert "无 case" in lj.render_summary_line(lj.summarize_outcomes([]))


def test_read_evidence_text_handles_gzip_and_plain():
    with tempfile.TemporaryDirectory() as tmp:
        plain = pathlib.Path(tmp, "a.txt")
        plain.write_text("L1|hello", encoding="utf-8")
        packed = pathlib.Path(tmp, "b.txt.gz")
        with gzip.open(packed, "wt", encoding="utf-8") as handle:
            handle.write("L1|hello")
        assert lj.read_evidence_text(plain) == "L1|hello"
        assert lj.read_evidence_text(packed) == "L1|hello", \
            "冻结集是 .gz，读不出来评测就跑不起来"


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
