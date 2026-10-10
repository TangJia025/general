"""第 4 步：历史问题定位归因（以 ascend-gha-runners/docs 的 issues 作为知识库）。

为什么匹配要靠「错误签名」而不是标签或标题：
  - **标签不可用**：实测 179 个 issue 里 2/3 无标签，有标签的几乎只有 bug / problem-tracking，
    没有集群、错误类型、owner 任何一种结构化维度。
  - **标题不可用**：前缀五花八门（[缺陷]/[需求]/[Bug]/[chore]…），而**最有价值的复盘贴
    （#263/#257/#255/#187 等）反而没有前缀**。
  - **正文可用**：正文是自由散文，但**含大量可直接 grep 的错误签名与稳定标识**
    （error code 507035、TaintManagerEviction、FileBaton、exit 137、ECONNREFUSED…）。
    这是唯一可靠的匹配键。

正文结构约定（人工撰写，非模板强制，但实测高价值复盘普遍遵循）：
    ## 现象 / ## 时间线 / ## 证据来源 / ## 根因 / ## 建议修复 / ## 影响

⚠️ 核心纪律：**错误信号所在层 ≠ 责任方所在层**。
历史误归属案例：#123 `error code 507035` 被当成平台硬件问题，实为业务方算子；
#212 `FailedScheduling Insufficient huawei.com/ascend-1980` 实为业务方资源名写错；
#238「找不到缓存模型」实为平台清理脚本 atime 缺陷。
故本模块的匹配结果只用于**提供线索与先例**，不自动改写 owner，必须由报告并列呈现供人判断。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

DEFAULT_ISSUE_REPO = "ascend-gha-runners/docs"

# 正文小节标题 → 归一后的键（容忍 `## ` / `### ` 两种层级与常见异写）
SECTION_ALIASES = {
    "根因": "root_cause", "根本原因": "root_cause", "原因": "root_cause",
    "修复": "fix", "修复方案": "fix", "建议修复": "fix", "已执行修复": "fix", "修复（已执行）": "fix",
    "防复发建议": "prevention", "建议": "prevention", "建议 / 待办": "prevention", "待办": "prevention",
    "现象": "symptom", "时间线": "timeline", "结论": "conclusion",
    "证据来源": "evidence", "影响": "impact", "定位手段": "diagnosis",
    "定位手段（py-spy 抓栈）": "diagnosis", "最小复现": "reproduction",
}

# 数值类签名单独处理：必须把码值**归一成裸码**，否则同一错误换个措辞就匹配不上。
# 实测 #123 正文写的是 `error code is 507035`，若整段保留为签名，
# 则 `error code 507035` 这种写法永远匹配不到它。归一后两边都产出 `errorcode:507035`。
ERROR_CODE_PATTERN = re.compile(r'error\s*code\s*(?:is|:|=)?\s*(\d{5,6})', re.I)
EXIT_CODE_PATTERNS = [
    re.compile(r'\bexit\w*[^\d\n]{0,20}?(\d{1,3})\b', re.I),      # exit 137 / exited with exit code 137
    re.compile(r'\bexitCode\b[^\d\n]{0,6}?(\d{1,3})', re.I),      # exitCode: 137（k8s JSON 形态）
]

# 其余错误签名：整体保留匹配文本（已足够判别），提取后统一小写去重。
SIGNATURE_PATTERNS = [
    r'ERR\d{4,6}',                                     # ERR99999
    r'\b[A-Z][A-Za-z]*(?:Error|Exception|Timeout|Failure)\b',   # ValueError / RuntimeError
    r'\[Errno \d+\]',                                  # [Errno 116] Stale file handle
    r'\b(?:OOMKilled|FailedMount|FailedScheduling|FailedBinding|Evicted|'
    r'TaintManagerEviction|ErrImagePull|ImagePullBackOff|CreateContainerConfigError|'
    r'ContainerCreating|CrashLoopBackOff|NodeNotReady|DiskPressure)\b',
    r'linux-[a-z0-9]+(?:-[a-z0-9]+)*',                   # runner 标签
    r'\b(?:CN12|cn12|HK001|hk-001|gy00[1-5]|sh-00[12]|sh001)\b',  # 集群标识
    r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?\b', # IP:PORT（如 ECONNREFUSED 10.247.0.1:443）
    r'\bFileBaton\b|\bpy-spy\b|\bnpu-smi\b',            # 特征工具/符号
    r'\bvllm[-a-z]*\b|\btorch_npu\b|\bCANN\b|\bHCCL\b',
]

# 低区分度词：出现在几乎所有 issue 里，参与打分只会稀释信号
STOPWORDS = {
    "the", "and", "for", "with", "not", "this", "that", "from", "have", "has",
    "问题", "失败", "错误", "原因", "修复", "建议", "现象", "集群", "任务", "运行",
    "job", "runner", "ci", "github", "action", "workflow", "npu", "ascend",
    # `vllm` 与上面这些同类：它是本仓所有 issue 的**共同背景**，不是判别特征。
    # 实测 df=108/190（57%）—— 几乎每条 issue 都提它，留着只会把「同为 vllm-ascend 的
    # 失败」冒充成「同一个现象」。实测它正是把 #227（pypi 镜像 CDN）顶到 41% 报告根因位的
    # 那个词。**注意不要顺手把 torch_npu / CANN / HCCL 一起加进来**：实测它们的 df 是
    # 6 / 10 / 4，属稀有且有判别力的词，加了会把真信号也掐掉（tests/test_issue_knowledge.py
    # 有反例守卫钉住这一条）。
    "vllm",
    # 2 字滑窗引入的虚词/泛用词：这些几乎出现在每篇 issue 里，留着只会稀释 IDF 信号。
    # 只加**无实义**的连接/泛化词，像「参数」「配置」「结果」这类有判别力的保留。
    "我们", "这个", "那个", "一个", "可以", "没有", "需要", "进行", "使用", "当前",
    "已经", "由于", "因为", "所以", "如果", "或者", "但是", "而且", "并且", "以及",
    "通过", "相关", "情况", "内容", "信息", "时间", "支持", "提供", "处理", "出现",
    "可能", "应该", "如下", "以下", "上述", "其中", "对于", "关于", "同时", "之后",
    "就是", "还是", "不是", "会有", "导致", "造成", "存在", "无法", "不能",
}


def fetch_issues(repo: str = DEFAULT_ISSUE_REPO, cache_path: str | None = None,
                 max_age_hours: int = 24, limit: int = 1000) -> dict:
    """取 issue 全量列表（含已关闭），带本地缓存。

    返回 {issues: [...], source: "cache"|"network", fetched_at, error}。
    缓存的意义：匹配只需读，重复运行不该反复打 GitHub API；且离线时仍可用。
    """
    if cache_path and os.path.exists(cache_path):
        age_hours = (time.time() - os.path.getmtime(cache_path)) / 3600
        if age_hours < max_age_hours:
            try:
                with open(cache_path, encoding="utf-8") as fh:
                    payload = json.load(fh)
                return {"issues": payload.get("issues", []), "source": "cache",
                        "fetched_at": payload.get("fetched_at"), "error": None}
            except Exception:
                pass  # 缓存损坏则回退网络

    try:
        proc = subprocess.run(
            ["gh", "issue", "list", "-R", repo, "--state", "all", "--limit", str(limit),
             "--json", "number,title,body,labels,state,createdAt,closedAt,url"],
            capture_output=True, timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"issues": [], "source": "network", "fetched_at": None, "error": str(exc)}
    if proc.returncode != 0:
        error = proc.stderr.decode("utf-8", errors="ignore").strip()[:300]
        # 网络失败但缓存陈旧时，宁可用陈旧缓存也不空手
        if cache_path and os.path.exists(cache_path):
            try:
                with open(cache_path, encoding="utf-8") as fh:
                    payload = json.load(fh)
                return {"issues": payload.get("issues", []), "source": "cache(过期)",
                        "fetched_at": payload.get("fetched_at"), "error": error}
            except Exception:
                pass
        return {"issues": [], "source": "network", "fetched_at": None, "error": error}

    try:
        issues = json.loads(proc.stdout.decode("utf-8", errors="ignore"))
    except Exception as exc:
        return {"issues": [], "source": "network", "fetched_at": None, "error": f"解析失败: {exc}"}

    fetched_at = time.strftime("%Y-%m-%d %H:%M:%S")
    if cache_path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump({"fetched_at": fetched_at, "repo": repo, "issues": issues},
                          fh, ensure_ascii=False)
        except OSError:
            pass
    return {"issues": issues, "source": "network", "fetched_at": fetched_at, "error": None}


def fetch_comments(repo: str = DEFAULT_ISSUE_REPO, cache_path: str | None = None,
                   max_age_hours: int = 24) -> dict:
    """取**全仓**评论并按 issue 号归并。

    为什么必须取评论：实测高价值根因经常只写在评论里。例如 #238「A2 任务找不到缓存模型」
    的正文是空模板（`_No response_`），真因全在评论：
    「清理规则只看 >50MB 权重文件的 atime → 大文件 atime 停在去年 12 月 →
      整个目录（含 config.json）被改名」。只索引正文会完全错过这条先例。

    用 `gh api --paginate` 一次取全仓（实测 3 页 ~269 条），比逐 issue 查便宜得多。
    返回 {comments: {issue_number: [comment, ...]}, source, error}
    """
    if cache_path and os.path.exists(cache_path):
        age_hours = (time.time() - os.path.getmtime(cache_path)) / 3600
        if age_hours < max_age_hours:
            try:
                with open(cache_path, encoding="utf-8") as fh:
                    payload = json.load(fh)
                # JSON 的键必然是字符串，转回 int
                return {"comments": {int(k): v for k, v in payload.get("comments", {}).items()},
                        "source": "cache", "error": None}
            except Exception:
                pass
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/comments?per_page=100",
             "--paginate", "--jq", ". | .[]"],
            capture_output=True, timeout=180,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"comments": {}, "source": "network", "error": str(exc)}
    if proc.returncode != 0:
        return {"comments": {}, "source": "network",
                "error": proc.stderr.decode("utf-8", errors="ignore").strip()[:300]}

    grouped: dict = {}
    for line in proc.stdout.decode("utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            comment = json.loads(line)
        except Exception:
            continue
        match = re.search(r'/issues/(\d+)$', comment.get("issue_url") or "")
        if not match:
            continue
        grouped.setdefault(int(match.group(1)), []).append({
            "author": ((comment.get("user") or {}).get("login")),
            "created_at": comment.get("created_at"),
            "body": comment.get("body") or "",
            "url": comment.get("html_url"),
        })
    if cache_path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump({"repo": repo, "comments": grouped}, fh, ensure_ascii=False)
        except OSError:
            pass
    return {"comments": grouped, "source": "network", "error": None}


# 平台侧动作特征词：用于提示「这条先例的根因可能落在平台而非业务方」。
#
# ⚠️ 用途仅限于**提示核对**，不用于自动改判责任方——从散文推断责任方本身就是不可靠的，
# 而本工具的纪律是「错误信号所在层 ≠ 责任方所在层」，宁可提示人去看，也不替人下结论。
PLATFORM_SIGNAL_PATTERNS = [
    (r'清理脚本|老化脚本|定时任务|清理规则|atime', "平台存在按时间/访问时间自动清理的脚本或规则"),
    (r'驱逐|Evicted|TaintManagerEviction|drain|污点|taint|NoExecute', "节点驱逐/污点相关"),
    (r'部署配置|nodeName|缩容|弹性节点|节点组|scale', "平台部署配置或弹性伸缩相关"),
    (r'kubelet|containerd|CRI|运行时', "容器运行时/kubelet 相关"),
    (r'白名单|网络策略|出网|出口|防火墙', "网络策略/出网白名单相关"),
    (r'镜像仓库|内网源|pypi|镜像站|registry', "内网源/镜像仓库可用性相关"),
    (r'调度器|调度失败|FailedScheduling|Insufficient', "调度器/资源配额相关"),
    (r'共享盘|挂载|PVC|存储|文件系统', "共享存储/挂载相关"),
]


def extract_platform_signals(text: str) -> list:
    """抽平台侧动作特征（仅作提示，见 PLATFORM_SIGNAL_PATTERNS 注释）。"""
    hits = []
    if not text:
        return hits
    for pattern, description in PLATFORM_SIGNAL_PATTERNS:
        if re.search(pattern, text, re.I):
            hits.append(description)
    return hits


def extract_sections(body: str) -> dict:
    """按 markdown 标题切正文，标题经 SECTION_ALIASES 归一。

    只认二级/三级标题；同一归一键出现多次时拼接（实测 #274 用 `### 族 A`/`### 族 B` 分列）。
    """
    sections: dict = {}
    if not body:
        return sections
    current_key = None
    buffer: list = []
    for line in body.splitlines():
        heading = re.match(r'^#{2,3}\s+(.*?)\s*$', line)
        if heading:
            if current_key:
                sections[current_key] = (sections.get(current_key, "") + "\n" + "\n".join(buffer)).strip()
            title = heading.group(1).strip()
            current_key = SECTION_ALIASES.get(title)
            # 容忍「定位手段（py-spy 抓栈）」这类带括号后缀的标题
            if current_key is None:
                base = re.sub(r'[（(].*?[)）]', '', title).strip()
                current_key = SECTION_ALIASES.get(base)
            buffer = []
        elif current_key:
            buffer.append(line)
    if current_key:
        sections[current_key] = (sections.get(current_key, "") + "\n" + "\n".join(buffer)).strip()
    # 正文里有裸的「根因：」行（无标题）时也捞一下
    if "root_cause" not in sections:
        inline = re.search(r'^[*\s]*根因[*\s]*[:：]\s*(.+)$', body, re.M)
        if inline:
            sections["root_cause"] = inline.group(1).strip()
    return sections


# 「无机制含义」的签名：命中它们不足以说明「同现象」，只能说明「都报错」。
#
# 为什么需要单独列出：稀有度（IDF）判断不了它们。`valueerror` 在本库 df=3，按 IDF 算是
# 「稀有」，可它只是 Python 异常**类名**，不含任何机制——实测 #190/#254/#101 都靠它被标成
# 「证据强度=强」，而它们与缓存未命中毫无关系。同理 ERR99999：本工具的知识表已明确
# 「ERR99999 是昇腾对任意未捕获应用层异常的通用包装，不是硬件信号」，那它自然也不能算机制证据。
#
# 反例对照（**不在**本清单里，属真机制证据）：exitcode:137（退出码）、errorcode:507035（错误码）、
# FailedScheduling/FailedMount（k8s 具体原因）、IP:PORT、runner 标签、集群标识。
#
# ⚠️ 本清单有**两个**生效点，缺一不可（曾经只有后者，于是白名单形同虚设）：
#   ① `IssueIndex.match` 的**打分**环节：签名路与关键词路都不给分（见那里的注释）；
#   ② `match` 组装结果时的 `mechanism_signatures` / `core_keywords` 过滤。
# 只做 ② 不做 ① 的话，这些词照样拿满权重把无关 issue 顶到第一名，只是最后被标成「弱」——
# 而「弱」的条目仍会进 weak_leads 提示、并靠分数挤掉真正同现象的候选。
GENERIC_SIGNATURES = {
    "valueerror", "typeerror", "keyerror", "indexerror", "attributeerror",
    "runtimeerror", "importerror", "assertionerror", "oserror",
    "calledprocesserror", "timeouterror", "err99999",
}


def extract_signatures(text: str) -> set:
    """抽错误签名（匹配键）。

    **唯一真值源**：索引侧与查询侧都必须调这个函数，否则归一规则一改两边就不一致。
    数值类码值归一为 `errorcode:<码>` / `exitcode:<码>`（见 ERROR_CODE_PATTERN 处注释）。
    """
    found = set()
    if not text:
        return found
    for match in ERROR_CODE_PATTERN.finditer(text):
        found.add(f"errorcode:{match.group(1)}")
    for pattern in EXIT_CODE_PATTERNS:
        for match in pattern.finditer(text):
            found.add(f"exitcode:{match.group(1)}")
    for pattern in SIGNATURE_PATTERNS:
        for match in re.findall(pattern, text, re.I):
            token = match.strip().lower()
            if token and token not in STOPWORDS:
                found.add(token)
    return found


def _tokenize(text: str) -> set:
    """中文按 2 字滑窗 + 贪心切片、英文/数字按词切，用于正文相似度打分。

    为什么必须加 2 字滑窗：中文无空格，`[一-鿿]{2,4}` 是**贪心**切片，
    「模型缓存未命中」只会切出「模型缓存」「未命中」，**切不出「缓存」**这个真正有语义的二元组。
    结果是本次失败原因的「模型缓存未命中」与历史 #238 正文「找不到缓存模型」永远无法匹配——
    而 #238 恰是解释该现象机制的唯一先例（平台老化脚本按 atime 判冷数据）。
    """
    if not text:
        return set()
    tokens = set(re.findall(r'[a-z0-9_]{3,}', text.lower()))
    for run in re.findall(r'[一-鿿]{2,}', text):
        tokens |= set(re.findall(r'[一-鿿]{2,4}', run))    # 保留原贪心切片
        for index in range(len(run) - 1):                  # 补 2 字滑窗
            tokens.add(run[index:index + 2])
    return {token for token in tokens if token not in STOPWORDS}


def keywords_for(text: str) -> set:
    """把任意文本转成匹配用关键词集合（对外入口，避免调用方依赖私有 _tokenize）。"""
    return _tokenize(text or "")


class IssueIndex:
    """issue 知识库索引：支持按签名/关键词匹配，带 IDF 加权。

    索引范围 = 标题 + 正文 + **全部评论**。评论必须纳入，否则会漏掉只写在评论里的根因
    （#238 就是典型：正文空模板，真因在评论）。根因取自哪个来源会记录在 root_cause_source。
    """

    def __init__(self, issues: list, comments: dict | None = None):
        self.issues = []
        self.signature_index: dict = {}     # 签名 → [issue 下标]
        self.token_index: dict = {}         # 词 → [issue 下标]
        comments = comments or {}
        for index, raw in enumerate(issues):
            body = raw.get("body") or ""
            title = raw.get("title") or ""
            issue_comments = comments.get(raw.get("number"), [])
            comment_text = "\n".join(c.get("body") or "" for c in issue_comments)
            # 正文与评论分别切小节：正文优先，正文缺的键用评论里的补
            sections = extract_sections(body)
            root_cause_source = "body" if sections.get("root_cause") else None
            comment_sections = extract_sections(comment_text) if comment_text else {}
            if not sections.get("root_cause") and comment_sections.get("root_cause"):
                sections["root_cause"] = comment_sections["root_cause"]
                root_cause_source = "comment"
            for key in ("fix", "prevention", "symptom", "conclusion", "diagnosis"):
                if not sections.get(key) and comment_sections.get(key):
                    sections[key] = comment_sections[key]
            labels = [lab.get("name") for lab in (raw.get("labels") or []) if isinstance(lab, dict)]
            searchable_text = title + "\n" + body + "\n" + comment_text
            entry = {
                "number": raw.get("number"),
                "title": title,
                "state": raw.get("state"),
                "url": raw.get("url") or f"https://github.com/{DEFAULT_ISSUE_REPO}/issues/{raw.get('number')}",
                "labels": labels,
                "created_at": raw.get("createdAt"),
                "closed_at": raw.get("closedAt"),
                "sections": sections,
                # 存成有序 list 而非 set：该条目会随报告一起 dump 成 JSON，
                # 而 set 不可序列化（曾导致整个 JSON 导出在最后一步崩掉）
                "signatures": sorted(extract_signatures(searchable_text)),
                "comments": issue_comments,
                "is_postmortem": bool(sections.get("root_cause")),
                # 根因来自正文还是评论——报告里须如实标注，来自评论的要给出评论链接
                "root_cause_source": root_cause_source,
                # 根因里是否提到平台侧动作（仅提示核对，不用于自动改判，见 PLATFORM_SIGNAL_PATTERNS）
                "platform_signals": extract_platform_signals(sections.get("root_cause") or ""),
            }
            self.issues.append(entry)
            for signature in entry["signatures"]:
                self.signature_index.setdefault(signature, []).append(index)
            for token in _tokenize(searchable_text):
                self.token_index.setdefault(token, []).append(index)

    def __len__(self) -> int:
        return len(self.issues)

    def match(self, signatures: set, keywords: set, top_k: int = 5,
              precedent_k: int = 3, provenance: set | None = None) -> list:
        """按签名（主）与关键词（辅）打分。返回 [{issue, score, ...}]，按分降序。

        签名命中权重高得多——它几乎不会偶然撞上；关键词只用来在同签名候选里排序。
        IDF 由签名在该 issue 库中的出现频次决定：只在 1-2 个 issue 出现的签名最有判别力。

        **precedent_k —— 为什么不能只取总榜前 top_k**：
        只按分数截断会让「有根因小节的复盘」被纯讨论贴挤光。实测 case 3（模型缓存未命中）
        的总榜前 5 全是无根因的讨论贴，于是第 5 步的「历史先例」机制**静默失效**，
        报告退回「仅日志侧正则定性」——而知识库里其实躺着同现象的 #238。
        故对「有根因小节」的条目单独保底 top precedent_k 个。

        **provenance —— 为什么出处名不能算证据**：
        workflow / job / step 名是**出处**，不是**机制**。实测 #228（AOP bisect 被超时杀死）
        仅因 `schedule_nightly_test_a2`(df=1) 这个工作流名就拿到 65.66 分，
        主题其实与缓存无关，却盖过了真正同现象的 #238(11.13 分)。
        调用方把出处类词传进来，本函数据此把「命中的词」分成两类：
          - `matched_keywords`：全部命中词（信息性，照原样展示）
          - `core_keywords`：命中词里**稀有、非出处、且非通用异常名**的部分
            （df ≤ max(3, 总数的 2%)）
        并给出 `evidence_strength`（强/中/弱），供报告决定该不该写「高度吻合」。
        其中「强」还要求签名**带机制**——`valueerror` 这类异常类名只说明「都报错」，
        不说明「同现象」，故单列在 GENERIC_SIGNATURES 里排除。

        **注意 `evidence_strength` 不是唯一的排除点**：GENERIC_SIGNATURES 在打分环节
        就已经不给分（签名路与关键词路各一处），否则这些词会先靠 IDF 把无关 issue
        顶到第一，再由本字段把它标成「弱」——排名已经错了，标弱救不回来。
        """
        total = len(self.issues) or 1
        scores: dict = {}
        matched_sigs: dict = {}
        matched_kws: dict = {}
        provenance = set(provenance or set())
        # 核心症状词的出现频次上限：低于此值的词在库里属「少见到能说明问题」
        core_df_limit = max(3, int(total * 0.02))

        for signature in signatures or set():
            # 通用异常名不给分。它们只说明「都报错了」，而且 IDF 恰恰会奖励它们：
            # 实测 `importerror` 在本库 df=1，单项就拿 3.0×IDF≈288 分 —— 足以把一条
            # 毫不相干的复盘（#227 pypi 镜像 CDN）顶到第一名，再被第 5 步写成
            # 「高度吻合」。原先 GENERIC_SIGNATURES 只在下面的 evidence_strength 处
            # 排除，打分环节照给 —— 那是本模块最贵的一处不一致。
            if signature in GENERIC_SIGNATURES:
                continue
            for index in self.signature_index.get(signature, []):
                # IDF：稀有签名权重高
                idf = 1.0 + (total / (1 + len(self.signature_index[signature])))
                scores[index] = scores.get(index, 0.0) + 3.0 * idf
                matched_sigs.setdefault(index, set()).add(signature)

        for keyword in keywords or set():
            # 通用异常名经分词后也从这条路进来（`importerror` 是合法 token），同样不给分：
            # 实测它在这一条路上又贡献 0.4×96≈38 分，是签名路被堵后的第二条升格路径。
            if keyword in GENERIC_SIGNATURES:
                continue
            for index in self.token_index.get(keyword, []):
                idf = 1.0 + (total / (1 + len(self.token_index[keyword])))
                scores[index] = scores.get(index, 0.0) + 0.4 * idf
                matched_kws.setdefault(index, set()).add(keyword)

        ranked = sorted(scores.items(), key=lambda item: -item[1])
        chosen = [index for index, _ in ranked[:top_k]]
        for index in [index for index, _ in ranked
                      if self.issues[index]["is_postmortem"]][:precedent_k]:
            if index not in chosen:
                chosen.append(index)
        # 并集之后仍按分数降序，避免「保底名额」把低分项排到高分项前面造成阅读误导
        chosen.sort(key=lambda index: -scores[index])

        results = []
        for index in chosen:
            issue = self.issues[index]
            issue_keywords = matched_kws.get(index, set())
            core = sorted(
                keyword for keyword in issue_keywords
                if keyword not in provenance
                and keyword not in GENERIC_SIGNATURES
                and len(self.token_index.get(keyword, [])) <= core_df_limit)
            # 只有「带机制的签名」才算强证据，valueerror 这类异常类名不算（见 GENERIC_SIGNATURES）
            mechanism_signatures = sorted(
                signature for signature in matched_sigs.get(index, set())
                if signature not in GENERIC_SIGNATURES)
            results.append({
                "issue": issue,
                "score": round(scores[index], 2),
                "matched_signatures": sorted(matched_sigs.get(index, set())),
                "mechanism_signatures": mechanism_signatures,
                "matched_keywords": sorted(issue_keywords)[:8],
                # 核心症状词：稀有、非出处、且非通用异常名，只有这类命中才算「同现象」
                "core_keywords": core[:8],
                # 强 = 命中带机制的签名；中 = 命中核心症状词；弱 = 只有通用词/出处名
                "evidence_strength": ("强" if mechanism_signatures
                                      else "中" if core else "弱"),
                # 有根因小节的才算真正的先例（其余多为需求/讨论贴）
                "has_root_cause": issue["is_postmortem"],
            })
        return results

    def resolve_numbers(self, numbers: list) -> list:
        """按 issue 号取出条目（供「人工确认的历史关联」这类**策展**链接使用）。

        辞书化匹配（match）靠词面，天然无法把「模型缓存未命中」与「找不到缓存模型」
        这种换了说法的同一现象连起来；这类已知关联必须**人工策展**在知识表里，
        本方法负责把策展的号解析成可渲染的条目。未索引到的号会被显式标出，
        不静默丢弃——策展表里写错号也是一种需要暴露的缺陷。
        """
        by_number = {issue["number"]: issue for issue in self.issues}
        resolved = []
        for number in numbers or []:
            issue = by_number.get(number)
            if issue is None:
                resolved.append({"number": number, "missing": True})
                continue
            resolved.append({
                "number": number,
                "missing": False,
                "title": issue["title"],
                "state": issue["state"],
                "url": issue["url"],
                "root_cause": issue["sections"].get("root_cause"),
                "root_cause_source": issue.get("root_cause_source"),
                # 带上平台侧信号，策展关联才能参与「日志侧判 code 但先例指向平台」的冲突判定
                "platform_signals": issue.get("platform_signals") or [],
            })
        return resolved

    def cluster_mentions(self) -> dict:
        """统计每个集群标识在历史 issue 里被提及的次数——给「该集群历史高发」提供依据。"""
        counter: dict = {}
        for issue in self.issues:
            for signature in issue["signatures"]:
                if re.match(r'^(?:cn12|hk001|hk-001|gy00[1-5]|sh-00[12]|sh001)', signature):
                    counter[signature] = counter.get(signature, 0) + 1
        return dict(sorted(counter.items(), key=lambda item: -item[1]))
