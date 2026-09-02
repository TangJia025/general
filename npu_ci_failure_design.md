# 昇腾 NPU CI 失败原因分析 · 设计文档

> 依据 `npu_ci_failure_analysis.py`（四仓通用）逆向整理。
> 配套产物：`npu_ci_failure_report.md`（分析报告）。

## 1. 背景与目标

昇腾（Ascend）NPU 仓库的 CI 由 GitHub Actions 驱动，runner 为 ARC/K8s 上的 NPU 节点。失败来源混杂：NPU 硬件/驱动故障、基础设施（网络/磁盘/调度）、PR 引入的代码 bug、精度回归、以及测试框架本身的问题。人工逐个点 run 排查成本高。

**目标**：对任意一个跑在 NPU 上的 CI workflow 仓库，低成本产出「近一周失败根因 Top3」，并给出每个根因的样例 run 链接与责任归属（基础设施 / 代码 / 待二次判定）。

**设计约束**：
- 四仓通用（vllm-ascend / triton-ascend / verl / sglang），不写死单仓特征；
- 只读操作全部走 `gh api`，无写权限要求；
- 抽样有上限，控制 API 调用量与耗时。

## 2. 总体流程

五步流水线，前两步为静态/统计，后三步为抽样定性：

```
Step1 静态筛出 NPU CI workflow
  → Step2 近 N 天各 workflow 执行记录（成功率）
    → Step3 抽样失败 run → 定位失败 NPU job（含 CPU 门禁 fallback）
      → Step4 下载日志 → 尾部窗口根因分类（16 桶 + owner 归属）
        → Step5 Top3 汇总 + 样例链接
```

## 3. 输入与参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--repo` | `vllm-project/vllm-ascend` | `owner/repo` |
| `--since` | 近 7 天 | 统计起始日期 YYYY-MM-DD |
| `--samples` | 40 | 最多分类的失败日志数（抽样上限） |
| `--sample-per-wf` | 8 | 每个 workflow 抽样失败 run 数 |
| `--sample-cancelled` | 5 | 每个 workflow 采样 cancelled run 数 |
| `--workflow-dir` | 临时目录 | 已下载 workflow 文件目录（缓存复用） |
| `--npu-label-pattern` | `linux-(?:aarch64\|amd64)-(?:a\d[\w-]*\|310p)-\d` | NPU runner 标签正则 |
| `--tail-lines` | 1200 | 日志分类扫描的尾部窗口行数 |

依赖：`gh` CLI 已认证（可读目标仓库）。

---

## 4. NPU CI workflow 筛选逻辑（静态）

通过 `gh api repos/{owner}/{repo}/contents/.github/workflows` 拉取该仓库**全部** workflow 文件，逐个做三层判定。

### 4.1 预处理与 CD 排除

文件名命中任一关键词直接跳过（CD/辅助类 workflow，即使引用了 NPU 镜像也排除，如 `build-docker`）：

```
release, build-docker, docker-build, wheels, create_release,
sync-, sync_, auto-label, stale, docs, documentation,
pre-commit, precommit, check-pr, pr-title, dco, ocr,
rebuild, protected, llvm-build, auto-
```

### 4.2 特征提取

对每个 workflow 文件扫 4 种特征（正则命中即标记）：

| 特征 | 正则命中 | 含义 |
|---|---|---|
| `direct_aarch64` | `runs-on: linux-aarch64` | 直接跑在 aarch64 runner 上（昇腾 NPU 基本都 aarch64） |
| `npu_smi` | 出现 `npu-smi` | 直接操作 NPU 硬件的命令 |
| `cann_image` | 镜像含 `swr.cn-southwest-2.myhuaweicloud.com` 且邻近 `ascend-ci` | 使用昇腾 CANN 容器镜像 |
| `dynamic_runner` | `runs-on: ${...}` | 动态 matrix runner |

### 4.3 三级判定

**① 强特征直接判定**：

- 命中 `direct_aarch64` **或** `npu_smi` → 直接判定为 NPU workflow（直接硬件信号）；
- `dynamic_runner` **且** `cann_image` 同时命中 → 判定（NPU 测试执行模板，如 vllm `_selected_tests.yaml` 的 `matrix.group.runner` 就是 `linux-aarch64-*`）。

> ⚠️ `cann_image` 单独出现**不能**判强 —— CPU runner 也能用 CANN 容器做编译检查（triton `DynamicCVPipeline-ci` 是反例，曾误判）。

**② 弱特征 + 文件名兜底**：

只有 `dynamic_runner` 或 `cann_image` 单独命中时，还需文件名带 `npu`/`ascend` 才纳入。裸 `dynamic_runner` 会污染 AMD/ROCm/release workflow。

**③ 传递 `uses:` 判定**：

部分顶层 workflow（如 triton `ci.yml`）自身无任何强特征，但 `uses: ./.github/workflows/integration-tests-ascend.yml` 间接引用了强特征文件。沿 `uses: ./` 链做 BFS 递归，能到达强特征文件的即判定为 NPU workflow。

### 4.4 判定汇总（伪代码）

```
for 每个 workflow f:
    if is_strong(feats):                     # ① direct_aarch64 / npu_smi / (dynamic+cann)
        纳入 candidates
    elif dynamic_runner 或 cann_image in feats:
        if 文件名匹配 npu|ascend:            # ② 弱特征兜底
            纳入 candidates
    elif transitively_uses_npu(f):           # ③ uses 传递链
        纳入 candidates
```

---

## 5. 执行记录统计

对每个候选 workflow：
- 跳过 `_` 前缀文件与 `workflow_call`-only（无独立 run 记录）的可复用 workflow；
- `gh api actions/workflows/{f}/runs?per_page=100&created=>since`；
- 按 `conclusion` 计数 success / failure / cancelled，输出：

```
  文件名  total=N success=… failure=… cancelled=… 成功率=…
```

> 成功率 = success / (success + failure)。cancelled 单独列出——它常对应 runner 挂掉/节点故障（infra），而非业务失败。

## 6. 失败抽样与 NPU job 定位（含门禁 fallback）

对每个 workflow，按失败数降序排序，取前 `sample-per-wf` 个失败 run：

1. 拉该 run 的全部 jobs；
2. 过滤 **label 命中 NPU 标签正则**且 `conclusion=failure` 的 job → 记为 NPU job；
3. **门禁 fallback**：若无 NPU 失败 job（NPU job 常被 skip），降级到该 run 的**全部**失败 job，标记为 gate（is_npu=False）——sglang 场景必需，失败常发生在 CPU 门禁 `pr-gate`。

**附带的 infra 信号**（抽样时顺带采集）：
- **cancelled run 采样**：job `conclusion=cancelled`，记录是否从未启动（无 `started_at`）。未启动占比高 → 调度/资源问题。
- **排队时长**：run 创建时间 → NPU job 实际启动时间的间隔。>30min 提示 runner 池不足（infra 侧）。

## 7. 日志下载与根因分类

### 7.1 日志预处理

- `gh api actions/jobs/{job_id}/logs`（二进制）；gzip 头则解压；
- 只扫**尾部 `tail-lines` 行**（默认 1200）——日志前 80% 是安装/构建噪音，错误集中在尾部；
- 受 `--samples` 上限约束，够数即停。

### 7.2 分类桶体系

顺序即优先级，首个命中即归类。共 16 桶，`owner` 用于责任归属：

| # | 桶（根因） | 关键信号（简化正则） | owner |
|---|---|---|---|
| 1 | 编译失败(C++/MLIR) | `FAILED: [code=1]` / `clang++ error` / `CMake Error` | code |
| 2 | 昇腾算子执行错误(ACL) | `NPU function error` / `aclnn* failed` / `error code is \d+` | mixed |
| 3 | 自定义算子so缺失(csrc构建) | `cannot open shared object file` / `torch_extensions.*.so` | mixed |
| 4 | 进程被kill(OOM/超内存) | `SIGKILL` / `exit code 137` / `OOMKilled` / `Signal 9` | mixed |
| 5 | 分布式通信/网络(HCCL) | `HCCL*error/timeout/failed` / `Connection reset` / `CollectiveError` | infra |
| 6 | 分布式通信/编排(Ray) | `RayTaskError` / `ActorDiedError` / `Actor *died` | code |
| 7 | 内网镜像/仓库下载失败 | `Failed to download metadata` / `repomd.xml` / `apt|yum Failed to fetch` | infra |
| 8 | 模型/包下载失败(外网) | `HfHubHTTPError` / `Curl error` / `503` / `rate limit` / `404` | mixed |
| 9 | 超时 | `timed out` / `TimeoutError` / `UV_HTTP_TIMEOUT` | mixed |
| 10 | OOM/显存不足 | `out of memory` / `aclrtMalloc failed` / `alloc.*failed.*memory` | mixed |
| 11 | 磁盘不足 | `No space left` / `ENOSPC` | infra |
| 12 | 依赖/安装(ImportError) | `ImportError` / `ModuleNotFoundError` | code |
| 13 | 断言失败(代码或精度) | `AssertionError` / `E assert` | code |
| 14 | 静态检查(ShellCheck) | `ShellCheck` | code |
| 15 | Python运行时错误 | `AttributeError` / `TypeError` / `ValueError` / `KeyError` / `IndexError` | code |
| 16 | 测试参数缺失(config未传入) | `must be provided` | code |

**owner 归属**：
- `infra` = 基础设施（资源/调度/网络/存储），直接责任；
- `code` = 业务方（编译/依赖/断言/配置）；
- `mixed` = 需二次判定（如 ACL 算子错误可能是硬件也可能是兼容性；下载可能是网络也可能是版本不存在）。

### 7.3 证据与样例链接

- 每条分类记录证据片段：命中位置 ±30 字符（换行折叠）；
- 未命中任何桶 → 「未分类」，用 `(FAILED|Error|error:)` 兜底截证据；
- 每桶至少保留**一条样例 run 链接**；同一桶后续若命中 NPU job 则覆盖 gate 链接（更接近真实 NPU 失败）。

## 8. Top3 汇总输出

- 按计数取 `most_common(3)`，输出次数、占比、样例链接（标注 NPU / gate）；
- 附全部分类明细；
- 必带说明：百分比为样本内占比；`[gate]` 表示失败在 CPU 门禁 job 上（NPU job 被 skip）；多节点测试的 GitHub 日志只有 orchestrator 层，真错误在 k8s pod 日志里。

---

## 9. 适用仓库差异（已实测校准）

| 仓库 | 特征差异 | 对筛选的影响 |
|---|---|---|
| vllm-ascend | NPU job 直接跑 `linux-aarch64-{a2,a3,a5,310p}-N` | 强特征命中即可，最直接 |
| triton-ascend | 顶层 `ci.yml` 无直接特征，靠 `uses: integration-tests-ascend.yml` 传递；**a5 昇腾950 跑在 amd64** | 必须走传递 `uses` 判定；NPU 标签正则须含 `amd64` |
| verl | 大量 `*_ascend.yml`，全 aarch64；`docker-build-ascend-*` 是 CD | CD 排除规则生效，防误纳入 |
| sglang | 失败常发生在 CPU 门禁 `pr-gate`，NPU job 被 skip | 门禁 fallback 必需 |

## 10. 已知局限

- **抽样上限**：百分比为样本内占比，不是全量统计；
- **尾部窗口**：只扫最后 `tail-lines` 行，头部错误（依赖解析、安装）可能漏判；命中率经真实日志校准，但依赖解析类错误需要专门 grep 兜底；
- **多节点日志缺失**：多节点测试 GitHub 只有 orchestrator 层，真实错误在 k8s pod 日志；
- **cancelled 语义**：cancelled 且从未启动 → 调度/资源问题；否则多为主动取消/上游中断；
- **假失败干扰**：sglang 的 draft PR 阻断（`PR is draft. Blocking CI.`）会在门禁上产生大量假失败，需人工识别，不属于真实错误；
- **未分类兜底**：依赖 `(FAILED|Error|error:)` 正则，可能把非根因的普通报错行当证据。
