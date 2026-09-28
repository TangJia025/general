#!/usr/bin/env python3
"""监听台账与触发判据的单元测试（含「两处规则必须一致」的契约断言）。

为什么这组断言值得写：监听器的正确性不在「能不能跑」，而在三类**静默错误**——
    1. 重复分析：轮询每 30s 一次，台账若漏判就同一失败分析几十遍，还把最贵的日志 API 放大几十倍；
    2. 静默丢弃：分析失败被跳过、或「run 已扫描」把没做完的 job 一起吞掉 → 报告里没有、也无人知晓；
    3. 假证据：pod 早已回收还去抢快照，查到的是同标签的**别的** pod —— 比没有证据更糟。
这三类错误都不会让程序报错，只会让结论悄悄变错，所以必须有断言守着。

运行：python3 tests/test_watch_state.py      （无需 pytest，也兼容 pytest）
"""
import ast
import datetime
import pathlib
import re
import sys
import tempfile

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
sys.path.insert(0, str(BASE_DIR))

from forensics import watch_state as ws                                  # noqa: E402
import npu_ci_forensics as pipeline                                      # noqa: E402
from forensics.report import render_case, snapshot_pod_still_running      # noqa: E402
import npu_ci_watch                                                     # noqa: E402
from npu_ci_watch import DEFAULT_PATH_PATTERN, PHASE_B_STATES             # noqa: E402

NOW = datetime.datetime(2026, 9, 28, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _iso(**delta):
    return (NOW + datetime.timedelta(**delta)).isoformat().replace("+00:00", "Z")


def make_job(status="in_progress", conclusion=None, completed_at=None, failed_step_number=3,
             steps=None, **extra):
    if steps is None:
        steps = [
            {"number": 1, "name": "Checkout", "conclusion": "success",
             "started_at": _iso(minutes=-20), "completed_at": _iso(minutes=-19)},
            {"number": failed_step_number, "name": "Run NPU test", "conclusion": "failure",
             "started_at": _iso(minutes=-18), "completed_at": _iso(minutes=-1)},
            {"number": failed_step_number + 1, "name": "Upload log", "conclusion": "failure",
             "started_at": _iso(minutes=-1), "completed_at": _iso(seconds=-30)},
        ]
    job = {"id": extra.pop("id", 108892970216), "name": "single-node / a2",
           "status": status, "conclusion": conclusion,
           "started_at": _iso(minutes=-20), "completed_at": completed_at,
           "runner_name": "linux-aarch64-a2-0-cn12-001-runner-abcde",
           "labels": ["linux-aarch64-a2-0"], "steps": steps,
           "run_id": extra.pop("run_id", 36409554688)}
    job.update(extra)
    return job


# ---------- 触发判据 ----------

def test_path_pattern_scope():
    """范围预筛按 run 的 path 匹配：仓库里 15 分钟有 63 个 run，绝大多数是 bot workflow。"""
    pattern = re.compile(DEFAULT_PATH_PATTERN)
    assert ws.is_in_scope({"path": ".github/workflows/schedule_nightly_test_a2.yaml"}, pattern)
    assert ws.is_in_scope({"path": ".github/workflows/schedule_weekly_test_a3.yaml"}, pattern)
    assert not ws.is_in_scope({"path": ".github/workflows/schedule_nightly_test_a5.yaml"}, pattern)
    assert not ws.is_in_scope({"path": ".github/workflows/upload-artifact.yaml"}, pattern)
    assert not ws.is_in_scope({}, pattern), "缺 path 不能算命中（否则等于监听全仓）"


def test_earliest_failed_step_takes_lowest_number():
    """级联失败取序号最靠前的那个步骤：后续步骤是被它带崩的，不是根因。"""
    step = ws.earliest_failed_step(make_job())
    assert step["number"] == 3 and step["name"] == "Run NPU test", step
    assert ws.earliest_failed_step(make_job(steps=[{"number": 1, "conclusion": "success"}])) is None


def test_snapshot_window_falls_back_to_job():
    """失败步骤缺时间戳时退回 job 起止：宁可窗口粗，也不能因此放弃取证。"""
    steps = [{"number": 2, "name": "x", "conclusion": "failure", "started_at": None, "completed_at": None}]
    job = make_job(steps=steps)
    assert ws.snapshot_window(job) == (job["started_at"], job["completed_at"])


def test_should_take_snapshot_window():
    """抢快照的判据是「pod 是否可能还在」，窗口外必须明确拒绝并说明原因。"""
    running = make_job(status="in_progress")
    worth, why = ws.should_take_snapshot(running, now=NOW)
    assert worth and "仍在运行" in why, why

    just_done = make_job(status="completed", conclusion="failure", completed_at=_iso(seconds=-30))
    worth, why = ws.should_take_snapshot(just_done, now=NOW)
    assert worth and "刚结束" in why, why

    long_done = make_job(status="completed", conclusion="failure", completed_at=_iso(minutes=-9))
    worth, why = ws.should_take_snapshot(long_done, now=NOW)
    assert not worth and "回收" in why, why
    assert "假证据" in why, "拒绝的理由必须写清「查到的是别的 pod」，否则读者会以为只是懒得查"


def test_ready_for_analysis_requires_completed_job():
    """job 未结束时日志取不到（实测 404 BlobNotFound），阶段 B 必须等它结束。"""
    ok, why = ws.ready_for_analysis(make_job(status="in_progress"))
    assert not ok and "404" in why, why
    ok, _ = ws.ready_for_analysis(make_job(status="completed", conclusion="failure",
                                           completed_at=_iso(minutes=-5)))
    assert ok


def test_not_a_failure_reason():
    """step 级失败 ≠ job 失败：带 continue-on-error 的步骤失败后 job 仍判成功，不能当失败分析。"""
    assert ws.not_a_failure_reason(make_job(status="in_progress")) is None, "job 没结束就不该下判断"
    assert ws.not_a_failure_reason(make_job(status="completed", conclusion="failure")) is None
    assert ws.not_a_failure_reason(make_job(status="completed", conclusion="timed_out")) is None
    reason = ws.not_a_failure_reason(make_job(status="completed", conclusion="success"))
    assert reason and "continue-on-error" in reason, reason


def test_group_by_run_and_interval():
    jobs = [make_job(id=1, run_id=100), make_job(id=2, run_id=100), make_job(id=3, run_id=200)]
    grouped = ws.group_by_run(jobs)
    assert sorted(grouped) == [100, 200] and len(grouped[100]) == 2
    assert ws.next_interval(True, 30, 300) == 30, "有目标 run 在跑就必须快（快照窗口只有几十秒）"
    assert ws.next_interval(False, 30, 300) == 300, "空闲时必须退避（否则一天空转几千次 API 调用）"


# ---------- 台账 ----------

def test_ledger_keys_by_job_id_not_run():
    """同一 run 里**后失败**的 job 必须能被补上：按 run 记会把后失败的那个吞掉。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = str(pathlib.Path(tmp) / "ledger.json")
        ledger = ws.Ledger(path)
        ledger.note(1001, ws.STATE_SEEN, run_id=77)
        ledger.note(1002, ws.STATE_SEEN, run_id=77)      # 同一 run 的第二个失败 job
        ledger.note(1001, ws.STATE_REPORTED, run_id=77)
        ledger.save()

        reloaded = ws.Ledger(path)
        assert reloaded.seen(1001) and reloaded.seen(1002)
        assert reloaded.is_terminal(1001) and not reloaded.is_terminal(1002)
        assert [item["job_id"] for item in reloaded.pending(PHASE_B_STATES)] == [1002], \
            "同 run 里后失败的 job 必须仍待处理，否则它会永远不进报告"
        assert not pathlib.Path(path + ".tmp").exists(), "落盘必须原子替换，不留临时文件"


def test_ledger_records_errors_until_gave_up():
    """失败必须留痕：attempts 累加到最后置 gave_up，绝不静默丢弃。"""
    with tempfile.TemporaryDirectory() as tmp:
        ledger = ws.Ledger(str(pathlib.Path(tmp) / "ledger.json"))
        ledger.note(5, ws.STATE_SEEN)
        assert ledger.record_error(5, "第一次网络抖动", 3) == ws.STATE_SEEN
        assert ledger.record_error(5, "第二次", 3) == ws.STATE_SEEN
        assert ledger.record_error(5, "第三次", 3) == ws.STATE_GAVE_UP
        record = ledger.entry(5)
        assert record["attempts"] == 3 and record["last_error"] == "第三次"
        assert record["state"] in ws.TERMINAL_STATES


def test_ledger_survives_corruption():
    """台账损坏时备份并重来，而不是抛异常让监听器起不来（起不来 = 所有失败都不再被处理）。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "ledger.json"
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        ledger = ws.Ledger(str(path))
        assert ledger.data["jobs"] == {}
        assert pathlib.Path(str(path) + ".corrupt").exists(), "损坏的台账要留备份便于事后查"
        ledger.note(9, ws.STATE_SEEN)
        ledger.save()
        assert ws.Ledger(str(path)).seen(9)


def test_ledger_cursor_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(pathlib.Path(tmp) / "ledger.json")
        ledger = ws.Ledger(path)
        ledger.set_cursor("scanned_runs", {"123": "2026-09-28T12:00:00Z"})
        ledger.save()
        assert ws.Ledger(path).cursor("scanned_runs") == {"123": "2026-09-28T12:00:00Z"}


# ---------- 跨文件契约：两处「最早失败步骤」规则必须一致 ----------

def _script_implementation():
    """从 npu_ci_failure_analysis.py 里单独取出 earliest_failed_step 函数体来执行。

    该脚本是模块级流程（import 会立刻联网跑分析），故不能 import。用 ast 摘出这一个函数节点
    单独 exec —— 这样断言的是**脚本里真正在跑的那份代码**，而不是我抄过来的一份。
    """
    source = (BASE_DIR / "npu_ci_failure_analysis.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "earliest_failed_step":
            module = ast.Module(body=[node], type_ignores=[])
            namespace = {}
            exec(compile(module, "npu_ci_failure_analysis.py:earliest_failed_step", "exec"), namespace)
            return namespace["earliest_failed_step"]
    raise AssertionError("npu_ci_failure_analysis.py 里找不到 earliest_failed_step —— 契约断言的靶子没了")


def test_earliest_failed_step_matches_analysis_script():
    """监听器与第 1 步脚本对「失败步骤」的判断必须一致。

    不一致的后果很隐蔽：监听器按 A 规则抢快照、脚本按 B 规则分类，两者锚定的失败步骤不同，
    快照里的 pod 时间窗就对不上（`time_consistent` 判负），最后报告说「未取证」——
    而真正的现场其实已经抢到手了。
    """
    compare = _script_implementation()
    fixtures = [
        make_job(),
        make_job(steps=[{"number": 1, "conclusion": "success"}]),
        make_job(steps=[]),
        make_job(steps=[{"number": 5, "name": "b", "conclusion": "failure"},
                        {"number": 2, "name": "a", "conclusion": "failure"}]),
    ]
    for job in fixtures:
        mine, theirs = ws.earliest_failed_step(job), compare(job)
        assert (mine is None) == (theirs is None), f"是否判定为「有失败步骤」不一致：{job['steps']}"
        if mine is not None:
            assert mine["number"] == theirs["number"], f"选中的失败步骤不同：{mine} vs {theirs}"


# ---------- 快照合并路径（阶段 A 的产物如何被第 2~5 步消费） ----------

class ExplodingRegistry:
    """任何查询都直接失败：用来断言「命中快照时一个集群调用都不该发生」。"""
    def __getattr__(self, item):
        def boom(*args, **kwargs):
            raise AssertionError(f"命中快照时不该调用 registry.{item} —— 现场查询看到的是另一个时刻")
        return boom


def test_snapshot_hit_skips_live_cluster_query():
    """命中快照 → 直接采用并标注来源，不发起任何现场查询。

    为什么这条必须成立：快照是 job 还在跑时抢下的真实现场，而现场查询必然是几分钟以后，
    那时 pod 多已回收 —— 再查一次只会用一个弱证据去冲淡强证据。
    """
    taken = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    snapshot = {
        "job_id": 4242, "taken_at": taken, "job_status_at_snapshot": "in_progress",
        "taken_reason": "job 仍在运行，pod 必然还在承载它", "snapshot_file": "/tmp/job_4242.json",
        "cluster_result": {"cluster_name": "mind-cluster-01", "pod_evidence": {"pod": "runner-xyz"},
                           "logs": [{"container": "runner", "source": "当前实例", "ok": True, "text": "boom"}]},
    }
    case = {"job_id": 4242, "labels": ["linux-aarch64-a3-800t-0"], "runner_name": "runner-xyz",
            "_repo": "vllm-project/vllm-ascend"}
    got = pipeline.step3_cluster_forensics(case, ExplodingRegistry(), {}, None, [], {4242: snapshot})
    assert got["snapshot_from"] == "/tmp/job_4242.json"
    assert got["snapshot_taken_at"] == taken
    assert got["cluster_name"] == "mind-cluster-01"
    assert got["logs"] and got["logs"][0]["text"] == "boom"


def test_snapshot_without_job_id_is_not_matched():
    """job_id 不匹配（或缺）时不能误用别人的快照：张冠李戴的证据比没有更糟。"""
    snapshot = {"job_id": 1, "cluster_result": {"cluster_name": "别的集群"}}
    case = {"job_id": 2, "labels": [], "_repo": "vllm-project/vllm-ascend"}
    try:
        got = pipeline.step3_cluster_forensics(case, ExplodingRegistry(), {}, None, [], {1: snapshot})
    except AssertionError as exc:
        assert "registry" in str(exc)          # 确实进了现场查询路径（本用例的 registry 是炸弹）
        return
    assert got["cluster_name"] != "别的集群", "把别的 job 的快照当成了本 job 的现场"


def test_report_marks_snapshot_origin_and_missing_exit_code():
    """报告要写清「证据来自失败时刻的快照」，并把「退出码尚未产生」与「无异常」区分开。"""
    cluster = {
        "snapshot_from": "/state/snapshots/job_4242.json", "snapshot_taken_at": "2026-09-28T20:00:00+08:00",
        "snapshot_job_status": "in_progress", "snapshot_note": "job 仍在运行，pod 必然还在承载它",
        "cluster_name": "mind-cluster-01", "candidate_note": "标签精确登记于 ['mind-cluster-01']",
        "pod_evidence": {"pod": "runner-xyz", "namespace": "vllm-project", "match_kind": "label",
                         "containers": [{"container": "runner", "state": "running"}]},
        "logs": [{"container": "runner", "source": "当前实例", "ok": True, "text": "boom"}],
    }
    assert snapshot_pod_still_running(cluster, cluster["pod_evidence"]), \
        "容器在跑、没有任何退出码 —— 这正是「退出码尚未产生」的情形"
    case = {"job_name": "single-node / a2", "workflow": ".github/workflows/schedule_nightly_test_a2.yaml",
            "link": "https://example.invalid/1", "step": "Run NPU test", "chip": "a2",
            "labels": ["linux-aarch64-a2-0"], "runner_name": "runner-xyz",
            "window": {"started_at": _iso(minutes=-18), "completed_at": _iso(minutes=-1)},
            "cluster": cluster, "history": [], "related_issues": [],
            "verdict": {"owning": "code"}}
    text = "\n".join(render_case(case, 1))
    assert "失败时刻的集群快照" in text, "报告没说明证据来源是快照"
    assert "尚未产生" in text and "不是「无异常」" in text, \
        "快照里空的退出码会被读成「无异常」—— 必须显式纠正"


# ---------- 监听器本体：写路径与 dry-run 的边界 ----------

def _watcher(tmp, extra_args=()):
    """按给定 state-dir 构造一个真实 Watcher（不联网，只测台账写入行为）。"""
    argv = sys.argv
    sys.argv = ["npu_ci_watch.py", "--once", "--state-dir", str(tmp)] + list(extra_args)
    try:
        return npu_ci_watch.Watcher(npu_ci_watch.parse_args())
    finally:
        sys.argv = argv


def test_watcher_writes_through_to_ledger_and_dry_run_does_not():
    """台账写路径必须真的写到磁盘；dry-run 必须一个字都不写。

    为什么专门测这个：dry-run 在写入口处就 return 了，所以**只有真实路径**能暴露写入口自身的错。
    实测踩过的坑：一次机械替换把 mark() 的函数体也替换成了 self.mark(...)，
    变成无限自递归 —— `--once --dry-run` 全绿，真实运行第一轮就 RecursionError。
    这类「只在非 dry-run 下才走到」的代码必须有测试真的走一遍。
    """
    run = {"id": 36418103916, "path": ".github/workflows/schedule_nightly_test_a2.yaml"}
    job = make_job(status="in_progress")

    with tempfile.TemporaryDirectory() as tmp:
        watcher = _watcher(tmp)
        watcher.record_job_seen(run, job)
        assert watcher.ledger.entry(job["id"])["state"] == ws.STATE_SEEN, "写入口没写进台账"
        assert watcher.ledger.entry(job["id"])["runner_name"] == job["runner_name"]
        watcher.ledger.save()
        assert pathlib.Path(tmp, "ledger.json").exists(), "台账没落盘"
        assert pathlib.Path(tmp, "watch.log").exists(), "真实运行必须留下 watch.log"

    with tempfile.TemporaryDirectory() as tmp:
        dry = _watcher(tmp, extra_args=("--dry-run",))
        dry.record_job_seen(run, job)
        dry.mark(job["id"], ws.STATE_REPORTED, report_path="/tmp/x.md")
        assert dry.ledger.entry(job["id"]) == {}, "dry-run 竟然写了台账"
        dry.ledger.save()                      # --once --dry-run 的收尾路径也不该写
        assert not pathlib.Path(tmp).exists() or not list(pathlib.Path(tmp).iterdir()), \
            f"dry-run 竟然落了盘：{list(pathlib.Path(tmp).iterdir()) if pathlib.Path(tmp).exists() else []}"


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
