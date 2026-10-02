#!/usr/bin/env python3
"""昇腾 CI 失败根因分析（集群取证版）—— 五段流水线的驱动入口。

把「上游 workflow CI 失败 → 失败日志提取 → 借助 kubeconfig 排查现场 →
历史问题定位归因 → 输出根因与修复建议」串成一条可复现的流程：

  [第1步] 上游 CI 失败 + 失败日志提取
          → 复用 npu_ci_failure_analysis.py（29 桶正则 + 步骤时间窗 + 按 run 去重），
            以 --emit-json 导出结构化结果作为交接面。
  [第2步] 借助 kubeconfig 排查现场
          → forensics.cluster_forensics：pod 状态 / 容器退出码与终止原因 / 容器日志 /
            runner 标签可用性核查。
  [第4步] 历史问题定位归因
          → forensics.issue_knowledge：以 ascend-gha-runners/docs 的 179 个 issue
            （含 269 条评论）为知识库，按**错误签名**匹配先例。
  [第5步] 输出根因 + 修复建议
          → forensics.report：三层证据并列、冲突显式标出、未取证如实记录。

用法：
  python3 npu_ci_forensics.py                        # 跑分析 + 集群取证 + 历史归因
  python3 npu_ci_forensics.py --handoff a.json       # 复用已有分析结果，不重跑第 1 步
  python3 npu_ci_forensics.py --self-check-only      # 只做集群连通性/身份自检
  python3 npu_ci_forensics.py --offline              # 只用本地缓存，不联网
  python3 npu_ci_forensics.py --chips a2,a3 --since 2026-09-01 --max-cases 20

设计纪律（详见 forensics/__init__.py 与 forensics/report.py 的模块注释）：
  - 集群身份必须自检；runner 标签后缀**不能**用于判集群。
  - 集群侧只执行只读动词（白名单在 cluster_forensics.READ_ONLY_VERBS）。
  - 拿不到证据就写「未取证」，绝不用推测填充结论。
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from forensics import cluster_forensics as cluster_ops          # noqa: E402
from forensics.cluster_registry import (ClusterRegistry, fetch_cluster_md,  # noqa: E402
                                        parse_cluster_map, DEFAULT_KUBECONFIG_DIR)
from forensics.issue_knowledge import (IssueIndex, extract_signatures,  # noqa: E402
                                       fetch_comments, fetch_issues, keywords_for)
from forensics.knowledge_tables import knowledge_for           # noqa: E402
from forensics.limitations import select_limitations           # noqa: E402
from forensics.report import render_report, synthesize         # noqa: E402

ANALYSIS_SCRIPT = os.path.join(BASE_DIR, "npu_ci_failure_analysis.py")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    # 第 1 步参数（透传给 npu_ci_failure_analysis.py）
    parser.add_argument("--repo", default="vllm-project/vllm-ascend")
    parser.add_argument("--since", default=None, help="起始日期 YYYY-MM-DD，默认近 7 天")
    parser.add_argument("--chips", default="a2,a3", help="芯片范围，空字符串=不限")
    parser.add_argument("--samples", type=int, default=40, help="日志侧采样数（透传）")
    parser.add_argument("--handoff", default=None,
                        help="复用已有的分析结果 JSON（npu_ci_failure_analysis.py --emit-json 的产物），"
                             "跳过第 1 步")
    parser.add_argument("--no-run-analysis", action="store_true",
                        help="不自动运行第 1 步分析，必须配合 --handoff")
    # 集群侧参数
    parser.add_argument("--kubeconfig-dir", default=DEFAULT_KUBECONFIG_DIR,
                        help=f"kubeconfig 目录（默认 {DEFAULT_KUBECONFIG_DIR}）")
    parser.add_argument("--cluster-md", default=None,
                        help="本地 Cluster.md 路径；缺省则从 ascend-gha-runners/docs 抓取并缓存")
    parser.add_argument("--max-cases", type=int, default=15, help="最多对多少个失败 job 做集群取证")
    parser.add_argument("--no-pod-logs", action="store_true", help="不抓容器日志（只取 pod 状态）")
    parser.add_argument("--cluster-snapshot", default=None, metavar="PATH",
                        help="复用监听器（npu_ci_watch.py）在失败时刻抢下的集群快照（文件或目录）。"
                             "命中快照的 job 直接采用该快照，**不再现场查集群** —— 事后补查到的"
                             "是另一个时刻的现场（runner pod 一次性的，job 结束即回收）")
    parser.add_argument("--self-check-only", action="store_true",
                        help="只做集群连通性与身份自检后退出")
    # 输出与缓存
    parser.add_argument("--cache-dir", default=os.path.join(BASE_DIR, ".forensics_cache"),
                        help="知识库/Cluster.md 的本地缓存目录")
    parser.add_argument("--report-dir", default=os.path.join(BASE_DIR, "npu_ci_reports"),
                        help="报告输出目录")
    parser.add_argument("--offline", action="store_true", help="只用本地缓存，不发起任何网络请求")
    parser.add_argument("--max-age-hours", type=float, default=24.0, help="缓存有效期（小时）")
    return parser.parse_args()


# ---------------- 各步骤 ----------------

def step1_get_handoff(args, cache_dir: str) -> dict:
    """取得第 1 步的结构化结果：复用 --handoff，或运行分析脚本导出。"""
    if args.handoff:
        with open(args.handoff, encoding="utf-8") as fh:
            payload = json.load(fh)
        payload.setdefault("meta", {})["handoff_source"] = args.handoff
        return payload
    if args.no_run_analysis:
        raise SystemExit("指定了 --no-run-analysis 但未提供 --handoff，无第 1 步结果可用")

    handoff_path = os.path.join(cache_dir, "analysis_handoff.json")
    os.makedirs(cache_dir, exist_ok=True)
    command = [sys.executable, ANALYSIS_SCRIPT, "--repo", args.repo, "--chips", args.chips,
               "--samples", str(args.samples), "--emit-json", handoff_path]
    if args.since:
        command += ["--since", args.since]
    print(f"[第1步] 运行分析脚本导出结构化结果……\n        {' '.join(command)}")
    completed = subprocess.run(command, capture_output=True)
    if completed.returncode != 0 or not os.path.exists(handoff_path):
        sys.stderr.write(completed.stderr.decode("utf-8", errors="ignore")[-2000:])
        raise SystemExit(f"第 1 步分析失败（returncode={completed.returncode}）")
    with open(handoff_path, encoding="utf-8") as fh:
        payload = json.load(fh)
    payload.setdefault("meta", {})["handoff_source"] = handoff_path
    return payload


def step2_load_registry(args, cache_dir: str):
    """装载集群注册表：Cluster.md（本地/抓取）+ kubeconfig 目录。"""
    if args.cluster_md:
        with open(args.cluster_md, encoding="utf-8") as fh:
            cluster_md_text = fh.read()
        source = args.cluster_md
    elif args.offline:
        cached = os.path.join(cache_dir, "Cluster.md")
        if not os.path.exists(cached):
            raise SystemExit(f"--offline 但缓存不存在: {cached}")
        with open(cached, encoding="utf-8") as fh:
            cluster_md_text = fh.read()
        source = f"{cached}(缓存)"
    else:
        fetched = fetch_cluster_md(cache_path=os.path.join(cache_dir, "Cluster.md"),
                                   max_age_hours=args.max_age_hours)
        if not fetched["text"]:
            raise SystemExit(f"无法取得 Cluster.md: {fetched['error']}")
        cluster_md_text = fetched["text"]
        source = f"ascend-gha-runners/docs（{fetched['source']}）"

    registry = ClusterRegistry(parse_cluster_map(cluster_md_text), args.kubeconfig_dir)
    print(f"[第2步] 集群映射来源: {source}；解析到 {len(registry.clusters)} 个集群、"
          f"{len(registry.kubeconfigs)} 个 kubeconfig")
    return registry


def load_cluster_snapshots(path: str | None) -> dict:
    """读取监听器在失败时刻抢下的集群快照，返回 {job_id: 快照字典}。

    为什么第 2 步要接受「快照」这种输入，而不是每次都现场查集群：
      runner pod 是一次性的，job 结束即被回收。实测「失败步骤结束 → job 结束」固定 55s，
      而 GitHub 的 job 日志在 job 结束前取不到（404 BlobNotFound）—— 即
      「能取日志的时刻」与「pod 还活着的时刻」几乎不重叠。监听器因此在 job 尚未结束时
      抢下快照，事后与日志侧结论合并。快照一旦存在就必须直接采信：此刻再查集群看到的
      是**另一个**现场（同标签的其它 pod），比快照弱得多。

    path 可以是单个 JSON 文件，也可以是一个目录（取其中所有 *.json）。
    """
    if not path:
        return {}
    candidates = []
    if os.path.isdir(path):
        candidates = [os.path.join(path, name) for name in sorted(os.listdir(path))
                      if name.endswith(".json")]
    elif os.path.exists(path):
        candidates = [path]
    else:
        raise SystemExit(f"--cluster-snapshot 路径不存在: {path}")

    snapshots, problems = {}, []
    for file_path in candidates:
        try:
            with open(file_path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            problems.append(f"{os.path.basename(file_path)} 读取失败：{exc}")
            continue
        job_id = payload.get("job_id")
        if not job_id:
            problems.append(f"{os.path.basename(file_path)} 缺 job_id，无法关联到失败 job")
            continue
        payload.setdefault("snapshot_file", file_path)
        snapshots[int(job_id)] = payload
    print(f"[第2步] 集群快照：载入 {len(snapshots)} 个"
          + (f"，{len(problems)} 个不可用（{'；'.join(problems)}）" if problems else ""))
    return snapshots


def apply_cluster_snapshot(cluster_result: dict, snapshot: dict) -> dict:
    """把快照内容贴进 cluster_result，并留下**来源与时刻**（报告要如实标注）。

    只覆盖第 2 步的取证字段，不动其它键（candidates / not_obtained 等由快照自身携带）。

    读取时**再判一次 pod 身份**：修复前抢下的快照里装着标签推定出来的错 pod（实测 19 份），
    只修 find_job_pod 挡不住它们 —— 重扫旧快照时会把别人的 node 与容器日志当成本次失败的
    现场。判据（runner_name 与 pod 名）快照里就有，无需再查集群，故在这里就地作废。
    """
    cluster_result.update(snapshot.get("cluster_result") or {})
    cluster_result["snapshot_from"] = snapshot.get("snapshot_file")
    cluster_result["snapshot_taken_at"] = snapshot.get("taken_at")
    cluster_result["snapshot_job_status"] = snapshot.get("job_status_at_snapshot")
    cluster_result["snapshot_note"] = snapshot.get("taken_reason")
    _retire_foreign_snapshot_pod(cluster_result, snapshot.get("runner_name"))
    return cluster_result


def _retire_foreign_snapshot_pod(cluster_result: dict, runner_name: str | None):
    """作废快照里那个「同标签的别的 job」的 pod 证据（容器状态与日志一并撤下）。

    作废而不是保留：这类证据的危害是读者据此下结论。撤下后本 case 会如实落到
    「未取得本 job 的 pod 实证」，并在未取证说明里写明作废原因 —— 与「压根没查」
    是两回事，故两边都留下文字。
    """
    reason = cluster_ops.foreign_pod_reason(cluster_result.get("pod_evidence"),
                                            cluster_result.get("match_kind"), runner_name)
    if not reason:
        return
    foreign_pod = (cluster_result.get("pod_evidence") or {}).get("pod")
    cluster_result["pod_evidence"] = None
    cluster_result["logs"] = []
    cluster_result["retired_pod"] = foreign_pod
    cluster_result["retired_reason"] = reason
    # match_kind / match_reason / time_consistent 都描述的是那个已经被撤下的 pod，
    # 留着会让报告说「pod 定位方式：runner 标签 + 时间窗收敛」却又不给出 pod，自相矛盾。
    # 置 None 而不是删键：字段集合保持稳定，调用方不必区分「没有这个键」与「值为空」。
    for key in ("match_kind", "match_reason", "time_consistent", "window_note", "placement_hint"):
        cluster_result[key] = None
    cluster_result["not_obtained"] = list(cluster_result.get("not_obtained") or []) + [reason]


class ClusterSession:
    """单个集群的会话：缓存 pod 列表与连通性，避免对同一集群反复拉取。

    `get pods -A` 是这几个只读操作里最重的一个（实测 3.77s，825 个 pod），
    而一次分析里同一个集群常被多次问到，故必须缓存。缓存键忽略 namespace，
    因为 -A 的结果是各 namespace 的超集。
    """

    def __init__(self, cluster_name: str, kubeconfig_info, namespace: str | None,
                 repo: str | None = None):
        self.cluster_name = cluster_name
        self.info = kubeconfig_info
        self.namespace = namespace
        self.repo = repo
        self._connectivity = None
        self._all_pods = None
        self._namespace_pods: dict = {}
        self._availability: dict = {}

    @property
    def path(self) -> str:
        return self.info.path

    def connectivity(self) -> dict:
        if self._connectivity is None:
            self._connectivity = cluster_ops.check_connectivity(self.path)
        return self._connectivity

    def all_pods(self) -> dict:
        if self._all_pods is None:
            self._all_pods = cluster_ops.list_pods(self.path)
        return self._all_pods

    def namespace_pods(self) -> dict:
        """优先只取业务 namespace（更轻），失败再退回全量。

        ⚠️ 单 namespace 版本，只适用于「已经确定 namespace 正确」的场景。
        要在集群里**找本 job 的 pod** 请用 pod_lists_for_lookup()：本方法把
        「namespace 能列举」当成了「namespace 正确」，而这两件事并不等价（见下）。
        """
        if self.namespace:
            scoped = self.pods_in_namespace(self.namespace)
            if scoped["ok"]:
                return scoped
        return self.all_pods()

    def pods_in_namespace(self, namespace: str) -> dict:
        """带缓存的 namespace 级列举。"""
        if namespace not in self._namespace_pods:
            self._namespace_pods[namespace] = cluster_ops.list_pods(self.path, namespace)
        return self._namespace_pods[namespace]

    def namespace_candidates(self) -> list:
        """可能承载本 repo runner pod 的 namespace，按优先级排列。

        为什么不能只用 Cluster.md 登记的那一个（实测踩坑，2026-09-28）：
          Cluster.md 把 `vllm-project/vllm-ascend` 登记到**项目共享** namespace `vllm-project`，
          该 namespace 能正常列举、里面有 32 个 pod，**但没有本 job 的 NPU runner**；
          runner pod 实际在按仓库名派生的 `vllm-project-vllm-ascend` 里。
          于是「列举成功」让调用方以为查过了，实际漏查了真正装着 pod 的那个 namespace，
          最终报告写成「pod 多已回收」—— 而那个 pod 当时已运行 9 分钟、活得好好的。
        派生规则由 Cluster.md 自身印证：`vllm-project/vllm-omni → vllm-project-vllm-omni`、
        `vllm-ascend/vllm-ascend-recipes → vllm-ascend-vllm-ascend-recipes`。
        """
        candidates = []
        if self.namespace:
            candidates.append(self.namespace)
        if self.repo:
            derived = self.repo.replace("/", "-")
            if derived not in candidates:
                candidates.append(derived)
        return candidates

    def pod_lists_for_lookup(self):
        """惰性产出 (来源说明, list_pods 结果)，供「在集群里找 pod」逐个尝试、命中即停。

        顺序：Cluster.md 登记的 namespace → 仓库名派生的 namespace → `-A` 全量。
        全量放最后是有代价考虑的：它实测 3.77s（825 个 pod），而 namespace 级只要 0.89s，
        故只在前面都没命中时才付出这个代价。
        惰性很重要：命中第一个就不该再发起后面的 kubectl 调用 —— 快照窗口只有几十秒。
        """
        for namespace in self.namespace_candidates():
            yield namespace, self.pods_in_namespace(namespace)
        yield "全量(-A)", self.all_pods()

    def availability(self, runner_label: str, base_labels=()) -> dict:
        """标签可用性核查。base_labels = job 上报的展示名，供「登记后缀对不上实际后缀」时兜底。

        为什么要带上展示名：实测 Cluster.md 登记的全名（`…-cn12-001`）与 pod 名实际后缀
        （`…-chlqk-runner-*`）不一致，只按登记全名匹配会得出与事实相反的「无 runner 在线」。
        详见 cluster_forensics.check_runner_availability。
        """
        key = (runner_label, tuple(base_labels or ()))
        if key not in self._availability:
            self._availability[key] = cluster_ops.check_runner_availability(
                self.path, runner_label, list(base_labels or ()))
        return self._availability[key]

    def health(self) -> dict:
        """健康快照。已有全量 pod 缓存时直接复用，避免为快照再拉一次 `get pods -A`。"""
        if self._all_pods is not None:
            if self._all_pods["ok"]:
                return cluster_ops.summarize_pods(self._all_pods["pods"])
            return {"available": False, "reason": f"无法列举 pod: {self._all_pods['error'][:200]}"}
        return cluster_ops.cluster_health(self.path, self.namespace)


def availability_rank(item: dict) -> int:
    """可用性结论的证据强度：全名匹配(2) > 标签主干匹配(1) > 无命中(0)。

    用于「多个候选集群各有一份结论时该展示哪一份」——有全名命中的那份才是强证据，
    不能因为遍历顺序先碰到主干命中就把它当成主结论。
    """
    if not item or not item.get("available"):
        return 0
    return 2 if item.get("match_kind") == "全名" else 1


def _merge_availability(per_label: dict) -> dict:
    """把「同一展示名对应的多个全名」的可用性核查结果合并成一条。

    合并规则：**任一全名查到 pod 即算 available**，并把每个全名的命中数都带上——
    合并后的结论必须有据可查，不能只留一个布尔值让人无法回溯。

    合并后取**最强**的匹配方式（全名 > 标签主干）：弱匹配只是兜底，
    若某个全名是精确命中的，结论就不能被表述成「靠主干才找到的」。
    """
    labels = list(per_label)
    if not labels:
        return {"available": False, "checked": True, "snapshot_only": True,
                "matched_pods": 0, "runners_online": 0, "listeners": 0,
                "namespaces": [], "samples": [], "per_label": {},
                "match_kind": None, "suffix_variants": {}, "scopes_checked": []}
    per_label_summary = {}
    samples, namespaces, scopes = [], set(), []
    suffix_variants: dict = {}
    total_pods = total_runners = total_listeners = 0
    for label in labels:
        item = per_label[label]
        per_label_summary[label] = {
            "available": item.get("available"),
            "match_kind": item.get("match_kind"),
            "matched_pods": item.get("matched_pods", 0),
            "runners_online": item.get("runners_online", 0),
            "listeners": item.get("listeners", 0),
            "checked": item.get("checked"),
            "reason": item.get("reason"),
        }
        total_pods += item.get("matched_pods") or 0
        total_runners += item.get("runners_online") or 0
        total_listeners += item.get("listeners") or 0
        samples.extend(item.get("samples") or [])
        namespaces.update(item.get("namespaces") or [])
        scopes.extend(item.get("scopes_checked") or [])
        for segment, count in (item.get("suffix_variants") or {}).items():
            suffix_variants[segment] = suffix_variants.get(segment, 0) + count
    available = any(item.get("available") for item in per_label.values())
    strongest = max(per_label.values(), key=availability_rank)
    return {
        "available": available,
        "checked": all(item.get("checked") for item in per_label.values()),
        # 仍是查询时刻快照，语义不因多查了几个标签而变强
        "snapshot_only": True,
        "reason": None if available else next(
            (item.get("reason") for item in per_label.values() if item.get("reason")), None),
        "matched_pods": total_pods,
        "runners_online": total_runners,
        "listeners": total_listeners,
        "namespaces": sorted(namespaces),
        "samples": sorted(samples)[:5],
        # 最强匹配方式：报告据此分叉「标签有效」与「后缀对不上」两种措辞
        "match_kind": strongest.get("match_kind") if available else None,
        # 最强那条结果当时查的是哪个标签（弱匹配时报告要写明「登记全名 X 是 0 命中」）
        "claimed_label": strongest.get("claimed_label"),
        "registered_suffix": next((item.get("registered_suffix") for item in per_label.values()
                                   if item.get("registered_suffix")), None),
        # 实测后缀变体 → 命中数（仅弱匹配有值），报告原样展示，供读者判断「注册表对不上」
        "suffix_variants": suffix_variants,
        # 实际查过的匹配方式并集：阴性结论据此证明「两级都查过」
        "scopes_checked": sorted(set(scopes)),
        # 逐标签明细：合并结果可能掩盖「哪个全名真的有」，故原样保留
        "per_label": per_label_summary,
        "labels_checked": labels,
    }


def resolve_exact_sessions(registry: ClusterRegistry, repo: str, labels: list,
                           sessions: dict) -> tuple:
    """解析该 runner 标签**精确登记**的集群，返回 (可用会话列表, 说明文字)。

    ⚠️ 本函数刻意**不提供「同仓库兜底」路径**。早先的实现会在精确集群无 kubeconfig 时
    静默退到其它同仓库集群探测，结果把 A 集群的「未找到 pod」当成 B 集群现象的集群侧证据——
    正是「拿错集群的证据比没有证据更糟」那类错误。宁可报「未取证」，也不换集群取证。

    Cluster.md 里同一标签可能登记在多个集群（Liqo 把虚拟节点 pod 反射进共享 namespace），
    故返回列表：调用方应逐个查找 pod，在**真正跑过这个 job 的那个集群**里命中。
    """
    labels = list(labels or [])
    candidates = registry.resolve_by_label(repo, labels)
    exact = candidates["exact"]
    if not exact:
        # 报告里不能只说「没登记」：得让读者看到**查过哪些形式**，
        # 否则分不清「Cluster.md 真没有」与「本工具只按一种写法找过」
        variants = "、".join(f"`{label}`" for label in labels) or "（无标签）"
        return [], (f"Cluster.md 未登记该 runner 标签（已按全名/展示名两种形式查过：{variants}）——"
                    f"标签可能已下线或写错，无法确定目标集群，故不做集群侧取证")

    usable, no_kubeconfig, unreachable = [], [], []
    for cluster_name in exact:
        info = registry.kubeconfig_for(cluster_name)
        if info is None:
            no_kubeconfig.append(cluster_name)
            continue
        if cluster_name not in sessions:
            sessions[cluster_name] = ClusterSession(
                cluster_name, info, registry.namespace_for(repo, cluster_name), repo)
        session = sessions[cluster_name]
        if session.connectivity().get("reachable"):
            usable.append((cluster_name, session))
        else:
            unreachable.append(cluster_name)

    note_parts = [f"标签精确登记于 {exact}"]
    if len(exact) > 1:
        note_parts.append(f"存在 {len(exact)} 个候选（Liqo 反射会导致同标签跨集群可见），已逐个查找")
    if no_kubeconfig:
        note_parts.append(f"其中无 kubeconfig 的集群：{no_kubeconfig}（**不会**改用其它集群代查）")
    if unreachable:
        note_parts.append(f"其中不可达的集群：{unreachable}")
    return usable, "；".join(note_parts)


def step3_skip_cluster(case: dict) -> dict:
    """日志侧已定性为业务侧的 case（`decisive`）：按规则**跳过**集群取证。

    为什么可以不查集群（这是本函数存在的唯一理由）：
      ① 判据由测试框架自己打印（pytest 的收集结果 / 退出码），责任方已落在业务侧 ——
         这正是「不必再排查基础设施的哪个环节失败」的依据；
      ② 失败发生在测试进程**内部**，pod/节点状态即便查到了也只能说明「容器当时活着」，
         给不出新信息。早先的实现把 `Stream logs` 判成「Runner 与 GitHub 通信问题」，
         于是真去集群找 pod 是否被驱逐，方向反了（实测历史样本里就有用例真失败被这么处理）。

    ⚠️ 不能简单地「把这类 case 从列表里剔掉」：报告只渲染传进去的 cases，归因分布也由 cases 统计，
    剔掉等于静默丢信息（业务侧失败就从此不在报告里出现了）。故此处**保留 case、只跳过取证**，
    并让报告显式写明「按规则跳过」——留白会被读成「查了但没查到」。
    """
    return {"cluster_name": None, "kubeconfig_path": None, "filename": None,
            "namespace": None, "identity": None, "match_kind": None,
            "pod_evidence": None, "logs": [], "availability": None,
            "availability_by_cluster": {}, "candidates": [], "placement_hint": None,
            "not_obtained": [], "candidate_note": None,
            "queried_labels": [], "queried_namespaces": {},
            "skipped": True,
            "skip_reason": (f"日志侧已定性为业务侧（桶【{case.get('bucket')}】，owner=code）："
                            f"pytest 的判定行已给出责任方，按规则不做集群取证")}


def step3_cluster_forensics(case: dict, registry: ClusterRegistry, sessions: dict,
                            args, errors: list, snapshots: dict | None = None) -> dict:
    """第 2 步：为一个失败 job 做集群侧取证。

    路径 0（最优先）：监听器已在**失败时刻**抢下快照 → 直接采用，不再现场查集群。
    路径 A：在候选集群中找到 pod → 取容器状态 + 日志（含重启前实例）。
    路径 B：pod 已回收（历史失败的常态）→ 降级为标签可用性核查，逐个候选集群做，
            并明确标注这只是查询时刻快照。
    三条路都走不通 → 如实记「未取证」。

    ⚠️ 路径 0 为什么要「直接采用、不叠加现场查询」：快照是 job 还在跑时抢下的真实现场，
    而现场查询必然是几分钟之后的事 —— 那时 pod 多已回收，查到的顶多是同标签的**别的** pod。
    把两者叠在一起，等于用一个弱证据去冲淡一个强证据。

    ⚠️ 查 pod 一律用**翻译后的全名**：job 在 GitHub 上报的标签常是展示名
    （`linux-aarch64-a3-800t-0`），而 pod 名用的是带集群后缀的全名
    （`linux-aarch64-a3-800t-0-cn12-001`）。拿展示名去前缀匹配必然查空，
    会凭空造出一条「该标签此刻无 runner」的假阴性。
    """
    cluster_result = {"cluster_name": None, "kubeconfig_path": None, "filename": None,
                      "namespace": None, "identity": None, "match_kind": None,
                      "pod_evidence": None, "logs": [], "availability": None,
                      "availability_by_cluster": {}, "candidates": [], "placement_hint": None,
                      "not_obtained": [], "candidate_note": None,
                      "queried_labels": [], "queried_namespaces": {}}
    job_id = case.get("job_id")
    if snapshots and job_id and int(job_id) in snapshots:
        return apply_cluster_snapshot(cluster_result, snapshots[int(job_id)])
    labels = case.get("labels") or []
    repo = case.get("_repo") or args.repo

    candidate_sessions, candidate_note = resolve_exact_sessions(registry, repo, labels, sessions)
    cluster_result["candidate_note"] = candidate_note
    cluster_result["candidates"] = [name for name, _ in candidate_sessions]
    if not candidate_sessions:
        cluster_result["not_obtained"].append(candidate_note)
        return cluster_result

    # 每个候选集群各自把自己登记的标签形式翻译成全名（不同集群后缀不同，不能共用一份）
    full_labels_by_cluster = {
        cluster_name: registry.full_labels_for(repo, cluster_name, labels) or list(labels)
        for cluster_name, _ in candidate_sessions}
    cluster_result["queried_labels"] = sorted(
        {label for items in full_labels_by_cluster.values() for label in items})

    # ---- 路径 A：逐个候选集群找 pod，命中即用（真正跑过该 job 的集群才会有这个 pod）----
    # find_job_pod 保证**只返回能承载过本 job 的 pod**（未启动/起点晚于失败步骤的候选
    # 在它内部就被剔除了），故这里命中即可用，不需要再判时序。
    # 每个集群内部再按 namespace 逐个查（见 ClusterSession.pod_lists_for_lookup）：
    # 登记的 namespace 可能只是项目共享 namespace，真正装着 runner pod 的是仓库名派生的那个。
    hit = None
    for cluster_name, session in candidate_sessions:
        failure_notes, informative_notes = [], []
        for source, pods_result in session.pod_lists_for_lookup():
            cluster_result["queried_namespaces"].setdefault(cluster_name, []).append(source)
            if not pods_result["ok"]:
                failure_notes.append(f"集群 {cluster_name} 列举 pod 失败（{source}）："
                                     f"{pods_result['error'][:160]}")
                continue
            found = cluster_ops.find_job_pod(
                pods_result["pods"], case.get("runner_name"), full_labels_by_cluster[cluster_name],
                case.get("failed_step_started_at") or case.get("job_started_at"),
                case.get("failed_step_completed_at") or case.get("job_completed_at"))
            if found["pod"] is not None:
                hit = (cluster_name, session, found)
                break
            # 「同标签的候选 pod 全在失败步骤之后才启动」这类**有信息量**的否定结论必须留下：
            # 它把「没找到」推进成了「找到了但不是本 job 的」——两者对读者的含义完全不同。
            # 只收 informative 的：否则每个落空集群都会塞进一句「pod 已回收」，纯噪音。
            if found.get("informative") and found.get("reason"):
                informative_notes.append(f"集群 {cluster_name}（{source}）：{found['reason']}")
        # 查询失败要**始终**留下（它是「某一路视野不可用」的记录）；
        # 而 informative 的落空说明只在整集群都没命中时才有意义 —— 已经命中时它只是噪音。
        notes = failure_notes + ([] if hit else informative_notes)
        cluster_result["not_obtained"].extend(dict.fromkeys(notes))
        if hit is not None:
            break

    if hit is not None:
        cluster_name, session, found = hit
        cluster_result.update({"cluster_name": cluster_name, "kubeconfig_path": session.path,
                               "filename": session.info.filename, "namespace": session.namespace,
                               "identity": session.connectivity(),
                               "match_kind": found["match_kind"],
                               "match_reason": found.get("reason"),
                               # 该 pod 是否可能承载过本 job（runner pod 会复用，见 _pod_window_check）
                               "time_consistent": found.get("time_consistent"),
                               "window_note": found.get("window_note")})
        evidence = cluster_ops.pod_evidence(found["pod"])
        cluster_result["pod_evidence"] = evidence
        # 取到的是 Liqo 影子对象时，真实负载不在本集群 —— 把「疑似提供方集群」一并算出来。
        # 算在这里而不是报告层：只有这里手上有注册表（虚拟节点名 → 已登记集群的映射）。
        placement = evidence.get("placement") or {}
        if placement.get("shadow_pod") is True:
            cluster_result["placement_hint"] = registry.cluster_for_virtual_node(
                placement.get("virtual_node"))
        # 取日志的两个前提缺一不可：① 用户没关掉（--no-pod-logs）；② 该 pod 时序自洽。
        # 时序不符的 pod 属于另一次运行，取它的日志既无用又会误导（报告层也不展示）。
        # ⚠️ 这个条件曾写成 `if args.no_pod_logs or ... is False:`（少了 not），后果是**恰好相反**：
        # 加了 --no-pod-logs 反而去抓日志，正常路径反而不抓 —— 即集群取证在正常路径下从来没取过
        # 容器日志，而给外来 pod 抓了日志。回归断言见 tests/test_pod_log_gating.py。
        if not args.no_pod_logs and cluster_result.get("time_consistent") is not False:
            namespace = evidence.get("namespace") or session.namespace
            for container in evidence.get("containers") or []:
                name = container.get("container")
                got = cluster_ops.pod_logs(session.path, namespace, evidence["pod"],
                                           container=name, tail=120)
                if got["ok"] and got["text"].strip():
                    cluster_result["logs"].append({"container": name, "source": "当前实例",
                                                   "ok": True, "text": got["text"]})
                # 容器曾重启 → 真错误在上一个实例里，必须另取一次
                if container.get("last_terminated_reason"):
                    prev = cluster_ops.pod_logs(session.path, namespace, evidence["pod"],
                                                container=name, previous=True, tail=120)
                    if prev["ok"] and prev["text"].strip():
                        cluster_result["logs"].append({"container": name, "source": "重启前的实例",
                                                       "ok": True, "text": prev["text"]})
        return cluster_result

    # ---- 路径 B：pod 已回收 → 逐个候选集群做标签可用性核查 ----
    # 措辞按实际原因分叉：若已知「同标签的候选 pod 全都不是本 job 的现场」，就不能再说
    # 「pod 多已回收」——那是**另一个**结论，两者不能混着写：前者说明集群里确实有该 runner
    # 在滚动、只是本次现场已被回收，后者才是「压根没找到」。
    if any("不构成本 job" in note or "无法判定" in note
           for note in cluster_result["not_obtained"]):
        detail = "（同标签的现存 pod 均非本 job 现场，详见上条）"
    else:
        detail = "（历史失败的 pod 多已回收）"
    # 把真正查过的 namespace 写进结论：否则读者分不清「这个集群真的没有」与「只查了一个 namespace」
    namespace_text = "、".join(sorted({source for sources in cluster_result["queried_namespaces"].values()
                                       for source in sources})) or "—"
    cluster_result["not_obtained"].append(
        f"已在候选集群 {cluster_result['candidates']} 中逐个查找"
        f"（按全名 {cluster_result['queried_labels']} 匹配 pod 名；查过的范围：{namespace_text}），"
        f"未取得本 job 的 pod 实证{detail}")
    # 上面这条只讲了「找本 job 的 pod」这一件事；它不构成「标签不存在」的结论 ——
    # 后者要等下面的可用性核查（含标签主干兜底）出结果，报告里两者分开表述。
    for cluster_name, session in candidate_sessions:
        cluster_labels = full_labels_by_cluster.get(cluster_name) or []
        if not cluster_labels:
            continue
        # 一个展示名可能对应多个全名（如同时登记了 -cn12-001 与 -sh-001），逐个查后合并。
        # 同时把展示名传进去：登记后缀与实际 pod 后缀不一致时，靠它做主干兜底
        # （实测 0 个 vs 6 个，见 tests/test_runner_availability.py）。
        per_label = {label: session.availability(label, labels) for label in cluster_labels}
        merged = _merge_availability(per_label)
        cluster_result["availability_by_cluster"][cluster_name] = merged
        # 取**证据最强**的那份作为 availability（全名命中 > 主干命中 > 无命中）；
        # 全部为否时保留第一份（rank 都是 0，不会被后来的替换），以展示核查确实执行过。
        if cluster_result["availability"] is None or (
                availability_rank(merged) > availability_rank(cluster_result["availability"])):
            cluster_result["availability"] = merged
            cluster_result["availability_cluster"] = cluster_name
    return cluster_result


def step4_history(case: dict, index: IssueIndex, top_k: int = 5) -> list:
    """第 4 步：用错误签名 + 关键词在历史 issue 知识库里找先例。"""
    signature_text = " ".join(filter(None, [
        case.get("sig"), case.get("step"), case.get("job_name"), case.get("bucket")]))
    signatures = extract_signatures(signature_text)
    keyword_text = " ".join(filter(None, [
        case.get("bucket"), case.get("sig"), case.get("step"), case.get("workflow"),
        case.get("job_name")]))
    keywords = keywords_for(keyword_text)
    # 出处类词（workflow / job / step 名）单列出来交给匹配器排除，避免
    # 「同一个 workflow」被误当成「同一个现象」（见 IssueIndex.match 的 provenance 说明）
    provenance = keywords_for(" ".join(filter(None, [
        case.get("workflow"), case.get("job_name"), case.get("step")])))
    return index.match(signatures, keywords, top_k=top_k, provenance=provenance)


def step4_related(case: dict, index: IssueIndex) -> list:
    """策展的历史关联：知识表里人工登记的「桶 ↔ 历史 issue」链接。

    为什么需要它：词面匹配（issue_knowledge.match）只能发现**换了说法也照样能对上**的关联。
    实测反例——本工具的桶说「模型缓存未命中」，#238 说「找不到缓存模型」，两者无稀有词重叠，
    #238 只拿到 11.13 分、排在第 44 名，纯靠词面永远浮不上来。可 #238 恰恰是解释该现象的
    「唯一先例。这类已知同现象必须人工策展，本函数据此把登记的号解析成可渲染条目。
    """
    entries = knowledge_for(case.get("bucket") or "").get("related_issues") or []
    if not entries:
        return []
    resolved = {item["number"]: item
                for item in index.resolve_numbers([entry["number"] for entry in entries])}
    related = []
    for entry in entries:
        item = resolved.get(entry["number"]) or {"number": entry["number"], "missing": True}
        related.append({**item, "why": entry.get("why")})
    return related


def select_cases(payload: dict, max_cases: int) -> list:
    """从分类结果里挑出要做集群取证与历史归因的 job。

    优先级：① 在 cluster_todo 里（日志侧明确要求集群取证）
            ② 桶知识表标了 probe（该桶的结论需要集群侧验证）
            ③ 其余按原顺序
    跳过假失败（非真实失败，取证无意义）。

    ⚠️ 日志侧已定性的 case（`decisive`，见第 1 步的 DECISIVE_BUCKETS）单独处理：
    **必进报告，但不占 --max-cases 名额**（它们不做集群取证，见 step3_skip_cluster）。
    名额是留给「不查集群就定不了性」的 case 的，被这类已定性的 case 占掉，
    就等于少查一个真正需要查的 —— 那是把「提前退出」省下的预算又浪费回去。

    ⚠️ 按优先级排完序**不能直接取前 N 个**：实测一次真实运行里 `--max-cases 4` 取到的
    4 个 job 全是同一个桶（都是「Wait for pods ready」），报告看起来做了 4 份取证，
    实际只有 1 份信息。故在同优先级内**按桶轮流取**（round-robin），
    让有限的取证名额覆盖尽可能多的失败类型；桶内仍按原顺序（先到先取）。
    """
    todo_keys = {(item.get("job_name"), item.get("step")) for item in payload.get("cluster_todo") or []}
    classifications = [item for item in payload.get("classifications") or []
                       if item.get("owner") != "假失败" and not item.get("duplicate")]
    # 已定性的先摘出来：下面按名额轮转的只是「待取证」的那些
    decided = [item for item in classifications if item.get("decisive")]
    pending = [item for item in classifications if not item.get("decisive")]

    groups: dict = {}      # 桶 → [(优先级, item), ...]，保持首次出现的顺序
    group_priority: dict = {}
    for item in pending:
        key = (item.get("job_name"), item.get("step"))
        if key in todo_keys:
            priority = 0
        elif knowledge_for(item.get("bucket") or "").get("probe"):
            priority = 1
        else:
            priority = 2
        bucket = item.get("bucket") or "未分类"
        groups.setdefault(bucket, []).append(item)
        group_priority[bucket] = min(group_priority.get(bucket, priority), priority)
    ordered_buckets = sorted(groups, key=lambda bucket: group_priority[bucket])

    picked, cursors = [], {bucket: 0 for bucket in ordered_buckets}
    while len(picked) < max_cases:
        progressed = False
        for bucket in ordered_buckets:
            if len(picked) >= max_cases:
                break
            index = cursors[bucket]
            if index < len(groups[bucket]):
                picked.append(groups[bucket][index])
                cursors[bucket] = index + 1
                progressed = True
        if not progressed:      # 所有桶都已取空
            break
    # 已定性的排在前面：它们是「看一眼就能派活」的业务侧结论，先读先办
    return decided + picked


def main():
    args = parse_args()
    started = datetime.datetime.now()
    os.makedirs(args.cache_dir, exist_ok=True)
    errors: list = []

    # ---- 自检模式：只依赖集群注册表，**不碰第 1 步** ----
    # （早先的版本会先跑完整的日志分析再自检，导致只想确认集群可达性却要等几分钟）
    if args.self_check_only:
        registry = step2_load_registry(args, args.cache_dir)
        print("\n=== 集群连通性与身份自检 ===")
        for row in registry.self_check_plan():
            mark = "✅" if row["has_kubeconfig"] else "❌"
            print(f"  {mark} {row['cluster']}")
            if row["has_kubeconfig"]:
                info = registry.kubeconfig_for(row["cluster"])
                state = cluster_ops.check_connectivity(info.path)
                print(f"       可达={state['reachable']} server={state.get('server_version')} "
                      f"身份={state.get('identity')}")
                if not state["reachable"]:
                    print(f"       错误={state.get('error')}")
            if row["warning"]:
                print(f"       ⚠️ {row['warning']}")
        return 0

    # ---- 第 1 步 ----
    payload = step1_get_handoff(args, args.cache_dir)
    meta = payload.get("meta") or {}
    repo = meta.get("repo") or args.repo
    print(f"[第1步] 失败 job {len(payload.get('failed_jobs') or [])} 个，"
          f"分类 {len(payload.get('classifications') or [])} 条，"
          f"待集群取证 {len(payload.get('cluster_todo') or [])} 条")

    # ---- 第 2 步 ----
    registry = step2_load_registry(args, args.cache_dir)
    registry_plan = registry.self_check_plan()

    # ---- 第 4 步的知识库（先装载，供第 3/5 步共用）----
    if args.offline:
        issues_path = os.path.join(args.cache_dir, "issues.json")
        comments_path = os.path.join(args.cache_dir, "comments.json")
        for path in (issues_path, comments_path):
            if not os.path.exists(path):
                raise SystemExit(f"--offline 但缓存不存在: {path}")
        with open(issues_path, encoding="utf-8") as fh:
            issue_payload = json.load(fh)
        with open(comments_path, encoding="utf-8") as fh:
            comment_payload = json.load(fh)
        issue_payload = {"issues": issue_payload.get("issues", []), "source": "cache"}
        comment_payload = {"comments": {int(k): v for k, v in comment_payload.get("comments", {}).items()},
                           "error": None}
    else:
        issue_payload = fetch_issues(cache_path=os.path.join(args.cache_dir, "issues.json"),
                                     max_age_hours=args.max_age_hours)
        comment_payload = fetch_comments(cache_path=os.path.join(args.cache_dir, "comments.json"),
                                         max_age_hours=args.max_age_hours)
    if issue_payload.get("error"):
        errors.append(f"取 issue 列表出错：{issue_payload['error']}")
    if comment_payload.get("error"):
        errors.append(f"取 issue 评论出错：{comment_payload['error']}")
    index = IssueIndex(issue_payload.get("issues") or [], comment_payload.get("comments") or {})
    postmortems = sum(1 for item in index.issues if item["is_postmortem"])
    print(f"[第4步] 知识库：{len(index)} 个 issue（来源 {issue_payload.get('source')}），"
          f"其中含根因的复盘 {postmortems} 条"
          f"（根因取自评论的 {sum(1 for i in index.issues if i['root_cause_source'] == 'comment')} 条）")

    # ---- 第 3 + 4 + 5 步：逐案取证 ----
    cases = select_cases(payload, args.max_cases)
    skipped = [item for item in cases if item.get("decisive")]
    print(f"[第3步] 选取 {len(cases)} 个失败 job 进入集群取证"
          f"{f'（其中 {len(skipped)} 个日志侧已定性为业务侧，按规则跳过集群取证）' if skipped else ''}")
    snapshots = load_cluster_snapshots(args.cluster_snapshot)
    sessions: dict = {}
    for item in cases:
        item["_repo"] = repo
    rendered_cases = []
    for item in cases:
        # 日志侧已定性 → 提前退出：不碰集群（连快照都不用查，判据不需要旁证）
        cluster_result = (step3_skip_cluster(item) if item.get("decisive")
                          else step3_cluster_forensics(item, registry, sessions, args, errors, snapshots))
        history = step4_history(item, index)
        case = {
            "workflow": item.get("workflow"), "job_name": item.get("job_name"),
            # job_id/run_id 必须带进 case：集群快照是按 job_id 关联的（见 load_cluster_snapshots），
            # 早先的 case 字典里没有它们，快照无从匹配 —— 表现为「明明抢到了快照却没用上」。
            "job_id": item.get("job_id"), "run_id": item.get("run_id"),
            # repo 进 case：报告要印「怎么把本 job 的控制台日志取回来」（gh api 命令需要 owner/repo）
            "repo": repo,
            "link": item.get("link"), "bucket": item.get("bucket"), "owner": item.get("owner"),
            "sig": item.get("sig"), "step": item.get("step"), "chip": item.get("chip"),
            "labels": item.get("labels") or [], "runner_name": item.get("runner_name"),
            "is_npu": item.get("is_npu"),
            # 对端节点日志（多节点 job 的第二证据源）：必须显式带进 case，
            # 因为它进的是渲染层与 synthesize() 的依据行（第 1 步的 detail 不会自动流过来）。
            "peer": item.get("peer"), "sig_source": item.get("sig_source"),
            "window": {"started_at": item.get("failed_step_started_at"),
                       "completed_at": item.get("failed_step_completed_at")},
            "cluster": cluster_result,
            "history": history,
            "related_issues": step4_related(item, index),
        }
        case["verdict"] = synthesize(case)
        rendered_cases.append(case)
        mark = ("⏭️ " if cluster_result.get("skipped")
                else "🅿️" if cluster_result.get("pod_evidence") else "🔎")
        print(f"  {mark} {str(item.get('job_name'))[:40]:40s} → {case['verdict']['owner']:8s} "
              f"| {case['verdict']['confidence'][:24]}")

    # ---- 各集群现场快照（给 infra 类归因提供旁证）----
    health: dict = {}
    for cluster_name, session in sessions.items():
        health[cluster_name] = session.health()

    # ---- 输出 ----
    # json_path 必须在渲染**之前**算出来：精简版报告的头部要写出「完整证据在哪个 json」，
    # 而早先它是渲染之后才拼的路径（报告里无处引用自己那份 json）。
    os.makedirs(args.report_dir, exist_ok=True)
    stamp = started.strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(args.report_dir, f"forensics_report_{stamp}.md")
    json_path = os.path.join(args.report_dir, f"forensics_report_{stamp}.json")
    run_meta = {
        "repo": repo, "since": meta.get("since"), "chips": meta.get("chips"),
        "generated_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        "handoff_source": meta.get("handoff_source"),
        "total_jobs": len(payload.get("failed_jobs") or []),
        "json_path": json_path,
    }
    # 局限带 triggered 标志：md 只渲染触发的那些，json 全量落盘（含未触发项与判据）
    limitations = select_limitations(rendered_cases, run_meta)
    integrity = {"limitations": limitations, "errors": errors}
    report_text = render_report(rendered_cases, run_meta, registry_plan, health, integrity)

    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report_text)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"meta": run_meta, "cases": rendered_cases,
                   "cluster_availability": registry_plan, "cluster_health": health,
                   "limitations": limitations, "errors": errors}, fh, ensure_ascii=False, indent=2)

    elapsed = (datetime.datetime.now() - started).total_seconds()
    print(f"\n=== 完成（耗时 {elapsed:.0f}s）===")
    print(f"  报告: {report_path}")
    print(f"  结构化结果: {json_path}")
    skipped_cases = [case for case in rendered_cases if case["cluster"].get("skipped")]
    queried_cases = [case for case in rendered_cases if not case["cluster"].get("skipped")]
    pod_hits = sum(1 for case in queried_cases if case["cluster"].get("pod_evidence"))
    print(f"  集群侧取得 pod 实证: {pod_hits}/{len(queried_cases)}；"
          f"其余为标签可用性核查或未取证（pod 多已回收）")
    if skipped_cases:
        # 单独一行：跳过不是「没查到」，不能混进上面那个分式的分母（会显得取证成绩变差）
        print(f"  日志侧已定性为业务侧、按规则跳过集群取证: {len(skipped_cases)} 个"
              f"（{'、'.join(sorted({c['bucket'] for c in skipped_cases}))}）")
    conflicts = sum(1 for case in rendered_cases if case["verdict"]["conflicts"])
    if conflicts:
        print(f"  ⚠️ {conflicts} 个案例存在证据冲突，需人工裁定（详见报告）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
