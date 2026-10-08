"""多节点 job 的**对端节点**日志：GitHub Actions `*-ascend-logs` 产物。

为什么需要这条证据源（实测 job 109264350421，run 36518916532，2026-09-29）：
  `gh api repos/…/actions/jobs/{id}/logs` 回的**只有 node0 一台机器**的容器 stdout。
  多节点 job 里其余机器（node1..nodeN）的日志**只**存在于 workflow 上传的
  `<分支>-<yaml stem>-ascend-logs` 产物里。该 case 的 node0 是 TCPStore 服务端，只说得出
  「8 个 rank 里有 1 个没连上」：
      torch.distributed.DistStoreError: Timed out after 1801 seconds waiting for clients.
      7/8 clients joined.
  而「是谁没连上、从哪台机器连不上」只在 node1 的日志里（实测该文件 839 行，其中
  `TCPStore.cpp:138 recvValueWithTimeout failed` 与 `DistNetworkError: Failed to recv,
  got 0 bytes` 这两行在 job log 里 grep 一行都没有）。

本模块的分工：**只负责取与切**（产物 → 各节点文本），判桶仍由第 1 步的 `classify_text` 做，
采用策略由 `adopt_peer_bucket` 明示。这样本模块可以脱离全模块级执行的
`npu_ci_failure_analysis.py` 被直接 import 测试。

实测事实（2026-09-29，run 36518916532 的 14 个产物，是下列实现的依据，不是推测）：
  - 产物命名 `<分支>-<yaml stem>-ascend-logs`；同 run 还有 `nightly-a3` 这类无关产物，
    故必须按命名规则匹配，不能「随便取一个」。
  - 同 run 的 job 名会被 GitHub 从**中间**截断成 `…te... / GLM-5.1-W8A8C8-A3_128k_90_50.yaml`，
    但**尾部完整** —— yaml 名在 ` / ` 之后的段里取。⚠️ 实测 42 个 job（含 19 个多节点
    matrix job）里 ` / ` 最多只出现 **1 次**，所以「取第一个」与「取最后一个」在当前数据上
    **结果相同、无法区分**；本模块取最后一个，是**防御性**选择（一旦 job 名里再出现一个
    ` / `，只有取最后一个才不会拿到半截路径）。见 `tests/test_peer_logs.py` 的构造样例。
  - 产物是 zip，内层是 `ascend-logs.tar.gz`，再内层是
    `collected-logs/node{N}/var/log/<pod>_logs.txt`（容器 stdout）与
    `collected-logs/node{N}/root/ascend/log/…`（昇腾设备日志，本模块不取）。
  - ⚠️ **「产物存在但为空」是常态**：实测 415B 的产物内层 tar 只有 260B，且**只有目录项、
    零个常规文件**，而目录里照样列着 node0..node3。故：
    ① 判空只能按「常规文件数为 0」，不能按文件大小、也不能按有没有 node 目录；
    ② 节点数只能按 `*_logs.txt` 的出现来数，否则会对一个空产物报出「4 个节点」。
"""
from __future__ import annotations

import io
import json
import os
import re
import tarfile
import zipfile

# 多节点 job 的命名前缀。实测 run 36518916532 的 14 个产物**全部**来自这两类 job；
# 单节点 job 的输出本就完整地落在 job log 里，取产物没有增量，故不取（`--no-peer-logs` 之外
# 的又一层成本控制）。
MULTI_NODE_JOB_RE = re.compile(r'^(?:multi-node|double-node)\s*\(')

# 产物名后缀。构造式：`<分支>-<yaml stem>-ascend-logs`
ARTIFACT_SUFFIX = "-ascend-logs"
ARTIFACT_PREFERRED_PREFIX = "main-"

# 内层 tar 的名字（workflow 侧固定）
INNER_TARBALL_NAME = "ascend-logs.tar.gz"

# 只取容器的 stdout/stderr：这是与 job log 同一来源、可与之对读的文本。
# 昇腾设备日志（`root/ascend/log/…`）属另一类证据，体量大且行格式不同，本模块不取。
NODE_LOG_MEMBER_RE = re.compile(r'^collected-logs/(node\d+)/var/log/[^/]+_logs\.txt$')

# job log 覆盖的那个节点。实测：job 109264350421 的 job log（2239 行）与产物里 node0 的
# `…-0_logs.txt`（2239 行）是同一份容器输出；故「对端」= node0 之外的节点。
PRIMARY_NODE_INDEX = 0

# 单个成员文件的大小上限：超过则记 oversize 跳过，避免为一个异常产物把内存吃满
# （实测最大的一份产物 zip 5.1MB / 内层 6.1MB / 单文件远小于此）
MAX_MEMBER_BYTES = 8 * 1024 * 1024

# 「该 run 有哪些产物」的进程内备忘：(repo, run_id) → artifacts。
# 同一 run 里常有多台 job 同时失败（实测 run 36518916532 里 20 个 job），逐 job 重列一遍
# 全是重复 API 调用；失败结果**不**入备忘，免得一次限流把整轮分析钉死。
_ARTIFACT_LISTING_CACHE: dict = {}


def is_multi_node_job(job_name: str) -> bool:
    """该 job 是否是「日志只覆盖 node0」的多节点 job。"""
    return bool(MULTI_NODE_JOB_RE.match(job_name or ""))


def artifact_stem_for_job(job_name: str) -> str | None:
    """从 job 名取出产物名里的 yaml stem；取不到返回 None。

    job 名形如 `double-node (main, multi-node-GLM-5.1-W8A8C8-A3_128k_90_50,
    GLM-5.1-W8A8C8-A3_128k_90_50.yaml, te... / GLM-5.1-W8A8C8-A3_128k_90_50.yaml`：
    GitHub 会把中间截断（甚至截断出 `…te... /`），yaml 名在 ` / ` 之后的段里。

    取**最后一个** ` / ` 之后的段：⚠️ 这是防御性选择而非实测定论 —— 实测 42 个 job 名里
    ` / ` 最多出现 1 次，取第一个与取最后一个**结果相同**；写成最后一个，是为了万一 job 名里
    再出现一个 ` / `（matrix 值本身含 ` / `、或上层 workflow/job 名带 ` / `）时不拿到半截路径。

    没有 ` / ` 分隔符的直接判无产物：19 个多节点 matrix job 的展示名**全部**带 ` / <yaml>`，
    缺了它说明这不是同一套命名，此时整串文字里任何一个 `.yaml` 结尾的子串都可能是巧合，
    宁可判「无产物」也不去撞一个不相干的产物名。
    """
    if not job_name or " / " not in job_name:
        return None
    tail = job_name.rsplit(" / ", 1)[-1].strip()
    if not tail.endswith(".yaml"):
        # 例如被跳过 job 的尾段是 `inputs.config_file_path` —— 不是 yaml 名，判定无产物
        return None
    stem = tail[: -len(".yaml")]
    return stem or None


def match_artifact(artifact_names, stem: str | None) -> str | None:
    """在产物名列表中按 stem 精确匹配；取不到返回 None。

    用 `endswith(f"-{stem}-ascend-logs")` 而不是「包含 stem」：后者在
    `GLM5_2-W8A8-A3-dual-nodes` 与 `GLM5_2-W8A8-A3-dual-nodes-x` 这类同前缀 stem 上会串。
    """
    if not stem:
        return None
    hits = [n for n in artifact_names if n.endswith(f"-{stem}{ARTIFACT_SUFFIX}")]
    if not hits:
        return None
    # 多个同名产物极少见（同 run 重跑），优先取分支前缀为 main 的那个
    for name in hits:
        if name.startswith(ARTIFACT_PREFERRED_PREFIX):
            return name
    return sorted(hits)[0]


def _node_index(node: str) -> int:
    m = re.search(r'(\d+)$', node)
    return int(m.group(1)) if m else 0


def _tail_text(raw: bytes, max_lines: int) -> tuple[str, int]:
    """把一段字节解成文本并取尾部 max_lines 行，返回 (文本, 总行数)。"""
    lines = raw.decode("utf-8", errors="ignore").splitlines()
    if max_lines and len(lines) > max_lines:
        return "\n".join(lines[-max_lines:]), len(lines)
    return "\n".join(lines), len(lines)


def extract_node_logs(zip_bytes: bytes, max_lines: int = 400) -> dict:
    """解出各节点的容器 stdout。**不做判桶**，只返回文本与「有没有内容」。

    返回（键恒定，便于下游断言）：
      {"ok", "nodes": {node: {"files", "lines", "kept_lines", "text", "oversize"}},
       "peers", "empty", "reason", "note"}
    `reason` 只在 `ok=False`/`empty=True` 时有值（即这次**没拿到可用文本**的原因）；
    `note` 是与成败无关的提示（如内层文件名与预期不符）。

    `empty=True` 专指「产物存在但为空」：内层 tar 里常规文件数为 0（实测 415B 产物即如此，
    且其目录里照样列着 node0..node3）。这是**另一种**结论，不能与「没有产物」或
    「对端节点无异常」混写——前者是产物没上传，后者是产物在但内容是空的。
    """
    result = {"ok": False, "nodes": {}, "peers": [], "empty": False,
              "reason": None, "note": None}
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except Exception as exc:                                  # 产物损坏/非 zip
        result["reason"] = f"产物不是可读的 zip（{type(exc).__name__}）"
        return result

    zip_names = archive.namelist()
    if INNER_TARBALL_NAME in zip_names:
        inner_name = INNER_TARBALL_NAME
    else:
        # 名字变了也尽量兜住（workflow 侧改名时不至于整条证据源失效），但要如实写进 reason
        candidates = [n for n in zip_names if n.endswith(".tar.gz")]
        if not candidates:
            result["reason"] = f"产物内没有 {INNER_TARBALL_NAME}"
            return result
        inner_name = candidates[0]
    try:
        inner = archive.read(inner_name)
        tar = tarfile.open(fileobj=io.BytesIO(inner), mode="r:gz")
    except Exception as exc:
        result["reason"] = f"内层 {inner_name} 无法打开（{type(exc).__name__}）"
        return result
    if inner_name != INNER_TARBALL_NAME:
        result["note"] = f"内层文件名与预期不同（实际为 {inner_name}，已按它解析）"

    members = tar.getmembers()
    regular = [m for m in members if m.isfile()]
    if not regular:
        # 判空只看这一条：实测该形态的产物 415B、内层 260B，**目录项齐全但零个常规文件**
        result.update(ok=True, empty=True,
                      reason="产物存在但为空（tar 内只有目录项，无任何常规文件）")
        return result

    for member in regular:
        match = NODE_LOG_MEMBER_RE.match(member.name)
        if not match:
            continue
        node = match.group(1)
        entry = result["nodes"].setdefault(
            node, {"node": node, "files": [], "lines": 0, "kept_lines": 0,
                   "text": "", "oversize": []})
        entry["files"].append(member.name)
        if member.size > MAX_MEMBER_BYTES:
            entry["oversize"].append(member.name)
            continue
        try:
            handle = tar.extractfile(member)
            raw = handle.read() if handle else b""
        except Exception:
            entry["oversize"].append(member.name)
            continue
        text, total = _tail_text(raw, max_lines)
        entry["text"] = (entry["text"] + "\n" + text).strip("\n")
        entry["lines"] += total
        entry["kept_lines"] = len(entry["text"].splitlines())

    result["ok"] = bool(result["nodes"])
    result["peers"] = sorted((n for n in result["nodes"] if _node_index(n) != PRIMARY_NODE_INDEX),
                             key=_node_index)
    if not result["ok"]:
        result["reason"] = ("产物里有常规文件，但没有 collected-logs/node{N}/var/log/*_logs.txt"
                            "（本 job 可能未产生容器日志）")
    return result


def peer_scan_text(extracted: dict) -> str:
    """把对端节点（node0 之外）的文本拼成待扫文本，带可读分隔头。"""
    parts = []
    for node in extracted.get("peers") or []:
        entry = (extracted.get("nodes") or {}).get(node) or {}
        if not entry.get("text"):
            continue
        parts.append(f"=== 对端节点 {node}（共 {entry.get('lines', 0)} 行，"
                     f"取尾部 {entry.get('kept_lines', 0)} 行）===")
        parts.append(entry["text"])
    return "\n".join(parts)


def adopt_peer_bucket(primary_bucket: str, peer_bucket: str) -> bool:
    """对端节点判出的桶是否被采用。**策略：仅兜底，不覆盖。**

    为什么不让对端覆盖主桶（哪怕对端命中的桶更靠前）：报告的第一条合成纪律是
    「三层证据并列，不互相覆盖」。node0 的窗口是**本 job 失败步骤**的时间窗，对端文本只是
    粗切的尾部若干行（无时间窗对齐），拿它去改写一个已经定性的结论，等于用一个时间基准更弱的
    证据推翻更强的那个。故只在主日志**判不出来**时才兜底采用，并在报告里注明来源。
    """
    return primary_bucket == "未分类" and bool(peer_bucket) and peer_bucket != "未分类"


def list_artifacts(repo: str, run_id, gh_call) -> tuple[list, str | None]:
    """列该 run 的全部产物，返回 (artifacts, 失败原因)。同一 run 只真正拉一次。

    `gh_call` 即第 1 步的 `gh`（`gh(path[, binary=True])`），由调用方注入 —— 这样本模块
    不依赖那个全模块级执行的脚本，测试也能注入假函数而不联网。
    """
    key = (repo, str(run_id))
    if key in _ARTIFACT_LISTING_CACHE:
        return _ARTIFACT_LISTING_CACHE[key], None
    listing = gh_call(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100")
    if not listing:
        return [], "产物列表拉取失败（gh api 返回空，可能是权限或限流）"
    try:
        artifacts = (json.loads(listing) or {}).get("artifacts") or []
    except Exception:
        return [], "产物列表不是合法 JSON"
    _ARTIFACT_LISTING_CACHE[key] = artifacts
    return artifacts, None


def pick_artifact(artifacts, artifact_name: str) -> tuple[dict | None, str | None]:
    """在产物列表里按名字取产物记录，返回 (记录, 失败原因)。"""
    hit = next((a for a in artifacts or [] if a.get("name") == artifact_name), None)
    if hit is None:
        return None, f"该 run 无名为 {artifact_name} 的产物"
    if hit.get("expired"):
        return None, f"产物 {artifact_name} 已过期（GitHub 默认保留 90 天）"
    return hit, None


def fetch_artifact_zip(repo: str, artifact: dict, cache_dir: str, gh_call) -> dict:
    """下载一份产物 zip。返回 {"ok","zip","artifact_id","size","reason","from_cache"}。

    缓存按 **artifact_id** 存盘：同一 run 的多个失败 job 常指向同一个产物（实测 run 36518916532
    里 14 个产物对应 20 个 job），缓存后只下载一次。
    """
    result = {"ok": False, "zip": None, "artifact_id": artifact.get("id"),
              "size": artifact.get("size_in_bytes"), "reason": None, "from_cache": False}
    artifact_id = artifact.get("id")
    cache_path = os.path.join(cache_dir, f"{artifact_id}.zip")
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        with open(cache_path, "rb") as handle:
            result["zip"] = handle.read()
        result.update(ok=True, from_cache=True)
        return result

    payload = gh_call(f"repos/{repo}/actions/artifacts/{artifact_id}/zip", binary=True)
    if not payload:
        result["reason"] = f"产物 {artifact.get('name')} 下载失败（gh api 返回空）"
        return result
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_path, "wb") as handle:
            handle.write(payload)
    except OSError:
        # 缓存写不进去不该让取证失败：本次仍然用内存里的这份
        pass
    result.update(ok=True, zip=payload)
    return result
