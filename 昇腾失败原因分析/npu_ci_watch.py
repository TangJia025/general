#!/usr/bin/env python3
"""上游 CI 失败近实时监听器 —— 目标 workflow 一失败就抢集群快照，job 结束后补日志分类与报告。

为什么必须常驻、且必须两阶段（本会话的实测数字，不是推测）：
  - 「失败步骤结束 → job 结束」实测固定 55s / 56s / 55s（3 例）；
  - 进行中的 job **取不到日志**（HTTP 404 BlobNotFound，日志 blob 尚未生成）；
  - runner pod 是一次性的，job 结束前后即被回收（实测失败 9 分钟后同标签 pod 已换人）。
所以「能取日志的时刻」与「pod 还活着的时刻」几乎不重叠：
  【阶段A】一看到某个步骤失败（此刻 pod 必在）→ 立刻抢集群快照；
  【阶段B】等 job 结束（此刻日志才可下载）→ 补日志分类 + 历史归因 + 出报告。
两阶段不可合并：合并成「等结束后一起做」会永远拿不到 pod；合并成「失败时就做」会永远拿不到日志。

行为边界（重要）：
  - 每个失败**只处理一次**（台账按 job_id 去重，见 forensics/watch_state.py 的状态机）；
  - 快照**每个 job 只抢一次**，抢在「第一次看到」那一刻 —— 越早越接近失败现场，重复抢只会覆盖成更晚的时刻；
  - 集群快照拿不到就如实记 `snapshot_missed` 并写明原因（pod 已回收），**不伪装成已取证**；
  - 全程**只落盘、不外发**：日志与状态都在 --state-dir；对外动作（如提 issue）不在本工具范围内，
    只留 `--notify-command` 由你自己接（默认空）。

用法：
  python3 npu_ci_watch.py --once --dry-run        # 只打印将要做什么，一个字都不写
  python3 npu_ci_watch.py --once                  # 跑一轮即退（供测试/cron）
  python3 npu_ci_watch.py                         # 常驻（systemd user 服务用这个）
  python3 npu_ci_watch.py --lookback 2h --once    # 手动补一段窗口（注意窗口上限见 --lookback 说明）
"""
import argparse
import datetime
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from forensics import watch_state as ws                              # noqa: E402
from forensics.cluster_registry import (ClusterRegistry, DEFAULT_KUBECONFIG_DIR,  # noqa: E402
                                        fetch_cluster_md, parse_cluster_map)
import npu_ci_forensics as pipeline                                  # noqa: E402

ANALYSIS_SCRIPT = os.path.join(BASE_DIR, "npu_ci_failure_analysis.py")
PIPELINE_SCRIPT = os.path.join(BASE_DIR, "npu_ci_forensics.py")

# 默认监听范围：nightly / weekly 的 a2、a3 系列测试 workflow。
# 按 run 的 path（.github/workflows/xxx.yaml）匹配 —— 用 path 而不是 name，因为 name 可被改，
# 而 path 正是 npu_ci_failure_analysis.py 里 failed_jobs[].workflow 的口径，两边能对上。
# 触发事件另有限定（ws.is_in_scope：schedule + workflow_dispatch）——
# 同一个文件被 pull_request 触发的 run 语义不同，不该收进来。
DEFAULT_PATH_PATTERN = r"schedule_(nightly|weekly)_test_a[23]"

# 阶段 B 的「待办」状态：这些状态的 job 还需要跑日志分类/出报告
PHASE_B_STATES = {ws.STATE_SEEN, ws.STATE_SNAPSHOT_OK, ws.STATE_SNAPSHOT_MISSED, ws.STATE_ANALYZED}

# 整轮失败（如 gh 未登录、网络不通）时的退避上限（秒）
MAX_BACKOFF_SECONDS = 600

# 停止信号标志：systemd 停服务时置位，主循环在下一轮开始时干净退出（而不是被 SIGTERM 打断落盘）
_STOP_REQUESTED = [False]


def _handle_stop(signum, frame):
    _STOP_REQUESTED[0] = True


def parse_duration(text: str) -> float:
    """把 `30s` / `15m` / `2h` / `1d` 解析成秒。"""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(text or ""))
    if not match:
        raise SystemExit(f"无法解析时长：{text}（支持 30s / 15m / 2h / 1d）")
    value = float(match.group(1))
    unit = match.group(2) or "s"
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default="vllm-project/vllm-ascend")
    parser.add_argument("--path-pattern", default=DEFAULT_PATH_PATTERN,
                        help=f"workflow 文件路径的正则（默认 {DEFAULT_PATH_PATTERN}）；空字符串=不限")
    parser.add_argument("--chips", default="a2,a3", help="芯片范围（透传给两个下游脚本）")
    # 轮询
    parser.add_argument("--interval", type=float, default=30.0,
                        help="有目标 run 在跑时的轮询间隔（秒，默认 30）")
    parser.add_argument("--idle-interval", type=float, default=300.0,
                        help="空闲时的轮询间隔（秒，默认 300）")
    parser.add_argument("--lookback", default="15m",
                        help="每轮回看的窗口（默认 15m）。窗口受两个上限约束：本参数，以及"
                             "「最近 run 列表的翻页上限」（见 --max-pages）；超出窗口的失败不会进台账，"
                             "需要人工用 --lookback 补")
    parser.add_argument("--max-pages", type=int, default=2,
                        help="最近 run 列表最多翻几页（每页 100 个，默认 2）；仓库繁忙时用于覆盖整个窗口")
    parser.add_argument("--snapshot-grace", type=float, default=float(ws.SNAPSHOT_GRACE_SECONDS),
                        help=f"job 结束后多久之内仍尝试抢快照（秒，默认 {ws.SNAPSHOT_GRACE_SECONDS}）")
    parser.add_argument("--max-attempts", type=int, default=3,
                        help="单个 job 连续失败多少次后置 gave_up（默认 3；置 gave_up 仍留痕，不静默丢弃）")
    # 运行控制
    parser.add_argument("--once", action="store_true", help="只跑一轮就退出")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做什么，不写台账/快照/报告")
    parser.add_argument("--notify-command", default="",
                        help="报告生成后执行的命令（默认空=不外发）；会把报告路径作为最后一个参数传入")
    # 集群侧（阶段 A）
    parser.add_argument("--kubeconfig-dir", default=DEFAULT_KUBECONFIG_DIR)
    parser.add_argument("--cluster-md", default=None,
                        help="本地 Cluster.md 路径；缺省则从 ascend-gha-runners/docs 抓取并缓存")
    parser.add_argument("--max-age-hours", type=float, default=24.0,
                        help="Cluster.md 缓存有效期（小时，默认 24）—— 集群映射必须常驻内存，不能每轮重抓")
    # 输出
    parser.add_argument("--state-dir", default=os.path.join(BASE_DIR, ".forensics_state"),
                        help="运行态目录（台账/快照/handoff/日志），默认 .forensics_state（已 gitignore）")
    parser.add_argument("--report-dir", default=os.path.join(BASE_DIR, "npu_ci_reports"),
                        help="第 2~5 步的报告输出目录（时间戳命名，不会覆盖手工运行的报告）")
    parser.add_argument("--cache-dir", default=os.path.join(BASE_DIR, ".forensics_cache"),
                        help="知识库/Cluster.md 缓存目录（与手工运行共用）")
    return parser.parse_args()


class Logger:
    """同时写终端与 watch.log；日志逐行落盘，进程被杀也能保留已发生的动作。

    path=None 时只写终端 —— `--dry-run` 用它来保证「一个字都不落盘」。
    """

    def __init__(self, path: str | None):
        self.file = None
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self.file = open(path, "a", encoding="utf-8")

    def __call__(self, message: str):
        line = f"[{datetime.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        print(line, flush=True)
        if self.file:
            self.file.write(line + "\n")
            self.file.flush()


def gh_api(endpoint: str, timeout: float = 60.0):
    """调用 `gh api`，返回 (解析后的 JSON 或 None, 错误说明)。

    错误一律**返回而不抛出**：监听器必须活到下一次轮询，一次 API 抖动不该让整个服务退出
    （systemd 会重启它，但重启会丢掉当前轮的所有上下文，且日志里只剩「反复重启」）。
    """
    try:
        proc = subprocess.run(["gh", "api", endpoint], capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return None, "找不到 gh 可执行文件（PATH 里没有）"
    except subprocess.TimeoutExpired:
        return None, f"gh api {endpoint} 超时（{timeout:.0f}s）"
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", errors="ignore").strip().splitlines()
        return None, f"gh api 失败（exit {proc.returncode}）：{lines[-1] if lines else ''}"
    try:
        return json.loads(proc.stdout.decode("utf-8", errors="ignore") or "null"), None
    except ValueError as exc:
        return None, f"响应不是合法 JSON：{exc}"


def collect_runs(repo: str, lookback_seconds: float, max_pages: int) -> tuple:
    """取「本轮要看的 run 集合」。返回 (run 列表, 问题列表, 是否降级)。

    为什么是三路并集而不是只看「最近创建的 run」：nightly 这类 run 会跑一两个小时，
    它的 job 可能在 run 创建后很久才失败。只看新建的 run 会漏掉它们，故额外把
    `status=in_progress` / `queued` 的 run 全量并入（不受创建时间限制）。

    降级（degraded）的含义：任何一路查询失败 → 本轮的视野是**残缺**的，
    此时**不推进**「已扫描 run」的记账（见 run_once），否则残缺的一轮会把没看到的失败
    永久标记为「已看过」。
    """
    combined: dict = {}
    problems: list = []
    degraded = False

    for page in range(1, max(1, max_pages) + 1):
        payload, error = gh_api(f"repos/{repo}/actions/runs?per_page=100&page={page}")
        if error:
            problems.append(f"最近创建的 run（第 {page} 页）：{error}")
            degraded = True
            break
        runs = payload.get("workflow_runs") or []
        for run in runs:
            combined[run["id"]] = run
        if len(runs) < 100:
            break

    for status, why in (("in_progress", "运行中的 run"), ("queued", "排队中的 run")):
        payload, error = gh_api(f"repos/{repo}/actions/runs?status={status}&per_page=100")
        if error:
            problems.append(f"{why}：{error}")
            degraded = True
            continue
        for run in payload.get("workflow_runs") or []:
            combined[run["id"]] = run

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=lookback_seconds)
    fresh = []
    for run in combined.values():
        if run.get("status") != "completed":
            fresh.append(run)                       # 还在跑：无论多老都要盯着，它的 job 随时可能失败
            continue
        # 已结束的 run 按 **created_at 或 updated_at 任一落在窗口内** 判定：
        # 只看 created_at 会漏掉「创建于窗口之前、却在窗口内失败结束」的长 run ——
        # 实测 nightly a3 跑了一个多小时，创建时间早已在 30m 窗口之外（--lookback 补历史时最常踩）。
        # updated_at 是 GitHub 自己维护的「最后一次变动」，job 结束会更新它，正是这里需要的信号。
        stamps = [ws.parse_timestamp(run.get(field)) for field in ("created_at", "updated_at")]
        if any(stamp is not None and stamp >= cutoff for stamp in stamps) or all(
                stamp is None for stamp in stamps):
            fresh.append(run)
    return fresh, problems, degraded


class Watcher:
    def __init__(self, args):
        self.args = args
        self.dry_run = bool(args.dry_run)
        self.state_dir = os.path.abspath(args.state_dir)
        self.snapshot_dir = os.path.join(self.state_dir, "snapshots")
        self.handoff_dir = os.path.join(self.state_dir, "handoffs")
        self.analysis_log_dir = os.path.join(self.state_dir, "analysis_logs")
        if not self.dry_run:
            # 三个子目录都要**先建好**：第 1 步脚本直接 open(emit_json, "w")，不会替我们建目录，
            # 少建一个 handoffs/ 的后果是一次失败分析从头再跑（实测 FileNotFoundError）。
            for directory in (self.state_dir, self.snapshot_dir, self.handoff_dir, self.analysis_log_dir):
                os.makedirs(directory, exist_ok=True)
        self.log_path = os.path.join(self.state_dir, "watch.log")
        self.pattern = re.compile(args.path_pattern) if args.path_pattern else None
        # dry-run 不落盘：日志只上终端，台账只读不写（下面所有写入都过 mark()/mark_error()）
        self.log = Logger(None if self.dry_run else self.log_path)
        self.ledger = ws.Ledger(os.path.join(self.state_dir, "ledger.json"), read_only=self.dry_run)
        self.registry = None
        self.registry_loaded_at = None
        self.current_run_status: dict = {}   # run_id → status（本轮），供阶段判断复用

    # ---------- 台账的唯一写入口（dry-run 在此统一拦掉） ----------

    def mark(self, job_id, state: str, **fields):
        if self.dry_run:
            return {}
        return self.ledger.note(job_id, state, **fields)

    def mark_error(self, job_id, message: str) -> str:
        if self.dry_run:
            return ws.STATE_SEEN
        return self.ledger.record_error(job_id, message, self.args.max_attempts)

    # ---------- 集群注册表（常驻内存，按 --max-age-hours 刷新） ----------

    def ensure_registry(self):
        """装载集群映射。**常驻内存**：每次迭代重抓 Cluster.md 会把 3s 级快照窗口吃掉一大截。

        返回 (registry 或 None, 错误说明)。失败不抛出 —— 由调用方记 attempts，
        下一轮还会再试（集群侧配置问题是暂时的：网络、VPN、Cluster.md 抓取都可能瞬时失败）。
        """
        now = datetime.datetime.now()
        if self.registry is not None and self.registry_loaded_at is not None:
            age_hours = (now - self.registry_loaded_at).total_seconds() / 3600
            if age_hours < self.args.max_age_hours:
                return self.registry, None
        if self.args.cluster_md:
            try:
                with open(self.args.cluster_md, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError as exc:
                return None, f"读取 --cluster-md 失败：{exc}"
        else:
            fetched = fetch_cluster_md(cache_path=os.path.join(self.args.cache_dir, "Cluster.md"),
                                       max_age_hours=self.args.max_age_hours)
            if not fetched.get("text"):
                return None, f"取 Cluster.md 失败：{fetched.get('error')}"
            text = fetched["text"]
        self.registry = ClusterRegistry(parse_cluster_map(text), self.args.kubeconfig_dir)
        self.registry_loaded_at = now
        self.log(f"集群映射已载入：{len(self.registry.clusters)} 个集群、"
                 f"{len(self.registry.kubeconfigs)} 个 kubeconfig")
        return self.registry, None

    # ---------- 台账 ----------

    def record_job_seen(self, run: dict, job: dict):
        step = ws.earliest_failed_step(job)
        self.mark(
            job["id"], ws.STATE_SEEN,
            run_id=run["id"], workflow=run.get("path") or run.get("name"),
            job_name=job.get("name"), runner_name=job.get("runner_name"),
            labels=job.get("labels") or [],
            failed_step=step, failed_step_completed_at=(step or {}).get("completed_at"),
            job_status=job.get("status"), job_conclusion=job.get("conclusion"),
            seen_at=datetime.datetime.now().astimezone().isoformat(timespec="seconds"))

    def note_stale_runs(self, stale_runs: list, now=None):
        """把僵尸 run 记进日志与台账游标 —— **每个 run_id 只记一次**。

        为什么要有「只记一次」：僵尸 run 的特征就是永远保持同一个 status，若每轮都打一行，
        日志会被它刷成一片（30s 一轮 = 一天 2880 行），真正的新发现反而看不见。
        游标用 run_id 而不是计数：游标随台账持久化，进程重启后不会把同一只僵尸再报一遍。
        """
        if not stale_runs:
            return
        recorded = self.ledger.cursor("stale_runs", {}) or {}
        changed = False
        for run in stale_runs:
            run_id = str(run.get("id"))
            if run_id in recorded:
                continue
            recorded[run_id] = (now or datetime.datetime.now(datetime.timezone.utc)) \
                .isoformat(timespec="seconds")
            changed = True
            self.log(f"🧟 run {run_id}（{run.get('path')}，event={run.get('event')}）"
                     f"创建于 {run.get('created_at')} 却仍是 {run.get('status')}，"
                     f"已超 {ws.STALE_RUN_HOURS}h 未开始 —— 判为僵尸：不计入活动、不再查它的 jobs")
        if changed:
            self.ledger.set_cursor("stale_runs", recorded)

    # ---------- 阶段 A：抢集群快照 ----------

    def take_snapshot(self, job: dict) -> tuple:
        """抢下一个失败 job 的集群快照，落盘并返回 (快照路径, 是否拿到 pod 实证)。

        复用第 2 步的 `step3_cluster_forensics`，**不重写任何集群逻辑**：
        pod 定位、时序自洽校验、容器日志（含重启前实例）、标签可用性核查都在那里面，
        这里只负责「什么时候调、拿到之后写哪」。
        """
        window = ws.snapshot_window(job)
        case = {
            "job_id": job["id"], "run_id": job.get("run_id"),
            "labels": job.get("labels") or [], "runner_name": job.get("runner_name"),
            "_repo": self.args.repo,
            "failed_step_started_at": window[0], "failed_step_completed_at": window[1],
            "job_started_at": job.get("started_at"), "job_completed_at": job.get("completed_at"),
        }
        # 阶段 A 的 args：第 2 步只用到 repo 与 no_pod_logs；快照当然要抓日志（否则取证毫无意义）
        step_args = argparse.Namespace(repo=self.args.repo, no_pod_logs=False)
        errors: list = []
        sessions: dict = {}      # 每轮新建：pod 列表必须现查，缓存的 pod 列表不是「失败时刻的现场」
        cluster_result = pipeline.step3_cluster_forensics(
            case, self.registry, sessions, step_args, errors)
        now = datetime.datetime.now().astimezone()
        taken_reason = ("job 仍在运行，pod 必然还在承载它" if job.get("status") != "completed"
                        else "job 刚结束，pod 可能尚未回收")
        payload = {
            "job_id": job["id"], "run_id": job.get("run_id"),
            "taken_at": now.isoformat(timespec="seconds"),
            "job_status_at_snapshot": job.get("status"),
            "taken_reason": taken_reason,
            "workflow": job.get("workflow"), "job_name": job.get("job_name"),
            "labels": job.get("labels") or [], "runner_name": job.get("runner_name"),
            "failed_step": job.get("failed_step"),
            "collect_errors": errors,
            "cluster_result": cluster_result,
        }
        path = os.path.join(self.snapshot_dir, f"job_{job['id']}.json")
        write_json_atomic(path, payload)
        return path, bool(cluster_result.get("pod_evidence"))

    def phase_a(self, jobs: list):
        """阶段 A：对「首次看到且窗口还在」的 job 抢快照。"""
        for job in jobs:
            job_id = job["id"]
            record = self.ledger.entry(job_id)
            if record.get("snapshot_attempted"):
                continue                      # 每个 job 只抢一次（见模块头「快照只抢一次」）
            worth, why = ws.should_take_snapshot(job)
            if not worth:
                # 不可伪装成「已取证」：明确记下没抢到快照以及原因
                self.mark(job_id, ws.STATE_SNAPSHOT_MISSED, snapshot_attempted=True,
                                 snapshot_note=why)
                self.log(f"⏭️  job {job_id} 不抢快照：{why}")
                continue
            if self.args.dry_run:
                self.log(f"💡[dry-run] job {job_id} 将抢集群快照（{why}）")
                continue
            registry, error = self.ensure_registry()
            if registry is None:
                # 集群侧暂时不可用 → 记 attempts 下轮再试，而不是直接判「没取证」
                state = self.mark_error(job_id, f"集群映射不可用：{error}")
                self.log(f"⚠️  job {job_id} 抢快照失败（{state}）：{error}")
                continue
            try:
                path, has_evidence = self.take_snapshot(job)
            except Exception as exc:                     # noqa: BLE001 — 单 job 失败不能拖垮整轮
                state = self.mark_error(job_id, f"抢快照异常：{exc}")
                self.log(f"⚠️  job {job_id} 抢快照异常（{state}）：{exc}")
                continue
            self.mark(job_id, ws.STATE_SNAPSHOT_OK, snapshot_attempted=True,
                             snapshot_path=path, snapshot_evidence=has_evidence,
                             snapshot_note=(None if has_evidence else
                                            "快照已抢下，但未取到本 job 的 pod 实证（详见报告）"))
            mark = "🅿️" if has_evidence else "📋"
            self.log(f"{mark} job {job_id} 快照已抢下（{'取得 pod 实证' if has_evidence else '仅取得标签可用性核查'}）："
                     f"{os.path.basename(path)}")

    # ---------- 阶段 B：日志分类 + 报告 ----------

    def phase_b(self, jobs: list):
        """阶段 B：job 结束后补日志分类 + 集群取证（并入快照）+ 历史归因 + 报告。"""
        for run_id, group in sorted(ws.group_by_run(jobs).items()):
            self.process_run(run_id, group)

    def process_run(self, run_id: int, group: list):
        job_ids = [job["id"] for job in group]
        handoff_path = os.path.join(self.handoff_dir, f"handoff_run_{run_id}.json")
        # 已经定向分析过（handoff 还在）的 job 不重复跑第 1 步：重试只补第 2~5 步
        analyzed = all(self.ledger.entry(jid).get("state") == ws.STATE_ANALYZED for jid in job_ids)
        if not (analyzed and os.path.exists(handoff_path)):
            if self.args.dry_run:
                self.log(f"💡[dry-run] run {run_id} 将定向分析 job {job_ids}")
                return
            command = [sys.executable, ANALYSIS_SCRIPT, "--repo", self.args.repo,
                       "--chips", self.args.chips, "--run-id", str(run_id),
                       "--report-dir", self.analysis_log_dir,
                       "--emit-json", handoff_path]
            for jid in job_ids:
                command += ["--job-id", str(jid)]
            completed = run_command(command, timeout=900)
            if completed is None or completed.returncode != 0:
                detail = (completed.stderr.decode("utf-8", errors="ignore")[-400:]
                          if completed is not None else "超时")
                for jid in job_ids:
                    state = self.mark_error(jid, f"第 1 步定向分析失败：{detail}")
                    self.log(f"⚠️  job {jid} 第 1 步失败（{state}）：{detail.strip()[:200]}")
                return
            try:
                with open(handoff_path, encoding="utf-8") as fh:
                    handoff = json.load(fh)
            except (OSError, ValueError) as exc:
                for jid in job_ids:
                    state = self.mark_error(jid, f"handoff 不可读：{exc}")
                    self.log(f"⚠️  job {jid} 的 handoff 不可读（{state}）：{exc}")
                return
            present = {item.get("job_id") for item in handoff.get("failed_jobs") or []}
            for jid in job_ids:
                if jid in present:
                    self.mark(jid, ws.STATE_ANALYZED, handoff_path=handoff_path)
                else:
                    # 没进失败清单 = 它不在 --chips 范围，或按脚本的判据不算失败 job。
                    # 这是**终态**：下轮不再重复跑一次必然同样结果的定向分析。
                    self.mark(jid, ws.STATE_NOT_A_FAILURE, handoff_path=handoff_path,
                                     snapshot_note=f"定向分析未把 job {jid} 计入失败清单"
                                                   f"（不在 --chips {self.args.chips} 范围，或未被判为失败 job）")
                    self.log(f"⏭️  job {jid} 不在失败清单内（--chips {self.args.chips}），不再分析")
        else:
            self.log(f"run {run_id}：复用已生成的 handoff（job {job_ids} 只补第 2~5 步）")

        pending = [jid for jid in job_ids
                   if self.ledger.entry(jid).get("state") == ws.STATE_ANALYZED]
        if not pending:
            return

        if self.args.dry_run:
            self.log(f"💡[dry-run] run {run_id} 将跑第 2~5 步（含快照）并出报告：job {pending}")
            return

        command = [sys.executable, PIPELINE_SCRIPT, "--handoff", handoff_path,
                   "--repo", self.args.repo, "--chips", self.args.chips,
                   "--cluster-snapshot", self.snapshot_dir,
                   "--report-dir", self.args.report_dir, "--cache-dir", self.args.cache_dir,
                   "--max-age-hours", str(self.args.max_age_hours)]
        if self.args.cluster_md:
            command += ["--cluster-md", self.args.cluster_md]
        completed = run_command(command, timeout=1800)
        if completed is None or completed.returncode != 0:
            detail = (completed.stderr.decode("utf-8", errors="ignore")[-400:]
                      if completed is not None else "超时")
            for jid in pending:
                state = self.mark_error(jid, f"第 2~5 步失败：{detail}")
                self.log(f"⚠️  job {jid} 第 2~5 步失败（{state}）：{detail.strip()[:200]}")
            return

        stdout = completed.stdout.decode("utf-8", errors="ignore")
        report_path = extract_report_path(stdout)
        for jid in pending:
            self.mark(jid, ws.STATE_REPORTED, report_path=report_path)
        self.log(f"📄 run {run_id} 报告完成：{report_path or '（未从输出中解析到报告路径）'}")
        if self.args.notify_command:
            self.notify(report_path, pending)

    def notify(self, report_path, job_ids):
        """执行用户自备的通知命令。失败只记日志 —— 通知是附属动作，不该影响台账状态。"""
        try:
            command = shlex.split(self.args.notify_command) + [report_path or "", ",".join(map(str, job_ids))]
            subprocess.run(command, capture_output=True, timeout=60)
            self.log(f"通知命令已执行：{self.args.notify_command}")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            self.log(f"⚠️ 通知命令失败（不影响报告）：{exc}")

    # ---------- 主循环 ----------

    def run_once(self) -> bool:
        """跑一轮。返回「本轮是否有目标 run 在活动」（供自适应间隔判定）。"""
        lookback = parse_duration(self.args.lookback)
        runs, problems, degraded = collect_runs(self.args.repo, lookback, self.args.max_pages)
        for problem in problems:
            self.log(f"⚠️  {problem}")
        if runs == [] and problems:
            # 三路查询全灭（通常是 gh 未登录或断网）→ 本轮什么都没看到，交给上层退避
            raise RuntimeError("；".join(problems))

        in_scope_all = [run for run in runs
                        if self.pattern is None or ws.is_in_scope(run, self.pattern)]
        # 僵尸 run 单独拎出来（见 ws.is_stale_run）：它们既不算「有活动」，也不再每轮查 /jobs。
        # 但**台账里已有的 job 仍要往下走**，否则「这个 run 被跳过了」会把没分析完的失败一起吞掉。
        now = datetime.datetime.now(datetime.timezone.utc)
        in_scope, stale_runs = [], []
        for run in in_scope_all:
            (stale_runs if ws.is_stale_run(run, now) else in_scope).append(run)
        self.note_stale_runs(stale_runs, now)
        self.current_run_status = {run["id"]: run.get("status") for run in in_scope}
        stale_note = (f"（其中 {len(stale_runs)} 个是 > {ws.STALE_RUN_HOURS}h 未开始的僵尸 run，"
                      f"已跳过：不计活动、不查 jobs）" if stale_runs else "")
        self.log(f"本轮：仓库 {len(runs)} 个 run，落在监听范围内 {len(in_scope_all)} 个{stale_note}"
                 + ("（⚠️ 降级：有查询失败，本轮不推进「已扫描」记账）" if degraded else ""))

        candidates: list = []
        for run in stale_runs:
            candidates.extend(self.candidates_from_ledger(run["id"]))

        for run in in_scope:
            run_id = run["id"]
            scanned = self.ledger.cursor("scanned_runs", {}) or {}
            if run.get("status") == "completed" and str(run_id) in scanned:
                # 已结束且扫过的 run 不再查 jobs（省一次 API 调用），但**它在台账里还没处理完的 job
                # 必须继续往下走**：否则「run 扫完了」会把没分析完的 job 一起吞掉。
                candidates.extend(self.candidates_from_ledger(run_id))
                continue
            payload, error = gh_api(f"repos/{self.args.repo}/actions/runs/{run_id}/jobs?per_page=100")
            if error:
                self.log(f"⚠️  run {run_id} 的 jobs 取不到：{error}")
                continue
            for job in payload.get("jobs") or []:
                job["run_id"] = run_id
                job["workflow"] = run.get("path")
                if not self.job_in_scope(job):
                    continue
                if not self.ledger.seen(job["id"]):
                    self.log(f"🆕 发现失败 job {job['id']}（run {run_id}，{job.get('name')}）"
                             + ("[dry-run]" if self.dry_run else ""))
                    self.record_job_seen(run, job)
                if not self.ledger.is_terminal(job["id"]):
                    candidates.append(job)
            if run.get("status") == "completed" and not degraded:
                # 只在**视野完整**的一轮里记账：降级轮漏看的失败若被记成「已扫描」，就永远不会再被看到
                recorded = self.ledger.cursor("scanned_runs", {}) or {}
                recorded[str(run_id)] = run.get("updated_at")
                self.ledger.set_cursor("scanned_runs", recorded)
        self.drain_stale_scans()

        # 阶段 A：抢快照（此刻 pod 必在）；阶段 B：job 结束后补日志与报告
        self.phase_a(candidates)
        ready = [job for job in candidates if ws.ready_for_analysis(job)[0]]
        ready_ids = {job["id"] for job in ready}
        for job in candidates:
            if job["id"] in ready_ids:
                continue
            # 结束前确认「其实不是失败」（如步骤带 continue-on-error）→ 终态，避免把成功 job 写进报告
            reason = ws.not_a_failure_reason(job)
            if reason:
                self.mark(job["id"], ws.STATE_NOT_A_FAILURE, snapshot_note=reason)
                self.log(f"⏭️  job {job['id']} {reason}")
        self.phase_b(ready)

        if not self.args.dry_run:
            self.ledger.save()
        counts = ", ".join(f"{state}={count}" for state, count in sorted(self.ledger.summary().items()))
        self.log(f"台账：{counts or '（空）'}")
        active = any(status != "completed" for status in self.current_run_status.values())
        return active or bool(self.ledger.pending(PHASE_B_STATES))

    def candidates_from_ledger(self, run_id: int) -> list:
        """从台账里把某个**已结束**run 中还没处理完的 job 还原成 job 字典。

        为什么需要这一步：`scanned_runs` 记账避免了重复取 jobs，但台账里可能还有刚记下、
        还没来得及分析（或分析失败待重试）的 job。若不还原，这些 job 会随着「run 已扫描」被
        永久跳过 —— 表现为「失败了、台账里有、但报告里没有」，是最难发现的那类丢失。

        用 status="completed" 是**可靠推断**而不是猜测：run 已结束 ⇒ 它的所有 job 都已结束。
        """
        restored = []
        for record in self.ledger.pending(PHASE_B_STATES):
            if record.get("run_id") != run_id:
                continue
            restored.append({
                "id": record["job_id"], "run_id": run_id,
                "name": record.get("job_name"), "workflow": record.get("workflow"),
                "runner_name": record.get("runner_name"), "labels": record.get("labels") or [],
                "failed_step": record.get("failed_step"),
                "status": "completed",
                # 失败步骤结束时间是 job 结束时间的下界：用它判「pod 是否已回收」只会更保守，
                # 不会把早已回收的 pod 当成还在（见 watch_state.should_take_snapshot）
                "completed_at": record.get("failed_step_completed_at"),
                "from_ledger": True,
            })
        return restored

    def job_in_scope(self, job: dict) -> bool:
        """job 级范围判定：必须有失败步骤，且 runner 标签落在芯片范围内。

        芯片判定交给第 1 步脚本（它按 job 级 labels 判，见 discover_by_run_ids）——
        这里只做「有失败步骤」这一条，避免两处范围规则打架。
        """
        return ws.earliest_failed_step(job) is not None

    def drain_stale_scans(self):
        """修剪「已扫描 run」记账：只保留最近 500 条，防止台账无限增长。

        按 run_id 数字大小近似按时间排序（run_id 单调递增），保留最大的 500 个即可。
        """
        scanned = self.ledger.cursor("scanned_runs", {}) or {}
        if len(scanned) <= 500:
            return
        keep = sorted(scanned, key=lambda key: int(key))[-500:]
        self.ledger.set_cursor("scanned_runs", {key: scanned[key] for key in keep})

    def run_forever(self):
        backoff = 0.0
        while not _STOP_REQUESTED[0]:
            try:
                active = self.run_once()
                backoff = 0.0
            except Exception as exc:                             # noqa: BLE001 — 常驻服务不能因单轮异常退出
                backoff = min(max(backoff * 2, self.args.interval), MAX_BACKOFF_SECONDS)
                # 打完整调用栈：常驻服务出问题时，日志里只有一句异常消息是没法定位的
                self.log(f"❌ 本轮整体失败（{exc}）；{backoff:.0f}s 后重试\n"
                         + traceback.format_exc())
                active = True                                    # 失败后按快节奏重试，别退到空闲档
            wait = ws.next_interval(active, self.args.interval, self.args.idle_interval)
            if backoff:
                wait = max(wait, backoff)
            self.log(f"⏳ 下一轮：{wait:.0f}s 后（{'目标 run 活动中/有待办' if active else '空闲'}）")
            sleep_seconds(wait)


def run_command(command: list, timeout: float):
    """执行子进程；超时返回 None（调用方按失败处理）。"""
    try:
        return subprocess.run(command, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def extract_report_path(stdout: str):
    """从第 2~5 步的输出里取报告路径（它自己打印的那一行就是权威来源，不猜文件名）。"""
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if stripped.startswith("报告:"):
            return stripped.split("报告:", 1)[1].strip()
    return None


def write_json_atomic(path: str, payload: dict):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    os.replace(tmp_path, path)


def sleep_seconds(seconds: float):
    """可被 SIGTERM/SIGINT 打断的睡眠。

    systemd 停服务时不必等满一个空闲间隔（默认 300s）才退出；被打断则抛 KeyboardInterrupt，
    由 run_forever 的外层循环收尾并保存台账。
    """
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if _STOP_REQUESTED[0]:
            raise KeyboardInterrupt
        time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))


def main():
    args = parse_args()
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    watcher = Watcher(args)
    watcher.log(f"=== 监听启动（repo={args.repo}，范围 /{args.path_pattern}/，"
                f"间隔 {args.interval:.0f}s / 空闲 {args.idle_interval:.0f}s，"
                f"回看 {args.lookback}{'，dry-run（不落盘）' if args.dry_run else ''}）===")
    if args.once:
        try:
            watcher.run_once()
        except Exception as exc:                                 # noqa: BLE001
            watcher.log(f"❌ 本轮失败：{exc}\n" + traceback.format_exc())
            return 1
        return 0
    try:
        watcher.run_forever()
    except KeyboardInterrupt:
        pass
    if not args.dry_run:
        watcher.ledger.save()
    watcher.log("=== 收到停止信号，退出 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
