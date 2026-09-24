# NPU CI 失败分析报告

> 方法：静态筛出跑在 NPU 上的 CI workflow → 近一周执行记录 → 抽样失败 run 定位 NPU job → 日志尾部窗口根因分类 → top3。
> 脚本：`npu_ci_failure_analysis.py`（四仓通用，支持 `--repo`/`--since`/`--samples`/`--report-dir`/`--summary-file`/`--infra-store`）。
> 本文件为自动精简版：**每个仓库的章节由脚本每次运行自动更新**（`<!-- @section:... -->` 标记内内容会被替换），
> 标记外内容（本头部、方法学）为手工保留；跨仓基础设施信号由脚本从 `--infra-store` 自动聚合。完整原始输出在 `npu_ci_reports/npu_ci_failure_report_<repo>_<ts>.md`（不入库）。

---

<!-- @section:infra-snapshot -->

## ⚠️ 跨仓基础设施信号（自动聚合）

| 仓库 | 排队样本 | 中位 | 最长 | >30min | cancelled 未启动 |
|---|---|---|---|---|---|
| vllm-ascend      |  1469 | 6min   | 588min     | **273 个** | 0   |
| sglang           |    59 | 4min   | 711min     | **26 个** | 0   |
| triton-ascend    |    21 | 1min   | 613min     | **5 个** | 0   |
| verl             |    82 | 0min   | 188min     | **16 个** | 0   |

> 数据来源：`npu_ci_reports/infra_snapshot.json`（各仓最近一次运行写入，快照 2026-09-02T11:20）。>30min 提示 runner 池不足（infra 侧）。

<!-- @/section:infra-snapshot -->

<!-- @section:infra-failures -->

## 🔧 跨仓基础设施失败原因汇总（infra/mixed）

| 原因 | owner | 仓库 | 次数 | 失败 run/job 链接 |
|---|---|---|---|---|
| 超时 | mixed | vllm-ascend | 6 | https://github.com/vllm-project/vllm-ascend/actions/runs/33527643422/job/100013866469 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/33484583910/job/99783222651 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/33472492154/job/99745743330 [NPU] |
| 超时 | mixed | verl | 6 | https://github.com/verl-project/verl/actions/runs/33535791096/job/99949650735 [NPU]、https://github.com/verl-project/verl/actions/runs/33417769337/job/99572442005 [NPU]、https://github.com/verl-project/verl/actions/runs/33324266266/job/99291522548 [NPU] |
| 进程被kill(OOM/超内存) | mixed | verl | 5 | https://github.com/verl-project/verl/actions/runs/33512153091/job/99871047050 [NPU]、https://github.com/verl-project/verl/actions/runs/33510593595/job/99865024285 [NPU]、https://github.com/verl-project/verl/actions/runs/33582765081/job/100100375458 [NPU] |
| GitHub API 调用失败 | infra | vllm-ascend | 3 | https://github.com/vllm-project/vllm-ascend/actions/runs/33585105239/job/100107518913 [gate]、https://github.com/vllm-project/vllm-ascend/actions/runs/33584921423/job/100106973404 [gate]、https://github.com/vllm-project/vllm-ascend/actions/runs/33584889223/job/100106879316 [gate] |
| 模型/包下载失败(外网) | mixed | vllm-ascend | 1 | https://github.com/vllm-project/vllm-ascend/actions/runs/33410221539/job/99610527631 [NPU] |
| 进程被kill(OOM/超内存) | mixed | vllm-ascend | 1 | https://github.com/vllm-project/vllm-ascend/actions/runs/33573446877/job/100073973162 [NPU] |
| 昇腾算子执行错误(ACL) | mixed | verl | 1 | https://github.com/verl-project/verl/actions/runs/33505772150/job/99849329027 [NPU] |

> 数据来源：各仓最近一次运行写入 `--infra-store`；mixed 桶需结合 runner 配置/节点网络二次确认。

<!-- @/section:infra-failures -->

---

<!-- @section:vllm-project/vllm-ascend -->

## vllm-project/vllm-ascend（2026-08-26 ~ 2026-09-02）

- 分析时间: 2026-09-02 11:20:32 → 11:25:26（295s）
- 完整原始输出: `npu_ci_reports/npu_ci_failure_report_vllm-ascend_20260902_112032.md`
- 抽样 64 失败 run → 114 失败 job（NPU 85 / 门禁 fallback 29）
- cancelled 采样 52 job，未启动/未分配 runner 0 个
- NPU runner 排队: 中位 7min，最长 588min，>30min 有 273 个（>30min 提示 runner 池不足，infra 侧）

**NPU CI workflows**：`_e2e_nightly_multi_node.yaml`、`_e2e_nightly_single_node.yaml`、`_e2e_nightly_single_node_560t.yaml`、`_e2e_nightly_single_node_models.yaml`、`_nightly_image_build.yaml`、`_selected_tests.yaml`、`_selected_tests_upstream.yaml`、`labeled_doctest.yaml`、`labeled_download_model_dataset.yaml`、`nightly_image_build.yaml`、`pr_test.yaml`、`schedule_e2e_test.yaml`、`schedule_e2e_upstream_test.yaml`、`schedule_main2main.yaml`、`schedule_nightly_test_a2.yaml`、`schedule_nightly_test_a3.yaml`、`schedule_nightly_test_a3_560t.yaml`、`schedule_nightly_test_a5.yaml`、`schedule_test_coverage.yaml`、`schedule_weekly_test_a2.yaml`、`schedule_weekly_test_a3.yaml`、`schedule_weekly_test_a3_560t.yaml`

**近一周成功率**：`schedule_nightly_test_a3.yaml` 24%、`schedule_e2e_test.yaml` 4%、`pr_test.yaml` 28%、`schedule_nightly_test_a2.yaml` 30%、`labeled_doctest.yaml` 60%、`schedule_weekly_test_a3.yaml` 10%、`schedule_nightly_test_a3_560t.yaml` 25%、`schedule_test_coverage.yaml` 20%、`schedule_nightly_test_a5.yaml` 0%、`schedule_e2e_upstream_test.yaml` 0%、`schedule_main2main.yaml` 89%、`schedule_weekly_test_a2.yaml` 0%、`labeled_download_model_dataset.yaml` 100%、`nightly_image_build.yaml` --、`schedule_weekly_test_a3_560t.yaml` --

### 全部失败原因分析

| 排名 | 原因 | 次数 | 占比 | owner | 样例 job 链接 |
|---|---|---|---|---|---|
| #1 | 断言失败(代码或精度) | 11 | 28% | code | https://github.com/vllm-project/vllm-ascend/actions/runs/33527643422/job/99924589834 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/33527643422/job/99934998714 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/33527643422/job/99939690124 [NPU] |
| #2 | 测试参数缺失(config未传入) | 8 | 20% | code | https://github.com/vllm-project/vllm-ascend/actions/runs/33583527260/job/100104105462 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/33583527260/job/100104105503 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/33581482929/job/100098696576 [NPU] |
| #3 | 超时 | 6 | 15% | mixed | https://github.com/vllm-project/vllm-ascend/actions/runs/33527643422/job/100013866469 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/33484583910/job/99783222651 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/33472492154/job/99745743330 [NPU] |
| #4 | 静态检查(pre-commit/ShellCheck) | 5 | 12% | code | https://github.com/vllm-project/vllm-ascend/actions/runs/33585366505/job/100110737924 [gate]、https://github.com/vllm-project/vllm-ascend/actions/runs/33585105239/job/100108139161 [gate]、https://github.com/vllm-project/vllm-ascend/actions/runs/33584921423/job/100107622108 [gate] |
| #5 | 多节点编排层包装失败(pod内真实错误) | 3 | 8% | unknown | https://github.com/vllm-project/vllm-ascend/actions/runs/33509908459/job/99864281694 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/33478289030/job/99763045110 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/33472492154/job/99745740829 [NPU] |
| #6 | GitHub API 调用失败 | 3 | 8% | infra | https://github.com/vllm-project/vllm-ascend/actions/runs/33585105239/job/100107518913 [gate]、https://github.com/vllm-project/vllm-ascend/actions/runs/33584921423/job/100106973404 [gate]、https://github.com/vllm-project/vllm-ascend/actions/runs/33584889223/job/100106879316 [gate] |
| #7 | 依赖/安装(ImportError) | 1 | 2% | code | https://github.com/vllm-project/vllm-ascend/actions/runs/33507175750/job/99855707455 [NPU] |
| #8 | 模型/包下载失败(外网) | 1 | 2% | mixed | https://github.com/vllm-project/vllm-ascend/actions/runs/33410221539/job/99610527631 [NPU] |
| #9 | 进程被kill(OOM/超内存) | 1 | 2% | mixed | https://github.com/vllm-project/vllm-ascend/actions/runs/33573446877/job/100073973162 [NPU] |
| #10 | CI 策略检查(CSRC 变更) | 1 | 2% | code | https://github.com/vllm-project/vllm-ascend/actions/runs/33584889223/job/100107561331 [gate] |

**owner 汇总**：infra 3，code 26，mixed 8，unknown 3


### 基础设施相关失败 Top3

1. **超时**（6 次，owner=mixed）：https://github.com/vllm-project/vllm-ascend/actions/runs/33527643422/job/100013866469 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/33484583910/job/99783222651 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/33472492154/job/99745743330 [NPU]
2. **GitHub API 调用失败**（3 次，owner=infra）：https://github.com/vllm-project/vllm-ascend/actions/runs/33585105239/job/100107518913 [gate]、https://github.com/vllm-project/vllm-ascend/actions/runs/33584921423/job/100106973404 [gate]、https://github.com/vllm-project/vllm-ascend/actions/runs/33584889223/job/100106879316 [gate]
3. **模型/包下载失败(外网)**（1 次，owner=mixed）：https://github.com/vllm-project/vllm-ascend/actions/runs/33410221539/job/99610527631 [NPU]
   其余：进程被kill(OOM/超内存)(1次)

> 说明：mixed 桶需结合 runner 配置/节点网络二次确认；调度/排队信号见上方 meta 行。

<!-- @/section:vllm-project/vllm-ascend -->

---

<!-- @section:sgl-project/sglang -->

## sgl-project/sglang（2026-08-26 ~ 2026-09-02）

- 分析时间: 2026-09-02 11:20:33 → 11:22:37（124s）
- 完整原始输出: `npu_ci_reports/npu_ci_failure_report_sglang_20260902_112033.md`
- 抽样 16 失败 run → 37 失败 job（NPU 25 / 门禁 fallback 12）
- cancelled 采样 92 job，未启动/未分配 runner 0 个
- NPU runner 排队: 中位 4min，最长 711min，>30min 有 26 个（>30min 提示 runner 池不足，infra 侧）

**NPU CI workflows**：`_npu-pr-test-stage.yml`、`_npu-single-node-test-stage.yml`、`bot-bump-sglang-version.yml`、`diffusion-ci-gt-gen-npu.yml`、`full-test-npu.yml`、`nightly-test-npu-e2e-multi-node.yml`、`nightly-test-npu.yml`、`pr-test-npu.yml`

**近一周成功率**：`pr-test-npu.yml` 32%、`nightly-test-npu.yml` 7%、`bot-bump-sglang-version.yml` --、`diffusion-ci-gt-gen-npu.yml` --、`full-test-npu.yml` --

### 全部失败原因分析

| 排名 | 原因 | 次数 | 占比 | owner | 样例 job 链接 |
|---|---|---|---|---|---|
| #1 | 未分类 | 8 | 100% | unknown | https://github.com/sgl-project/sglang/actions/runs/33586694353/job/100112266874 [gate]、https://github.com/sgl-project/sglang/actions/runs/33586657725/job/100112161481 [gate]、https://github.com/sgl-project/sglang/actions/runs/33586377073/job/100111322015 [gate] |

**owner 汇总**：unknown 8


### 基础设施相关失败 Top3

无（本次样本内无基础设施相关失败，均为业务方代码/测试问题）。

<!-- @/section:sgl-project/sglang -->

---

<!-- @section:triton-lang/triton-ascend -->

## triton-lang/triton-ascend（2026-08-26 ~ 2026-09-02）

- 分析时间: 2026-09-02 11:20:32 → 11:21:26（54s）
- 完整原始输出: `npu_ci_reports/npu_ci_failure_report_triton-ascend_20260902_112032.md`
- 抽样 8 失败 run → 18 失败 job（NPU 18 / 门禁 fallback 0）
- cancelled 采样 11 job，未启动/未分配 runner 0 个
- NPU runner 排队: 中位 1min，最长 613min，>30min 有 5 个（>30min 提示 runner 池不足，infra 侧）

**NPU CI workflows**：`ci.yml`、`integration-tests-ascend.yml`

**近一周成功率**：`ci.yml` 74%

### 全部失败原因分析

| 排名 | 原因 | 次数 | 占比 | owner | 样例 job 链接 |
|---|---|---|---|---|---|
| #1 | 断言失败(代码或精度) | 5 | 62% | code | https://github.com/triton-lang/triton-ascend/actions/runs/33581185674/job/100095656852 [NPU]、https://github.com/triton-lang/triton-ascend/actions/runs/33581185674/job/100095656854 [NPU]、https://github.com/triton-lang/triton-ascend/actions/runs/33581185674/job/100095656920 [NPU] |
| #2 | 编译失败(C++/MLIR) | 2 | 25% | code | https://github.com/triton-lang/triton-ascend/actions/runs/33583245239/job/100101863737 [NPU]、https://github.com/triton-lang/triton-ascend/actions/runs/33583245239/job/100101863789 [NPU] |
| #3 | Python运行时错误 | 1 | 12% | code | https://github.com/triton-lang/triton-ascend/actions/runs/33578908660/job/100088934439 [NPU] |

**owner 汇总**：code 8


### 基础设施相关失败 Top3

无（本次样本内无基础设施相关失败，均为业务方代码/测试问题）。

<!-- @/section:triton-lang/triton-ascend -->

---

<!-- @section:verl-project/verl -->

## verl-project/verl（2026-08-26 ~ 2026-09-02）

- 分析时间: 2026-09-02 11:20:33 → 11:25:06（273s）
- 完整原始输出: `npu_ci_reports/npu_ci_failure_report_verl_20260902_112033.md`
- 抽样 98 失败 run → 31 失败 job（NPU 31 / 门禁 fallback 0）
- cancelled 采样 49 job，未启动/未分配 runner 0 个
- NPU runner 排队: 中位 0min，最长 189min，>30min 有 16 个（>30min 提示 runner 池不足，infra 侧）

**NPU CI workflows**：`e2e_ascend.yml`、`e2e_ppo_trainer_megatron_sglang_2_ascend.yml`、`e2e_ppo_trainer_megatron_sglang_ascend.yml`、`e2e_ppo_trainer_megatron_vllm_2_ascend.yml`、`e2e_ppo_trainer_veomni_vllm_ascend.yml`、`e2e_sft_llm_ascend.yml`、`model_ascend.yml`、`nightly_ascend.yml`、`nightly_ascend_multinode.yml`、`npu_unit_tests.yml`、`reward_model_sglang_ascend.yml`、`reward_model_vllm_ascend.yml`、`sgl_ascend.yml`、`vllm_ascend.yml`

**近一周成功率**：`vllm_ascend.yml` 20%、`e2e_ppo_trainer_megatron_vllm_2_ascend.yml` 29%、`e2e_ppo_trainer_megatron_sglang_ascend.yml` 55%、`reward_model_vllm_ascend.yml` 60%、`e2e_ppo_trainer_veomni_vllm_ascend.yml` 58%、`e2e_ppo_trainer_megatron_sglang_2_ascend.yml` 62%、`e2e_ascend.yml` 62%、`model_ascend.yml` 67%、`reward_model_sglang_ascend.yml` 69%、`npu_unit_tests.yml` 64%、`e2e_sft_llm_ascend.yml` 69%、`nightly_ascend_multinode.yml` 0%、`nightly_ascend.yml` 78%、`sgl_ascend.yml` --

### 全部失败原因分析

| 排名 | 原因 | 次数 | 占比 | owner | 样例 job 链接 |
|---|---|---|---|---|---|
| #1 | 分布式通信/编排(Ray) | 10 | 40% | code | https://github.com/verl-project/verl/actions/runs/33582004912/job/100098041643 [NPU]、https://github.com/verl-project/verl/actions/runs/33523913225/job/99943980872 [NPU]、https://github.com/verl-project/verl/actions/runs/33512152839/job/99871031343 [NPU] |
| #2 | 超时 | 6 | 24% | mixed | https://github.com/verl-project/verl/actions/runs/33535791096/job/99949650735 [NPU]、https://github.com/verl-project/verl/actions/runs/33417769337/job/99572442005 [NPU]、https://github.com/verl-project/verl/actions/runs/33324266266/job/99291522548 [NPU] |
| #3 | 进程被kill(OOM/超内存) | 5 | 20% | mixed | https://github.com/verl-project/verl/actions/runs/33512153091/job/99871047050 [NPU]、https://github.com/verl-project/verl/actions/runs/33510593595/job/99865024285 [NPU]、https://github.com/verl-project/verl/actions/runs/33582765081/job/100100375458 [NPU] |
| #4 | 依赖/安装(ImportError) | 2 | 8% | code | https://github.com/verl-project/verl/actions/runs/33510593594/job/99865024236 [NPU]、https://github.com/verl-project/verl/actions/runs/33506161469/job/99850597233 [NPU] |
| #5 | 未分类 | 1 | 4% | unknown | https://github.com/verl-project/verl/actions/runs/33484351188/job/99780994258 [NPU] |
| #6 | 昇腾算子执行错误(ACL) | 1 | 4% | mixed | https://github.com/verl-project/verl/actions/runs/33505772150/job/99849329027 [NPU] |

**owner 汇总**：code 12，mixed 12，unknown 1


### 基础设施相关失败 Top3

1. **超时**（6 次，owner=mixed）：https://github.com/verl-project/verl/actions/runs/33535791096/job/99949650735 [NPU]、https://github.com/verl-project/verl/actions/runs/33417769337/job/99572442005 [NPU]、https://github.com/verl-project/verl/actions/runs/33324266266/job/99291522548 [NPU]
2. **进程被kill(OOM/超内存)**（5 次，owner=mixed）：https://github.com/verl-project/verl/actions/runs/33512153091/job/99871047050 [NPU]、https://github.com/verl-project/verl/actions/runs/33510593595/job/99865024285 [NPU]、https://github.com/verl-project/verl/actions/runs/33582765081/job/100100375458 [NPU]
3. **昇腾算子执行错误(ACL)**（1 次，owner=mixed）：https://github.com/verl-project/verl/actions/runs/33505772150/job/99849329027 [NPU]

> 说明：mixed 桶需结合 runner 配置/节点网络二次确认；调度/排队信号见上方 meta 行。

<!-- @/section:verl-project/verl -->

---

## 5. 方法学要点 / 架构坑（手工保留）

1. **transitive `uses:` 检测**：triton `ci.yml` 不含任何直接 NPU 特征，靠 `uses: integration-tests-ascend.yml` 传递链判定。
2. **`cann_image` 单独不能判强**：CPU runner 也能用 CANN 容器（triton `DynamicCVPipeline-ci` 曾误判）；`dynamic_runner`+`cann_image` 组合才是 NPU 执行模板。
3. **NPU 标签正则**：`linux-(?:aarch64|amd64)-(?:a\d[\w-]*|310p)-\d`，覆盖 a5/amd64 形态；aarch64 子串匹配会漏掉 triton 的 a5。
4. **门禁 fallback**：无 NPU 失败 job（NPU job 被 skip）时，降级分析该 run 的失败 job——sglang pr-gate 场景必需。
5. **owner 维度**：每桶标注 infra/code/mixed，基础设施相关失败一眼可筛；mixed 需结合 runner 配置/节点网络二次确认。
6. **调度指标**：`run.created_at → job.started_at` 排队时长，>30min 提示 runner 池不足（infra 侧），比翻日志更直接。
7. **未分类优化方向**：`::error::pre-commit did not succeed` 可归静态检查桶、多节点 `Error: failed to run script step` 可归 orchestrator 桶，可降低 vllm(42%)/sglang(75%) 未分类率。

<!-- @section:vllm-project/vllm-ascend@a2-a3 -->

## vllm-project/vllm-ascend@a2-a3（2026-09-17 ~ 2026-09-24）

- 分析时间: 2026-09-24 10:45:49 → 10:50:05（256s）
- 完整原始输出: `/home/tangjia/work/general/昇腾失败原因分析/npu_ci_reports/npu_ci_failure_report_vllm-ascend_a2-a3_20260924_104549.md`
- 芯片范围: `a2-a3`；抽样 59 失败 run → 87 失败 job（NPU 69 / 门禁 fallback 18）
- 已定性 40 份 → 去重后根因 35 个（同 run 同根因合并 5 次），假失败 0 份，门禁聚合级联 8 份（后两者均不计入根因分布）
- cancelled 采样 45 job，未启动/未分配 runner 0 个
- NPU runner 排队: 中位 7min，最长 326min，>30min 有 279 个（>30min 提示 runner 池不足，infra 侧）
- 日志扫描: 按失败步骤时间窗切分 37/40 份，其余回退全局尾部窗口
- 方法: 失败步骤（序号最靠前者）决定归因路径——容器/日志上传类直接判 infra 不读日志，安装/构建类扫时间窗前段，测试类扫时间窗尾部

### 失败步骤分布（序号最靠前的失败步骤）

| 失败步骤 | 失败 job 数 | 归因路径 |
|---|---|---|
| Run Pytest (YAML-driven) | 17 | 扫时间窗尾部 |
| Wait for pods ready | 10 | 多节点编排（需集群侧） |
| Run vllm-project/vllm-ascend accuracy test | 10 | 扫时间窗尾部 |
| Check npu and CANN info | 9 | 扫时间窗尾部 |
| Check all required jobs | 8 | 门禁聚合级联（不计入根因） |
| Run selected tests with device | 7 | 扫时间窗尾部 |
| Run vllm-project/vllm test | 6 | 扫时间窗尾部 |
| Run Installation doctest | 5 | 扫时间窗尾部 |
| Validate PR title prefix | 3 | 扫时间窗尾部 |
| Stream logs | 3 | 直接定性 infra（不读日志） |
| Run main2main flow | 3 | 扫时间窗尾部 |
| Run mypy | 1 | 扫时间窗尾部 |
| Run pre-commit | 1 | 扫时间窗尾部 |
| Rebase on main snapshot | 1 | 扫时间窗尾部 |
| Build nightly-a2 image | 1 | 扫时间窗前段 |
| Create multi-arch manifest | 1 | 扫时间窗尾部 |
| Build doctest plan | 1 | 扫时间窗前段 |

**NPU CI workflows**：`_e2e_nightly_multi_node.yaml`、`_e2e_nightly_single_node.yaml`、`_e2e_nightly_single_node_560t.yaml`、`_e2e_nightly_single_node_models.yaml`、`_selected_tests.yaml`、`_selected_tests_upstream.yaml`、`labeled_download_model_dataset.yaml`、`pr_test.yaml`、`schedule_doc_getting_started_test.yaml`、`schedule_e2e_upstream_test.yaml`、`schedule_main2main.yaml`、`schedule_nightly_test_a2.yaml`、`schedule_nightly_test_a3.yaml`、`schedule_nightly_test_a3_560t.yaml`、`schedule_test_coverage.yaml`、`schedule_weekly_test_a2.yaml`、`schedule_weekly_test_a3.yaml`、`schedule_weekly_test_a3_560t.yaml`

**近一周成功率**：`schedule_nightly_test_a3.yaml` 29%、`pr_test.yaml` 25%、`schedule_weekly_test_a3.yaml` 9%、`schedule_nightly_test_a3_560t.yaml` 33%、`schedule_nightly_test_a2.yaml` 49%、`schedule_test_coverage.yaml` 33%、`schedule_doc_getting_started_test.yaml` 95%、`schedule_e2e_upstream_test.yaml` 0%、`schedule_main2main.yaml` 75%、`schedule_weekly_test_a2.yaml` 0%、`schedule_weekly_test_a3_560t.yaml` 0%、`labeled_download_model_dataset.yaml` 100%

### 全部失败原因分析

| 排名 | 原因 | 次数 | 占比 | owner | 样例 job 链接 |
|---|---|---|---|---|---|
| #1 | 多节点pod调度/就绪失败(k8s侧) | 9 | 26% | infra | https://github.com/vllm-project/vllm-ascend/actions/runs/35945318692/job/107463057026 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/35943916849/job/107458606435 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/35847027412/job/107137639125 [NPU] |
| #2 | 断言失败(代码或精度) | 6 | 17% | code | https://github.com/vllm-project/vllm-ascend/actions/runs/35913944605/job/107384292391 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/35849194176/job/107144428907 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/35834822842/job/107097072719 [NPU] |
| #3 | 脚本步骤通用包装失败(需按失败步骤细化) | 5 | 14% | unknown | https://github.com/vllm-project/vllm-ascend/actions/runs/35948198384/job/107470808313 [gate]、https://github.com/vllm-project/vllm-ascend/actions/runs/35948164054/job/107470703396 [gate]、https://github.com/vllm-project/vllm-ascend/actions/runs/35947393377/job/107468290782 [gate] |
| #4 | 模型缓存未命中(离线模式 local_files_only) | 4 | 11% | code | https://github.com/vllm-project/vllm-ascend/actions/runs/35873808460/job/107226470947 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/35843928529/job/107127563487 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/35859176853/job/107176446069 [NPU] |
| #5 | 步骤直接定性:Stream logs | 3 | 9% | infra | https://github.com/vllm-project/vllm-ascend/actions/runs/35864170220/job/107193712056 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/35848635287/job/107142685064 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/35765724272/job/106876283603 [gate] |
| #6 | 依赖/安装(ImportError) | 3 | 9% | code | https://github.com/vllm-project/vllm-ascend/actions/runs/35842916764/job/107124146339 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/35745271509/job/106847568705 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/35720949164/job/106725244145 [NPU] |
| #7 | 静态类型检查失败(mypy) | 1 | 3% | code | https://github.com/vllm-project/vllm-ascend/actions/runs/35948267435/job/107471013241 [gate] |
| #8 | 静态检查(pre-commit/ShellCheck) | 1 | 3% | code | https://github.com/vllm-project/vllm-ascend/actions/runs/35947383006/job/107468461391 [gate] |
| #9 | 分布式通信/网络(HCCL/Store) | 1 | 3% | infra | https://github.com/vllm-project/vllm-ascend/actions/runs/35864608973/job/107220928141 [NPU] |
| #10 | 编译失败(C++/MLIR) | 1 | 3% | code | https://github.com/vllm-project/vllm-ascend/actions/runs/35846140918/job/107137185005 [NPU] |
| #11 | 未分类 | 1 | 3% | unknown | https://github.com/vllm-project/vllm-ascend/actions/runs/35741765889/job/106793863099 [gate] |

**owner 汇总**：infra 13，code 16，unknown 6
**按失败步骤直接定性（未读日志）**：Stream logs 3

### 基础设施相关失败 Top3

1. **多节点pod调度/就绪失败(k8s侧)**（9 次，owner=infra）：https://github.com/vllm-project/vllm-ascend/actions/runs/35945318692/job/107463057026 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/35943916849/job/107458606435 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/35847027412/job/107137639125 [NPU]
2. **步骤直接定性:Stream logs**（3 次，owner=infra）：https://github.com/vllm-project/vllm-ascend/actions/runs/35864170220/job/107193712056 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/35848635287/job/107142685064 [NPU]、https://github.com/vllm-project/vllm-ascend/actions/runs/35765724272/job/106876283603 [gate]
3. **分布式通信/网络(HCCL/Store)**（1 次，owner=infra）：https://github.com/vllm-project/vllm-ascend/actions/runs/35864608973/job/107220928141 [NPU]

> 说明：mixed 桶需结合 runner 配置/节点网络二次确认；pod 调度类结论需集群侧佐证。

### 待集群取证（第 2 步）

以下 11 项失败无法由日志单独定性，需用 CI 专用只读 kubeconfig 反查 runner pod 调度状态：

| runner pod 名 | 芯片 | 失败步骤 | 原因 | job 链接 |
|---|---|---|---|---|
| `linux-aarch64-a3-800t-0-chlqk-runner-58b8h` | a3 | Wait for pods ready | 多节点集群编排阶段（pod 调度/资源，需集群侧确认） | https://github.com/vllm-project/vllm-ascend/actions/runs/35945318692/job/107463057026 |
| `linux-aarch64-a3-800t-0-chlqk-runner-w99hs` | a3 | Wait for pods ready | 多节点集群编排阶段（pod 调度/资源，需集群侧确认） | https://github.com/vllm-project/vllm-ascend/actions/runs/35943916849/job/107458606435 |
| `linux-aarch64-a3-800t-0-chlqk-runner-qlcls` | a3 | Wait for pods ready | 多节点集群编排阶段（pod 调度/资源，需集群侧确认） | https://github.com/vllm-project/vllm-ascend/actions/runs/35847027412/job/107137639125 |
| `linux-aarch64-a3-800t-0-chlqk-runner-xxxtz` | a3 | Wait for pods ready | 多节点集群编排阶段（pod 调度/资源，需集群侧确认） | https://github.com/vllm-project/vllm-ascend/actions/runs/35844565118/job/107136919282 |
| `linux-aarch64-a3-800t-0-chlqk-runner-r7rkn` | a3 | Wait for pods ready | 多节点集群编排阶段（pod 调度/资源，需集群侧确认） | https://github.com/vllm-project/vllm-ascend/actions/runs/35844565118/job/107136923516 |
| `linux-aarch64-a3-800t-0-chlqk-runner-ftl56` | a3 | Wait for pods ready | 多节点集群编排阶段（pod 调度/资源，需集群侧确认） | https://github.com/vllm-project/vllm-ascend/actions/runs/35946362895/job/107466292643 |
| `linux-aarch64-a3-800t-0-chlqk-runner-258ms` | a3 | Wait for pods ready | 多节点集群编排阶段（pod 调度/资源，需集群侧确认） | https://github.com/vllm-project/vllm-ascend/actions/runs/35944236469/job/107459678429 |
| `linux-aarch64-a3-800t-0-chlqk-runner-9swq6` | a3 | Wait for pods ready | 多节点集群编排阶段（pod 调度/资源，需集群侧确认） | https://github.com/vllm-project/vllm-ascend/actions/runs/35849062599/job/107144046737 |
| `linux-aarch64-a3-800t-0-chlqk-runner-9jbn6` | a3 | Wait for pods ready | 多节点集群编排阶段（pod 调度/资源，需集群侧确认） | https://github.com/vllm-project/vllm-ascend/actions/runs/35841849997/job/107120877431 |
| `linux-aarch64-a3-800t-0-chlqk-runner-fzfj6` | a3 | Wait for pods ready | 多节点集群编排阶段（pod 调度/资源，需集群侧确认） | https://github.com/vllm-project/vllm-ascend/actions/runs/35841794639/job/107120866726 |
| `linux-amd64-cpu-4-cn12-001-xmkmq-runner-6v6hg` | gate | Create multi-arch manifest | 日志未给出根因，需集群侧确认 pod/节点状态 | https://github.com/vllm-project/vllm-ascend/actions/runs/35741765889/job/106793863099 |

> 本次未提供 `--cluster-kubeconfig`，集群取证已跳过（不阻塞第 1、3 步）。待昇腾 CI 专用只读 kubeconfig 就位后用上述 runner pod 名反查。

<!-- @/section:vllm-project/vllm-ascend@a2-a3 -->
