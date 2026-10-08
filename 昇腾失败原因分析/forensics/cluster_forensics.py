"""第 2 步：集群侧取证（借助 kubeconfig 排查现场）。

能力边界（实测自 SA `system:serviceaccount:ascend:sa-for-tangjia-*` 的 RBAC）：
  ✅ pods        : get, list        —— 可看 pod 状态/容器状态/所在节点
  ✅ pods/log    : get, watch       —— 可读容器日志
  ❌ nodes / events / namespaces / deployments —— 全部 Forbidden
  ⇒ 因此**拿不到**调度事件（FailedScheduling 的具体原因）、节点 condition、taint 列表。
     `kubectl describe pod` 的 Events 段会 403，本模块不用 describe，改用 `-o json` 读 status 字段。

取证分两条路，取决于失败 job 的 pod 是否还在：
  路径 A（pod 还在）：完整现场——容器状态、上次终止原因、日志、所在节点。
  路径 B（pod 已回收，绝大多数历史失败如此）：降级为**标签可用性核查**——
     查该 runner 标签对应的 scale-set/listener pod 是否存在。这能证实或证伪官方分类树里的
     `leaf_wait_label`（runs-on 标签不存在）与 `leaf_runner_offline`（runner 未上线），
     是历史失败唯一还能做的集群侧判断。

安全：只允许只读动词（get/logs/version/auth），变更类动词直接拒绝执行。
"""
from __future__ import annotations

import datetime
import json
import re
import subprocess

# 允许的 kubectl 子命令白名单（只读）。任何变更类操作一律拒绝。
READ_ONLY_VERBS = {"get", "logs", "version", "auth", "api-resources", "explain"}

# pod 名文法（实测）：<runner 标签>-<5位字母数字>-runner-<5位字母数字>，
# 每个 job 还常有一个 `<同名>-workflow` 伴生 pod 承载实际步骤容器。
POD_NAME_GRAMMAR = re.compile(r"^(?P<label>.+?)-(?P<hash>[a-z0-9]{5})-runner-(?P<suffix>[a-z0-9]{5})(?P<workflow>-workflow)?$")

# ARC scale-set 的 listener/controller 所在 namespace（实测）
ARC_NAMESPACES = ("arc-systems", "arc-system")

# Liqo 跨集群反射的判据（实测）：消费方集群里的 pod 带 liqo.io/shadowPod=true，
# 其 nodeName 是**虚拟节点名**；真实负载在提供方集群，同一个 pod 从提供方看没有该标签。
LIQO_SHADOW_LABEL = "liqo.io/shadowPod"
LIQO_API_SERVER_ANNOTATION = "liqo.io/api-server-support"

# 不保留的注解：kubectl 的 last-applied-configuration 是整份提交清单的副本，动辄数十 KB，
# 与本工具的结论无关；其余注解一律保留（每条截断 200 字），因为集群归属的判据就在注解里。
DROPPED_ANNOTATION_KEYS = ("kubectl.kubernetes.io/last-applied-configuration",)


def kubectl(kubeconfig_path: str, *args: str, timeout: int = 20) -> dict:
    """执行一条只读 kubectl，返回 {ok, stdout, stderr}。

    降级风格对齐 npu_ci_failure_analysis.py 的 gh()：失败不抛异常，返回 ok=False，
    由调用方决定如何记「未取证」。但比 gh() 多返回 stderr —— 集群取证里
    「403 权限不足」与「连接超时」是两种完全不同的结论，必须能区分。
    """
    if args and args[0] not in READ_ONLY_VERBS:
        return {"ok": False, "stdout": "", "stderr": f"拒绝执行非只读动词: {args[0]}"}
    try:
        proc = subprocess.run(
            ["kubectl", "--kubeconfig", kubeconfig_path, *args,
             f"--request-timeout={max(1, timeout - 4)}s"],
            capture_output=True, timeout=timeout,
        )
    except FileNotFoundError:
        return {"ok": False, "stdout": "", "stderr": "未找到 kubectl 可执行文件"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": f"kubectl 超时（{timeout}s）"}
    return {"ok": proc.returncode == 0,
            "stdout": proc.stdout.decode("utf-8", errors="ignore"),
            "stderr": proc.stderr.decode("utf-8", errors="ignore").strip()}


def check_connectivity(kubeconfig_path: str) -> dict:
    """连通性 + 身份探测。返回 {reachable, server_version, identity, error}。

    注意：`get ns` 被 Forbidden **也算连通**——说明认证通过、只是权限不足。
    只有连不上/超时/证书错才算不可达。
    """
    result = kubectl(kubeconfig_path, "version", "-o", "json")
    if not result["ok"] and "Forbidden" not in result["stderr"]:
        return {"reachable": False, "server_version": None, "identity": None,
                "error": result["stderr"] or "未知错误"}
    server_version = None
    if result["ok"]:
        try:
            payload = json.loads(result["stdout"])
            server_version = (payload.get("serverVersion") or {}).get("gitVersion")
        except Exception:
            pass
    # 用一次必然失败的集群级查询换取身份信息（Forbidden 报文里带 serviceaccount 全名）
    probe = kubectl(kubeconfig_path, "get", "namespaces")
    identity = None
    match = re.search(r'User "([^"]+)"', probe["stderr"])
    if match:
        identity = match.group(1)
    return {"reachable": True, "server_version": server_version, "identity": identity, "error": None}


def list_pods(kubeconfig_path: str, namespace: str | None = None) -> dict:
    """列出 pod。namespace 为空则 `-A`（实测 SA 有集群级 pod list 权限）。

    返回 {ok, pods: [...], error}。pods 为原始 pod dict 列表。
    """
    args = ["get", "pods", "-o", "json"]
    if namespace:
        args += ["-n", namespace]
    else:
        args += ["-A"]
    result = kubectl(kubeconfig_path, *args, timeout=30)
    if not result["ok"]:
        return {"ok": False, "pods": [], "error": result["stderr"]}
    try:
        return {"ok": True, "pods": json.loads(result["stdout"]).get("items", []), "error": None}
    except Exception as exc:
        return {"ok": False, "pods": [], "error": f"解析 pod JSON 失败: {exc}"}


def pod_name_of(pod: dict) -> str:
    return (pod.get("metadata") or {}).get("name", "")


def pod_namespace_of(pod: dict) -> str:
    return (pod.get("metadata") or {}).get("namespace", "")


def _parse_timestamp(value: str | None):
    """k8s 时间戳是 RFC3339，转成可比较的 datetime；失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


# 判定「pod 是否可能承载过本 job」允许的时钟偏差（秒）。
# 两套系统（GitHub / kubelet）的时钟不会完全一致，留一点余量。
# 实测校准：曾用 120s，结果放过了真·外来 pod —— 样例 job 的失败步骤在 09:37:19~09:37:21，
# 而那个 pod 启动于 09:38:53（晚 92s），120s 余量让它被判成「自洽」。压到 60s 才对它有判别力，
# 同时仍能容忍正常量级的时钟漂移。
POD_WINDOW_SLACK_SECONDS = 60


def _pod_start_reference(pod: dict) -> tuple:
    """取该 pod 可供比较的「存在起点」：优先 status.startTime，缺失时退到 creationTimestamp。

    为什么必须退：Pod 处于 Pending（还没被 kubelet 受理）时 `status.startTime` 是**空**的，
    而 `metadata.creationTimestamp` 由 API server 在创建时写入，永远存在。只看 startTime 会让
    「刚创建、尚未调度」的 pod 失去时间判据，于是「时间未知」被当成了「可能就是它」——
    实测 2026-09-24 的两例正是如此：12 分钟前失败的 job 匹配上了**刚刚才创建**的 Pending pod，
    而那个 pod 的 Unschedulable 条件（PVC 未绑定）读起来还挺像失败原因，极易被误读。
    返回 (datetime|None, 该时间的来源说明)。
    """
    status = pod.get("status") or {}
    started = _parse_timestamp(status.get("startTime"))
    if started is not None:
        return started, "容器启动时间(status.startTime)"
    created = _parse_timestamp((pod.get("metadata") or {}).get("creationTimestamp"))
    if created is not None:
        return created, "pod 创建时间(metadata.creationTimestamp)"
    return None, ""


def _pod_can_have_run_job(pod: dict) -> tuple:
    """该 pod 是否「真的跑起来过」。返回 (可否承载, 不可承载的原因)。

    必须在时间判据之前先过这一关：**从未启动的 pod 不可能承载一个已经结束的 job**，
    与时间无关。判据只有两条，都来自 pod 自身状态，不做推测：
      - phase=Pending（还没调度上）→ 不可能跑过 job；
      - 没有任何 containerStatuses（容器从未被创建）→ 同上。
    """
    status = pod.get("status") or {}
    phase = status.get("phase")
    if phase == "Pending":
        return False, "phase=Pending（尚未调度成功），不可能承载已结束的 job"
    if not (status.get("containerStatuses") or []):
        return False, "无任何容器状态（容器从未启动），不可能承载已结束的 job"
    return True, None


def _pod_window_check(pod: dict, started_at: str | None, completed_at: str | None) -> dict:
    """判断该 pod 的启动时间能否与 job 的时间窗自洽。

    为什么必须查：runner pod 是**复用**的——同一个 scale-set 的 pod 会连续接多个 job。
    实测样例：job 的失败步骤在 09:37:19~09:37:21，而按标签+时间窗「收敛」到的 pod
    启动于 09:38:53，日志里正在续租的是**另一个** job（b1a4807c…，有效至 09:51:13）。
    把这种 pod 的容器状态当成该 job 的现场证据，就是拿「另一次运行」的状态去解释本次失败。

    判据取「pod 必须在**失败步骤开始之前**就已存在」：这是承载该步骤的必要条件，
    时刻本身有确定含义，不依赖任何推测。反过来用「pod 启动不晚于 job 结束」是**错**的方向——
    job 结束后新建的 pod 照样满足它，判别力为零（实测 92s 的间隔就是这么溜过去的）。
    completed_at 只在拿不到步骤开始时间时退化为兜底判据。
    """
    pod_start, start_source = _pod_start_reference(pod)
    reference = _parse_timestamp(started_at)
    reference_label = "失败步骤开始时间"
    if reference is None:
        reference = _parse_timestamp(completed_at)
        reference_label = "job 结束时间（缺步骤时间，退化为兜底判据）"
    if pod_start is None or reference is None:
        return {"time_consistent": None, "window_note": None}
    if pod_start <= reference + datetime.timedelta(seconds=POD_WINDOW_SLACK_SECONDS):
        return {"time_consistent": True, "window_note": None}
    gap = int((pod_start - reference).total_seconds())
    return {
        "time_consistent": False,
        "window_note": (f"该 pod 的{start_source} {pod_start.isoformat()} 晚于{reference_label} "
                        f"{reference.isoformat()} 共 {gap} 秒 —— 承载本步骤的 pod 必须先于"
                        f"该步骤存在，故它承载的是**之后的另一次运行**，"
                        f"其容器状态与日志**不能**归因于本 job"),
    }


def find_job_pod(pods: list, runner_name: str | None, labels: list | None,
                 started_at: str | None = None, completed_at: str | None = None) -> dict:
    """在 pod 列表里定位失败 job 对应的 pod。

    按**证据强度降序**尝试，并把命中的方式记进 match_kind —— 因为「精确等于 runner_name」
    和「靠标签前缀猜的」可信度天差地别，报告里必须能区分，不能都写成「找到了 pod」。
    命中后一律附 time_consistent / window_note（见 _pod_window_check）。
    全部失败时返回 {pod: None, match_kind: None, reason: ...}，由调用方记「未取证」。

    ⚠️ **runner_name 一旦给出，标签猜测这条退路就被封死**：精确名三级都不命中即返回
    「未取证」，不再拿同标签的 pod 顶替（理由与实测代价见下面那道闸门处的注释）。
    标签匹配因此只在 job 压根没有 runner_name 时才生效。
    """
    by_name = {pod_name_of(pod): pod for pod in pods}
    candidates = []

    def _hit(pod, match_kind, reason=None, identity_based=False):
        window = _pod_window_check(pod, started_at, completed_at)
        # 精确名匹配时 pod 身份是**确凿**的（runner 名带 5 位随机段，不会被复用），
        # 此时时间戳对不上只说明时间戳本身可疑，不足以否定身份 —— 不能反过来把
        # 最硬的证据当「外来 pod」丢掉。故降级为「时间戳存疑」提示，仍按精确匹配采信。
        if identity_based and window["time_consistent"] is False:
            window = {"time_consistent": True,
                      "window_note": ("⚠️ 时间戳存疑：" + window["window_note"]
                                      + "；但 pod 名与本 job 的 runner_name 精确一致，"
                                        "身份确凿，故仍按精确匹配采信")}
        return {"pod": pod, "match_kind": match_kind, "reason": reason, **window}

    # 证据强度 1：pod 名精确等于 GitHub 报的 runner_name
    if runner_name and runner_name in by_name:
        return _hit(by_name[runner_name], "runner_name 精确匹配", identity_based=True)
    # 证据强度 2：runner_name 的 workflow 伴生 pod
    if runner_name and f"{runner_name}-workflow" in by_name:
        return _hit(by_name[f"{runner_name}-workflow"], "runner_name + -workflow",
                    identity_based=True)
    # 证据强度 3：以 runner_name 为前缀（后缀段可能被截断）
    if runner_name:
        for name, pod in by_name.items():
            if name.startswith(runner_name):
                return _hit(pod, "runner_name 前缀匹配", identity_based=True)

    # 身份已知却三级都没命中 → **到此为止**，不许再退到下面的标签猜测。
    # 为什么这道闸门是必须的：pod 名是**精确**身份（runner 名带 5 位随机段，与 pod 名逐字相等，
    # 实测 161 份快照里只要 pod 还在就 100% 精确命中）。精确名查不到只说明一件事 ——
    # 本 job 的 pod 已被回收；而**同一 scale-set 上并发的其它 job** 的 pod 会照样满足标签匹配与
    # 时间窗（它先于本 job 的失败步骤启动），于是被当成本 job 的现场证据。
    # 实测代价：161 份快照里 19 份正是这样取错了 pod。最典型的一例 ——
    #   job 110701473809 的 runner_name = …-26v84-runner-wddd8（已回收）
    #   标签匹配选中同 run 内**另一个成功 job**(110701477428) 的 pod …-runner-5j29n
    #   报告于是把 5j29n 的 node（mind-third-ci）与容器日志当成该失败 job 的现场。
    # 这一类错误的危害是「读者据此下结论」，比「未取证」严重得多，故宁可少取证也不能猜。
    # 例外只在 runner_name **缺失**时成立（job 未被分配 runner）：那时标签匹配是唯一的线索，
    # 才继续往下走。
    if runner_name:
        return {"pod": None, "match_kind": None, "time_consistent": None, "window_note": None,
                "informative": True,
                "reason": (f"本 job 的 runner_name 精确名（{runner_name}）在候选集群中不存在 —— "
                           f"该 pod 已被回收；同标签的现存 pod 均属并发的**其它** job，"
                           f"不构成本 job 的现场证据")}

    # 证据强度 4：按 pod 名文法拆出 runner 标签，与 job 的 labels 求交
    # （仅在 job 没有 runner_name 时才会走到这里）
    wanted = set(labels or [])
    if wanted:
        for pod in pods:
            grammar = POD_NAME_GRAMMAR.match(pod_name_of(pod))
            if grammar and grammar.group("label") in wanted:
                candidates.append((pod, grammar))
        if candidates:
            # 这一档是**靠标签猜**的，证据标准必须是「有正面佐证」（time_consistent 为 True），
            # 而不是「没被排除」：
            #   ① 先剔除从未启动的 pod（Pending / 无容器状态）——它不可能承载已结束的 job；
            #   ② 再剔除起点晚于失败步骤的 pod —— 那是之后的另一次运行；
            #   ③ 起点判不出来的（缺两种时间戳）也不收——「不知道」不等于「可以采信」。
            # 实测①②两类都踩过：晚 92s 的复用 pod、以及 12 分钟前失败的 job 匹配上刚创建的
            # Pending pod（其 Unschedulable 条件看着还挺像原因）。见 _pod_window_check。
            plausible, late_pods, unstarted, undecidable = [], [], 0, 0
            for pod, grammar in candidates:
                can_run, _why_not = _pod_can_have_run_job(pod)
                if not can_run:
                    unstarted += 1
                    continue
                window = _pod_window_check(pod, started_at, completed_at)
                if window.get("time_consistent") is False:
                    late_pods.append(pod)
                    continue
                if window.get("time_consistent") is not True:
                    undecidable += 1
                    continue
                plausible.append((pod, grammar))
            rejected_notes = []
            if late_pods:
                rejected_notes.append(f"{len(late_pods)} 个起点晚于本 job 失败步骤的")
            if unstarted:
                rejected_notes.append(f"{unstarted} 个尚未启动的")
            if undecidable:
                rejected_notes.append(f"{undecidable} 个起点无法判定的")
            rejected_note = f"，剔除 {'、'.join(rejected_notes)}" if rejected_notes else ""
            if not plausible:
                # 「全部被排除」与「全部判不出来」是两回事，措辞不能混：
                # 前者能断言现场已回收，后者只能如实说未取证。
                if late_pods or unstarted:
                    reason = (f"同标签有 {len(candidates)} 个候选 pod，但**全部**不构成本 job "
                              f"的现场证据（{'、'.join(rejected_notes)}），故本 job 的真实现场已回收")
                else:
                    reason = (f"同标签有 {len(candidates)} 个候选 pod，但均无法判定"
                              f"是否承载过本 job（缺可信的时间判据），未取证")
                return {"pod": None, "match_kind": None, "time_consistent": None,
                        "window_note": None, "informative": True, "reason": reason}
            if len(plausible) == 1:
                return _hit(plausible[0][0], "runner 标签唯一匹配",
                            reason=(f"同标签共 {len(candidates)} 个 pod{rejected_note}"
                                    if rejected_notes else None))
            # 多个可用候选：用 job 的时间窗收敛
            window_start = _parse_timestamp(started_at)
            scored = []
            for pod, _ in plausible:
                pod_start, _source = _pod_start_reference(pod)
                if pod_start is None or window_start is None:
                    continue
                # 起点在失败步骤之前的优先（runner pod 先起、步骤后跑），其次按时间接近程度
                before_step = pod_start <= window_start
                scored.append((not before_step,
                               abs((pod_start - window_start).total_seconds()), pod))
            if scored:
                scored.sort(key=lambda item: (item[0], item[1]))
                note = f"同标签候选 {len(candidates)} 个，已按时间窗收敛{rejected_note}"
                return _hit(scored[0][2], "runner 标签 + 时间窗收敛", reason=note)
            return {"pod": None, "match_kind": None, "time_consistent": None,
                    "window_note": None, "informative": True,
                    "reason": f"同标签有 {len(plausible)} 个候选 pod 且时间窗无法收敛"}
    return {"pod": None, "match_kind": None, "time_consistent": None, "window_note": None,
            "reason": "pod 已回收（历史失败的常态）或未在本集群找到"}


def container_evidence(pod: dict) -> list:
    """提取容器级状态：这是集群侧最有价值的证据。

    lastState.terminated.reason 能区分三件在日志里长得一样的事：
      OOMKilled（内存） / 137+Error（可能被 drain 杀） / Completed（正常退出但 job 判失败）
    """
    findings = []
    for status in (pod.get("status") or {}).get("containerStatuses") or []:
        entry = {"container": status.get("name"), "ready": status.get("ready"),
                 "restart_count": status.get("restartCount"),
                 "image": status.get("image")}
        state = status.get("state") or {}
        for key in ("waiting", "running", "terminated"):
            if key in state:
                detail = state[key] or {}
                entry["state"] = key
                entry["state_reason"] = detail.get("reason")
                entry["exit_code"] = detail.get("exitCode")
                entry["signal"] = detail.get("signal")
                entry["message"] = (detail.get("message") or "")[:300]
                entry["started_at"] = detail.get("startedAt")
                entry["finished_at"] = detail.get("finishedAt")
        last = (status.get("lastState") or {}).get("terminated")
        if last:
            entry["last_terminated_reason"] = last.get("reason")
            entry["last_terminated_exit_code"] = last.get("exitCode")
            entry["last_terminated_message"] = (last.get("message") or "")[:300]
        findings.append(entry)
    return findings


def init_container_evidence(pod: dict) -> list:
    """init 容器失败常是「pod 根本没起来」的真因（镜像/权限/挂载），单独提取。"""
    findings = []
    for status in (pod.get("status") or {}).get("initContainerStatuses") or []:
        state = status.get("state") or {}
        entry = {"container": status.get("name"), "image": status.get("image")}
        for key in ("waiting", "terminated"):
            if key in state:
                detail = state[key] or {}
                entry["state"] = key
                entry["reason"] = detail.get("reason")
                entry["exit_code"] = detail.get("exitCode")
                entry["message"] = (detail.get("message") or "")[:300]
        if entry.get("reason"):
            findings.append(entry)
    return findings


# 以**精确身份**命中 pod 的三档定位方式（见 find_job_pod）；其余都是靠标签猜的推定
IDENTITY_MATCH_KINDS = ("runner_name 精确匹配", "runner_name + -workflow",
                        "runner_name 前缀匹配")


def foreign_pod_reason(pod_evidence: dict | None, match_kind: str | None,
                       runner_name: str | None) -> str | None:
    """快照里的 pod 是不是「同标签的**别的** job 的 pod」—— 事后可判定的假证据，返回原因。

    为什么要在**读取**快照时再判一次：修复前抢下的快照已经落在盘上，里面装着推定出来的
    错 pod（实测 19 份）。只修 find_job_pod 只能挡住新快照，这些旧快照被重扫时照样会把
    别人的 node / 容器日志当成本次失败的现场。快照里同时存着 job 的 runner_name 与选中的
    pod 名，判据是现成的，无需再查集群。

    判据：定位方式不是精确身份三档，且 pod 名与 runner_name 不是同一个 runner 段
    （`runner_name + "-workflow"` 是伴生 pod，故用前缀判）。
    """
    if not pod_evidence or not runner_name:
        return None
    if match_kind in IDENTITY_MATCH_KINDS:
        return None
    pod_name = pod_evidence.get("pod") or ""
    if pod_name == runner_name or pod_name.startswith(runner_name + "-"):
        return None
    return (f"快照里的 pod `{pod_name}` 不是本 job 的现场：它按「{match_kind}」推定而来，"
            f"而本 job 的 runner_name 精确名是 `{runner_name}`（两者是不同的 runner 段，"
            f"runner 名带 5 位随机段、不会被复用）—— 属并发的**其它** job，"
            f"其容器状态与日志已作废，不作为本 job 的证据")


def pod_placement(pod: dict) -> dict:
    """这个 pod 是**真实负载**还是 Liqo 影子对象 —— 决定「job 实际跑在哪个集群」。

    为什么必须判：运行集群**不能**由 runner 标签推出来。实测同一批标签
    （`linux-aarch64-a3-800i-16-cn12-001`）的 pod 能同时从 aiframework / cn12-001 /
    mind-third-ci 三个 kubeconfig 看到 —— Liqo 把虚拟节点上的 pod 反射进了共享 namespace。
    决定性判据是标签 `liqo.io/shadowPod`（实测同一个 pod `…-26v84-runner-l75ch`）：
        从 cn12-001 看：nodeName=`mind-third-ci`（**虚拟节点名**），shadowPod=true   → 影子对象
        从 mind-third-ci 看：nodeName=`192.168.0.181`（真实节点），无该标签        → 真实负载
    即真实负载跑在提供方集群 mind-third-ci，cn12-001 只是消费方。两者的容器状态是**一致**的
    （Liqo 反射，实测 startTime 与容器状态逐字相同），故日志与容器状态仍可用，
    但**集群归属不能算在消费方头上** —— 只报 nodeName 会让读者把虚拟节点名当成真实节点。

    shadow_pod 取三态：None 表示标签缺失（非 Liqo 集群，或 pod 尚未被反射）。
    """
    labels = (pod.get("metadata") or {}).get("labels") or {}
    annotations = (pod.get("metadata") or {}).get("annotations") or {}
    raw = str(labels.get(LIQO_SHADOW_LABEL, "")).strip().lower()
    shadow = True if raw == "true" else (False if raw == "false" else None)
    node = (pod.get("spec") or {}).get("nodeName")
    return {
        "shadow_pod": shadow,
        # 「虚拟节点」只在影子对象上才成立：nodeName 取自提供方集群名，不是真实节点
        "virtual_node": node if shadow else None,
        "node": node,
        "liqo_api_server_support": annotations.get(LIQO_API_SERVER_ANNOTATION),
    }


def pod_evidence(pod: dict) -> dict:
    """汇总一个 pod 的全部可用证据（不含日志）。"""
    status = pod.get("status") or {}
    spec = pod.get("spec") or {}
    metadata = pod.get("metadata") or {}
    conditions = [{"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason"),
                   "message": (c.get("message") or "")[:200]}
                  for c in status.get("conditions") or [] if c.get("status") != "True"]
    return {
        "pod": pod_name_of(pod),
        "namespace": pod_namespace_of(pod),
        "phase": status.get("phase"),
        "node": spec.get("nodeName"),
        # start_time 在 pod 还没被调度（Pending）时是空的；created_time 永远有，
        # 两者都报出来，读者才能自己看出「这个 pod 是什么时候存在的」
        "start_time": status.get("startTime"),
        "created_time": metadata.get("creationTimestamp"),
        "qos_class": status.get("qosClass"),
        "reason": status.get("reason"),
        "message": (status.get("message") or "")[:300],
        # labels/annotations 必须留着：集群归属的**唯一**判据就在 `liqo.io/shadowPod`
        # 这个标签里（见 pod_placement）。早先只留 node/phase 等字段，等于把判据丢掉，
        # 报告只能给出「node=mind-third-ci」这种读者无法解释的组合。
        "labels": dict(metadata.get("labels") or {}),
        "annotations": {key: (value or "")[:200]
                        for key, value in (metadata.get("annotations") or {}).items()
                        if key not in DROPPED_ANNOTATION_KEYS},
        "placement": pod_placement(pod),
        "containers": container_evidence(pod),
        "init_containers": init_container_evidence(pod),
        # 非 True 的 condition（如 PodScheduled=False / Ready=False）是 Pending 类失败的关键
        "abnormal_conditions": conditions,
    }


def pod_logs(kubeconfig_path: str, namespace: str, pod_name: str,
             container: str | None = None, previous: bool = False,
             since_time: str | None = None, tail: int = 400) -> dict:
    """读容器日志。返回 {ok, text, error}。

    previous=True 读**上一个**容器实例的日志——容器崩溃重启后，当前实例日志是空的，
    真错误在上一个实例里。这是 pod 侧取证最容易漏的一步。
    """
    args = ["logs", pod_name, "-n", namespace, f"--tail={tail}"]
    if container:
        args += ["-c", container]
    if previous:
        args += ["--previous"]
    if since_time:
        args += [f"--since-time={since_time}"]
    result = kubectl(kubeconfig_path, *args, timeout=30)
    if not result["ok"]:
        return {"ok": False, "text": "", "error": result["stderr"][:300]}
    return {"ok": True, "text": result["stdout"], "error": None}


# 弱匹配（标签主干）时名字里必须带的标记：本函数的结论是「runner/listener 是否在线」，
# 而主干前缀比登记全名宽得多，不加这道门会把同前缀的其它工作负载算成 runner 在线。
RUNNER_MARKERS = ("runner", "listener")


def _pod_hits(pods: list, prefix: str, require_runner_marker: bool = False) -> list:
    """名字以 prefix 开头的 pod，返回 [(pod 名, namespace)]。

    prefix 由调用方带上结尾 `-`，可避免 `…-800i-2` 误匹配 `…-800i-20` 这类前缀污染。
    """
    hits = []
    for pod in pods:
        name = pod_name_of(pod)
        if not name.startswith(prefix):
            continue
        if require_runner_marker and not any(marker in name for marker in RUNNER_MARKERS):
            continue
        hits.append((name, pod_namespace_of(pod)))
    return hits


def _shape(label: str, hits: list, match_kind):
    """把命中列表整理成核查结果，命中数与旧实现口径一致（全名匹配时行为不变）。"""
    listeners = [name for name, _ in hits if "listener" in name]
    runners = [name for name, _ in hits if "runner" in name]
    return {
        "available": len(hits) > 0,
        "checked": True,
        "reason": None,
        # 明确标注「仅快照」：消费方不得据此断言失败时刻的 runner 状态
        "snapshot_only": True,
        "matched_pods": len(hits),
        "runners_online": len(runners),
        "listeners": len(listeners),
        "namespaces": sorted({namespace for _, namespace in hits}),
        "samples": sorted(name for name, _ in hits)[:5],
        "match_kind": match_kind if hits else None,
        "claimed_label": label,
    }


def check_runner_availability(kubeconfig_path: str, runner_label: str, base_labels=()) -> dict:
    """路径 B 的核心：该 runner 标签对应的 scale-set / listener 是否存在于本集群。

    用来证实或证伪官方分类树的 `leaf_wait_label`（标签不存在）与
    `leaf_runner_offline`（runner 未上线）—— 这是历史失败唯一还能做的集群侧判断。

    判据：存在名字以该标签开头的 pod（scale-set listener 或任何 runner pod）。
    listener 在 arc-systems namespace、runner pod 在业务 namespace，故用 -A 全量看。

    ## 两级匹配（2026-09-28 实测后新增）

    实测（`ascend-cn12-001-cluster`，同一时刻，同一份 `-A` pod 列表）：
        Cluster.md 登记的全名 `linux-aarch64-a3-800t-0-cn12-001` 前缀匹配 → **0 个**
        标签主干           `linux-aarch64-a3-800t-0-`            → **6 个**
            （4 个 `…-chlqk-runner-*` + 2 个 `…-{8位hex}-listener`）
    也就是说该标签族的 runner 当时**正在线**，而只按登记全名匹配的实现会得出
    「该标签此刻在本集群无 runner/listener」——**与事实相反的假阴性**。
    根因是 Cluster.md 登记的后缀（`cn12-001`）与 pod 名实际用的后缀（`chlqk`）不一致，
    属于**注册表对不上实际命名**，不是 runner 掉线；报告若把它写成后者，会把归因
    推向 `leaf_wait_label` / `leaf_runner_offline`。

    故分两级：
      1. **全名匹配（强证据）**：以 `runner_label + "-"` 为前缀。命中即停，返回的
         matched_pods / runners_online / listeners 与旧实现**完全一致**（登记正确时不改变任何口径）。
      2. **标签主干匹配（弱证据）**：仅在①0 命中时进行，以 `base_labels`（job 上报的展示名）
         加 `-` 为前缀。前缀变宽必然引入污染，故要求名字里带 runner/listener 标记；
         且必须把实测后缀变体（suffix_variants）与登记后缀（registered_suffix）一并带出，
         让读者看到的是「后缀对不上」而不是一个孤零零的布尔值。

    `base_labels` 缺省时不进行第二级（老调用方与既有测试的口径不变）。

    ⚠️ **时间语义**：这是**查询时刻**的快照，不是失败时刻的快照。
    查到 available=True 不能证明失败当时 runner 在线；查到 False 也不能证明当时掉线。
    故结论只能用作「标签是否存在于本集群」的佐证，判断「当时是否掉线」必须另有失败时刻的证据
    （如 runner 侧日志时间戳、平台监控）。snapshot_only 字段即为此设，报告须据此措辞。
    """
    bases = [base for base in dict.fromkeys(base_labels or []) if base and base != runner_label]
    registered_suffix = next((runner_label[len(base) + 1:] for base in bases
                              if runner_label.startswith(base + "-")), None)
    # **实际执行过**的匹配方式（不是「可以查哪些」）：阴性结论的可信度取决于查得多宽，
    # 若写成静态清单，全名一命中就停了却仍记着「查过主干」，那是假留痕。
    scopes_checked = ["全名"]
    result = list_pods(kubeconfig_path)
    if not result["ok"]:
        return {"available": False, "checked": False,
                "reason": f"无法列举 pod: {result['error'][:200]}",
                "claimed_label": runner_label}
    pods = result["pods"]

    # 第 1 级：登记全名。命中即停 —— 后一级只是给「注册表对不上」兜底，不是常态。
    hits = _pod_hits(pods, runner_label + "-")
    match_kind = "全名"
    suffix_variants: dict = {}
    if not hits and bases:
        # 第 2 级：标签主干。逐个主干取并集（一个展示名可能对应多个登记全名）。
        scopes_checked.append("标签主干")
        for base in bases:
            for hit in _pod_hits(pods, base + "-", require_runner_marker=True):
                if hit not in hits:
                    hits.append(hit)
        match_kind = "标签主干"
        # 每个命中按**最长**匹配主干切后缀，避免两个主干同时命中时把同一段数两遍
        for name, _ in hits:
            prefix = max((base + "-" for base in bases if name.startswith(base + "-")),
                         key=len, default="")
            segment = name[len(prefix):].split("-")[0] or "?"
            suffix_variants[segment] = suffix_variants.get(segment, 0) + 1

    outcome = _shape(runner_label, hits, match_kind)
    outcome.update({
        # 实际查过的匹配方式：阴性结论必须能证明「两级都查了」，否则无法与「只查了全名」区分
        "scopes_checked": scopes_checked,
        "base_labels": bases,
        "registered_suffix": registered_suffix,
        # 仅弱匹配有值：实测后缀变体 → 命中数
        "suffix_variants": suffix_variants,
    })
    return outcome


def summarize_pods(pods: list) -> dict:
    """把 pod 列表汇总成健康快照。

    与 cluster_health() 分离，是为了让调用方能复用**已缓存的** pod 列表——
    `get pods -A` 是本模块最重的调用，健康快照不该为此再拉一次。
    """
    by_phase: dict = {}
    not_ready, pending_like, restarts = [], [], []
    for pod in pods:
        name = pod_name_of(pod)
        status = pod.get("status") or {}
        phase = status.get("phase") or "Unknown"
        by_phase[phase] = by_phase.get(phase, 0) + 1
        counts = [c.get("ready") for c in status.get("containerStatuses") or []]
        ready = sum(1 for flag in counts if flag)
        if counts and ready < len(counts):
            not_ready.append(f"{name} ({ready}/{len(counts)})")
        if phase in ("Pending", "Failed", "Unknown"):
            pending_like.append(f"{name} [{phase}] {status.get('reason') or ''}".strip())
        total_restarts = sum(c.get("restartCount") or 0 for c in status.get("containerStatuses") or [])
        if total_restarts:
            restarts.append(f"{name} (restarts={total_restarts})")
    return {
        "available": True, "reason": None,
        "pod_total": len(pods),
        "by_phase": by_phase,
        "not_ready": sorted(not_ready)[:10],
        "not_ready_count": len(not_ready),
        "abnormal_pods": sorted(pending_like)[:10],
        "restarted_pods": sorted(restarts)[:10],
    }


def cluster_health(kubeconfig_path: str, namespace: str | None = None) -> dict:
    """集群/namespace 健康快照。用于给 infra 类归因提供「集群此刻是否异常」的旁证。

    ⚠️ 与标签可用性核查同样只是**查询时刻**的快照，不是失败时刻的状态。
    能说明「该集群当前有 N 个 pod 未就绪」，不能说明「失败当时就是这样」。
    """
    result = list_pods(kubeconfig_path, namespace)
    if not result["ok"]:
        return {"available": False, "reason": f"无法列举 pod: {result['error'][:200]}"}
    return summarize_pods(result["pods"])
