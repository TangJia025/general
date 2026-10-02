"""集群注册表：把「失败 job 的 runner 标签」映射到「该去哪个集群取什么证」。

两个数据源：
  1. ascend-gha-runners/docs 的 docs/Cluster.md —— 官方自动生成（源 = ArgoCD Application），
     提供 集群 → 项目(仓库) → namespace → runner 标签 的权威映射。缓存到本地避免每次联网。
  2. ~/kconf/asci/ 下的 kubeconfig —— 本地凭据，按**文件名**建索引。

⚠️ 三条实测教训，本模块的设计就是为了不踩它们：
  A. **runner 标签后缀不能用来判集群**。Liqo 会把虚拟节点 pod 反射进共享 namespace，
     实测 `linux-aarch64-a3-800i-*-cn12-001` 的 pod 能同时从 aiframework / cn12-001 /
     mind-third-ci 三个 kubeconfig 看到。故 resolve_by_label() 返回**候选集合**，
     绝不返回单一猜测——由 cluster_forensics 逐个探测后用证据收敛。
  B. **kubeconfig 的内部 name 字段不可信**。实测 openmerlin-guiyang-005 那个文件的
     `clusters[].name` 和 `current-context` 都写成 `gy-003`（复制粘贴 bug），
     但 server 确实是 gy-005。故一律按**文件名**索引，不读 current-context。
  C. **集群身份必须自检**。拿错集群的证据去解释 CI 失败，比没有证据更糟。
     self_check() 用「集群本地 CPU scale-set 标签」这类唯一属于该集群的标识做交叉验证。
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field

# 默认 kubeconfig 目录（用户提供）
DEFAULT_KUBECONFIG_DIR = os.path.expanduser("~/kconf/asci")

# 集群名 → 用于「本地性交叉验证」的标签片段（实测自 pod 名，非推测）。
#
# 为什么需要这张表：CN12-001 / HK-001 / SH-001 这类后缀会出现在**带集群限定的 runner 标签**里，
# 是强判别依据；但部分集群的标签不带后缀（如实测 hk-001 的 `linux-aarch64-910b-1`），
# 这些集群改用它**独有的 CPU runner 标签片段**做判别——CPU 池是集群本地的，不会被 Liqo 反射。
# 若某集群两个都拿不到，self_check 会明确报「无法自检」，而不是假装验证通过。
CLUSTER_LOCAL_MARKERS = {
    "ascend-cn12-001-cluster": ["-cn12-001", "-cn12"],
    "ascend-hk-001-cluster": ["-hk-001", "-hk"],
    "ascend-infra-guiyang-cluster-001": ["-gy001", "-guiyang-001", "-gy-001"],
    "openmerlin-guiyang-003-cluster": ["-gy003", "-guiyang-003", "-gy-003"],
    "openmerlin-guiyang-004-cluster": ["-gy004", "-guiyang-004", "-gy-004"],
    "openmerlin-guiyang-005-cluster": ["-gy005", "-guiyang-005", "-gy-005"],
    "openmerlin-sh-001-cluster": ["-sh-001", "-sh001"],
    "openmerlin-sh-002-cluster": ["-sh-002", "-sh002"],
    "ascend-aiframework": ["-aiframe", "-aiframework"],
    "ascend-mind-third-ci": ["-mind-third", "-mind-third-ci"],
    "in-cluster": [],
}

# 集群名 → kubeconfig 文件名里出现的名字（文件名是 `…_upload_<epoch>_<NAME>-kubeconfig_…yaml`）
CLUSTER_KUBECONFIG_NAMES = {
    "ascend-cn12-001-cluster": "ascend-cn12-001-cluster",
    "ascend-hk-001-cluster": "ascend-hk-001-cluster",
    "ascend-infra-guiyang-cluster-001": "ascend-infra-guiyang-cluster-001",
    "openmerlin-guiyang-003-cluster": "openmerlin-guiyang-003-cluster",
    "openmerlin-guiyang-004-cluster": "openmerlin-guiyang-004-cluster",
    "openmerlin-guiyang-005-cluster": "openmerlin-guiyang-005-cluster",
    "openmerlin-sh-001-cluster": "openmerlin-sh-001-cluster",
    "openmerlin-sh-002-cluster": "openmerlin-sh-002-cluster",
    "ascend-aiframework": "aiframework",
    "ascend-mind-third-ci": "ascend-mind-third-ci",
}

_KUBECONFIG_FILENAME_RE = re.compile(r"_upload_\d+_(.+?)-kubeconfig")


def canonical_aliases(cluster_name: str) -> list:
    """取集群名的合法别名（短名）。

    必须用别名集而非子串比较来判「kubeconfig 内部 name 是否异常」：
    实测 kubeconfig 内部 name 普遍是短名（`cn12-001` 之于 `ascend-cn12-001-cluster`），
    这是正常的；只有**不等于任何自身别名**才是真缺陷（gy-005 文件内部写成 gy-003）。
    """
    aliases = []
    for marker in CLUSTER_LOCAL_MARKERS.get(cluster_name, []):
        aliases.append(marker.lstrip("-"))
    # 从集群名本身派生：去掉组织前缀与 -cluster 后缀
    stripped = re.sub(r"^(?:ascend|openmerlin)-", "", cluster_name)
    stripped = re.sub(r"-cluster$", "", stripped)
    aliases.append(stripped)
    return sorted(set(a for a in aliases if a))


@dataclass
class KubeconfigInfo:
    """一个 kubeconfig 文件的可信元信息（只信文件名与 server 字段）。"""
    path: str
    filename: str
    cluster_name: str          # 来自文件名，可信
    server: str = ""           # 来自 YAML 的 server:，可信
    internal_name: str = ""    # 来自 YAML 的 name:，**不可信**（实测有复制粘贴错误）
    default_namespace: str = ""

    @property
    def name_mismatch(self) -> bool:
        """内部 name 不等于本集群的任何合法别名时告警（实测 gy-005 文件内部写成 gy-003）。"""
        if not self.internal_name:
            return False
        return self.internal_name not in canonical_aliases(self.cluster_name)


@dataclass
class ClusterLabels:
    """一个集群里某个项目的 runner 标签集合。"""
    namespace: str = ""
    labels: dict = field(default_factory=dict)   # 带集群后缀的全名 → npu 资源名
    # 展示名 → [带后缀全名]。Cluster.md 里 `<div class="machine" data-label="…-cn12-001">
    # <span class="machine-label">…</span>` 成对出现，job 在 GitHub 上报的标签是**展示名**，
    # 而 pod 名与 Cluster.md 的 data-label 用的是**带后缀全名**。不做这个映射，
    # 真实 job（如 `linux-aarch64-a3-800t-0`）会被判成「Cluster.md 未登记该标签」而无法取证。
    display_aliases: dict = field(default_factory=dict)

    def full_labels_for(self, reported_label: str) -> list:
        """把 job 上报的标签翻译成该集群里真正的全名（可多个）。"""
        if reported_label in self.labels:
            return [reported_label]
        return list(self.display_aliases.get(reported_label) or [])


def parse_cluster_map(html_text: str) -> dict:
    """解析 docs/Cluster.md 的 HTML，返回 {集群名: {项目: ClusterLabels}}。

    结构（实测）：
      <div class="cluster-card" data-name="…">
        <div class="project-row" data-search="<repo> <label> …">
          <span class="project-name-text">owner/repo</span>
          <div class="machine" data-label="<带集群后缀的全名>" data-npu="ascend-1980">
            <span class="machine-label">展示名（不带后缀）</span>
          <div class="project-ns">namespace: <code>ascend</code></div>
    注意 namespace 在 **project-row** 内，同一集群不同项目 namespace 不同。
    """
    clusters: dict = {}
    # 按 cluster-card 切块，块内再按 project-row 切
    for card in re.split(r'<div class="cluster-card"', html_text)[1:]:
        card_name = re.search(r'data-name="([^"]*)"', card)
        if not card_name:
            continue
        cluster_name = card_name.group(1)
        projects: dict = {}
        for row in re.split(r'<div class="project-row"', card)[1:]:
            project = re.search(r'class="project-name-text">([^<]*)', row)
            if not project:
                continue
            namespace = re.search(r'class="project-ns">namespace:\s*<code>([^<]*)', row)
            labels, display_aliases = {}, {}
            # 一次抓三元组：全名 / npu 资源 / 同 div 内的展示名（无展示名时为空串）。
            # `\s*` 不能省：展示名 span 与 div 开标签之间只要有换行/缩进，写死紧邻就会**静默**
            # 丢掉别名映射 —— 那是「真实 job 被判成 Cluster.md 未登记」这类假阴性的来源。
            for label, npu, display in re.findall(
                    r'data-label="([^"]*)" data-npu="([^"]*)"[^>]*>\s*'
                    r'(?:<span class="machine-label">([^<]*)</span>)?', row):
                labels[label] = npu
                display = (display or "").strip()
                # 展示名与全名相同时不算别名，避免制造无意义的第二份索引
                if display and display != label:
                    display_aliases.setdefault(display, []).append(label)
            projects[project.group(1).strip()] = ClusterLabels(
                namespace=(namespace.group(1).strip() if namespace else ""),
                labels=labels,
                display_aliases=display_aliases,
            )
        clusters[cluster_name] = projects
    return clusters


def fetch_cluster_md(cache_path: str | None = None, max_age_hours: int = 24,
                     repo: str = "ascend-gha-runners/docs") -> dict:
    """取官方 Cluster.md（集群 → 项目 → namespace → 标签 的权威映射）并缓存。

    该文件由 CI 部署配置（ArgoCD Application）自动生成，是集群归属的权威来源；
    但每次分析都联网取不合适（内容变动很慢），故默认缓存 24h。

    返回 {text, source, error}
    """
    if cache_path and os.path.exists(cache_path):
        age_hours = (time.time() - os.path.getmtime(cache_path)) / 3600
        if age_hours < max_age_hours:
            try:
                with open(cache_path, encoding="utf-8") as fh:
                    return {"text": fh.read(), "source": "cache", "error": None}
            except OSError:
                pass
    try:
        proc = subprocess.run(
            ["gh", "api", "-H", "Accept: application/vnd.github.raw",
             f"repos/{repo}/contents/docs/Cluster.md"],
            capture_output=True, timeout=90,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"text": "", "source": "network", "error": str(exc)}
    if proc.returncode != 0:
        error = proc.stderr.decode("utf-8", errors="ignore").strip()[:300]
        # 网络失败时宁可用陈旧缓存也不空手（否则整个集群归属环节失效）
        if cache_path and os.path.exists(cache_path):
            try:
                with open(cache_path, encoding="utf-8") as fh:
                    return {"text": fh.read(), "source": "cache(过期)", "error": error}
            except OSError:
                pass
        return {"text": "", "source": "network", "error": error}
    text = proc.stdout.decode("utf-8", errors="ignore")
    if cache_path and text:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass
    return {"text": text, "source": "network", "error": None}


def load_kubeconfigs(directory: str = DEFAULT_KUBECONFIG_DIR) -> dict:
    """扫描 kubeconfig 目录，返回 {集群名: KubeconfigInfo}，**按文件名索引**（见模块头教训 B）。"""
    found: dict = {}
    if not os.path.isdir(directory):
        return found
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith((".yaml", ".yml")):
            continue
        match = _KUBECONFIG_FILENAME_RE.search(filename)
        if not match:
            continue
        path = os.path.join(directory, filename)
        info = KubeconfigInfo(path=path, filename=filename, cluster_name=match.group(1))
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        # 极简 YAML 提取：无需引入 pyyaml，只取我们需要的三个标量
        server = re.search(r'^\s*server:\s*(\S+)', text, re.M)
        info.server = server.group(1) if server else ""
        # clusters[].name —— 取 clusters 段内第一个 name:
        block = re.search(r'^clusters:\s*$(.*?)(?=^\w|\Z)', text, re.M | re.S)
        name = re.search(r'^\s*-?\s*name:\s*(\S+)', block.group(1), re.M) if block else None
        info.internal_name = name.group(1) if name else ""
        ns = re.search(r'^\s*namespace:\s*(\S+)', text, re.M)
        info.default_namespace = ns.group(1) if ns else ""
        found[info.cluster_name] = info
    return found


class ClusterRegistry:
    """集群映射查询入口。所有查询都返回**候选**，不做单点猜测。"""

    def __init__(self, cluster_map: dict | None = None, kubeconfig_dir: str = DEFAULT_KUBECONFIG_DIR):
        self.clusters = cluster_map or {}
        self.kubeconfigs = load_kubeconfigs(kubeconfig_dir)
        self.kubeconfig_dir = kubeconfig_dir

    @classmethod
    def from_cluster_md(cls, cluster_md_path: str, kubeconfig_dir: str = DEFAULT_KUBECONFIG_DIR):
        with open(cluster_md_path, encoding="utf-8") as fh:
            return cls(parse_cluster_map(fh.read()), kubeconfig_dir)

    # ---------- 查询 ----------

    def kubeconfig_for(self, cluster_name: str) -> KubeconfigInfo | None:
        """按集群名取 kubeconfig（经 CLUSTER_KUBECONFIG_NAMES 别名表，容忍文件名命名差异）。"""
        if cluster_name in self.kubeconfigs:
            return self.kubeconfigs[cluster_name]
        alias = CLUSTER_KUBECONFIG_NAMES.get(cluster_name)
        if alias and alias in self.kubeconfigs:
            return self.kubeconfigs[alias]
        # 退一步：文件名包含关系
        for name, info in self.kubeconfigs.items():
            if name in cluster_name or cluster_name in name:
                return info
        return None

    def clusters_for_repo(self, repo: str) -> list:
        """该仓库在哪些集群有 runner（Cluster.md 的 project-name-text 就是 owner/repo）。"""
        return [name for name, projects in self.clusters.items() if repo in projects]

    def resolve_by_label(self, repo: str, labels) -> dict:
        """给定仓库 + job 的 runner 标签，返回**候选集群**，并显式区分命中强度。

        返回 {"exact": [...], "fallback": [...]} 而不是平铺列表：
          exact    = Cluster.md 里该集群**登记了这个标签**（强候选，但仍可能多个，见模块头教训 A）
          fallback = 只登记了同仓库、没有这个标签（弱候选，仅用于「标签可能已下线」时探测）
        调用方必须知道自己在哪一档上做判断——平铺列表会让「仅同仓库」被误读成「就是这个集群」。

        「登记了这个标签」包含两种等价形式：Cluster.md 的 data-label 全名，
        以及同一个 machine div 里的展示名（job 在 GitHub 上报的往往是展示名）。
        """
        wanted = set(labels or [])
        exact, fallback = [], []
        for cluster_name, projects in self.clusters.items():
            if repo not in projects:
                continue
            if wanted & set(self.registered_names_for(repo, cluster_name)):
                exact.append(cluster_name)
            else:
                fallback.append(cluster_name)
        return {"exact": exact, "fallback": fallback}

    def registered_names_for(self, repo: str, cluster_name: str) -> set:
        """该集群该仓库下**可被 job 上报**的标签名集合（全名 + 展示名）。"""
        project = (self.clusters.get(cluster_name) or {}).get(repo)
        if project is None:
            return set()
        return set(project.labels) | set(project.display_aliases)

    def full_labels_for(self, repo: str, cluster_name: str, reported_labels) -> list:
        """把 job 上报的标签翻译成该集群里 pod 名真正使用的全名（可能多个）。

        为什么不直接用上报的标签去查 pod：pod 名用的是带集群后缀的全名，
        拿展示名去前缀匹配**必然查空**，会被误报成「该标签此刻在本集群无 runner」——
        一个纯属工具自己造成的假阴性。翻译后再查，查空才有意义。
        """
        project = (self.clusters.get(cluster_name) or {}).get(repo)
        if project is None:
            return []
        resolved = []
        for label in reported_labels or []:
            for full in project.full_labels_for(label):
                if full not in resolved:
                    resolved.append(full)
        return resolved

    def namespace_for(self, repo: str, cluster_name: str) -> str | None:
        """该仓库在该集群的 namespace（集群取证必须用对 namespace）。"""
        project = (self.clusters.get(cluster_name) or {}).get(repo)
        return project.namespace if project else None

    def label_known_in(self, label: str, cluster_name: str) -> bool:
        """该标签是否登记在 Cluster.md 的该集群下（全名、展示名或全名前缀都算）。"""
        for project in (self.clusters.get(cluster_name) or {}).values():
            known_names = set(project.labels) | set(project.display_aliases)
            if label in known_names or any(
                    label == known or label.startswith(known + "-") for known in known_names):
                return True
        return False

    def cluster_for_virtual_node(self, node_name: str) -> str | None:
        """Liqo 虚拟节点名 → 已登记集群名；**仅当唯一命中**时返回，否则 None。

        为什么只做「唯一命中」：Liqo 的虚拟节点名取自提供方集群（实测 `mind-third-ci`
        对应登记的 `ascend-mind-third-ci`，正好是 `canonical_aliases` 里的短名），
        但这是**命名约定**而非接口保证。多义时返回 None —— 报一个不确定的集群名，
        比报「不确定」危害大得多（读者会直接采信）。
        """
        if not node_name:
            return None
        matched = [name for name in self.clusters if node_name in canonical_aliases(name)]
        return matched[0] if len(matched) == 1 else None

    # ---------- 身份自检 ----------

    def identity_markers(self, cluster_name: str) -> list:
        """取该集群专属的标签片段（用于交叉验证连的集群对不对）。"""
        return CLUSTER_LOCAL_MARKERS.get(cluster_name, [])

    def self_check_plan(self) -> list:
        """产出「集群 → 取证可用性」清单，供报告显式列出哪些集群能查、哪些查不了。

        返回每条：{cluster, has_kubeconfig, kubeconfig_path, markers, warning}
        """
        plan = []
        for cluster_name in sorted(set(self.clusters) | set(CLUSTER_LOCAL_MARKERS)):
            info = self.kubeconfig_for(cluster_name)
            warning = ""
            if info is None:
                warning = "无 kubeconfig —— 该集群的失败无法做集群侧取证，只能依据日志侧结论"
            elif info.name_mismatch:
                warning = (f"kubeconfig 内部 name={info.internal_name!r} 与文件名 {info.cluster_name!r} 不一致"
                           f"（已知复制粘贴缺陷），本工具按文件名与 server 判定，不受影响")
            if not self.identity_markers(cluster_name) and info is not None:
                warning = (warning + "；" if warning else "") + "无可用的集群本地标识，身份无法交叉验证"
            plan.append({"cluster": cluster_name,
                         "has_kubeconfig": info is not None,
                         "kubeconfig_path": info.path if info else None,
                         "server": info.server if info else None,
                         "markers": self.identity_markers(cluster_name),
                         "warning": warning})
        return plan
