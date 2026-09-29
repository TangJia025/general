#!/usr/bin/env python3
"""runner 标签可用性核查的回归测试：全名匹配 vs 标签主干匹配。

为什么需要（实测踩坑，2026-09-28，`ascend-cn12-001-cluster`，同一份 `-A` pod 列表）：
    Cluster.md 登记的全名 `linux-aarch64-a3-800t-0-cn12-001` 前缀匹配 → **0 个**
    标签主干           `linux-aarch64-a3-800t-0-`            → **6 个**
        （4 个 `…-chlqk-runner-*` + 2 个 `…-{8位hex}-listener`）
即该标签族的 runner 当时**正在线**，而只按登记全名匹配的实现会写成
「未找到任何匹配 pod → 该标签此刻在本集群无 runner/listener」——
一个**与事实相反的假阴性**，且会把归因推向官方分类树的
`leaf_wait_label`（标签不存在）/ `leaf_runner_offline`（runner 未上线）。
根因是 Cluster.md 登记的后缀（`cn12-001`）与 pod 名实际用的后缀（`chlqk`）不一致：
对不上的是**注册表与实际命名**，不是 runner 掉线。这两件事的后续动作完全不同。

断言口径分两层，都落在「用户看到什么」这一层：
  ① 核查函数：登记正确时数字与旧口径**完全一致**（不许因为加了兜底就动正常路径）；
     全名落空而主干命中时，必须报「标签族有效 + 后缀对不上」并带上实测后缀变体；
     主干匹配不得把同前缀的其它工作负载（agent/daemon 之类）算成 runner。
  ② 报告层（用户真正读的那句话）：不得再出现「无 runner/listener」这种反向结论，
     且阴性结论必须交代**查过哪些匹配方式**（否则分不清「真没有」与「只按全名试过一次」）。

运行：python3 tests/test_runner_availability.py     （无需 pytest，也兼容 pytest）
"""
import pathlib
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR.parent))

import npu_ci_forensics as nf                                              # noqa: E402
from forensics import cluster_forensics as cf                             # noqa: E402
from forensics import report as report_mod                                # noqa: E402

REPO = "vllm-project/vllm-ascend"
CLUSTER = "ascend-cn12-001-cluster"
KUBECONFIG = "/fake/kubeconfig.yaml"
BASE = "linux-aarch64-a3-800t-0"                    # job 上报的展示名（= 标签主干）
FULL = "linux-aarch64-a3-800t-0-cn12-001"           # Cluster.md 登记的全名：后缀与实际不符
REGISTERED_NS = "vllm-project"

# --- 实测 pod 名（去掉无关项，只留形状）---
STEM_PODS = ["linux-aarch64-a3-800t-0-chlqk-runner-9cnln",
             "linux-aarch64-a3-800t-0-chlqk-runner-j4s88",
             "linux-aarch64-a3-800t-0-chlqk-runner-n9jps",
             "linux-aarch64-a3-800t-0-chlqk-runner-xp6m4",
             "linux-aarch64-a3-800t-0-848db74b-listener",
             "linux-aarch64-a3-800t-0-8678b6f8-listener"]
# 登记后缀真的对得上时才会出现的 pod 名（用于「登记正确」这一路）
EXACT_POD = "linux-aarch64-a3-800t-0-cn12-001-nlk5t-runner-q8np9"
# 主干再往后多一段数字的**别的**标签（`-800t-0` 与 `-800t-03` 是不同 runner 池）：
# 主干匹配若忘了带结尾 `-`，这条会被误算进来
DECOY_OTHER_POOL = "linux-aarch64-a3-800t-03-chlqk-runner-zzzzz"
# 同主干但既非 runner 也非 listener 的工作负载：主干匹配比全名宽，必须靠 runner/listener 标记挡掉
DECOY_NOT_RUNNER = "linux-aarch64-a3-800t-0-chlqk-agent-stack-k8s-755f6f85c5-smd8t"
# 完全无关的标签
DECOY_OTHER_LABEL = "linux-aarch64-cpu-4-cn12-001-nl4tw-runner-dfrvd"

JOB_START = "2026-09-28T11:55:00Z"
STEP_END = "2026-09-28T12:04:00Z"
# 失败当时承载本 job 的 pod 名（核查发生在它已被回收之后，故它不在当前 pod 列表里）
GONE_RUNNER = "linux-aarch64-a3-800t-0-chlqk-runner-a1b2c"


def _pod(name, namespace=REGISTERED_NS):
    return {"metadata": {"name": name, "namespace": namespace,
                         "creationTimestamp": "2026-09-28T11:56:00Z"},
            "status": {"phase": "Running", "startTime": "2026-09-28T11:56:00Z",
                       "containerStatuses": [{"name": "runner", "state": {"running": {}}}]}}


def _install_pods(pod_names, ok=True):
    """装一份 `-A` pod 列表。返回一个记录调用次数的列表，用于断言「只列举一次」。"""
    calls = []

    def fake_list_pods(path=None, namespace=None):
        calls.append(namespace)
        if not ok:
            return {"ok": False, "pods": [], "error": "connection refused"}
        return {"ok": True, "pods": [_pod(name) for name in pod_names], "error": None}

    cf.list_pods = fake_list_pods
    return calls


# ============ 第 1 层：核查函数自身 ============

def test_exact_match_keeps_old_numbers():
    """登记后缀对得上时，行为与旧实现**逐字段一致**（不许因为加了兜底就动正常路径）。"""
    _install_pods([EXACT_POD] + STEM_PODS + [DECOY_NOT_RUNNER, DECOY_OTHER_LABEL])
    got = cf.check_runner_availability(KUBECONFIG, FULL, [BASE])
    assert got["available"] is True, got
    assert got["match_kind"] == "全名", got["match_kind"]
    assert got["matched_pods"] == 1, f"全名命中时不该把主干上的其它 pod 算进来：{got['matched_pods']}"
    assert got["runners_online"] == 1 and got["listeners"] == 0, got
    assert got["suffix_variants"] == {}, f"没有走主干兜底，就不该有后缀统计：{got['suffix_variants']}"
    assert got["scopes_checked"] == ["全名"], got["scopes_checked"]


def test_suffix_mismatch_is_caught_by_stem():
    """实测场景：登记全名 0 命中，标签主干 6 命中 —— 结论必须是「标签族有效、后缀对不上」。

    旧实现（只按登记全名匹配）在这份 fixture 上给出 available=False，
    报告随后写成「该标签此刻在本集群无 runner/listener」——正是要防的假阴性。
    """
    _install_pods(STEM_PODS + [DECOY_OTHER_POOL, DECOY_NOT_RUNNER, DECOY_OTHER_LABEL])
    got = cf.check_runner_availability(KUBECONFIG, FULL, [BASE])
    assert got["available"] is True, (
        f"漏报了在线的 runner（这正是只按登记全名匹配的后果）：{got}")
    assert got["match_kind"] == "标签主干", got["match_kind"]
    assert got["matched_pods"] == 6, got["matched_pods"]
    assert got["runners_online"] == 4 and got["listeners"] == 2, got
    assert got["claimed_label"] == FULL, f"要能看出是查哪个全名落空的：{got['claimed_label']}"
    assert got["registered_suffix"] == "cn12-001", got["registered_suffix"]
    # 实测到的后缀变体必须原样带出：读者要看到「登记 cn12-001，实际 chlqk」
    assert got["suffix_variants"] == {"chlqk": 4, "848db74b": 1, "8678b6f8": 1}, got["suffix_variants"]
    assert got["scopes_checked"] == ["全名", "标签主干"], got["scopes_checked"]


def test_stem_match_does_not_swallow_other_pools_or_workloads():
    """主干匹配比全名宽，必须挡住两类污染：别的 runner 池（`-800t-03`）、非 runner 负载。

    去掉主干匹配里的 runner/listener 标记门禁，本用例即会红（agent-stack 会被算成 runner）。
    """
    _install_pods([DECOY_OTHER_POOL, DECOY_NOT_RUNNER])
    got = cf.check_runner_availability(KUBECONFIG, FULL, [BASE])
    assert got["available"] is False, f"把别的池/非 runner 工作负载算成了 runner：{got['samples']}"
    assert got["match_kind"] is None, got["match_kind"]


def test_negative_conclusion_records_both_scopes():
    """真没有时，结论里要留下「两级都查过」的痕迹 —— 阴性强度取决于查得多宽。"""
    _install_pods([DECOY_OTHER_POOL, DECOY_OTHER_LABEL])
    got = cf.check_runner_availability(KUBECONFIG, FULL, [BASE])
    assert got["available"] is False and got["matched_pods"] == 0, got
    assert got["scopes_checked"] == ["全名", "标签主干"], got["scopes_checked"]
    assert got["suffix_variants"] == {}, got["suffix_variants"]


def test_no_base_labels_keeps_backward_compatible_behaviour():
    """老的调用方（不传展示名）不做主干兜底：口径不变，避免影响别的消费方。"""
    _install_pods(STEM_PODS)
    got = cf.check_runner_availability(KUBECONFIG, FULL)
    assert got["available"] is False, got
    assert got["scopes_checked"] == ["全名"], got["scopes_checked"]


def test_single_list_pods_call():
    """两级匹配共用**同一份** pod 列表：快照窗口只有几十秒，不能为兜底再拉一次。"""
    calls = _install_pods(STEM_PODS)
    cf.check_runner_availability(KUBECONFIG, FULL, [BASE])
    assert calls == [None], f"`-A` 列举次数应为 1，实际 {len(calls)} 次：{calls}"


def test_unreachable_cluster_is_unchecked_not_negative():
    """连不上集群只能是「未取证」，绝不能变成「标签不存在」。"""
    _install_pods([], ok=False)
    got = cf.check_runner_availability(KUBECONFIG, FULL, [BASE])
    assert got["checked"] is False and got["available"] is False, got
    assert got["reason"], got


# ============ 第 2 层：报告里用户读到的那句话 ============

class FakeInfo:
    path = KUBECONFIG
    filename = "fake-ascend-cn12-001-cluster-kubeconfig.yaml"


class FakeRegistry:
    """只提供 resolve_exact_sessions 需要的四个查询。"""

    def resolve_by_label(self, repo, labels):
        return {"exact": [CLUSTER], "fuzzy": [], "all": [CLUSTER]}

    def kubeconfig_for(self, cluster_name):
        return FakeInfo()

    def namespace_for(self, repo, cluster_name):
        return REGISTERED_NS

    def full_labels_for(self, repo, cluster_name, reported_labels):
        return [FULL]


def _run_pipeline(pod_names):
    """走一遍真实的路径 B（pod 已回收 → 可用性核查），返回 (cluster_result, verdict)。

    用真实的 check_runner_availability 与真实的 ClusterSession，只把集群调用换成假 pod 列表 ——
    这样断言的是「最终报告怎么说」，而不是某个中间函数的返回值。
    """
    _install_pods(pod_names)
    cf.check_connectivity = lambda path: {"reachable": True, "server_version": "v1.29",
                                          "identity": "sa-test"}
    cf.pod_evidence = lambda item: {"pod": item["metadata"]["name"],
                                    "namespace": item["metadata"]["namespace"],
                                    "phase": item["status"]["phase"], "containers": []}
    nf.cluster_ops.check_connectivity = cf.check_connectivity
    nf.cluster_ops.pod_evidence = cf.pod_evidence
    args = type("Args", (), {"no_pod_logs": True, "repo": REPO})()
    case = {"job_id": 108909230397, "run_id": 36418103916, "labels": [BASE],
            "runner_name": GONE_RUNNER, "_repo": REPO,
            "failed_step_started_at": JOB_START, "failed_step_completed_at": STEP_END,
            "job_started_at": JOB_START, "job_completed_at": STEP_END}
    cluster = nf.step3_cluster_forensics(case, FakeRegistry(), {}, args, [])
    case["cluster"] = cluster
    return cluster, report_mod.synthesize(case)


def test_report_never_claims_no_runner_when_suffix_differs():
    """报告层：登记后缀对不上时，**不得**出现「无 runner/listener」这种反向结论。

    这一条正是本次缺陷的现场：报告写的是「未找到任何匹配 pod → 该标签此刻在本集群
    无 runner/listener」，而实际有 4 个 runner + 2 个 listener 在线。
    """
    cluster, verdict = _run_pipeline(STEM_PODS + [DECOY_NOT_RUNNER])
    text = "\n".join(verdict["basis"] + verdict.get("hints_requiring_human") or [])
    availability = cluster["availability"]
    assert availability and availability["available"], f"可用性核查结论丢了：{availability}"
    assert "未找到任何匹配 pod" not in text, f"仍然报「无 runner」：\n{text}"
    assert "确有" in text and "标签族有效" in text, f"没说明「标签族确实在线」：\n{text}"
    # 必须让读者看到「对不上的是后缀」以及具体后缀，而不是只给一个布尔值
    assert "chlqk" in text and "cn12-001" in text, f"没写明登记后缀与实际后缀：\n{text}"
    assert "不是** runner 未上线" in text or "不是 runner 未上线" in text, \
        f"没排除「runner 未上线」这个错误结论：\n{text}"
    # 登记信息本身有问题 → 必须转人工（改注册表，而不是去查 runner）
    assert any("Cluster.md" in hint for hint in verdict.get("hints_requiring_human") or []), \
        verdict.get("hints_requiring_human")


def test_report_detail_line_shows_match_kind_for_stem_match():
    """明细区（草稿正文）同样要写明匹配方式与后缀，供人复核。"""
    _install_pods(STEM_PODS)
    cf.check_connectivity = lambda path: {"reachable": True, "server_version": "v1.29",
                                          "identity": "sa-test"}
    nf.cluster_ops.check_connectivity = cf.check_connectivity
    cluster, _ = _run_pipeline(STEM_PODS)
    case = {"job_name": "test", "workflow": "schedule_nightly_test_a3.yaml", "link": "http://x",
            "step": "Wait for pods ready", "chip": "a3", "labels": [BASE],
            "cluster": cluster}
    text = "\n".join(report_mod.render_case(case, 1))
    assert "标签主干" in text and "chlqk" in text, f"明细区没交代匹配方式：\n{text}"
    assert "假阴性" in text, f"明细区没提示按全名匹配会假阴性：\n{text}"


def test_report_negative_case_states_scopes():
    """真没有时，报告要写明查过哪些匹配方式（否则读者以为只按全名试过一次）。"""
    cluster, verdict = _run_pipeline([DECOY_OTHER_POOL, DECOY_OTHER_LABEL])
    text = "\n".join(verdict["basis"])
    assert "未查到" in text, text
    assert "标签主干" in text, f"阴性结论没交代查过多宽：\n{text}"


def main():
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = []
    for name, func in tests:
        try:
            func()
            print(f"✅ {name}")
        except AssertionError as exc:
            failed.append(name)
            print(f"❌ {name}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} 通过"
          + (f"，失败：{failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
