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
