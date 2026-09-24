# 昇腾 CI 失败取证流水线 · 设计文档

> 配套文档：[`npu_ci_failure_design.md`](./npu_ci_failure_design.md)（第 1 步：GitHub 侧失败分析）
> 入口脚本：[`npu_ci_forensics.py`](./npu_ci_forensics.py)
> 最后更新：2026-09-24

## 1. 目标

把一次 CI 失败的定位过程固化成一条可复现的流水线，覆盖五个环节：

```
上游 workflow CI 失败 → 失败日志提取 → 借助 kubeconfig 排查现场 → 历史问题定位归因 → 根因 + 修复建议
        [第1步]            [第1步]            [第2步]                  [第4步]            [第5步]
```

其中第 1 步复用已有的 `npu_ci_failure_analysis.py`（29 桶正则 + 失败步骤时间窗 + 按 run 去重），
本工具从它的结构化产物接着往下做。第 3 步（案例选取）不是一个独立环节，而是「选哪些 job 值得花
取证成本」的裁剪策略。

一条命令即可跑通全流程：

```bash
python3 npu_ci_forensics.py                       # 跑分析 + 集群取证 + 历史归因 + 出报告
python3 npu_ci_forensics.py --handoff a.json      # 复用已有分析结果，跳过第 1 步
python3 npu_ci_forensics.py --self-check-only     # 只做集群连通性与身份自检
python3 npu_ci_forensics.py --offline             # 只用本地缓存（不联网）
```

## 2. 模块布局

| 模块 | 职责 | 关键约束 |
|---|---|---|
| `npu_ci_forensics.py` | 驱动入口：串起 5 步、参数解析、报告落盘 | 是唯一会写文件的层 |
| `forensics/cluster_registry.py` | runner 标签 → 集群 → namespace → kubeconfig 的映射；集群身份自检 | 只返回**候选**，不做单点猜测 |
| `forensics/cluster_forensics.py` | 只读 kubectl：pod 定位、容器状态、容器日志、标签可用性核查 | 动词白名单，只读 |
| `forensics/issue_knowledge.py` | 179 个历史 issue（含 269 条评论）的知识索引与匹配 | 词面匹配 + 人工策展 |
| `forensics/knowledge_tables.py` | 29 桶的补充知识：修复动作、验证指引、关联 issue | 按桶**标签**索引，不按序号 |
| `forensics/report.py` | 三层证据合成、置信度阶梯、Markdown 渲染 | 冲突必须显式标出 |

## 3. 第 2 步：集群侧取证

### 3.1 为什么必须做这一步

多节点测试的 GitHub 日志只有 orchestrator 层，真实错误在 k8s pod 日志里（结构性缺失，日志侧修不了）。
更常见的是：**失败信号所在的层 ≠ 责任方所在的层** —— 官方分类树里的「runs-on 标签不存在 / Runner 未上线」
「pod 调度失败」这类叶子，日志里根本看不到，只能到集群侧确认。

### 3.2 集群身份：三条实测教训

集群身份错判的代价是「拿别的集群的证据解释本次失败」，比没有证据更糟，所以这三条是硬约束：

1. **runner 标签后缀不能用来判集群**。Liqo 会把虚拟节点的 pod 反射进共享 namespace，实测
   `linux-aarch64-a3-800i-*-cn12-001` 的 pod 能同时从 aiframework / cn12-001 / mind-third-ci
   三个 kubeconfig 看到。因此 `resolve_by_label()` 返回**候选集合**，由第 2 步逐个探测后用证据收敛。
2. **kubeconfig 的内部 name 字段不可信**。实测 `openmerlin-guiyang-005` 那个文件的
   `clusters[].name` 与 `current-context` 都写成 `gy-003`（复制粘贴缺陷），但 server 确实是 gy-005。
   故一律按**文件名**索引，不读 `current-context`；`name_mismatch` 只作告警，不影响判定。
3. **集群身份必须自检**。`self_check_plan()` 用「集群本地 CPU scale-set 标签」这类唯一属于该集群的
   标识做交叉验证；某个集群两个标识都拿不到时，报告明确写「无法自检」，而不是假装验证通过。

另外两条环境事实：
- `~/.kube/config` 的内容是字面量 `KUBECONFIG-DATA-FAKE`，必须显式传 `KUBECONFIG=<路径>`；
- `ascend-backend` 那个 kubeconfig 是**昇腾社区微服务集群**，不是 CI 资源集群，**不可用于 CI 取证**。

### 3.3 标签的两种形式：展示名 vs 全名

`Cluster.md`（源 = ArgoCD Application，自动生成）里同一个 machine 有两套名字：

```html
<div class="machine" data-label="linux-aarch64-a3-800t-0-cn12-001" data-npu="ascend-1980">
  <span class="machine-label">linux-aarch64-a3-800t-0</span>
```

job 在 GitHub 上报的是**展示名**（`linux-aarch64-a3-800t-0`），而 pod 名与 `data-label` 用的是
**带集群后缀的全名**（`…-cn12-001`）。不做这个映射，真实 job 会被判成「Cluster.md 未登记该标签」而
完全跳过集群取证 —— 这是一个纯属工具自己造成的假阴性。故 `parse_cluster_map()` 一次抓三元组，
`full_labels_for()` 负责翻译，查 pod 一律用翻译后的全名。

### 3.4 pod 定位：只接受「可能有本 job」的候选

`find_job_pod()` 按证据强度降序尝试，命中方式会记进 `match_kind` 并写进报告：

| 档 | 判据 | 身份是否确凿 |
|---|---|---|
| 1 | pod 名精确等于 job 上报的 `runner_name` | 确凿（后缀带 5 位随机段，不会复用） |
| 2 | `runner_name` + `-workflow` 伴生 pod | 确凿 |
| 3 | 以 `runner_name` 为前缀（后缀段可能被截断） | 确凿 |
| 4 | 按 pod 名文法拆出 runner 标签，与 job 标签求交 | **推定**（靠标签猜的） |

第 4 档证据标准是「有正面佐证」而非「没被排除」，要连过三关：

1. **真的启动过**：`phase=Pending`（还没调度上）或没有任何 `containerStatuses`（容器从未创建）→ 剔除。
   从未启动的 pod 不可能承载一个已结束的 job，与时间无关。
2. **存在起点早于失败步骤**：承载该步骤的必要条件是 pod 先于该步骤存在。起点取
   `status.startTime`，缺失时退到 `metadata.creationTimestamp`（Pending pod 没有 startTime，
   而 creationTimestamp 由 API server 写入，永远存在）。
3. **起点判不出来也不收**：缺两种时间戳时是「不知道」，不等于「可以采信」。

第 1、2 关都是实测踩出来的：

- **踩坑 A（复用 pod）**：runner pod 会被复用，同一 scale-set 的 pod 连续接多个 job。实测某 job 的失败
  步骤在 `09:37:19~09:37:21`，而按标签+时间窗「收敛」到的 pod 启动于 `09:38:53`，日志里正在续租的是
  **另一个** job。曾用「pod 启动不晚于 job 结束」+120s 余量做判据 —— 方向就是错的：job 结束后新建的
  pod 照样满足它，判别力为零，92s 的间隔就是这么溜过去的。正确方向是「早于失败步骤开始」，余量压到 60s。
- **踩坑 B（未调度的 pod）**：实测两例 12 分钟前失败的 job 匹配上了**刚刚才创建**的 Pending pod，
  其 `PodScheduled=False Unschedulable`（PVC 未绑定）读起来还挺像失败原因。只看 `startTime` 会让
  「时间未知」被当成「可能就是它」。独立复核证实：该标签下现存 pod 最早创建于 `09:45:52`，
  而目标 job 的失败步骤在 `09:36:02` —— 现存 pod 确实全都不是本 job 现场。

第 1、2 档不做这种剔除：pod 名精确一致时身份是确凿的，时间戳对不上只说明**时间戳本身**可疑，
不足以否定身份。此时降级为「⚠️ 时间戳存疑」提示并仍按精确匹配采信，不能反过来把最硬的证据丢掉。

**报告措辞**分开三种否定结论，它们的含义完全不同：

- 找到的不是本 job 的（`同标签有 N 个候选 pod，但全部不构成本 job 的现场证据（X 个起点晚于…、Y 个尚未启动的）`）
  → 集群里该 runner 确实在滚动，只是本次现场已被回收；
- 没法判定的（`均无法判定是否承载过本 job（缺可信的时间判据），未取证`）；
- 压根没找到的（`pod 已回收（历史失败的常态）或未在本集群找到`）。

### 3.5 取到的证据与权限天花板

能取到：pod phase / node / QoS / 创建与启动时间、容器状态（`state`/`lastState.terminated`
的 reason 与 exitCode）、重启次数、init 容器失败、非 True 的 condition、容器日志（含**重启前实例**的日志，
`previous=True`）、标签可用性核查。

取不到（SA 权限所致，报告里作为已知局限写明）：`nodes`/`events`/`namespaces` 全 Forbidden，
故**没有调度事件**（`FailedScheduling` 的具体原因）、没有节点 condition 与 taint，
`kubectl describe` 的 Events 段会 403。

`lastState.terminated.reason` 是集群侧最有价值的一格：它能区分三件在日志里长得一样的事 ——
`OOMKilled`（内存）/ `137 + Error`（可能被 drain 杀）/ `Completed`（正常退出但 job 判失败）。

### 3.6 降级路径

pod 已回收是历史失败的常态。此时不做假装取证，而是降级为**标签可用性核查**：逐个候选集群查
该标签下现存的 runner / listener 数量。这是**查询时刻的快照**，查到「无 runner」只能说明此刻没部署，
**不能**证明失败当时 runner 掉线，因此它不计入 owner 票，只作提示并强制人工确认。

## 4. 第 3 步：案例选取

优先级：① 日志侧明确要求集群取证（`cluster_todo`）→ ② 桶知识表标了 `probe`（该桶结论需集群侧验证）
→ ③ 其余。跳过假失败。

同一优先级内**按桶轮流取**（round-robin），而不是直接取前 N 个：实测 `--max-cases 4` 取到的
4 个 job 全是同一个桶（都是「Wait for pods ready」），报告看起来做了 4 份取证，实际只有 1 份信息。

## 5. 第 4 步：历史归因

知识库 = `ascend-gha-runners/docs` 的 issue（含评论；根因常在评论里，故 `root_cause_source` 会标明
取自正文还是评论）。

### 5.1 匹配：签名 + 关键词，IDF 加权

- 签名（`extract_signatures()` 统一产出，如 `errorcode:507035`、`exitcode:137`）权重 3.0 × IDF；
- 关键词（含 CJK 2-字滑窗 bigram）权重 0.4 × IDF；
- 签名与关键词都做归一化，避免同一现象的多种写法互相错过。

### 5.2 三个反「看起来很有道理」的校准

1. **出处 ≠ 机制**。实测 `schedule_nightly_test_a2` 只出现 1 次（按 IDF 是「稀有词」），但它只是
   **工作流名**：命中它只说明「同一个 workflow 里的另一次失败」，不代表同现象。故 workflow / job /
   step 名统一作为 `provenance` 传给匹配器排除。
2. **异常类名不是机制证据**。`valueerror` 在本库 `df=3`，按 IDF 算「稀有」，可它只是 Python 异常
   **类名**。实测 #190/#254/#101 都靠它被标成「证据强度=强」，而它们与缓存未命中毫无关系。
   故 `GENERIC_SIGNATURES` 把异常类名（以及 `ERR99999` —— 本工具知识表已明确它是昇腾对任意未捕获
   应用层异常的通用包装，不是硬件信号）排除在机制证据之外。
3. **中文切词**：STOPWORDS 收进了 2 字虚词（我们/这个/可以/没法/…），否则滑窗产生的虚词 bigram
   会稀释 IDF，让真正的症状词失去区分度。

### 5.3 先例机制

`match()` 除了取 top_k，还**保证给复盘留槽位**（`precedent_k`）：实测 top_k=5 时 5 个名额全被
非复盘 issue 占满，导致「先例」机制静默失效 —— 命中了却永远没先例。

先例不是「高分」就算，而要看证据类型：

| 证据强度 | 含义 | 报告措辞 |
|---|---|---|
| 强 | 命中带机制的签名（如 `exitcode:137`） | 「与历史先例 #N 高度吻合」 |
| 中 | 只命中核心症状词（稀有且非出处名） | 「与历史先例 #N 主题相近（可参考，但机制未必相同）」 |
| 弱 | 只命中通用词/出处名 | 不采信为先例，列为「分数不低的线索」提示人工核对 |

### 5.4 策展关联：词面匹配连不上的同一现象

本工具说「模型缓存未命中」，历史 #238 说「找不到缓存模型」，两者无稀有词重叠（#238 只得 11 分、
排 44 名）。这类**已知同现象**靠 `knowledge_tables.py` 里按桶登记的人工策展 `related_issues` 兜住，
每条都写明「为何关联」。策展表是有限的、需要人维护，报告里凡出自策展的条目都会标明，不冒充自动发现。
（这条也是本工具最大的固有局限，已写进报告的「已知局限」。）

## 6. 第 5 步：合成与输出

### 6.1 三层证据并列

日志侧（桶 + owner）、集群侧（pod 实证 / 标签可用性）、历史侧（先例 + 策展）三层各自陈述，
**冲突显式标出**而不是取其一：

- 集群侧实证与日志侧 owner 不一致 → 记冲突；
- 日志侧判 `code`，而先例/策展的根因提到平台侧动作 → 记冲突并提示「错误信号所在层 ≠ 责任方所在层」，
  但**不自动改判**；
- 集群侧「查无 runner」这类快照级负向结论 + 历史先例 → 记「需人工确认」的提示项，不记冲突。

### 6.2 置信度阶梯

| 条件 | 置信度 |
|---|---|
| 三层证据冲突 | 低（需人工裁定） |
| 集群侧实证 + 与日志侧一致 | 高 |
| 命中同签名先例（强），缺集群侧实证 | 中高 |
| 有同主题先例（中，机制未必相同） | 中 + 需人工 |
| 集群侧已定位 pod 但状态无异常 | 中 + 需人工 |
| 仅日志侧正则定性 | 中 + 需人工 |
| 未分类 | 低 |

**只治理了否定结论、没有任何正向证据，不能提升置信度**。有「需人工确认」提示项时，`needs_human`
强制为真。

### 6.3 输出

- Markdown 报告（人读）：逐案列根因 / owner / 置信度 / 依据 / 冲突 / 修复建议 / 先例；
  另附集群取证可用性表、集群现场快照、已知局限、执行期间的错误。
- 结构化 JSON（机读）：与报告同源，便于后续接看板或回归比对。
- 权限受限、拿不到的东西一律写「未取证」，不用推测填充。

## 7. 已知局限（摘录，完整清单随报告输出）

- **官方分类树不含正文**：`problem-tree.json` 的 19 个叶子节点 text 为空，只能对齐分类口径，
  不能提供根因描述 —— 建议内容来自桶知识表 + 历史 issue 先例。
- **靠标签定位的 pod 是「推定」而非「确证」**：参见 §3.4。同一窗口内该 scale-set 若并发跑过多个
  job，仍需用容器日志里的 job 号二次确认。
- **标签可用性核查是快照**：见 §3.6。
- **换说法的同一现象，词面匹配连不上**：见 §5.4。
- **分数高 ≠ 同现象**：实测 #228（AOP bisect 超时）仅凭 `schedule_nightly_test_a2` 这个工作流名
  就拿到 65 分。故对每条匹配都标证据强度，且只让「强/中」充当先例。
- **不自动裁定责任方**：历史先例只作线索，冲突交人工。

## 8. 交付面与验证方式

- 第 1 步的交接面是 `--emit-json` 产物（`failed_jobs` / `classifications` / `cluster_todo`），
  `--handoff` 复用它，因此本工具可以在不重跑 GitHub 分析的情况下被反复验证。
- 缓存：`.forensics_cache/{Cluster.md, issues.json, comments.json, analysis_handoff.json}`
  （Cluster.md 默认 24h 过期；取不到网络时宁可用陈旧缓存，也不让集群归属环节整体失效）。
- 回归样例：`/tmp/test_handoff.json`（3 个合成案例）用于端到端冒烟，覆盖「有先例无实证」
  「同签名先例」「三层冲突 + 策展关联」三种梯度。
- 实测（2026-09-24，近 7 天、a2+a3）：70 个失败 job → 40 条分类 → 8 条待集群取证 → 8 个案例，
  集群侧 0 个 pod 实证（现场均已回收，逐个给出了否定理由）、2 个案例存在证据冲突。
