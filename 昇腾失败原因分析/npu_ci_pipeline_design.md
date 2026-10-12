# 昇腾 CI 失败分析流水线 · 端到端设计（当前实现）

> **本文是什么**：把「上游 CI 一失败 → 抢集群快照 → 日志分类 → 集群取证 → 历史归因 → LLM 判决 → 出报告」
> 这条**当前实际在跑**的链路，从头到尾写成一份可以按图读代码的文档。
>
> **本文不是什么**：不是设计理由的全集。每一处「为什么这么定」的完整推导与实测数据，仍在两份专题文档里
> （见 §8 文档地图）。本文只负责**流程本身**，并在 §7 如实列出当前已知缺口。
>
> **对应实现**：截至 2026-10-12 的工作区实现。注意 LLM 判决层（§3.6）尚未合入 `main`，
> 只存在于 `fix/npu-precedent-lead-not-verdict` 分支上。

---

## 1. 全景：三个进程，一条单向链

```
                         ┌───────────────────────────────────────────────┐
                         │  npu_ci_watch.py（常驻，单线程同步循环）        │
                         │  systemd user 服务，每 30s / 空闲 300s 一轮     │
                         └───────────────┬───────────────────────────────┘
                                         │ 发现失败 job（台账按 job_id 去重）
                    ┌────────────────────┴────────────────────┐
                    │ 阶段 A：立刻抢集群快照                    │ 阶段 B：等 job 结束后
                    │ pipeline.step3_cluster_forensics()      │ 才做（日志此刻才可下载）
                    │ → .forensics_state/snapshots/           │
                    └────────────────────┬────────────────────┘
                                         │ 子进程
                    ┌────────────────────▼────────────────────┐
                    │  npu_ci_failure_analysis.py（第 1 步）    │
                    │  静态筛 workflow → 抽样 → 失败步骤时间窗  │
                    │  → 32 桶正则分类 → Top3 汇总              │
                    └────────────────────┬────────────────────┘
                          ┌──────────────┴──────────────┐
                          │                             │
              handoff_run_<id>.json            scans/job_<id>.txt.gz
              （结构化交接面，不含日志正文）      （扫描窗正文，LLM 的输入）
                          └──────────────┬──────────────┘
                                         │ 子进程
                    ┌────────────────────▼────────────────────┐
                    │  npu_ci_forensics.py（第 2~5 步）         │
                    │  集群取证 → 历史归因 → LLM 判决 → 渲染报告 │
                    └────────────────────┬────────────────────┘
                                         │
                    ┌────────────────────▼────────────────────┐
                    │  forensics/report.py::render_report()    │
                    │  → npu_ci_reports/forensics_report_<ts>.md + .json
                    └─────────────────────────────────────────┘
```

**为什么必须有两个阶段，而不是一个**（这是整条链的形状由来，三条都是实测，不是推测）：

| 事实 | 后果 |
|---|---|
| 「失败步骤结束 → job 结束」实测 **55s / 56s / 55s** | 抢快照的窗口只有一个分钟级 |
| 进行中的 job **取不到日志**（`HTTP 404 BlobNotFound`，日志 blob 尚未生成） | 失败那一刻拿不到日志 |
| runner pod 一次性，**失败 9 分钟后**同标签 pod 已换人 | job 结束后很快拿不到现场 |

⇒「能取日志的时刻」与「pod 还活着的时刻」几乎不重叠。合并成「等结束后一起做」会永远拿不到 pod；
合并成「失败时就做」会永远拿不到日志。故：**阶段 A 抢现场，阶段 B 补日志**，两阶段不可合并。

---

## 2. 常驻监听器 `npu_ci_watch.py`

### 2.1 监听范围（双限定，两个条件都要满足）

- **workflow 文件路径**匹配 `schedule_(nightly|weekly)_test_a[23]`（默认 `--path-pattern`）。
  用 path 不用 name：name 可被改，而 path 正是阶段 A 的 `failed_jobs[].workflow` 口径，两边能对上。
- **触发事件**在 `watch_state.IN_SCOPE_EVENTS = {schedule, workflow_dispatch}` 内。
  同一个文件被 `pull_request` 触发的 run 语义不同，不收。
  ⚠️ 上游那几个文件只声明了 `workflow_dispatch`（nightly 由上游调度 workflow 拉起），所以**不要**
  把事件进一步收成「仅 schedule」——实测那会让 7 个在跑的 run 全被排除，而且不报错。

### 2.2 一轮做什么（`Watcher.run_once`）

1. `collect_runs()`：取 run 集合，是**三路并集**，不是「最近创建的 run」——
   翻页取最近创建的（`--max-pages` 默认 2）+ 全量并入 `status=in_progress` + 全量并入 `status=queued`。
   长 run（nightly 跑一两个小时）的 job 可能在创建后很久才失败，只看新建会漏。
   已结束的 run 按 **`created_at` 或 `updated_at` 任一落在 `--lookback`（默认 15m）窗口内**判定。
2. **降级保护**：任何一路查询失败 ⇒ `degraded=True` ⇒ 本轮**不推进**「已扫描 run」记账。
   否则残缺的一轮会把没看到的失败永久标记为「已看过」。
3. `note_stale_runs()`：创建超 `STALE_RUN_HOURS=24h` 仍未开始的 run 判为僵尸，
   不计入活动、不再每轮查它的 jobs；**每个 run_id 只记一次**（游标存台账，重启不重复报）。
4. 逐个 in-scope run 取 jobs；新失败 job 先 `record_job_seen()` 落台账（状态 `seen`）。
5. **阶段 A**：对「首次看到且窗口还在」（`should_take_snapshot`）的 job 抢快照。
   ⚠️ 「每个 job 只抢一次」的精确边界：`snapshot_attempted=True` 只在**成功**（`snapshot_ok`）
   或**判定已过窗口不值得抢**（`snapshot_missed`）两条路径上设置；因集群映射不可用 /
   `take_snapshot` 抛异常而没抢成的**只记 attempts、下轮会重试**（重试到 `--max-attempts` 后 `gave_up`）。
   「只抢一次」的意图是**抢得越晚越糟**：重复抢只会把现场覆盖成更晚的时刻，甚至拿到同标签的**别的** pod（假证据）。
6. **阶段 B**：对 `ready_for_analysis` 的 job 按 run 分组，逐 run 调两个子进程。
7. 落台账、算下一轮间隔（`next_interval`：有活动用 `--interval`，空闲用 `--idle-interval`）。

### 2.3 退出与容错

- `--dry-run`：一个字都不落盘（日志只上终端，台账只读；所有写入统一过 `mark()` / `mark_error()` 拦掉）。
- SIGTERM/SIGINT：置 `_STOP_REQUESTED`，主循环在**下一轮开头**干净退出（不打断台账落盘）；
  `sleep_seconds()` 是可被打断的睡眠，不必等满一个空闲间隔。
- 单轮整体失败（gh 未登录、断网、集群 API 不可达）：指数退避，上限 `MAX_BACKOFF_SECONDS=600`，
  进程**活着继续等**，并在日志里打完整调用栈。
- 单 job 失败：`record_error` 累计，连续 `--max-attempts`（默认 3）后置 `gave_up`（终态，留痕不静默丢弃）。

### 2.4 台账状态机（`forensics/watch_state.py`）

⚠️ 这张图**跨阶段 A、阶段 B，且跨多轮轮询**——每轮只推进一格，不是一次跑完的线性流程。
「谁推进了哪一格」见图下的表；图里没标它的话很容易被读成「拿快照 → 分析 → 出结果」的单趟流程。

```
发现失败 job ──(record_job_seen)──→ seen
                                     │
                   ┌─────────────────┴──────────────────┐
             阶段A：抢快照                        （窗口已过/不值得抢）
             ← 此刻 job 可能还在跑                  ← 明确放弃，不许伪装成已取证
                   │                                    │
             snapshot_ok                       snapshot_missed
                   └──────────────┬─────────────────────┘
                                  │
                    ⏸ 等 job 结束（ready_for_analysis：进行中的 job 日志是 404 BlobNotFound）
                                  │
              阶段B：一次 process_run，内部两段子进程
                 ① ANALYSIS_SCRIPT  定向日志分析   → analyzed
                 ② PIPELINE_SCRIPT  取证+归因+LLM+报告 → reported   ← 终态
                                  │
                  失败（mark_error ×3）→ gave_up        ← 终态
                  发现其实不是失败    → not_a_failure   ← 终态
```

| 转移 | 推进主体 | 时机 |
|---|---|---|
| `→ seen` | `record_job_seen`（在 `run_once` 的 job 循环里） | 发现失败 job，**在阶段 A 之前** |
| `seen → snapshot_ok` / `snapshot_missed` | `Watcher.phase_a` ← **这才是「阶段 A」** | 发现后立刻（job 可能还在跑） |
| `→ analyzed` | `process_run` 里跑完 `ANALYSIS_SCRIPT` | **必须等 job 结束** |
| `→ reported` | **同一次 `process_run`** 里接着跑完 `PIPELINE_SCRIPT` | 紧接上一步 |
| `→ not_a_failure` / `→ gave_up` | `run_once` 收尾 / `record_error` 累计 | `gave_up` 见 §2.3，`not_a_failure` 见下 |

三个容易读错的地方：

- **`analyzed → reported` 是同一次调用里先后标的**，正常路径不会停在 `analyzed`。停在 `analyzed`
  只有一个原因：**阶段 B 子进程失败**（`mark_error` 保留原状态）——这也正是 `PHASE_B_STATES`
  要包含 `analyzed` 的原因：它是重试入口。
- **快照没抢到（`snapshot_missed`）照样会走到 `reported`**。`snapshot_*` 与 `analyzed` 之间
  **没有依赖**，只是报告里如实写「未取证」（集群侧那节降级为标签可用性核查）。
  `snapshot_missed` 不是失败分支，是「现场没了，后面照常做」的另一条正常路径。
- **「阶段 A / 阶段 B」是监听器自己的两阶段划分**（抢快照 / 补日志+报告），
  而 `npu_ci_failure_analysis.py`、`npu_ci_forensics.py` 内部**各自还有一套「第 1~5 步」**。
  同名不同物。更绕的是监听器的阶段 A **内部复用了 forensics 流水线的「第 2 步」函数**
  `pipeline.step3_cluster_forensics`，于是两个语境的名字会出现在同一段代码里。**本文档一律用监听器语境。**

- `TERMINAL_STATES = {reported, not_a_failure, gave_up}`；终态不再进候选。
- `PHASE_B_STATES = {seen, snapshot_ok, snapshot_missed, analyzed}`。
- `not_a_failure` 的两个来源：① 阶段 B 定向分析没把这个 job 计入失败清单（不在 `--chips` 范围等）；
  ② 结束前发现步骤带 `continue-on-error`（`not_a_failure_reason`）。这是**终态**，
  避免把成功 job 写进报告、也避免每轮重跑一次必然同样结果的定向分析。
- `SNAPSHOT_GRACE_SECONDS = 90`：job 结束后 90s 内仍尝试抢快照。

### 2.5 已知的静默丢失与它怎么被堵住

- `scanned_runs` 记账让已结束的 run 不再取 jobs（省一次 API）。但台账里可能还有**刚记下、还没分析完**
  的 job —— 若不还原，它们会随着「run 已扫描」被永久跳过。故有
  `candidates_from_ledger()`：按 `run_id` 从台账把 `PHASE_B_STATES` 的 job 还原成 job 字典。
  它用 `status="completed"` 是**可靠推断**（run 已结束 ⇒ 它的 job 都结束了），不是猜测。
- 僵尸 run 的 job 同样走这条还原路径 —— 跳过的是「不再查它的 jobs」，不是「丢掉没分析完的 job」。

**「反复看到」与「反复动作」是两回事**：一个 job 只要没进终态，**每一轮都在候选里**
（必须如此：要轮询到 job 结束才能取日志），但「发现」（`ledger.seen` 守卫）、
抢快照（`snapshot_attempted` 守卫）、定向分析（handoff 存在则复用）、出报告（成功即终态）
各只发生一次 —— 只有**失败**路径会重试（见 §2.2 第 5 点与 §2.4 的 `gave_up`）。

---

## 3. 阶段 A：`npu_ci_failure_analysis.py`

被监听器以**定向模式**拉起：`--run-id <id> --job-id <id> ... --emit-json <handoff> --scan-dir <dir>`。
它自身仍是五步流水线（作为独立批处理工具时跑全量，被监听器拉起时只跑指定的 run/job）。

### 3.1 五步

| 步 | 做什么 | 关键点 |
|---|---|---|
| 1 | 静态筛出跑在 NPU 上的 CI workflow | 按 `--chips` 预过滤。**定向模式（`--run-id`，`INCREMENTAL`）整个跳过**：目标 run/job 已点名，workflow 名直接取 run 的 `path`。不能只是「结果用不上」—— `prepare_workflows()` 要为每个 workflow 文件发一次 `gh api`（约 100 次），而监听器每消费一个失败就要跑一次本脚本。⚠️ **芯片范围仍按 job 级 `chip` 判定**，不因跳过筛选而放宽 |
| 2 | 近 N 天各 workflow 执行记录 | 成功率 + 排队时长；跨仓基础设施信号持久化到 `--infra-store`。定向模式下打印「跳过 workflow 枚举与执行记录统计」，且不打印「抽样 / cancelled / 排队」三行（否则只会是一串 0，读者会误以为统计过） |
| 3 | 抽样失败 run → 定位失败 job | 取 `steps[]` 中 conclusion=failure 且**序号最小**的步骤（后续失败是级联）；同时采 runner pod 名、labels、芯片。定向模式改走 `discover_by_run_ids(run_ids, job_ids)` |
| 4 | 按失败步骤选扫描窗口 → 下载日志 → 根因分类 | 见 §3.2–§3.4 |
| 5 | Top3 汇总 + 失败步骤分布 + 假失败 + 待集群取证 | 写章节化 markdown；`--cross-repo` 时才更新跨仓表 |

**归因路由**（`route_for`，按失败步骤名）：步骤类型决定错误位置 ——
`no_log` / `pod` / `window_head`（安装类错误在**头部**）/ `window_tail`（测试类在**尾部**）/
（k8s 调度类**根本不在 GitHub 日志里**）。

### 3.2 扫描窗怎么切（`build_scan_window`）

1. **去噪**：丢弃 `\x1b[36;1m` 青色前缀行（GHA 回显的脚本源码，实测占 vllm 样本 42.5%，
   正则会命中脚本里 echo 的报错文案）与 `NOISE_PATTERNS`（失败后的清理动作、GHA 收尾输出）。
2. **按失败步骤时间窗切**（`slice_by_step_window`）：日志行首带 ISO 时间戳，
   取 `[started_at, completed_at]` 内的行。切不出来则退回**全局尾部窗口**（`windowed=False`）。
3. 按路由取 `[:tail_lines]` 或 `[-tail_lines:]`。

抽成**纯函数**（不读模块级 `ARGS`）的目的：离线评测复用生产代码本身的窗口算法 ——
评测里另写一遍窗口逻辑，测的就不是线上行为。

### 3.3 分类：32 桶，**表序首命中**（`classify_text`）

```
for pattern, label, owner in BUCKETS:
    if re.search(pattern, text_scan, re.I):
        return label, 命中片段±30字
return "未分类", ""
```

- `BUCKETS` 的**排列即优先级**（本身是校准结果）：真根因桶在前，通用包装桶在后。
  多处注释写着「必须排在 X 之前」，例如 HCCL/Store 两桶必须先于 ACL 桶（否则
  `hcclComm_, error code is 7` 会被 `error code is \d+` 吞进 ACL 桶、owner 从 infra 错配成 mixed）。
- `BUCKET_OWNER` 是桶 → owner 真值表；owner ∈ `infra / code / mixed / unknown / 假失败`。
- ⚠️ **首命中是按表的顺序，不是按日志里出现的先后** —— 这条在 §7.2 是一个已实测的缺陷来源。

### 3.4 两条特殊通道

- **决定性判据提前退出**（`DECISIVE_BUCKETS`）：`测试未执行(入口/用例集不存在，脚本与代码错配)` 与
  `测试用例失败(pytest ret=1)`，判据是测试框架**自己打印的判定行**
  （`pytest exit code: ret=\d+`），硬到不需要集群侧旁证。命中即：
  ① 不进 `cluster_todo`；② 阶段 B 跳过集群取证且**不占 `--max-cases` 名额**；
  ③ 报告里显式写「集群侧：按规则跳过」，不留白。
  `is_decisive()` 还有第 ② 条判据：桶判 owner=code **且**日志里有 pytest 判定行 ——
  因为 ret=1 的日志尾部常带断言 traceback，首选命中会落到【断言失败】等桶（owner 同为 code，
  但不在 `DECISIVE_BUCKETS` 里），只按桶名判会漏。
- **对端节点日志（第二证据源）**（`peer_logs.adopt_peer_bucket`，**仅兜底**）：
  多节点 job 的 node0 之外的机器只在产物 `*-ascend-logs` 里。策略是**只在 node0 判「未分类」时才采用**，
  并在报告里注明来源 —— 对端文本是产物**尾部粗切、无时间窗对齐**，拿它改写一个已定性的结论
  等于用更弱的证据推翻更强的那个。

### 3.5 扫描窗落盘（LLM 判决层的输入，`write_scan_sidecar`）

- **只写「真的扫到了窗口」的**（`scan_lines` 非空）。`scan_lines` 为空时 `text_scan` 会回退成
  **整份日志**，那种 case 不落 sidecar（模型会吃到一份远超 token 预算的文本），下游也就没有 LLM 判决。
- **必须在「对端节点兜底之前」写**：写的是 **node0 失败步骤时间窗**，不是 peer 采纳后的文本。
- 用 `gzip.GzipFile(..., mtime=0)` 写，让内容相同的 sidecar 逐字节相同（便于比对/去重）；
  返回 `(文件名, sha256(原始字节))`。
- 落盘目录必须被 `.gitignore` 覆盖（默认 `.forensics_state/scans`）：**日志含内网集群名/IP，本仓是 PUBLIC**。
- 保留期 `--scan-retention-days`（默认 14 天），启动时 `prune_scan_sidecars()` 清理，
  只删自己命名的 `job_*.txt.gz`，不递归、不碰别的文件。

---

## 4. 交接面：`handoff_run_<run_id>.json`（`--emit-json`）

结构化交接面，**不含日志正文**。下游据此决定「这条能不能交给 LLM 判」「要不要查集群」。

```jsonc
{
  "meta":            { repo, since, chips, generated_at, report_path, ... },
  "failed_jobs":     [ /* 全量失败 job：run_id/job_id/runner_name/labels/失败步骤及时间窗 */ ],
  "classifications": [ /* 每条：桶/owner/sig/step/link + job_id/runner_name/labels/chip
                          + failed_step_started_at/completed_at + scan_ref + scan_sha256 */ ],
  "cluster_todo":    [ /* 日志侧明确要求集群取证的队列（阶段 B 直接消费） */ ],
  "buckets":         [ { label, owner }, ... ]   // 桶 → owner 真值表，供下游做修复建议映射
}
```

- `_enrich()` 用 `link` 里的 `/job/(\d+)` 反解 job_id，与 `failed_jobs` 做关联，
  保证导出文件**自包含**、下游无需再猜。
- `scan_ref` / `scan_sha256` 可能为 `null`（no_log 路径 / 窗口切不出来 / 写盘失败）——
  下游看到没有 ref 就退回规则结论。
- `buckets` 由**拥有桶表的阶段 A** 发出（而不是让阶段 B 反向 import 阶段 A 去解析源码）。

---

## 5. 阶段 B：`npu_ci_forensics.py`

被监听器以 `--handoff <path> --cluster-snapshot <dir> --scan-dir <dir> --llm-cache-dir <dir>
[--llm-judge] ...` 拉起。**不传 `--handoff` 时会自己拉起阶段 A**（那条路径必须透传 `--scan-dir`，
否则阶段 A 写默认目录、阶段 B 读别处 → 每条静默降级成 `evidence_unavailable` 且不报错）。

### 5.1 顺序

| 步 | 做什么 | 降级行为 |
|---|---|---|
| 1 | 取 handoff（或自己拉起阶段 A 生成） | 失败即整批 `mark_error` |
| 2 | 装载集群注册表（`step2_load_registry`） | 集群映射不可用 → 记错误、下轮再试 |
| 4(前置) | 装载 issue 知识库（`IssueIndex`） | 取 issue 出错 → 记进 `errors`，不中断 |
| 3 | 逐案集群取证 | `decisive` 的走 `step3_skip_cluster`，**不碰集群** |
| 4 | 逐案历史归因（`step4_history` / `step4_related`） | 无先例即空 |
| 5 | 合成判决 + 渲染 | 见 §5.4 / §3.6 |

**集群身份必须自检**（`cluster_registry`）：runner 标签后缀**不是**可靠的集群判别依据
（Liqo 会把虚拟节点 pod 反射进共享 namespace，同一标签能从 3 个不同 kubeconfig 看到）。
集群侧只执行**只读动词**（白名单 `cluster_forensics.READ_ONLY_VERBS`）。
拿不到证据就写「未取证」，绝不用推测填充。

**快照并入**（`load_cluster_snapshots` / `apply_cluster_snapshot`）：
阶段 A 抢到的快照按 `job_id` 关联进 `cluster_result`。
⚠️ case 字典里**必须**带 `job_id` —— 早先漏了它，表现为「明明抢到了快照却没用上」。

### 5.2 案例选取（`select_cases`）

- 优先级：① 在 `cluster_todo` 里 → ② 桶知识表标了 `probe` → ③ 其余按原顺序。
- **`decisive` 的 case 必进报告，但不占 `--max-cases` 名额**（它们不做集群取证）。
  名额是留给「不查集群就定不了性」的 case 的。
- ⚠️ 按优先级排完**不能直接取前 N 个**：实测一次真实运行里 `--max-cases 4` 取到的 4 个 job
  全是同一个桶（都是「Wait for pods ready」），报告看起来做了 4 份取证、实际只有 1 份信息。
  故同优先级内**按桶轮流取**（round-robin），桶内仍按原顺序。
- 跳过假失败（非真实失败，取证无意义）。

### 5.3 逐案的 case 结构

`bucket / owner / sig / step / chip / labels / runner_name / is_npu / link / job_id / run_id /
window{started_at, completed_at} / cluster{...} / history[] / related_issues[] / peer / sig_source`
→ 再过 `synthesize()` 得到 `verdict` → 过 `merge_llm_verdict()`（§3.6）。

### 5.4 输出（`--report-dir`，时间戳命名，不覆盖手工运行的报告）

- `forensics_report_<ts>.md`：章节 = 摘要 / 集群取证可用性（含各集群现场快照）/ 逐案取证 /
  能力边界与局限（含本次运行遇到的错误）。
- `forensics_report_<ts>.json`：`{meta, cases, cluster_availability, cluster_health, limitations, errors}`。
  `meta.llm = {enabled, model, prompt_version, summary}`（开了 LLM 判决层时）。
- 监听器用 `extract_report_path()` 从 stdout 的 `报告:` 行取路径 —— **它自己打印的那行就是权威来源，
  不猜文件名**。

---

## 6. 第 5 步（LLM 判决层）：`forensics/llm_*.py`

用**同一段扫描窗**（sidecar）判根因，结论进根因行，规则结论**并列保留**。
不传 `--llm-judge` 时输出与不装这一层**逐字节相同**。

### 6.1 五个模块

| 模块 | 职责 |
|---|---|
| `forensics/llm_client.py` | `DeepSeekClient`（urllib 传输、错误分类、可重试项重试）；`FakeLLMClient` / `ReplayLLMClient` 供测试与离线回放 |
| `forensics/llm_evidence.py` | 证据正文构造：`estimate_tokens`（`CHARS_PER_TOKEN=2.6`）、`find_terminal_lines`（终局行优先保留）、超预算时头尾切分 |
| `forensics/llm_prompt.py` | system/user prompt 组装；**桶闭集由调用方注入**（写死在 prompt 里的话，桶表一改闭集就会与解析器静默分叉） |
| `forensics/llm_judge.py` | `judge_case(scan_text, *, client, allowed_classes, rule_hint=None, budget_tokens, max_tokens, timeout, **case_meta)` —— **绝不抛异常** |
| `forensics/llm_verdict.py` | `PROMPT_VERSION="v2"`、`LLMOutcome`、`parse_llm_verdict`、`validate_citations`、`apply_llm_verdict` |

### 6.2 调用点与范围门（`npu_ci_forensics.merge_llm_verdict`）

```python
if llm_client is None or not verdict["root_cause"].startswith("未能定性"):
    return verdict, None                    # ← 范围门：只判规则层判不出来的
verdict["root_cause_rule"] = verdict["root_cause"]   # ⚠️ 必须在合并**之前**留对照
return apply_llm_verdict(verdict, outcome, rule_bucket=case.get("bucket")), outcome
```

- **范围门 = `startswith("未能定性")`**：已有集群侧实证的 case 完全不碰（连 `llm` 块都不会有），
  LLM 不去覆盖硬证据。
- `merge_llm_verdict()` 是**为可证伪而抽出来的调用点函数**：原设计内联在 `main()` 循环里，
  那段逻辑要跑完整套集群取证才走得到，「拆掉范围门」不会有测试红。
- **不传 `rule_hint`**（模型不看规则桶，避免被它锚定）。

### 6.3 缓存（`read_llm_cache` / `write_llm_cache`）

- 位置 `.forensics_state/llm_verdicts/job_<job_id>.json`（已被 gitignore 覆盖）。
- **键 = `job_id` + `PROMPT_VERSION` + `scan_sha256` + `model`，四元组任一不符即未命中**
  （文件名只按 job_id）。提示词一改，旧判决全部作废。
- **只缓存成功**（`if not outcome.used or not outcome.parsed: return`）；降级不缓存，下轮会重判。
- 原子写（tmp + `os.replace`）；**不存 `raw_response`**（原始响应的回放是 eval 的事）。

### 6.4 预算与降级

- **启动即硬失败**：`--llm-judge` 打开但 `DEEPSEEK_API_KEY` 为空 → `SystemExit`，并提示
  `~/.config/npu-ci-watch/deepseek.env`。理由：否则整轮每个 case 都静默降级成
  「LLM 判决不可用（原因：http_4xx），已退回规则判决」，白跑一轮还看不出来。
- **墙钟预算**（`--llm-budget-seconds`，默认 900s）：`deadline = time.monotonic() + budget_seconds`；
  每次调用前若已超预算 → 直接 `LLMOutcome.degraded("budget_exhausted")`，**不发请求**。
- 单次调用超时 `--llm-timeout` 120s；输出上限 `--llm-max-tokens` 4000；
  输入证据预算 `--llm-budget-tokens` 60000。
- 循环结束后 `summarize_outcomes` + `render_summary_line` 打一行汇总。

### 6.5 合并纪律（`apply_llm_verdict`，每条都有对应测试）

| 纪律 | 含义 |
|---|---|
| `owner_from_cluster` 为真时**不得覆盖 owner** | 集群侧实证比模型推断硬；只加一条 conflict 并降置信度 |
| `basis` **只追加不插队** | 既有测试断言 `basis[0]` 的前缀 |
| `needs_human` 是**单向棘轮** | LLM 只能置真，不能清除规则/集群侧已判出的复核标记 |
| **归类不出现在任何给人读的句子里** | 给人看的是 `root_cause` 那段自由文本；归类只落 `merged["llm_class"]`，供统计/去重/派活 |
| 只有「模型真的给了闭集内的归类」才谈冲突 | 投影成 `其他`（留空或越界）不是冲突，是「闭集里没有这一格」 |
| `official_leaf` / `precedent` / `owner_from_cluster` / `suggestions` **原样保留** | LLM 层是纯后处理，不改 `synthesize()` 一个字 |

降级路径：`confidence` 置 `"低（LLM 判决不可用，退回规则）"`，`needs_human=True`，
`basis` 追加 `LLM 判决不可用（原因：…），已退回规则判决：<原根因>`。

**报告里两段根因并列**（`render_case`）：LLM 判决行之后补
`- **规则判断**：{root_cause_rule}（以上为 LLM 判决，此行仅作对照）`。

---

## 7. 报告合成层 `forensics/report.py`

### 7.1 三条合成纪律

1. **日志侧与集群侧两层证据并列，不互相覆盖**。**历史先例不在这两层里** ——
   它是线索，不是证据（见 §7.4）。
2. **冲突要显式标出**：当先例根因指向的责任方与日志侧桶的 owner 不一致时，报告必须摆出来提示核对，
   而不是挑一个写进结论。
3. **未取证就写未取证**：缺 kubeconfig、权限不足、pod 已回收都是正常结果，绝不用推测填充。

### 7.2 `synthesize()` 返回的 verdict 结构

```
root_cause, owner, owner_from_cluster, confidence, basis[], conflicts[],
hints_requiring_human[], needs_human, official_leaf, suggestions[], precedent{...}
```

（LLM 层合并后再添 `confidence_rule` / `llm` / `llm_class` / `root_cause_rule`。）

**置信度阶梯**（从高到低，逐条判）：

| 条件 | 置信度 |
|---|---|
| pod 实证 + 日志侧一致 | 高（集群侧实证 + 日志侧一致） |
| 集群侧只找到同 scale-set **另一次运行**的 pod（`time_consistent is False`） | 中低（非本 job 现场） |
| `decisive` 且桶非未分类 | 中高（日志侧决定性判据；按规则未做集群取证） |
| 有 pod 实证但状态无异常 | 中（已定位 pod 但未能据此定性） |
| 桶非未分类 | 中（仅日志侧正则定性，未经集群侧验证） |
| 都不满足 | 低（未分类） |
| **对端日志兜底定性**：上表任何「高」都被降为 | 中（对端文本无时间窗对齐） |

⚠️ **这里没有「命中历史先例 → 提高置信度」这一档**（曾经有，已删）：先例是线索不是证据，
留着它会造成措辞自相矛盾 —— 根因行已经写了「未能定性」，置信度却说「中高（命中同签名的历史先例）」。

### 7.3 根因行只写有实证的话

```
pod_evidence 且 cluster_owner_votes  → "集群侧实证：" + 容器终止原因解读
bucket != 未分类                     → "未能定性（仅有日志侧归类，需人工介入）"
否则                                 → "未能定性（需人工介入）"
```

桶**不是**结论：规则层的桶是「正则表序首个命中」，冻结集 30 例里「现象归因一致」只有
**6 例（20.0%，95% CI 6.7%~36.7%）**，即自报结论时约 **80% 是错的**；同一种真因在不同噪声分布下
还会拿到两个不同的桶（aisbench 性能未达标 → A1 判 HCCL/infra、B3 判 断言失败/code）。
**这是 §6 的范围门要接上来的那个空位**：范围门命中的正是上面这两行「未能定性」。

### 7.4 先例（第 14 节口径）

- 先例**不冒充结论**：不进根因行、不抬置信度。产线实测：588 份报告里 #227 一条先例被写成根因 241 次（41%），
  人工可核的样本 4/4 全错。原因是「检索到同现象的 issue」推不出「本次失败就是这个原因」——
  知识库是有偏样本（只有值得写复盘的才进去）。这是**用先例代替取证**，换更好的检索器也修不掉。
- 先例仍逐条列出（`case["history"]`、`basis` 行、返回值的 `precedent` 键），
  修复建议里带的先例修复记录**必须标出「未采信为结论，仅供人工比对」**。

### 7.5 集群侧证据的解读细节（几个"说错方向"的坑）

- **快照取自 job 结束前**（`snapshot_pod_still_running`）时，容器退出码与终止原因**尚未产生**。
  必须点明，否则读者会把空值读成「查过了，没有异常终止」—— 这是**与事实相反**的结论。
- **时序不符的 pod**（启动于本 job 失败步骤之后）不计入 owner 票，降级为「邻近 pod」，
  只能证明该 runner 标签可用。
- **标签族有效但后缀不一致**（登记 `cn12-001`、pod 名实际 `chlqk`）与「有 runner」「没 runner」
  都是两回事，单独措辞：对不上的是**注册表**，不是 runner 未上线。
- **「未查到」不计入 owner 票**：那是查询时刻的快照，证不了失败当时的状态。

---

## 8. 关键不变量（拆掉就会有测试红）

1. `--llm-judge` 关闭时，阶段 B 输出与不装这一层**逐字节相同**。
2. `--llm-judge` 开 + 无 key → `SystemExit`，**不是**整轮静默降级。
3. 只有「未能定性」的 case 被判；集群侧实证的 case 不得出现 `llm` 块与 `root_cause_rule`。
4. `root_cause_rule` == 合并**前**的规则那句。
5. 缓存命中不调 client；改 `PROMPT_VERSION` 或 `scan_sha256` 必须重判。
6. 预算耗尽 → `degraded("budget_exhausted")`，不抛异常、不中断整批。
7. sidecar 往返：阶段 A 写 → 阶段 B 读 → sha 一致；sha 被改则拒绝使用。
8. sidecar 的**正文不落 handoff**；`scan_lines` 为空时不落 sidecar。
9. `DECISIVE_BUCKETS` 的 job **绝不**出现在 `cluster_todo` 里，且**不占** `--max-cases` 名额。
10. 对端文本里有 `ret=1` 也不得让 case 变 `decisive`（用更弱的证据推成「已定性」）。

> 证伪前**必须** `rm -rf forensics/__pycache__ tests/__pycache__ eval/__pycache__` 并用 `python3 -B`
> —— 等长改写会同秒复用旧字节码，造出假绿。

---

## 9. 运行态落盘物（`--state-dir`，默认 `.forensics_state/`，已 gitignore）

| 路径 | 内容 | 写者 | 删掉的后果 |
|---|---|---|---|
| `ledger.json` | 台账：状态、尝试次数、报告路径、游标 | 监听器 | **会重新分析已处理过的失败**（多花日志 API、重复出报告） |
| `snapshots/job_<id>.json` | 失败时刻的集群快照（pod 状态 + 容器日志） | 监听器 | **不可重建**：pod 一次性。最该备份的东西 |
| `handoffs/handoff_run_<id>.json` | 阶段 A 的结构化交接面 | 阶段 A | 阶段 B 会重跑第 1 步（再花一次日志下载） |
| `scans/job_<id>.txt.gz` | 失败步骤时间窗的日志正文（LLM 的输入；保留 14 天） | 阶段 A | 保留期内该 job 的 LLM 判决做不了，退回规则结论 |
| `llm_verdicts/job_<id>.json` | LLM 判决缓存（键 = job_id + prompt_version + scan_sha256 + model） | 阶段 B | 只是重判一次（要花钱），无正确性问题 |
| `watch.log` | 轮询与动作日志（同 journalctl，但独立留存） | 监听器 | 仅丢日志 |
| `analysis_logs/` | 每次调流水线的 stdout/stderr | 监听器 | 出问题时少一条排查线索 |

**日志与 fixture 绝不进入本仓**（含内网集群名/IP）——适用于 `.forensics_cache/`、`eval/fixtures/`、
`.forensics_state/`（含 `scans/` 与 `llm_verdicts/`）。

---

## 10. 当前已知缺口（截至 2026-10-12，如实列出）

| # | 缺口 | 影响 | 状态 |
|---|---|---|---|
| 1 | 阶段 B 的 stdout 被 `subprocess.run(capture_output=True)` 收走后**只用于抠报告路径**，不落盘、不进 journal | 第 5 步那行 `[第5步] LLM 判决已启用 / N 成功 / M 降级` 在生产里**看不见**；唯一可用处是报告 JSON 的 `meta.llm.summary` | 未修 |
| 2 | 桶表**缺两条 GHA 包装语**：① 自托管 runner 的 `Executing the custom container implementation failed…`（桶表唯一的包装语兜底是 `failed to run script step`，是**另一条**消息、实测计数 0）；② manifest 合并脚本自述的 `No variant could be merged` | 实测 9/22 例零命中 → `未分类`（实为「表认不出的英文自述句」） | 未修 |
| 3 | 表序首命中被包装语抢占：`Failed to build`（#8）压过 `RPC failed`（#15），"依赖解析/构建失败(含链式噪音)" | 实测 6/22 例的**真机制**（外网/内网拉取失败）被包装语桶接走 | 未修 |
| 4 | 报告上的桶可能**不来自 LLM 判的那段文本**：node0 未分类时采对端日志改写桶，而 sidecar 是 node0 窗口 | 实测 1/22 例；同源假设不成立，读者会误以为两者同源 | 未修 |
| 5 | `npu_ci_forensics.py` 模块 docstring 写「29 桶正则」，实际 **32 桶** | 文档与代码不符 | 未修 |
| 6 | `npu_ci_forensics_design.md` §2 模块布局**漏了 5 个 `llm_*` 模块** | 照该表找 LLM 层会找不到 | 未修 |
| 7 | LLM 判决**质量无人验证**：`eval/truths.jsonl` 30/30 仍是 `proposed` | 「模型与规则的分歧」「模型是否给出更具体的机制」可量；**准确率不可量**，当线索读 | 已知边界 |
| 8 | 停服期间失败的 job 没有快照 | pod 一次性，**不可重建**；之后只能降级为「仅有日志侧归类」 | 设计使然 |

---

## 11. 文档地图

| 文档 | 讲什么 | 什么时候读它 |
|---|---|---|
| **本文** | **端到端流程与模块清单（当前实现）** | 接手代码、想知道「现在怎么跑的」 |
| `npu_ci_failure_design.md` | 阶段 A 的完整设计与四次口径变更（§12 LLM 化 / §13 结论走自由文本 / §14 先例是线索 / §15 LLM 层接入生产） | 想知道「为什么这么定」 |
| `npu_ci_forensics_design.md` | 阶段 B 与监听器的完整设计（集群身份、降级路径、案例选取、先例机制、置信度阶梯、监听器实测数字） | 想改集群取证或监听器 |
| `deploy/README.md` | 部署与运维（三步逐级放开的验证、运行态文件表、LLM 层开关、排障表） | 要装/要排障 |
| `npu_ci_failure_report.md` | **产物，不是文档** —— 章节由脚本每次运行自动更新 | 看统计 |

---

## 12. 验证方式

```bash
# 全部单测（20 套件）
rm -rf forensics/__pycache__ tests/__pycache__ eval/__pycache__   # 必须
python3 -B tests/test_llm_integration.py    # 单套
for t in tests/test_*.py; do python3 -B "$t" || echo "FAIL $t"; done

# 只打印将要做什么，一个字都不写
python3 npu_ci_watch.py --once --dry-run

# 关开关等价性：拿一份存档 handoff 跑阶段 B，diff 与改动前生成的报告 —— 必须逐字节相同
python3 npu_ci_forensics.py --handoff <存档>.json --offline --report-dir /tmp/cmp

# 集群连通性与身份自检（不碰阶段 A）
python3 npu_ci_forensics.py --self-check-only

# 手动补一段窗口（不必改服务）
python3 npu_ci_watch.py --lookback 2h --once
```
