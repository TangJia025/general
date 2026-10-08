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
      → Step4 按「失败步骤」决定扫描窗口与归因路径 → 下载日志 → 根因分类（32 桶 + owner）
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
| `--no-peer-logs` | 关闭 | 不抓取 `*-ascend-logs` 产物（多节点 job 的对端节点日志，见 §7.5） |
| `--peer-log-lines` | 400 | 每个对端节点取尾部多少行参与判桶。对端文本无时间窗对齐，取尾部是唯一可行的粗切 |
| `--artifact-cache-dir` | `<脚本目录>/.forensics_cache/artifacts` | 对端产物 zip 的缓存目录（按 artifact_id 存盘，同一 run 的多个失败 job 只下载一次；已在 `.gitignore`） |
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

顺序即优先级，首个命中即归类。共 **32 桶**，`owner` 用于责任归属：

| # | 桶（根因） | 关键信号（简化正则） | owner |
|---|---|---|---|
| 1 | 假失败(draft PR 阻断) | `PR is draft. Blocking CI.` | 假失败 |
| 2 | 多节点pod调度/就绪失败(k8s侧) | `phase=Pending` / `Readiness probe failed` / `0/N nodes are available` / `Insufficient npu` | infra |
| 3 | HCCL 集合通信失败 | `HCCL*error/timeout/failed` / `hcclComm…error` / `CollectiveError` | infra |
| 4 | Store 会合超时(TCPStore，对端 rank 未加入) | `DistStoreError` / `StoreError…Timed out` / `DistNetworkError` / `recvValueWithTimeout failed` / `waiting for clients` / `Connection reset` / `broken pipe` | infra |
| 5 | 模型缓存未命中(离线模式 local_files_only) | `Cannot find the requested files in the cached path` / `outgoing traffic has been disabled` | code |
| 6 | 昇腾NPU硬件错误(507xxx/ERR99999+设备) | `error code( is)? 507\d{3}` / `Device:非-1 … ERR99999` | infra |
| 7 | CANN运行时参数非法(107xxx) | `error code( is)? 107\d{3}` | mixed |
| 8 | 昇腾算子执行错误(ACL) | `NPU function error` / `aclnn* failed` / `error code is \d+` | mixed |
| 9 | 依赖解析/构建失败(含链式噪音) | `No solution found when resolving` / `no version of` / `No matching distribution` / `Failed to build` / `detected dubious ownership` | mixed |
| 10 | 编译失败(C++/MLIR) | `FAILED: [code=1]` / `clang++ error` / `CMake Error` | code |
| 11 | 自定义算子so缺失(csrc构建) | `cannot open shared object file` / `torch_extensions.*.so` | mixed |
| 12 | 进程被kill(OOM/超内存) | `SIGKILL` / `exit code 137` / `OOMKilled` / `Signal 9` | mixed |
| 13 | 分布式通信/编排(Ray) | `RayTaskError` / `ActorDiedError` / `Actor *died` | code |
| 14 | 内网镜像/仓库下载失败 | `Failed to download metadata` / `repomd.xml` / `apt\|yum Failed to fetch` | infra |
| 15 | GitHub API 调用失败 | `Failed to fetch PR title` | infra |
| 16 | 模型/包下载失败(外网) | `HfHubHTTPError` / `huggingface_hub.errors` / `bytes of body are still expected` / `RPC failed` | mixed |
| 17 | 超时 | `timed out` / `TimeoutError` / `UV_HTTP_TIMEOUT` | mixed |
| 18 | OOM/显存不足 | `out of memory` / `aclrtMalloc failed` / `alloc.*failed.*memory` | mixed |
| 19 | 磁盘不足 | `No space left` / `ENOSPC` | infra |
| 20 | 依赖/安装(ImportError) | `ImportError` / `ModuleNotFoundError` | code |
| 21 | 断言失败(代码或精度) | `AssertionError` / `E assert` | code |
| 22 | 静态检查(pre-commit/ShellCheck) | `ShellCheck` / `pre-commit did not succeed` | code |
| 23 | 静态类型检查失败(mypy) | `Found N errors in N files` / `error: … [attr-defined\|assignment\|arg-type\|…]` | code |
| 24 | CI 策略检查(CSRC 变更) | `CSRC build workflows changed` | code |
| 25 | vLLM引擎崩溃(级联，真因在上游) | `Engine core died/failed` / `EngineDeadError` | unknown |
| 26 | Python运行时错误 | `AttributeError` / `TypeError` / `ValueError` / `KeyError` / `IndexError` | code |
| 27 | 测试参数缺失(config未传入) | `must be provided` | code |
| 28 | 昇腾框架异常兜底(ERR99999，非硬件信号) | `ERR99999`（无设备绑定时的兜底，排真实根因桶之后） | unknown |
| 29 | 测试未执行(入口/用例集不存在，脚本与代码错配) | `pytest exit code: ret=4\|5` / `file or directory not found` / `collected 0 items` | code（`decisive`） |
| 30 | 测试用例失败(pytest ret=1) | `pytest exit code: ret=1` | code（`decisive`） |
| 31 | 步骤被强制终止(exit 255，非根因) | `exit code 255` / `command terminated with exit code 255` | infra |
| 32 | 脚本步骤通用包装失败(需按失败步骤细化) | `failed to run script step` | unknown |

**排序不是随意的——以下顺序都是踩坑后校准的，改动需回归验证**：

- **桶 3/4 先于桶 8**：否则 `hcclComm_), error code is 7` 会被 `error code is \d+` 吞进 ACL 桶，owner 从 infra 错配成 mixed；
- **桶 3 与桶 4 必须分开**（2026-09-29 拆分，原为合并桶「分布式通信/网络(HCCL/Store)」）：两者是**相反**的机制
  ——① 是「进程凑齐了但集合通信出错」，② 是「进程根本没凑齐」。合并时两桶共用知识表的官方叶子
  `leaf_hccl_port_bound`（HCCL 通信端口被占用），于是**每一次 Store 会合超时都会生成一句错的
  「官方口径对齐：HCCL 通信端口被占用」**。实测 job `109264350421`（run 36518916532）即如此：日志里既无 HCCL
  错误码、也无 `bind`/`address already in use`，真因是对端节点（node1）迟到导致 TCPStore 会合超时；
  ② 的桶名读作「对端 rank 未加入」才对得上。⚠️ ② 刻意**不**收裸 `TCPStore\.cpp`——它在良性告警里也出现，
  而本桶排在桶 12（进程被 kill）**之前**，误命中会把 mixed 的日志错配成 infra；
- **桶 6/7 按错误码分档**（依据 `classification-guide.md` 场景 C）：`507xxx` 是硬件/驱动故障（infra）；`107xxx` 是 CANN runtime 参数非法，不是硬件信号（mixed）。旧版一律归 ACL/mixed，把硬件故障漏成了「待判定」；
- **桶 5 先于桶 6，裸 `ERR99999` 下沉到桶 28**（2026-09-20 实测纠偏）：`ERR99999` 是昇腾对「任意未捕获应用层异常」的**通用兜底包装**，**不是硬件信号**——实测两例（job `106046329358` 模型缓存未命中、job `105440985558` 投机解码断言失败）都是紧跟在真实 Python traceback 之后打印，同行 `Device:-1, RankID:-1` 表示**未绑定 NPU 设备**。旧版把 `ERR99999` 无条件并进硬件桶，导致这两例用户侧问题被判成 infra。改法：① 硬件桶只认 `507xxx`，或 `ERR99999` 且同行 `Device/RankID` 非 `-1`；② 裸 `ERR99999` 下沉到桶 28 标 `unknown`，让真实根因先命中（实测两例分别纠正为桶 5 `code` 与桶 21 `code`）。⚠️ 与「桶 25 早于桶 26」同一原则：**级联症状不能压倒根因**；
- **桶 9 先于桶 10/25**：依赖解析失败会连锁产生大量 `error`/`failed` 噪音，不前置则根因被级联噪音吞掉；
- **桶 10 带负向前瞻**排除 `7739 bytes of body are still expected`——这是网络下载不全，旧版被 `error:.*expected` 误判成编译失败并把 owner 从 mixed 错配成 code；
- **桶 13 带两处负向前瞻**（`RayTaskError(?!\(Assertion)` 和 `ray\.exceptions(?![^\n]{0,60}Assertion)`）——`ray.exceptions.RayTaskError(AssertionError)` 本质是断言失败，应落到桶 21。⚠️ 两处缺一不可：断言写在**括号里**，只挡点号形式会漏网（已实测踩坑）；
- **桶 16 需收紧**：裸 `huggingface_hub` 会命中正常进度行 `Downloading huggingface_hub-1.30.0-py3-none-any.whl`，故必须限定为 `.errors` 或后随 `Error|Timeout|Failed|Connection`；
- **桶 29/30（pytest 判定行）插在桶 28 之后、桶 31/32 之前**（2026-09-28 新增，三处顺序都要对）：
  它们必须晚于硬件/网络/OOM 等真根因桶（否则「OOM 导致用例失败」会被写成业务侧用例失败），
  又必须早于 `exit 255` 与 `failed to run script step` 这两个通用包装桶（否则真判定被外层包装覆盖成
  unknown/infra，即改前的实际行为）。语义与「提前退出」的联动见 §7.4；
- **桶 25 必须早于桶 26/31**：`RuntimeError: engine core died` 是**级联症状**（引擎子进程被更早的错误打死，真因在其上游日志）。若不单列，它会落到桶 31 被标成 owner=infra——等于给一个我们并不掌握的责任方下结论；
- **桶 31 排在真实根因桶之后**：exit 255 是 K8s 强杀，本身不是根因，只有确实无其他信号时才归到这里（它之后只剩桶 32 这个「脚本步骤通用包装」兜底桶）；
- **桶 32 命名已更正**：`failed to run script step` 是 GitHub 对「任意脚本步骤失败」的通用包装，**并非多节点专属**（实测 sglang/triton 的 CPU 门禁 job 也被它命中），旧桶名「多节点编排层包装失败」属误命名；
- **桶 23 是补漏**：mypy 的真实错误形态（实测 job `106079239560`）是
  `pool_scheduler.py:175: error: "KVPoolScheduler" has no attribute "mamba_group_ids"  [attr-defined]`
  + `Found 1 error in 1 file (checked 615 source files)`。旧版没有任何桶匹配它 → 落到桶 32 被标 `unknown`。
  实测 6/40 份样本（15%）因此被误归 unknown。**注意与同一 run 的 `cpu-ut` job 的关系**：同一个属性缺失
  会让 UT 崩成 `AttributeError`（桶 26），也就是**同一根因落进两个桶**——这正是按 `(run, 桶)` 去重之外，
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
| `window_head`/`window_tail` | 下载日志 + 时间窗切片 + 32 桶扫描 | 计入根因；全部未命中 → 也进 `待集群取证` |

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

### 7.5 第二日志证据源：对端节点（`*-ascend-logs` 产物）

**为什么需要它**：`gh api …/actions/jobs/{job_id}/logs` 回的**只有一台机器（node0）**的容器 stdout。
多节点 job（`multi-node (`/`double-node (` 开头）里其余机器的日志**只**存在于 workflow 上传的
`<分支>-<yaml stem>-ascend-logs` 产物里。这不是偶发：多节点 job 跑测试的步骤就叫 `Stream logs`，
node1..nodeN 的输出从来不进 job log。

实测代价（job `109264350421`，run `36518916532`）：node0 是 TCPStore **服务端**，只说得出
「8 个 rank 里 1 个没连上」——
`torch.distributed.DistStoreError: Timed out after 1801 seconds waiting for clients. 7/8 clients joined.`
而「是谁没连上、从哪台机器连不上」只在 node1 的日志里（该文件 839 行，其中
`TCPStore.cpp:138 recvValueWithTimeout failed` 与 `DistNetworkError: Failed to recv, got 0 bytes`
在 job log 里 **grep 一行都没有**）。

**取值链路与实测规则**（实现见 `forensics/peer_logs.py`，8 个真实 job 名 / 14 个真实产物校准）：

| 环节 | 规则 | 实测依据 |
|---|---|---|
| job 名 → yaml stem | 取**最后一个** ` / ` 之后的段去掉 `.yaml`；无 ` / ` 分隔符则判无产物 | GitHub 会把 job 名从**中间**截断成 `…te... / GLM-5.1-W8A8C8-A3_128k_90_50.yaml`，但**尾部完整**。⚠️ 实测 42 个 job 名里 ` / ` 最多出现 **1 次**，取第一个与取最后一个结果相同、**无法区分** —— 「取最后一个」是防御性选择，不是实测定论 |
| stem → 产物名 | `endswith(f"-{stem}-ascend-logs")`，多命中优先 `main-` 前缀 | 同 run 还有 `nightly-a3` 这类无关产物；用「包含 stem」会在 `…-dual-nodes` 与 `…-dual-nodes-x` 上串 |
| 产物内层 | zip → `ascend-logs.tar.gz` → `collected-logs/node{N}/…` | 外层 zip 415B~5.1MB，内层 260B~6.1MB |
| 取哪些文件 | 只取 `collected-logs/node{N}/var/log/*_logs.txt`（容器 stdout，与 job log 同源可比对） | 昇腾设备日志（`root/ascend/log/…`）属另一类证据、行格式不同，不取 |
| 判「空」 | 内层 tar **常规文件数为 0** | 实测 415B 产物内层 260B、**只有目录项**，且目录里照样列着 node0..node3 |
| 节点数 | 只能由 `*_logs.txt` 的出现来数 | 按目录名数会对一个空产物报出「4 个节点」 |
| 时间切分 | 无（产物是整段容器 stdout，没有失败步骤的时间戳对齐依据） | 只能取尾部 `--peer-log-lines` 行（默认 400） |

**「产物存在但为空」是一条独立结论**，既不是「没有产物」，更不是「对端节点无异常」：
实测 run `36518916532` 的 **9 个失败多节点 job 里 8 个产物是空的**——它们的 node0 日志多为
`phase=Pending`（pod 没起来，容器日志根本没产生）。故报告/控制台在**每一档**都必须出话
（产物为空 / 未取得 / 只有 node0 / 有文本），留白会被读成「对端节点无异常」，与事实正相反。

**作用范围是三档短路**（成本控制）：`--no-peer-logs` · 非多节点 job（日志本就完整落在 job log 里，
取产物没有增量）· **日志侧已定性**（`decisive`，结论已由 node0 时间窗给出，产物不改变归因）。

**采用策略：仅兜底，不覆盖。**

| node0（主日志） | 对端节点 | 结果 |
|---|---|---|
| 判出桶 | 判出桶 / 未分类 | 沿用 node0 的桶；对端只作报告里**并列的一条证据**（`sig_source=失败步骤窗口`） |
| 未分类 | 判出桶 | **采用**对端桶，`sig_source=对端节点日志`，报告首行写明「node0 时间窗未能分类，采用对端节点日志判桶」 |
| 未分类 | 未分类 / 产物为空 / 未取得 | 维持未分类，进 `待集群取证` |

不给对端覆盖权的理由：`synthesize()` 的第一条纪律是「三层证据并列，不互相覆盖」。node0 的窗口是
**本 job 失败步骤的时间窗**，对端文本只是粗切的尾部若干行（无时间窗对齐）；拿它改写一个已经定性的
结论，等于用一个时间基准更弱的证据推翻更强的那个。

**对端文本不参与「已定性」判定**（`decisive` 只用主日志那对桶/owner 算）：对端文本若含
`pytest exit code: ret=1` 就会被判成 `DECISIVE_BUCKETS` 里的桶，拿它算 `decisive` 会让第 2 步
按规则跳过集群取证——而真因可能恰恰是集群侧的调度延迟（本案例正是如此）。
`tests/test_peer_logs.py::test_peer_text_cannot_make_case_decisive` 用源码层断言钉住这一点。

对端产物按 **artifact_id** 缓存（默认 `<脚本目录>/.forensics_cache/artifacts/`，已在 `.gitignore`），
「该 run 有哪些产物」在进程内按 `(repo, run_id)` 备忘一次：实测 run `36518916532` 里 14 个产物
对应 20 个 job，不缓存会对同一份产物重复下载。

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
  `"KVPoolScheduler" has no attribute "mamba_group_ids"`（桶 23）与同一 run 的 UT 崩溃
  `AttributeError: 'KVPoolScheduler' object has no attribute ...`（桶 26）是**同一处代码缺陷**，
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
- **时间窗依赖日志时间戳**：窗口切不出来时回退到尾部窗口，此时会退化回旧版的级联噪音问题。可用 `--no-step-window` 显式对照。
  可观测性现状：控制台逐条标了扫描方式（`时间窗`/`尾部窗口`）、汇总行给了 `window_hits/logs_done`，
  `detail[]` 里也有 `windowed` 字段 —— 但**第 5 步渲染的取证报告不写这一项**，
  只看 `forensics_report_*.md` 的读者仍分不出「窗口生效」与「回退」（缺口，未修）；
- **多节点日志缺失（结构性）**：多节点 job 的 job log 只有 node0 一台机器（多节点测试的 GitHub 日志只有 orchestrator 层），
  node1..nodeN 只在 `*-ascend-logs` 产物里（见 §7.5）。第 2 步仍是必须的：产物**可能为空或未上传**
  （实测 run `36518916532` 的 9 个失败多节点 job 里 8 个为空），且对端文本没有时间窗对齐。
  故「对端节点已查过」**不能**读成「对端节点无异常」；
- **cancelled 语义**：cancelled 且从未启动 → 调度/资源问题；否则多为主动取消/上游中断；
- **未分类兜底**：依赖 `(FAILED|Error|error:)` 正则，可能把非根因的普通报错行当证据。这类样本现在也会进 `待集群取证`，不再硬给一个桶；
- **桶体系是经验校准的产物**：32 桶的**顺序**承载了大量踩坑结论（见 §7.2 的校准说明），新增桶时必须回归验证既有样例，不能只测新样例。
- **现象归因这一层的实测准确率与它的天花板**：见 §12.5 —— 这批数字同时说明了「排序坏了」与「桶表本身盖不住一部分真因」两件事，后者不是判决器能修的。

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

## 12. 根因判决 LLM 化 · 第一阶段（离线评测，未接入服务）

### 12.1 决策与边界

**判决这一层换成 LLM，规则层退到「取数与校验」位。** 规则层擅长的部分（`runner_name`
精确身份、liqo placement、集群归属、先例检索）实测可靠，不动。

第一阶段**只做离线评测**，不碰服务：不改 `npu_ci_watch.py`、不改 `deploy/` 里的 unit、
`--llm-judge` 默认关闭。设计目标是一个可复现的数字，而不是先上线再解释效果。

### 12.2 病灶：是排序，不是取数

决定性证据是「规则命中的那一行到底是什么行」：

| 观测 | 数值 |
|---|---|
| 语料最大桶 `依赖/安装(ImportError)` 抽样 40 条，真依赖问题 | **0 条**（40/40 的 ImportError 字串只在 `WARNING` 行） |
| 该桶 66 份缓存日志里 ImportError 命中 345 次，落在 `WARNING` 行 | **343 次** |
| 本设计文档 §7.2 的桶序：`classify_text` 按表序取**首个命中** | 良性 WARNING 因此排在真判据之前 |
| 冻结集 30 例中，规则命中行落进人工标注证据行 | **3/30** |
| 弱决定性行率（命中行是 WARNING/INFO 而置信度非低） | **26.7%** |

即：真判据通常在窗口内（早期那批样本上下过一条观测：错判样本 11/12 的真值行就在被扫描的尾部
窗口里；该批样本的代理真值口径已退役，见 §12.5，故这条只作旁证，不作依据），
只是被「首个命中正则胜出」排到了良性噪声后面。**换判决器换的是排序能力，
不是取数能力** —— 这也是为什么不能为了省 token 而用正则预筛喂给模型：
那等于把「首个命中正则胜出」原封不动搬到模型前面，会把天花板焊死。

### 12.3 判决契约与降级

判决同时产出**闭集类**与**自由文本现象**：

```jsonc
{
  "root_cause": "一句话，含机制",                 // ← 结论就是这一段自由文本
  "owner": "code",                    // infra|code|mixed|unknown
  "confidence": "high",               // high|medium|low
  "verdict_class": "测试用例失败(pytest ret=1)",  // 选填：闭集里贴合才填，不贴合留空串
  "phenomenon": "用例真失败（精度/逻辑）",         // 自由文本，≤20 字名词短语
  "decisive_line": 4,                 // 必须是 evidence_lines 之一
  "evidence_lines": [2, 3, 4, 5],     // ⊆ 窗口行号集合，非空
  "missing_evidence": "缺用例级 traceback…"
}
```

**为什么要两套**：只有自由文本 → 一致率算不出来、无法与基线比；只有闭集 → 把模型锁回桶粒度，
而「桶粒度错了」正是要修的问题。故**指标用闭集类**（可算、可跨臂比），**报告显示自由文本**；
`verdict_class == "其他"` 的样本按现象聚类，就是「该新增哪个桶」的数据驱动候选。

> `verdict_class` 的「必填」已于 prompt v2 改为**选填**，顺序也改为「先写 `root_cause` 与
> `phenomenon`，最后才看 `verdict_class`」。原因与后果见 **§13** —— 强迫模型在定性之前先落一个桶，
> 会把「首个命中正则胜出」原样搬进模型。

**不信任模型自报**：`disagrees_with_rule` 由服务端按 `verdict_class != rule_bucket` 计算；
`evidence_lines ⊆ 窗口行号集合` 是抑制幻觉的核心闸门（模型可以编，编的行号过不了）。

**降级绝不静默**：超时 / 429 / 5xx / 4xx / 网络 / 空 content / 非法 JSON / 缺字段 / 枚举越界 /
引用窗口外行号 / 证据不可用 / 预算耗尽，全部退回规则判决，且 `fallback_reason` 具名。
降级时四条硬约束：① `root_cause`/`owner` **逐字等于**规则判决；② 置信度封顶到「低」并注明退回；
③ `needs_human=True`；④ `basis` 追加一行原因说明。报告摘要给出降级率与原因分布 ——
**降级率本身就是线上健康指标**。

置信度与结论同排（`根因（LLM 判决，置信度 high）`），`low` 时把 `missing_evidence` 渲染成
「补齐什么才能定性」；`needs_human` 做成单向棘轮（LLM 只能说 True，不能清除规则或集群侧已判出的 True）；
`owner_from_cluster=True` 时 LLM 不得覆盖 owner，只加冲突标记并降置信。

### 12.4 Prompt 纪律（成败几乎全在这里）

同一份日志的 A/B 实测：朴素 prompt（「你是根因分析器，输出 JSON」）给出 `owner=infra`、
`confidence=high`、把 WARNING 当根因、`disagrees_with_rule=false` —— **原样重演了正则的锚定偏差**；
带纪律的 prompt 给出 `owner=code`、`decisive_line=4`、`disagrees_with_rule=true`。

system prompt 的六条纪律：
1. **只有终局判定行能定性**，逐个点名形态：pytest 的 `short test summary info` 段与
   `N failed, M warnings in …`、`pytest exit code: ret=N`；benchmark 的 `Performance verification failed`。
2. **终局行出现即责任方在业务侧**，不得再去上游找「更根本」的原因（直接对抗 §12.2 的实测缺陷）。
3. **WARNING/INFO 不是根因**，并**具名**列出本仓陷阱：`No module named 'vllm._deepselect_C'`、
   `Failed to import the … extension`、`ERR99999` 是框架兜底打印。具名比抽象规则有效得多。
4. **强制引用证据行**：`decisive_line` 是其中最能定性的一行；**拿不出决定性行就必须选 `low`**。
5. **禁止外推**：不得补日志里没有的机制，缺什么写进 `missing_evidence`。
6. 输出**只有一个 JSON 对象**，无解释文本。

`eval/canaries/` 把上述 A/B 钉成回归，三条自包含摘录（陷阱 → 判据）：

| 金丝雀 | 陷阱 | 判据 | 真值是否在闭集内 |
|---|---|---|---|
| `import_error_warning` | `Error retrieving safetensors…` / `Failed to import the DeepSelect extension` 两条 WARNING | `not greater than or equal to 0.97 * baseline` | **否** → 纪律臂应留空 |
| `keyerror_missing_config_fields` | cfg 字典 dump（含 `HCCL_BUFFSIZE` / `HCCL_CONNECT_TIMEOUT` 字样） | `KeyError: "Missing required config fields: ['deployment']"` | 是 → 纪律臂必须判出桶 |
| `external_dp_rank_exit` | 打印 External DP 启动命令的 **INFO**（命令行里塞满 `HCCL_*`） | `RuntimeError: External DP rank process exited before ready` | **否** → 纪律臂应留空 |

三条里两条的真值**闭集里没有**，所以「纪律臂必对」这个旧期望本身是错的（旧契约下它等于要求
模型违反自己的输出契约）—— 正确的期望是「留空 + 把该取什么证据写进 `missing_evidence`」，
详见 §13。`PROMPT_VERSION` 进缓存键：改 prompt 必须改它，否则会静默复用旧口径的判决。

### 12.5 评测口径（本阶段的核心交付）

**三层避免自证**：
- 终局行抽取器**只用于分层抽样与选金丝雀，绝不作评分真值** —— 拿它当真值，测出来的只是
  「LLM 跟我的启发式像不像」；
- 评分真值是**人工裁定表**，且顺序是硬要求：**先冻结真值、再跑 LLM**；裁定者**看不到 LLM 输出**；
- 抽 20 例双裁并报 **Cohen's κ**：κ 是任何准确率的天花板，不报 κ 的准确率在评审里站不住。

**冻结集**（`eval/`，30 例，确定性配额，名单可复跑）：defect 21（benchmark 12 / pytest 8 / 其他 1）、
normal 5、undetermined 4。单按分层会让 42 例 benchmark 压满缺陷层，于是「缺陷层」实际只测一个家族，
故按**失败家族**再分。日志窗口只在本机与端点之间流动，**不入库**（本仓 PUBLIC）；
入库的 `cases.jsonl` 用 sha256 钉住窗口文本，可按 job_id 重取并逐字节校验。

**规则基线：48% 已退役，为什么** —— 原基线是用「终局行代理真值」在另一批样本上量出来的 48%。
本次按计划要求复现时只得到 8.2%（5/61），且加不加步骤时间窗**完全一样**（同为 8.2%），
证明窗口口径不是原因。机制是两条：① 代理真值是用**同一张 32 桶表**在少数行上生成的，
在该池里退化成 3 个粗粒度值（36× `测试用例失败(pytest ret=1)`、22× `未分类`、3× `测试未执行`），
细粒度规则桶在结构上不可能匹配，8.2% 是伪影；② `terminal_bucket` 与 `rule_bucket` 是在**同一份**
`scan_text` 上算的，该指标实际比的是「规则 vs 规则自己的一个子集」，对窗口不敏感。
**故改用人工真值、同一 N、同一函数（`eval_metrics.arm_metrics`）三臂同口径对比。**

**现行基线**（`python3 eval/run_eval.py --arms rule`，30 例人工真值、状态 `proposed`）：

| 指标 | 值 |
|---|---|
| 现象归因一致率 | **6/30 = 20.0%**（95% CI 6.7%~36.7%） |
| ├ 闭集内可表达（n=12） | 6/12 = **50.0%** |
| └ 闭集内不可表达（n=18） | 0/18 = **0.0%** |
| owner 一致率 | 23/30 = 76.7%（CI 60%~90%） |
| 分层 | defect 3/21 = 14.3% / normal 3/5 = 60.0% / undetermined 0/4 = 0.0% |
| 弱决定性行率 | 26.7% |

**必须一起读的两个天花板**：
- **闭集一致率的上限是 40%**（12/30）—— 18 条真值在现有 32 桶里**没有正确的桶**，
  任何判决器（含 LLM）都表达不出来。这 18 条里 10 条是 benchmark 性能/精度未达标，
  而 `npu_ci_failure_analysis.py` 的 32 个桶里**根本没有性能桶**（grep「性能」零命中）。
  这是**覆盖缺陷**，换个判决器修不了，得先加桶；
- 因此指标必须**分半算**（`eval_metrics.coverage_split`）：可表达子集量的是判决器好坏
  （规则已 50%），不可表达子集量的是桶表覆盖度。不分半，就会拿覆盖缺口给判决器记功。
- **决策门需重定**：原定「缺陷层准确率的 CI 下界 > 规则基线 + 5pp」在本冻结集上几乎无区分力
  （defect 层规则 14.3%、n=21、CI 极宽）。建议改成「**可表达子集上，LLM 的 CI 下界 > 50%**」，
  覆盖度单列一项跟进。

**由真值集直接暴露的误配**（规则桶 → 真值，均可在 `eval/runs/*/metrics.json` 的混淆矩阵复核）：
`性能未达标(benchmark)` → `断言失败(代码或精度)` ×5 / `依赖/安装(ImportError)` ×3；
`测试参数缺失(config未传入)` → `依赖/安装(ImportError)` ×2；`内网镜像拉取失败(SWR)`
→ `多节点pod调度/就绪失败(k8s侧)` ×2（pod 调度正常，卡在拉镜像）；
`KV传输后端(Mooncake)初始化失败(EL0004)` → `OOM/显存不足` ×2（owner 会因此错派给业务侧）。

### 12.6 复现步骤

```bash
# 1) 重取日志窗口并校验 sha（窗口文本不入库）
python3 eval/build_fixtures.py            # scan_sha256 必须与 cases.jsonl 逐字节相同
# 2) 复跑抽样名单（确定性；变了说明配额或池子变了）
python3 eval/select_sample.py --json
# 3) 规则基线（同 N、同函数；LLM 臂走同一个 arm_metrics）
python3 eval/run_eval.py --arms rule --job-id <id> …      # 或 --dry-run 验 harness，$0
# 4) 三臂跑分（先冻结并裁定真值，再跑 LLM；真值未裁定时所有数字不成立）
python3 eval/run_eval.py --arms rule,deepseek-flash,deepseek-v4-pro
```

### 12.7 第二阶段（本次未做）与阶段边界

第二阶段才动服务：`forensics/report.py` 新增 `apply_llm_verdict()`（**`synthesize()` 一个字不改**，
`--llm-judge` 关闭时输出与今天逐字节相同）；扫描窗写 sidecar + handoff 加 `scan_ref`（sha 钉住）；
第 5 步按 `job_id` 缓存判决（缓存键**必须含 `prompt_version`**）；`npu_ci_watch.py` 读
`LLM_ENABLED` 决定是否给阶段 B 子进程加 `--llm-judge`；unit 加
`EnvironmentFile=-%h/.config/npu-ci-watch/deepseek.env`（`chmod 600`）—— **改 unit 属需用户确认的操作**。

**阶段边界**：阶段 A（`npu_ci_watch.py` 里抢集群快照的进程内直调）**绝不引入 LLM** ——
它跑在「失败步骤结束 → job 结束」实测固定 55s 的窗口里，一次 40K token 推理会直接吃掉抢快照的时间窗；
且它是进程内直调，任何改动都要重启常驻服务，与「先离线评测再上线」冲突。

**成本量级**：单例约 47K 输入 token（1200 行窗口实测 36K~64K，均值 46.6K，chars/token ≈ 2.6）；
30 例 × 2 模型臂 ≈ 2.8M 输入 token，一次评测在几毛钱量级。两个省钱的直觉**都不成立**：
折叠重复行只省 1%；去掉噪声行窗口反而**变大 11%**（行数上限往回吃更多内容）。
**上限才是约束，噪声不是** —— 要省就压输出或压样本量。

---

## 13. 判决改口径：结论走自由文本，桶降为投影

§12 的设计把**桶当成了判决的入口**（`verdict_class` 必填、且是报告里给人看的结论本身）。
第一批 7 个 job 的人工复核证明这个入口是错的：**桶应该是判决的产物、而且只是投影**。

### 13.1 病灶：入口错了，不是判得不准

| job | 真因 | 窗口里的东西 | 规则/旧契约判成 |
|---|---|---|---|
| A1 | `Performance verification failed`（业务侧，性能未达标） | 一行 `[INFO] … HCCL_CONNECT_TIMEOUT=400 …` 的环境变量转储 | `HCCL 集合通信失败` / infra |
| A2 | `KeyError: Missing required config fields: ['deployment']`（业务侧） | 同一份 cfg 字典 dump | `HCCL 集合通信失败` / infra |
| B2 | `RuntimeError: External DP rank process exited before ready` | `indexer_topk.py:29` 的 **WARNING**：`No module named 'vllm._deepselect_C'`（prompt 纪律里点名的良性兜底打印） | `依赖/安装(ImportError)` / code |
| B3 | **与 A1 同一种真因** | 噪声分布不同 | `断言失败(代码或精度)` ← 与 A1 拿到**两个不同的桶** |

最后一行是决定性的：**同一种真因，噪声换一换就落到不同的桶**。这不是「判得不准」，
是「桶本身就不是一个稳定的判据」。

`HCCL_CONNECT_TIMEOUT=400` 那一条尤其说明问题：`HCCL\w*(?:timeout)` 在 `re.I` 下命中的是
**变量名**，与「集合通信失败」毫无关系，但它足以把 owner 从 code 派到 infra。
**任何原因的失败，只要夹杂 hccl 关键词就会被误判** —— 关键词匹配作为判决入口，天花板就在这里。

### 13.2 实测天花板：60% 的真值不在闭集里

`eval/truths.jsonl` 30 条人工真值中 **18 条 `closed_set_expressible: false`**。
即：无论换什么判决器，**闭集一致率的上限是 40%（12/30）**。
故「把桶判得更准」这条路有 60% 的硬天花板；必须让**结论走自由文本**，桶只做统计投影。
（这条与 §12.5 的「必须分半算」是同一件事，§13 把它推到了契约层。）

### 13.3 改了什么

**一、LLM 判决层（prompt v2）**
- `verdict_class` 由**必填降为选填**：闭集里恰好贴合才填，**不贴合就留空**，不挑「最接近的」；
- 输出契约里的顺序改为 **`root_cause` → `phenomenon` → 最后才 `verdict_class`**。
  顺序很重要：先落桶，模型就会围绕那个桶去组织结论（实测 B3 与 A1 分叉的机制）；
- 取值越界**不再作废整条判决**，只记 `verdict_class_in_closed_set: false`。
  把「模型挑了个闭集外的名字」升级成「整条判决不可用」，代价与收益完全不成比例；
- `projected_class(parsed)`：非空且闭集内才算数，否则一律投影成 `其他`；
- `apply_llm_verdict` 的 `basis` 那行去掉桶名（改为 `LLM 判决：{root_cause}`），
  桶只留机器可读字段；冲突行**只在模型真给了闭集内的桶时**产生 ——
  把每条 `其他` 都报成证据冲突，冲突段就被噪声淹没，而它的价值就是「出现即要人看」。

**二、产线报告渲染**
- `synthesize` 的根因块改为**三档降级，且不再以桶名打头**：

  | 条件 | `root_cause` |
  |---|---|
  | 有集群侧实证 | `集群侧实证：<interpret_pod_evidence 的结论句>` |
  | 有先例（强/弱） | `与历史先例 #N 高度吻合` / `…主题相近（可参考，但机制未必相同）` |
  | 都没有 | `未能定性（仅有日志侧归类，需人工介入）` |

  理由：冻结集上规则层**自报结论时 80% 是错的**。没有硬证据时**不说结论**，比说一个 80% 错的结论更负责；
  信息并没有丢 —— 归类行与依据行都还在，回显的命中行紧跟在结论下面；
- 新增一行 `- **归类**：{bucket}（{source}，仅供统计与派活，非结论）`，
  `source ∈ {rule_regex, peer_regex, none}` 由 `sig_source` 派生；
- `basis` 追加 `日志侧正则命中行：{sig}`（`sig` 非空才加）。这一行是给人**当场核对**用的：
  看到「未能定性」时，下面紧接着就是那条被判为命中、但不足以定性的行。

**三、评测层**
- `declined_rate()`：LLM 真判了、但**拒绝归类**（留空 → 投影成 `其他`）的比例。
  与降级率**分列**：降级是「LLM 整条不可用」，留空是「闭集里没有贴合的那一格」——
  合成一列会把「桶表不够用」读成「模型不好用」，正好读反；
- `phenomenon_clusters()`：把归类为 `其他` 的按 `phenomenon` **归一化后**（去空白/标点/大小写）
  聚类，`render_cluster_candidates()` 渲染成「该新增哪个桶」的候选表，进评测报告新一节。
  不归一化，同一现象会碎成一簇一个 case，这份聚类就不可复核、也不能跨运行比较；
- `eval/run_eval.py` 用 `projected_class` 把空串与越界值投影成 `其他` 并记 `verdict_class_source`；
  **降级路径（`used=False` → 规则桶）的计分口径一字不改** —— 它等于「LLM 挂了线上会怎样」。

### 13.4 明确不做

- **不给 `npu_ci_watch.py` / `deploy/npu-ci-watch.service` / `--llm-judge` 接线**，阶段边界不变（§12.7）；
- **不动 `is_decisive` / `DECISIVE_BUCKETS`**：B2 走的是 `decisive=True` 路径，
  owner 因此跳过集群取证、永远拿不到反驳证据（`is_decisive → code → 跳过集群取证 → 无反驳`）——
  这是同一条级联，但它是规则层核心，且改动会改变集群查询量，**单独评估**；
- **不动 `classify_text` 的 32 桶正则表**：桶表是投影的闭集来源，改它会让冻结集真值失配；
- **不做「按引用行跑 `classify_text`」的兜底投影**：实测反例 —— 最大的真值族
  `性能未达标(benchmark)` 里模型会引用 `E AssertionError: some aisbench cases failed`
  （那是 benchmark harness 自己抛的），兜底投影照样投出 `断言失败(代码或精度)`，
  **原样复现规则层的错**。「不硬塞最近桶」包括不拿正则去硬塞。

### 13.5 已知遗留（本阶段未修，需单独决策）

**归档 7 个 job 的重跑结果符合设计**：7/7 的根因行都读作「未能定性（仅有日志侧归类，需人工介入）」，
紧随其后是「归类」行与「日志侧正则命中行」的回显 —— 不再有把关键词当机制的那一行。

**但有一条口径打架的路径**（构造用例复现，非归档里观测到的）：当 `cluster.skipped=True` 时
（即日志侧已判为决定性判据、按规则跳过集群取证，`report.py:312`），置信度仍读
**「中高（日志侧决定性判据：测试框架自身的判定行）」**，而根因行已改读「未能定性」。
读者会看到「结论说定不了性 / 置信度说中高」并列在同一段里。
根因是 `is_decisive` 的结论没跟着根因块一起降级 —— 这正是 §13.4 第二条要单独评估的那条级联
（`is_decisive → code → 跳过集群取证 → 永远拿不到反驳证据`）。
归档里 7 个 job 都走的 `cluster.skipped=False` 分支，**这条路径本阶段未经真实样本验证**，
评审时需一并说明。

### 13.6 验证

- 18 个套件全绿（`test_pytest_verdict` 15→21、`test_llm_verdict_parse` 17→20、
  `test_llm_prompt` 19→21、`test_llm_fallback` 14→20、`test_freeze_set` 13→16、
  `test_eval_metrics` 31→40）；新增 `tests/test_eval_wiring.py`（7 例）——
  投影自己是对的由 `test_llm_fallback` 守着，但**投影有没有真的接到 record 上**此前零覆盖；
- 每条新断言逐个注入反向改动证伪（清 `__pycache__` + `python3 -B`，防止同秒复用旧字节码），
  确认恰好对应那条红，复原后复绿；
- `eval/run_eval.py --dry-run`（$0）：规则臂一致率仍为 **0.2**，冻结集口径未漂移；
- 日志窗口与 fixture **不入库**（本仓 PUBLIC），金丝雀摘录里的内网 IP 一律替换为占位符。
