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
from forensics.report import render_report, synthesize         # noqa: E402

ANALYSIS_SCRIPT = os.path.join(BASE_DIR, "npu_ci_failure_analysis.py")

LIMITATIONS = [
    "**官方分类树不含正文**：problem-tree.json 的 19 个叶子节点 text 为空，只能用来对齐分类口径，"
    "不能提供根因描述或修复建议——建议内容来自本工具的桶知识表 + 历史 issue 先例。",
    "**SA 权限受限**：kubeconfig 对应的 serviceaccount 只有 `pods[get,list]` 与 `pods/log[get,watch]`；"
    "`nodes`/`events`/`namespaces` 均 Forbidden。因此**拿不到调度事件**（FailedScheduling 的具体原因）、"
    "节点 condition 与 taint 列表，也无法用 `kubectl describe`（其 Events 段会 403）。",
    "**runner 标签后缀不能判集群**：Liqo 会把虚拟节点 pod 反射进共享 namespace，"
    "实测 `linux-aarch64-a3-800i-*-cn12-001` 的 pod 能同时从 aiframework/cn12-001/mind-third-ci 三个"
    "kubeconfig 看到。本工具靠 Cluster.md 的标签登记 + 集群本地 CPU scale-set 标识收敛，并把歧义显式写出。",
    "**标签可用性核查是查询时刻的快照**：查到「无 runner」只能说明此刻没部署，"
    "不能证明失败当时 runner 掉线。官方分类树的「runs-on 标签不存在 / Runner 未上线」需另有失败时刻证据。",
    "**pod 已回收是常态**：历史失败的 job pod 多数早已回收，第 2 步只能降级为标签可用性核查；"
    "只有仍在运行或刚结束的 job 才能拿到 pod 级实证。",
    "**靠标签定位的 pod 是「推定」而非「确证」**：runner pod 会被复用，且未调度成功的 pod 也存在，"
    "故本工具在按标签匹配时只接受『真的启动过、且存在起点早于失败步骤』的 pod，"
    "拿不到就如实报「未取证」。但这只排除了**不可能**的候选，不能证明选中的那个一定跑过本 job ——"
    "同一窗口内该 scale-set 若并发跑过多个 job，仍需用容器日志里的 job 号二次确认。",
    "**pod 名精确匹配才免疫上面的推定问题**：job 在 GitHub 上报的 runner_name 带 5 位随机段，"
    "与 pod 名精确一致时可确认身份；只是这种理想情况依赖 pod 尚未被回收，实测多为已回收。",
    "**历史匹配的覆盖面**：仅索引 issue 的标题、正文与评论，不含 PR 讨论、文档、IM 记录；"
    "且标签（label）无结构化语义（179 个 issue 中 2/3 无标签），匹配完全依赖错误签名，"
    "对没有可判别签名的纯描述性 issue 会漏检。",
    "**换说法的同一现象，词面匹配连不上**：本工具说「模型缓存未命中」，历史 #238 说「找不到缓存模型」，"
    "两者无稀有词重叠（#238 只得 11 分、排 44 名）。这类已知同现象靠知识表里的**人工策展关联**兜住，"
    "策展表是有限的、需要人维护——报告里凡出自策展的条目都会标明，不会冒充自动发现。",
    "**分数高 ≠ 同现象**：实测 #228（AOP bisect 超时）仅凭 `schedule_nightly_test_a2` 这个工作流名"
    "就拿到 65 分。故报告对每条匹配都标出证据强度（强/中/弱），且只让命中「带机制签名」"
    "（如 exitcode:137）或「核心症状词」的复盘充当先例；`valueerror` 这类异常类名不算机制证据。",
    "**不自动裁定责任方**：历史先例只作线索与先例引用。当先例根因提到平台侧动作而日志侧判 code 时，"
    "报告会标为「证据冲突」并要求人工裁定，不会自动改写 owner。",
]


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


class ClusterSession:
    """单个集群的会话：缓存 pod 列表与连通性，避免对同一集群反复拉取。

    `get pods -A` 是这几个只读操作里最重的一个，而一次分析里同一个集群常被多次问到，
    故必须缓存。缓存键忽略 namespace，因为 -A 的结果是各 namespace 的超集。
    """

    def __init__(self, cluster_name: str, kubeconfig_info, namespace: str | None):
        self.cluster_name = cluster_name
        self.info = kubeconfig_info
        self.namespace = namespace
        self._connectivity = None
        self._all_pods = None
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
        """优先只取业务 namespace（更轻），失败再退回全量。"""
        if self.namespace:
            scoped = cluster_ops.list_pods(self.path, self.namespace)
            if scoped["ok"]:
                return scoped
        return self.all_pods()

    def availability(self, runner_label: str) -> dict:
        if runner_label not in self._availability:
            self._availability[runner_label] = cluster_ops.check_runner_availability(self.path, runner_label)
        return self._availability[runner_label]

    def health(self) -> dict:
        """健康快照。已有全量 pod 缓存时直接复用，避免为快照再拉一次 `get pods -A`。"""
        if self._all_pods is not None:
            if self._all_pods["ok"]:
                return cluster_ops.summarize_pods(self._all_pods["pods"])
            return {"available": False, "reason": f"无法列举 pod: {self._all_pods['error'][:200]}"}
        return cluster_ops.cluster_health(self.path, self.namespace)


def _merge_availability(per_label: dict) -> dict:
    """把「同一展示名对应的多个全名」的可用性核查结果合并成一条。

    合并规则：**任一全名查到 pod 即算 available**，并把每个全名的命中数都带上——
    合并后的结论必须有据可查，不能只留一个布尔值让人无法回溯。
    """
    labels = list(per_label)
    if not labels:
        return {"available": False, "checked": True, "snapshot_only": True,
                "matched_pods": 0, "runners_online": 0, "listeners": 0,
                "namespaces": [], "samples": [], "per_label": {}}
    per_label_summary = {}
    samples, namespaces = [], set()
    total_pods = total_runners = total_listeners = 0
    for label in labels:
        item = per_label[label]
        per_label_summary[label] = {
            "available": item.get("available"),
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
    available = any(item.get("available") for item in per_label.values())
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
                cluster_name, info, registry.namespace_for(repo, cluster_name))
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


def step3_cluster_forensics(case: dict, registry: ClusterRegistry, sessions: dict,
                            args, errors: list) -> dict:
    """第 2 步：为一个失败 job 做集群侧取证。

    路径 A：在候选集群中找到 pod → 取容器状态 + 日志（含重启前实例）。
    路径 B：pod 已回收（历史失败的常态）→ 降级为标签可用性核查，逐个候选集群做，
            并明确标注这只是查询时刻快照。
    两条路都走不通 → 如实记「未取证」。

    ⚠️ 查 pod 一律用**翻译后的全名**：job 在 GitHub 上报的标签常是展示名
    （`linux-aarch64-a3-800t-0`），而 pod 名用的是带集群后缀的全名
    （`linux-aarch64-a3-800t-0-cn12-001`）。拿展示名去前缀匹配必然查空，
    会凭空造出一条「该标签此刻无 runner」的假阴性。
    """
    cluster_result = {"cluster_name": None, "kubeconfig_path": None, "filename": None,
                      "namespace": None, "identity": None, "match_kind": None,
                      "pod_evidence": None, "logs": [], "availability": None,
                      "availability_by_cluster": {}, "candidates": [],
                      "not_obtained": [], "candidate_note": None,
                      "queried_labels": []}
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
    hit = None
    for cluster_name, session in candidate_sessions:
        pods_result = session.namespace_pods()
        if not pods_result["ok"]:
            cluster_result["not_obtained"].append(
                f"集群 {cluster_name} 列举 pod 失败：{pods_result['error'][:160]}")
            continue
        found = cluster_ops.find_job_pod(
            pods_result["pods"], case.get("runner_name"), full_labels_by_cluster[cluster_name],
            case.get("failed_step_started_at") or case.get("job_started_at"),
            case.get("failed_step_completed_at") or case.get("job_completed_at"))
        if found["pod"] is None:
            # 「同标签的候选 pod 全在失败步骤之后才启动」这类**有信息量**的否定结论必须留下：
            # 它把「没找到」推进成了「找到了但不是本 job 的」——两者对读者的含义完全不同。
            # 只收 informative 的：否则每个落空集群都会塞进一句「pod 已回收」，纯噪音。
            if found.get("informative") and found.get("reason"):
                cluster_result["not_obtained"].append(f"集群 {cluster_name}：{found['reason']}")
            continue
        hit = (cluster_name, session, found)
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
        # 时序不符的 pod 属于另一次运行，取它的日志既无用又会误导（报告层也不展示）
        if args.no_pod_logs or cluster_result.get("time_consistent") is False:
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
    cluster_result["not_obtained"].append(
        f"已在候选集群 {cluster_result['candidates']} 中逐个查找"
        f"（按全名 {cluster_result['queried_labels']} 匹配 pod 名），未取得本 job 的 pod 实证"
        f"{detail}")
    for cluster_name, session in candidate_sessions:
        cluster_labels = full_labels_by_cluster.get(cluster_name) or []
        if not cluster_labels:
            continue
        # 一个展示名可能对应多个全名（如同时登记了 -cn12-001 与 -sh-001），逐个查后合并
        per_label = {label: session.availability(label) for label in cluster_labels}
        merged = _merge_availability(per_label)
        cluster_result["availability_by_cluster"][cluster_name] = merged
        # 取第一个「确实查到有 runner」的结果作为 availability；全部为否时保留最后一个以展示核查已执行
        if cluster_result["availability"] is None or (
                merged.get("available") and not cluster_result["availability"].get("available")):
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

    ⚠️ 按优先级排完序**不能直接取前 N 个**：实测一次真实运行里 `--max-cases 4` 取到的
    4 个 job 全是同一个桶（都是「Wait for pods ready」），报告看起来做了 4 份取证，
    实际只有 1 份信息。故在同优先级内**按桶轮流取**（round-robin），
    让有限的取证名额覆盖尽可能多的失败类型；桶内仍按原顺序（先到先取）。
    """
    todo_keys = {(item.get("job_name"), item.get("step")) for item in payload.get("cluster_todo") or []}
    classifications = [item for item in payload.get("classifications") or []
                       if item.get("owner") != "假失败" and not item.get("duplicate")]

    groups: dict = {}      # 桶 → [(优先级, item), ...]，保持首次出现的顺序
    group_priority: dict = {}
    for item in classifications:
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
    return picked


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
    print(f"[第3步] 选取 {len(cases)} 个失败 job 进入集群取证")
    sessions: dict = {}
    for item in cases:
        item["_repo"] = repo
    rendered_cases = []
    for item in cases:
        cluster_result = step3_cluster_forensics(item, registry, sessions, args, errors)
        history = step4_history(item, index)
        case = {
            "workflow": item.get("workflow"), "job_name": item.get("job_name"),
            "link": item.get("link"), "bucket": item.get("bucket"), "owner": item.get("owner"),
            "sig": item.get("sig"), "step": item.get("step"), "chip": item.get("chip"),
            "labels": item.get("labels") or [], "runner_name": item.get("runner_name"),
            "is_npu": item.get("is_npu"),
            "window": {"started_at": item.get("failed_step_started_at"),
                       "completed_at": item.get("failed_step_completed_at")},
            "cluster": cluster_result,
            "history": history,
            "related_issues": step4_related(item, index),
        }
        case["verdict"] = synthesize(case)
        rendered_cases.append(case)
        mark = "🅿️" if cluster_result.get("pod_evidence") else "🔎"
        print(f"  {mark} {str(item.get('job_name'))[:40]:40s} → {case['verdict']['owner']:8s} "
              f"| {case['verdict']['confidence'][:24]}")

    # ---- 各集群现场快照（给 infra 类归因提供旁证）----
    health: dict = {}
    for cluster_name, session in sessions.items():
        health[cluster_name] = session.health()

    # ---- 输出 ----
    run_meta = {
        "repo": repo, "since": meta.get("since"), "chips": meta.get("chips"),
        "generated_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        "handoff_source": meta.get("handoff_source"),
        "total_jobs": len(payload.get("failed_jobs") or []),
    }
    integrity = {"limitations": LIMITATIONS, "errors": errors}
    report_text = render_report(rendered_cases, run_meta, registry_plan, health, integrity)

    os.makedirs(args.report_dir, exist_ok=True)
    stamp = started.strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(args.report_dir, f"forensics_report_{stamp}.md")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report_text)
    json_path = os.path.join(args.report_dir, f"forensics_report_{stamp}.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"meta": run_meta, "cases": rendered_cases,
                   "cluster_availability": registry_plan, "cluster_health": health,
                   "limitations": LIMITATIONS, "errors": errors}, fh, ensure_ascii=False, indent=2)

    elapsed = (datetime.datetime.now() - started).total_seconds()
    print(f"\n=== 完成（耗时 {elapsed:.0f}s）===")
    print(f"  报告: {report_path}")
    print(f"  结构化结果: {json_path}")
    pod_hits = sum(1 for case in rendered_cases if case["cluster"].get("pod_evidence"))
    print(f"  集群侧取得 pod 实证: {pod_hits}/{len(rendered_cases)}；"
          f"其余为标签可用性核查或未取证（pod 多已回收）")
    conflicts = sum(1 for case in rendered_cases if case["verdict"]["conflicts"])
    if conflicts:
        print(f"  ⚠️ {conflicts} 个案例存在证据冲突，需人工裁定（详见报告）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
