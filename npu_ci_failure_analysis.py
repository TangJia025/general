#!/usr/bin/env python3
"""
NPU CI workflow 失败分析工具
静态筛出跑在 NPU 上的 CI workflow → 近 N 天执行记录 → 抽样失败 run 定位 NPU job → 日志根因分类 → top3

适用仓库架构差异（已实测校准）：
  - vllm-ascend: NPU job 直接跑在 linux-aarch64-{a2,a3,a5,310p}-N runner 上
  - triton-ascend: NPU job 跑在 linux-aarch64-a3-4 / linux-amd64-a5-4（a5 昇腾950 在 amd64！），
    顶层 ci.yml 不含直接特征，靠 uses: integration-tests-ascend.yml 传递
  - verl: 大量 *_ascend.yml，全 aarch64 runner；docker-build-ascend-* 是 CD 排除
  - sglang: 失败常发生在 CPU 门禁 pr-gate（ubuntu-latest），NPU job 被 skip —— 无 NPU 失败 job 时 fallback 到该 run 的失败 job

用法:
  python3 npu_ci_failure_analysis.py                                  # 默认 vllm-project/vllm-ascend 近7天
  python3 npu_ci_failure_analysis.py --repo triton-lang/triton-ascend
  python3 npu_ci_failure_analysis.py --repo sgl-project/sglang --samples 30

依赖: gh CLI 已认证（可读目标仓库）。工作流文件会自动下载到临时目录，可用 --workflow-dir 复用缓存。
"""
import argparse, subprocess, json, re, os, gzip, sys, tempfile, base64, datetime
from collections import Counter, defaultdict

def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default="vllm-project/vllm-ascend", help="owner/repo")
    ap.add_argument("--since", default=None, help="起始日期 YYYY-MM-DD，默认近7天")
    ap.add_argument("--samples", type=int, default=40, help="最多分类的失败日志数（默认40）")
    ap.add_argument("--sample-per-wf", type=int, default=8, help="每个 workflow 抽样失败 run 数（默认8）")
    ap.add_argument("--workflow-dir", default=None, help="已下载 workflow 文件目录（复用缓存）；缺省自动下载到临时目录")
    ap.add_argument("--npu-label-pattern",
                    default=r"linux-(?:aarch64|amd64)-(?:a\d[\w-]*|310p)-\d",
                    help="NPU runner 标签正则（job labels 过滤）。覆盖 aarch64 与 a5/amd64 等昇腾芯片形态")
    ap.add_argument("--tail-lines", type=int, default=1200, help="日志分类扫描的尾部窗口行数（默认1200）")
    ap.add_argument("--sample-cancelled", type=int, default=5, help="每个 workflow 采样 cancelled run 数（默认5）")
    ap.add_argument("--report-dir", default="npu_ci_reports", help="报告输出目录，每次运行生成带时间戳的 md 文件（默认 npu_ci_reports/）")
    ap.add_argument("--summary-file", default="npu_ci_failure_report.md",
                    help="精简版报告路径，每次运行自动更新对应仓库章节（默认 npu_ci_failure_report.md）")
    ap.add_argument("--infra-store", default=os.path.join("npu_ci_reports", "infra_snapshot.json"),
                    help="跨仓基础设施信号（排队/cancelled 统计）持久化文件，供报告自动聚合跨仓表格（默认 npu_ci_reports/infra_snapshot.json）")
    return ap.parse_args()

ARGS = parse_args()
OWNER, REPO = ARGS.repo.split("/")
if ARGS.since:
    SINCE = ARGS.since
else:
    from datetime import date, timedelta
    SINCE = (date.today() - timedelta(days=7)).isoformat()
NPU_LABEL = ARGS.npu_label_pattern

# ---------- 输出双写：终端 + 带时间戳的报告文件（历史回溯） ----------
import sys as _sys
_T0 = datetime.datetime.now()          # 分析开始时间
REPORT_DIR = ARGS.report_dir
os.makedirs(REPORT_DIR, exist_ok=True)
_ts = _T0.strftime("%Y%m%d_%H%M%S")
report_path = os.path.join(REPORT_DIR, f"npu_ci_failure_report_{REPO}_{_ts}.md")
_report_file = open(report_path, "w", encoding="utf-8")
_orig_stdout = _sys.stdout
_report_file.write(f"# NPU CI 失败分析报告\n\n"
                   f"- 分析开始: {_T0.strftime('%Y-%m-%d %H:%M:%S')}\n"
                   f"- 仓库: `{ARGS.repo}`\n"
                   f"- 起始日期: `{SINCE}`\n"
                   f"- 参数: samples={ARGS.samples}, sample_per_wf={ARGS.sample_per_wf}, "
                   f"sample_cancelled={ARGS.sample_cancelled}, tail_lines={ARGS.tail_lines}\n\n")
_report_file.flush()
class _Tee:
    """同时写终端与报告文件；文件逐行落盘，脚本中断也能保留已输出内容"""
    def write(self, s):
        if not _orig_stdout.closed:
            _orig_stdout.write(s)
        _report_file.write(s)
        _report_file.flush()
        return len(s)
    def flush(self):
        if not _orig_stdout.closed:
            _orig_stdout.flush()
        if not _report_file.closed:
            _report_file.flush()
_sys.stdout = _Tee()

# ---------- 跨仓基础设施信号：排队/cancelled 统计持久化（报告自动聚合跨仓表格的数据源，替代手工快照） ----------
INFRA_STORE = ARGS.infra_store
os.makedirs(os.path.dirname(INFRA_STORE) or ".", exist_ok=True)

def load_infra_store():
    if os.path.exists(INFRA_STORE):
        try:
            with open(INFRA_STORE, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {"repos": {}}

def save_infra_store(store):
    with open(INFRA_STORE, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False, indent=2)

def gh(*args, binary=False):
    r = subprocess.run(["gh", "api", *args], capture_output=True)
    if r.returncode != 0:
        return b"" if binary else ""
    return r.stdout if binary else r.stdout.decode()

# ---------- 0. 准备 workflow 文件 ----------
def prepare_workflows():
    if ARGS.workflow_dir and os.path.isdir(ARGS.workflow_dir):
        return ARGS.workflow_dir
    d = tempfile.mkdtemp(prefix="npu_ci_")
    for entry in json.loads(gh(f"repos/{OWNER}/{REPO}/contents/.github/workflows")):
        name = entry["name"]
        if not name.endswith((".yaml", ".yml")):
            continue
        b64 = gh(f"repos/{OWNER}/{REPO}/contents/.github/workflows/{name}", binary=True)
        try:
            data = json.loads(b64)["content"]
            open(os.path.join(d, name), "w", encoding="utf-8").write(
                base64.b64decode(data).decode("utf-8", errors="ignore"))
        except Exception:
            pass
    return d

# ---------- Step 1: 静态筛选 NPU CI workflow ----------
# CD/辅助类文件名关键词，无论命中什么特征都排除（build-docker 也引用 ascend-ci 镜像，必须排除）
CD_KEYWORDS = ("release", "build-docker", "docker-build", "wheels", "create_release",
               "sync-", "sync_", "auto-label", "stale", "docs", "documentation",
               "pre-commit", "precommit", "check-pr", "pr-title", "dco", "ocr",
               "rebuild", "protected", "llvm-build", "auto-")

# 强 NPU 特征：直接硬件信号（aarch64 runner / npu-smi），或 动态runner+CANN容器（NPU 测试执行模板，
# 如 vllm _selected_tests.yaml 的 matrix.group.runner 就是 linux-aarch64-*）。cann_image 单独出现
# 不可靠（CPU runner 也能用 CANN 容器做编译检查，如 triton DynamicCVPipeline-ci）
def is_strong(feats):
    return any(x in feats for x in ("direct_aarch64", "npu_smi")) or \
           ("dynamic_runner" in feats and "cann_image" in feats)

def scan_features(path):
    """返回 (特征集合, uses 列表, 原始文本)"""
    try:
        txt = open(path, encoding="utf-8", errors="ignore").read()
    except FileNotFoundError:
        return set(), [], ""
    feats = set()
    if re.search(r'runs-on:\s*linux-aarch64', txt):
        feats.add("direct_aarch64")
    if re.search(r'\bnpu-smi\b', txt):
        feats.add("npu_smi")
    if re.search(r'swr\.cn-southwest-2\.myhuaweicloud\.com[^\n]*ascend-ci', txt) or \
       re.search(r'ascend-ci[^\n]*swr\.cn-southwest-2\.myhuaweicloud\.com', txt):
        feats.add("cann_image")
    if re.search(r'runs-on:\s*\$\{', txt):
        feats.add("dynamic_runner")
    uses = re.findall(r'uses:\s*\./\.github/workflows/([\w.-]+\.ya?ml)', txt)
    return feats, uses, txt

WF_DIR = prepare_workflows()
info = {}            # 文件名 -> (特征, uses)
for f in sorted(os.listdir(WF_DIR)):
    if not f.endswith(('.yaml', '.yml')):
        continue
    if any(k in f for k in CD_KEYWORDS):
        continue
    feats, uses, txt = scan_features(os.path.join(WF_DIR, f))
    info[f] = (feats, uses)

strong_files = {f for f, (feats, _) in info.items() if is_strong(feats)}

def transitively_uses_npu(f):
    """f 是否（间接）uses 了某个强 NPU 特征文件（如 triton ci.yml → integration-tests-ascend.yml）"""
    seen, stack = set(), [f]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur in strong_files:
            return True
        stack.extend(info.get(cur, (set(), []))[1])
    return False

candidates = {}
for f, (feats, uses) in info.items():
    if is_strong(feats):
        candidates[f] = feats
    elif 'dynamic_runner' in feats or 'cann_image' in feats:
        # 只有弱特征 + 文件名带 npu/ascend 才算（裸 dynamic_runner 会污染 AMD/ROCm/release）
        if re.search(r'npu|ascend', f):
            candidates[f] = feats
    elif transitively_uses_npu(f):
        candidates[f] = feats

print(f"=== Step1 静态筛出 NPU CI 候选: {len(candidates)} 个 ===")
for f, h in sorted(candidates.items()):
    print(f"  {f:48s} {','.join(sorted(h)) or 'uses_npu_exec'}")

# ---------- Step 2: 近 N 天 runs 记录 ----------
def has_standalone_trigger(txt):
    """workflow_call-only 的可复用 workflow 无独立 run 记录，跳过"""
    return bool(re.search(r'^\s*(push|pull_request|pull_request_target|workflow_dispatch|schedule|'
                          r'issue_comment|repository_dispatch|workflow_run|merge_group):', txt, re.M))

print(f"\n=== Step2 近({SINCE}~) 执行记录 ===")
wf_stats = {}
for f, feats in candidates.items():
    if f.startswith('_') or not has_standalone_trigger(open(os.path.join(WF_DIR, f), encoding="utf-8", errors="ignore").read()):
        continue
    data = gh(f"repos/{OWNER}/{REPO}/actions/workflows/{f}/runs?per_page=100&created=%3E{SINCE}")
    try:
        runs = json.loads(data)['workflow_runs']
    except Exception:
        continue
    c = Counter()
    for r in runs:
        if r['status'] == 'completed':
            c[r['conclusion']] += 1
    wf_stats[f] = (len(runs), dict(c))
    tot = len(runs); s = c['success']; fl = c['failure']
    rate = f"{s/(s+fl)*100:.0f}%" if (s+fl) else "--"
    print(f"  {f:48s} total={tot:4d} success={s:4d} failure={fl:4d} cancelled={c['cancelled']:4d} 成功率={rate}")

# ---------- Step 3: 失败 run → 定位 NPU job（含门禁 fallback）；另采样 cancelled run 与排队时长 ----------
print(f"\n=== Step3 对失败 run 抽样（按失败量加权），定位失败 job ===")
SAMPLE_PER_WF = ARGS.sample_per_wf
failed_jobs = []      # (wf, run_id, job_id, job_name, is_npu)
cancelled_jobs = []   # (wf, run_id, job_id, job_name, never_started)
queue_times = []      # 秒：run.created_at → NPU job.started_at（runner 排队时长）
fallback_jobs = 0
for f, (_, counts) in sorted(wf_stats.items(), key=lambda kv: -kv[1][1].get('failure', 0)):
    runs = json.loads(gh(f"repos/{OWNER}/{REPO}/actions/workflows/{f}/runs?per_page=100&created=%3E{SINCE}"))['workflow_runs']
    failed = [r for r in runs if r['conclusion'] == 'failure']
    for r in failed[:SAMPLE_PER_WF]:
        jobs = json.loads(gh(f"repos/{OWNER}/{REPO}/actions/runs/{r['id']}/jobs?per_page=100"))['jobs']
        npu_fail = [j for j in jobs
                    if any(re.search(NPU_LABEL, l) for l in (j.get('labels') or []))
                    and j['conclusion'] == 'failure']
        if npu_fail:
            for j in npu_fail:
                failed_jobs.append((f, r['id'], j['id'], j['name'], True))
        else:
            # 无 NPU 失败 job（NPU job 常被 skip），fallback 到该 run 的全部失败 job（多为 CPU 门禁）
            for j in jobs:
                if j['conclusion'] == 'failure':
                    failed_jobs.append((f, r['id'], j['id'], j['name'], False))
                    fallback_jobs += 1
        # 排队时长：run 创建 → NPU job 实际启动。长排队 = runner 池不足（调度，infra 侧）
        if r.get('created_at'):
            t0 = datetime.datetime.fromisoformat(r['created_at'].replace('Z', '+00:00'))
            for j in jobs:
                if j.get('started_at') and any(re.search(NPU_LABEL, l) for l in (j.get('labels') or [])):
                    t1 = datetime.datetime.fromisoformat(j['started_at'].replace('Z', '+00:00'))
                    queue_times.append((t1 - t0).total_seconds())
    # cancelled run 采样：cancelled 常对应 runner 挂掉/节点故障（infra），而非业务失败
    cancelled = [r for r in runs if r['conclusion'] == 'cancelled']
    for r in cancelled[:ARGS.sample_cancelled]:
        jobs = json.loads(gh(f"repos/{OWNER}/{REPO}/actions/runs/{r['id']}/jobs?per_page=100"))['jobs']
        for j in jobs:
            if j['conclusion'] == 'cancelled':
                cancelled_jobs.append((f, r['id'], j['id'], j['name'], not j.get('started_at')))
n_sampled = sum(min(c.get('failure', 0), SAMPLE_PER_WF) for _, c in wf_stats.values())
print(f"  共抽样失败 run {n_sampled} 个 → 失败 job {len(failed_jobs)} 个（其中 NPU job {len(failed_jobs)-fallback_jobs}，门禁 fallback {fallback_jobs}）")
n_never = sum(1 for c in cancelled_jobs if c[4])
print(f"  cancelled run 采样 {len(cancelled_jobs)} 个 job，其中从未启动/未分配到 runner {n_never} 个"
      f"（{n_never and '→ 调度/资源问题' or '→ 多为主动取消/上游中断'}）")
if queue_times:
    q = sorted(queue_times)
    med = q[len(q)//2] / 60
    over30 = sum(1 for t in queue_times if t > 1800)
    print(f"  NPU runner 排队时长: 样本 {len(q)}，中位 {med:.0f}min，最长 {q[-1]/60:.0f}min，"
          f">30min 有 {over30} 个（>30min 提示 runner 池不足，infra 侧）")

# 持久化本仓 infra 统计到快照存储（跨仓表格自动聚合的数据源，替代手工快照）
infra_store = load_infra_store()
repo_entry = infra_store.setdefault("repos", {}).setdefault(ARGS.repo, {})
if queue_times:
    q = sorted(queue_times)
    repo_entry["queue"] = {"samples": len(q),
                           "median_min": round(q[len(q)//2]/60, 1),
                           "max_min": round(q[-1]/60, 1),
                           "over_30min": sum(1 for t in q if t > 1800)}
else:
    repo_entry["queue"] = {"samples": 0, "median_min": None, "max_min": None, "over_30min": 0}
repo_entry["cancelled"] = {"samples": len(cancelled_jobs), "never_started": n_never}
repo_entry["since"] = SINCE
repo_entry["snapshot_at"] = _T0.isoformat()
infra_store["snapshot_at"] = _T0.isoformat()
save_infra_store(infra_store)

# ---------- Step 4: 下载日志 → 根因分类 ----------
# 基于真实日志校准的精准模式，顺序即优先级；只扫尾部窗口（前 80% 是安装/构建噪音）
# owner: infra=基础设施(资源/调度/网络) / code=业务方 / mixed=需二次判定
BUCKETS = [
    (r'FAILED:\s*\[code=1\]|\berror: no viable|\berror:.*(?:expected|undeclared|cannot convert|no member named)|'
     r'error:.*required.*include|\bclang\+\+.*error:|Error:.*CMake Error', "编译失败(C++/MLIR)", "code"),
    (r'NPU function error|aclnn\w*\s*failed|error code is \d+', "昇腾算子执行错误(ACL)", "mixed"),
    (r'cannot open shared object file|\.so: cannot open|torch_extensions.*\.so', "自定义算子so缺失(csrc构建)", "mixed"),
    (r'(?:SIGKILL|exit code 137|killed.*(?:OOM|137)|OOMKilled|Signal 9|container.*not found)', "进程被kill(OOM/超内存)", "mixed"),
    # 分布式拆两类：网络侧(HCCL timeout/连接重置)偏基础设施，编排侧(Ray actor 管理)偏业务方
    (r'HCCL\w*(?:error|timeout|failed)|Connection reset|broken pipe|CollectiveError', "分布式通信/网络(HCCL)", "infra"),
    (r'RayTaskError|ray\.exceptions|ActorDiedError|Actor.*(?:died|dead)|Driver of actor.*fail', "分布式通信/编排(Ray)", "code"),
    # 下载拆两类：内网镜像仓库(infra 直接责任) vs 外网(huggingface 等，mixed，可加镜像/缓存缓解)
    (r'Failed to download metadata for repo|repomd\.xml|Cannot download|apt-get update.*Failed to fetch|Failed to fetch.*mirror|yum.*Error', "内网镜像/仓库下载失败", "infra"),
    # 对外 API 调用失败：GitHub API 拉取失败（token/限流属 infra）；放在外网下载前，避免 404/503 细节被后者吞掉
    (r'Failed to fetch PR title', "GitHub API 调用失败", "infra"),
    (r'HfHubHTTPError|huggingface_hub|Curl error|503.*Service Unavailable|Github.*rate limit|404 Client Error', "模型/包下载失败(外网)", "mixed"),
    (r'timed out|TimeoutError|UV_HTTP_TIMEOUT|timeout.*exceed|timed out waiting', "超时", "mixed"),
    (r'out of memory|OOM error|MemoryError|memory.*not enough|aclrtMalloc failed|alloc.*failed.*memory', "OOM/显存不足", "mixed"),
    (r'No space left|disk full|ENOSPC', "磁盘不足", "infra"),
    (r'ImportError|ModuleNotFoundError|No module named', "依赖/安装(ImportError)", "code"),
    (r'AssertionError|E\s+assert', "断言失败(代码或精度)", "code"),
    # 门禁策略失败：pre-commit/ShellCheck 属同一类静态检查；CSRC 变更检查是 CI 策略门禁（业务方）
    (r'ShellCheck|shellcheck|pre-commit did not succeed', "静态检查(pre-commit/ShellCheck)", "code"),
    (r'CSRC build workflows changed', "CI 策略检查(CSRC 变更)", "code"),
    (r'AttributeError|TypeError|ValueError|KeyError|IndexError', "Python运行时错误", "code"),
    (r'Either .tests. or .config_file_path. must be provided|must be provided', "测试参数缺失(config未传入)", "code"),
    # 多节点编排层兜底：GitHub 日志只到 orchestrator 包装层，真错误在 k8s pod 里（owner 置 unknown）
    (r'failed to run script step', "多节点编排层包装失败(pod内真实错误)", "unknown"),
]
BUCKET_OWNER = {label: owner for _, label, owner in BUCKETS}
classified = Counter(); detail = []
bucket_link = defaultdict(list)   # 桶 -> [(样例 run 链接, 是否 NPU job)]，每桶最多3条
by_owner = defaultdict(Counter)   # owner -> 桶计数
logs_done = 0
for f, run_id, job_id, job_name, is_npu in failed_jobs:
    if logs_done >= ARGS.samples:
        break
    log = gh(f"repos/{OWNER}/{REPO}/actions/jobs/{job_id}/logs", binary=True)
    if not log:
        continue
    if log[:2] == b'\x1f\x8b':
        try:
            log = gzip.decompress(log)
        except Exception:
            pass
    logs_done += 1
    text = log.decode('utf-8', errors='ignore')
    # 丢弃 GitHub Actions 回显的脚本源码行（\x1b[36;1m 青色前缀），避免正则命中脚本里写死的报错文案造成误分类
    lines = [l for l in text.splitlines() if '\x1b[36;1m' not in l]
    tail = "\n".join(lines[-ARGS.tail_lines:])
    text_scan = tail if tail else text
    bucket = "未分类"; sig = ""
    for pat, label, _ in BUCKETS:
        m = re.search(pat, text_scan, re.I)
        if m:
            bucket = label
            sig = text_scan[max(0, m.start()-30):m.end()+30].replace("\n", " ")
            break
    if bucket == "未分类":
        m = re.search(r'(FAILED|Error|error:)', text_scan)
        sig = text_scan[max(0, m.start()-20):m.end()+40].replace("\n", " ") if m else "(无匹配)"
    owner = BUCKET_OWNER.get(bucket, "unknown")
    classified[bucket] += 1
    by_owner[owner][bucket] += 1
    tag = "NPU" if is_npu else "gate"
    link = f"https://github.com/{OWNER}/{REPO}/actions/runs/{run_id}/job/{job_id}"
    if len(bucket_link[bucket]) < 3:
        bucket_link[bucket].append((link, is_npu))
    detail.append((f, job_name[:32], tag, bucket, sig[:70], link, owner))
print(f"\n=== Step4 已分类日志 {logs_done} 份（[NPU]=NPU job / [gate]=CPU门禁fallback，附证据片段与 run 链接）===")
for d in detail:
    print(f"  [{d[6][:4]:4s}|{d[2]:4s}] {d[0][:26]:26s} {d[1]:30s} → {d[3]:16s} | {d[4]}")
    print(f"      run: {d[5]}")

# ---------- Step 5: top3 ----------
print(f"\n=== Top3 失败原因 ===")
if logs_done:
    for i, (bucket, cnt) in enumerate(classified.most_common(3), 1):
        links = bucket_link.get(bucket, [])
        link, npu = links[0] if links else ("", False)
        print(f"  #{i} {bucket}: {cnt} 次 ({cnt/logs_done*100:.0f}%)")
        print(f"      样例 run（{'NPU' if npu else 'gate'}）: {link}" if link else "")
    print(f"\n  全部分类:")
    for bucket, cnt in classified.most_common():
        links = bucket_link.get(bucket, [])
        link, npu = links[0] if links else ("", False)
        ow = BUCKET_OWNER.get(bucket, "unknown")
        print(f"    [{ow:6s}] {bucket}: {cnt} 次  [{'NPU' if npu else 'gate'}] {link}")
    print(f"\n  按 owner 汇总（infra=基础设施(资源/调度/网络) / code=业务方 / mixed=需二次判定 / unknown=未分类）:")
    for owner in ("infra", "code", "mixed", "unknown"):
        c = by_owner.get(owner)
        if c:
            detail_str = ", ".join(f"{b}×{n}" for b, n in c.most_common())
            print(f"    [{owner:6s}] 共 {sum(c.values())} 次: {detail_str}")
    print(f"\n  说明: 失败日志为抽样(上限{ARGS.samples}份)，百分比为样本内占比。[gate] 表示该失败在 CPU 门禁 job 上"
          f"（NPU job 被 skip），多节点测试的 GitHub 日志只有 orchestrator 层，真错误在 k8s pod 日志里。"
          f"\n  infra/code 为脚本根据日志特征自动判定，mixed 桶需人工结合 runner 配置/节点网络二次确认。")

# 分类完成后，把本仓基础设施相关失败桶（owner∈{infra,mixed}）写回快照存储，供跨仓汇总表聚合
repo_entry["infra_failures"] = [
    {"bucket": b, "count": c, "owner": BUCKET_OWNER.get(b, "unknown"),
     "links": [{"url": u, "npu": n} for u, n in bucket_link.get(b, [])]}
    for b, c in classified.most_common()
    if BUCKET_OWNER.get(b, "unknown") in ("infra", "mixed")
]
save_infra_store(infra_store)

def _fmt_links(links):
    """样例链接列表 -> 'url [NPU]、url [gate]'"""
    return "、".join(f"{l} [{'NPU' if n else 'gate'}]" for l, n in links)

def write_summary():
    """将本次运行的精简版章节写入 --summary-file。
    章节结构：meta → NPU CI workflows → 近一周成功率 → 全部失败原因分析(每桶1-3条链接) → 基础设施相关失败 Top3。
    章节用 HTML 注释标记包裹（<!-- @section:repo --> ... <!-- @/section:repo -->），
    脚本只替换本仓库那段，文件内其余手工内容保留——跑完各仓库即拼成完整跨仓报告。"""
    slug = ARGS.repo
    sec = f"<!-- @section:{slug} -->\n\n"
    sec += f"## {slug}（{SINCE} ~ {datetime.date.today().isoformat()}）\n\n"
    sec += f"- 分析时间: {_T0.strftime('%Y-%m-%d %H:%M:%S')} → {_T1.strftime('%H:%M:%S')}（{(_T1 - _T0).total_seconds():.0f}s）\n"
    sec += f"- 完整原始输出: `{report_path}`\n"
    sec += f"- 抽样 {n_sampled} 失败 run → {len(failed_jobs)} 失败 job（NPU {len(failed_jobs)-fallback_jobs} / 门禁 fallback {fallback_jobs}）\n"
    sec += f"- cancelled 采样 {len(cancelled_jobs)} job，未启动/未分配 runner {n_never} 个\n"
    if queue_times:
        q = sorted(queue_times)
        med = q[len(q)//2]/60; mx = q[-1]/60
        over = sum(1 for t in q if t > 1800)
        sec += f"- NPU runner 排队: 中位 {med:.0f}min，最长 {mx:.0f}min，>30min 有 {over} 个（>30min 提示 runner 池不足，infra 侧）\n"
    wf_names = sorted(candidates.keys())
    sec += f"\n**NPU CI workflows**：`{'`、`'.join(wf_names)}`\n\n"
    rates = []
    for f, (_, c) in sorted(wf_stats.items(), key=lambda kv: -kv[1][1].get('failure', 0)):
        s, fl = c.get('success', 0), c.get('failure', 0)
        rates.append(f"`{f}` {s/(s+fl)*100:.0f}%" if (s+fl) else f"`{f}` --")
    sec += f"**近一周成功率**：" + "、".join(rates) + "\n"

    # --- 全部失败原因分析：全桶表（样例 job 链接直接挂进表格列）+ owner 汇总 ---
    sec += "\n### 全部失败原因分析\n\n"
    sec += "| 排名 | 原因 | 次数 | 占比 | owner | 样例 job 链接 |\n|---|---|---|---|---|---|\n"
    if classified:
        for i, (b, c) in enumerate(classified.most_common(), 1):
            links = bucket_link.get(b, [])
            sec += (f"| #{i} | {b} | {c} | {c/logs_done*100:.0f}% | {BUCKET_OWNER.get(b, 'unknown')} | "
                    f"{_fmt_links(links) if links else '-'} |\n")
    else:
        sec += "| - | (无日志样本可分类) | - | - | - | - |\n"
    sec += "\n**owner 汇总**：" + "，".join(
        f"{o} {sum(by_owner[o].values())}" for o in ("infra", "code", "mixed", "unknown") if by_owner.get(o)) + "\n"

    # --- 基础设施相关失败 Top3：owner ∈ {infra, mixed}，按次数排名，附链接 ---
    infra_relevant = [b for b, _ in classified.most_common()
                      if BUCKET_OWNER.get(b, "unknown") in ("infra", "mixed")]
    sec += "\n### 基础设施相关失败 Top3\n\n"
    if infra_relevant:
        for i, b in enumerate(infra_relevant[:3], 1):
            links = bucket_link.get(b, [])
            sec += (f"{i}. **{b}**（{classified[b]} 次，owner={BUCKET_OWNER.get(b)}）："
                    f"{_fmt_links(links) if links else '(无链接)'}\n")
        if len(infra_relevant) > 3:
            sec += f"   其余：{'、'.join(f'{b}({classified[b]}次)' for b in infra_relevant[3:])}\n"
        sec += "\n> 说明：mixed 桶需结合 runner 配置/节点网络二次确认；调度/排队信号见上方 meta 行。\n"
    else:
        sec += "无（本次样本内无基础设施相关失败，均为业务方代码/测试问题）。\n"
    sec += f"\n<!-- @/section:{slug} -->\n"

    path = ARGS.summary_file
    sm, em = f"<!-- @section:{slug} -->", f"<!-- @/section:{slug} -->"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        if sm in content:
            s = content.index(sm)
            e = content.index(em) + len(em)
            new = content[:s] + sec.rstrip("\n") + content[e:]
        else:
            new = content.rstrip("\n") + "\n\n" + sec.rstrip("\n") + "\n"
    else:
        new = ("# NPU CI 失败分析报告（自动精简版）\n\n"
               f"> 由 `npu_ci_failure_analysis.py` 每次运行自动更新对应仓库章节（章节外内容手工保留），"
               f"完整原始输出见 `npu_ci_reports/`。\n\n" + sec)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    print(f"\n精简版报告已更新: {path}", file=_orig_stdout)

# 精简版报告的固定仓库顺序（章节排列 + 跨仓表格行），新增仓库可追加到末尾
REPO_ORDER = ["vllm-project/vllm-ascend", "sgl-project/sglang",
              "triton-lang/triton-ascend", "verl-project/verl"]

def _repo_key(repo):
    return REPO_ORDER.index(repo) if repo in REPO_ORDER else len(REPO_ORDER)

def update_infra_section():
    """用 infra 快照存储生成「跨仓基础设施信号」表格，更新精简版报告的 @section:infra-snapshot 标记段。
    表格按 REPO_ORDER 固定顺序聚合最新快照；>30min 提示 runner 池不足（infra 侧）。"""
    store = load_infra_store()
    repos = store.get("repos", {})
    if not repos:
        return
    rows = []
    for repo in sorted(repos, key=_repo_key):
        e = repos[repo]
        q, c = e.get("queue") or {}, e.get("cancelled") or {}
        n = q.get("samples", 0)
        med = f"{q['median_min']:.0f}min" if q.get("median_min") is not None else "--"
        mx = f"{q['max_min']:.0f}min" if q.get("max_min") is not None else "--"
        over = f"**{q.get('over_30min', 0)} 个**" if q.get("over_30min", 0) else "0 个"
        never = c.get("never_started") if c else None
        never_s = f"{never}" if never is not None else "--"
        rows.append(f"| {repo.split('/')[-1]:16s} | {n:5d} | {med:6s} | {mx:10s} | {over} | {never_s:3s} |")
    sec = ("## ⚠️ 跨仓基础设施信号（自动聚合）\n\n"
           "| 仓库 | 排队样本 | 中位 | 最长 | >30min | cancelled 未启动 |\n"
           "|---|---|---|---|---|---|\n" + "\n".join(rows) +
           f"\n\n> 数据来源：`{INFRA_STORE}`（各仓最近一次运行写入，快照 {store.get('snapshot_at', '—')[:16]}）。"
           ">30min 提示 runner 池不足（infra 侧）。")
    path = ARGS.summary_file
    sm, em = "<!-- @section:infra-snapshot -->", "<!-- @/section:infra-snapshot -->"
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    block = f"{sm}\n\n{sec}\n\n{em}"
    if sm in content:
        s = content.index(sm)
        e = content.index(em) + len(em)
        new = content[:s] + block + content[e:]
    else:
        anchor = "<!-- @section:"
        i = content.index(anchor) if anchor in content else len(content)
        new = content[:i] + block + "\n\n" + content[i:]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    print(f"跨仓基础设施信号表格已更新: {path}", file=_orig_stdout)

def update_infra_failure_summary():
    """从 infra 快照存储聚合「跨仓基础设施失败原因汇总」表，更新精简版报告的 @section:infra-failures 标记段。
    汇总四仓 owner∈{infra,mixed} 的失败桶：原因 | owner | 仓库 | 次数 | 失败 run/job 链接。
    首次运行插入到「跨仓基础设施信号」标记段之后；后续运行原位替换。"""
    store = load_infra_store()
    repos = store.get("repos", {})
    rows = []
    for repo in sorted(repos, key=_repo_key):
        for e in repos[repo].get("infra_failures", []):
            links = "、".join(f"{x['url']} [{'NPU' if x['npu'] else 'gate'}]" for x in e.get("links", []))
            rows.append([repo.split("/")[-1], e["bucket"], e.get("owner", "mixed"), e["count"], links])
    if not rows:
        return
    rows.sort(key=lambda r: -r[3])
    sec = ("## 🔧 跨仓基础设施失败原因汇总（infra/mixed）\n\n"
           "| 原因 | owner | 仓库 | 次数 | 失败 run/job 链接 |\n|---|---|---|---|---|\n" +
           "\n".join(f"| {b} | {o} | {r} | {c} | {l or '-'} |" for r, b, o, c, l in rows) +
           "\n\n> 数据来源：各仓最近一次运行写入 `--infra-store`；mixed 桶需结合 runner 配置/节点网络二次确认。")
    path = ARGS.summary_file
    sm, em = "<!-- @section:infra-failures -->", "<!-- @/section:infra-failures -->"
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    block = f"{sm}\n\n{sec}\n\n{em}"
    if sm in content:
        s = content.index(sm)
        e = content.index(em) + len(em)
        new = content[:s] + block + content[e:]
    else:
        anchor = "<!-- @/section:infra-snapshot -->"
        i = content.index(anchor) + len(anchor) if anchor in content else len(content)
        new = content[:i] + "\n\n" + block + content[i:]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    print(f"跨仓基础设施失败原因汇总表已更新: {path}", file=_orig_stdout)

def reorder_repo_sections(path):
    """把精简版报告里的仓库章节按 REPO_ORDER 重排（vllm → sglang → triton → verl）。
    只重排标记内仓库章节，标记外内容（头部/跨仓表格/方法学）保持原位。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    pat = re.compile(r"(<!-- @section:([^>\s]+) -->.*?<!-- @/section:\2 -->)", re.S)
    matches = list(pat.finditer(content))
    repo_blocks = [m for m in matches if m.group(2) in REPO_ORDER]
    if len(repo_blocks) < 2:
        return  # 少于 2 个仓库章节无需重排
    ordered = sorted(repo_blocks, key=lambda m: _repo_key(m.group(2)))
    # 确认当前顺序已一致则跳过，避免每次运行都触发写入
    if [m.group(2) for m in repo_blocks] == [m.group(2) for m in ordered]:
        return
    first, last = repo_blocks[0], repo_blocks[-1]
    body = "\n\n---\n\n".join(m.group(1) for m in ordered)
    new = content[:first.start()] + body + content[last.end():]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    print(f"仓库章节已按固定顺序重排（{' → '.join(r.split('/')[-1] for r in REPO_ORDER)}）: {path}",
          file=_orig_stdout)

_T1 = datetime.datetime.now()          # 分析结束时间
_report_file.write(f"\n---\n- 分析结束: {_T1.strftime('%Y-%m-%d %H:%M:%S')}\n"
                   f"- 耗时: {(_T1 - _T0).total_seconds():.0f}s\n")
_report_file.flush()
_report_file.close()
print(f"\n报告已写入: {report_path}", file=_orig_stdout)
write_summary()
update_infra_section()
update_infra_failure_summary()
reorder_repo_sections(ARGS.summary_file)
