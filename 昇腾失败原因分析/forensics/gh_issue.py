#!/usr/bin/env python3
"""按 owner=infra 的失败 job 建 GitHub issue —— 一个 job 一个，正文脱敏。

## 为什么要脱敏

目标仓 `TangJia025/general` 是 **PUBLIC**，而 case 里带着内网标识：集群名、节点名
（实测多为内网 IP，如 172.16.1.108）、镜像仓库、kubeconfig 路径（含工号）、
serviceaccount、runner 名。仓库 .gitignore 已经为同一原因写过禁令：
「冻结的日志窗口含内网集群名/节点名/镜像地址，而本仓库是 PUBLIC」。

## 为什么是两层，而不是一层

这些标识**不只在固定字段里**：`verdict.root_cause` / `verdict.basis` 是 LLM 生成的
自由文本，实测 `basis` 里就嵌着节点名（「……节点 `gy005`」）。所以：

  第一层 · 字面量：从 case 的结构化字段里**准确**取出必须抹掉的值，再按字面量替换 ——
                    连它们出现在自由文本里的那次一并覆盖。这一层不依赖任何猜测，是保底。
  第二层 · 形状  ：正则匹配已知形状（集群名 / 私网 IP / 镜像仓库 / runner 名 / 凭据路径），
                    覆盖「结构化字段里没有、但自由文本里冒出来」的情况
                    （例如历史 issue 提到了另一个集群）。

只做第二层会漏（形状是猜的），只做第一层也会漏（自由文本可能提到别的集群）。两层叠加。

## 一条容易踩的线

`vllm-project` 是**公开的 GitHub org**，CI 链接里就有它。把 queried_namespaces 里的
`vllm-project` 当敏感值照抹，会把链接打成 `https://github.com/<已脱敏>/vllm-ascend/...`。
所以字面量收集有一条护栏：**出现在公开 `link` 里的值不收**。

## 失败不阻断通知

建 issue 失败只记警告，邮件照发 —— 与 notify() 的口径一致：通知是最后一跳，
不该因为附属动作失败而整条断掉。
"""
import json
import os
import re
import subprocess

# ---------------------------------------------------------------- 第二层：形状

# RFC1918 私网地址。实测节点名大多是这个形状，而不是主机名 —— 只按主机名写正则会漏。
_PRIVATE_IP = re.compile(
    r'\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}'
    r'|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}'
    r'|192\.168\.\d{1,3}\.\d{1,3})\b'
)

# 顺序即优先级：更具体的形状放前面，避免被通用形状先咬掉一半留下残片。
SHAPE_RULES = (
    # kubeconfig 路径与文件名（含工号）。必须早于集群名规则 ——
    # 文件名里嵌着集群名，先抹集群名会把这条切碎、剩下的工号反而露出来。
    (re.compile(r'[\w./~-]*tangjia_upload_\d+[\w./~-]*'), '<kubeconfig>'),
    (re.compile(r'[\w./~-]*kconf[\w./~-]*'), '<kubeconfig 路径>'),
    (re.compile(r'ascend-[a-z0-9]+-\d+-cluster', re.IGNORECASE), '<集群>'),
    (re.compile(r'swr\.[\w.-]+', re.IGNORECASE), '<镜像仓库>'),
    (re.compile(r'sa-for-[\w-]+', re.IGNORECASE), '<服务账号>'),
    # runner 名。注意公开的 runner **标签**（linux-aarch64-a3-800t-0）不含 `-runner-`，
    # 不会被这条吃掉 —— 那是权威标签表里的公开值，抹了反而丢信息。
    (re.compile(r'\b[\w-]+-runner-\w+\b'), '<runner>'),
    # 命名空间写全名才抹。单抹 `vllm-project` 会打坏 CI 链接（它是公开 org）
    (re.compile(r'\bvllm-project-vllm-ascend\b'), '<命名空间>'),
    (re.compile(r'\bgy\d{3}\b', re.IGNORECASE), '<节点>'),
    (_PRIVATE_IP, '<内网IP>'),
)

REDACTED = '<已脱敏>'

# 收进 issue 正文时每段的长度上限，避免模型跑飞把正文撑爆
FIELD_LIMIT = 2000


def scrub(text, literals=()):
    """把一段文本里的内网标识抹成占位符。**写进 issue 的每一段都要过这里。**"""
    if text is None:
        return ''
    result = str(text)
    for value in literals:
        if value and value in result:
            result = result.replace(value, REDACTED)
    for pattern, placeholder in SHAPE_RULES:
        result = pattern.sub(placeholder, result)
    return result.strip()


# ---------------------------------------------------------------- 第一层：字面量


def harvest_sensitive(case):
    """从结构化字段里取出「必须原样抹掉」的字面量，长的在前。

    刻意**不**收 labels（linux-aarch64-a3-800t-0）—— 那是公开的 runner 标签，
    来自权威标签表，属于该保留的信息。收的是内网侧的值。
    """
    values = set()
    # 公开链接里出现过的值一定不敏感（典型：vllm-project）。照抹会打坏链接。
    public_text = str(case.get('link') or '')

    def add(value):
        if value is None:
            return
        text = str(value).strip()
        # 太短的不收：抹掉「1」这种会把正文打成筛子
        if len(text) > 1 and text not in public_text:
            values.add(text)

    cluster = case.get('cluster') or {}
    add(cluster.get('cluster_name'))
    add(cluster.get('kubeconfig_path'))
    add(cluster.get('filename'))
    add(cluster.get('candidate_note'))
    for item in cluster.get('candidates') or []:
        add(item)
    for names in (cluster.get('queried_namespaces') or {}).values():
        for item in names or []:
            add(item)

    identity = (cluster.get('identity') or {}).get('identity')
    add(identity)
    if identity:
        # system:serviceaccount:ascend:sa-for-xxx —— 后半段单独出现时也要抹
        add(str(identity).split(':')[-1])

    pod = cluster.get('pod_evidence') or {}
    add(pod.get('node'))
    add(pod.get('namespace'))
    add(pod.get('pod'))
    for container in pod.get('containers') or []:
        # 只收镜像，**不收容器名** —— 容器名可能是 runner 这种通用词，抹了会误伤
        add(container.get('image'))

    add(case.get('runner_name'))

    for item in case.get('history') or []:
        issue = (item or {}).get('issue') or {}
        for signature in issue.get('signatures') or []:
            add(signature)

    # 长串先抹：短串先命中会把长串切碎，留下残片
    return sorted(values, key=len, reverse=True)


# ---------------------------------------------------------------- issue 渲染


def issue_marker(job_id):
    """埋在正文里的去重标记。查重按它搜，不靠标题（标题会被脱敏改动）。"""
    return '<!-- npu-ci-job:%s -->' % job_id


def build_issue_title(case, literals=()):
    bucket = scrub(case.get('bucket'), literals) or '未归类'
    return '[昇腾CI][infra] %s — job %s' % (bucket[:80], case.get('job_id'))


def _bullet(label, value):
    return '- **%s**：%s' % (label, value) if value else ''


def build_issue_body(case, literals=None):
    """渲染 issue 正文。只取公开字段 + 脱敏，cluster 整块不进正文。"""
    if literals is None:
        # 允许调用方省略：自己收一遍，保证「忘了传 literals」也不会漏脱敏
        literals = harvest_sensitive(case)

    def clean(value, limit=FIELD_LIMIT):
        return scrub(value, literals)[:limit]

    verdict = case.get('verdict') or {}

    lines = ['## 失败 job', '']
    for label, raw in (
        ('工作流', case.get('workflow')),
        ('job 名', case.get('job_name')),
        ('job id', case.get('job_id')),
        ('run id', case.get('run_id')),
        ('CI 链接', case.get('link')),
        ('芯片', case.get('chip')),
        ('失败步骤', case.get('step')),
        ('归类', case.get('bucket')),
        ('归属', case.get('owner')),
    ):
        value = clean(raw, 300)
        if value:
            lines.append(_bullet(label, value))

    root_cause = clean(verdict.get('root_cause'))
    if root_cause:
        lines += ['', '## 根因', '', root_cause]

    basis = [clean(item, 600) for item in (verdict.get('basis') or [])]
    basis = [item for item in basis if item]
    if basis:
        lines += ['', '## 判据', '']
        lines += ['- %s' % item for item in basis]

    signature = clean(case.get('sig'), 600)
    if signature:
        lines += ['', '## 证据签名', '', '```', signature, '```']

    lines += [
        '',
        '---',
        '由昇腾 CI 失败分析工具自动创建（`npu_ci_watch.py` → `npu_ci_notify.py`）。',
        '正文已脱敏：内网集群名 / 节点名 / 私网 IP / 镜像仓库 / kubeconfig 路径 / '
        'serviceaccount / runner 名均以占位符替代。',
        issue_marker(case.get('job_id')),
    ]
    return '\n'.join(lines)


# ---------------------------------------------------------------- 建 issue


def default_state_path():
    """与 watcher 的 --state-dir 默认值保持一致（.forensics_state，已 gitignore）。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, '.forensics_state', 'issues.json')


def load_state(path=None):
    path = path or default_state_path()
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def save_state(state, path=None):
    path = path or default_state_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def run_gh(arguments, stdin_text=None, timeout=90.0):
    """调 gh CLI。返回 (returncode, stdout, stderr)；起不来时 returncode 为 -1。"""
    try:
        proc = subprocess.run(['gh'] + list(arguments), input=stdin_text,
                              capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=timeout)
    except FileNotFoundError:
        return -1, '', '找不到 gh 可执行文件（PATH 里没有）'
    except subprocess.TimeoutExpired:
        return -1, '', 'gh 超时（%.0fs）' % timeout
    return proc.returncode, proc.stdout or '', proc.stderr or ''


def find_existing_issue(repo, job_id, runner=run_gh):
    """按正文里的去重标记找已有 issue（含已关闭的）。找不到返回 None。"""
    code, out, _ = runner(['issue', 'list', '--repo', repo, '--state', 'all',
                           '--limit', '50', '--search', str(job_id),
                           '--json', 'number,url,title,body'])
    if code != 0:
        return None
    try:
        found = json.loads(out or '[]')
    except ValueError:
        return None
    marker = issue_marker(job_id)
    for item in found:
        if isinstance(item, dict) and marker in str(item.get('body') or ''):
            return item.get('url')
    return None


def create_issue(repo, title, body, runner=run_gh):
    """建 issue 并返回 URL；失败返回 None。正文走 stdin，避免命令行长度与转义问题。"""
    code, out, err = runner(['issue', 'create', '--repo', repo,
                             '--title', title, '--body-file', '-'], stdin_text=body)
    if code != 0:
        return None, (err or out or '').strip().splitlines()[-1:] or ['（无输出）']
    for token in (out or '').split():
        if token.startswith('http'):
            return token.strip(), []
    return None, ['gh 未回显 issue URL']


def ensure_issue(case, repo, state, runner=run_gh):
    """确保这个 job 有且只有一个 issue。返回 (url, 备注)；url 为 None 表示没建/没找到。

    查重三层：本地映射 → 远端按标记搜 → 才建。本地映射是快路径，远端搜索兜住
    「state 文件丢了 / 换机器跑」的情况，否则会重复建 issue。
    """
    job_id = case.get('job_id')
    if not job_id:
        return None, 'case 没有 job_id，跳过'

    key = str(job_id)
    if state.get(key):
        return state[key], None

    existing = find_existing_issue(repo, job_id, runner=runner)
    if existing:
        state[key] = existing
        return existing, '已存在，复用'

    url, problems = create_issue(repo, build_issue_title(case, harvest_sensitive(case)),
                                 build_issue_body(case), runner=runner)
    if not url:
        return None, '建 issue 失败：%s' % (problems[0] if problems else '未知')
    state[key] = url
    return url, None


def ensure_infra_issues(cases, repo, state, runner=run_gh):
    """对传入的 case 里 owner=infra 的那些逐个建 issue。

    只处理**调用方选中的** case（而不是报告里全部）—— 不然按 job_ids 过滤后仍会给
    本次没通知的 job 建 issue，凭空多出一批没人跟的条目。

    返回 (job_id → url 映射, 日志行列表)。**任何失败都只进日志，不抛异常** ——
    调用方是通知链路，不能因为建 issue 失败而发不出邮件。
    """
    mapping = {}
    notes = []
    for case in cases or []:
        if not isinstance(case, dict):
            continue
        if str(case.get('owner') or '').strip().lower() != 'infra':
            continue
        job_id = case.get('job_id')
        try:
            url, note = ensure_issue(case, repo, state, runner=runner)
        except Exception as exc:                       # noqa: BLE001 —— 兜底，见 docstring
            url, note = None, '建 issue 抛异常：%s: %s' % (type(exc).__name__, exc)
        if url:
            mapping[str(job_id)] = url
            notes.append('issue %s：job %s%s' % (url, job_id, '（%s）' % note if note else ''))
        else:
            notes.append('⚠️ job %s 未建 issue：%s' % (job_id, note))
    return mapping, notes
