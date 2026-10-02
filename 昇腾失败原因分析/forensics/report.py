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

import re

from .knowledge_tables import knowledge_for, official_leaf_title

# 容器终止原因 → (责任方倾向, 说明)。集群侧最硬的一类证据。
TERMINATION_INTERPRETATION = {
    "OOMKilled": ("mixed", "容器被 cgroup OOM 杀死（内存超限），需区分宿主内存与 NPU 显存"),
    "Error": ("infra", "容器异常退出，常伴随节点驱逐/drain；需结合 exitCode 与节点事件"),
    "Completed": ("code", "容器正常退出（exit 0）但 job 判失败——失败发生在业务步骤逻辑内，非容器层"),
    "ContainerStatusUnknown": ("infra", "容器状态丢失，通常因节点失联或 pod 被强制删除"),
}

# 报告默认是**精简版**：正文只留结论与实时证据；完整证据（容器日志原文、全部历史匹配、
# 15 条局限）在同名 json 里，md 只给检索路径。下面两个函数是精简的两种基本手法 ——
# 把引用来的多行文本压成一行、把整段日志截成有信息量的样例。
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_WS_RE = re.compile(r"\s+")
# job 链接 → (owner/repo, job_id)：取全文的命令要能从 case 自己拼出来（case 未必带 repo 字段）
_GITHUB_JOB_LINK_RE = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/actions/runs/\d+/job/(\d+)")


def _oneline(text: str, limit: int = 120) -> str:
    """把**引用来的**多行文本压成单行，供 bullet 内联使用。

    为什么必须压：历史 issue 的正文自带 markdown 标题（实测 `#### 1. 路径映射不一致`），
    原样贴进报告会**冒充本报告的章节** —— 读者在编辑器大纲里看到它，会以为那是本工具的一节；
    多行内容还会把 bullet 列表撑断。换行按 ` / ` 接续（保留语义分隔），并剥掉标题前缀。
    """
    if not text:
        return ""
    # 先按行去掉标题前缀，再把换行折成 ` / `：先折行会把 `\n####` 变成句中片段、剥不掉
    lines = [_HEADING_RE.sub("", line) for line in text.splitlines()]
    text = _WS_RE.sub(" ", " / ".join(line.strip() for line in lines if line.strip())).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _sample_log_lines(text: str, sig: str = "", limit: int = 3) -> list[str]:
    """从整段容器日志里取 ≤limit 行**有信息量**的样例。

    优先取命中本 case 签名（`sig`，即命中桶正则命中的原文片段）的行：快照场景下容器还在跑，
    尾部全是 `HostContext: Well known directory` 这类 INFO 噪音，贴尾部几行等于没贴
    （实测 run 36658382517 的容器日志尾部 40 行全是这种行）。命中不足 limit 条时用尾部行补齐。
    """
    all_lines = text.splitlines()
    if not all_lines:
        return []
    sample: list[str] = []
    needles = [line.strip() for line in (sig or "").splitlines() if line.strip()]
    if needles:
        for line in all_lines:
            if any(needle in line for needle in needles):
                sample.append(line)
                if len(sample) >= limit:
                    return sample
    for line in all_lines[-limit:]:
        if len(sample) >= limit:
            break
        if line not in sample:
            sample.append(line)
    return sample[:limit]


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
        # 措辞必须说成「命中片段」而不是「首条异常」：这个字段是**命中桶正则的原文片段**，
        # 不是「第一条异常」。实测有 case 命中的是环境变量行（`HCCL_EXEC_TIMEOUT=204`），
        # 叫「异常」会把一行 INFO 读成报错，等于替一个假阳性作证。
        # 走 _oneline：日志片段可能多行，原样内联会把 bullet 列表撑断
        lines.append(f"  - 对端节点命中片段：`{_oneline(peer.get('sig') or '', 160)}`")
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
    basis, basis_tags, conflicts, hints_requiring_human = [], [], [], []

    def add(text: str, tag: str = "") -> None:
        """记一条判断依据，并打上去重标签。

        tag 对应**正文里已经渲染过**的那个段落（见 `_inline_rendered_tags`）：渲染层据此
        跳过重复句 —— 依据要能独立读懂，但不必把上面的集群/对端叙述再抄一遍。
        `basis` 本身仍是 list[str]，全量进 json，去重只发生在 md 上。
        """
        basis.append(text)
        basis_tags.append(tag)

    cluster = case.get("cluster") or {}
    pod_evidence = cluster.get("pod_evidence")
    availability = cluster.get("availability")
    history = case.get("history") or []
    peer_adopted = case.get("sig_source") == "对端节点日志"

    # --- 日志侧基线 ---
    if peer_adopted:
        # 桶是从对端节点兜底来的，首行必须说清「node0 没判出来、结论来自对端」，
        # 否则读者会以为 node0 的失败步骤时间窗本身就命中了这个桶（证据强度差一个档）。
        add(f"日志侧：node0 失败步骤时间窗**未能分类**，采用对端节点日志判桶 "
            f"→ 【{bucket}】，owner={log_owner}")
    elif bucket != "未分类":
        add(f"日志侧：命中桶【{bucket}】，owner={log_owner}")
    else:
        add("日志侧：未能分类（正则无命中），需人工或集群侧补足")
    # 对端节点证据紧跟日志侧基线：它属同一层（容器 stdout），只是机器不同
    for line in peer_basis_lines(case):
        add(line, "peer")

    # --- 集群侧证据 ---
    cluster_owner_votes = []
    pod_verdicts = interpret_pod_evidence(pod_evidence) if pod_evidence else []
    # 「按规则跳过」必须是一条**独立**的依据，不能落进下面「未解析出候选集群」那一支：
    # 后者说的是「想查但没查到集群」，与「日志已定性、规则上不必查」是两个相反的意思。
    if cluster.get("skipped"):
        add(f"集群侧：**按规则跳过** —— {cluster.get('skip_reason')}", "cluster.skip")
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
            add(f"集群侧：找到的 pod `{pod_evidence.get('pod')}` 与本 job **时序不符** —— "
                f"{cluster.get('window_note')}", "cluster.pod")
            add("  ⚠️ 该 pod 的容器状态与日志**不计入**本次归因（它属于同 scale-set 的"
                "另一次运行）；它只能证明该 runner 标签可用、集群本身在正常工作", "cluster.pod")
            hints_requiring_human.append(
                "集群侧命中的 pod 启动时间晚于本 job 的失败步骤，属同 scale-set 的另一次运行；"
                "本 job 的真实现场已回收，需靠 runner 侧日志或平台监控补足")
        else:
            for verdict in pod_verdicts:
                add(f"集群侧：{verdict['verdict']} —— {verdict['detail']}", "cluster.pod-verdict")
                cluster_owner_votes.append(verdict["owner"])
        if not foreign_pod and not pod_verdicts:
            if snapshot_pod_still_running(cluster, pod_evidence):
                # 快照取自 job 结束前：没有退出码是**时间点**造成的，不是「无异常」。
                # 说成「无异常终止记录」是反向结论 —— 它会让一个本该存疑的现场显得清白。
                add(f"集群侧：pod 已找到（phase={pod_evidence.get('phase')}，"
                    f"节点 `{pod_evidence.get('node')}`），但快照取自 job 结束前、"
                    f"容器仍在运行 —— 退出码与终止原因**尚未产生**，不能据此判「无异常」")
                hints_requiring_human.append(
                    "集群侧拿到的是失败时刻的进程内日志，没有退出码；"
                    "如需退出码必须在 job 结束的瞬间补查（实测窗口约 55s，随后 pod 即被回收）")
            else:
                add(f"集群侧：pod 已找到（phase={pod_evidence.get('phase')}，"
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
            add(f"集群侧：runner 标签 `{labels_text}` 在集群 `{cluster_label}` **确有** "
                f"{availability.get('runners_online')} 个 runner / "
                f"{availability.get('listeners')} 个 listener，但 pod 名用的后缀是 "
                f"{variants_text or '—'}，与 Cluster.md 登记的后缀 "
                f"`{availability.get('registered_suffix') or '—'}` **不一致** —— "
                f"即**标签族有效**，对不上的是登记后缀与实际命名（注册表问题），"
                f"**不是** runner 未上线", "cluster.availability")
            hints_requiring_human.append(
                f"Cluster.md 为该标签登记的集群后缀（`{availability.get('registered_suffix') or '—'}`）"
                f"与 pod 名实际后缀（{variants_text or '—'}）不一致；按登记全名匹配会得出"
                f"「无 runner 在线」的**假阴性**，登记信息需要修正")
        elif availability.get("available"):
            add(f"集群侧：runner 标签 `{labels_text}` 在集群 `{cluster_label}` 有 "
                f"{availability.get('runners_online')} 个 runner / "
                f"{availability.get('listeners')} 个 listener，标签本身有效", "cluster.availability")
        else:
            # 「没查到」**不计入 owner 票**：这是查询时刻的快照，证不了失败当时的状态。
            # 只作为提示，并强制人工确认——早先的实现把它当成 infra 的集群侧实证，
            # 会让一个无效的负向结果抬高归因置信度。
            # 措辞要交代**查过哪些匹配方式**：阴性结论的强度取决于查得多宽，
            # 只写「未查到」会让读者以为只按登记全名试过一次（那正是假阴性的来源）。
            scopes = "、".join(availability.get("scopes_checked") or ["全名"])
            add(f"集群侧：在候选集群中**未查到** runner 标签 `{labels_text}` 的任何 pod"
                f"（匹配方式：{scopes}，均 0 命中）", "cluster.availability")
            add("  ⚠️ 仅为查询时刻快照，**不能据此断言**失败当时 runner 掉线；"
                "官方分类树的「runs-on 标签不存在 / Runner 未上线」需另有失败时刻证据",
                "cluster.availability")
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
        add(f"历史先例：#{issue['number']}（score={strong_precedent['score']}，"
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
            add(f"集群侧与日志侧 owner 一致（{log_owner}），归因可信度提升")
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
    # 先例的 fix/prevention 同样是**引用来的正文**（自带标题、多行），走 _oneline 压成一行：
    # 建议是给人照着做的，带 `####` 的整段正文贴进来只会把这一节的层级搞乱。
    suggestions = list(knowledge.get("action") or [])
    if strong_precedent and strong_precedent["issue"]["sections"].get("fix"):
        suggestions.append(f"历史先例 #{strong_precedent['issue']['number']} 的修复记录："
                           f"{_oneline(strong_precedent['issue']['sections']['fix'], 200)}")
    if strong_precedent and strong_precedent["issue"]["sections"].get("prevention"):
        suggestions.append(f"历史先例 #{strong_precedent['issue']['number']} 的防复发建议："
                           f"{_oneline(strong_precedent['issue']['sections']['prevention'], 150)}")

    return {
        "root_cause": root_cause,
        "owner": owner,
        "owner_from_cluster": bool(cluster_owner_votes),
        "confidence": confidence,
        "basis": basis,
        # 与 basis 等长、逐条对应的去重标签（渲染层用，见 _inline_rendered_tags）。
        # basis 仍是 list[str]：json 与其他读者不受影响，去重只发生在 md 渲染这一步。
        "basis_tags": basis_tags,
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


def _job_log_command(case: dict) -> str | None:
    """本 job 控制台日志（含测试输出）的取回命令。

    报告默认不贴整段日志，那就必须给出**怎么把它拿回来**：这条命令是按 job_id 直接拉
    GitHub 侧原始日志（第 1 步分析的也是它）。repo/job_id 缺失时退回从 job 链接里取，
    链接不是 GitHub 形态（测试 fixture）则返回 None —— 宁可少一行，也不印一条假的命令。
    """
    repo, job_id = case.get("repo"), case.get("job_id")
    if not (repo and job_id):
        match = _GITHUB_JOB_LINK_RE.search(str(case.get("link") or ""))
        if match:
            repo, job_id = match.group(1), match.group(2)
    if not (repo and job_id):
        return None
    return f"`gh api repos/{repo}/actions/jobs/{job_id}/logs`"


def _log_retrieval_commands(case: dict, cluster: dict, pod_evidence: dict,
                            log_entry: dict) -> list:
    """「怎么把这段容器日志完整取回来」的命令列表（k8s 侧 + GitHub 侧）。

    集群侧那份用 kubectl 就能复现（kubeconfig 路径、namespace、pod、容器都在报告里）；
    GitHub 侧那份是 job 的控制台日志，两者是**不同的产物**，故都给。
    """
    commands = []
    kubeconfig = cluster.get("kubeconfig_path")
    # namespace 必须取 **pod 实际所在**的那个：`cluster["namespace"]` 是 Cluster.md 登记的
    # 项目共享 namespace（实测 `vllm-project`），而 runner pod 在仓库名派生的
    # `vllm-project-vllm-ascend` 里 —— 用登记名拼出来的命令照抄会 NotFound。
    pod = pod_evidence.get("pod")
    namespace = pod_evidence.get("namespace") or cluster.get("namespace")
    if kubeconfig and pod and namespace:
        command = (f"kubectl --kubeconfig {kubeconfig} logs {pod} -n {namespace} "
                   f"-c {log_entry.get('container')}")
        if log_entry.get("source") == "重启前的实例":
            command += " --previous"
        commands.append(f"`{command}`")
    job_log = _job_log_command(case)
    if job_log:
        commands.append(f"{job_log}（本 job 的控制台日志，含测试输出）")
    return commands


def placement_lines(cluster: dict, pod_evidence: dict) -> list:
    """pod 是**真实负载**还是 Liqo 影子对象 —— 直接回答「这个 job 到底跑在哪个集群」。

    为什么必须单独说一句：上面那行 `取证集群` 只说「**从**哪个集群查到了这个 pod」，
    不等于「负载跑在那儿」。实测 runner 标签登记在 cn12-001，而同一个 pod 从
    cn12-001 看是影子对象（node 为虚拟节点名 `mind-third-ci`），从 mind-third-ci 看
    才是真实负载（node 为真实节点）—— 只报「取证集群：cn12-001」会被读成
    「job 跑在 cn12-001」，这是本工具最容易误导人的一处。
    """
    placement = pod_evidence.get("placement")
    if not placement:                     # 旧快照没有这个字段：宁可不写，也不猜
        return []
    node = placement.get("node") or "—"
    if placement.get("shadow_pod") is True:
        hint = cluster.get("placement_hint")
        if hint:
            provider = (f"提供方集群 `{hint}`（按虚拟节点名与已登记集群名匹配推断，"
                        f"属命名约定，需人工确认）")
        else:
            provider = (f"提供方集群（虚拟节点名 `{placement.get('virtual_node') or node}` "
                        f"未能唯一对应到某个已登记集群，无法判定是哪一个）")
        return [f"- ⚠️ **真实负载不在本集群**：该 pod 带标签 `liqo.io/shadowPod=true`（Liqo 影子对象），"
                f"`node` 字段 `{node}` 是**虚拟节点名**，真实负载运行在{provider}。"
                f"本集群只是消费方 —— runner 标签登记在此，只说明**调度请求**发在此，"
                f"不说明负载跑在此（容器状态与日志是 Liqo 反射来的，仍属该 pod 本身）"]
    evidence_note = ("`liqo.io/shadowPod=false`" if placement.get("shadow_pod") is False
                     else "无 Liqo 影子标签")
    return [f"- 真实负载在**本集群**：pod {evidence_note}，`node` 字段 `{node}` 是真实节点名"]


def _inline_rendered_tags(case: dict) -> set:
    """本 case 在**正文段落里已经渲染过**的依据类别 —— 判断依据据此去重，同一句不说两遍。

    只在对应段落**确实会输出**时才算「已渲染」：这些 tag 各自对应下面某个渲染分支，
    分支不成立时依据里的那条就是独一份（例如集群段整段缺失时，依据里的集群叙述不能被删掉）。
    """
    cluster = case.get("cluster") or {}
    rendered = set()
    if cluster.get("skipped"):
        rendered.add("cluster.skip")
    pod_evidence = cluster.get("pod_evidence")
    if pod_evidence:
        if cluster.get("time_consistent") is False:
            rendered.add("cluster.pod")
        elif interpret_pod_evidence(pod_evidence):
            rendered.add("cluster.pod-verdict")
    availability = cluster.get("availability") or {}
    if availability.get("checked"):
        rendered.add("cluster.availability")
    if peer_basis_lines(case):
        rendered.add("peer")
    return rendered


def render_case(case: dict, index: int) -> list:
    """渲染单个失败 job 的完整取证过程。"""
    lines = []
    lines.append(f"### case {index}. {case.get('job_name')}　`{case.get('workflow')}`")
    lines.append("")
    # 抬头按「这是什么 job」与「跑在哪台机器、哪个时间窗」并成两行：
    # 四个 bullet 各占一行时，case 的开头要六行才读到证据
    lines.append(f"- 失败 job：{case.get('link')}"
                 f"　步骤：`{case.get('step') or '未知'}`"
                 f"　芯片：{case.get('chip') or 'CPU 门禁'}"
                 f"　runner 标签：`{', '.join(case.get('labels') or []) or '—'}`")
    window = case.get("window") or {}
    pod_line = f"- runner pod 名：`{case['runner_name']}`　" if case.get("runner_name") else "- "
    if window.get("started_at"):
        pod_line += (f"失败步骤时间窗：{window['started_at']} ~ "
                     f"{window.get('completed_at') or '(未完成)'}")
    if pod_line != "- ":
        lines.append(pod_line.rstrip("　"))
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
            lines.append("<details><summary>对端节点命中片段原文</summary>")
            lines.append("")
            lines.append("```")
            lines.append(_oneline(peer.get("sig") or "", 300))
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
    # 集群归属、实际查询的标签全名、定位方式并成一行：它们是「这次查的是谁」的三个侧面，
    # 分开写会各占一行而读者总要连着读。（标签全名必须如实记下：job 上报展示名、pod 名用
    # 带后缀全名，不写清楚「查无 pod」就会被误读成「runner 不在线」。）
    attribution = []
    if cluster.get("candidate_note"):
        attribution.append(f"集群归属：{cluster['candidate_note']}")
    if cluster.get("queried_labels"):
        attribution.append("实际查询的标签全名："
                           + "、".join(f"`{label}`" for label in cluster["queried_labels"]))
    if cluster.get("kubeconfig_path") and cluster.get("match_kind"):
        attribution.append(f"pod 定位方式：{cluster['match_kind']}")
    if attribution:
        lines.append("- " + "　".join(attribution))
    if cluster.get("kubeconfig_path"):
        identity = cluster.get("identity") or {}
        if identity.get("reachable"):
            reachability = (f"正常（server {identity.get('server_version')}，"
                            f"身份 `{identity.get('identity')}`）")
        else:
            reachability = f"**不可达** —— {identity.get('error')}"
        # namespace 写**两个**：登记的那个是查询口径，pod 实际所在的那个才是取证口径。
        # 实测登记 `vllm-project`、pod 在 `vllm-project-vllm-ascend` —— 只写前者会让人
        # 以为「登记的 namespace 里就有 runner pod」，取日志时也会找错地方。
        pod_namespace = (cluster.get("pod_evidence") or {}).get("namespace")
        registered_namespace = cluster.get("namespace")
        if pod_namespace and pod_namespace != registered_namespace:
            namespace_text = (f"pod 实际 namespace `{pod_namespace}`"
                              f"（登记 namespace `{registered_namespace}`）")
        else:
            namespace_text = f"namespace `{registered_namespace}`"
        lines.append(f"- 取证集群：`{cluster.get('cluster_name')}`"
                     f"（{namespace_text}，kubeconfig `{cluster.get('filename')}`）"
                     f"　连通性：{reachability}")
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
            lines.extend(placement_lines(cluster, pod_evidence))
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
            for log_index, log_entry in enumerate(logs):
                if not (log_entry.get("ok") and log_entry.get("text")):
                    continue
                # 默认**不贴**容器日志：实测尾部 40 行全是 `HostContext: Well known directory`
                # 这类 INFO（快照场景容器还在跑），贴了等于给报告灌水 45 行。改为给「怎么取全文」
                # 加一个有信息量的样例（优先命中本 case 签名的行）。全文在同名 json 里，不丢证据。
                sample = _sample_log_lines(log_entry["text"], case.get("sig") or "")
                sample_note = f"，样例 {len(sample)} 行" if sample else ""
                lines.append(f"- 容器 `{log_entry.get('container')}` 日志（{log_entry.get('source')}）："
                             f"共 {len(log_entry['text'].splitlines())} 行{sample_note}。"
                             f"本工具取的那份在 json `cases[{index - 1}].cluster.logs[{log_index}].text`")
                retrievals = _log_retrieval_commands(case, cluster, pod_evidence, log_entry)
                if retrievals:
                    lines.append(f"  - 取全文：{'；'.join(retrievals)}")
                if sample:
                    lines.append("")
                    lines.append("```")
                    lines.extend(sample)
                    lines.append("```")
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
    # 只展开前 2 条：历史匹配已按分数降序，第 3 名往后基本都是「只命中通用词或 workflow 名」
    # 的弱线索 —— 那正是 synthesize 明令**不采信**的一类（强先例已被它单独提升到判断依据里）。
    # 其余压成一行计数，完整匹配在同名 json 的 cases[i].history 里。
    shown = history[:2]
    if not history:
        lines.append("- 知识库中未找到相关历史 issue（签名与关键词均无命中）")
    else:
        for match in shown:
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
            # 不再输出「命中关键词」：实测是 `aarch64, linux, open, run` 这类通用词，
            # 既不能区分现象也不能支撑结论，只让读者以为匹配很强。签名与症状词才是机制证据。
            if issue["sections"].get("root_cause"):
                lines.append(f"  - 历史根因：{_oneline(issue['sections']['root_cause'], 120)}")
        rest = history[2:]
        if rest:
            best = max(rest, key=lambda item: item["score"])
            lines.append(f"- 另有 {len(rest)} 条弱命中（最高 #{best['issue']['number']} "
                         f"score={best['score']}）—— 完整匹配见 json `cases[{index - 1}].history`")
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
                lines.append(f"- #{item['number']}：为何关联 —— {_oneline(item.get('why') or '（未填）', 120)}")
                continue
            lines.append(f"- #{item['number']}（{item.get('state')}"
                         f"{'，根因在' + source if source else ''}）"
                         f"[{item['title'][:70]}]({item['url']})")
            if item.get("why"):
                lines.append(f"  - 为何关联：{_oneline(item['why'], 120)}")
            if item.get("root_cause"):
                lines.append(f"  - 历史根因：{_oneline(item['root_cause'], 120)}")
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
    # 去重：对端/pod 判定/标签可用性三类依据与上面的段落**逐句相同**（它们本就是同一批数据
    # 的两种呈现），这里不再抄一遍 —— 但要按 case 实际渲染了哪几段来判（见 _inline_rendered_tags），
    # 段落没渲染时依据里那条就是独一份，删掉就成丢证据了。basis 全量仍在 json 里。
    rendered_tags = _inline_rendered_tags(case)
    basis_items = verdict.get("basis") or []
    for position, item in enumerate(basis_items):
        tags = verdict.get("basis_tags") or []
        if position < len(tags) and tags[position] in rendered_tags:
            continue
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
    if meta.get("json_path"):
        # 本页是**精简版**：容器日志原文、全部历史匹配、全部局限条目都在同名 json 里。
        # 这条路径必须写在开头 —— 读者看到「全文见 json」时得知道去哪儿找。
        lines.append(f"- 完整证据：`{meta['json_path']}`"
                     f"（本页只留结论与实时证据；原文与未渲染的条目都在其中）")
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
    # 缺 kubeconfig 的集群不逐行占位：它们那四行的备注文字一模一样，只是集群名不同，
    # 逐行展开会把一张清单读成四句重复的话。改为表下列一行，信息不减。
    missing_kubeconfig = []
    for row in registry_plan:
        if not row["has_kubeconfig"]:
            missing_kubeconfig.append(row["cluster"])
            continue
        lines.append(f"| `{row['cluster']}` | ✅ | {row.get('server') or '—'} | {row.get('warning') or ''} |")
    lines.append("")
    if missing_kubeconfig:
        lines.append(f"- 无 kubeconfig 的集群 {len(missing_kubeconfig)} 个："
                     + "、".join(f"`{name}`" for name in missing_kubeconfig)
                     + "　→ 落到这些集群的失败做不了集群侧取证，只能依据日志侧结论")
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

    # 局限：只列**本次运行真的触发**的条目。15 条全量输出时每份报告逐字节相同、与本次结论
    # 多半无关，反而把该读的那几条淹掉；全量清单与触发判据在设计文档 §7，这里给一行指针。
    lines.append("## 能力边界与局限")
    lines.append("")
    all_limitations = integrity.get("limitations") or []
    shown = []
    for item in all_limitations:
        # 兼容纯字符串（旧调用方/测试直接塞文本的形态）：那种一律视为「本次相关」
        if isinstance(item, str):
            shown.append(item)
        elif item.get("triggered"):
            shown.append(item.get("text"))
    if shown:
        for item in shown:
            lines.append(f"- {item}")
    else:
        lines.append("- 本次运行未触发任何已知局限条目。")
    lines.append(f"- 局限共 {len(all_limitations)} 条（本页只列本次触发项）—— 完整清单与触发判据见 "
                 f"`npu_ci_forensics_design.md` §7")
    if integrity.get("errors"):
        lines.append("")
        lines.append("### 本次运行遇到的错误")
        lines.append("")
        for error in integrity["errors"]:
            lines.append(f"- {error}")
    return "\n".join(lines) + "\n"
