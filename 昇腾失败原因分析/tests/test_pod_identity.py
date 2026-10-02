#!/usr/bin/env python3
"""
集群取证「pod 身份」回归测试：标签匹配到的 pod 必须真的可能是本 job 的现场。

为什么需要这组测试（设计文档 npu_ci_forensics_design.md §3.3/§3.4 的踩坑记录）：
  按标签匹配是**推定**——runner 标签相同不等于同一个 pod。已实测踩过两次，
  两次都把不是本次失败的现场当成了证据，而这类错误的危害是「读者会据此下结论」：
    1. pod 复用：job 的失败步骤在 09:37:19~09:37:21，按标签+时间窗收敛到的 pod
       启动于 09:38:53（晚 92s），日志里正在续租的是**另一个** job；
    2. 未调度的 pod：12 分钟前失败的 job 匹配上了**刚刚才创建**的 Pending pod，
       它带 `PodScheduled=False Unschedulable`（PVC 未绑定），读起来还挺像失败原因。
  这两种 pod 都必须被剔除，且**措辞**要能区分「都不是本 job 的」与「压根没找到」。

另外两个必须一起测的坑：
    3. 早先的判据是「pod 启动不晚于 job 结束」+120s 余量 —— 方向就是错的：
       job 结束后新建的 pod 照样满足它，92s 的间隔正是这么溜过去的；
    4. Pending pod 的 `status.startTime` 是空的，只看它会退化成「时间未知 → 可能就是它」，
       故必须以 `metadata.creationTimestamp` 兜底。

运行：python3 tests/test_pod_identity.py      （无需 pytest，也兼容 pytest）
"""
import datetime
import pathlib
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR.parent))

from forensics import cluster_forensics as cf  # noqa: E402
from forensics.cluster_registry import ClusterLabels, parse_cluster_map  # noqa: E402

# ---- 实测样例的时间锚点（2026-09-24，来自真实 job 与真实 pod）----
STEP_START = "2026-09-24T09:37:19Z"
STEP_END = "2026-09-24T09:37:21Z"
LABEL = "linux-amd64-cpu-8-hk"
OTHER_RUNNER = "linux-amd64-cpu-8-hk-nomatch-runner-zzzzz"


def make_pod(name, start_time=None, created=None, phase="Running", with_containers=True):
    pod = {"metadata": {"name": name, "creationTimestamp": created},
           "status": {"phase": phase}}
    if start_time:
        pod["status"]["startTime"] = start_time
    if with_containers:
        pod["status"]["containerStatuses"] = [{"name": "runner", "state": {"running": {}}}]
    return pod


def named(suffix):
    """构造合法 pod 名：<label>-<5 位>-runner-<5 位>（POD_NAME_GRAMMAR 的格式）。"""
    return f"{LABEL}-{suffix}-runner-{suffix}"


def find(pods, runner_name=None):
    """按标签匹配（不传 runner_name）—— 这是 job 未被分配 runner 时才走的路径。"""
    return cf.find_job_pod(pods, runner_name, [LABEL], STEP_START, STEP_END)


# ---------- 1. 时间窗判据的方向 ----------

def test_window_check_direction():
    """承载本步骤的 pod 必须先于该步骤存在；「不晚于 job 结束」是错的方向。"""
    before = cf._pod_window_check(make_pod("p", start_time="2026-09-24T09:20:00Z"),
                                  STEP_START, STEP_END)
    assert before["time_consistent"] is True, before

    # 实测样例：晚于失败步骤开始 94s —— 旧判据（晚于 job 结束 92s，余量 120s）会放过它
    foreign = cf._pod_window_check(make_pod("p", start_time="2026-09-24T09:38:53Z"),
                                   STEP_START, STEP_END)
    assert foreign["time_consistent"] is False, foreign
    assert "不能**归因于本 job" in foreign["window_note"] or "不能" in foreign["window_note"]

    # 恰好等于步骤开始：算自洽（余量之内）
    equal = cf._pod_window_check(make_pod("p", start_time=STEP_START), STEP_START, STEP_END)
    assert equal["time_consistent"] is True, equal


def test_window_check_falls_back_to_creation_timestamp():
    """Pending pod 没有 status.startTime，必须退到 creationTimestamp，否则「未知」会被当成「可用」。"""
    pending_old = make_pod(named("aaaaa"), created="2026-09-24T09:20:00Z",
                           phase="Pending", with_containers=False)
    got = cf._pod_window_check(pending_old, STEP_START, STEP_END)
    # 只看 startTime 的实现会返回 None（判不出来），那样这个候选就会被下一层当成「可能就是它」
    assert got["time_consistent"] is True, got

    # 实测样例：job 失败于 09:36:02，而这个 pod 创建于 09:48:56
    pending_new = make_pod(named("nlk5t"), created="2026-09-24T09:48:56Z",
                           phase="Pending", with_containers=False)
    got = cf._pod_window_check(pending_new, "2026-09-24T09:36:02Z", "2026-09-24T09:36:44Z")
    assert got["time_consistent"] is False, got
    assert "创建时间" in got["window_note"], got


def test_no_timestamp_is_undecidable_not_acceptable():
    """两种时间戳都没有 → 判不出来，不能当成「可能就是它」。"""
    got = cf._pod_window_check(make_pod("p"), STEP_START, STEP_END)
    assert got["time_consistent"] is None, got


# ---------- 2. 「真的跑起来过」这一关 ----------

def test_unstarted_pod_cannot_have_run_job():
    pending = make_pod("p", created="2026-09-24T09:20:00Z", phase="Pending",
                       with_containers=False)
    can_run, why = cf._pod_can_have_run_job(pending)
    assert can_run is False and "Pending" in why, why

    no_containers = make_pod("p", start_time="2026-09-24T09:20:00Z", with_containers=False)
    can_run, why = cf._pod_can_have_run_job(no_containers)
    assert can_run is False and "容器" in why, why

    running = make_pod("p", start_time="2026-09-24T09:20:00Z")
    can_run, why = cf._pod_can_have_run_job(running)
    assert can_run is True and why is None, why


# ---------- 3. find_job_pod 整体行为 ----------

def test_returns_none_when_all_candidates_are_unstarted_or_late():
    """实测的两类假证据：一个刚创建的 Pending pod（带 Unschedulable）+ 一个复用 pod。"""
    pending = make_pod(named("nlk5t"), created="2026-09-24T09:48:56Z",
                       phase="Pending", with_containers=False)
    reused = make_pod(named("bbbbb"), start_time="2026-09-24T09:38:53Z",
                      created="2026-09-24T09:38:53Z")
    got = find([pending, reused])
    assert got["pod"] is None, got
    # 措辞必须断言「都不是本 job 的现场」，且分别计数——不能笼统说「pod 已回收」
    assert got.get("informative") is True
    assert "不构成本 job" in got["reason"], got["reason"]
    assert "尚未启动" in got["reason"] and "晚于本 job 失败步骤" in got["reason"], got["reason"]


def test_returns_none_with_undecidable_wording_when_no_time_evidence():
    """判不出来时要如实说「无法判定」，不能包装成「已排除」。"""
    got = find([make_pod(named("ddddd"))])
    assert got["pod"] is None, got
    assert "无法判定" in got["reason"], got["reason"]


def test_picks_the_plausible_candidate_over_the_pending_one():
    pending = make_pod(named("nlk5t"), created="2026-09-24T09:48:56Z",
                       phase="Pending", with_containers=False)
    plausible = make_pod(named("ccccc"), start_time="2026-09-24T09:20:00Z",
                         created="2026-09-24T09:20:00Z")
    got = find([pending, plausible])
    assert got["pod"] is plausible, got
    assert got["time_consistent"] is True
    assert "剔除 1 个尚未启动的" in (got.get("reason") or ""), got.get("reason")


def test_known_runner_name_blocks_label_guessing():
    """★ 本次修的缺陷：runner_name 已知却查不到时，**不许**拿同标签的别的 pod 顶替。

    实测样例（2026-10-02）：job 110701473809 的 runner_name = …-26v84-runner-wddd8 已被回收，
    标签匹配选中同 run 内**另一个成功 job**(110701477428) 的 pod …-runner-5j29n，并把它标成
    「runner 标签 + 时间窗收敛」。161 份快照里 19 份是这样取错 pod 的（其中 5 份取到的是
    别人 runner 的 `-workflow` 伴生 pod）。
    """
    sibling = make_pod(named("5j29n"), start_time="2026-09-24T09:20:00Z",
                       created="2026-09-24T09:20:00Z")
    got = cf.find_job_pod([sibling], OTHER_RUNNER, [LABEL], STEP_START, STEP_END)
    assert got["pod"] is None, "精确名查不到时不该退到标签猜测"
    assert got.get("informative") is True
    assert "runner_name 精确名" in got["reason"] and "不构成本 job" in got["reason"], got["reason"]
    # 措辞必须能落到路径 B 的「同标签现存 pod 均非本 job 现场」那一支，而不是「pod 多已回收」
    assert "已被回收" in got["reason"]


def test_workflow_pod_of_another_runner_is_not_adopted():
    """别人的 runner 的 `-workflow` 伴生 pod 同样不许顶替：它属于另一个 runner 段。"""
    other_workflow = make_pod(f"{LABEL}-w9qwr-runner-b8f9x-workflow",
                              start_time="2026-09-24T09:20:00Z",
                              created="2026-09-24T09:20:00Z")
    got = cf.find_job_pod([other_workflow], OTHER_RUNNER, [LABEL], STEP_START, STEP_END)
    assert got["pod"] is None, got


def test_label_guessing_still_works_without_runner_name():
    """job 未被分配 runner（runner_name 缺失）时，标签匹配仍是唯一线索，必须保留。"""
    plausible = make_pod(named("ccccc"), start_time="2026-09-24T09:20:00Z",
                         created="2026-09-24T09:20:00Z")
    got = find([plausible], runner_name=None)
    assert got["pod"] is plausible, got
    assert "标签" in got["match_kind"], got


def test_exact_runner_name_wins_over_label_guessing():
    """pod 名与 job 的 runner_name 精确一致时身份确凿，即使时间戳对不上也不能当外来 pod 丢掉。"""
    pod = make_pod("linux-amd64-cpu-8-hk-nlk5t-runner-q8np9",
                   start_time="2026-09-24T09:38:53Z", created="2026-09-24T09:38:53Z")
    got = cf.find_job_pod([pod], "linux-amd64-cpu-8-hk-nlk5t-runner-q8np9", [LABEL],
                          STEP_START, STEP_END)
    assert got["pod"] is pod, got
    assert got["match_kind"] == "runner_name 精确匹配"
    # 身份确凿 → 不判时序不符，但必须留下「时间戳存疑」的提示
    assert got["time_consistent"] is True, got
    assert "时间戳存疑" in (got.get("window_note") or ""), got.get("window_note")


# ---------- 4. 展示名 ↔ 全名 的翻译（不做这一步，真实 job 会被判「未登记」）----------

CLUSTER_MD_SNIPPET = """
<div class="cluster-card" data-name="ascend-cn12-001-cluster">
  <div class="project-row" data-search="vllm-project/vllm-ascend">
    <span class="project-name-text">vllm-project/vllm-ascend</span>
    <div class="machine" data-label="linux-aarch64-a3-800t-0-cn12-001" data-npu="ascend-1980">
      <span class="machine-label">linux-aarch64-a3-800t-0</span>
    </div>
    <div class="project-ns">namespace: <code>vllm-project</code></div>
  </div>
</div>
"""


def test_display_label_translates_to_full_label():
    clusters = parse_cluster_map(CLUSTER_MD_SNIPPET)
    project = clusters["ascend-cn12-001-cluster"]["vllm-project/vllm-ascend"]
    assert project.namespace == "vllm-project"
    # job 上报展示名，pod 名用带后缀全名
    assert project.full_labels_for("linux-aarch64-a3-800t-0") == [
        "linux-aarch64-a3-800t-0-cn12-001"]
    # 全名反查自身也要成立
    assert project.full_labels_for("linux-aarch64-a3-800t-0-cn12-001") == [
        "linux-aarch64-a3-800t-0-cn12-001"]
    # 展示名相同（无后缀）时不制造无意义的别名
    assert project.display_aliases == {"linux-aarch64-a3-800t-0":
                                       ["linux-aarch64-a3-800t-0-cn12-001"]}


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
