# 昇腾 NPU CI 失败原因分析 · 设计文档

> 对应实现：`npu_ci_failure_analysis.py`。配套产物：`npu_ci_failure_report.md`（精简版报告）、
> `npu_ci_reports/`（完整原始输出，不入库）。
>
> 本文档同时是**校准记录**：§2.3.1、§7.2、§10 里的「实测」条目都是真实踩坑结论，
> 改动对应代码前请先读那一节，否则容易把已修好的问题改回去。

## 1. 背景与目标

昇腾（Ascend）NPU 仓库的 CI 由 GitHub Actions 驱动，runner 为 ARC/K8s 上的 NPU 节点。失败来源混杂：NPU 硬件/驱动故障、基础设施（网络/磁盘/调度）、PR 引入的代码 bug、精度回归、以及测试框架本身的问题。人工逐个点 run 排查成本高。

**目标**：对跑在 NPU 上的 CI workflow 仓库，低成本产出「近一周失败根因 Top3」，并给出每个根因的样例 run 链接与责任归属（基础设施 / 代码 / 待二次判定 / 证据不足）。

**首轮范围（用户明确收窄）**：仅 `vllm-project/vllm-ascend` 社区，仅 **A2 / A3 卡**的失败。
对应 workflow：`schedule_nightly_test_{a2,a3,a3_560t}.yaml`、`schedule_weekly_test_{a2,a3,a3_560t}.yaml`。
收窄的理由是**保证首轮做透而非铺开**——先把 A2/A3 这条链路（含第 2 步集群取证）打通，再谈扩面。

**设计约束**：
- 脚本保持多仓通用（vllm-ascend / triton-ascend / verl / sglang），不写死单仓特征；但四仓**汇总机制改为可选**（`--cross-repo`，缺省关闭），避免单仓/单芯片分析时误改跨仓表格；
- 只读操作全部走 `gh api`，无写权限要求；
- 抽样有上限，控制 API 调用量与耗时。

## 2. 总体流程

**设计意图（用户明确要求）**：昇腾 CI 资源团队自己反馈「即便有专业知识，只看 workflow 错误日志也看不出啥，还是需要登集群看」。因此工具按三步法取证组织：

1. 查上游仓库 CI workflow 失败日志（`gh api`）——本文档 §4–§8；
2. 登对应 CI 集群看 pod 状态、作业排队信息（`kubectl`）——**当前受阻**，见 §11；
3. 结合两者得出更准确的错误结论。

第 2 步未就位前，第 1 步的输出必须**显式标注哪些失败单靠日志无法定性**（`待集群取证` 队列），而不是给一个貌似确定的结论。

代码上是五步流水线，前两步静态/统计，后三步抽样定性：

```
Step1 静态筛出 NPU CI workflow（按 --chips 预过滤）
  → Step2 近 N 天各 workflow 执行记录（成功率 + 排队时长）
    → Step3 抽样失败 run → 定位失败 job（含 CPU 门禁 fallback）
         并采集：最早失败步骤 / runner pod 名 / 芯片
      → Step4 按「失败步骤」决定扫描窗口与归因路径 → 下载日志 → 根因分类（31 桶 + owner）
        → Step5 Top3 汇总 + 失败步骤分布 + 假失败 + 待集群取证
```

### 2.1 相对旧版的三个核心改进

| 改进 | 旧版 | 新版 | 为什么必须改 |
|---|---|---|---|
| **失败步骤定位** | 不取失败步骤，只看 job 整体失败 | 取 `steps[]` 中 conclusion=failure 且**序号最小**的步骤（后续失败是级联） | 一次 job 失败常报多个失败步骤，不取最早的会把级联症状当根因 |
| **扫描窗口** | 一律扫尾部 1200 行 | 按失败步骤的 `[started_at, completed_at]` 切日志（日志行首带 ISO 时间戳） | 尾部窗口会混入失败后的大量清理噪音；时间窗能干净排除后续级联 |
| **归因路径** | 所有 job 同一套桶 | 按失败步骤路由：`no_log` / `pod` / `window_head` / `window_tail` | 步骤类型决定错误位置：安装类错误在**头部**，测试类在**尾部**，k8s 调度类**根本不在 GitHub 日志里** |

### 2.2 归因路由表

`STEP_ROUTES` 顺序即优先级，命中即路由：

| 失败步骤（正则） | 路径 | 直接 owner | 说明 |
|---|---|---|---|
| `Check * required jobs` | `aggregate` | — | **门禁聚合步骤**：语义是「别的 job 挂了所以我也挂」，必然是级联。不计入根因分布，也不消耗样本预算 |
| `Set up job` / `Initialize containers` | `no_log` | infra | runner 容器初始化失败，日志无信息量，无需下载 |
| `Upload *logs*` / `Upload failed` / `Upload *artifact*` | `no_log` | infra | 产物回传/PVC 失败，是**症状**不是根因（pod 已死才有这步失败） |
| `Wait for pods ready` / `Launch cluster` / `Clear resources` / `Decode kubeconfig` / `Fetch * from PVC` | `pod` | 待定 | k8s 侧问题，GitHub 日志只有 orchestrator 层 → 进 `待集群取证` |
| `Install*` / `Build*` / `Set up *` / `Config mirrors` / `Restore * cache` | `window_head` | 待定 | 错误在步骤输出**开头**，扫尾部会漏 |
| `Stream logs` | `window_tail` | 待定 | **多节点 job 跑测试的那一步**（harness 在此跑 pytest 并流式输出），真因在日志里——`no_log` 会把它整个挡在日志之外 |
| 其余（默认） | `window_tail` | 待定 | 测试/执行类，错误在尾部 |

> ⚠️ `Stream logs` 曾被并进上面那条 `no_log`（当成「日志回传步骤」→ 一律判 infra，**不读日志**）。
> 实测证伪（2026-09-28）：多节点 job 的测试执行步骤名就是 `Stream logs`，
> 其日志里明确写着 `FAILED tests/…::test_external_dp` + `1 failed in 3631.38s` +
> `pytest exit code: ret=1`（用例真失败）；另一批是 `ERROR: file or directory not found: …` +
> `collected 0 items` + `pytest exit code: ret=4`（入口不存在，脚本与代码版本错配）。
> 旧口径把这两类都记成「Runner 与 GitHub 通信问题」，再让第 2 步去集群找 pod 是否被驱逐 ——
> 方向完全反了（该桶占历史语料 19%，是第二大桶）。**步骤名像「收尾」不等于它是收尾**：
> 判定依据只能是它实际执行了什么，而不是它叫什么。

- `no_log`：短路为 infra，**跳过日志下载**（省 API 调用，且避免从无信息量日志里瞎猜）；
- `pod`：不下载日志，直接写入 `待集群取证` 队列（附 runner pod 名）；
- `aggregate`：只计数不归类（见 §7.4）；
- `window_head` / `window_tail`：下载日志，按失败步骤时间窗切片后扫描（切不出窗口时回退全局尾部窗口）。

### 2.3 集群连接键：`runner_name`

`jobs[].runner_name` 直接给出 runner pod 名（如 `linux-aarch64-a3-800t-0-chlqk-runner-frcdl`），**无需解析日志**即可作为第 2 步 `kubectl` 取证的连接键。Step3 对每个失败 job 记录该字段。

### 2.3.1 runner 标签：`is_npu` 与 `chip` 必须同源（踩坑记录）

`is_npu`（job 是否 NPU job，决定报告里的 `[NPU]`/`[gate]` 标注）由 `--npu-label-pattern` 判定；
`chip`（a2/a3/…）由 `chip_of()` 判定。两者是**不同正则**，一旦不一致就会自相矛盾
——实测出现过 `[gate]` 与 `chip=a3` 同时出现，即 **A3 的 NPU job 被标成 CPU 门禁**。

根因：旧默认正则 `linux-(?:aarch64|amd64)-(?:a\d[\w-]*|310p)-\d` 不覆盖
`linux-aarch64-nightly-a3-16` 这种带 `nightly-` 中缀的形态。实测该池出现 **524 次**（第二大池），
连带三处伤害：

1. 报告标 `[gate]`（语义是「失败在 CPU 门禁 job 上」）——**事实错误**；
2. 这些 run 的 `npu_fail` 为空 → 走 fallback 分支 → **该 run 所有失败 job 都被标 `is_npu=False`**；
3. 排队时长统计用同一个正则 → `nightly-a3` 池**整池漏统计**（`run.created_at → job.started_at` 的样本量、中位数、>30min 计数全部低估）。

**教训：采样验证不够，真值集必须用权威全量。** 第一次只拿观察到的 24 个标签验证，
「全部通过」——但权威集里有 102 个。改用
`ascend-gha-runners/docs` 的 `docs/assets/problem-labels.json`（官方「仓库 → 合法 runner 标签」
映射表，19 仓 102 个标签）回归后，又暴露两类漏判：

| 漏判形态 | 后果 | 数量 |
|---|---|---|
| `linux-aarch64-a2b3-v-half` / `-v-quarter` | **A2 板型的变体，在 A2/A3 范围内** → 误标 `[gate]` | 2 |
| `linux-aarch64-910b-{1,2,4,8}` | **整族漏判**（`KNOWN_CHIPS` 里没有 910b）→ 整族误标 `[gate]` | 4 |

另外确认了三种此前没考虑到的形态：**arch 有三种**（`aarch64`/`amd64`/`arm64`）、
**尾部卡数是可选的**（`linux-aarch64-a3`、`a5`、`310p`、`a2b3` 均无卡数后缀）、
**板型 token 需归一**（`a2b1`/`a2b3`/`a2b4` → `a2`）。

修正（三步一起做，缺一不可）：

1. **单一真值源**：新增 `CHIP_FAMILY_TO_CHIP` 表，**同时**驱动 `NPU_LABEL_PATTERN` 与 `chip_of()`
   ——两个正则分叉正是本节的病根，同源后不可能再自相矛盾；
2. `(?:[\w-]*?-)?` 可选中缀（覆盖 `nightly-`），并加 `(?!cpu(?:-|$))` 显式排除 CPU 池；
3. 芯片族后加 `(?:-|$)` 边界，避免 `a3` 在长名里被部分匹配。

⚠️ **改动此正则必须跑 `tests/test_label_classification.py`**：它以那 102 个标签为真值集，
断言「全部 NPU 标签命中 + 全部 CPU 标签排除 + 判为 NPU 的必能提取芯片」。
该测试已反向验证有效（移除 `910b` 会挂 4 项断言）。真值集更新方式见测试文件头。

⚠️ **真值集会滞后于现实，别只看它全绿**：2026-09-24 扫 32 个真实 run 采到 24 个标签，
其中 `linux-aarch64-a3-800it-16`（`800it` 变体）**不在** `problem-labels.json` 里。
所以测试除真值集外另有一组「实测但不在真值集」的用例（`OBSERVED_BUT_NOT_IN_FIXTURE`）。
另：同日用新旧正则对全部 24 个实测标签做对照，**判定完全一致**
——本次修正对现有 A2/A3 范围是行为保持的，只补上了真值集证明存在、但尚未在采样中出现的漏判形态。

## 3. 输入与参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--repo` | `vllm-project/vllm-ascend` | `owner/repo` |
| `--since` | 近 7 天 | 统计起始日期 YYYY-MM-DD |
| `--samples` | 40 | 最多分类的失败日志数（抽样上限） |
| `--sample-per-wf` | 8 | 每个 workflow 抽样失败 run 数 |
| `--sample-cancelled` | 5 | 每个 workflow 采样 cancelled run 数 |
| `--workflow-dir` | 临时目录 | 已下载 workflow 文件目录（缓存复用） |
| `--npu-label-pattern` | 由 `CHIP_FAMILY_TO_CHIP` 生成：`linux-(?:aarch64\|amd64\|arm64)-(?!cpu(?:-\|$))(?:[\w-]*?-)?(?:芯片族)(?:-\|$)` | NPU runner 标签正则。⚠️ **不要手工改字面量**——与 `chip_of()` 同源于 `CHIP_FAMILY_TO_CHIP` 才是设计意图；改动后必须跑 `tests/test_label_classification.py`，详见 §2.3.1 |
| `--chips` | `a2,a3` | 芯片范围（逗号分隔）；**空字符串=不限**。同时过滤 workflow 文件名与 job runner 标签 |
| `--tail-lines` | 1200 | 步骤时间窗**切不出来时**的回退窗口行数 |
| `--no-step-window` | 关闭 | 关闭步骤时间窗切分，退回旧的固定尾部窗口方式（用于新旧结果对照） |
| `--cross-repo` | **关闭** | 启用四仓汇总机制（跨仓基础设施信号表、跨仓失败原因汇总表、仓库章节重排）。缺省只产出本仓本章片范围的章节，不动跨仓表格 |
| `--cluster-kubeconfig` | 无 | **第 2 步预留**：昇腾 CI 专用只读 kubeconfig 路径。⚠️ 不可传 `ascend-backend`（那是社区微服务集群，见 §11） |

- 芯片过滤只丢弃**明确识别出非目标芯片**的 job；`chip=None`（CPU 门禁等）保留——它是 NPU job 被 skip 的原因，对定性有用。
- 产物路径（报告/快照）以 `BASE_DIR`（脚本所在目录）为基准，而非 cwd，避免在仓库根与脚本目录下运行时产物散落两处。

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

**跨仓基础设施信号持久化**：排队/cancelled 统计写入 `--infra-store`（默认 `npu_ci_reports/infra_snapshot.json`，JSON 按仓库分键，每次运行覆盖本仓条目）。精简版报告顶部的「跨仓基础设施信号」表格由 `update_infra_section()` 从 store 自动聚合生成（`<!-- @section:infra-snapshot -->` 标记段，与各仓章节同一套替换机制），跑完四个仓库即自动拼出全量快照，无需手工维护。

## 7. 日志下载与根因分类

### 7.1 日志预处理与切片

- `gh api actions/jobs/{job_id}/logs`（二进制）；gzip 头则解压；
- **丢弃 GitHub Actions 回显的脚本源码行**（`\x1b[36;1m` 青色前缀）——否则正则会命中脚本里写死的报错文案（如 `echo "::error::Failed to fetch PR title..."`）造成误分类，即使该命令实际成功；同理丢弃失败后的清理动作行（`Cleaning up orphan processes`、`Post job cleanup` 等）；
- **按失败步骤时间窗切片**：日志行首带 ISO 时间戳（`2026-09-20T09:40:29.3656474Z`），据此保留落在 `[failed_step.started_at, failed_step.completed_at]` 内的行。切不出来（无时间戳/窗口缺失/`--no-step-window`）则回退扫尾部 `--tail-lines` 行；
- 受 `--samples` 上限约束，够数即停。

时间窗的价值：失败步骤之后的行（清理、`Upload failed` 连锁报错）会被整体排除，避免「症状压过根因」。实测 job `106057742807` 从 2308 行切到 1686 行，排除了 4 条 `Upload failed` 级联错误。

### 7.2 分类桶体系

顺序即优先级，首个命中即归类。共 **31 桶**，`owner` 用于责任归属：

| # | 桶（根因） | 关键信号（简化正则） | owner |
|---|---|---|---|
| 1 | 假失败(draft PR 阻断) | `PR is draft. Blocking CI.` | 假失败 |
| 2 | 多节点pod调度/就绪失败(k8s侧) | `phase=Pending` / `Readiness probe failed` / `0/N nodes are available` / `Insufficient npu` | infra |
| 3 | 分布式通信/网络(HCCL/Store) | `HCCL*error/timeout/failed` / `hcclComm…error` / `DistStoreError` / `StoreError…Timed out` | infra |
| 4 | 模型缓存未命中(离线模式 local_files_only) | `Cannot find the requested files in the cached path` / `outgoing traffic has been disabled` | code |
| 5 | 昇腾NPU硬件错误(507xxx/ERR99999+设备) | `error code( is)? 507\d{3}` / `Device:非-1 … ERR99999` | infra |
| 6 | CANN运行时参数非法(107xxx) | `error code( is)? 107\d{3}` | mixed |
| 7 | 昇腾算子执行错误(ACL) | `NPU function error` / `aclnn* failed` / `error code is \d+` | mixed |
| 8 | 依赖解析/构建失败(含链式噪音) | `No solution found when resolving` / `no version of` / `No matching distribution` / `Failed to build` / `detected dubious ownership` | mixed |
| 9 | 编译失败(C++/MLIR) | `FAILED: [code=1]` / `clang++ error` / `CMake Error` | code |
| 10 | 自定义算子so缺失(csrc构建) | `cannot open shared object file` / `torch_extensions.*.so` | mixed |
| 11 | 进程被kill(OOM/超内存) | `SIGKILL` / `exit code 137` / `OOMKilled` / `Signal 9` | mixed |
| 12 | 分布式通信/编排(Ray) | `RayTaskError` / `ActorDiedError` / `Actor *died` | code |
| 13 | 内网镜像/仓库下载失败 | `Failed to download metadata` / `repomd.xml` / `apt\|yum Failed to fetch` | infra |
| 14 | GitHub API 调用失败 | `Failed to fetch PR title` | infra |
| 15 | 模型/包下载失败(外网) | `HfHubHTTPError` / `huggingface_hub.errors` / `bytes of body are still expected` / `RPC failed` | mixed |
| 16 | 超时 | `timed out` / `TimeoutError` / `UV_HTTP_TIMEOUT` | mixed |
| 17 | OOM/显存不足 | `out of memory` / `aclrtMalloc failed` / `alloc.*failed.*memory` | mixed |
| 18 | 磁盘不足 | `No space left` / `ENOSPC` | infra |
| 19 | 依赖/安装(ImportError) | `ImportError` / `ModuleNotFoundError` | code |
| 20 | 断言失败(代码或精度) | `AssertionError` / `E assert` | code |
| 21 | 静态检查(pre-commit/ShellCheck) | `ShellCheck` / `pre-commit did not succeed` | code |
| 22 | 静态类型检查失败(mypy) | `Found N errors in N files` / `error: … [attr-defined\|assignment\|arg-type\|…]` | code |
| 23 | CI 策略检查(CSRC 变更) | `CSRC build workflows changed` | code |
| 24 | vLLM引擎崩溃(级联，真因在上游) | `Engine core died/failed` / `EngineDeadError` | unknown |
| 25 | Python运行时错误 | `AttributeError` / `TypeError` / `ValueError` / `KeyError` / `IndexError` | code |
| 26 | 测试参数缺失(config未传入) | `must be provided` | code |
| 27 | 昇腾框架异常兜底(ERR99999，非硬件信号) | `ERR99999`（无设备绑定时的兜底，排真实根因桶之后） | unknown |
| 28 | 测试未执行(入口/用例集不存在，脚本与代码错配) | `pytest exit code: ret=4\|5` / `file or directory not found` / `collected 0 items` | code（`decisive`） |
| 29 | 测试用例失败(pytest ret=1) | `pytest exit code: ret=1` | code（`decisive`） |
| 30 | 步骤被强制终止(exit 255，非根因) | `exit code 255` / `command terminated with exit code 255` | infra |
| 31 | 脚本步骤通用包装失败(需按失败步骤细化) | `failed to run script step` | unknown |

**排序不是随意的——以下顺序都是踩坑后校准的，改动需回归验证**：

- **桶 3 先于桶 7**：否则 `hcclComm_), error code is 7` 会被 `error code is \d+` 吞进 ACL 桶，owner 从 infra 错配成 mixed；
- **桶 5/6 按错误码分档**（依据 `classification-guide.md` 场景 C）：`507xxx` 是硬件/驱动故障（infra）；`107xxx` 是 CANN runtime 参数非法，不是硬件信号（mixed）。旧版一律归 ACL/mixed，把硬件故障漏成了「待判定」；
- **桶 4 先于桶 5，裸 `ERR99999` 下沉到桶 27**（2026-09-20 实测纠偏）：`ERR99999` 是昇腾对「任意未捕获应用层异常」的**通用兜底包装**，**不是硬件信号**——实测两例（job `106046329358` 模型缓存未命中、job `105440985558` 投机解码断言失败）都是紧跟在真实 Python traceback 之后打印，同行 `Device:-1, RankID:-1` 表示**未绑定 NPU 设备**。旧版把 `ERR99999` 无条件并进硬件桶，导致这两例用户侧问题被判成 infra。改法：① 硬件桶只认 `507xxx`，或 `ERR99999` 且同行 `Device/RankID` 非 `-1`；② 裸 `ERR99999` 下沉到桶 27 标 `unknown`，让真实根因先命中（实测两例分别纠正为桶 4 `code` 与桶 20 `code`）。⚠️ 与「桶 24 早于桶 25」同一原则：**级联症状不能压倒根因**；
- **桶 8 先于桶 9/24**：依赖解析失败会连锁产生大量 `error`/`failed` 噪音，不前置则根因被级联噪音吞掉；
- **桶 9 带负向前瞻**排除 `7739 bytes of body are still expected`——这是网络下载不全，旧版被 `error:.*expected` 误判成编译失败并把 owner 从 mixed 错配成 code；
- **桶 12 带两处负向前瞻**（`RayTaskError(?!\(Assertion)` 和 `ray\.exceptions(?![^\n]{0,60}Assertion)`）——`ray.exceptions.RayTaskError(AssertionError)` 本质是断言失败，应落到桶 20。⚠️ 两处缺一不可：断言写在**括号里**，只挡点号形式会漏网（已实测踩坑）；
- **桶 15 需收紧**：裸 `huggingface_hub` 会命中正常进度行 `Downloading huggingface_hub-1.30.0-py3-none-any.whl`，故必须限定为 `.errors` 或后随 `Error|Timeout|Failed|Connection`；
- **桶 28/29（pytest 判定行）插在桶 27 之后、桶 30/31 之前**（2026-09-28 新增，三处顺序都要对）：
  它们必须晚于硬件/网络/OOM 等真根因桶（否则「OOM 导致用例失败」会被写成业务侧用例失败），
  又必须早于 `exit 255` 与 `failed to run script step` 这两个通用包装桶（否则真判定被外层包装覆盖成
  unknown/infra，即改前的实际行为）。语义与「提前退出」的联动见 §7.4；
- **桶 24 必须早于桶 25/30**：`RuntimeError: engine core died` 是**级联症状**（引擎子进程被更早的错误打死，真因在其上游日志）。若不单列，它会落到桶 30 被标成 owner=infra——等于给一个我们并不掌握的责任方下结论；
- **桶 30 排在真实根因桶之后**：exit 255 是 K8s 强杀，本身不是根因，只有确实无其他信号时才归到这里（它之后只剩桶 31 这个「脚本步骤通用包装」兜底桶）；
- **桶 31 命名已更正**：`failed to run script step` 是 GitHub 对「任意脚本步骤失败」的通用包装，**并非多节点专属**（实测 sglang/triton 的 CPU 门禁 job 也被它命中），旧桶名「多节点编排层包装失败」属误命名；
- **桶 22 是补漏**：mypy 的真实错误形态（实测 job `106079239560`）是
  `pool_scheduler.py:175: error: "KVPoolScheduler" has no attribute "mamba_group_ids"  [attr-defined]`
  + `Found 1 error in 1 file (checked 615 source files)`。旧版没有任何桶匹配它 → 落到桶 31 被标 `unknown`。
  实测 6/40 份样本（15%）因此被误归 unknown。**注意与同一 run 的 `cpu-ut` job 的关系**：同一个属性缺失
  会让 UT 崩成 `AttributeError`（桶 25），也就是**同一根因落进两个桶**——这正是按 `(run, 桶)` 去重之外，
  仍需要人工留意「同 run 跨桶同源」的原因（当前未自动合并，见 §10）。

**owner 归属**：
- `infra` = 基础设施（资源/调度/网络/存储），直接责任；
- `code` = 业务方（编译/依赖/断言/配置）；
- `mixed` = 需二次判定（如 ACL 算子错误可能是硬件也可能是兼容性；下载可能是网络也可能是版本不存在）；
- `unknown` = **证据不足，不给结论**。配合 §7.4 的 `待集群取证` 队列使用；
- `假失败` = 不是真实失败（draft PR 阻断等），不计入根因分母。

### 7.3 证据与样例链接

- 每条分类记录证据片段：命中位置 ±30 字符（换行折叠）；
- 未命中任何桶 → 「未分类」，用 `(FAILED|Error|error:)` 兜底截证据；
- 每桶至少保留**一条样例 run 链接**；同一桶后续若命中 NPU job 则覆盖 gate 链接（更接近真实 NPU 失败）。

### 7.4 两步短路与反查队列

Step4 的分类循环据路由结果分三条出口：

| 路由 | 处理 | 产出 |
|---|---|---|
| `aggregate` | 不下载日志、不归桶 | 仅计数，**不计入根因分布**，也不消耗 `--samples` 预算 |
| `no_log` | 不下载日志，直接按表的 owner 定性 | 计入根因，标注「无需日志」 |
| `pod` | 不下载日志 | 写入 `待集群取证` 队列（附 runner pod 名 + 失败步骤） |
| `window_head`/`window_tail` | 下载日志 + 时间窗切片 + 31 桶扫描 | 计入根因；全部未命中 → 也进 `待集群取证` |

**第四种出口：日志侧已定性为业务侧 → 提前退出（`decisive`，2026-09-28 新增）**

读日志的路径上还有一次**提前退出**：若日志里出现**测试框架自己打印的判定行**，且结论指向业务侧，
则该失败**不进** `待集群取证`、第 2 步也不再为它查集群。判据（`is_decisive()`）两条满足其一：

| 判据 | 桶 | owner | 实测形态 |
|---|---|---|---|
| 桶本身在 `DECISIVE_BUCKETS` 里 | 【测试未执行(入口/用例集不存在，脚本与代码错配)】 | code | `ERROR: file or directory not found: …` + `collected 0 items` + `pytest exit code: ret=4` |
| 同上 | 【测试用例失败(pytest ret=1)】 | code | `1 failed, 14 warnings in 3631.38s` + `pytest exit code: ret=1` |
| 桶判 `code` **且**日志有 `pytest exit code: ret=\d+` | 任意 code 桶（如【断言失败】【Python运行时错误】） | code | 尾部带断言 traceback 的常见形态 |

三个必须一起记住的点：

1. **顺序即优先级**：这两条 pytest 桶必须排在硬件/网络/OOM 等真根因桶**之后**（否则「OOM 导致用例失败」会被写成业务侧用例失败），
   又必须排在 `exit code 255`、`failed to run script step` 等**通用包装桶之前**（否则真判定被外层包装覆盖成 unknown/infra）。
2. **第三条判据不能省**：最常见的 ret=1 形态尾部带断言 traceback，会先命中更靠前的【断言失败】桶
   （owner 同为 code，但不在 `DECISIVE_BUCKETS` 里）。只按桶名判会漏掉整整一类，它们照样会占掉取证名额。
3. **只在桶判 code 时才算已定性**：桶判 `mixed`/`infra`（OOM、HCCL、节点调度…）即便日志里有 ret=1 也**不**跳过
   —— 那时责任方尚未落在业务侧，集群侧证据仍可能是关键，不能把硬件问题读成业务问题。

下游三处联动（缺一不可，`tests/test_pytest_verdict.py` 逐条守着）：
`cluster_todo` 不收它们 · `select_cases` 让它们**必进报告但不占 `--max-cases` 名额**（名额是留给「不查集群就定不了性」的）·
报告里写「**按规则跳过**」而**不是**「未取证」（后者是「查了没查到」，语义相反）。

为什么不能简单地把它们从 case 列表里剔掉：报告只渲染传进去的 case，归因分布也由 case 统计 ——
剔掉等于让业务侧失败从此在报告里消失（静默丢信息），而不是「少花一次集群查询」。

**去重口径（关键）**：计数键是 `(run_id, 桶)`，同一 run 内的同一根因只计一次。

理由（实测）：40 份样本只对应 **26 个 run**，重复率 35%；极端 run `35452779632` 被计 **5 次**
（5 个 job 都归【依赖/安装(ImportError)】）——同一根因算 5 份，Top3 的「次数/占比」被膨胀。
去重后报告同时给出去重前的份数与合并次数，避免读者误判分母。

注意去重是**按桶**而非按 run：同一 run 内若确有多个不同的根因，仍会分别计数。
但「同 run 跨桶同源」（如 mypy 的 `no attribute` 与 UT 的 `AttributeError` 是同一处代码缺陷）
**当前不会自动合并**，见 §10。

**分母口径**：报告所有百分比的分母是**去重后的根因数**，且已排除 `aggregate`（级联）与
`假失败`（非真实失败）两类。分母在报告里显式写出，便于核对。

`待集群取证` 队列是第 1 步与第 2 步之间的**显式接口**：它不假装知道答案，而是列出「日志侧已尽力、必须登集群才能定性」的失败清单。这是本工具对「仅看日志看不出啥」这一反馈的结构性回应。

## 8. Top3 汇总输出

报告章节按 `SECTION_SLUG`（`{repo}` 或 `{repo}@{chips}`）定位，只写自己这一节；跨仓表格由 `--cross-repo` 控制是否联动。

输出内容：
- 按计数取 `most_common(3)`，输出次数、占比、样例链接（标注 NPU / gate）；
- 附全部分类明细；
- **失败步骤分布**：失败集中在哪些步骤上（决定归因路径的输入，也是判断「该不该登集群」的依据）；
- **假失败**：单列，不计入根因分母；
- **待集群取证**：见 §7.4，第 2 步的输入队列；
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

- **抽样上限**：百分比为样本内占比，不是全量统计。分母已从「下载成功份数」改为「**按 run 去重后的根因数**」（`aggregate` 级联与假失败均不计入），否则百分比会被未下载的样本、重复 job 与级联失败三重稀释；
- **去重只到「按桶」粒度**：同一 run 内**同源但落进不同桶**的失败不会自动合并。实测 mypy 的
  `"KVPoolScheduler" has no attribute "mamba_group_ids"`（桶 22）与同一 run 的 UT 崩溃
  `AttributeError: 'KVPoolScheduler' object has no attribute ...`（桶 25）是**同一处代码缺陷**，
  但会计成 2 个根因。跨桶同源识别需要更深的语义关联，当前未做；
- **样本预算未随去重收缩**：去重发生在计数阶段，日志下载仍按 job 进行。因此一个 run 若有 5 个同因 job，
  仍会下载 5 份日志（只是计 1 个根因），`--samples` 额度存在浪费。要在采样阶段就合并需要先知道桶，
  而桶要读日志才能定，除非改成「同 run 限 N 个 job」的采样策略；
- **`aggregate` 的判定是白名单**：只覆盖 `^Check … required jobs?` 形态。若某仓库用别的步骤名做门禁聚合
  （如 `Verify gate`），仍会被当根因计入，需按实测补进 `STEP_ROUTES`；
- **无法识别「基线分支缺陷」（重要）**：当缺陷在 `main` 上时，每个从 main 切出的 PR 都会**继承同一个失败**，
  工具会把这些 run 当成 N 个独立根因逐个计数（owner 记 `code`），而真相是**1 个系统性缺陷 + N 个受害 PR**。

  实测（2026-09-20）：mypy 报 `pool_scheduler.py:175: "KVPoolScheduler" has no attribute "mamba_group_ids"`
  共 6 次，分别来自 6 个**不同分支**（含 `main` 自身），却报同一个错误。核对 main 上的文件确认：
  `KVPoolScheduler.__init__`（第 64 行）从未赋值 `mamba_group_ids`，全文件仅第 175 行引用它——
  **main 本身是坏的**。同一缺陷还会让 `cpu-ut` 崩成 `AttributeError`（桶 25），
  并使 `pr_test.yaml` 成功率跌到 15%（一周 57 次失败），实际受害的是**所有 PR**，不只是这 6 个。

  识别思路（尚未实现）：同一错误签名在**多个不同 head_branch** 上重复出现、且其中一个分支是默认分支时，
  应判为基线缺陷而非 N 个 PR 缺陷。这需要把 `head_branch` 纳入分析并做签名聚合；
- **时间窗依赖日志时间戳**：窗口切不出来时静默回退到尾部窗口，此时会退化回旧版的级联噪音问题。可用 `--no-step-window` 显式对照，但**无法从输出区分**「窗口生效」与「回退」，是当前的一个可观测性缺口；
- **多节点日志缺失（结构性）**：多节点测试的 GitHub 日志只有 orchestrator 层，真实错误在 k8s pod 日志——这正是必须走第 2 步的原因，不是本工具能修的；
- **cancelled 语义**：cancelled 且从未启动 → 调度/资源问题；否则多为主动取消/上游中断；
- **未分类兜底**：依赖 `(FAILED|Error|error:)` 正则，可能把非根因的普通报错行当证据。这类样本现在也会进 `待集群取证`，不再硬给一个桶；
- **桶体系是经验校准的产物**：31 桶的**顺序**承载了大量踩坑结论（见 §7.2 的校准说明），新增桶时必须回归验证既有样例，不能只测新样例。

## 11. 第 2 步（集群取证）· 状态与边界

**当前状态：已接入。** kubeconfig 已由用户提供于 `~/kconf/asci/`（7 个集群可达），
集群取证、历史归因、根因输出的完整实现见 **[`npu_ci_forensics_design.md`](./npu_ci_forensics_design.md)**，
入口为 `npu_ci_forensics.py`。本节只保留第 1 步需要知道的连接键与边界。

本步骤（第 1 步）为下游提供的接口：
- `待集群取证` 队列（含 runner 标签、`runner_name`、失败**步骤**及其起止时间）；
- `runner_name` 字段作为连接键（与 pod 名精确匹配时身份确凿）；
- 失败步骤的时间窗 —— 第 2 步判断「某个 pod 是否可能承载过本 job」必须用它。

⚠️ **集群身份边界（用户明确告知，勿踩）**：
- `~/kconf/infra-hk-test-cluster-002-ascend-backend-robot-kubeconfig`（`ascend-backend`）**不是昇腾 CI 的资源集群**，而是**昇腾社区的微服务部署集群**。**不可用它做 CI 取证**——拿错集群的证据去解释 CI 失败，比没有证据更糟。
- `~/.kube/config` 内容是字面量 `KUBECONFIG-DATA-FAKE`，不是合法 kubeconfig，默认 `kubectl` 连不上任何集群；必须显式指定 `KUBECONFIG=<路径>`。
- runner 标签后缀**不能**用于判集群（Liqo 会把虚拟节点 pod 反射进共享 namespace），
  集群归属以 `ascend-gha-runners/docs` 的 `Cluster.md` 登记为准。详见新文档 §3.2。
- 但反过来也**不能**盲信 `Cluster.md`：登记的全名后缀（`…-cn12-001`）与 pod 名实际后缀
  （实测 `…-chlqk-runner-*`）可能不一致，按登记全名查空**不能**推出「该标签不存在」。
  详见新文档 §3.3 / §3.6 与 `tests/test_runner_availability.py`。

> 📍 **监听器（近实时自动运行）**：本步骤所在的五段流水线已由 `npu_ci_watch.py` 常驻驱动 ——
> 目标 workflow 一失败就抢集群快照（pod 是一次性的），job 结束后自动补日志分类与报告。
> 为什么必须两阶段、台账怎么防重复与防静默丢弃、systemd 部署，见
> [npu_ci_forensics_design.md](npu_ci_forensics_design.md) 的 §9 与 [deploy/README.md](deploy/README.md)。
