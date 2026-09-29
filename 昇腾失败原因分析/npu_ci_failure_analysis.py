#!/usr/bin/env python3
"""
NPU CI workflow 失败分析工具（三步法取证，当前实现第 1、3 步，第 2 步留可插拔入口）

分析模型（昇腾 CI 资源团队反馈：仅看 workflow 日志看不出根因，必须结合集群侧）：
  [第1步] GitHub 侧真相：gh api 取失败 run → job → **失败步骤**（steps[]）+ runner pod 名
  [第2步] 集群侧真相：kubectl 查 runner pod 调度/排队（待专用 kubeconfig，见 --cluster-* 参数）
  [第3步] 综合定性：失败步骤决定归因路径，日志仅作证据来源

核心改进（经真实失败实证，job 106057742807）：
  - 步骤感知：读 jobs API 的 steps[]，取「序号最靠前的失败步骤」规避级联误判。
    此前该 job 被判【多节点编排层包装失败/unknown】，实为【pod 调度失败/卡 Pending/infra】。
  - 时间窗切分：用失败步骤的 started_at/completed_at 切日志，替代固定 tail-1200，
    可自动排除失败后的次生失败（如 `Upload failed`）与清理噪音。
  - 归因路径：Initialize containers / Stream logs 类步骤直接判 infra 且不读日志
    （`Stream logs` 失败时测试可能已通过）；Install/Build 类扫该步骤时间窗（自然覆盖日志头部，
    解决「只扫尾部漏掉依赖解析错误」）。

默认范围：vllm-project/vllm-ascend 的 A2/A3 卡失败（--chips a2,a3）。

用法（脚本位于 昇腾失败原因分析/，在任意 cwd 下运行均可，产物固定落在脚本同目录）:
  python3 昇腾失败原因分析/npu_ci_failure_analysis.py                    # 默认 vllm-ascend A2/A3 近7天
  python3 昇腾失败原因分析/npu_ci_failure_analysis.py --chips a2          # 只看 A2
  python3 昇腾失败原因分析/npu_ci_failure_analysis.py --chips ""          # 不限芯片（全 NPU runner）
  python3 昇腾失败原因分析/npu_ci_failure_analysis.py --repo sgl-project/sglang --chips "" --cross-repo
                                                                        # 多仓模式：需显式 --cross-repo 才更新跨仓表格

产出（默认均相对脚本所在目录）:
  npu_ci_reports/npu_ci_failure_report_<repo>_<ts>.md   完整原始输出（不入库）
  npu_ci_failure_report.md                              精简版报告（章节 slug 含芯片范围，不覆盖全量章节）
  npu_ci_reports/infra_snapshot.json                    跨仓基础设施信号快照（不入库，--cross-repo 时聚合）

适用仓库架构差异（已实测校准）：
  - vllm-ascend: NPU job 直接跑在 linux-aarch64-{a2,a2b*,a3,a5,310p,910b}-* runner 上
    （芯片族取自权威标签表 problem-labels.json，见 CHIP_FAMILY_TO_CHIP）
  - triton-ascend: NPU job 跑在 linux-aarch64-a3-4 / linux-amd64-a5-4（a5 昇腾950 在 amd64！），
    顶层 ci.yml 不含直接特征，靠 uses: integration-tests-ascend.yml 传递
  - verl: 大量 *_ascend.yml，全 aarch64 runner；docker-build-ascend-* 是 CD 排除
  - sglang: 失败常发生在 CPU 门禁 pr-gate（ubuntu-latest），NPU job 被 skip —— 无 NPU 失败 job 时 fallback 到该 run 的失败 job

依赖: gh CLI 已认证（可读目标仓库）。工作流文件会自动下载到临时目录，可用 --workflow-dir 复用缓存。
      第 2 步集群取证需专用只读 kubeconfig（环境变量 KUBECONFIG 指定），未配置时自动跳过。
"""
import argparse, subprocess, json, re, os, gzip, sys, tempfile, base64, datetime
from collections import Counter, defaultdict

# 脚本自身所在目录：报告/快照等产物默认与该脚本同目录存放，
# 这样在任意 cwd 下运行（仓库根或本目录内）产物路径都一致，不会因 cwd 变化而散落
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 对端节点日志（多节点 job 的第二日志证据源）。实现放 forensics/ 的理由与 classify_text 同：
# 本脚本是**全模块级执行**的（import 即联网跑整轮分析），纯逻辑只有放在那个包里才能被测试直接 import。
sys.path.insert(0, BASE_DIR)
from forensics import peer_logs as peer_ops        # noqa: E402

# ---------- 昇腾芯片族：is_npu 与 chip_of 的唯一真值源 ----------
# 权威来源 ascend-gha-runners/docs 的 docs/assets/problem-labels.json
# ——「仓库 → 合法 runner 标签」映射表（19 仓 102 个标签），全量回归见 tests/test_label_classification.py。
# ⚠️ 这张表同时决定 is_npu（job 是否 NPU job，决定报告里的 [NPU]/[gate] 标注）与 chip_of（--chips 过滤）。
#    两者先前用**两个独立正则**，实测出现过 [gate] 与 chip=a3 并存的自相矛盾（见设计文档 §2.3.1）。
#    新增芯片族只改这里。910b 就是实测漏掉的一整族（曾整族被判成 CPU 门禁，同 nightly-a3 那类 bug）。
# 键 = 标签里 `linux-{arch}-` 之后的芯片族 token；值 = 归一后的芯片名（a2b1/a2b3/a2b4 是 A2 的不同板型）
CHIP_FAMILY_TO_CHIP = {
    "a2": "a2", "a2b1": "a2", "a2b3": "a2", "a2b4": "a2",
    "a3": "a3",
    "a5": "a5",
    "310p": "310p",
    "910b": "910b",
}
KNOWN_CHIPS = tuple(sorted(set(CHIP_FAMILY_TO_CHIP.values())))

# arch 实测三种：arm64 当前只出现在 cpu 标签上，一并纳入以免将来漏判
NPU_ARCH = r"(?:aarch64|amd64|arm64)"
# 芯片族按长度降序，保证 a2b3 先于 a2 尝试（否则 a2 先匹配、后面接不上边界而整体失配）
NPU_CHIP_ALT = "|".join(sorted(CHIP_FAMILY_TO_CHIP, key=len, reverse=True))
# NPU runner 标签判定：
#   linux-{arch}-(?!cpu…) 先排除 CPU 池（实测 15 个形态：cpu-4-hk / cpu-4-cn12-001 / cpu-4-buildkit-… ）
#   (?:[\w-]*?-)?         可选中缀，覆盖 linux-aarch64-nightly-a3-16（实测 524 次，第二大池）
#   (?:芯片族)(?:-|$)      芯片族后必须是分隔符或结尾，避免 a3 在 a3xyz 这类长名里被部分匹配
NPU_LABEL_PATTERN = rf"linux-{NPU_ARCH}-(?!cpu(?:-|$))(?:[\w-]*?-)?(?:{NPU_CHIP_ALT})(?:-|$)"

def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default="vllm-project/vllm-ascend", help="owner/repo")
    ap.add_argument("--since", default=None, help="起始日期 YYYY-MM-DD，默认近7天")
    ap.add_argument("--samples", type=int, default=40, help="最多分类的失败日志数（默认40）")
    ap.add_argument("--sample-per-wf", type=int, default=8, help="每个 workflow 抽样失败 run 数（默认8）")
    ap.add_argument("--workflow-dir", default=None, help="已下载 workflow 文件目录（复用缓存）；缺省自动下载到临时目录")
    ap.add_argument("--npu-label-pattern", default=NPU_LABEL_PATTERN,
                    help="NPU runner 标签正则（job labels 过滤）。缺省由 CHIP_FAMILY_TO_CHIP 生成，"
                         "覆盖 aarch64/amd64/arm64 三种 arch、全部已知芯片族（含 910b）、"
                         "nightly- 中缀与无卡数后缀形态；已验证 102 个权威标签全量通过")
    ap.add_argument("--tail-lines", type=int, default=1200, help="日志分类扫描的窗口行数上限（默认1200）")
    ap.add_argument("--sample-cancelled", type=int, default=5, help="每个 workflow 采样 cancelled run 数（默认5）")
    ap.add_argument("--chips", default="a2,a3",
                    help="芯片范围（逗号分隔，如 a2,a3 或 a5,310p）；空字符串=不限芯片。"
                         "同时过滤 workflow 文件名与 job 的 runner 标签")
    ap.add_argument("--cross-repo", action="store_true",
                    help="启用四仓汇总机制（跨仓基础设施信号表、跨仓失败原因汇总表、仓库章节重排）。"
                         "缺省关闭：只产出本仓本章片范围的报告章节，不动跨仓表格")
    ap.add_argument("--cluster-kubeconfig", default=None,
                    help="[第2步预留] 昇腾 CI 专用只读 kubeconfig 路径。配置后脚本会尝试用 kubectl "
                         "反查失败 job 的 runner pod 调度状态；未配置则跳过集群取证（不阻塞第1、3步）")
    ap.add_argument("--no-step-window", action="store_true",
                    help="关闭步骤时间窗切分，退回旧的「固定尾部窗口」日志扫描方式（用于新旧结果对照）")
    ap.add_argument("--no-peer-logs", action="store_true",
                    help="不抓取 ascend-logs 产物（多节点 job 的对端节点日志，第二日志证据源）。"
                         "缺省开启：仅对 multi-node/double-node 开头且日志侧未定性的 job 抓取")
    ap.add_argument("--peer-log-lines", type=int, default=400,
                    help="每个对端节点取尾部多少行参与判桶（默认400）。对端文本无时间窗对齐，"
                         "取尾部是唯一可行的粗切，行数越大越可能扫到与本步骤无关的旧噪声")
    ap.add_argument("--artifact-cache-dir",
                    default=os.path.join(BASE_DIR, ".forensics_cache", "artifacts"),
                    help="ascend-logs 产物 zip 的缓存目录（按 artifact_id 存盘，"
                         "同一 run 的多个失败 job 只下载一次）。默认 <脚本目录>/.forensics_cache/artifacts")
    ap.add_argument("--report-dir", default=os.path.join(BASE_DIR, "npu_ci_reports"),
                    help="报告输出目录，每次运行生成带时间戳的 md 文件（默认 <脚本目录>/npu_ci_reports/）")
    ap.add_argument("--summary-file", default=os.path.join(BASE_DIR, "npu_ci_failure_report.md"),
                    help="精简版报告路径，每次运行自动更新对应仓库章节（默认 <脚本目录>/npu_ci_failure_report.md）")
    ap.add_argument("--infra-store", default=os.path.join(BASE_DIR, "npu_ci_reports", "infra_snapshot.json"),
                    help="跨仓基础设施信号（排队/cancelled 统计）持久化文件，供报告自动聚合跨仓表格"
                         "（默认 <脚本目录>/npu_ci_reports/infra_snapshot.json）")
    ap.add_argument("--emit-json", default=None, metavar="PATH",
                    help="把本次分析的结构化结果（失败 job 全量记录 + 逐条分类 + 待集群取证队列）"
                         "导出为 JSON，供第 2 步集群取证/历史归因程序（npu_ci_forensics.py）消费。"
                         "缺省不导出，行为与旧版完全一致")
    ap.add_argument("--run-id", dest="run_ids", type=int, action="append", default=None,
                    metavar="RUN_ID",
                    help="**定向模式**：只分析指定的 run（可重复）。供近实时监听器"
                         "（npu_ci_watch.py）逐次消费单个失败，跳过 workflow 枚举与近 N 天抽样；"
                         "同时**不**写精简版报告章节与 infra 快照（否则一次单 job 的分析会覆盖整仓统计）")
    ap.add_argument("--job-id", dest="job_ids", type=int, action="append", default=None,
                    metavar="JOB_ID",
                    help="定向模式的进一步收窄：只分析指定的 job（可重复，须落在 --run-id 给定的 run 内）。"
                         "用于「同一 run 里稍后又失败了另一个 job」被单独消费的场景")
    return ap.parse_args()

ARGS = parse_args()
OWNER, REPO = ARGS.repo.split("/")
if ARGS.since:
    SINCE = ARGS.since
else:
    from datetime import date, timedelta
    SINCE = (date.today() - timedelta(days=7)).isoformat()
NPU_LABEL = ARGS.npu_label_pattern

# 定向模式（--run-id）：只分析点名的 run/job，不做 workflow 枚举与近 N 天抽样。
# 为什么需要它：近实时监听器是在「某个步骤刚失败」时触发的，而那时
#   ① 失败 run 还没结束（抽样发现按 conclusion=='failure' 过滤会整条漏掉）；
#   ② 每次只该消费这一个失败，不能把 7 天窗口里的历史失败重新分类一遍。
INCREMENTAL = bool(ARGS.run_ids)
if ARGS.job_ids and not ARGS.run_ids:
    raise SystemExit("--job-id 必须与 --run-id 一起使用（job 归属哪个 run 无法自行推断）")

# 芯片范围（空字符串 = 不限芯片，退回旧的全 NPU runner 行为）
CHIPS = [c.strip().lower() for c in ARGS.chips.split(",") if c.strip()]
CHIPS_TAG = "-".join(CHIPS) if CHIPS else "all"
# 章节 slug 带芯片范围：避免 A2/A3 范围的章节覆盖掉报告里原有的全量仓库章节
SECTION_SLUG = ARGS.repo if not CHIPS else f"{ARGS.repo}@{CHIPS_TAG}"

def chip_of(labels):
    """从 runner 标签识别芯片，返回 KNOWN_CHIPS 中的一个；无法识别返回 None。
    注意：这里识别的是「任意」已知芯片（不受 --chips 限制），过滤由调用方按 CHIPS 判定，
    否则 --chips a2,a3 下永远过滤不掉 a5 的 job。
    芯片族表与 NPU_LABEL_PATTERN 同源（CHIP_FAMILY_TO_CHIP），保证 is_npu 与 chip 永不自相矛盾。"""
    for label in labels or []:
        m = re.search(rf"-({NPU_CHIP_ALT})(?:-|$)", label)
        if m:
            return CHIP_FAMILY_TO_CHIP[m.group(1)]
    return None

# ---------- 输出双写：终端 + 带时间戳的报告文件（历史回溯） ----------
import sys as _sys
_T0 = datetime.datetime.now()          # 分析开始时间
REPORT_DIR = ARGS.report_dir
os.makedirs(REPORT_DIR, exist_ok=True)
_ts = _T0.strftime("%Y%m%d_%H%M%S")
report_path = os.path.join(REPORT_DIR, f"npu_ci_failure_report_{REPO}_{CHIPS_TAG}_{_ts}.md")
_report_file = open(report_path, "w", encoding="utf-8")
_orig_stdout = _sys.stdout
_report_file.write(f"# NPU CI 失败分析报告\n\n"
                   f"- 分析开始: {_T0.strftime('%Y-%m-%d %H:%M:%S')}\n"
                   f"- 仓库: `{ARGS.repo}`\n"
                   f"- 芯片范围: `{CHIPS_TAG}`\n"
                   f"- 起始日期: `{SINCE}`\n"
                   f"- 参数: samples={ARGS.samples}, sample_per_wf={ARGS.sample_per_wf}, "
                   f"sample_cancelled={ARGS.sample_cancelled}, tail_lines={ARGS.tail_lines}, "
                   f"step_window={not ARGS.no_step_window}, cross_repo={ARGS.cross_repo}\n\n")
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

def persist_infra_store(store):
    """把 infra 快照写回磁盘；**定向模式下一律不写**。

    为什么把这个判断收进一个函数：原先有两处 `save_infra_store(store)`（排队/cancelled 统计一处、
    分类结果 infra_failures 一处），给一处加锁、漏掉另一处就会出事 —— 实测漏掉第二处时，
    一次单 job 的定向运行在 3 秒内把 infra_snapshot.json 里本仓的聚合数据覆盖成了 1 条。
    这不是「本次没数据」，而是销毁其它运行采集来的跨仓汇总数据，所以规则必须只有一处。
    """
    if INCREMENTAL:
        print("（定向模式：跳过 infra 快照写回，避免用单 job 结果覆盖整仓聚合）", file=_orig_stdout)
        return False
    save_infra_store(store)
    return True

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

def workflow_chip(f):
    """从 workflow 文件名识别芯片；不含芯片信息返回 None"""
    m = re.search(r'_(a2|a3|a5|310p)(?:_|\.|-|$)', f)
    return m.group(1) if m else None

if INCREMENTAL:
    # 定向模式不做 workflow 静态筛选：目标 run/job 已点名，workflow 名直接取 run 的 path 字段。
    # 不能只是「结果用不上」—— prepare_workflows() 要为每个 workflow 文件发一次 gh api
    # （约 100 次），而监听器每消费一个失败就要跑一次本脚本，这个代价必须省掉。
    WF_DIR, info, candidates, strong_files = None, {}, {}, set()
    print(f"=== Step1 跳过 workflow 静态筛选（定向模式 --run-id；芯片仍按 job 级 chip 判定，"
          f"范围 {CHIPS_TAG}）===")
else:
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

    # 芯片范围预过滤：文件名若已指明芯片（如 _a2 / _a3_560t / _a5 / _310p），
    # 只在芯片命中 --chips 时保留；文件名不含芯片信息者（如 pr_test）保留，
    # 因为其 job 仍可能跑在目标芯片的 runner 上，由 job 级 chip_of() 兜底判定。
    if CHIPS:
        excluded = {f: workflow_chip(f) for f in candidates
                    if workflow_chip(f) and workflow_chip(f) not in CHIPS}
        for f in excluded:
            del candidates[f]
        if excluded:
            print(f"芯片范围 --chips {CHIPS_TAG}: 排除 {len(excluded)} 个非目标芯片 workflow: "
                  f"{', '.join(sorted(excluded))}")

    print(f"=== Step1 静态筛出 NPU CI 候选: {len(candidates)} 个（芯片范围 {CHIPS_TAG}）===")
    for f, h in sorted(candidates.items()):
        wc = workflow_chip(f)
        print(f"  {f:48s} {','.join(sorted(h)) or 'uses_npu_exec':24s} 芯片={wc or '未标注'}")

# ---------- Step 2: 近 N 天 runs 记录 ----------
def has_standalone_trigger(txt):
    """workflow_call-only 的可复用 workflow 无独立 run 记录，跳过"""
    return bool(re.search(r'^\s*(push|pull_request|pull_request_target|workflow_dispatch|schedule|'
                          r'issue_comment|repository_dispatch|workflow_run|merge_group):', txt, re.M))

wf_stats = {}
if INCREMENTAL:
    print(f"\n=== Step2 定向模式（--run-id {ARGS.run_ids}）：跳过 workflow 枚举与近 N 天执行记录统计 ===")
else:
    print(f"\n=== Step2 近({SINCE}~) 执行记录 ===")
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

# ---------- Step 3: 失败 run → 定位 NPU job + 采集失败步骤/runner pod；另采样 cancelled 与排队时长 ----------
if INCREMENTAL:
    print(f"\n=== Step3 定向采集（--run-id {ARGS.run_ids}"
          + (f" --job-id {ARGS.job_ids}" if ARGS.job_ids else "") + "）===")
else:
    print(f"\n=== Step3 对失败 run 抽样（按失败量加权），定位失败 job 并采集失败步骤 ===")
SAMPLE_PER_WF = ARGS.sample_per_wf

def earliest_failed_step(job):
    """取「序号最靠前的失败步骤」。
    一次 job 失败常伴随多个步骤 conclusion=failure（后续步骤是级联失败），
    只有序号最靠前的那个才是根因。返回 dict 或 None。"""
    failed_steps = [s for s in (job.get('steps') or []) if s.get('conclusion') == 'failure']
    if not failed_steps:
        return None
    s = min(failed_steps, key=lambda x: x.get('number') or 9999)
    return {"name": s.get('name') or "", "number": s.get('number'),
            "started_at": s.get('started_at'), "completed_at": s.get('completed_at')}

def build_job_record(workflow, run_id, job, is_npu):
    """构造失败 job 记录。runner_name 即 runner pod 名，是第 2 步集群取证的连接键"""
    labels = job.get('labels') or []
    return {
        "workflow": workflow,
        "run_id": run_id,
        "job_id": job['id'],
        "job_name": job['name'],
        "is_npu": is_npu,
        "chip": chip_of(labels),
        "labels": labels,
        "failed_step": earliest_failed_step(job),
        "runner_name": job.get('runner_name'),
        "runner_id": job.get('runner_id'),
        "started_at": job.get('started_at'),
        "completed_at": job.get('completed_at'),
    }

def discover_by_run_ids(run_ids, job_ids):
    """定向采集：只查点名的 run/job，供近实时监听器逐次消费。

    与抽样发现（Step3 主循环）共用 build_job_record 与芯片/NPU 范围判定，差异只在「从哪来」。
    两条必须保留的差异：
      1. **不要求 run/job 已结束**。监听器在「某个步骤刚失败」时就被触发，那时 job 往往还在
         跑收尾步骤（实测「失败步骤结束 → job 结束」固定 55s），`conclusion` 还是 null。
         故这里以 `earliest_failed_step(job) 非空` 作为「失败」判据，而不是 conclusion=='failure'。
      2. **不按 run 是否含 NPU 失败做整体取舍**，逐 job 判 is_npu —— 定向模式下要分析的
         就是被点名的那一个 job。
    """
    records = []
    skipped_chip = 0
    for run_id in run_ids:
        run_text = gh(f"repos/{OWNER}/{REPO}/actions/runs/{run_id}")
        if not run_text:
            print(f"  ⚠️ run {run_id} 取不到（已删除/无权访问/网络失败），本次跳过")
            continue
        try:
            run = json.loads(run_text)
        except Exception as exc:
            print(f"  ⚠️ run {run_id} 响应解析失败（{exc}），本次跳过")
            continue
        # workflow 取 path（如 .github/workflows/schedule_nightly_test_a2.yaml），
        # 与抽样模式里 failed_jobs[].workflow 的口径保持一致（那边用的是候选文件名）
        workflow = run.get("path") or run.get("name") or f"run:{run_id}"
        jobs_text = gh(f"repos/{OWNER}/{REPO}/actions/runs/{run_id}/jobs?per_page=100")
        if not jobs_text:
            print(f"  ⚠️ run {run_id} 的 jobs 取不到，本次跳过")
            continue
        for job in json.loads(jobs_text).get("jobs") or []:
            if job_ids and job["id"] not in job_ids:
                continue
            if earliest_failed_step(job) is None:
                # 步骤还没失败（含已结束但成功、被跳过、仍在跑的 job）
                continue
            is_npu = any(re.search(NPU_LABEL, label) for label in (job.get('labels') or []))
            record = build_job_record(workflow, run_id, job, is_npu)
            if CHIPS and record["chip"] and record["chip"] not in CHIPS:
                skipped_chip += 1
                print(f"  - job {job['id']} 芯片 {record['chip']} 不在 --chips {CHIPS_TAG} 内，跳过")
                continue
            records.append(record)
    return records, skipped_chip


failed_jobs = []
cancelled_jobs = []
queue_times = []      # 秒：run.created_at → NPU job.started_at（runner 排队时长）
fallback_jobs = 0
chip_filtered = 0
if INCREMENTAL:
    failed_jobs, chip_filtered = discover_by_run_ids(ARGS.run_ids, ARGS.job_ids or [])
    n_sampled = len(ARGS.run_ids)
    n_never = 0       # 定向模式不采样 cancelled，下面的 cancelled/排队统计一律不参与
    print(f"  定向采集到 {len(failed_jobs)} 个「已有失败步骤」的 job"
          + (f"（另有 {chip_filtered} 个非目标芯片 job 已丢弃）" if chip_filtered else ""))
    if not failed_jobs:
        print("  ⚠️ 未采到失败 job：可能是 job 尚未产生失败步骤、或被 --job-id 过滤掉了")
for f, (_, counts) in sorted(wf_stats.items(), key=lambda kv: -kv[1][1].get('failure', 0)):
    runs = json.loads(gh(f"repos/{OWNER}/{REPO}/actions/workflows/{f}/runs?per_page=100&created=%3E{SINCE}"))['workflow_runs']
    failed = [r for r in runs if r['conclusion'] == 'failure']
    for r in failed[:SAMPLE_PER_WF]:
        jobs = json.loads(gh(f"repos/{OWNER}/{REPO}/actions/runs/{r['id']}/jobs?per_page=100"))['jobs']
        npu_fail = [j for j in jobs
                    if any(re.search(NPU_LABEL, l) for l in (j.get('labels') or []))
                    and j['conclusion'] == 'failure']
        if npu_fail:
            picked = [(j, True) for j in npu_fail]
        else:
            # 无 NPU 失败 job（NPU job 常被 skip），fallback 到该 run 的全部失败 job（多为 CPU 门禁）
            picked = [(j, False) for j in jobs if j['conclusion'] == 'failure']
            fallback_jobs += len(picked)
        for j, is_npu in picked:
            record = build_job_record(f, r['id'], j, is_npu)
            # 芯片范围过滤：明确识别出非目标芯片的 job 丢弃；
            # chip 为 None（CPU 门禁等）保留——它是 NPU job 被 skip 的原因，对定性有用
            if CHIPS and record["chip"] and record["chip"] not in CHIPS:
                chip_filtered += 1
                continue
            failed_jobs.append(record)
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
                cancelled_jobs.append({"workflow": f, "run_id": r['id'], "job_id": j['id'],
                                       "job_name": j['name'], "never_started": not j.get('started_at')})
n_never = sum(1 for c in cancelled_jobs if c["never_started"])
n_npu = sum(1 for r in failed_jobs if r["is_npu"])
# 定向模式不采样 run、不算排队时长，故「抽样 N 个 run / cancelled / 排队」三行都不打印：
# 打印出来只会是一串 0，读者会误以为「本次统计过、且都是 0」。
if not INCREMENTAL:
    n_sampled = sum(min(c.get('failure', 0), SAMPLE_PER_WF) for _, c in wf_stats.values())
    print(f"  共抽样失败 run {n_sampled} 个 → 失败 job {len(failed_jobs)} 个"
          f"（其中 NPU job {n_npu}，门禁 fallback {len(failed_jobs)-n_npu}）")
if CHIPS:
    chip_dist = Counter(r["chip"] or "未识别(CPU门禁)" for r in failed_jobs)
    print(f"  芯片分布({CHIPS_TAG}): " + "，".join(f"{k} {v}" for k, v in chip_dist.most_common())
          + (f"；另有 {chip_filtered} 个非目标芯片 job 已丢弃" if chip_filtered else ""))
step_dist = Counter((r["failed_step"] or {}).get("name") or "步骤未知" for r in failed_jobs)
print(f"  失败步骤分布（序号最靠前的失败步骤，决定归因路径）:")
for name, cnt in step_dist.most_common():
    print(f"    {cnt:4d}  {name}")
if not INCREMENTAL:
    print(f"  cancelled run 采样 {len(cancelled_jobs)} 个 job，其中从未启动/未分配到 runner {n_never} 个"
          f"（{n_never and '→ 调度/资源问题' or '→ 多为主动取消/上游中断'}）")
    if queue_times:
        q = sorted(queue_times)
        med = q[len(q)//2] / 60
        over30 = sum(1 for t in queue_times if t > 1800)
        print(f"  NPU runner 排队时长: 样本 {len(q)}，中位 {med:.0f}min，最长 {q[-1]/60:.0f}min，"
              f">30min 有 {over30} 个（>30min 提示 runner 池不足，infra 侧）")

# 持久化本仓 infra 统计到快照存储（跨仓表格自动聚合的数据源，替代手工快照）
# ⚠️ 定向模式必须跳过：它不采样 cancelled、不算排队时长，写进去会把本仓的跨仓统计
# 覆盖成一串 0 —— 那是对其它仓/其它运行采集结果的数据破坏，不是「本次没数据」。
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
persist_infra_store(infra_store)

# ---------- Step 4: 按失败步骤选择扫描窗口 → 下载日志 → 根因分类 ----------
# 归因路径：失败步骤决定「是否读日志 / 读哪一段 / 能否直接定性」，日志只在需要时作为证据来源。
# 这修正了旧版「一律扫固定尾部窗口」的两个结构性缺陷：
#   1) 头部盲区：安装/依赖解析失败的真错误在日志前段，尾部扫描看不见
#   2) 尾部污染：失败后的次生失败（如 `Upload failed`）与收尾清理噪音会被误当根因
# 扫描窗口用失败步骤的 started_at/completed_at 切分（日志每行带 ISO 时间戳）；
# 失败步骤/时间戳缺失时回退到旧的全局尾部窗口（--no-step-window 可强制回退以做新旧对照）。
# owner: infra=基础设施(资源/调度/网络) / code=业务方 / mixed=需二次判定 / 假失败=非真实错误
BUCKETS = [
    # 假失败：非真实错误（draft PR 阻断，文本不含 error 关键字，旧版会落进「未分类」）。单列且不计入根因分布
    (r'PR is draft\.?\s*Blocking CI', "假失败(draft PR 阻断)", "假失败"),
    # 多节点 pod 生命周期：这是「多节点编排层包装失败」背后的真实根因。
    # 实测 job 106057742807 的真相是 Worker pod 一直 phase=Pending（调度不上），旧版因无此桶落到 unknown 兜底
    (r'phase=Pending|pod failed to come online|Readiness probe failed|'
     r'Insufficient\s+(?:npu|cpu|memory)|0/\d+ nodes are available|nodes are available.*didn.t match',
     "多节点pod调度/就绪失败(k8s侧)", "infra"),
    # 分布式通信/网络拆成两桶（原为一个合并桶「分布式通信/网络(HCCL/Store)」）：
    # 两桶**都必须先于 ACL 桶**——否则 `hcclComm_, error code is 7` 会被 `error code is \d+` 吞进 ACL 桶，
    # owner 从 infra 错配成 mixed。同理两者都先于通用超时桶。
    # 拆的理由（实测 job 109264350421，run 36518916532）：合并桶把两种**不同机制**合成一个标签，
    # 再在知识表里配上官方叶子 `leaf_hccl_port_bound`（HCCL 通信端口被占用），于是每一次 Store 会合超时
    # 都会生成一句**错的**「官方口径对齐：HCCL 通信端口被占用」——而该 case 日志里既无 HCCL 错误码、
    # 也无 bind / address already in use，真因是对端节点迟到导致 TCPStore 会合超时。
    # 桶名是报告「根因」一行的原文：粒度错了，读者拿到的机制就是错的。
    # ① HCCL 集合通信失败 = 通信**已建立**后的集合通信出错/超时。
    (r'HCCL\w*(?:error|timeout|failed)|hcclComm[^\n]{0,80}error|CollectiveError',
     "HCCL 集合通信失败", "infra"),
    # ② Store 会合超时 = 进程**还没凑齐**（与①相反）。判据取服务端与客户端两侧的实测原文：
    #      node0（服务端）：`DistStoreError: Timed out after 1801 seconds waiting for clients. 7/8 clients joined.`
    #      node1（客户端）：`TCPStore.cpp:138 [c10d] recvValueWithTimeout failed … Failed to recv, got 0 bytes.`
    #                       `torch.distributed.DistNetworkError: Failed to recv, got 0 bytes.`
    #    ⚠️ 刻意**不**收裸 `TCPStore\.cpp`：它在良性告警里也会出现，而本桶排在 OOM/进程被 kill 桶**之前**，
    #       误命中会把 mixed 的日志错配成 infra。只收带「失败」语义的串。
    (r'DistStoreError|StoreError[^\n]{0,40}[Tt]imed out|DistNetworkError|'
     r'recvValueWithTimeout failed|waiting for clients|Connection reset|broken pipe',
     "Store 会合超时(TCPStore，对端 rank 未加入)", "infra"),
    # 模型缓存未命中（离线模式）：必须排在昇腾错误码桶之前。
    # 实测 job 106046329358：modelscope snapshot_download 在 local_files_only 且缓存为空时 raise ValueError，
    # 昇腾框架紧接着打印 ERR99999 兜底（Device:-1, RankID:-1），旧版因此把用户侧配置问题误判成 infra 硬件故障。
    # ⚠️ 不能锚定裸 `local_files_only`——traceback 会回显函数签名 `local_files_only: Optional[bool] = False`，必然误命中
    (r'Cannot find the requested files in the cached path|outgoing traffic has been disabled',
     "模型缓存未命中(离线模式 local_files_only)", "code"),
    # 昇腾算子错误按错误码分档（依据 classification-guide 场景 C）：
    #   507xxx = 驱动/硬件错误码（infra）
    #   107xxx = CANN runtime 参数非法，不是硬件信号（版本兼容性类，mixed）
    # ⚠️ ERR99999 已移出本桶：它是昇腾框架对「任意未捕获应用层异常」的通用兜底打印，
    #    实测紧跟在 ValueError traceback 之后，本身不是硬件信号（旧版无条件判 infra，实测命中皆假阳性）。
    #    仅当同一行绑定了真实设备（Device/RankID 非 -1）时才仍算硬件故障，故用负向前瞻排除 -1。
    (r'error code(?: is)?\s*507\d{3}|(?:Device|RankID):\s*(?!-1)\d+[^\n]{0,120}ERR99999',
     "昇腾NPU硬件错误(507xxx/ERR99999+设备)", "infra"),
    (r'error code(?: is)?\s*107\d{3}', "CANN运行时参数非法(107xxx)", "mixed"),
    (r'NPU function error|aclnn\w*\s*failed|error code is \d+', "昇腾算子执行错误(ACL)", "mixed"),
    # 依赖解析失败：真错误在安装日志前段，且会连锁产生大量 error/failed 噪音（如 find: no such file），
    # 必须排在 Python 错误类与编译类之前，否则根因被级联噪音吞掉
    (r'No solution found when resolving|requirements are unsatisfiable|unsatisfiable|no version of|'
     r'No matching distribution found|Could not find a version that satisfies|'
     r'subprocess-exited-with-error|Failed to build|detected dubious ownership',
     "依赖解析/构建失败(含链式噪音)", "mixed"),
    # 编译失败：加负向前瞻排除 `7739 bytes of body are still expected`（这是网络下载不全，
    # 旧版被 `error:.*expected` 误判成编译失败并把 owner 从 infra 错配成 code）
    (r'FAILED:\s*\[code=1\]|\berror: no viable|'
     r'\berror:(?!.*bytes of body are still expected).*(?:expected|undeclared|cannot convert|no member named)|'
     r'error:.*required.*include|\bclang\+\+.*error:|Error:.*CMake Error', "编译失败(C++/MLIR)", "code"),
    (r'cannot open shared object file|\.so: cannot open|torch_extensions.*\.so', "自定义算子so缺失(csrc构建)", "mixed"),
    (r'(?:SIGKILL|exit code 137|killed.*(?:OOM|137)|OOMKilled|Signal 9|container.*not found)', "进程被kill(OOM/超内存)", "mixed"),
    # Ray 编排桶加负向前瞻：`RayTaskError(AssertionError)` 本质是断言失败（精度/逻辑），
    # 应落到后面的断言桶，而不是被 Ray 桶吞掉
    # 两个负向前瞻都必须覆盖「括号内」写法：`ray.exceptions.RayTaskError(AssertionError)`
    # 里断言在括号中，只挡 `\.\w*Assertion`（点号形式）会漏网（已实测踩坑）
    (r'RayTaskError(?!\(Assertion)|ray\.exceptions(?![^\n]{0,60}Assertion)|ActorDiedError|'
     r'Actor.*(?:died|dead)|Driver of actor.*fail', "分布式通信/编排(Ray)", "code"),
    # 下载拆两类：内网镜像仓库(infra 直接责任) vs 外网(huggingface 等，mixed，可加镜像/缓存缓解)
    (r'Failed to download metadata for repo|repomd\.xml|Cannot download|apt-get update.*Failed to fetch|Failed to fetch.*mirror|yum.*Error', "内网镜像/仓库下载失败", "infra"),
    # 对外 API 调用失败：GitHub API 拉取失败（token/限流属 infra）；放在外网下载前，避免 404/503 细节被后者吞掉
    (r'Failed to fetch PR title', "GitHub API 调用失败", "infra"),
    # 外网下载：收紧 huggingface 匹配——旧版裸 `huggingface_hub` 会命中正常进度行
    # `Downloading huggingface_hub-1.30.0-py3-none-any.whl` 造成误判；
    # 另纳入下载不完整（bytes of body are still expected / early EOF）这类网络故障
    (r'HfHubHTTPError|huggingface_hub\.errors|huggingface_hub[^\n]{0,60}(?:Error|Timeout|Failed|Connection)|'
     r'bytes of body are still expected|early EOF|RPC failed|Curl error|503.*Service Unavailable|'
     r'Github.*rate limit|404 Client Error', "模型/包下载失败(外网)", "mixed"),
    (r'timed out|TimeoutError|UV_HTTP_TIMEOUT|timeout.*exceed|timed out waiting', "超时", "mixed"),
    (r'out of memory|OOM error|MemoryError|memory.*not enough|aclrtMalloc failed|alloc.*failed.*memory', "OOM/显存不足", "mixed"),
    (r'No space left|disk full|ENOSPC', "磁盘不足", "infra"),
    (r'ImportError|ModuleNotFoundError|No module named', "依赖/安装(ImportError)", "code"),
    (r'AssertionError|E\s+assert', "断言失败(代码或精度)", "code"),
    # 门禁策略失败：pre-commit/ShellCheck 属同一类静态检查；CSRC 变更检查是 CI 策略门禁（业务方）
    (r'ShellCheck|shellcheck|pre-commit did not succeed', "静态检查(pre-commit/ShellCheck)", "code"),
    # mypy 静态类型检查失败。实测形态（job 106079239560，Run mypy 步骤）：
    #   `.../pool_scheduler.py:175: error: "KVPoolScheduler" has no attribute "mamba_group_ids"  [attr-defined]`
    #   `Found 1 error in 1 file (checked 615 source files)`
    # 这是业务方代码问题（code），但旧版无此桶 → 落到兜底 `failed to run script step` 被标 unknown。
    # 实测 6 份样本（15%）因此被误归 unknown，且同一 run 的 cpu-ut job 会因同一属性缺失崩成
    # AttributeError 而落进另一个桶 —— 同一根因被算两遍
    (r'Found \d+ errors? in \d+ files?|'
     r'error:.*\[(?:attr-defined|assignment|arg-type|return-value|union-attr|call-arg|call-overload|'
     r'override|misc|index|operator|import|valid-type|var-annotated|name-defined|no-redef|has-type|'
     r'typeddict-\w+|list-item|dict-item|str-format|exit-status|abstract|no-untyped-def|type-arg)\]',
     "静态类型检查失败(mypy)", "code"),
    (r'CSRC build workflows changed', "CI 策略检查(CSRC 变更)", "code"),
    # vLLM 引擎崩溃是级联症状而非根因（引擎子进程被更早的错误打死，真因在其上游日志），
    # 必须单列并标 unknown：否则 `RuntimeError: engine core died` 会一路落到 exit 255 桶，
    # 被标成 owner=infra —— 那是在给一个我们并不掌握的责任方下结论
    (r'Engine core (?:died|failed)|EngineDeadError|engine.*(?:died|dead)|RuntimeError.*[Ee]ngine',
     "vLLM引擎崩溃(级联，真因在上游)", "unknown"),
    (r'AttributeError|TypeError|ValueError|KeyError|IndexError', "Python运行时错误", "code"),
    (r'Either .tests. or .config_file_path. must be provided|must be provided', "测试参数缺失(config未传入)", "code"),
    # 昇腾框架异常兜底：ERR99999 是昇腾对「任意未捕获应用层异常」的通用包装，属级联症状而非根因，
    # 与 vLLM 引擎崩溃同类，故排在真实根因桶（ImportError/断言/Python运行时错误…）之后并标 unknown：
    # 真错误能被前面的桶命中；只有确实无其他信号时才落到这里，此时不假装知道责任方。
    # ⚠️ 判别依据：实测形态为 `(PID:27482, Device:-1, RankID:-1) ERR99999 UNKNOWN applicaiton exception`
    #    —— `Device:-1` 表示未绑定 NPU 设备，是应用层异常。绑定真实设备的 ERR99999 由前面的硬件桶接走。
    (r'ERR99999', "昇腾框架异常兜底(ERR99999，非硬件信号)", "unknown"),
    # ---- 测试执行侧的判定行（harness 在测试步骤打印，属**决定性**证据，见 DECISIVE_BUCKETS）----
    # 位置：排在硬件/网络/依赖/断言各桶**之后**（那些是真根因，命中优先），
    #       又排在 exit 255 / `failed to run script step` 这类通用包装**之前** ——
    #       否则 pytest 自己给出的判定会被外层包装覆盖成 unknown/infra（实测正是如此）。
    # ret=4 用法错误 / ret=5 未收集到用例：实测形态（2026-09-28 Nightly-A3 (PR) 17618）
    #   `ERROR: file or directory not found: tests/e2e/nightly/multi_node/scripts/test_multi_node.py`
    #   `collected 0 items` + `pytest exit code: ret=4` —— 一条用例都没跑。
    #   根因是测试脚本与被测代码**版本错配**（run.sh 取自 main，被测代码取自 PR 分支），
    #   责任方在业务侧（同一仓库的 CI 编排），**不是**基础设施：容器起来了、pytest 正常执行了。
    (r'pytest exit code: ret=[45]\b|file or directory not found|collected 0 items|no tests ran',
     "测试未执行(入口/用例集不存在，脚本与代码错配)", "code"),
    # ret=1：pytest 跑完并判定有用例失败 —— 产品/精度/逻辑问题，业务侧。判据是 pytest 自己的退出码，
    # 不需要再上集群找旁证（pod 状态即便查到，也只能说明「容器当时活着」）。
    (r'pytest exit code: ret=1\b', "测试用例失败(pytest ret=1)", "code"),
    # exit code 255 = K8s 强制终止，本身不是根因（真因是前面的 RuntimeError/AssertionError），
    # 故排在这些桶之后：真错误优先命中，只有确实无其他信号时才归到这里
    (r'exit code 255|command terminated with exit code 255', "步骤被强制终止(exit 255，非根因)", "infra"),
    # 兜底：`failed to run script step` 是 GitHub 对「任意脚本步骤失败」的通用包装，并非多节点专属。
    # 实测 sglang/triton 的 CPU 门禁 job 也被它命中，旧版桶名「多节点编排层包装失败」属误命名，此处更正
    (r'failed to run script step', "脚本步骤通用包装失败(需按失败步骤细化)", "unknown"),
]
BUCKET_OWNER = {label: owner for _, label, owner in BUCKETS}

# 「日志侧已定性」的桶：命中即**提前退出**，不再去排查基础设施的哪个环节失败。
# 判据是测试框架自己打印的判定行（pytest 的退出码与收集结果），证据硬到不需要集群侧旁证：
# 它同时说明了责任方（业务侧）与「集群侧查不出新东西」（失败发生在测试进程内）。
# 后果（三处联动，缺一不可）：
#   ① 不进 cluster_todo（第 2 步输入）→ 不去 kubectl 反查 runner pod；
#   ② 下游 npu_ci_forensics 的 select_cases 不占 --max-cases 名额且跳过集群取证（见那里注释）；
#   ③ 报告里显式写「集群侧：按规则跳过」，不能留白让人以为「没查」。
# ⚠️ 只收录这类判据，不扩大到推测性桶：能跳过集群取证的前提是证据足够硬。
DECISIVE_BUCKETS = frozenset({
    "测试未执行(入口/用例集不存在，脚本与代码错配)",
    "测试用例失败(pytest ret=1)",
})


# pytest 自己打印的判定行。出现它 = 测试进程真的跑到了「给出结论」那一步，
# 是业务侧责任方的直接证据（而不是从报错文本里猜出来的）。
PYTEST_VERDICT_RE = re.compile(r'pytest exit code: ret=\d+', re.I)


def is_decisive(bucket, owner, text_scan):
    """该失败是否「日志侧已定性为业务侧」→ 下游据此提前退出集群取证。

    两个条件满足其一即可：
      ① 桶本身就在 DECISIVE_BUCKETS 里（pytest 的收集结果/退出码直接命中的桶）；
      ② 桶判 owner=code **且**日志里有 pytest 的判定行。
    为什么要第 ② 条：只按桶名判会**漏**——ret=1 的日志尾部常带断言 traceback，
    于是首选命中更靠前的【断言失败】【Python运行时错误】等桶（owner 同为 code，
    但不在 DECISIVE_BUCKETS 里），这类 case 照样会占掉一个取证名额、还白跑一次集群查询。
    反之，若桶判 mixed/infra（OOM、HCCL、节点调度…），即便日志里有 ret=1 也**不**跳过：
    那时责任方尚未落在业务侧，集群侧证据仍可能是关键（不能把硬件问题读成业务问题）。
    """
    if bucket in DECISIVE_BUCKETS:
        return True
    return owner == "code" and bool(PYTEST_VERDICT_RE.search(text_scan or ""))


def classify_text(text_scan):
    """在待扫文本上按 BUCKETS 顺序取**首个**命中的桶，返回 (桶标签, 证据片段)。

    顺序即优先级（BUCKETS 的排列本身是校准结果）：真根因桶在前，通用包装桶在后。
    独立成函数的唯一目的是**可单测**——「给定一段真实日志 → 判成哪个桶」是这套工具最核心的
    判定，早先它埋在脚本主流程里（全模块级执行、无法安全 import），只能靠跑全流程观察，
    于是「pytest 判定行被外层包装覆盖」这类错误顺序长期没被守住（见 tests/test_pytest_verdict.py）。
    """
    for pattern, label, _owner in BUCKETS:
        match = re.search(pattern, text_scan, re.I)
        if match:
            return label, text_scan[max(0, match.start() - 30):match.end() + 30].replace("\n", " ")
    return "未分类", ""


def collect_peer_evidence(rec, bucket, owner, text_scan):
    """取该 job 的**对端节点**日志（多节点 job 的第二日志证据源）；不适用时返回 None。

    为什么需要这条源（实测 job 109264350421 / run 36518916532）：`gh api …/jobs/{id}/logs`
    回的**只有 node0 一台机器**的容器 stdout。该 case 是多节点 DP，node0 是 TCPStore 服务端，
    只说得出「8 个 rank 里 1 个没连上」：
        torch.distributed.DistStoreError: Timed out after 1801 seconds waiting for clients. 7/8 clients joined.
    「是谁没连上」只在 node1 的日志里（实测 839 行，其 `TCPStore.cpp:138 recvValueWithTimeout failed`
    与 `DistNetworkError: Failed to recv, got 0 bytes` 在 job log 里 grep 一行都没有）。
    多节点 job 跑测试的步骤就叫 `Stream logs`，这个错配是结构性的，不是偶发。

    三档短路（成本控制，见 --no-peer-logs / --peer-log-lines）：
      ① `--no-peer-logs`
      ② 非 multi-node/double-node 开头的 job —— 单节点 job 的日志本就完整落在 job log 里，取产物没有增量
      ③ 日志侧**已定性**（is_decisive）—— 结论已经由 node0 时间窗给出，产物不改变归因，
         还要多花一次 API + 一次解包。这一条与「已定性→退出集群取证」是同一个判断。

    返回的 dict 直接进 `classifications[]`（`_enrich()` 用 dict(item) 复制，无需改下游解析），
    报告与测试都按这些键取值，故键名固定、缺项也留 None 而不是删键。
    """
    if ARGS.no_peer_logs or not peer_ops.is_multi_node_job(rec.get("job_name") or ""):
        return None
    if is_decisive(bucket, owner, text_scan):
        return None
    stem = peer_ops.artifact_stem_for_job(rec.get("job_name") or "")
    if not stem:
        return None

    repo = f"{OWNER}/{REPO}"
    peer = {"artifact": None, "artifact_id": None, "size": None, "from_cache": False,
            "ok": False, "empty": False, "nodes": [], "peers": [],
            "node_lines": {}, "kept_lines": {},
            "bucket": None, "sig": None, "adopted": False, "reason": None, "note": None}

    artifacts, reason = peer_ops.list_artifacts(repo, rec["run_id"], gh)
    if reason:
        peer["reason"] = reason
        return peer
    # ⚠️ 必须按命名规则匹配到**这个** job 的产物：同一 run 里还有 `nightly-a3` 这类无关产物
    name = peer_ops.match_artifact([a.get("name") or "" for a in artifacts], stem)
    if not name:
        peer["reason"] = f"该 run 无与 yaml「{stem}」匹配的 -ascend-logs 产物"
        return peer
    peer["artifact"] = name
    record_, reason = peer_ops.pick_artifact(artifacts, name)
    if reason:
        peer["reason"] = reason
        return peer

    fetched = peer_ops.fetch_artifact_zip(repo, record_, ARGS.artifact_cache_dir, gh)
    peer.update(artifact_id=fetched.get("artifact_id"), size=fetched.get("size"),
                from_cache=fetched.get("from_cache"))
    if not fetched.get("ok"):
        peer["reason"] = fetched.get("reason")
        return peer

    extracted = peer_ops.extract_node_logs(fetched["zip"], ARGS.peer_log_lines)
    peer.update(ok=extracted.get("ok"), empty=extracted.get("empty"),
                nodes=sorted(extracted.get("nodes") or {}),
                peers=extracted.get("peers") or [],
                node_lines={n: e.get("lines") for n, e in (extracted.get("nodes") or {}).items()},
                kept_lines={n: e.get("kept_lines") for n, e in (extracted.get("nodes") or {}).items()},
                reason=extracted.get("reason"), note=extracted.get("note"))
    if not extracted.get("ok"):
        return peer

    # 判桶仍走同一个 classify_text：对端文本与主日志是同一类证据（容器 stdout），
    # 只是机器不同、没有时间窗对齐，故复用同一张 BUCKETS 表而不是另立一套规则。
    peer_text = peer_ops.peer_scan_text(extracted)
    peer_bucket, peer_sig = classify_text(peer_text) if peer_text else ("未分类", "")
    if peer_bucket == "未分类" and peer_text:
        m = re.search(r'(FAILED|Error|error:)', peer_text)
        peer_sig = (peer_text[max(0, m.start() - 20):m.end() + 40].replace("\n", " ")
                    if m else "(无匹配)")
    peer.update(bucket=peer_bucket, sig=peer_sig,
                adopted=peer_ops.adopt_peer_bucket(bucket, peer_bucket))
    return peer


def peer_console_line(peer):
    """控制台里对端证据那一行。**留白会被读成「对端节点无异常」**，故每档都必须有话说。"""
    if peer.get("empty"):
        return "↳ 对端节点：产物存在但为空（tar 内只有目录项），无对端证据"
    if not peer.get("ok"):
        return f"↳ 对端节点：未取得（{peer.get('reason') or '未知原因'}）"
    if not peer.get("peers"):
        return (f"↳ 对端节点：产物内只有 node0 的容器日志（{peer.get('artifact')}），无对端节点文本")
    nodes = "、".join(f"{n}（{peer['node_lines'].get(n, 0)} 行，取尾部 "
                      f"{peer['kept_lines'].get(n, 0)} 行）" for n in peer["peers"])
    role = "采用兜底：本 case 的桶来自对端日志" if peer.get("adopted") else "并列证据，未参与本 case 定性"
    return f"↳ 对端节点 {nodes}：命中桶【{peer.get('bucket')}】（{role}）| {peer.get('sig')}"

# 与根因无关的噪音行：失败后的清理动作、GHA 自身收尾输出（实测占尾部窗口的绝大多数）
NOISE_PATTERNS = [
    r'Cleaning up orphan processes',
    r'Removing credentials config',
    r'git config --local --unset includeif',
    r'##\[(?:end)?group\]Post job cleanup',
    r'^\[command\]/usr/bin/git ',
]

# 步骤 → 归因路径（按失败步骤名匹配，顺序即优先级）
STEP_ROUTES = [
    (r'^(?:Set up job|Initialize containers)$', "no_log", "infra",
     "容器/Runner 初始化失败，按分类指南直接判基础设施，无需读日志"),
    (r'^(?:Upload .*logs.*|Upload failed|Upload .*artifact.*|Upload benchmark.*)$', "no_log", "infra",
     "日志上传/产物归档失败——测试可能已通过，属 Runner 与 GitHub 通信问题"),
    # ⚠️ `Stream logs` 曾与本行上面的上传类步骤并列为 no_log/infra，那是**误判**，已拆出：
    #   在多节点 job 里 `Stream logs` 就是**执行测试的那一步**（harness 在此跑 pytest 并流式输出），
    #   真因在日志里，必须读。实测 3 份历史样本（job 107537041127 等）的日志里明确写着
    #   `FAILED tests/...::test_external_dp` + `1 failed in 3631.38s` + `pytest exit code: ret=1`
    #   —— 是用例真失败，却被旧版一律判成 infra「Runner 与 GitHub 通信问题」，
    #   还去集群找 pod 是否被驱逐，方向完全反了（该桶在历史语料里占 19%）。
    (r'^Stream logs$', "window_tail", None,
     "多节点 job 的测试执行步骤（harness 在此跑 pytest 并流式输出），真因在日志里"),
    # 门禁聚合步骤：`Check all required jobs` 之类只是汇总其他 job 的结论，
    # 语义上「别的 job 挂了所以我也挂」，必然是级联而非根因。
    # 实测占 vllm 样本 8/40（20%），其中 2 条落成「未分类」——旧版把这些算作独立根因，膨胀分母
    (r'^Check\b[^\n]*\brequired jobs?\b', "aggregate", None,
     "门禁聚合步骤：失败必然由其他 job 级联而来，不计入根因分布"),
    (r'^(?:Wait for pods ready|Launch cluster|Clear resources|Decode kubeconfig|Fetch .* from PVC)$',
     "pod", None, "多节点集群编排阶段（pod 调度/资源，需集群侧确认）"),
    (r'^(?:Install|Build|Set up .*|Config mirrors|Restore .* cache)', "window_head", None,
     "安装/构建/依赖阶段，错误在日志前段"),
    (r'.*', "window_tail", None, "测试阶段，扫失败步骤时间窗尾部"),
]

def route_for(step_name):
    """按失败步骤名返回 (归因路径, 直接owner, 说明)"""
    for pat, route, owner, note in STEP_ROUTES:
        if re.search(pat, step_name or "", re.I):
            return route, owner, note
    return "window_tail", None, ""

TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})')

def slice_by_step_window(lines, failed_step):
    """按失败步骤的 [started_at, completed_at] 切日志（日志行首带 ISO 时间戳）。
    切不出来（无时间戳/窗口缺失）返回 None，由调用方回退到全局尾部窗口。"""
    if ARGS.no_step_window or not failed_step:
        return None
    start_s, end_s = failed_step.get("started_at"), failed_step.get("completed_at")
    if not start_s:
        return None
    try:
        start = datetime.datetime.fromisoformat(start_s.replace("Z", "+00:00"))
        end = datetime.datetime.fromisoformat((end_s or start_s).replace("Z", "+00:00"))
    except Exception:
        return None
    kept = []
    for line in lines:
        m = TS_RE.match(line)
        if not m:
            continue
        try:
            if start <= datetime.datetime.fromisoformat(m.group(1) + "+00:00") <= end:
                kept.append(line)
        except Exception:
            continue
    return kept or None

classified = Counter(); detail = []
bucket_link = defaultdict(list)   # 桶 -> [(样例 run 链接, 是否 NPU job)]，每桶最多3条
by_owner = defaultdict(Counter)   # owner -> 桶计数
step_owner = Counter()            # 由失败步骤直接定性（未读日志）的步骤计数
false_positive = Counter()        # 假失败计数（不计入根因分布）
cluster_todo = []                 # 待集群取证：runner pod 名 + 失败步骤（第 2 步的输入）
# 不变式：DECISIVE_BUCKETS（日志侧已定性为业务侧）的 job **绝不**出现在 cluster_todo 里 ——
# 那正是「提前退出、不去排查基础设施」的定义。两条登记路径都天然满足（编排阶段 route=="pod"、
# 未分类兜底），但这条不变式由测试守着（tests/test_pytest_verdict.py），不靠读者推断。
aggregate_cascade = Counter()     # 门禁聚合步骤：级联失败，不计入根因分布
# 去重键 (run_id, 桶)：同一 run 的同一根因只计一次。
# 实测 40 份样本只对应 26 个 run（重复率 35%），极端 run 35452779632 被计 5 次
# （5 个 job 都归【依赖/安装(ImportError)】）——同一根因算 5 份，Top3 占比被膨胀
dedup_seen = set()
dedup_skipped = 0                 # 因「同 run 同根因」被跳过的计数次数
logs_done = 0
window_hits = 0                   # 成功按步骤时间窗切分的次数
for rec in failed_jobs:
    if logs_done >= ARGS.samples:
        break
    failed_step = rec["failed_step"]
    step_name = (failed_step or {}).get("name") or ""
    route, forced_owner, route_note = route_for(step_name)

    # 路径零：门禁聚合步骤——级联失败，不计入根因分母，不进待集群取证（集群侧查不出新东西）。
    # 也不消耗 --samples 预算，把额度让给真正的失败 job
    if route == "aggregate":
        aggregate_cascade[step_name] += 1
        continue

    tag = "NPU" if rec["is_npu"] else "gate"
    link = f"https://github.com/{OWNER}/{REPO}/actions/runs/{rec['run_id']}/job/{rec['job_id']}"

    def record(bucket, sig, owner, logs_scanned, windowed=False, decisive=None,
               peer=None, sig_source=None):
        global dedup_skipped
        # 登记伪桶的 owner：`步骤直接定性:{步骤名}` 是 no_log 路径动态生成的桶名，
        # 不在 BUCKETS 里，因此 BUCKET_OWNER 取不到 → 报告表格的 owner 列会显示 unknown，
        # 与同一报告「按 owner 汇总」（走 by_owner，owner 正确）自相矛盾。此处补齐。
        BUCKET_OWNER.setdefault(bucket, owner)
        key = (rec["run_id"], bucket)
        duplicate = key in dedup_seen
        if duplicate:
            dedup_skipped += 1
        else:
            dedup_seen.add(key)
            classified[bucket] += 1
            by_owner[owner][bucket] += 1
            if len(bucket_link[bucket]) < 3:
                bucket_link[bucket].append((link, rec["is_npu"]))
        detail.append({"workflow": rec["workflow"], "job_name": rec["job_name"], "tag": tag,
                       "bucket": bucket, "sig": sig, "link": link, "owner": owner,
                       "step": step_name, "chip": rec["chip"], "scanned": logs_scanned,
                       "windowed": windowed, "duplicate": duplicate,
                       # 日志侧已定性为业务侧 → 下游（第 2/5 步）据此提前退出集群取证。
                       # 默认按桶判（no_log 路径的动态桶名「步骤直接定性:*」天然不在集合里）
                       "decisive": (bucket in DECISIVE_BUCKETS if decisive is None else decisive),
                       # 对端节点日志（多节点 job 的第二证据源；None = 不适用/未抓取）
                       # 与「本条的桶来自哪段文本」——报告靠它决定是否写「结论来自对端节点」
                       "peer": peer, "sig_source": sig_source})

    # 多节点编排阶段的失败：登记 runner pod，供第 2 步 kubectl 反查调度/排队
    if route == "pod":
        cluster_todo.append({"runner_name": rec["runner_name"], "chip": rec["chip"],
                             "step": step_name, "workflow": rec["workflow"],
                             "job_name": rec["job_name"], "link": link,
                             "reason": route_note})

    # 路径一：无需读日志，按失败步骤直接定性（省一次大 API 调用）
    if route == "no_log":
        logs_done += 1
        step_owner[step_name] += 1
        record(f"步骤直接定性:{step_name}", f"失败步骤={step_name}（{route_note}）", forced_owner, False)
        continue

    log = gh(f"repos/{OWNER}/{REPO}/actions/jobs/{rec['job_id']}/logs", binary=True)
    if not log:
        continue
    if log[:2] == b'\x1f\x8b':
        try:
            log = gzip.decompress(log)
        except Exception:
            pass
    logs_done += 1
    text = log.decode('utf-8', errors='ignore')
    # 丢弃两类噪音行：
    #   \x1b[36;1m 青色前缀 = GHA 回显的脚本源码（否则正则命中脚本里 echo 的报错文案，实测占 vllm 样本 42.5%）
    #   NOISE_PATTERNS = 失败后的清理动作与 GHA 收尾输出
    lines = [l for l in text.splitlines()
             if '\x1b[36;1m' not in l and not any(re.search(p, l) for p in NOISE_PATTERNS)]

    # 路径二：按失败步骤时间窗切分；切不出来回退全局尾部窗口
    window = slice_by_step_window(lines, failed_step)
    if window is not None:
        window_hits += 1
        if route == "window_head":
            # 安装/构建阶段：真错误（依赖解析）在最前面
            scan_lines = window[:ARGS.tail_lines]
        else:
            # 测试/编排阶段：错误集中在窗口尾部
            scan_lines = window[-ARGS.tail_lines:]
    else:
        scan_lines = lines[-ARGS.tail_lines:]
    text_scan = "\n".join(scan_lines) or text

    bucket, sig = classify_text(text_scan)
    if bucket == "未分类":
        m = re.search(r'(FAILED|Error|error:)', text_scan)
        sig = text_scan[max(0, m.start()-20):m.end()+40].replace("\n", " ") if m else "(无匹配)"

    # ---- 第二日志证据源：对端节点（多节点 job 才有；node0 之外的机器只在产物里）----
    # 策略是**仅兜底**（见 peer_logs.adopt_peer_bucket）：node0 的窗口是本 job 失败步骤的时间窗，
    # 对端文本只是粗切的尾部若干行、无时间窗对齐，拿它改写一个已经定性的结论 = 用更弱的证据
    # 推翻更强的那个。故只在 node0 判「未分类」时才采用，并在报告里注明来源。
    owner = BUCKET_OWNER.get(bucket, "unknown")
    peer, sig_source = None, "失败步骤窗口"
    # 「已定性」判据固定用**主日志（node0）**这一段：对端文本无时间窗对齐、且只是兜底，
    # 拿它命中判定行等于用更弱的证据把 case 推成「已定性→跳过集群取证」
    # （反例见 tests/test_peer_logs.py：对端文本里有 ret=1 也不得让 case 变 decisive）。
    verdict_bucket, verdict_owner = bucket, owner
    if owner != "假失败":      # 假失败不是真失败，为它抓产物纯属浪费一次 API
        peer = collect_peer_evidence(rec, bucket, owner, text_scan)
        if peer and peer.get("adopted"):
            bucket, sig = peer["bucket"], peer["sig"]
            owner = BUCKET_OWNER.get(bucket, "unknown")
            sig_source = "对端节点日志"

    if bucket == "未分类":
        # 未分类且属编排阶段 → 日志确实没给出根因，登记待集群取证。
        # 注意这里的 bucket 是**兜底之后**的结果：被对端日志救回来的 case 已不属于「未给出根因」，
        # 再写这条 reason 会与报告里的桶自相矛盾；它的集群取证走知识表的 probe 通道（Store 桶
        # 的 probe=pod_node），不依赖本清单。
        if step_name and not any(t["job_name"] == rec["job_name"] for t in cluster_todo):
            cluster_todo.append({"runner_name": rec["runner_name"], "chip": rec["chip"],
                                 "step": step_name, "workflow": rec["workflow"],
                                 "job_name": rec["job_name"], "link": link,
                                 "reason": "日志未给出根因，需集群侧确认 pod/节点状态"})

    if owner == "假失败":
        false_positive[bucket] += 1
        detail.append({"workflow": rec["workflow"], "job_name": rec["job_name"], "tag": tag,
                       "bucket": bucket, "sig": sig, "link": link, "owner": owner,
                       "step": step_name, "chip": rec["chip"], "scanned": True,
                       "windowed": window is not None, "duplicate": False,
                       "decisive": False,        # 假失败不是真失败，无所谓「提前退出」
                       "peer": None, "sig_source": None})
    else:
        # 判据用的 text_scan 与本函数的扫描窗口一致（同一段文本判桶、判是否已定性），
        # 不能换成全文：全文里别的步骤留下的 pytest 判定行不是本步骤的结论。
        # 同理桶与 owner 也用**兜底之前**的那对（verdict_*），见上面赋值处的注释。
        record(bucket, sig, owner, True, window is not None,
               decisive=is_decisive(verdict_bucket, verdict_owner, text_scan),
               peer=peer, sig_source=sig_source)

n_root_cause = sum(classified.values())
print(f"\n=== Step4 已定性失败 {logs_done} 份 → 去重后根因 {n_root_cause} 个"
      f"{f'（同 run 同根因合并 {dedup_skipped} 次）' if dedup_skipped else ''}"
      f"（[NPU]=NPU job / [gate]=CPU门禁fallback；括号内为扫描方式，兼附证据片段与 run 链接）===")
if aggregate_cascade:
    print(f"  未计入根因的门禁聚合级联 {sum(aggregate_cascade.values())} 份: "
          + "，".join(f"{k}×{v}" for k, v in aggregate_cascade.most_common()))
for d in detail:
    if d["owner"] == "假失败":
        print(f"  [假失败|{d['tag']:4s}] {d['workflow'][:26]:26s} {d['job_name'][:30]:30s} → {d['bucket']}")
        continue
    # 逐条按该条自己的扫描方式标注（旧版用全局 window_hits，回退的样本也被标成「时间窗」）
    # 日志侧已定性的单列一种模式：它不只是「读日志」，而是**读完即退出**、不再做基础设施排查
    if d.get("decisive"):
        mode = "已定性→退出"
    else:
        mode = "步骤直接定性" if not d["scanned"] else ("时间窗" if d["windowed"] else "尾部窗口")
    chip = d["chip"] or "gate"
    dup = "（同run同因，未计数）" if d["duplicate"] else ""
    print(f"  [{d['owner'][:4]:4s}|{d['tag']:4s}|{chip:4s}|{mode}] {d['workflow'][:24]:24s} "
          f"{d['job_name'][:28]:28s} → {d['bucket'][:22]:22s} | {d['sig']}{dup}")
    print(f"      step={d['step'] or '未知'}  run: {d['link']}")
    # 对端节点证据单列一行：产物为空/抓取失败也照写（留白会被读成「对端节点无异常」）
    if d.get("peer"):
        print(f"      {peer_console_line(d['peer'])}")

# ---------- Step 5: top3 ----------
print(f"\n=== Top3 失败原因（共 {n_root_cause} 个根因（已按 run 去重）"
      f"{f'，另有假失败 {sum(false_positive.values())} 份不计入' if false_positive else ''}"
      f"{f'，门禁聚合级联 {sum(aggregate_cascade.values())} 份不计入' if aggregate_cascade else ''}）===")
if n_root_cause:
    for i, (bucket, cnt) in enumerate(classified.most_common(3), 1):
        links = bucket_link.get(bucket, [])
        link, npu = links[0] if links else ("", False)
        print(f"  #{i} {bucket}: {cnt} 次 ({cnt/n_root_cause*100:.0f}%)")
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
    if step_owner:
        print(f"\n  其中按失败步骤直接定性（未读日志，省 API 调用）: "
              + "，".join(f"{s}×{n}" for s, n in step_owner.most_common()))
    print(f"\n  扫描方式: 成功按失败步骤时间窗切分 {window_hits}/{logs_done} 份"
          f"（其余回退全局尾部窗口{'(已 --no-step-window 强制回退)' if ARGS.no_step_window else ''}）")
    # 第二证据源同样要报「拿不到」，而不是只在拿到时才提：产物存在但为空是**常态**，
    # 静默跳过会让人以为「多节点 job 的对端节点都查过了」。
    peer_items = [d["peer"] for d in detail if d.get("peer")]
    if peer_items:
        got = [p for p in peer_items if p.get("ok") and not p.get("empty")]
        empty = [p for p in peer_items if p.get("empty")]
        adopted = [p for p in peer_items if p.get("adopted")]
        print(f"  对端节点日志(第二证据源): 抓取 {len(peer_items)} 份 → 有文本 {len(got)} / "
              f"产物存在但为空 {len(empty)} / 未取得 {len(peer_items) - len(got) - len(empty)}"
              f"{f'；其中兜底采用了 {len(adopted)} 条' if adopted else ''}"
              f"（抓取范围: 多节点 job 且日志侧未定性）")

# 待集群取证清单（第 2 步输入）
if cluster_todo:
    print(f"\n=== 待集群取证 {len(cluster_todo)} 项（第 2 步：用 CI 专用只读 kubeconfig 反查 runner pod）===")
    print(f"  {'runner pod 名':52s} {'芯片':6s} {'失败步骤':28s} 原因")
    for t in cluster_todo:
        print(f"  {(t['runner_name'] or '(未知)'):52s} {t['chip'] or 'gate':6s} "
              f"{t['step'][:26]:28s} {t['reason']}")
    if not ARGS.cluster_kubeconfig:
        print(f"  提示: 未提供 --cluster-kubeconfig，集群取证已跳过（不阻塞第1、3步）。"
              f"待昇腾 CI 专用只读 kubeconfig 就位后，可用上述 runner pod 名直接 kubectl 反查。")

print(f"\n  说明: 失败为抽样(上限{ARGS.samples}份)，百分比为样本内占比。[gate] 表示失败在 CPU 门禁 job 上"
      f"（NPU job 被 skip）。owner 由「失败步骤 + 失败步骤时间窗内日志」共同判定；"
      f"mixed 桶需人工结合 runner 配置/节点网络二次确认，pod 调度类结论需集群侧佐证。")

# 分类完成后，把本仓基础设施相关失败桶（owner∈{infra,mixed}）写回快照存储，供跨仓汇总表聚合
# ⚠️ 落盘一律走 persist_infra_store()：定向模式下它不写盘。infra_failures 是「整仓一轮分析」的
#    结论，单 job 运行只会把它覆盖成 1 条（实测会真的落盘），那不是「本次没数据」而是数据销毁。
repo_entry["infra_failures"] = [
    {"bucket": b, "count": c, "owner": BUCKET_OWNER.get(b, "unknown"),
     "links": [{"url": u, "npu": n} for u, n in bucket_link.get(b, [])]}
    for b, c in classified.most_common()
    if BUCKET_OWNER.get(b, "unknown") in ("infra", "mixed")
]
persist_infra_store(infra_store)

def _fmt_links(links):
    """样例链接列表 -> 'url [NPU]、url [gate]'"""
    return "、".join(f"{l} [{'NPU' if n else 'gate'}]" for l, n in links)

def write_summary():
    """将本次运行的精简版章节写入 --summary-file。
    章节结构：meta → 失败步骤分布 → NPU CI workflows → 近一周成功率
             → 全部失败原因分析(每桶1-3条链接) → 基础设施相关失败 Top3 → 待集群取证。
    章节 slug 含芯片范围（如 vllm-project/vllm-ascend@a2-a3），
    避免芯片范围的章节覆盖掉报告里原有的全量仓库章节。
    --cross-repo 关闭时（默认）只写本章节，不动跨仓表格与章节重排。"""
    slug = SECTION_SLUG
    sec = f"<!-- @section:{slug} -->\n\n"
    sec += f"## {slug}（{SINCE} ~ {datetime.date.today().isoformat()}）\n\n"
    sec += f"- 分析时间: {_T0.strftime('%Y-%m-%d %H:%M:%S')} → {_T1.strftime('%H:%M:%S')}（{(_T1 - _T0).total_seconds():.0f}s）\n"
    sec += f"- 完整原始输出: `{report_path}`\n"
    sec += f"- 芯片范围: `{CHIPS_TAG}`；抽样 {n_sampled} 失败 run → {len(failed_jobs)} 失败 job" \
           f"（NPU {n_npu} / 门禁 fallback {len(failed_jobs)-n_npu}）\n"
    sec += f"- 已定性 {logs_done} 份 → 去重后根因 {n_root_cause} 个" \
           f"{f'（同 run 同根因合并 {dedup_skipped} 次）' if dedup_skipped else ''}，" \
           f"假失败 {sum(false_positive.values())} 份" \
           f"{f'，门禁聚合级联 {sum(aggregate_cascade.values())} 份' if aggregate_cascade else ''}" \
           f"（后两者均不计入根因分布）\n"
    sec += f"- cancelled 采样 {len(cancelled_jobs)} job，未启动/未分配 runner {n_never} 个\n"
    if queue_times:
        q = sorted(queue_times)
        med = q[len(q)//2]/60; mx = q[-1]/60
        over = sum(1 for t in q if t > 1800)
        sec += f"- NPU runner 排队: 中位 {med:.0f}min，最长 {mx:.0f}min，>30min 有 {over} 个（>30min 提示 runner 池不足，infra 侧）\n"
    sec += f"- 日志扫描: 按失败步骤时间窗切分 {window_hits}/{logs_done} 份，其余回退全局尾部窗口\n"
    # 按**记录**的实际桶列，而不是只看 DECISIVE_BUCKETS：相当一部分已定性的记录命中的是
    # 【断言失败】这类 code 桶（判据是「桶判 code + 日志里有 pytest 判定行」），
    # 只列集合里的桶会让括号内的明细与前面的份数对不上
    decisive_buckets = Counter(d["bucket"] for d in detail if d.get("decisive"))
    if decisive_buckets:
        sec += f"- 日志侧已定性为业务侧（`code`）{sum(decisive_buckets.values())} 份（" \
               f"{'、'.join(f'{b}×{n}' for b, n in decisive_buckets.most_common())}）" \
               f"：pytest 自己给出的判定（收集结果/退出码）已指出责任方，" \
               f"**按规则提前退出**，不再排查基础设施的哪个环节失败（不进集群取证清单）\n"
    sec += f"- 方法: 失败步骤（序号最靠前者）决定归因路径——容器/产物上传类直接判 infra 不读日志，" \
           f"安装/构建类扫时间窗前段，测试类扫时间窗尾部（含多节点 job 的 `Stream logs`，" \
           f"它就是跑测试的那一步）；日志里出现 pytest 判定行时直接定性为业务侧并提前退出\n"

    # --- 失败步骤分布：这是本次新增的第一维度，直接决定归因路径 ---
    if step_dist:
        sec += "\n### 失败步骤分布（序号最靠前的失败步骤）\n\n"
        sec += "| 失败步骤 | 失败 job 数 | 归因路径 |\n|---|---|---|\n"
        for name, cnt in step_dist.most_common():
            route, forced_owner, note = route_for(name if name != "步骤未知" else "")
            path_desc = {"no_log": "直接定性 infra（不读日志）", "pod": "多节点编排（需集群侧）",
                         "aggregate": "门禁聚合级联（不计入根因）",
                         "window_head": "扫时间窗前段", "window_tail": "扫时间窗尾部"}.get(route, route)
            sec += f"| {name} | {cnt} | {path_desc} |\n"

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
            sec += (f"| #{i} | {b} | {c} | {c/n_root_cause*100:.0f}% | {BUCKET_OWNER.get(b, 'unknown')} | "
                    f"{_fmt_links(links) if links else '-'} |\n")
    else:
        sec += "| - | (无日志样本可分类) | - | - | - | - |\n"
    sec += "\n**owner 汇总**：" + "，".join(
        f"{o} {sum(by_owner[o].values())}" for o in ("infra", "code", "mixed", "unknown") if by_owner.get(o)) + "\n"
    if false_positive:
        sec += (f"**假失败（不计入根因分布）**：" +
                "，".join(f"{b} {c}" for b, c in false_positive.most_common()) +
                "——draft PR 阻断等非真实错误，按设计文档 §10 需人工识别，此处已单列\n")
    if step_owner:
        sec += ("**按失败步骤直接定性（未读日志）**：" +
                "，".join(f"{s} {n}" for s, n in step_owner.most_common()) + "\n")

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
        sec += "\n> 说明：mixed 桶需结合 runner 配置/节点网络二次确认；pod 调度类结论需集群侧佐证。\n"
    else:
        sec += "无（本次样本内无基础设施相关失败，均为业务方代码/测试问题）。\n"

    # --- 待集群取证：第 2 步的输入清单（runner pod 名是连接键） ---
    sec += "\n### 待集群取证（第 2 步）\n\n"
    if cluster_todo:
        sec += f"以下 {len(cluster_todo)} 项失败无法由日志单独定性，需用 CI 专用只读 kubeconfig 反查 runner pod 调度状态：\n\n"
        sec += "| runner pod 名 | 芯片 | 失败步骤 | 原因 | job 链接 |\n|---|---|---|---|---|\n"
        for t in cluster_todo[:20]:
            sec += (f"| `{t['runner_name'] or '(未知)'}` | {t['chip'] or 'gate'} | {t['step'] or '—'} | "
                    f"{t['reason']} | {t['link']} |\n")
        if len(cluster_todo) > 20:
            sec += f"\n   其余 {len(cluster_todo)-20} 项见完整原始输出。\n"
        if not ARGS.cluster_kubeconfig:
            sec += ("\n> 本次未提供 `--cluster-kubeconfig`，集群取证已跳过（不阻塞第 1、3 步）。"
                    "待昇腾 CI 专用只读 kubeconfig 就位后用上述 runner pod 名反查。\n")
    else:
        sec += "无（本次样本内所有失败均可由失败步骤 + 日志定性）。\n"
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
# ⚠️ 定向模式跳过 write_summary()：它会按 `仓库@芯片` 章节**覆盖**写 npu_ci_failure_report.md 里
# 该仓的整章。单个失败 job 的统计覆盖掉整仓章节，等于把一份汇总报告降级成一次抽样结果。
if INCREMENTAL:
    print("（定向模式：跳过精简版报告章节更新，不改动 npu_ci_failure_report.md）", file=_orig_stdout)
else:
    write_summary()

# ---------- 结构化导出（--emit-json）：第 1 步 → 第 2/4/5 步的交接面 ----------
# 供 npu_ci_forensics.py 消费。detail 里没有 run_id/job_id（只有 link），
# 而集群取证必须按 job_id 关联到 runner_name/labels/时间戳，
# 故在此从 link 反解 job_id 与 failed_jobs 做一次关联，保证导出文件自包含、下游无需再猜。
if ARGS.emit_json:
    _jobs_by_id = {j["job_id"]: j for j in failed_jobs}

    def _enrich(item):
        """把 detail 的一条按 link 里的 job_id 补上集群取证所需的字段"""
        m = re.search(r'/job/(\d+)', item.get("link") or "")
        if not m:
            return dict(item)
        job = _jobs_by_id.get(int(m.group(1)))
        if not job:
            return dict(item)
        return {**item,
                "job_id": job["job_id"], "run_id": job["run_id"],
                "runner_name": job["runner_name"], "labels": job["labels"],
                "is_npu": job["is_npu"], "chip": job["chip"],
                "job_started_at": job["started_at"], "job_completed_at": job["completed_at"],
                "failed_step_started_at": (job["failed_step"] or {}).get("started_at"),
                "failed_step_completed_at": (job["failed_step"] or {}).get("completed_at")}

    _payload = {
        "meta": {
            "repo": f"{OWNER}/{REPO}", "since": SINCE, "chips": ARGS.chips,
            "section_slug": SECTION_SLUG, "generated_at": datetime.datetime.now().isoformat(),
            "report_path": report_path, "summary_file": ARGS.summary_file,
            "samples": ARGS.samples, "dedup_skipped": dedup_skipped,
        },
        # 全量失败 job 记录：run_id/job_id/runner_name/labels/失败步骤时间窗
        "failed_jobs": failed_jobs,
        # 逐条分类结果（已补 job_id/runner_name/labels/时间窗）
        "classifications": [_enrich(d) for d in detail],
        # 待集群取证队列（第 2 步直接消费）
        "cluster_todo": cluster_todo,
        # 桶 → owner 真值表：下游做修复建议映射时不必再解析本脚本源码
        "buckets": [{"label": label, "owner": owner} for _, label, owner in BUCKETS],
    }
    with open(ARGS.emit_json, "w", encoding="utf-8") as fh:
        json.dump(_payload, fh, ensure_ascii=False, indent=2)
    print(f"结构化结果已导出: {ARGS.emit_json}"
          f"（{len(failed_jobs)} 个失败 job / {len(detail)} 条分类 / {len(cluster_todo)} 条待取证）",
          file=_orig_stdout)

# 四仓汇总机制改为可选（--cross-repo）：默认只写本仓本章片范围的章节，
# 不触碰跨仓表格与章节重排，避免单仓单芯片的一次运行改动跨仓报告
if ARGS.cross_repo and not INCREMENTAL:
    update_infra_section()
    update_infra_failure_summary()
    reorder_repo_sections(ARGS.summary_file)
elif INCREMENTAL and ARGS.cross_repo:
    print("（定向模式：--cross-repo 被忽略，跨仓汇总只应由全量运行更新，避免单 job 结果污染跨仓表格）",
          file=_orig_stdout)
else:
    print(f"（未启用 --cross-repo：跳过跨仓基础设施信号表、跨仓失败原因汇总表、仓库章节重排）",
          file=_orig_stdout)
