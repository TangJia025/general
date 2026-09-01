# NPU CI 失败分析报告（2026-08-25 ~ 09-01）

> 方法：静态筛出跑在 NPU 上的 CI workflow → 近一周执行记录 → 抽样失败 run 定位 NPU job → 日志尾部窗口根因分类 → top3。
> 脚本：`npu_ci_failure_analysis.py`（同目录，三仓通用，支持 `--repo`/`--since`/`--samples`）。
> 样本：每仓抽样 25~30 份失败日志，百分比为样本内占比。日志只扫尾部 1200 行（避开安装/构建噪音）。

---

## 1. vllm-project/vllm-ascend

**NPU CI workflows**：`schedule_nightly_test_{a2,a3,a3_560t,a5,310p}.yaml`、`schedule_weekly_test_*`、`schedule_e2e_upstream_test.yaml`、`schedule_main2main.yaml`、`labeled_doctest.yaml`、`pr_test.yaml`（E2E 编排）。NPU runner：`linux-aarch64-{310p,a2,a3,a5}-N`（`linux-amd64-cpu-8-hk` 是纯 CPU）。

**近一周**：40 份日志抽样，共 149 次失败。

| 排名 | 原因 | 次数 | 占比 | 证据 |
|---|---|---|---|---|
| #1 | 断言失败(代码或精度) | 9 | 22% | `AssertionError: some aisbench cases failed` |
| #2 | 模型/包下载失败(网络) | 9 | 22% | yum `Failed to download metadata for repo 'update'` + huggingface whl |
| #3 | 自定义算子so缺失(csrc构建) | 8 | 20% | `ImportError: npu_mega_moe.so cannot open shared object file`（a2 单节点，csrc 缓存缺口） |

其他：未分类 5（多节点 orchestrator 层）、Python运行时错误 3、测试参数缺失 3、分布式/HCCL 1、昇腾算子执行错误(ACL) 1、超时 1。

---

## 2. triton-lang/triton-ascend

**NPU CI workflows**：`ci.yml`（Integration Tests；内部经 `runner-preparation.yml` 动态矩阵 → `integration-tests-ascend.yml`）。NPU runner：`linux-aarch64-a3-4` 与 **`linux-amd64-a5-4`（a5=昇腾950，跑在 amd64 上）**。

**近一周**：100 runs，成功 45 / 失败 30 / cancelled 25，**成功率 60%**。抽样 8 个失败 run → 22 个失败 job **全部在 NPU runner** 上。

| 排名 | 原因 | 次数 | 占比 | 证据 |
|---|---|---|---|---|
| #1 | 编译失败(C++/MLIR) | 18 | 82% | DynamicCVPipeline 库 `SplitMatmulPattern.cpp:740`：`no viable conversion from 'llvm::APFloat' to 'mlir::FloatType'`（MLIR API 版本不兼容，配 `-Werror`） |
| #2 | Python运行时错误 | 3 | 14% | `AttributeError: module 'triton.language...` |
| #3 | 断言失败 | 1 | 5% | `AssertionError: Tensor-likes are...` |

> 结论：近周 NPU CI 失败几乎全被同一个编译错误卡住——自定义 DynamicCVPipeline 库与当前 LLVM/MLIR 版本不兼容，属代码侧 bug 而非测试失败。

---

## 3. verl-project/verl

**NPU CI workflows**（14 个，全 `aarch64` runner，如 `linux-aarch64-a2b3-8`/`a3-8`）：`e2e_ascend.yml`、`npu_unit_tests.yml`、`vllm_ascend.yml`、`model_ascend.yml`、`nightly_ascend*.yml`、各 `*_ascend.yml`。

**近一周**：e2e 系列成功率 43%~67%；`vllm_ascend` 40%（27 失败）；`nightly_ascend_multinode` **0%**（5 战 5 败）。抽样 25 个失败 job 全在 NPU。

| 排名 | 原因 | 次数 | 占比 | 证据 |
|---|---|---|---|---|
| #1 | 进程被kill(OOM/超内存) | 6 | 24% | 训练容器被 SIGKILL：`exit code 137` / `A worker died or was killed` |
| #2 | 分布式通信/HCCL/Ray | 5 | 20% | `ray.exceptions.RayTaskError(RuntimeError...`、`ActorDiedError: The actor...` |
| #3 | 超时 | 4 | 16% | multinode RayJob `timed out waiting for RayJob submitter` |

其他：依赖/安装(ImportError) 3、断言失败 2、模型下载 2、未分类 2、OOM 1。

> 结论：e2e 训练场景，失败集中在训练中进程被 OOM kill、Ray actor 死掉、多节点 RayJob 超时。多节点真实错误在 k8s pod 日志里，GitHub 只留 orchestrator 层。

---

## 4. sgl-project/sglang

**NPU CI workflows**：`pr-test-npu.yml`、`nightly-test-npu.yml`（`full-test-npu`/`diffusion-ci-gt-gen-npu` 近周无 run）。

**近一周**：`pr-test-npu` 100 runs 成功率**仅 18%**（47 失败）；`nightly-test-npu` 成功率 **6%**（31 失败）。

⚠️ **架构特殊**：失败常发生在 CPU 门禁 `pr-gate`（ubuntu-latest），NPU job 全被 skip。抽样 8 个 pr-gate 失败，根因是 **`PR is draft. Blocking CI.`（draft PR 主动阻断，非真实错误）**——解释了 pr-test-npu 的高失败数。

| 排名 | 原因 | 次数 | 占比 | 证据 |
|---|---|---|---|---|
| #1 | 测试框架脚本 bug | 13 | 43% | `TypeError: run_unittest_files() got an unexpected keyword argument 'fork_worker_batch_size'`（`test/run_suite.py:391`） |
| #2 | 昇腾算子执行错误(ACL) | 3 | 10% | `NPU function error: call aclnnFusedInferenceScore...`、`error code is 507899` |
| #3 | 依赖/安装 + 模型下载 | 各 1 | — | `No module named 'vllm'` / huggingface_hub 503 |

其他：未分类 12（含 8 个 draft 阻断的 pr-gate + 4 个 nightly 真实失败）、Python运行时错误 13（即 #1 同批次）。

> 结论：#1 的 13 次失败**全部集中在 08-29 一天**——sglang 改测试框架（`run_suite.py`）时调用签名不一致，当晚所有 nightly NPU job 批量挂掉，属单次坏 commit 引入的系统性失败，不是芯片问题。真实 NPU 硬件失败是 ACL 算子错误（aclnnFusedInferenceScore）与 server 崩溃。

---

## 5. 方法学要点 / 三仓踩出的架构坑

1. **transitive `uses:` 检测**：triton `ci.yml` 不含任何直接 NPU 特征，靠 `uses: integration-tests-ascend.yml` 传递链判定。
2. **`cann_image` 单独不能判强**：CPU runner 也能用 CANN 容器（triton `DynamicCVPipeline-ci` 曾误判，已在 CPU-16 上跑）；`dynamic_runner`+`cann_image` 组合才是 NPU 执行模板（如 vllm `_selected_tests.yaml`）。
3. **NPU 标签正则**：`linux-(?:aarch64|amd64)-(?:a\d[\w-]*|310p)-\d`，覆盖 a5/amd64 形态；aarch64 子串匹配会漏掉 triton 的 a5。
4. **门禁 fallback**：无 NPU 失败 job（NPU job 被 skip）时，降级分析该 run 的失败 job——sglang pr-gate 场景必需。
5. **桶校准**：新增「编译失败(C++/MLIR)」「进程被kill(137)」；分布式桶收紧为 HCCL/Connection reset/RayTaskError 专属——verl e2e 日志满屏 `[Rank N]` 前缀，裸 `rank.*(fail|error)` 会误匹配。
