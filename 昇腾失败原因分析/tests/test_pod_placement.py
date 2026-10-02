#!/usr/bin/env python3
"""集群归属回归：「这个 job 到底跑在哪个集群」。

背景（2026-10-02 实测）：**runner 标签登记在哪个集群，不等于负载跑在哪个集群**。
Liqo 把提供方集群上的 pod 反射进消费方集群的共享 namespace，于是同一个 pod
（`…-26v84-runner-l75ch`）从两个 kubeconfig 看是两个样子：

    从 cn12-001 看：       nodeName=`mind-third-ci`（**虚拟节点名**），liqo.io/shadowPod=true
    从 mind-third-ci 看：  nodeName=`192.168.0.181`（真实节点），无该标签

即真实负载跑在 mind-third-ci，cn12-001 只是消费方。只报「取证集群：cn12-001」会被
读成「job 跑在 cn12-001」—— 这正是本工具最容易误导人的一处，故单独一组断言守着。

另外两个必须一起守的点：
  3) pod 的 labels/annotations 必须留在证据里：判据就是这个标签，早先只留 node/phase
     等于把判据丢掉，报告只能给出「node=mind-third-ci」这种读者无法解释的组合；
  4) 取日志的命令必须用 **pod 实际所在** 的 namespace：实测登记 `vllm-project`、pod 在
     `vllm-project-vllm-ascend`，用登记名拼出来的命令照抄会 NotFound。

运行：python3 tests/test_pod_placement.py      （无需 pytest，也兼容 pytest）
"""
import pathlib
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
sys.path.insert(0, str(BASE_DIR))

from forensics import cluster_forensics as cf                             # noqa: E402
from forensics.cluster_registry import ClusterRegistry                    # noqa: E402
from forensics.report import placement_lines, render_case                 # noqa: E402

POD_NAME = "linux-aarch64-a3-800i-16-cn12-001-26v84-runner-l75ch"
REAL_NODE = "192.168.0.181"
VIRTUAL_NODE = "mind-third-ci"
# 实测：Cluster.md 给该仓库登记的是 `vllm-project`，runner pod 却在仓库名派生的这个 namespace 里
POD_NAMESPACE = "vllm-project-vllm-ascend"


def make_pod(labels=None, annotations=None, node=VIRTUAL_NODE):
    """实测同构的 pod（字段取自 2026-10-02 的真实对象）。"""
    return {
        "metadata": {"name": POD_NAME, "namespace": POD_NAMESPACE,
                     "creationTimestamp": "2026-10-02T08:08:48Z",
                     "labels": labels if labels is not None else {},
                     "annotations": annotations or {}},
        "spec": {"nodeName": node},
        "status": {"phase": "Running", "startTime": "2026-10-02T08:08:48Z",
                   "containerStatuses": [{"name": "runner", "ready": True, "restartCount": 0,
                                          "state": {"running": {"startedAt": "2026-10-02T08:08:59Z"}}}]},
    }


SHADOW_POD = make_pod(labels={"liqo.io/shadowPod": "true",
                              "actions.github.com/scale-set-name":
                                  "linux-aarch64-a3-800i-16-cn12-001"},
                      annotations={"liqo.io/api-server-support": "remote"})
# 提供方集群看到的**同一个** pod：没有影子标签，node 是真实节点
REAL_POD = make_pod(labels={"actions.github.com/scale-set-name":
                                "linux-aarch64-a3-800i-16-cn12-001"},
                    node=REAL_NODE)


def make_case(**overrides) -> dict:
    """最小 case：登记 namespace `vllm-project`，而 pod 实际在 `vllm-project-vllm-ascend`。"""
    case = {
        "job_name": "single-node (main, demo)", "workflow": "schedule_nightly_test_a3_560t.yaml",
        "link": "https://github.com/vllm-project/vllm-ascend/actions/runs/1/job/2",
        "job_id": 2, "run_id": 1, "repo": "vllm-project/vllm-ascend",
        "step": "Set up job", "chip": "a3", "labels": ["linux-aarch64-a3-800i-16"],
        "bucket": "步骤直接定性:Set up job", "owner": "infra", "sig": "",
        "cluster": {
            "cluster_name": "ascend-cn12-001-cluster", "namespace": "vllm-project",
            "filename": "k.yaml", "kubeconfig_path": "/home/x/k.yaml",
            "match_kind": "runner_name 精确匹配",
            "identity": {"reachable": True, "server_version": "v1.31.14", "identity": "system:sa"},
            "pod_evidence": cf.pod_evidence(SHADOW_POD),
            "placement_hint": "ascend-mind-third-ci",
            "logs": [{"container": "runner", "source": "当前实例", "ok": True,
                      "text": "[WORKER INFO HostContext] Well known directory 'Root'"}],
        },
        "history": [], "related_issues": [],
    }
    case.update(overrides)
    return case


# ------------------------------------------------- ① 影子对象判定

def test_shadow_pod_is_detected_from_label():
    """消费方看到的影子对象：shadowPod=true，node 是**虚拟节点名**。"""
    got = cf.pod_placement(SHADOW_POD)
    assert got["shadow_pod"] is True, got
    assert got["virtual_node"] == VIRTUAL_NODE, got
    assert got["liqo_api_server_support"] == "remote", got


def test_real_pod_is_not_shadow():
    """提供方看到的真实负载：没有该标签，node 是真实节点 —— 不能把虚拟节点名当节点。"""
    got = cf.pod_placement(REAL_POD)
    assert got["shadow_pod"] is None, got
    assert got["virtual_node"] is None, got
    assert got["node"] == REAL_NODE, got


def test_pod_evidence_keeps_labels_and_drops_giant_annotation():
    """判据在标签里，标签必须留在证据里；只有整份清单副本那种大注解才丢。"""
    big = "x" * 5000
    pod = make_pod(labels={"liqo.io/shadowPod": "true"},
                   annotations={"liqo.io/api-server-support": "remote",
                                "kubectl.kubernetes.io/last-applied-configuration": big})
    evidence = cf.pod_evidence(pod)
    assert evidence["labels"]["liqo.io/shadowPod"] == "true", evidence.get("labels")
    assert evidence["annotations"]["liqo.io/api-server-support"] == "remote"
    assert "kubectl.kubernetes.io/last-applied-configuration" not in evidence["annotations"]
    assert evidence["placement"]["shadow_pod"] is True


# ------------------------------------------------- ② 虚拟节点名 → 已登记集群

def test_virtual_node_maps_to_registered_cluster():
    registry = ClusterRegistry({"ascend-mind-third-ci": {}, "ascend-cn12-001-cluster": {}},
                               kubeconfig_dir="/nonexistent")
    assert registry.cluster_for_virtual_node(VIRTUAL_NODE) == "ascend-mind-third-ci"
    assert registry.cluster_for_virtual_node("cn12-001") == "ascend-cn12-001-cluster"
    assert registry.cluster_for_virtual_node("who-knows") is None
    assert registry.cluster_for_virtual_node(None) is None


def test_ambiguous_virtual_node_refuses_to_guess():
    """多义时返回 None —— 报一个不确定的集群名比报「不确定」危害大得多。"""
    registry = ClusterRegistry({"ascend-foo-cluster": {}, "openmerlin-foo-cluster": {}},
                               kubeconfig_dir="/nonexistent")
    assert registry.cluster_for_virtual_node("foo") is None


# ------------------------------------------------- ③ 报告怎么说

def test_placement_lines_says_workload_is_elsewhere():
    lines = placement_lines({"placement_hint": "ascend-mind-third-ci"},
                            cf.pod_evidence(SHADOW_POD))
    assert len(lines) == 1, lines
    text = lines[0]
    assert "真实负载不在本集群" in text, text
    assert VIRTUAL_NODE in text and "虚拟节点名" in text, text
    assert "ascend-mind-third-ci" in text, text
    # 消费方这个结论必须说清楚：标签登记 ≠ 负载跑在这里
    assert "调度请求" in text, text


def test_placement_lines_without_hint_says_so_instead_of_guessing():
    lines = placement_lines({}, cf.pod_evidence(SHADOW_POD))
    assert "无法判定是哪一个" in lines[0], lines[0]


def test_placement_lines_confirms_local_workload():
    lines = placement_lines({"cluster_name": "ascend-cn12-001-cluster"},
                            cf.pod_evidence(REAL_POD))
    assert "真实负载在**本集群**" in lines[0], lines[0]


def test_placement_lines_is_silent_for_old_snapshots():
    """旧快照没有 placement 字段 → 一个字的结论都不许编。"""
    assert placement_lines({}, {"pod": "p-1", "node": "mind-third-ci"}) == []


def test_render_case_states_the_real_cluster_end_to_end():
    """端到端：影子 pod 的 case 里必须出现「真实负载不在本集群」与提供方提示。"""
    text = "\n".join(render_case(make_case(), 1))
    assert "真实负载不在本集群" in text, text
    assert "ascend-mind-third-ci" in text, text


# ------------------------------------------------- ④ namespace

def test_retrieval_command_uses_pod_namespace_not_registered_one():
    """实测坑：登记 `vllm-project`、pod 在 `vllm-project-vllm-ascend` —— 命令必须用后者。"""
    text = "\n".join(render_case(make_case(), 1))
    assert "-n vllm-project-vllm-ascend" in text, text
    assert "-n vllm-project " not in text, "取日志命令用了登记 namespace，照抄会 NotFound"


def test_cluster_line_shows_both_namespaces():
    """两个 namespace 都要写明：一个是查询口径，一个是取证口径。"""
    text = "\n".join(render_case(make_case(), 1))
    assert "pod 实际 namespace `vllm-project-vllm-ascend`" in text, text
    assert "登记 namespace `vllm-project`" in text, text


def test_cluster_line_keeps_single_namespace_when_they_agree():
    case = make_case()
    case["cluster"]["pod_evidence"]["namespace"] = "vllm-project"
    text = "\n".join(render_case(case, 1))
    assert "namespace `vllm-project`" in text, text
    assert "pod 实际 namespace" not in text, text


# ------------------------------------------------- ⑤ 旧快照里的错 pod 事后作废

def test_foreign_pod_in_old_snapshot_is_detected():
    """修复前抢下的快照里装着推定出来的错 pod，读取时必须能判出来。

    实测样例：快照 job_110701473809.json 的 runner_name 是 `…-runner-wddd8`，
    而 pod_evidence.pod 是 `…-runner-5j29n`（同 run 内另一个成功 job 的 pod）。
    """
    runner = "linux-aarch64-a3-800i-16-cn12-001-26v84-runner-wddd8"
    foreign = {"pod": "linux-aarch64-a3-800i-16-cn12-001-26v84-runner-5j29n"}
    reason = cf.foreign_pod_reason(foreign, "runner 标签 + 时间窗收敛", runner)
    assert reason and "不是本 job 的现场" in reason, reason
    assert "5j29n" in reason and "wddd8" in reason, reason


def test_foreign_pod_check_keeps_legitimate_evidence():
    """精确身份命中的 pod（含 `-workflow` 伴生 pod）不许被误伤。"""
    runner = "linux-aarch64-a3-800i-16-cn12-001-26v84-runner-wddd8"
    assert cf.foreign_pod_reason({"pod": runner}, "runner_name 精确匹配", runner) is None
    assert cf.foreign_pod_reason({"pod": runner + "-workflow"},
                                 "runner_name + -workflow", runner) is None
    # 没有 runner_name 时，标签匹配是唯一线索，不能算「外来 pod」
    assert cf.foreign_pod_reason({"pod": "other"}, "runner 标签唯一匹配", None) is None
    assert cf.foreign_pod_reason(None, "runner 标签唯一匹配", runner) is None


def test_snapshot_loading_retires_foreign_pod_and_its_logs():
    """端到端：旧快照被读入时，那个错 pod 的容器状态与日志必须一并撤下。"""
    import npu_ci_forensics as pipeline
    runner = "linux-aarch64-a3-800i-16-cn12-001-26v84-runner-wddd8"
    snapshot = {
        "runner_name": runner, "snapshot_file": "/tmp/job_1.json",
        "taken_at": "2026-10-02T12:17:47+08:00", "job_status_at_snapshot": "completed",
        "taken_reason": "job 刚结束，pod 可能尚未回收",
        "cluster_result": {
            "cluster_name": "ascend-cn12-001-cluster", "candidates": ["ascend-cn12-001-cluster"],
            "match_kind": "runner 标签 + 时间窗收敛",
            "pod_evidence": {"pod": "linux-aarch64-a3-800i-16-cn12-001-26v84-runner-5j29n",
                             "phase": "Running", "node": "mind-third-ci"},
            "logs": [{"container": "runner", "ok": True, "text": "别人的日志"}],
            "not_obtained": [],
        },
    }
    got = pipeline.apply_cluster_snapshot({}, snapshot)
    assert got["pod_evidence"] is None, "错 pod 的容器状态没被撤下"
    assert got["logs"] == [], "错 pod 的日志没被撤下"
    assert got["retired_pod"].endswith("-5j29n") and got["retired_reason"]
    assert got["match_kind"] is None and got["time_consistent"] is None
    assert any("不是本 job 的现场" in note for note in got["not_obtained"]), got["not_obtained"]


def test_snapshot_loading_keeps_exact_match_evidence():
    """精确匹配的快照一个字都不许动。"""
    import npu_ci_forensics as pipeline
    runner = "linux-aarch64-a3-800i-16-cn12-001-26v84-runner-wddd8"
    snapshot = {"runner_name": runner,
                "cluster_result": {"match_kind": "runner_name 精确匹配",
                                   "pod_evidence": {"pod": runner, "node": "mind-third-ci"},
                                   "logs": [{"container": "runner", "ok": True, "text": "本 job 的日志"}]}}
    got = pipeline.apply_cluster_snapshot({}, snapshot)
    assert got["pod_evidence"]["pod"] == runner
    assert got["logs"] and got["match_kind"] == "runner_name 精确匹配"
    assert "retired_pod" not in got


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
