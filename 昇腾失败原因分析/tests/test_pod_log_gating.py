#!/usr/bin/env python3
"""集群取证「取不取容器日志」的回归测试。

为什么需要这组测试（npu_ci_forensics.py 的 step3_cluster_forensics）：
    这里曾写出一个**反向**条件 ——
        if args.no_pod_logs or cluster_result.get("time_consistent") is False:
    少了最外层的 not，于是行为完全颠倒：
      1. 传 `--no-pod-logs` 反而去抓日志；
      2. 时序不符（属于另一次运行）的 pod 也去抓日志；
      3. **正常路径反而不抓日志** —— 即集群取证自交付起从未取到过容器日志，
         而容器日志恰恰是「拿不到退出码时唯一的错误现场」。
    这类「条件少一个 not」的缺陷不会报错、不会崩溃，只会安静地把证据丢空，
    所以必须由断言守住，而不是靠读代码发现。

    测试手法：把集群操作与集群会话整体替换为假实现，只验「调用/未调用」这一件事。

运行：python3 tests/test_pod_log_gating.py      （无需 pytest，也兼容 pytest）
"""
import pathlib
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR.parent))

import npu_ci_forensics as nf                    # noqa: E402
from forensics import cluster_forensics as cf    # noqa: E402

LABEL = "linux-aarch64-a3-800t-0"
FULL_LABEL = "linux-aarch64-a3-800t-0-cn12-001"
STEP_START = "2026-09-28T09:37:19Z"
STEP_END = "2026-09-28T09:37:21Z"

CALLS = []


def _pod():
    return {"metadata": {"name": f"{FULL_LABEL}-nlk5t-runner-q8np9",
                         "namespace": "vllm-project",
                         "creationTimestamp": "2026-09-28T09:20:00Z"},
            "status": {"phase": "Running", "startTime": "2026-09-28T09:20:00Z",
                       "containerStatuses": [{"name": "runner", "state": {"running": {}}}]}}


class FakeSession:
    def __init__(self):
        self.path = "/fake/kubeconfig.yaml"
        self.namespace = "vllm-project"
        self.info = type("Info", (), {"filename": "fake-kubeconfig.yaml"})()

    def connectivity(self):
        return {"reachable": True, "server_version": "v1.29", "identity": "sa-test"}

    def namespace_pods(self):
        return {"ok": True, "pods": [_pod()]}

    def pod_lists_for_lookup(self):
        """第 2 步找 pod 时按「登记 namespace → 仓库名派生的 namespace → 全量」逐个尝试，
        命中即停（见 ClusterSession.pod_lists_for_lookup）。本测试只关心日志门禁，
        故给一路即命中；namespace 解析本身由 tests/test_namespace_lookup.py 守着。"""
        yield "vllm-project", self.namespace_pods()

    def availability(self, label):
        return {"available": True, "checked": True, "snapshot_only": True,
                "matched_pods": 1, "runners_online": 1, "listeners": 0,
                "namespaces": ["vllm-project"], "samples": []}


class FakeRegistry:
    """只提供 step3 真正用到的两个查询：标签翻译与 namespace。"""

    def full_labels_for(self, repo, cluster_name, reported_labels):
        return [FULL_LABEL]          # 展示名 → 带集群后缀全名，与真实翻译口径一致

    def namespace_for(self, repo, cluster_name):
        return "vllm-project"


def _install_fakes(time_consistent):
    """装好假集群：find_job_pod 返回的 pod 时序自洽或不符由 time_consistent 决定。"""
    CALLS.clear()
    nf.resolve_exact_sessions = lambda registry, repo, labels, sessions: (
        [("ascend-cn12-001-cluster", FakeSession())], "标签精确登记于 ['ascend-cn12-001-cluster']")
    nf.cluster_ops.find_job_pod = lambda pods, runner_name, labels, started, completed: {
        "pod": _pod(), "match_kind": "标签+时间窗", "reason": "按标签匹配到 1 个候选",
        "time_consistent": time_consistent, "window_note": "时间窗判定自洽",
        "informative": True}
    nf.cluster_ops.pod_evidence = lambda pod: {
        "pod": pod["metadata"]["name"], "namespace": "vllm-project", "phase": "Running",
        "start_time": "2026-09-28T09:20:00Z", "created_time": "2026-09-28T09:20:00Z",
        "containers": [{"container": "runner", "state": "running", "exit_code": None,
                        "last_terminated_reason": None, "restart_count": 0}]}

    def fake_pod_logs(path, namespace, pod_name, container=None, previous=False, tail=120):
        CALLS.append((container, previous))
        return {"ok": True, "text": f"fake log for {container} previous={previous}"}

    nf.cluster_ops.pod_logs = fake_pod_logs


def _run(no_pod_logs, time_consistent):
    _install_fakes(time_consistent)
    args = type("Args", (), {"no_pod_logs": no_pod_logs})()
    case = {"labels": [LABEL], "_repo": "vllm-project/vllm-ascend", "runner_name": None,
            "failed_step_started_at": STEP_START, "failed_step_completed_at": STEP_END}
    return nf.step3_cluster_forensics(case, FakeRegistry(), {}, args, [])


# ---------- 1. 正常路径必须取到日志（这条正是原 bug 丢掉的场景）----------

def test_normal_path_fetches_pod_logs():
    got = _run(no_pod_logs=False, time_consistent=True)
    assert CALLS, "正常路径没有发起任何 pod_logs 调用 —— 这就是那个反向条件的后果"
    assert len(got["logs"]) == 1 and got["logs"][0]["text"], got["logs"]
    assert got["logs"][0]["container"] == "runner"


# ---------- 2. --no-pod-logs 必须真的不取 ----------

def test_no_pod_logs_flag_actually_skips():
    got = _run(no_pod_logs=True, time_consistent=True)
    assert CALLS == [], f"--no-pod-logs 下仍发起了调用：{CALLS}"
    assert got["logs"] == [], got["logs"]
    # 关掉日志不影响 pod 状态取证
    assert got["pod_evidence"], "关日志不该连 pod 状态一起丢"


# ---------- 3. 外来 pod（时序不符）不得取日志 ----------

def test_foreign_pod_logs_are_not_fetched():
    got = _run(no_pod_logs=False, time_consistent=False)
    assert CALLS == [], f"给时序不符的 pod 抓了日志：{CALLS}"
    assert got["logs"] == [], got["logs"]
    assert got["time_consistent"] is False


# ---------- 4. 重启过的容器要额外取一份上一实例的日志 ----------

def test_restarted_container_fetches_previous_instance():
    _install_fakes(time_consistent=True)
    original = nf.cluster_ops.pod_evidence
    nf.cluster_ops.pod_evidence = lambda pod: {
        **original(pod),
        "containers": [{"container": "runner", "state": "running", "exit_code": None,
                        "last_terminated_reason": "Error", "restart_count": 1}]}
    args = type("Args", (), {"no_pod_logs": False})()
    case = {"labels": [LABEL], "_repo": "vllm-project/vllm-ascend", "runner_name": None,
            "failed_step_started_at": STEP_START, "failed_step_completed_at": STEP_END}
    got = nf.step3_cluster_forensics(case, FakeRegistry(), {}, args, [])
    assert ("runner", False) in CALLS and ("runner", True) in CALLS, CALLS
    assert {entry["source"] for entry in got["logs"]} == {"当前实例", "重启前的实例"}, got["logs"]


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
