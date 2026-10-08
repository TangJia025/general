"""第 5 步：根因 + 修复建议的输出合成。

合成纪律（本工具最容易被误用的地方）：
  1. **三层证据并列，不互相覆盖**。日志侧（桶+owner）、集群侧（pod/容器/标签可用性）、
     历史侧（issue 先例）各自独立呈现，谁也不能静默改写谁。
  2. **冲突要显式标出**。当历史先例的根因指向的责任方与日志侧桶的 owner 不一致时
     （实测常见：#238 桶判 code、先例证明是平台清理脚本），报告必须把冲突摆出来并降置信度，
     而不是挑一个写进结论。
  3. **未取证就写未取证**。缺 kubeconfig、权限不足、pod 已回收都是正常结果，
     必须如实写，绝不用推测填充。
  4. 置信度只在有把握时给高：集群侧实证 > 历史强先例 > 仅日志桶正则 > 未分类。
"""
from __future__ import annotations

from .knowledge_tables import knowledge_for, official_leaf_title

# 容器终止原因 → (责任方倾向, 说明)。集群侧最硬的一类证据。
TERMINATION_INTERPRETATION = {
    "OOMKilled": ("mixed", "容器被 cgroup OOM 杀死（内存超限），需区分宿主内存与 NPU 显存"),
    "Error": ("infra", "容器异常退出，常伴随节点驱逐/drain；需结合 exitCode 与节点事件"),
    "Completed": ("code", "容器正常退出（exit 0）但 job 判失败——失败发生在业务步骤逻辑内，非容器层"),
    "ContainerStatusUnknown": ("infra", "容器状态丢失，通常因节点失联或 pod 被强制删除"),
}


def snapshot_pod_still_running(cluster: dict, pod_evidence: dict) -> bool:
    """本次 pod 证据是否取自「job 尚未结束、容器还在跑」的快照。

    为什么需要这个判断：那种快照下，容器的退出码与终止原因**尚未产生**，表里是空的。
    若不点明，读者会把空值读成「查过了，没有异常终止」—— 这是与事实相反的结论，
    而退出码恰恰是集群侧最硬的那件证据（实测「失败步骤结束 → job 结束」只有 55s，
    等得到退出码时 pod 多已回收，所以这个「空」是常态、必须如实解释）。

    判据不看快照时刻的 job 状态、而看**有没有任何容器给出退出码或终止原因**：
    只要确实拿到了退出码，就不需要那句提示，无论快照是什么时候取的。
    """
    if not cluster.get("snapshot_from"):
        return False
    for container in pod_evidence.get("containers") or []:
        if container.get("exit_code") is not None:
            return False
        if container.get("last_terminated_reason") or container.get("last_terminated_exit_code") is not None:
            return False
    return True


def interpret_pod_evidence(pod_evidence: dict) -> list:
    """把 pod 证据翻译成判断（而非罗列原始字段）。返回 [{"verdict","owner","detail"}]。"""
    verdicts = []
    if not pod_evidence:
        return verdicts
    for container in pod_evidence.get("containers") or []:
        reason = container.get("last_terminated_reason") or container.get("state_reason")
        if reason and reason in TERMINATION_INTERPRETATION:
            owner, detail = TERMINATION_INTERPRETATION[reason]
            verdicts.append({"verdict": f"容器 {container.get('container')} 上次终止原因 = {reason}",
                             "owner": owner, "detail": detail})
        exit_code = container.get("last_terminated_exit_code")
        if exit_code is not None and exit_code == 137 and reason != "OOMKilled":
            verdicts.append({
                "verdict": f"容器 {container.get('container')} exitCode=137 但原因非 OOMKilled",
                "owner": "infra",
                "detail": "137 = SIGKILL。既非 OOM 就多为外部终止（节点驱逐 / kubelet drain / 手动删除）。"
                          "实测先例 #257：postStart 钩子泄漏 exec 管道导致 containerd drain 超时后被杀。",
            })
    for init in pod_evidence.get("init_containers") or []:
        verdicts.append({"verdict": f"init 容器 {init.get('container')} 失败: {init.get('reason')}",
                         "owner": "infra",
                         "detail": "pod 根本没起来，真因在 init 阶段（镜像/权限/挂载），"
                                   "而非业务测试代码。"})
    for condition in pod_evidence.get("abnormal_conditions") or []:
        verdicts.append({"verdict": f"pod condition {condition.get('type')}={condition.get('status')}",
                         "owner": "infra",
                         "detail": condition.get("message") or condition.get("reason") or "调度/就绪未达预期"})
    return verdicts


def peer_basis_lines(case: dict) -> list[str]:
    """对端节点日志（多节点 job 的第二日志证据源）在报告里的依据行。

    ⚠️ **无论取到与否都要出话**：产物存在但为空是多节点 job 的常态（实测 run 36518916532 的
    9 个失败多节点 job 里 8 个产物内层 tar 零个常规文件），留白会被读成「对端节点无异常」——
    与事实正相反（那 8 个 job 的 node0 日志多为 pod Pending，日志根本没产生）。
    """
    peer = case.get("peer") or {}
    if not peer:
        return []
    adopted = case.get("sig_source") == "对端节点日志"
    artifact = peer.get("artifact") or "(未匹配到产物)"
    lines = []
    if peer.get("empty"):
        return [f"对端节点日志：**产物存在但为空**（`{artifact}`，tar 内只有目录项、无任何常规文件）"
                f" —— 该 job 大概率在容器日志产出前就已失败（如 pod 未就绪），"
                f"**不能**据此判「对端节点无异常」"]
    if not peer.get("ok"):
        return [f"对端节点日志：**未取得**（{peer.get('reason') or '未知原因'}）"
                f" —— 多节点 job 的 job log 只覆盖 node0，对端节点本次**没有**证据"]
    if not peer.get("peers"):
        return [f"对端节点日志：产物 `{artifact}` 内只有 node0 的容器日志，无对端节点文本"]
    nodes = "、".join(f"`{node}`（{peer.get('node_lines', {}).get(node, 0)} 行，"
                      f"取尾部 {peer.get('kept_lines', {}).get(node, 0)} 行）"
                      for node in peer["peers"])
    role = ("**node0 时间窗未分类，本 case 的桶由对端节点日志兜底得出**" if adopted
            else "与 node0 并列的第二证据（未参与本 case 定性）")
    lines.append(f"对端节点日志：{nodes}，命中桶【{peer.get('bucket')}】—— {role}")
    # 子行用 `  - `（嵌套列表）而不是裸缩进：渲染层对缩进行是原样透传，
    # 裸缩进在 markdown 里会变成上一行的续行、丢掉换行，读起来像一句话没说完。
    if peer.get("sig"):
        lines.append(f"  - 对端节点首条异常：`{(peer.get('sig') or '')[:160]}`")
    note = peer.get("note")
    if note:
        lines.append(f"  - ⚠️ {note}")
    return lines


def synthesize(case: dict) -> dict:
    """综合三层证据，产出 {root_cause, owner, confidence, basis[], conflicts[], needs_human}。

    只做加权与冲突标注，不做「换个 owner 写死」——责任方最终由人定。
    """
    bucket = case.get("bucket") or "未分类"
    log_owner = case.get("owner") or "unknown"
    knowledge = knowledge_for(bucket)
    basis, conflicts, hints_requiring_human = [], [], []

    cluster = case.get("cluster") or {}
    pod_evidence = cluster.get("pod_evidence")
    availability = cluster.get("availability")
    history = case.get("history") or []
    peer_adopted = case.get("sig_source") == "对端节点日志"

    # --- 日志侧基线 ---
    if peer_adopted:
        # 桶是从对端节点兜底来的，首行必须说清「node0 没判出来、结论来自对端」，
        # 否则读者会以为 node0 的失败步骤时间窗本身就命中了这个桶（证据强度差一个档）。
        basis.append(f"日志侧：node0 失败步骤时间窗**未能分类**，采用对端节点日志判桶 "
                     f"→ 【{bucket}】，owner={log_owner}")
    elif bucket != "未分类":
        basis.append(f"日志侧：命中桶【{bucket}】，owner={log_owner}")
    else:
        basis.append("日志侧：未能分类（正则无命中），需人工或集群侧补足")
    # 对端节点证据紧跟日志侧基线：它属同一层（容器 stdout），只是机器不同
    basis.extend(peer_basis_lines(case))

    # --- 集群侧证据 ---
    cluster_owner_votes = []
    pod_verdicts = interpret_pod_evidence(pod_evidence) if pod_evidence else []
    # 「按规则跳过」必须是一条**独立**的依据，不能落进下面「未解析出候选集群」那一支：
    # 后者说的是「想查但没查到集群」，与「日志已定性、规则上不必查」是两个相反的意思。
    if cluster.get("skipped"):
        basis.append(f"集群侧：**按规则跳过** —— {cluster.get('skip_reason')}")
    # 只有「一个候选集群都没解析出来」才能说「无可用 kubeconfig」。路径 B（pod 已回收、
    # 降级为标签可用性核查）不设 kubeconfig_path，那不是「没用上 kubeconfig」——
    # 早先只判 kubeconfig_path 为空就贴这条，会在明明查过集群的案例上凭空多出一句误导。
    if case.get("runner_name") and not cluster.get("candidates") and not cluster.get("skipped"):
        cluster["not_obtained"] = (cluster.get("not_obtained") or []) + [
            "集群侧未取证：未解析出任何候选集群（标签未登记于 Cluster.md，或无对应 kubeconfig）"]
    if pod_evidence:
        # 时序自洽性：runner pod 会被复用，若该 pod 启动于本 job 的失败步骤之后，
        # 它承载的是**另一次运行**，其状态与日志都不是本次失败的现场。
        # 这种证据不但不能计入 owner 票，还必须在报告里明确降级为「邻近 pod」。
        foreign_pod = cluster.get("time_consistent") is False
        if foreign_pod:
            basis.append(f"集群侧：找到的 pod `{pod_evidence.get('pod')}` 与本 job **时序不符** —— "
                         f"{cluster.get('window_note')}")
            basis.append("  ⚠️ 该 pod 的容器状态与日志**不计入**本次归因（它属于同 scale-set 的"
                         "另一次运行）；它只能证明该 runner 标签可用、集群本身在正常工作")
            hints_requiring_human.append(
                "集群侧命中的 pod 启动时间晚于本 job 的失败步骤，属同 scale-set 的另一次运行；"
                "本 job 的真实现场已回收，需靠 runner 侧日志或平台监控补足")
        else:
            for verdict in pod_verdicts:
                basis.append(f"集群侧：{verdict['verdict']} —— {verdict['detail']}")
                cluster_owner_votes.append(verdict["owner"])
        if not foreign_pod and not pod_verdicts:
            if snapshot_pod_still_running(cluster, pod_evidence):
                # 快照取自 job 结束前：没有退出码是**时间点**造成的，不是「无异常」。
                # 说成「无异常终止记录」是反向结论 —— 它会让一个本该存疑的现场显得清白。
                basis.append(f"集群侧：pod 已找到（phase={pod_evidence.get('phase')}，"
                             f"节点 `{pod_evidence.get('node')}`），但快照取自 job 结束前、"
                             f"容器仍在运行 —— 退出码与终止原因**尚未产生**，不能据此判「无异常」")
                hints_requiring_human.append(
                    "集群侧拿到的是失败时刻的进程内日志，没有退出码；"
                    "如需退出码必须在 job 结束的瞬间补查（实测窗口约 55s，随后 pod 即被回收）")
            else:
                basis.append(f"集群侧：pod 已找到（phase={pod_evidence.get('phase')}，"
                             f"节点 `{pod_evidence.get('node')}`），"
                             f"但容器状态无异常终止记录，未能据此指向责任方")
    elif availability and availability.get("checked"):
        labels_text = "、".join(case.get("labels") or []) or "—"
        cluster_label = cluster.get("availability_cluster") or cluster.get("cluster_name")
        variants_text = "、".join(f"`{segment}`×{count}" for segment, count
                                 in sorted((availability.get("suffix_variants") or {}).items()))
        if availability.get("available") and availability.get("match_kind") == "标签主干":
            # 登记后缀与 pod 名实际后缀不一致（实测：登记 cn12-001，实际 chlqk）。
            # 标签族**确实在线**，所以「标签不存在」「runner 未上线」两个结论都不成立；
            # 对不上的是注册表本身。这与「有 runner」和「没 runner」都是两回事，必须单独措辞。
            basis.append(f"集群侧：runner 标签 `{labels_text}` 在集群 `{cluster_label}` **确有** "
                         f"{availability.get('runners_online')} 个 runner / "
                         f"{availability.get('listeners')} 个 listener，但 pod 名用的后缀是 "
                         f"{variants_text or '—'}，与 Cluster.md 登记的后缀 "
                         f"`{availability.get('registered_suffix') or '—'}` **不一致** —— "
                         f"即**标签族有效**，对不上的是登记后缀与实际命名（注册表问题），"
                         f"**不是** runner 未上线")
            hints_requiring_human.append(
                f"Cluster.md 为该标签登记的集群后缀（`{availability.get('registered_suffix') or '—'}`）"
                f"与 pod 名实际后缀（{variants_text or '—'}）不一致；按登记全名匹配会得出"
                f"「无 runner 在线」的**假阴性**，登记信息需要修正")
        elif availability.get("available"):
            basis.append(f"集群侧：runner 标签 `{labels_text}` 在集群 `{cluster_label}` 有 "
                         f"{availability.get('runners_online')} 个 runner / "
                         f"{availability.get('listeners')} 个 listener，标签本身有效")
        else:
            # 「没查到」**不计入 owner 票**：这是查询时刻的快照，证不了失败当时的状态。
            # 只作为提示，并强制人工确认——早先的实现把它当成 infra 的集群侧实证，
            # 会让一个无效的负向结果抬高归因置信度。
            # 措辞要交代**查过哪些匹配方式**：阴性结论的强度取决于查得多宽，
            # 只写「未查到」会让读者以为只按登记全名试过一次（那正是假阴性的来源）。
            scopes = "、".join(availability.get("scopes_checked") or ["全名"])
            basis.append(f"集群侧：在候选集群中**未查到** runner 标签 `{labels_text}` 的任何 pod"
                         f"（匹配方式：{scopes}，均 0 命中）")
            basis.append("  ⚠️ 仅为查询时刻快照，**不能据此断言**失败当时 runner 掉线；"
                         "官方分类树的「runs-on 标签不存在 / Runner 未上线」需另有失败时刻证据")
            hints_requiring_human.append(
                "集群侧未查到该 runner 标签 —— 若怀疑是「标签不存在 / runner 未上线」，"
                "需补充失败时刻的 runner 侧证据后确认")
    elif availability and availability.get("checked") is False:
        cluster["not_obtained"] = (cluster.get("not_obtained") or []) + [
            f"集群侧未取证：{availability.get('reason')}"]

    # --- 历史先例 ---
    # 只有「机制性证据」命中的复盘才能当先例：命中了错误签名，或命中了核心症状词
    # （稀有且非出处名，见 IssueIndex.match）。仅靠 nightly/e2e/yaml/workflow 名命中的，
    # 哪怕分数更高也只能是「同为夜间任务」——实测 #228（AOP bisect 超时）就以 65.66 分
    # 盖过同现象的 #238，若采信它，报告会把无关的修复方案抄进建议里，比不给先例更糟。
    strong_precedent, weak_leads = None, []
    for match in history:
        if not match["issue"].get("is_postmortem") or match["score"] < 30:
            continue
        if match.get("evidence_strength") in ("强", "中"):
            if strong_precedent is None or match["score"] > strong_precedent["score"]:
                strong_precedent = match
        else:
            weak_leads.append(match)
    if strong_precedent:
        issue = strong_precedent["issue"]
        where = "评论" if issue.get("root_cause_source") == "comment" else "正文"
        mechanism = strong_precedent.get("mechanism_signatures") or []
        evidence = ("命中带机制的签名 `" + "`, `".join(mechanism[:3]) + "`"
                    if mechanism
                    else "命中核心症状词 " + "、".join(strong_precedent["core_keywords"][:4]))
        basis.append(f"历史先例：#{issue['number']}（score={strong_precedent['score']}，"
                     f"根因取自{where}，{evidence}）{issue['title'][:60]}")
    if weak_leads:
        listed = "、".join(f"#{item['issue']['number']}(score={item['score']})"
                          for item in weak_leads[:3])
        hints_requiring_human.append(
            f"历史库中另有分数不低的复盘（{listed}），但它们只命中通用词或 workflow 名，"
            f"机制未必与本例相同，故**未**采信为先例；如需引用请先人工核对")

    # --- owner 判定与冲突 ---
    owner = log_owner
    if cluster_owner_votes:
        # 集群侧实证比日志侧正则硬：一致则强化，不一致则记冲突
        if all(vote == log_owner for vote in cluster_owner_votes):
            basis.append(f"集群侧与日志侧 owner 一致（{log_owner}），归因可信度提升")
        else:
            conflicts.append(f"日志侧判 owner={log_owner}，集群侧证据指向 {'/'.join(sorted(set(cluster_owner_votes)))}")
    # 只在知识表**确实给了** owner 且与日志侧不符时才算冲突。
    # 动态桶（「步骤直接定性:*」）的知识条目 owner=None，若无条件比较会把 None 当差异，
    # 给每个动态桶都凭空造出一条「证据冲突」。
    knowledge_owner = knowledge.get("owner")
    if strong_precedent and knowledge_owner and log_owner not in (knowledge_owner, "unknown"):
        conflicts.append(f"日志侧判 owner={log_owner}，但知识表对该桶的预置 owner 是 {knowledge_owner}")
    # 历史先例的根因若提到平台侧动作，而日志侧却判了 code，这是最危险的一类误归属
    # （实测 #238：桶判 code，先例证明是平台老化脚本）——摆出来提示核对，但不自动改判。
    if log_owner == "code" and strong_precedent:
        signals = strong_precedent["issue"].get("platform_signals") or []
        if signals:
            conflicts.append(
                f"日志侧判 owner=code，但历史先例 #{strong_precedent['issue']['number']} "
                f"的根因提到平台侧动作（{'；'.join(signals)}）——"
                f"请核对是否属「错误信号所在层 ≠ 责任方所在层」的情形，勿直接采信 code")
    # 策展关联走的是另一条路：词面匹配连不上它们（见 step4_related），
    # 但它们恰恰是「同现象、责任方相反」的高价值判例，冲突判定必须一并覆盖。
    for item in case.get("related_issues") or []:
        signals = item.get("platform_signals") or []
        if log_owner == "code" and signals:
            conflicts.append(
                f"日志侧判 owner=code，但知识表登记的同现象先例 #{item['number']} "
                f"的根因提到平台侧动作（{'；'.join(signals)}）——"
                f"请核对是否属「错误信号所在层 ≠ 责任方所在层」的情形，勿直接采信 code")

    # --- 置信度 ---
    if conflicts:
        confidence = "低（三层证据存在冲突，需人工裁定）"
        needs_human = True
    elif pod_evidence and cluster_owner_votes:
        confidence = "高（集群侧实证 + 日志侧一致）"
        needs_human = False
    elif pod_evidence and cluster.get("time_consistent") is False:
        # 找到的 pod 属于另一次运行：既不能提升置信度，也不能当作「pod 状态无异常」的证据
        confidence = "中低（集群侧只找到同 scale-set 另一次运行的 pod，非本 job 现场）"
        needs_human = True
    elif cluster.get("skipped") and bucket != "未分类":
        # 判据是测试框架**自己打印**的判定行（不是正则撞上的关键词），比普通日志桶硬一档；
        # 但不给「高」：没有集群侧实证，且「改 CI 编排还是让分支 rebase」仍需人来定。
        confidence = "中高（日志侧决定性判据：测试框架自身的判定行；按规则未做集群取证）"
        needs_human = True
    elif strong_precedent and strong_precedent["evidence_strength"] == "强":
        confidence = "中高（命中同签名的历史先例，但缺集群侧实证）"
        needs_human = False
    elif strong_precedent:
        # 同主题先例只是线索：它证明过「同类失败曾这样发生」，不证明「本次就是这样」。
        # 因此不给「中高」，且要求人工过一眼。
        confidence = "中（有同主题历史先例，机制未必相同，且缺集群侧实证）"
        needs_human = True
    elif pod_evidence:
        confidence = "中（集群侧已定位 pod 但状态无异常，未能据此定性）"
        needs_human = True
    elif bucket != "未分类":
        confidence = "中（仅日志侧正则定性，未经集群侧验证）"
        needs_human = True
    else:
        confidence = "低（未分类）"
        needs_human = True
    # 靠对端节点日志兜底才定性的 case 不给「高」：那个桶是从产物的**尾部粗切**文本里判出来的，
    # 没有时间窗对齐，而 node0 自己什么都没有判出来（见 peer_logs.adopt_peer_bucket）。
    # 集群侧即便一致，也只是「同一台机器的旁证」，不等于给这段粗切文本补上了时间基准。
    if peer_adopted and confidence.startswith("高"):
        confidence = "中（采用对端节点日志兜底定性，对端文本无时间窗对齐）"
    if peer_adopted:
        hints_requiring_human.append(
            "本 case 的桶来自对端节点日志（node0 时间窗未分类）：对端文本是产物的尾部粗切、"
            "与失败步骤没有时间窗对齐，需人工按该节点的时间戳复核后再落库")

    # 有「需人工确认」的提示项时，置信度不能标为不需人工
    if hints_requiring_human:
        needs_human = True

    # --- 根因描述 ---
    if pod_evidence and cluster_owner_votes:
        root_cause = f"{bucket}；集群侧实证：" + "；".join(
            item["verdict"] for item in interpret_pod_evidence(pod_evidence))
    elif strong_precedent and strong_precedent["evidence_strength"] == "强":
        # 措辞由**证据类型**决定，不由裸分数决定：命中带机制的签名（如 exitcode:137）
        # 几乎不会偶然撞上，才配得上「高度吻合」；分数高但只有通用词/桶名重叠的，
        # 只能说「主题相近」——两者对读者的含义差别很大，不能混为一谈。
        root_cause = f"{bucket}；与历史先例 #{strong_precedent['issue']['number']} 高度吻合"
    elif strong_precedent:
        root_cause = (f"{bucket}；与历史先例 #{strong_precedent['issue']['number']} 主题相近"
                      f"（可参考，但机制未必相同）")
    elif bucket != "未分类":
        root_cause = bucket
    else:
        root_cause = "未能定性（需人工介入）"

    # --- 修复建议：知识表 + 先例的修复小节 ---
    suggestions = list(knowledge.get("action") or [])
    if strong_precedent and strong_precedent["issue"]["sections"].get("fix"):
        suggestions.append(f"历史先例 #{strong_precedent['issue']['number']} 的修复记录："
                           f"{strong_precedent['issue']['sections']['fix'][:400]}")
    if strong_precedent and strong_precedent["issue"]["sections"].get("prevention"):
        suggestions.append(f"历史先例 #{strong_precedent['issue']['number']} 的防复发建议："
                           f"{strong_precedent['issue']['sections']['prevention'][:300]}")

    return {
        "root_cause": root_cause,
        "owner": owner,
        "owner_from_cluster": bool(cluster_owner_votes),
        "confidence": confidence,
        "basis": basis,
        "conflicts": conflicts,
        # 未达「冲突」程度、但必须人工确认的提示项（如集群侧查无 runner 的快照级负向结果）
        "hints_requiring_human": hints_requiring_human,
        "needs_human": needs_human,
        "official_leaf": knowledge.get("leaf"),
        "suggestions": suggestions,
        "precedent": ({"number": strong_precedent["issue"]["number"],
                       "title": strong_precedent["issue"]["title"],
                       "url": strong_precedent["issue"]["url"],
                       "score": strong_precedent["score"],
                       "root_cause": strong_precedent["issue"]["sections"].get("root_cause"),
                       "source": strong_precedent["issue"].get("root_cause_source")}
                      if strong_precedent else None),
    }


def render_case(case: dict, index: int) -> list:
    """渲染单个失败 job 的完整取证过程。"""
    lines = []
    lines.append(f"### case {index}. {case.get('job_name')}　`{case.get('workflow')}`")
    lines.append("")
    lines.append(f"- 失败 job：{case.get('link')}")
    lines.append(f"- 失败步骤：`{case.get('step') or '未知'}`"
                 f"　芯片：{case.get('chip') or 'CPU 门禁'}"
                 f"　runner 标签：`{', '.join(case.get('labels') or []) or '—'}`")
    if case.get("runner_name"):
        lines.append(f"- runner pod 名：`{case['runner_name']}`")
    window = case.get("window") or {}
    if window.get("started_at"):
        lines.append(f"- 失败步骤时间窗：{window['started_at']} ~ {window.get('completed_at') or '(未完成)'}")
    lines.append("")

    # --- 第 1 步的第二证据源：对端节点日志（紧跟日志侧元信息，在集群段之前）---
    # 「未取得/产物为空」也必须成段出现：多节点 job 的 job log 只有 node0 一台机器，
    # 对端节点本次没有证据，与「对端节点无异常」是两回事。
    peer = case.get("peer") or {}
    if peer:
        lines.append("#### 对端节点日志（第 1 步第二证据源）")
        lines.append("")
        lines.append(f"- 产物：`{peer.get('artifact') or '(未匹配到产物)'}`"
                     + (f"（artifact_id={peer['artifact_id']}，"
                        f"{'本地缓存' if peer.get('from_cache') else '本次下载'}）"
                        if peer.get("artifact_id") else ""))
        for line in peer_basis_lines(case):
            lines.append(f"- {line}" if not line.startswith(" ") else line)
        if peer.get("peers") and peer.get("sig"):
            # 展开块只放「依据原文 + 怎么拿全文」：摘要行已在上面的依据里给过，
            # 不重复；整段日志不复制进报告（一份就 800+ 行），按 artifact_id 随时可取回。
            lines.append("")
            lines.append("<details><summary>对端节点首条异常原文</summary>")
            lines.append("")
            lines.append("```")
            lines.append((peer.get("sig") or "")[:300])
            lines.append("```")
            lines.append(f"完整文本随产物留存，可用 `gh api repos/<owner>/<repo>/actions/"
                         f"artifacts/{peer.get('artifact_id')}/zip` 重新取得"
                         f"（本报告只登记判定依据，不复制整段日志）")
            lines.append("")
            lines.append("</details>")
        lines.append("")

    # --- 第 2 步：集群现场 ---
    lines.append("#### 集群侧现场（第 2 步）")
    lines.append("")
    cluster = case.get("cluster") or {}
    if cluster.get("skipped"):
        # 「按规则跳过」与「未取证」必须分开写：前者是日志已定性、规则上不必查，
        # 后者是查了没查到。写成后者的措辞会把一个确定结论读成一次失败的取证。
        lines.append(f"- 取证集群：**按规则跳过** —— {cluster.get('skip_reason')}")
        lines.append("- 该 case 的判据来自日志里测试框架自己的输出（pytest 的收集结果/退出码），"
                     "责任方已落在业务侧；集群侧的 pod/节点状态即便查到，也只能说明「容器当时活着」，"
                     "给不出新信息——故**不**计入「集群侧取得 pod 实证」的分母")
    if cluster.get("candidate_note"):
        lines.append(f"- 集群归属判定：{cluster['candidate_note']}")
    if cluster.get("queried_labels"):
        # 如实记下**实际查的是哪个标签**：job 上报展示名，pod 名用带后缀全名，
        # 两者不写清楚，「查无 pod」就会被误读成「runner 不在线」
        lines.append("- 实际查询的标签全名："
                     + "、".join(f"`{label}`" for label in cluster["queried_labels"]))
    if cluster.get("kubeconfig_path"):
        lines.append(f"- 取证集群：`{cluster.get('cluster_name')}`"
                     f"（namespace `{cluster.get('namespace')}`，kubeconfig `{cluster.get('filename')}`）")
        identity = cluster.get("identity") or {}
        if identity.get("reachable"):
            lines.append(f"- 连通性：正常（server {identity.get('server_version')}，"
                         f"身份 `{identity.get('identity')}`）")
        else:
            lines.append(f"- 连通性：**不可达** —— {identity.get('error')}")
        if cluster.get("match_kind"):
            lines.append(f"- pod 定位方式：{cluster['match_kind']}")
    elif cluster.get("candidates"):
        # 有候选集群却没拿到 pod：多是路径 B（pod 已回收）。这句话不能说成「没有目标集群」，
        # 那是**另一个**结论，会把「现场已回收」误读成「工具没找到集群」。
        lines.append(f"- 取证集群：候选 {['`%s`' % name for name in cluster['candidates']]}"
                     f"　**未取得本 job 的 pod 实证**（详见下方未取证说明）")
    elif cluster.get("not_obtained"):
        lines.append("- 取证集群：**未取证**（详见下方未取证说明）")
    elif not cluster.get("skipped"):
        lines.append("- 取证集群：**未取证**（未解析出候选集群，详见上方判定说明）")
    # 跳过的 case 到此不再输出「未取证」字样（它已在上面写明「按规则跳过」）——两者相反，不可混用
    if cluster.get("snapshot_from"):
        # 证据来源必须写明「什么时候取的」：快照是 job 还在跑时抢下的，
        # 与「事后补查」是两个不同时刻的现场，读者据此判断证据有多硬。
        lines.append(f"- 证据来源：**失败时刻的集群快照**（{cluster.get('snapshot_taken_at') or '时刻未记录'}"
                     f"；{cluster.get('snapshot_note') or '监听器在 job 结束前抢下'}）")
        lines.append(f"  - 快照文件：`{cluster.get('snapshot_from')}`")
    pod_evidence = cluster.get("pod_evidence")
    if pod_evidence:
        # 兜底防呆：按 find_job_pod 的构造（只返回能承载过本 job 的 pod），走到这里的 pod
        # 时序必然自洽；但万一将来那层被改松，这一层必须拦住——把另一次运行的容器状态与日志
        # 贴成「本次失败的现场」是本工具最危险的错误，报告层不能再假设上游一定对。
        if cluster.get("time_consistent") is False:
            lines.append("")
            lines.append(f"**邻近 pod（非本 job）**：`{pod_evidence.get('pod')}` "
                         f"phase={pod_evidence.get('phase')} node=`{pod_evidence.get('node')}` "
                         f"start={pod_evidence.get('start_time')}")
            lines.append("")
            lines.append(f"- ⚠️ {cluster.get('window_note')}")
            lines.append("- 故此处**不展示**其容器状态与日志（属另一次运行）；"
                         "它只能说明该 runner 标签可用、集群在正常工作")
        else:
            lines.append("")
            lines.append(f"**pod 证据**：`{pod_evidence.get('pod')}` "
                         f"phase={pod_evidence.get('phase')} node=`{pod_evidence.get('node')}` "
                         f"启动={pod_evidence.get('start_time') or '—（未调度，无 startTime）'}"
                         f" 创建={pod_evidence.get('created_time') or '—'}")
            if cluster.get("match_reason"):
                lines.append(f"- 定位说明：{cluster['match_reason']}")
            if cluster.get("window_note"):
                # 时序本身自洽，但时间戳存疑（如精确名匹配却对不上时间）——仍需提示
                lines.append(f"- {cluster['window_note']}")
            lines.append("")
            lines.append("| 容器 | 状态 | 退出码 | 上次终止原因 | 重启 |")
            lines.append("|---|---|---|---|---|")
            for container in pod_evidence.get("containers") or []:
                lines.append(f"| {container.get('container')} | {container.get('state_reason') or container.get('state') or '—'} "
                             f"| {container.get('exit_code') if container.get('exit_code') is not None else '—'} "
                             f"| {container.get('last_terminated_reason') or '—'} "
                             f"| {container.get('restart_count')} |")
            if snapshot_pod_still_running(cluster, pod_evidence):
                lines.append(f"- ⚠️ 取证时本 job **尚未结束**、容器仍在运行，"
                             f"故表中的退出码/终止原因**尚未产生**（不是「无异常」）。"
                             f"要拿到退出码必须等 job 结束，而实测那时 pod 多已被回收")
            for verdict in interpret_pod_evidence(pod_evidence):
                lines.append(f"- 判定：{verdict['verdict']} —— {verdict['detail']}")
            logs = cluster.get("logs") or []
            for log_entry in logs:
                if log_entry.get("ok") and log_entry.get("text"):
                    lines.append("")
                    lines.append(f"<details><summary>容器 {log_entry.get('container')} 日志尾部"
                                 f"（{log_entry.get('source')}）</summary>")
                    lines.append("")
                    lines.append("```")
                    lines.append("\n".join(log_entry["text"].splitlines()[-40:]))
                    lines.append("```")
                    lines.append("</details>")
    availability = cluster.get("availability")
    if availability and availability.get("checked"):
        lines.append("")
        lines.append(f"**runner 标签可用性**（集群 `{cluster.get('availability_cluster')}`）：")
        variants = "、".join(f"`{segment}`×{count}" for segment, count
                            in sorted((availability.get("suffix_variants") or {}).items()))
        if availability.get("available") and availability.get("match_kind") == "标签主干":
            lines.append(f"存在 {availability.get('runners_online')} 个 runner、"
                         f"{availability.get('listeners')} 个 listener"
                         f"（namespace: {', '.join(availability.get('namespaces') or [])}）"
                         f" → 标签族有效，失败**不是**标签不存在导致的")
            lines.append(f"- ⚠️ 匹配方式为**标签主干**（登记全名 `{availability.get('claimed_label') or '—'}` "
                         f"0 命中）：实测 pod 后缀 {variants or '—'}，Cluster.md 登记后缀 "
                         f"`{availability.get('registered_suffix') or '—'}` —— 对不上的是**登记后缀与实际命名**，"
                         f"**不是** runner 未上线；按登记全名匹配会得出「无 runner」的假阴性")
        elif availability.get("available"):
            lines.append(f"存在 {availability.get('runners_online')} 个 runner、"
                         f"{availability.get('listeners')} 个 listener"
                         f"（namespace: {', '.join(availability.get('namespaces') or [])}）"
                         f" → 标签有效，失败**不是**标签不存在导致的")
        else:
            scopes = "、".join(availability.get("scopes_checked") or ["全名"])
            lines.append(f"未找到任何匹配 pod（匹配方式：{scopes}，均 0 命中）"
                         f" → 该标签此刻在本集群无 runner/listener。"
                         f"⚠️ 仅为**查询时刻**快照，不能证明失败当时掉线")
        # 逐标签明细：job 上报的是展示名，pod 名用的是带集群后缀的全名，
        # 两者都可能被查过；不列出就分不清「哪个写法真的没有」
        per_label = availability.get("per_label") or {}
        if per_label:
            lines.append("")
            lines.append("| 实际查询的标签全名 | 匹配 pod | runner | listener |")
            lines.append("|---|---|---|---|")
            for label, item in per_label.items():
                lines.append(f"| `{label}` | {item.get('matched_pods')} | "
                             f"{item.get('runners_online')} | {item.get('listeners')} |")
        by_cluster = cluster.get("availability_by_cluster") or {}
        if len(by_cluster) > 1:
            lines.append("")
            lines.append("| 候选集群 | 匹配 pod | runner | listener |")
            lines.append("|---|---|---|---|")
            for name, item in by_cluster.items():
                lines.append(f"| `{name}` | {item.get('matched_pods')} | "
                             f"{item.get('runners_online')} | {item.get('listeners')} |")
    for note in cluster.get("not_obtained") or []:
        lines.append(f"- ⚠️ 未取证：{note}")
    lines.append("")

    # --- 第 4 步：历史先例 ---
    history = case.get("history") or []
    lines.append("#### 历史问题定位（第 4 步）")
    lines.append("")
    if not history:
        lines.append("- 知识库中未找到相关历史 issue（签名与关键词均无命中）")
    else:
        for match in history:
            issue = match["issue"]
            tag = "📌" if issue.get("is_postmortem") else "　"
            source = {"body": "正文", "comment": "评论"}.get(issue.get("root_cause_source"), "")
            # 逐条标出证据强度：分数高不等于同现象（#228 65 分靠工作流名，#238 11 分才是同现象）
            strength = match.get("evidence_strength") or "—"
            lines.append(f"- {tag} #{issue['number']}（score={match['score']}，证据强度={strength}"
                         f"{'，根因在' + source if source else ''}，{issue.get('state')}）"
                         f"[{issue['title'][:70]}]({issue['url']})")
            if match["matched_signatures"]:
                lines.append(f"  - 命中签名：`{'`, `'.join(match['matched_signatures'][:6])}`")
            if match.get("core_keywords"):
                lines.append(f"  - 命中核心症状词：{'、'.join(match['core_keywords'][:6])}")
            if match["matched_keywords"]:
                lines.append(f"  - 命中关键词：{', '.join(match['matched_keywords'][:6])}")
            if issue["sections"].get("root_cause"):
                lines.append(f"  - 历史根因：{issue['sections']['root_cause'][:300]}")
    related = case.get("related_issues") or []
    if related:
        matched_numbers = {match["issue"]["number"] for match in history}
        lines.append("")
        lines.append("**人工策展的历史关联**（知识表登记的已知同现象；词面匹配对它们不可靠，"
                     "故不依赖分数，一律列出）：")
        lines.append("")
        for item in related:
            if item.get("missing"):
                lines.append(f"- ⚠️ #{item['number']}：知识表登记了这个号，但当前索引里查不到"
                             f"（号写错或该 issue 已不可见）")
                continue
            source = {"body": "正文", "comment": "评论"}.get(item.get("root_cause_source"), "")
            # 上面按分数列过的，这里只补「为何关联」（那是策展独有的信息），
            # 不再重复贴一遍标题/根因，免得同一 issue 在报告里出现两次长得一样的块
            if item["number"] in matched_numbers:
                lines.append(f"- #{item['number']}：为何关联 —— {item.get('why') or '（未填）'}")
                continue
            lines.append(f"- #{item['number']}（{item.get('state')}"
                         f"{'，根因在' + source if source else ''}）"
                         f"[{item['title'][:70]}]({item['url']})")
            if item.get("why"):
                lines.append(f"  - 为何关联：{item['why']}")
            if item.get("root_cause"):
                lines.append(f"  - 历史根因：{item['root_cause'][:300]}")
    lines.append("")

    # --- 第 5 步：结论 ---
    verdict = case.get("verdict") or {}
    lines.append("#### 根因与修复建议（第 5 步）")
    lines.append("")
    lines.append(f"- **根因**：{verdict.get('root_cause')}")
    lines.append(f"- **责任方（owner）**：`{verdict.get('owner')}`"
                 f"{'（含集群侧实证）' if verdict.get('owner_from_cluster') else '（仅日志侧判定）'}")
    leaf_title = official_leaf_title(verdict.get("official_leaf"))
    if leaf_title:
        lines.append(f"- **官方口径对齐**：{leaf_title}")
    lines.append(f"- **置信度**：{verdict.get('confidence')}")
    if verdict.get("needs_human"):
        lines.append("- ⚠️ **需人工确认**")
    lines.append("")
    lines.append("**判断依据：**")
    lines.append("")
    for item in verdict.get("basis") or []:
        lines.append(f"- {item}" if not item.startswith("  ") else item)
    if verdict.get("conflicts"):
        lines.append("")
        lines.append("**⚠️ 证据冲突（不自动裁定，请人工判断）：**")
        lines.append("")
        for conflict in verdict["conflicts"]:
            lines.append(f"- {conflict}")
    if verdict.get("hints_requiring_human"):
        lines.append("")
        lines.append("**需人工确认（未达冲突程度，但不能自动采信）：**")
        lines.append("")
        for hint in verdict["hints_requiring_human"]:
            lines.append(f"- {hint}")
    lines.append("")
    lines.append("**修复建议：**")
    lines.append("")
    for number, suggestion in enumerate(verdict.get("suggestions") or [], 1):
        # 显式编号：Markdown 渲染时会自动重排，但本报告常被当纯文本阅读，全 1. 会看不出顺序
        lines.append(f"{number}. {suggestion}")
    lines.append("")
    return lines


def render_report(cases: list, meta: dict, registry_plan: list, health: dict,
                  integrity: dict) -> str:
    """渲染整份报告。"""
    lines = ["# 昇腾 CI 失败根因分析报告（集群取证版）", ""]
    lines.append(f"- 仓库：`{meta.get('repo')}`　起始：{meta.get('since')}　"
                 f"芯片范围：{meta.get('chips') or '不限'}")
    lines.append(f"- 生成时间：{meta.get('generated_at')}")
    lines.append(f"- 日志侧来源：`{meta.get('handoff_source')}`")
    lines.append("")

    # 摘要
    lines.append("## 摘要")
    lines.append("")
    lines.append(f"- 本次分析失败 job {meta.get('total_jobs', 0)} 个，"
                 f"选取 {len(cases)} 个进入集群取证与历史归因")
    skipped_cases = [case for case in cases if (case.get("cluster") or {}).get("skipped")]
    if skipped_cases:
        # 跳过不是少查了：判据在日志里已经给全了。这句话同时解释了为什么下面的
        # 「取得 pod 实证」的分母比 cases 少 —— 否则读者会以为有 case 被漏掉
        lines.append(f"- 其中 {len(skipped_cases)} 个日志侧已定性为业务侧（"
                     f"{'、'.join(sorted({case.get('bucket') or '未分类' for case in skipped_cases}))}），"
                     f"**按规则跳过**集群取证（判据来自测试框架自身，集群侧给不出新信息）")
    owners: dict = {}
    needs_human = 0
    for case in cases:
        verdict = case.get("verdict") or {}
        owners[verdict.get("owner")] = owners.get(verdict.get("owner"), 0) + 1
        if verdict.get("needs_human"):
            needs_human += 1
    lines.append(f"- 归因分布：{'，'.join(f'{k} × {v}' for k, v in sorted(owners.items(), key=lambda x: -x[1]))}")
    lines.append(f"- 需人工确认：{needs_human} 个")
    cluster_hits = sum(1 for case in cases
                       if (case.get("cluster") or {}).get("pod_evidence")
                       and (case.get("cluster") or {}).get("time_consistent") is not False)
    foreign_hits = sum(1 for case in cases
                       if (case.get("cluster") or {}).get("time_consistent") is False)
    snapshot_hits = sum(1 for case in cases if (case.get("cluster") or {}).get("snapshot_from"))
    queried_total = len(cases) - len(skipped_cases)
    lines.append(f"- 集群侧取得 pod 实证：{cluster_hits}/{queried_total} 个"
                 f"（其余 pod 多已回收，降级为标签可用性核查或未取证）")
    if snapshot_hits:
        # 快照与现场查询的证据强度不同，必须在汇总里分开计数：
        # 快照取自失败时刻（job 还在跑），现场查询是几分钟后的事。
        lines.append(f"- 其中 {snapshot_hits} 个取自**失败时刻的集群快照**"
                     f"（监听器在 job 结束前抢下），而非事后现场补查")
    if foreign_hits:
        # 不能把这几个算进「取得了实证」：它们找到的是另一次运行的 pod
        lines.append(f"- 其中 {foreign_hits} 个只找到同 scale-set **另一次运行**的 pod，"
                     f"已标注「时序不符」且未计入归因")
    lines.append("")

    # 集群可用性
    lines.append("## 集群取证可用性")
    lines.append("")
    lines.append("| 集群 | kubeconfig | server | 备注 |")
    lines.append("|---|---|---|---|")
    for row in registry_plan:
        flag = "✅" if row["has_kubeconfig"] else "❌ 缺失"
        lines.append(f"| `{row['cluster']}` | {flag} | {row.get('server') or '—'} | {row.get('warning') or ''} |")
    lines.append("")
    if health:
        lines.append("### 各集群现场快照")
        lines.append("")
        for cluster_name, snapshot in health.items():
            if not snapshot.get("available"):
                lines.append(f"- `{cluster_name}`：未取证 —— {snapshot.get('reason')}")
                continue
            lines.append(f"- `{cluster_name}`：pod 共 {snapshot.get('pod_total')} 个，"
                         f"分布 {snapshot.get('by_phase')}")
            if snapshot.get("not_ready"):
                lines.append(f"  - 未就绪 {snapshot.get('not_ready_count')} 个："
                             f"{'; '.join(snapshot['not_ready'][:5])}")
            if snapshot.get("abnormal_pods"):
                lines.append(f"  - 异常 pod：{'; '.join(snapshot['abnormal_pods'][:5])}")
        lines.append("")

    # 逐案例
    lines.append("## 逐案取证")
    lines.append("")
    for index, case in enumerate(cases, 1):
        lines.extend(render_case(case, index))

    # 局限
    lines.append("## 能力边界与局限")
    lines.append("")
    for item in integrity.get("limitations") or []:
        lines.append(f"- {item}")
    if integrity.get("errors"):
        lines.append("")
        lines.append("### 本次运行遇到的错误")
        lines.append("")
        for error in integrity["errors"]:
            lines.append(f"- {error}")
    return "\n".join(lines) + "\n"
