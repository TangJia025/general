"""能力边界与局限：条目 + **触发判据**。

为什么要有触发判据：这 15 条是工具固有的边界，与单次运行无关，早先全量写进每份报告 ——
结果是每份报告的「能力边界与局限」逐字节相同，而真正该读的那两三条（例如「本次其实
只做了标签可用性核查、没拿到 pod 实证」）被另外 12 条淹掉。现在改成**只输出本次运行
真的踩到的**条目，完整清单与判据留在 `npu_ci_forensics_design.md` §7。

放在 `forensics/` 而不是 `npu_ci_forensics.py`：后者是全模块级执行的脚本（import 即联网
跑整轮分析），纯逻辑只有放这里才能被测试直接 import，不必再套 AST 抽取。
"""
from __future__ import annotations


def _cluster_of(case: dict) -> dict:
    return case.get("cluster") or {}


def limitation_facts(cases: list, meta: dict | None = None) -> dict:
    """把本次运行的事实压成一组布尔量，供各条目的 `when` 判据使用。

    每条判据只看**本次真的走到过那条路径**，不预测「将来可能怎样」：局限条目是给读者
    校准证据强度的，只有在它确实影响本次结论时才需要出现。
    """
    verdicts = [case.get("verdict") or {} for case in cases]
    clusters = [_cluster_of(case) for case in cases]
    availabilities = [cluster.get("availability") or {} for cluster in clusters]
    histories = [case.get("history") or [] for case in cases]
    matches = [match for history in histories for match in history]

    return {
        # 官方口径对齐被用上了（否则「分类树不含正文」与读者无关）
        "official_leaf_shown": any(item.get("official_leaf") for item in verdicts),
        # 真的用 kubeconfig 查过集群 —— 没查过就谈不上「SA 权限受限」
        "cluster_queried": any(cluster.get("kubeconfig_path") for cluster in clusters),
        # 集群归属存在歧义（Liqo 反射会让同一个 pod 从多个 kubeconfig 看到）
        "cluster_ambiguous": any(len(cluster.get("candidates") or []) > 1
                                 or cluster.get("candidate_note") for cluster in clusters),
        "availability_checked": any(item.get("checked") for item in availabilities),
        "availability_stem_match": any(item.get("match_kind") == "标签主干"
                                       for item in availabilities),
        # 查了但没拿到 pod 实证（pod 已回收是常态）
        "pod_recycled": any(cluster.get("not_obtained") for cluster in clusters),
        # pod 是靠标签定位的（推定），不是靠 pod 名精确匹配（确证）
        "pod_by_label": any((cluster.get("pod_evidence") or {}).get("pod")
                            and str(cluster.get("match_kind") or "").startswith("runner 标签")
                            for cluster in clusters),
        "history_matched": bool(matches),
        "curated_related": any(case.get("related_issues") for case in cases),
        # 有分数不低、但只命中通用词/workflow 名的匹配 —— 「分数高 ≠ 同现象」的现场
        "weak_leads": any(match.get("score", 0) >= 30
                          and match.get("evidence_strength") not in ("强",)
                          for match in matches),
        "conflicts": any(item.get("conflicts") for item in verdicts),
        "cluster_skipped": any(cluster.get("skipped") for cluster in clusters),
        # 抓过对端节点产物（多节点 job 才会走到这条路）
        "peer_attempted": any(case.get("peer") for case in cases),
        # 对端节点日志真的被用来兜底判桶了
        "peer_adopted": any(case.get("sig_source") == "对端节点日志" for case in cases),
    }


# 每条 = {id, text, when}。text 是**原文照搬**的历史条目（措辞里写着实测数据，不改写）；
# when 收一个 limitation_facts() 的返回字典。顺序保持与历史报告一致，便于与旧报告对照。
LIMITATIONS = [
    {"id": "official_tree_no_body",
     "text": "**官方分类树不含正文**：problem-tree.json 的 19 个叶子节点 text 为空，只能用来对齐分类口径，"
             "不能提供根因描述或修复建议——建议内容来自本工具的桶知识表 + 历史 issue 先例。",
     "when": lambda facts: facts["official_leaf_shown"]},
    {"id": "sa_permission_limited",
     "text": "**SA 权限受限**：kubeconfig 对应的 serviceaccount 只有 `pods[get,list]` 与 `pods/log[get,watch]`；"
             "`nodes`/`events`/`namespaces` 均 Forbidden。因此**拿不到调度事件**（FailedScheduling 的具体原因）、"
             "节点 condition 与 taint 列表，也无法用 `kubectl describe`（其 Events 段会 403）。",
     "when": lambda facts: facts["cluster_queried"]},
    # 原文这条的「三个」与「kubeconfig」之间少一个空格（跨行拼接时漏了），移过来时补上
    {"id": "label_suffix_not_cluster",
     "text": "**runner 标签后缀不能判集群**：Liqo 会把虚拟节点 pod 反射进共享 namespace，"
             "实测 `linux-aarch64-a3-800i-*-cn12-001` 的 pod 能同时从 aiframework/cn12-001/mind-third-ci 三个 "
             "kubeconfig 看到。本工具靠 Cluster.md 的标签登记 + 集群本地 CPU scale-set 标识收敛，并把歧义显式写出。",
     "when": lambda facts: facts["cluster_ambiguous"]},
    {"id": "availability_is_snapshot",
     "text": "**标签可用性核查是查询时刻的快照**：查到「无 runner」只能说明此刻没部署，"
             "不能证明失败当时 runner 掉线。官方分类树的「runs-on 标签不存在 / Runner 未上线」需另有失败时刻证据。",
     "when": lambda facts: facts["availability_checked"]},
    {"id": "registered_suffix_mismatch",
     "text": "**登记全名与 pod 实际命名可能对不上**：Cluster.md 登记的集群后缀（如 `-cn12-001`）与 pod 名实际用的"
             "后缀（实测 `…-chlqk-runner-*`、`…-{8位hex}-listener`）不一致，只按登记全名匹配会得出"
             "「无 runner 在线」的**假阴性**。故核查分两级：全名匹配（强证据）落空后，再按标签主干匹配（弱证据，"
             "要求名字带 runner/listener 标记），并把实测后缀变体原样列出 —— 此时结论是「标签族有效、登记后缀"
             "对不上实际命名」，**不是** runner 未上线。",
     "when": lambda facts: facts["availability_stem_match"]},
    {"id": "pod_recycled",
     "text": "**pod 已回收是常态**：历史失败的 job pod 多数早已回收，第 2 步只能降级为标签可用性核查；"
             "只有仍在运行或刚结束的 job 才能拿到 pod 级实证。",
     "when": lambda facts: facts["pod_recycled"]},
    {"id": "pod_by_label_is_inferred",
     "text": "**靠标签定位的 pod 是「推定」而非「确证」**：runner pod 会被复用，且未调度成功的 pod 也存在，"
             "故本工具在按标签匹配时只接受『真的启动过、且存在起点早于失败步骤』的 pod，"
             "拿不到就如实报「未取证」。但这只排除了**不可能**的候选，不能证明选中的那个一定跑过本 job ——"
             "同一窗口内该 scale-set 若并发跑过多个 job，仍需用容器日志里的 job 号二次确认。",
     "when": lambda facts: facts["pod_by_label"]},
    {"id": "exact_pod_name_immune",
     "text": "**pod 名精确匹配才免疫上面的推定问题**：job 在 GitHub 上报的 runner_name 带 5 位随机段，"
             "与 pod 名精确一致时可确认身份；只是这种理想情况依赖 pod 尚未被回收，实测多为已回收。",
     "when": lambda facts: facts["pod_by_label"] or facts["pod_recycled"]},
    {"id": "history_coverage",
     "text": "**历史匹配的覆盖面**：仅索引 issue 的标题、正文与评论，不含 PR 讨论、文档、IM 记录；"
             "且标签（label）无结构化语义（179 个 issue 中 2/3 无标签），匹配完全依赖错误签名，"
             "对没有可判别签名的纯描述性 issue 会漏检。",
     "when": lambda facts: facts["history_matched"]},
    {"id": "phrase_mismatch_curated",
     "text": "**换说法的同一现象，词面匹配连不上**：本工具说「模型缓存未命中」，历史 #238 说「找不到缓存模型」，"
             "两者无稀有词重叠（#238 只得 11 分、排 44 名）。这类已知同现象靠知识表里的**人工策展关联**兜住，"
             "策展表是有限的、需要人维护——报告里凡出自策展的条目都会标明，不会冒充自动发现。",
     "when": lambda facts: facts["curated_related"]},
    {"id": "high_score_not_same_issue",
     "text": "**分数高 ≠ 同现象**：实测 #228（AOP bisect 超时）仅凭 `schedule_nightly_test_a2` 这个工作流名"
             "就拿到 65 分。故报告对每条匹配都标出证据强度（强/中/弱），且只让命中「带机制签名」"
             "（如 exitcode:137）或「核心症状词」的复盘充当先例；`valueerror` 这类异常类名不算机制证据。",
     "when": lambda facts: facts["weak_leads"]},
    {"id": "no_auto_owner",
     "text": "**不自动裁定责任方**：历史先例只作线索与先例引用。当先例根因提到平台侧动作而日志侧判 code 时，"
             "报告会标为「证据冲突」并要求人工裁定，不会自动改写 owner。",
     "when": lambda facts: facts["conflicts"]},
    {"id": "decisive_early_exit",
     "text": "**部分 case 按规则跳过集群取证**：日志里出现 pytest 的判定行（用例收集结果/退出码）时，"
             "责任方已落在业务侧且集群侧查不出新信息，工具会**提前退出**第 2 步（报告里写明「按规则跳过」）。"
             "跳过不是「没查到」——两者在报告里的措辞与计数都分开。",
     "when": lambda facts: facts["cluster_skipped"]},
    {"id": "job_log_only_node0",
     "text": "**job log 只覆盖多节点 job 的一台机器（node0）**：`gh api …/jobs/{id}/logs` 返回的是 node0 的"
             "容器 stdout，其余机器（node1..nodeN）的日志**只**存在于 workflow 上传的 `*-ascend-logs` 产物里。"
             "本工具对 multi-node/double-node 开头的 job 会额外取该产物（报告里单列「对端节点日志」一节），"
             "但该产物**可能为空或未上传**：实测 run 36518916532 的 9 个失败多节点 job 里 8 个产物内层 tar"
             "零个常规文件（job 在容器日志产出前就已失败）。**产物为空 ≠ 对端节点无异常**，"
             "它只说明本次没有对端证据，此时结论仍只基于 node0。",
     "when": lambda facts: facts["peer_attempted"]},
    {"id": "peer_no_time_window",
     "text": "**对端节点日志没有时间窗对齐**：产物里的文本是 Docker 收集的整段容器 stdout，"
             "没有按失败步骤切分的依据，本工具只能取尾部若干行（`--peer-log-lines`，默认 400）。"
             "故对端文本只用于**兜底**（node0 判「未分类」时才采用，报告里注明来源并降置信度），"
             "不覆盖 node0 时间窗已给出的结论。",
     "when": lambda facts: facts["peer_adopted"]},
]


def select_limitations(cases: list, meta: dict | None = None) -> list:
    """返回**全部**条目，每条附 `triggered` 标志（不裁剪列表）。

    不在这里过滤的原因：json 要保持全量（哪些触发了也是一条信息，供回归比对），
    裁剪只发生在渲染层。`text` 与 `triggered` 都是纯数据，能直接进 json。
    """
    facts = limitation_facts(cases, meta)
    selected = []
    for item in LIMITATIONS:
        selected.append({"id": item["id"], "text": item["text"],
                         "triggered": bool(item["when"](facts))})
    return selected
