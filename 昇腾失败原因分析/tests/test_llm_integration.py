#!/usr/bin/env python3
"""LLM 判决层**接进生产**的回归测试（阶段 B 的调用点、缓存、预算、证据取用）。

为什么单独一个文件（`test_llm_fallback.py` / `test_llm_judge.py` 已经各自守好了一层）：
那些测试守的是**零件**，全绿也不能说明零件被接上了 —— 本仓刚吃过这个亏：
`apply_llm_verdict` 写得再对，它此前有 0 个生产调用者，整条链路**一次都没执行过**，
而 4 次评测跑下来 `arms=rule`、`verdicts=0`，报表上一切正常。这个文件守的是**接线**：

  1. **关开关 = 与今天逐字节一致**：这是这次上线的安全底线，也是唯一能回退的路径；
  2. **没 key 就启动即退出**，不是整轮逐案静默降级（那会跑完一轮、花了钱、还看不出来）；
  3. **范围门只放「未能定性」进来**：集群侧实证的 case 一个字都不碰；
  4. `root_cause_rule` 取在合并**之前**（取在之后会拿到模型那句，对照就成了自说自话）；
  5. **缓存命中不调模型**；键（prompt_version / scan_sha256 / model）任一变了必须重判；
  6. **超预算不发请求**且逐条记 `budget_exhausted`，不抛异常、不中断整批；
  7. sidecar 往返：写→读→sha 一致；sha 被改则拒绝使用（宁可退回规则，也不喂错证据）；
  8. **真实 `synthesize()` 输出 → `apply_llm_verdict`** 的端到端缺口 —— 此前零覆盖，
     `test_llm_fallback.py` 的 18 个调用点用的全是手写的 `RULE_VERDICT` 快照。

运行：python3 tests/test_llm_integration.py     （无需 pytest，也兼容 pytest）
"""
import gzip
import hashlib
import json
import os
import pathlib
import sys
import tempfile

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
sys.path.insert(0, str(BASE_DIR))

import npu_ci_forensics as F                          # noqa: E402
from forensics import llm_verdict as lv               # noqa: E402
from forensics.llm_client import FakeLLMClient        # noqa: E402
from forensics.report import render_case, synthesize  # noqa: E402

ALLOWED = ("依赖/安装(ImportError)", "测试用例失败(pytest ret=1)", "其他", "未分类")

# 一段能被 judge_case 接受的扫描窗（含终局计数行，让引用能自洽）
SCAN_TEXT = "\n".join([
    "WARNING Failed to import the vllm._deepselect_C extension",
    "collecting ...",
    "=== 1 failed, 12 passed in 45.2s ===",
    "FAILED tests/test_precision.py::test_single_node - AssertionError",
    "pytest exit code: ret=1",
])


def _payload(**overrides):
    data = {"root_cause": "业务侧用例真失败：单节点精度不达标", "owner": "code",
            "confidence": "high", "verdict_class": "测试用例失败(pytest ret=1)",
            "phenomenon": "用例真失败", "decisive_line": 4, "evidence_lines": [3, 4, 5]}
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def _parsed(**overrides):
    """过**真实解析器**（而不是手拼 dict）：`missing_evidence` 这类字段由它补默认值，
    手拼的快照一旦落后于解析器（字段增删）就会在 `apply_llm_verdict` 里 KeyError。"""
    return lv.parse_llm_verdict(_payload(**overrides), allowed_classes=ALLOWED)


def _used(**overrides):
    return lv.LLMOutcome(True, parsed=_parsed(**overrides), meta={"model": "test-model"})


class _Client(FakeLLMClient):
    """带 `model` 的假客户端：缓存键里的 model 一档要有东西可验。"""

    def __init__(self, model="test-model", **kwargs):
        super().__init__(**kwargs)
        self.model = model


def _case(**overrides):
    """一个能走到 synthesize「未能定性」那档的真实 case。

    ⚠️ `cluster.skipped=True` + 非未分类桶是**关键**：走的是「日志侧已定性为业务侧」那条
    分支，根因行才会是 `未能定性（仅有日志侧归类，需人工介入）`；换成有 pod 实证的形态，
    根因行就不是「未能定性」了，范围门压根不会开 —— 那样整份测试在验一个不可达的分支。
    """
    case = {
        "workflow": "schedule_nightly_test_a2.yaml", "job_name": "Nightly-A2 精度",
        "job_id": 12345, "run_id": 1, "link": "https://example.invalid/job/1",
        "bucket": "测试用例失败(pytest ret=1)", "owner": "code",
        "sig": "=== 1 failed, 12 passed ===", "step": "Run tests", "chip": "a2",
        "labels": [], "runner_name": None, "is_npu": True, "peer": None,
        "sig_source": "node0",
        "window": {"started_at": "2026-10-01T00:00:00Z",
                   "completed_at": "2026-10-01T00:05:00Z"},
        "cluster": {"skipped": True, "skip_reason": "日志侧已定性", "cluster_name": "EXAMPLE-CLUSTER",
                    "kubeconfig_path": "/tmp/example.kubeconfig", "pod_evidence": None,
                    "availability": None, "candidates": [], "not_obtained": [], "logs": []},
        "history": [], "related_issues": [],
    }
    case.update(overrides)
    return case


def _item(scan_dir, job_id=12345, **overrides):
    """一条 handoff 记录：含 sidecar 引用与 sha（阶段 A 的产物形态）。"""
    raw = SCAN_TEXT.encode("utf-8")
    name = f"job_{job_id}.txt.gz"
    with gzip.GzipFile(os.path.join(scan_dir, name), "wb", mtime=0) as handle:
        handle.write(raw)
    item = {"job_id": job_id, "job_name": "Nightly-A2 精度", "step": "Run tests",
            "chip": "a2", "scan_ref": name, "scan_sha256": hashlib.sha256(raw).hexdigest()}
    item.update(overrides)
    return item


def _call(item, client, scan_dir, cache_dir, **kwargs):
    params = dict(allowed_classes=ALLOWED, scan_dir=scan_dir, cache_dir=cache_dir,
                  budget_tokens=60000, max_tokens=4000, timeout=120.0)
    params.update(kwargs)
    return F.llm_outcome_for(item, client=client, **params)


# ---------------- 1. 关开关 = 与今天一致 ----------------

def _merge(case, verdict, item, client, scan_dir, cache_dir, **kwargs):
    """走**真实的调用点函数** —— 门、取值时机、合并都在这一个函数里，测它才算测接线。"""
    params = dict(allowed_classes=ALLOWED, scan_dir=scan_dir, cache_dir=cache_dir,
                  budget_tokens=60000, max_tokens=4000, timeout=120.0)
    params.update(kwargs)
    return F.merge_llm_verdict(case, verdict, item, llm_client=client, **params)


def test_gate_off_leaves_the_rule_verdict_untouched():
    """不打开 LLM 时，verdict 就是 synthesize 的**原样输出**，同一个对象、不多一个键。"""
    case = _case()
    verdict = synthesize(case)
    assert verdict["root_cause"].startswith(F.LLM_RULE_GATE_PREFIX), \
        "这个 case 没走到「未能定性」—— 后面的门内测试全都在验不可达分支"
    returned, outcome = _merge(case, verdict, {"job_id": 12345}, None, "/nonexistent", "/nonexistent")
    assert returned is verdict, "关着开关还重建了 verdict 对象"
    assert outcome is None
    assert "root_cause_rule" not in returned and "llm" not in returned


def test_gate_off_renders_no_extra_line():
    case = _case()
    case["verdict"] = synthesize(case)
    text = "\n".join(render_case(case, 1))
    assert "规则判断" not in text, "没走 LLM 判决却渲染出了对照行"


def test_gate_on_renders_the_rule_line_beside_the_model_line():
    with tempfile.TemporaryDirectory() as scan_dir, tempfile.TemporaryDirectory() as cache_dir:
        case = _case()
        item = _item(scan_dir)
        verdict, outcome = _merge(case, synthesize(case), item,
                                  _Client(text=_payload()), scan_dir, cache_dir)
        assert outcome is not None and outcome.used
        case["verdict"] = verdict
        text = "\n".join(render_case(case, 1))
        assert "（以上为 LLM 判决，此行仅作对照）" in text
        assert _parsed()["root_cause"] in text


# ---------------- 2. 没 key 就启动即退出 ----------------

def test_missing_key_is_a_hard_failure_not_a_silent_downgrade():
    saved = os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        class _Args:
            llm_model = "test-model"
        try:
            F.build_llm_client(_Args())
        except SystemExit as exc:
            assert "DEEPSEEK_API_KEY" in str(exc)
        else:
            raise AssertionError("没有 key 也把客户端造出来了 —— 那会整轮静默降级")
    finally:
        if saved is not None:
            os.environ["DEEPSEEK_API_KEY"] = saved


def test_present_key_builds_a_client_with_the_requested_model():
    saved = os.environ.get("DEEPSEEK_API_KEY")
    os.environ["DEEPSEEK_API_KEY"] = "sk-not-a-real-key"
    try:
        class _Args:
            llm_model = "deepseek-flash"
        client = F.build_llm_client(_Args())
        assert client.model == "deepseek-flash"
        assert client.api_key == "sk-not-a-real-key"
    finally:
        if saved is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = saved


# ---------------- 3. 范围门 ----------------

def test_cluster_evidenced_cases_never_enter_the_gate():
    """有集群侧实证的 case 连模型都不该调 —— 这就是用户选的「只判未能定性的」。

    断言必须落在**调用点**上：只断言 `synthesize` 输出的前缀是过不了假的
    （那是 synthesize 的性质，不是门的行为）。这里用 `call_count` 直接验「没发请求」。
    """
    # pod_evidence 必须带 `containers[].last_terminated_reason`：根因行的「集群侧实证：…」
    # 走的正是 `interpret_pod_evidence` 那一路，形状不对就退化成「未能定性」，
    # 门又会开 —— 那样这条测试是在自证一个不存在的防线（软失败，不报错）。
    case = _case(cluster={"skipped": False, "skip_reason": None, "cluster_name": "EXAMPLE-CLUSTER",
                          "kubeconfig_path": "/tmp/example.kubeconfig",
                          "pod_evidence": {"pod": "runner-abc", "namespace": "ns",
                                           "containers": [{"container": "runner",
                                                           "last_terminated_reason": "OOMKilled",
                                                           "last_terminated_exit_code": 137}],
                                           "init_containers": [], "abnormal_conditions": []},
                          "availability": None, "candidates": [], "not_obtained": [], "logs": []})
    verdict = synthesize(case)
    assert not verdict["root_cause"].startswith(F.LLM_RULE_GATE_PREFIX), \
        "这个 case 根因行不是「未能定性」，后面验的东西不可达"
    with tempfile.TemporaryDirectory() as scan_dir, tempfile.TemporaryDirectory() as cache_dir:
        client = _Client(text=_payload())
        returned, outcome = _merge(case, verdict, _item(scan_dir), client, scan_dir, cache_dir)
        assert outcome is None and client.call_count == 0, "集群侧实证的 case 发给模型了"
        assert "llm" not in returned and "root_cause_rule" not in returned
        assert returned["root_cause"] == verdict["root_cause"]


# ---------------- 4. root_cause_rule 取在合并之前 ----------------

def test_root_cause_rule_is_the_pre_merge_rule_text():
    """取在合并之后的话，对照行会变成模型那句 —— 看起来「有对照」，实际是自说自话。"""
    with tempfile.TemporaryDirectory() as scan_dir, tempfile.TemporaryDirectory() as cache_dir:
        case = _case()
        verdict = synthesize(case)
        before = verdict["root_cause"]
        merged, outcome = _merge(case, verdict, _item(scan_dir),
                                 _Client(text=_payload()), scan_dir, cache_dir)
        assert outcome is not None and outcome.used
        assert merged["root_cause_rule"] == before, "对照行不是合并前那句 —— 取晚了"
        assert merged["root_cause"] == _parsed()["root_cause"] != before


# ---------------- 5. 缓存 ----------------

def test_cache_hit_never_calls_the_client():
    with tempfile.TemporaryDirectory() as scan_dir, tempfile.TemporaryDirectory() as cache_dir:
        item = _item(scan_dir)
        first = _Client(text=_payload())
        outcome = _call(item, first, scan_dir, cache_dir)
        assert outcome.used and first.call_count == 1

        second = _Client(text=_payload())
        again = _call(item, second, scan_dir, cache_dir)
        assert again.used and second.call_count == 0, "缓存命中了却还是调了模型（白花钱）"
        assert again.parsed == outcome.parsed


def test_cache_key_invalidation_forces_a_redraw():
    for changed in ("prompt_version", "scan_sha256", "model"):
        with tempfile.TemporaryDirectory() as scan_dir, tempfile.TemporaryDirectory() as cache_dir:
            item = _item(scan_dir)
            first = _Client(text=_payload())
            _call(item, first, scan_dir, cache_dir)
            assert first.call_count == 1

            if changed == "scan_sha256":
                item = dict(item, scan_sha256="0" * 64)
                # sha 对不上 → 连证据都不读，直接降级（不给模型喂错文本）
                second = _Client(text=_payload())
                outcome = _call(item, second, scan_dir, cache_dir)
                assert not outcome.used and outcome.fallback_reason == "evidence_unavailable"
                assert second.call_count == 0
                continue

            saved = lv.PROMPT_VERSION
            other = _Client(model="other-model" if changed == "model" else "test-model",
                            text=_payload())
            try:
                if changed == "prompt_version":
                    lv.PROMPT_VERSION = "v99"
                    F.PROMPT_VERSION = "v99"
                outcome = _call(item, other, scan_dir, cache_dir)
            finally:
                lv.PROMPT_VERSION = saved
                F.PROMPT_VERSION = saved
            assert other.call_count == 1, f"{changed} 变了却吃到了旧缓存"


def test_degraded_outcomes_are_not_cached():
    """瞬时故障（限流/超时）不许被永久钉在某个 job 上 —— 那一条会永远退回规则结论。"""
    with tempfile.TemporaryDirectory() as scan_dir, tempfile.TemporaryDirectory() as cache_dir:
        item = _item(scan_dir)
        failing = _Client(error="http_429")
        outcome = _call(item, failing, scan_dir, cache_dir)
        assert not outcome.used and outcome.fallback_reason == "http_429"

        retry = _Client(text=_payload())
        again = _call(item, retry, scan_dir, cache_dir)
        assert again.used and retry.call_count == 1, "降级被缓存了，重试再也走不到模型"


# ---------------- 6. 预算 ----------------

def test_over_budget_degrades_without_calling_the_client():
    with tempfile.TemporaryDirectory() as scan_dir, tempfile.TemporaryDirectory() as cache_dir:
        client = _Client(text=_payload())
        outcome = _call(_item(scan_dir), client, scan_dir, cache_dir,
                        deadline=100.0, clock=lambda: 200.0)
        assert not outcome.used and outcome.fallback_reason == "budget_exhausted"
        assert client.call_count == 0, "超预算了还发了请求 —— 预算就没意义了"


def test_budget_exhaustion_is_summarized_not_swallowed():
    outcomes = [_used(),
                lv.LLMOutcome.degraded("budget_exhausted"),
                lv.LLMOutcome.degraded("budget_exhausted"),
                lv.LLMOutcome.degraded("evidence_unavailable")]
    summary = F.summarize_outcomes(outcomes)
    line = F.render_summary_line(summary)
    assert summary == {"total": 4, "used": 1, "degraded": 3,
                       "reasons": {"budget_exhausted": 2, "evidence_unavailable": 1}}, summary
    assert "budget_exhausted×2" in line


# ---------------- 7. sidecar 往返与 sha 守卫 ----------------

def test_scan_sidecar_roundtrip_matches_the_sha():
    with tempfile.TemporaryDirectory() as scan_dir:
        text, reason = F.load_scan_text(_item(scan_dir), scan_dir)
        assert reason is None and text == SCAN_TEXT


def test_missing_ref_or_tampered_sha_refuses_the_text():
    with tempfile.TemporaryDirectory() as scan_dir:
        item = _item(scan_dir)
        assert F.load_scan_text(dict(item, scan_ref=None), scan_dir) == (None, "evidence_unavailable")
        assert F.load_scan_text(dict(item, scan_sha256=None), scan_dir) == (None, "evidence_unavailable")
        # 文件在、但内容被换过（同名不同内容）→ 拒用，而不是把别人的现场喂给模型
        with gzip.GzipFile(os.path.join(scan_dir, item["scan_ref"]), "wb", mtime=0) as handle:
            handle.write(b"someone else's log")
        assert F.load_scan_text(item, scan_dir) == (None, "evidence_unavailable")


def test_allowed_classes_is_the_bucket_table_plus_the_two_fallbacks():
    payload = {"buckets": [{"label": "A", "owner": "code"}, {"label": "其他", "owner": "unknown"}]}
    assert F.llm_allowed_classes(payload) == ("A", "其他", "未分类")
    assert F.llm_allowed_classes({}) == ("其他", "未分类")


# ---------------- 8. 真实的 synthesize 输出 → apply_llm_verdict ----------------

def test_real_synthesize_output_merges_cleanly():
    """端到端：真实 synthesize 的 verdict 进 apply_llm_verdict，四个字段各就各位。

    这条覆盖的是此前**零覆盖**的地方：`test_llm_fallback.py` 的调用点全用手写快照，
    synthesize 真输出的形态（多了 official_leaf / precedent / needs_human 等键）没人验过。
    """
    case = _case()
    verdict = synthesize(case)
    rule_text = verdict["root_cause"]
    parsed = _parsed()
    merged = lv.apply_llm_verdict(dict(verdict, root_cause_rule=rule_text),
                                  _used(),
                                  rule_bucket=case["bucket"])
    assert merged["root_cause"] == parsed["root_cause"]
    assert merged["root_cause_rule"] == rule_text
    assert merged["confidence_rule"] == verdict["confidence"]
    assert merged["confidence"] == lv.CONFIDENCE_TEXT["high"]
    assert merged["llm"]["used"] is True and merged["llm"]["model"] == "test-model"
    assert merged["llm_class"] == parsed["verdict_class"]
    # 规则层原有的键一个都不能丢：它们要么进报告、要么进 JSON，掉了就是静默的信息缺失
    for field in ("official_leaf", "suggestions", "needs_human", "owner_from_cluster"):
        assert field in merged
    json.dumps(merged, ensure_ascii=False)


def test_real_synthesize_degraded_path_still_preserves_the_rule_text():
    case = _case()
    verdict = synthesize(case)
    rule_text = verdict["root_cause"]
    merged = lv.apply_llm_verdict(dict(verdict, root_cause_rule=rule_text),
                                  lv.LLMOutcome.degraded("evidence_unavailable"))
    assert merged["root_cause"] == rule_text
    assert merged["root_cause_rule"] == rule_text
    assert merged["confidence"] == lv.DEGRADED_CONFIDENCE
    assert merged["needs_human"] is True
    assert any("evidence_unavailable" in line for line in merged["basis"])


def main():
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = []
    for name, func in tests:
        try:
            func()
            print(f"✅ {name}")
        except Exception as exc:
            # 崩在某个用例上会掩盖后面所有用例：「套件整体崩溃」在证伪里看起来像「没红」
            failed.append(name)
            detail = str(exc) if isinstance(exc, AssertionError) else f"{type(exc).__name__}: {exc}"
            print(f"❌ {name}: {detail}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} 通过" + (f"，失败：{failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
