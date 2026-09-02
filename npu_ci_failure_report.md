# NPU CI 失败分析报告

> 方法：静态筛出跑在 NPU 上的 CI workflow → 近一周执行记录 → 抽样失败 run 定位 NPU job → 日志尾部窗口根因分类 → top3。
> 脚本：`npu_ci_failure_analysis.py`（四仓通用，支持 `--repo`/`--since`/`--samples`/`--report-dir`/`--summary-file`）。
> 本文件为自动精简版：**每个仓库的章节由脚本每次运行自动更新**（`<!-- @section:... -->` 标记内内容会被替换），
> 标记外内容（本头部、跨仓快照、方法学）为手工保留。完整原始输出在 `npu_ci_reports/npu_ci_failure_report_<repo>_<ts>.md`（不入库）。

---

## ⚠️ 跨仓基础设施信号（手动快照，跑新数据后请对照各仓章节更新）

| 仓库 | 排队样本 | 中位 | 最长 | >30min |
|---|---|---|---|---|
| vllm-ascend | 1540 | 7min | 588min (~10h) | **239 个** |
| sglang | 59 | 4min | 711min (~12h) | **26 个** |
| verl | 92 | 0min | 203min | 5 个 |
| triton-ascend | 21 | 1min | 30min | 0 个 |

> 快照时间：2026-09-01 21:12~21:17。vllm / sglang 的 a2/a3/a5 NPU runner 池饥饿是主要基础设施信号（加 runner 或错峰可解）。

---

<!-- @section:vllm-project/vllm-ascend -->
## 1. vllm-project/vllm-ascend

**NPU CI workflows**：`schedule_nightly_test_{a2,a3,a3_560t,a5,310p}.yaml`、`schedule_weekly_test_*`、`schedule_e2e_test.yaml`、`schedule_main2main.yaml`、`pr_test.yaml`、`labeled_doctest.yaml` 等 23 个。NPU runner：`linux-aarch64-{310p,a2,a3,a5}-N`。

**近一周成功率**：nightly_a3 24%、a2 30%、a3_560t 20%、a5 0%、weekly_a3 9%、e2e 4%、pr_test 26%、labeled_doctest 60%、main2main 92%。抽样 67 个失败 run → 122 个失败 job（NPU 89 / 门禁 33）。

| 排名 | 原因 | 次数 | 占比 | owner |
|---|---|---|---|---|
| #1 | 未分类 | 17 | 42% | unknown |
| #2 | 断言失败(代码或精度) | 8 | 20% | code |
| #3 | 超时 | 5 | 12% | mixed |

其他：静态检查(ShellCheck) 5、测试参数缺失 3、依赖/安装 1、模型下载(外网) 1。

**owner 汇总**：code 17（断言 8 / ShellCheck 5 / 参数缺失 3 / ImportError 1）、mixed 6（超时 5 / 外网下载 1）、unknown 17。**infra 0**。

**调度**：runner 排队中位 7min、最长 588min、>30min 有 239 个（样本 1540）→ **池饥饿**。

> 结论：断言失败仍为 `AssertionError: some aisbench cases failed`（业务精度）。未分类主要来自 pr_test 门禁失败（pre-commit/select-tests/ci-gate）与多节点 orchestrator 层错误。示例：[断言失败 run](https://github.com/vllm-project/vllm-ascend/actions/runs/33496560846/job/99821415969)、[超时 run](https://github.com/vllm-project/vllm-ascend/actions/runs/33484583910/job/99783222651)。
<!-- @/section:vllm-project/vllm-ascend -->

---

<!-- @section:triton-lang/triton-ascend -->

## triton-lang/triton-ascend（2026-08-26 ~ 2026-09-02）

- 分析时间: 2026-09-02 10:32:02 → 10:32:56（54s）
- 完整原始输出: `npu_ci_reports/npu_ci_failure_report_triton-ascend_20260902_103202.md`
- 抽样 8 失败 run → 16 失败 job（NPU 16 / 门禁 fallback 0）
- cancelled 采样 11 job，未启动/未分配 runner 0 个
- NPU runner 排队: 中位 1min，最长 613min，>30min 有 3 个
- 近一周 workflow 成功率: ci.yml 75%

| 排名 | 原因 | 次数 | 占比 | owner |
|---|---|---|---|---|
| #1 | 未分类 | 3 | 38% | unknown |
| #2 | 断言失败(代码或精度) | 2 | 25% | code |
| #3 | 编译失败(C++/MLIR) | 2 | 25% | code |
| - | 其余 1 类 | 1 | - | - |

**owner 汇总**：code 5，unknown 3

样例 run 链接：
- 未分类: https://github.com/triton-lang/triton-ascend/actions/runs/33524864496/job/100091004108 [NPU]
- 断言失败(代码或精度): https://github.com/triton-lang/triton-ascend/actions/runs/33578908660/job/100088934376 [NPU]
- 编译失败(C++/MLIR): https://github.com/triton-lang/triton-ascend/actions/runs/33501861674/job/99836849611 [NPU]
- Python运行时错误: https://github.com/triton-lang/triton-ascend/actions/runs/33578908660/job/100088934439 [NPU]

<!-- @/section:triton-lang/triton-ascend -->

---

<!-- @section:verl-project/verl -->
## 3. verl-project/verl

**NPU CI workflows**（14 个，全 `aarch64` runner）：`e2e_ascend.yml`、`npu_unit_tests.yml`、`vllm_ascend.yml`、各 `e2e_ppo_trainer_*_ascend.yml`、`nightly_ascend*.yml`、`model_ascend.yml`。

**近一周成功率**：e2e 系列 39%~68%、`vllm_ascend` 27%（33 失败）、`nightly_ascend_multinode` **0%**（6 战 6 败）。抽样 99 个失败 run → 29 个失败 job 全在 NPU。

| 排名 | 原因 | 次数 | 占比 | owner |
|---|---|---|---|---|
| #1 | 超时 | 6 | 24% | mixed |
| #2 | 依赖/安装(ImportError) | 5 | 20% | code |
| #3 | 进程被kill(OOM/超内存) | 5 | 20% | mixed |

其他：分布式/编排(Ray) 3、模型下载(外网) 2、OOM/显存不足 1、昇腾算子(ACL) 1、未分类 2。

**owner 汇总**：code 8（ImportError 5 / Ray 3）、mixed 15（超时 6 / kill 5 / 外网下载 2 / OOM 1 / ACL 1）、unknown 2。**infra 0**。

**调度**：runner 排队中位 0min、最长 203min、>30min 5 个 → 基本正常。

> 结论：超时 6 次中 **5 次是 multinode RayJob submitter 超时**，nightly_ascend_multinode 持续 0%。GitHub 只留 orchestrator 层，**真错误在 k8s pod 日志**。示例：[RayJob 超时 run](https://github.com/verl-project/verl/actions/runs/33417769337/job/99572442005)、[进程被kill run](https://github.com/verl-project/verl/actions/runs/33496244483/job/99819165299)。
<!-- @/section:verl-project/verl -->

---

<!-- @section:sgl-project/sglang -->
## 4. sgl-project/sglang

**NPU CI workflows**：`pr-test-npu.yml`、`nightly-test-npu.yml`（`full-test-npu`/`diffusion-ci-gt-gen-npu` 近周无 run）。

**近一周**：`pr-test-npu` 100 runs 成功率 **44%**（27 失败，较上周 18% 回升）；`nightly-test-npu` 58 runs 成功率 **6%**（33 失败）。抽样 16 个失败 run → 37 个失败 job（NPU 25 / 门禁 fallback 12）。

| 排名 | 原因 | 次数 | 占比 | owner |
|---|---|---|---|---|
| #1 | 未分类 | 6 | 75% | unknown |
| #2 | 模型/包下载失败(外网) | 2 | 25% | mixed |

**owner 汇总**：mixed 2、unknown 6。**infra 0**。

**调度**：runner 排队中位 4min、最长 711min（~12h）、>30min 有 **26 个**（样本 59）→ **池饥饿**。

> 结论：上周 #1 的测试框架 bug（08-29）**已修复**。本轮失败集中在 nightly 多节点 poc job（真实错误在 k8s pod）与 503 外网下载，分类质量差（75% 未分类）源于 orchestrator 层信息不足。示例：[未分类 run](https://github.com/sgl-project/sglang/actions/runs/33322021236/job/99285556153)、[503 下载 run](https://github.com/sgl-project/sglang/actions/runs/33322021236/job/99285556460)。
<!-- @/section:sgl-project/sglang -->

---

## 5. 方法学要点 / 架构坑（手工保留）

1. **transitive `uses:` 检测**：triton `ci.yml` 不含任何直接 NPU 特征，靠 `uses: integration-tests-ascend.yml` 传递链判定。
2. **`cann_image` 单独不能判强**：CPU runner 也能用 CANN 容器（triton `DynamicCVPipeline-ci` 曾误判）；`dynamic_runner`+`cann_image` 组合才是 NPU 执行模板。
3. **NPU 标签正则**：`linux-(?:aarch64|amd64)-(?:a\d[\w-]*|310p)-\d`，覆盖 a5/amd64 形态；aarch64 子串匹配会漏掉 triton 的 a5。
4. **门禁 fallback**：无 NPU 失败 job（NPU job 被 skip）时，降级分析该 run 的失败 job——sglang pr-gate 场景必需。
5. **owner 维度**：每桶标注 infra/code/mixed，基础设施相关失败一眼可筛；mixed 需结合 runner 配置/节点网络二次确认。
6. **调度指标**：`run.created_at → job.started_at` 排队时长，>30min 提示 runner 池不足（infra 侧），比翻日志更直接。
7. **未分类优化方向**：`::error::pre-commit did not succeed` 可归静态检查桶、多节点 `Error: failed to run script step` 可归 orchestrator 桶，可降低 vllm(42%)/sglang(75%) 未分类率。
