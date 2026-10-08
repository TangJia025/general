#!/usr/bin/env python3
"""证据打包的回归测试：行号可回映、终局行永不截断、摘要跨进程稳定。

为什么守这三条（实测教训，2026-10-08）：
  规则层的病不是「缺证据」——12 条错判里有 11 条的真值行**就在被扫描的尾部 1200 行内**，
  而是「首个命中正则胜出」把良性 WARNING 排到了终局行前面。换成 LLM 判决后，
  喂进去的证据若在截断/行号上出错，就会把同一个病以另一种形式复发：
    - 截断从头切 → 终局行（结论）先丢，模型只能抓中间的噪声；
    - 行号与原文对不上 → 「引用窗口外行号」这道抑制幻觉的闸门失效。
  另外证据摘要 sha 会被写进报告与判决缓存，跨进程不稳定就会让缓存永远 miss。

运行：python3 tests/test_llm_evidence.py      （无需 pytest，也兼容 pytest）
"""
import pathlib
import subprocess
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
sys.path.insert(0, str(BASE_DIR))

from forensics import llm_evidence as ev    # noqa: E402

# ---- 真实形态的日志片段（取自 2026-10-08 上游 nightly 失败 job 的窗口尾部）----
# 第 1~3 行是**干扰项**：引擎启动时的可选扩展缺失 WARNING，实测它正是规则层误判的来源
# （该桶 128 条里抽样 40 条，40/40 的 ImportError 字串都只出现在这类 WARNING 行）。
WARNING_NOISE = [
    "2026-10-08T04:11:02.1000000Z WARNING 10-08 04:11:02 [indexer_topk.py:29] Failed to import "
    "the DeepSelect extension (vllm._deepselect_C)",
    "2026-10-08T04:11:02.2000000Z WARNING 10-08 04:11:02 [utils.py:1200] No module named "
    "'vllm._deepselect_C'",
    "2026-10-08T04:11:02.3000000Z INFO 10-08 04:11:02 [core.py:88] Initializing engine",
]
PYTEST_TERMINAL = [
    "2026-10-08T04:28:31.5000000Z =========================== short test summary info "
    "===========================",
    "2026-10-08T04:28:31.6000000Z FAILED tests/e2e/common/single_node/test_single_node.py::"
    "test_single_node[Qwen3-30B-QuaRot] - AssertionError: some aisbench cases failed",
    "2026-10-08T04:28:31.7000000Z ================== 1 failed, 14 warnings in 1043.00s "
    "(0:17:22) ==================",
    "2026-10-08T04:28:32.0000000Z pytest exit code: ret=1",
]
BENCH_TERMINAL = [
    "2026-10-08T04:13:11.6426541Z [2026-10-08 04:13:11] [ERROR] [1/1] Benchmark failed: perf_1, "
    "reason: Performance verification failed. The current Output Token Throughput is 80.1246 ...",
]


def _log(*extra, tail_pad=6):
    """WARNING 干扰 + 可选终局块 + 若干填充行（模拟窗口里绝大部分是引擎日志）。"""
    lines = list(WARNING_NOISE)
    for i in range(4, 4 + tail_pad):
        lines.append(f"2026-10-08T04:11:0{i}.0000000Z INFO 10-08 04:11 [worker.py:{i}] step {i}")
    lines.extend(extra)
    return "\n".join(lines)


# ---------------- 行号与回映 ----------------

def test_line_numbers_are_one_based_and_roundtrip():
    text = _log(*PYTEST_TERMINAL)
    raw = text.splitlines()
    evidence = ev.build_evidence(text)
    assert evidence.line_numbers() == frozenset(range(1, len(raw) + 1)), \
        "未超预算时证据必须覆盖全部行"
    # 每个行号都能按 1-based 回映到原文 —— 行号错位会让引用校验误判
    for number, line in evidence.lines:
        assert line == raw[number - 1], f"L{number} 回映错位"


def test_rendered_block_keeps_line_numbers_with_original_text():
    text = _log(*PYTEST_TERMINAL)
    block = ev.render_evidence_block(ev.build_evidence(text))
    assert block.splitlines()[0].startswith("L1|"), block.splitlines()[0]
    assert "L1|" in block and "DeepSelect" in block, "证据块必须回显原文而不只是行号"


# ---------------- 终局行识别 ----------------

def test_terminal_lines_detected_for_pytest_and_benchmark():
    pytest_text = _log(*PYTEST_TERMINAL)
    benchmark_text = _log(*BENCH_TERMINAL)
    pytest_lines = ev.find_terminal_lines(pytest_text)
    benchmark_lines = ev.find_terminal_lines(benchmark_text)
    # pytest：摘要标题行、FAILED 行之外的最终计数行、退出码行都算终局
    assert len(pytest_lines) == 3, pytest_lines
    assert benchmark_lines == [10], benchmark_lines


def test_no_tests_ran_is_terminal():
    text = _log("2026-10-08T04:28:31.5000000Z ============================ no tests ran in 0.01s "
                "=============================")
    assert ev.find_terminal_lines(text) == [10]


def test_warning_noise_is_not_terminal():
    text = _log()
    assert ev.find_terminal_lines(text) == [], \
        "WARNING 干扰行不得被当成终局行 —— 那正是规则层误判的来源"


# ---------------- 截断不变式（本文件的核心）----------------

def _big_log(terminal_line=0, total=1000):
    """1000 行 × 约 100 字符；terminal_line>0 时把该行替换成 benchmark 终局行。"""
    lines = []
    for number in range(1, total + 1):
        if number == terminal_line:
            lines.append(BENCH_TERMINAL[0])
        else:
            lines.append(f"2026-10-08T04:11:02.0000000Z INFO padding line {number} "
                         + "x" * 60)
    return "\n".join(lines)


def test_no_truncation_when_under_budget():
    text = _log(*PYTEST_TERMINAL)
    evidence = ev.build_evidence(text)
    assert evidence.truncated() == (), evidence.truncated()


def test_budget_forces_truncation_from_the_middle():
    text = _big_log()
    evidence = ev.build_evidence(text, budget_tokens=600)
    spans = evidence.truncated()
    assert len(spans) == 1, f"应从中间挖一个区间，实际 {spans}"
    start, end = spans[0]
    assert start > 1 and end < evidence.total_lines, f"头尾都必须保留，实际省略了 {spans}"
    rendered = len(ev.render_evidence_block(evidence))
    assert rendered <= 600 * ev.CHARS_PER_TOKEN * 1.6, f"渲染后 {rendered} 字符，超预算太多"


def test_terminal_line_in_the_truncated_middle_is_forced_kept():
    """终局行落在会被省略的中段时，必须被强制找回 —— 丢了它这次判决就没有依据。"""
    text = _big_log(terminal_line=400)
    evidence = ev.build_evidence(text, budget_tokens=600)
    assert evidence.truncated(), "样本没有触发截断，这条测试就没在测东西"
    assert 400 in evidence.line_numbers(), \
        "终局行在截断区里被丢了 —— 预算必须为终局行让路"
    assert 400 in evidence.terminal_lines, evidence.terminal_lines
    block = ev.render_evidence_block(evidence)
    assert "Performance verification failed" in block, "终局行原文必须出现在证据块里"


def test_truncation_marker_rendered_with_omitted_range():
    text = _big_log()
    evidence = ev.build_evidence(text, budget_tokens=600)
    block = ev.render_evidence_block(evidence)
    start, end = evidence.truncated()[0]
    assert f"[省略 L{start}-L{end}]" in block, block[-500:]


def test_rendered_block_shrinks_when_budget_applied():
    text = _big_log()
    full = ev.render_evidence_block(ev.build_evidence(text))
    tight = ev.render_evidence_block(ev.build_evidence(text, budget_tokens=600))
    assert len(tight) < len(full) / 10, (len(tight), len(full))


# ---------------- 摘要稳定性 ----------------

def test_sha_stable_across_calls_and_processes():
    text = _log(*PYTEST_TERMINAL)
    first = ev.build_evidence(text).sha256
    second = ev.build_evidence(text).sha256
    assert first == second, "同一输入两次构建的 sha 必须相同"
    # 跨进程：sha 会被写进报告与判决缓存，不稳定就会让缓存永远 miss
    code = (f"import sys; sys.path.insert(0, {str(BASE_DIR)!r});"
            "from forensics import llm_evidence as e;"
            f"print(e.build_evidence({text!r}).sha256)")
    out = subprocess.run([sys.executable, "-B", "-c", code],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == first, f"跨进程 sha 不一致：{out.stdout.strip()} != {first}"


def test_sha_changes_when_content_changes():
    base = ev.build_evidence(_log(*PYTEST_TERMINAL)).sha256
    changed = ev.build_evidence(_log(*BENCH_TERMINAL)).sha256
    assert base != changed, "内容变了 sha 必须变，否则缓存会串味"


def test_sha_is_unaffected_by_line_text_being_identical():
    """同样的内容、同样的行数 → 同样的 sha（无时间戳/无集合序泄漏）。"""
    text = "\n".join(["same line"] * 50)
    assert ev.build_evidence(text).sha256 == ev.build_evidence(text).sha256
    assert ev.build_evidence(text, budget_tokens=5).sha256 == \
        ev.build_evidence(text, budget_tokens=5).sha256


# ---------------- 边界 ----------------

def test_empty_text_does_not_crash():
    evidence = ev.build_evidence("")
    assert evidence.lines == () and evidence.truncated() == ()
    assert evidence.total_lines == 0
    assert ev.render_evidence_block(evidence) == ""


def test_estimate_tokens_matches_measured_calibration():
    # 实测：1200 行窗口 36K~64K tokens，chars/token ≈ 2.6
    assert ev.estimate_tokens("x" * 2600) == 1000
    assert ev.estimate_tokens("") == 1, "空文本也要给下界，避免除零式的预算退化"


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
