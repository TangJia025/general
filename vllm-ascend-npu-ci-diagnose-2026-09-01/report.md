# vllm-ascend 近一周 NPU CI 失败诊断报告（2026-08-25 ~ 09-01）

> 按 `github-action-diagnose` skill（KadenZhang3321/agent-skills）流程执行：collect → fetch → diagnose。
> 诊断由 Claude 自身能力完成（未用 diagnose-ci-runs.js 的阿里云 DashScope 后端）。
> 分类规则遵循 SKILL.md 类型 A（基础设施）/ B（代码 Bug）/ C（精度回归）/ D（YAML/配置错误）/ E（疑难）。

## 0. 方法

- **collect**：`collect-failed-runs.js` 收集近 7 天失败 run 到 `failed-runs-2026-09-01.xlsx`。vllm-ascend 失败量**超过 GitHub API 分页上限 1000**，实际只记录到最新 1000 条。
- **fetch**：`fetch-run.sh` 逐 run 抓取失败 job 的 runner / 失败 step / annotations / 预过滤关键日志。
- **diagnose**：从 NPU 相关 workflow 抽样 40 个 run → 91 个失败 job（NPU 61 + CPU 门禁 30），逐 job 抓全量日志按根因签名归类。

依赖文件（本目录下）：
| 文件 | 说明 |
|---|---|
| `failed-runs-2026-09-01.xlsx` | collect 产物：1000 条失败 run |
| `sample_runs.tsv` | 抽样 run 列表（workflow / run_id / 时间 / 分支） |
| `fetch_out/*.txt` | 40 个 run 的 fetch-run.sh 输出（job 列表 + annotations + 关键日志） |
| `classify_result.txt` | 91 个失败 job 的根因分类明细 |
| `npu_failed_runs.json` | NPU workflow 失败 run 清单 |
| `scripts/*.py` | 分类/深挖辅助脚本（classify_jobs / dig_other / tail_log / hw_acl） |

## 1. 失败量总览（按 workflow）

| Workflow | 失败 run | 性质 |
|---|---|---|
| **E2E**（PR 触发） | **748** | ⚠️ 绝大部分是**门禁层失败，非 NPU 测试失败**（见 §2） |
| Nightly-A3 / A2 / A3-560T / 310P / A5 | 43 / 25 / 3 / 3 / 1 | 真实 NPU 测试失败 |
| schedule_e2e_test.yaml | 25 | NPU E2E |
| pr_test.yaml / Schedule All E2E Tests | 18 / 16 | NPU E2E |
| Weekly-A3 / 310P / A2 | 4 / 1 / 1 | 真实 NPU 测试失败 |
| Doc Test / Docs link / Cancel-on-close / Image-Build / Release | 16 / 5 / 51 / 24 / 5 | CPU / CD / 机器人，非 NPU |

NPU 相关 workflow 失败合计 ≈ 890/1000。

## 2. 关键结论 #1：E2E 的 748 次失败 ≠ NPU 问题

抽样 12 个 E2E run，**NPU 测试 job（run-selected-tests-\*）全部被 skip**，失败全在门禁层（CPU runner）：

- **`ci-gate`「Selected tests are required; add the ready-precise or ready-all label」**（8/12）→ **类型 D**（CI 门禁配置/流程）：PR 未打 `ready-precise`/`ready-all` label，测试选择失败，门禁主动阻断。
- **`select-tests`「CSRC build workflows changed on base, please rebase the code」+「Coverage recommendation did not succeed (skipped)」**（~5/12）→ **类型 D**：base 分支 csrc 构建变更要求 rebase。
- **`pre-commit`「Failed to fetch PR title from GitHub API」/ Run mypy 失败**（~4/12）→ 类型 A（GitHub API 调用失败）/ B（mypy 代码类型错误）。

> 三类共占 E2E workflow 失败大头。修门禁逻辑（label 判定、rebase 提示、PR title 抓取）可消除绝大多数假失败。

## 3. NPU 真实测试失败 top3（61 个 NPU job）

| # | 根因 | 次数 | 占比 | 类型 | 责任方 | 证据 |
|---|---|---|---|---|---|---|
| 1 | **aisbench 用例断言失败** | 25 | 41% | B/C | PR 作者 / 算法团队（重跑稳定通过则偏 A） | `AssertionError: some aisbench cases failed`（tools/aisbench.py:303），遍布 A3/A2/A5/310P 各档大模型（DeepSeek-V3.2、Qwen3.5-122B/397B、kimi-k2.5 等） |
| 2 | **分布式/HCCL 卡死 + EngineCore 死亡** | ~10 | 16% | **A 基础设施** | 基础设施团队 | `HCCL operator is 1836s. Timeout in seconds for execute_model RPC`、`shm_broadcast ... 60 seconds`（连发 12+ 分钟）、`EngineCore encountered a fatal error` → `RuntimeError: cancelled` → `EngineDeadError`、`httpcore.ConnectError: All connection attempts failed` |
| 3 | **NPU 硬件 / ACL 算子错误** | ~7 | 11% | **A** | 基础设施 / 昇腾 | `ERR99999`（A3-560T kimi-k2.5 两连）、`NPU function error: call aclnnFusedInferAttentionScoreV3 failed, error code is 161002`（e2e-upstream）、`error code 507xxx` |

### 次要但根因明确的模式

- **测试 config 引用缺失（310P 全灭 6/6）** → **类型 B**：`FileNotFoundError: 'tests/e2e/weekly/single_node/configs/Qwen3.6-35B-W8A8-310P-300I-DUO.yaml'`——Weekly-310P 6 个单节点 job 全因配置路径不存在而 collection error，属 CI 维护者的测试配置 bug。
- **精度回归（3 次）** → **类型 C**：`Acceptance length regression detected for Eco-Tech/GLM-5.2-w4a8`（Schedule All E2E 的 a3-8 三次同因）。
- **e2e-upstream 上游测试失败（6 job）** → 类型 B/A：`test_siglip.py` 跑了 **1377s**（估 20s，挂死/超时）、`test_mooncake_store_worker`、`test_dynamic_sd` + 2 个 OOM kill（exit 137）。
- **main2main** `resolve-source`：`remote error: upload-pack: not our ref 1a086ce8...` → 类型 D（上游 ref 失效）。

## 4. 节点池维度

- **310P 节点池**（`linux-aarch64-310p-{2,4}`-txxhn/jk67d）：本周失败全因 **config 文件缺失**，是脚本问题不是节点问题。
- **A3-0 / A3-16（cn12-001）多节点池**：集中出现 shm_broadcast/HCCL 超时 + EngineDead（类型 A，调度/通信不稳定）。
- **A3-560T**：出现 `ERR99999` 硬件信号（kimi-k2.5 两连），建议标记节点上报硬件团队。

## 5. 建议（按优先级）

1. **修 E2E 门禁**：label 判定逻辑（`ready-precise`/`ready-all`）与 select-tests 的 rebase 提示——748 次假失败主要来源。
2. **修 310P 测试配置**：补齐 `tests/e2e/weekly/single_node/configs/*.yaml`，一次消除 Weekly-310P 全灭。
3. **多节点稳定性**：查 shm_broadcast/execute_model RPC 超时根因（master EngineCore 被谁 kill），A3 多节点池调度不稳定。
4. **A3-560T `ERR99999`**：节点标记 SchedulingDisabled，交硬件团队。
5. **aisbench 断言**：抽样看单个 case 重跑是否稳定通过——稳定复现则报精度/性能回归，偶发则归 flaky。
