#!/usr/bin/env python3
"""pod 查找范围（namespace）的回归测试。

为什么需要这组测试（实测踩坑，2026-09-28，live job 108916075758）：
    Cluster.md 把 `vllm-project/vllm-ascend` 登记到**项目共享** namespace `vllm-project`，
    该 namespace 能正常列举、里面有 32 个 pod，唯独没有本 job 的 NPU runner ——
    而 runner pod 实际在仓库名派生的 `vllm-project-vllm-ascend` 里。
    旧实现 `if scoped["ok"]: return scoped` 把「namespace 能列举」当成了「namespace 正确」，
    于是既不查派生 namespace、也不回退 `-A`，最终报告写成
    「未取得 pod 实证（历史失败的 pod 多已回收）」——
    而那个 pod 当时已运行 9 分钟、活得好好的。这是**假阴性 + 假理由**，比没查更糟。

断言口径刻意选在「用户看到什么」这一层：pod 实证必须拿到，理由是「pod 多已回收」时必须是
真的查遍了所有范围。这样即使将来重构查询代码，只要结论退化成假阴性就会红。

运行：python3 tests/test_namespace_lookup.py     （无需 pytest，也兼容 pytest）
"""
import pathlib
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
sys.path.insert(0, str(BASE_DIR))

import npu_ci_forensics as nf                                          # noqa: E402
from forensics import cluster_forensics as cluster_ops                 # noqa: E402

REPO = "vllm-project/vllm-ascend"
FULL_LABEL = "linux-aarch64-a3-800t-0-cn12-001"      # Cluster.md 登记的（带 cn12-001 后缀）
RUNNER_NAME = "linux-aarch64-a3-800t-0-chlqk-runner-9cnln"   # GitHub 报的真实 runner pod 名
JOB_START = "2026-09-28T11:55:00Z"
STEP_END = "2026-09-28T12:04:00Z"

# 实测的三个 namespace 与它们的内容（去掉无关 pod，只留形状）
REGISTERED_NS = "vllm-project"                        # 32 个 pod，但没有本 job 的 runner
DERIVED_NS = "vllm-project-vllm-ascend"               # 本 job 的 runner 在这里
OTHER_NS = "vllm-project-vllm-omni"


def pod(name, namespace, start="2026-09-28T11:56:00Z", phase="Running"):
    return {"metadata": {"name": name, "namespace": namespace, "creationTimestamp": start},
            "status": {"phase": phase, "startTime": start,
                       "containerStatuses": [{"name": "runner", "state": {"running": {}}}]}}


# 登记 namespace 里的 pod：确实是本项目的 CPU runner，但不是本 job 的
REGISTERED_PODS = [pod(f"linux-aarch64-cpu-4-cn12-001-nl4tw-runner-dfrvd", REGISTERED_NS),
                   pod("linux-aarch64-a3-agent-stack-k8s-755f6f85c5-smd8t", REGISTERED_NS)]
# 真正承载本 job 的 pod
JOB_PODS = [pod(RUNNER_NAME, DERIVED_NS), pod(f"{RUNNER_NAME}-workflow", DERIVED_NS)]
FOREIGN_PODS = [pod("linux-aarch64-a3-800t-0-other-runner-zzzzz", OTHER_NS)]


class FakeInfo:
    path = "/fake/kubeconfig.yaml"
    filename = "fake-ascend-cn12-001-cluster.yaml"


class FakeRegistry:
    """只提供 resolve_exact_sessions 用到的三个查询。"""

    def resolve_by_label(self, repo, labels):
        return {"exact": ["ascend-cn12-001-cluster"], "fuzzy": [], "all": ["ascend-cn12-001-cluster"]}

    def kubeconfig_for(self, cluster_name):
        return FakeInfo()

    def namespace_for(self, repo, cluster_name):
        return REGISTERED_NS                      # Cluster.md 的登记值：项目共享 namespace

    def full_labels_for(self, repo, cluster_name, reported_labels):
        return [FULL_LABEL]


def _install_fakes(extra_pods=(), fail_namespaces=(), derived_pods=None):
    """装假集群：按 namespace 返回不同内容，并把实际发起的列举调用记录下来。

    derived_pods 可覆盖派生 namespace 里的内容 —— 用来造「两个 namespace 都没有、只有全量里才有」
    这种必须退到 -A 的场景。
    """
    calls = []
    derived = JOB_PODS if derived_pods is None else list(derived_pods)

    def fake_list_pods(path, namespace=None):
        calls.append(namespace)
        if namespace in fail_namespaces:
            return {"ok": False, "pods": [], "error": f"namespace {namespace} 无权限"}
        if namespace is None:
            return {"ok": True, "pods": REGISTERED_PODS + derived + FOREIGN_PODS + list(extra_pods),
                    "error": None}
        listing = {REGISTERED_NS: REGISTERED_PODS, DERIVED_NS: derived,
                   OTHER_NS: FOREIGN_PODS}.get(namespace, [])
        return {"ok": True, "pods": list(listing), "error": None}

    nf.cluster_ops.list_pods = fake_list_pods
    nf.cluster_ops.check_connectivity = lambda path: {"reachable": True, "server_version": "v1.29",
                                                      "identity": "sa-test"}
    nf.cluster_ops.check_runner_availability = lambda path, label, base_labels=(): {
        "available": True, "checked": True, "snapshot_only": True, "matched_pods": 1,
        "runners_online": 1, "listeners": 0, "namespaces": [REGISTERED_NS], "samples": [],
        "match_kind": "全名", "claimed_label": label, "scopes_checked": ["全名"],
        "suffix_variants": {}, "registered_suffix": None}
    nf.cluster_ops.pod_evidence = lambda item: {
        "pod": item["metadata"]["name"], "namespace": item["metadata"]["namespace"],
        "phase": item["status"]["phase"], "start_time": item["status"]["startTime"],
        "created_time": item["metadata"]["creationTimestamp"],
        "containers": [{"container": "runner", "state": "running", "exit_code": None,
                        "last_terminated_reason": None, "restart_count": 0}]}
    return calls


def _case():
    return {"job_id": 108916075758, "run_id": 36418103916, "labels": ["linux-aarch64-a3-800t-0"],
            "runner_name": RUNNER_NAME, "_repo": REPO,
            "failed_step_started_at": JOB_START, "failed_step_completed_at": STEP_END,
            "job_started_at": JOB_START, "job_completed_at": None}


class Args:
    repo = REPO
    no_pod_logs = True          # 本测试不关心日志，只关心 pod 是否被找到


def _run(extra_pods=(), fail_namespaces=(), derived_pods=None):
    calls = _install_fakes(extra_pods, fail_namespaces, derived_pods)
    got = nf.step3_cluster_forensics(_case(), FakeRegistry(), {}, Args, [])
    return got, calls


def test_pod_in_derived_namespace_is_found():
    """本 job 的 pod 在派生 namespace 里时必须找到 —— 这是被实测漏掉的那一个。"""
    got, calls = _run()
    assert got["pod_evidence"] is not None, (
        "没找到 pod（旧实现就是这样：登记 namespace 能列举就停手了）—— "
        f"实际查过的范围：{calls}")
    assert got["pod_evidence"]["pod"] == RUNNER_NAME
    assert got["match_kind"] == "runner_name 精确匹配", got["match_kind"]
    assert got["pod_evidence"]["namespace"] == DERIVED_NS
    # 命中即停：不该为了「多查一点」去拉全量（实测 3.77s vs 0.89s）
    assert None not in calls, f"命中后仍拉了全量 pod 列表：{calls}"


def test_registered_namespace_is_still_first():
    """顺序不变：先查 Cluster.md 登记的 namespace（多数情况一次就中，最省）。"""
    got, calls = _run()
    assert calls[0] == REGISTERED_NS, f"第一个查的应该是登记的 namespace，实际：{calls}"
    assert calls[1:2] == [DERIVED_NS], f"登记 namespace 落空后要接着查派生的那个，实际：{calls}"


def test_falls_back_to_all_namespaces():
    """两个 namespace 都没有时，必须退到 -A 全量 —— 那是最后一道防线。"""
    got, calls = _run(extra_pods=[pod(RUNNER_NAME, "some-unregistered-ns")], derived_pods=[])
    assert got["pod_evidence"] is not None, f"没退到全量，pod 明明在某个未登记 namespace 里：{calls}"
    assert None in calls, f"没有发起 -A 全量列举：{calls}"


def test_namespace_listing_failure_does_not_abort_cluster():
    """登记 namespace 列举失败（无权限等）不能中断整个集群的查找，要继续查下一个范围。"""
    got, calls = _run(fail_namespaces=(REGISTERED_NS,))
    assert got["pod_evidence"] is not None, f"登记 namespace 失败后没有继续查：{calls}"
    assert any("列举 pod 失败" in note and REGISTERED_NS in note
               for note in got["not_obtained"]), \
        f"某一路视野不可用必须留痕（否则读者以为查过）：{got['not_obtained']}"


def test_no_pod_anywhere_reports_the_real_scope():
    """确实哪儿都没有时，理由必须写明**查过哪些范围**，且不能凭空说「pod 已回收」。"""
    got, _ = _run(fail_namespaces=(REGISTERED_NS, DERIVED_NS, None))
    assert got["pod_evidence"] is None
    text = "；".join(got["not_obtained"])
    assert DERIVED_NS in text and "全量(-A)" in text, \
        f"结论里要点明查过的范围，否则分不清「真没有」与「只查了一个 namespace」：{text}"
    summary = got["queried_namespaces"].get("ascend-cn12-001-cluster") or []
    assert summary == [REGISTERED_NS, DERIVED_NS, "全量(-A)"], summary


def test_foreign_pod_in_registered_namespace_is_not_accepted():
    """登记 namespace 里的**别的**同标签 pod 不能被当成本 job 的现场（时间窗不符要排除）。"""
    stale = pod(FULL_LABEL + "-stale-runner-aaaaa", REGISTERED_NS,
                start="2026-09-28T13:00:00Z")      # 起点晚于失败步骤 → 不可能是本 job
    got, calls = _run(extra_pods=[])               # JOB_PODS 仍在派生 namespace，命中它
    assert got["pod_evidence"] is not None, f"没找到本 job 的 pod（查过的范围：{calls}）"
    assert got["pod_evidence"]["pod"] == RUNNER_NAME, \
        f"选中了外来 pod：{got['pod_evidence']['pod']}"
    # 再来一次：只有过期 pod、没有本 job 的 pod，则必须报未取证而不是拿过期的凑数
    calls = _install_fakes()
    nf.cluster_ops.list_pods = lambda path, namespace=None: (
        {"ok": True, "pods": [stale], "error": None} if namespace in (REGISTERED_NS, None)
        else {"ok": True, "pods": [], "error": None})
    got = nf.step3_cluster_forensics(_case(), FakeRegistry(), {}, Args, [])
    assert got["pod_evidence"] is None, "把时间窗不符的外来 pod 当成了本 job 的现场"


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
