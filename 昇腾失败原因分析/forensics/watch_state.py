"""监听台账与触发判据：决定「哪次失败该在什么时候做什么」，并保证每个失败只做一次。

为什么这层要单独成一个模块：监听器的核心风险不是「跑不起来」，而是**重复分析**与**静默丢弃**：
  - 重复分析：轮询每 30s 一次，同一个失败会被反复看到。若没有台账，一次失败会产出几十份报告，
    并把 GitHub 的日志 API 调用量放大几十倍（日志下载是整条链里最贵的一步）。
  - 静默丢弃：某个 job 因瞬时网络错误分析失败，若直接跳过就再也不会被处理 —— 报告里没有、
    台账里也没有，看起来像「这个失败不存在」。故失败要记 attempts 并最终置 gave_up，
    留下「我知道它失败了但我没能分析」的痕迹。

台账键是 **job_id** 而不是 run_id：同一个 run 里可能有多个 job 先后失败
（实测一次 nightly a2 运行里失败 job 与后续 job 相隔数十分钟），按 run 记会把后失败的那个吞掉。

⚠️ 与 npu_ci_failure_analysis.py 的一处**有意重复**：earliest_failed_step()。
   那份实现在脚本里（脚本是模块级流程、不可 import），而监听器必须在调脚本**之前**
   就知道失败步骤的时间窗 —— 那正是抢集群快照要用的锚点。两份实现必须保持同一规则
   （「序号最靠前的失败步骤」），否则监听器抢快照用的窗口会和分类用的窗口不一致。
"""
from __future__ import annotations

import datetime
import json
import os

# 台账状态机：
#   seen ──► snapshot_ok / snapshot_missed ──► analyzed ──► reported
#   seen ──► not_a_failure（job 结束后确认并非失败，如 continue-on-error 的步骤）
#   任一态 ──(出错累积到上限)──► gave_up
STATE_SEEN = "seen"
STATE_SNAPSHOT_OK = "snapshot_ok"
STATE_SNAPSHOT_MISSED = "snapshot_missed"
STATE_ANALYZED = "analyzed"
STATE_REPORTED = "reported"
STATE_NOT_A_FAILURE = "not_a_failure"
STATE_GAVE_UP = "gave_up"

# 终态：不再需要任何后续动作
TERMINAL_STATES = {STATE_REPORTED, STATE_NOT_A_FAILURE, STATE_GAVE_UP}

# 失败步骤结束后多久之内仍然值得抢集群快照（秒）。
# 实测「失败步骤结束 → job 结束」固定 55~56s（3 例），且 job 结束前后 pod 即被回收；
# 留 90s 是给「步骤结束后还有 always() 收尾步骤」的情况一点余量，而不是假装窗口很大。
SNAPSHOT_GRACE_SECONDS = 90

LEDGER_VERSION = 1


def parse_timestamp(value):
    """解析 GitHub 的 ISO 时间戳（Z 结尾），失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def earliest_failed_step(job: dict):
    """取「序号最靠前的失败步骤」，与 npu_ci_failure_analysis.py 的同名函数同一规则。

    一次 job 失败常伴随多个步骤 conclusion=failure（后续是级联失败），
    只有序号最靠前的那个才是根因 —— 它同时也是集群取证的**时间锚点**。
    """
    failed_steps = [step for step in (job.get("steps") or [])
                    if step.get("conclusion") == "failure"]
    if not failed_steps:
        return None
    step = min(failed_steps, key=lambda item: item.get("number") or 9999)
    return {"name": step.get("name") or "", "number": step.get("number"),
            "started_at": step.get("started_at"), "completed_at": step.get("completed_at")}


def is_in_scope(run: dict, path_pattern) -> bool:
    """run 是否落在监听范围内（按 workflow 文件路径匹配）。"""
    path = run.get("path") or ""
    return bool(path_pattern.search(path))


def snapshot_window(job: dict) -> tuple:
    """抢快照用的时间锚点：(失败步骤开始, 失败步骤结束)。

    缺失时退回 job 自身的起止时间 —— 宁可窗口粗一点，也不要因为一个字段缺失就放弃取证。
    """
    step = earliest_failed_step(job) or {}
    return (step.get("started_at") or job.get("started_at"),
            step.get("completed_at") or job.get("completed_at"))


def should_take_snapshot(job: dict, now=None) -> tuple:
    """现在是否还值得为这个 job 抢集群快照。返回 (是否抢, 说明)。

    判据是「pod 是否可能还在」：job 未结束 → 一定在（pod 正在跑它）；
    job 刚结束（≤ SNAPSHOT_GRACE_SECONDS）→ 可能还在（回收有延迟），值得一试；
    更早结束的 → 已回收，抢快照只会得到同标签的**别的** pod，那是假证据，宁可不抢。
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if job.get("status") != "completed":
        return True, f"job 仍在运行（status={job.get('status')}），pod 必然还在承载它"
    completed_at = parse_timestamp(job.get("completed_at"))
    if completed_at is None:
        return True, "job 已结束但没有 completed_at，无法判断回收时间，按仍可取处理"
    elapsed = (now - completed_at).total_seconds()
    if elapsed <= SNAPSHOT_GRACE_SECONDS:
        return True, f"job 刚结束 {elapsed:.0f}s（≤{SNAPSHOT_GRACE_SECONDS}s），pod 可能尚未回收"
    return False, (f"job 已结束 {elapsed / 60:.1f} 分钟，pod 早已回收 —— "
                   f"此时再查只能查到同标签的其它 pod，属假证据，故不抢快照")


def ready_for_analysis(job: dict) -> tuple:
    """现在能否进入阶段 B（日志分类）。返回 (可否, 说明)。

    硬条件：job 必须已结束 —— GitHub 的 job 日志在 job 结束前取不到（实测 404 BlobNotFound，
    blob 尚未生成）。这不是保守，是接口事实。
    """
    if job.get("status") != "completed":
        return False, f"job 未结束（status={job.get('status')}），其日志此刻还取不到（404 BlobNotFound）"
    return True, ""


def not_a_failure_reason(job: dict):
    """job 结束后确认「其实不是失败」时的说明；确属失败则返回 None。

    为什么要这一关：触发条件是「某个步骤 conclusion=failure」，而 step 级失败不等于 job 失败 ——
    `continue-on-error: true` 的步骤失败后 job 仍判成功。不拦这一下，会把一个成功 job
    当失败分析并写进报告。
    """
    if job.get("status") != "completed":
        return None
    conclusion = job.get("conclusion")
    if conclusion in ("failure", "timed_out", "startup_failure"):
        return None
    return (f"job 结束后 conclusion={conclusion}（非失败）—— 触发它的失败步骤可能带 "
            f"continue-on-error，不作为失败分析")


def group_by_run(jobs: list) -> dict:
    """把待分析 job 按 run 分组：一个 run 一次调用即可，省掉重复的 API 调用与去重开销。"""
    grouped: dict = {}
    for job in jobs:
        grouped.setdefault(job["run_id"], []).append(job)
    return grouped


def next_interval(had_target_activity: bool, interval: float, idle_interval: float) -> float:
    """自适应轮询间隔。

    为什么必须自适应：目标 workflow 一天里大部分时间没有 run 在跑，若一直 30s 轮询，
    一天要发约 2880 次 API 调用却几乎全是空转；而一旦有 run 在跑，就必须快 ——
    快照窗口只有几十秒（见 SNAPSHOT_GRACE_SECONDS）。
    """
    return float(interval) if had_target_activity else float(idle_interval)


class Ledger:
    """JSON 文件台账：{jobs: {job_id: {...}}, cursors: {...}}。写入采用「临时文件 + rename」原子替换。

    read_only=True（监听器 --dry-run）时 save() 直接返回：**把「不落盘」做成台账自身的属性**，
    而不是在每个调用点写 `if not dry_run: ledger.save()`。理由与 persist_infra_store 那次教训相同 ——
    散在调用点的守卫，只要漏掉一处就会写出去，而且只有真实运行才能暴露。
    """

    def __init__(self, path: str, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        self.data = {"version": LEDGER_VERSION, "cursors": {}, "jobs": {}}
        self.loaded = False
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            # 台账损坏不能导致「全部重新分析一遍」：宁可从头记（并留下备份），
            # 也不要静默丢弃历史状态 —— 后者会让监听器重启后重复分析已处理过的失败。
            try:
                os.replace(self.path, self.path + ".corrupt")
            except OSError:
                pass
            return
        if isinstance(payload, dict):
            self.data = {"version": payload.get("version", LEDGER_VERSION),
                         "cursors": payload.get("cursors") or {},
                         "jobs": payload.get("jobs") or {}}
            self.loaded = True

    def save(self):
        if self.read_only:
            return False
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp_path, self.path)
        return True

    # ---------- 游标 ----------

    def cursor(self, key: str, default=None):
        return self.data["cursors"].get(key, default)

    def set_cursor(self, key: str, value):
        self.data["cursors"][key] = value

    # ---------- job 记录 ----------

    def entry(self, job_id) -> dict:
        return self.data["jobs"].get(str(job_id)) or {}

    def seen(self, job_id) -> bool:
        return str(job_id) in self.data["jobs"]

    def is_terminal(self, job_id) -> bool:
        return self.entry(job_id).get("state") in TERMINAL_STATES

    def note(self, job_id, state: str, **fields):
        """登记/更新一个 job 的状态与附加字段。"""
        key = str(job_id)
        record = self.data["jobs"].setdefault(key, {})
        record["state"] = state
        record["updated_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        record.update({k: v for k, v in fields.items() if v is not None})
        return record

    def record_error(self, job_id, message: str, max_attempts: int) -> str:
        """记一次失败并累加 attempts；达到上限则置 gave_up。返回新状态。

        gave_up 是**留痕**而不是放弃：台账里会保留 attempts 与最后一次错误，
        报告/日志里能看到「这个失败被看到了但没能分析」。
        """
        key = str(job_id)
        record = self.data["jobs"].setdefault(key, {})
        attempts = int(record.get("attempts") or 0) + 1
        state = STATE_GAVE_UP if attempts >= max_attempts else record.get("state") or STATE_SEEN
        record.update({"attempts": attempts, "last_error": str(message)[:500],
                       "state": state,
                       "updated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")})
        return state

    def pending(self, states) -> list:
        """处于给定状态集合中的记录（按 job_id 排序，便于复现）。"""
        wanted = set(states)
        return [{"job_id": int(key), **record} for key, record in sorted(self.data["jobs"].items())
                if record.get("state") in wanted]

    def summary(self) -> dict:
        """各状态计数，供日志与 --once 输出。"""
        counts: dict = {}
        for record in self.data["jobs"].values():
            state = record.get("state") or "unknown"
            counts[state] = counts.get(state, 0) + 1
        return counts
