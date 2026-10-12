# 部署：上游 CI 失败监听器（`npu-ci-watch`）

把 [npu_ci_watch.py](../npu_ci_watch.py) 装成本机 **systemd user 服务**，常驻监听
`vllm-project/vllm-ascend` 的 nightly / weekly a2·a3 测试 workflow，一失败就抢集群快照、
job 结束后补日志分类与报告。设计理由（两阶段为何不可合并）见
[npu_ci_forensics_design.md](../npu_ci_forensics_design.md) 的「监听器」章节。

监听范围 = **workflow 文件路径**（`schedule_(nightly|weekly)_test_a[23]`）+ **触发事件**
（`schedule`、`workflow_dispatch`）双限定。注意上游这几个文件只声明了 `workflow_dispatch`
（nightly 由上游调度 workflow 拉起），所以**不要**把事件进一步收成「仅 schedule」——
实测那会让 7 个在跑的 run 全被排除，而且不报错。另：从未开始且创建超 24h 的 run 判为僵尸，
不再计入活动、也不再每轮查它的 jobs（日志里出现 `🧟` 即属此列，每个 run 只报一次）。

## 1. 前置条件

| 依赖 | 要求 | 自查 |
|---|---|---|
| `gh` | 已登录，能读 `vllm-project/vllm-ascend` 的 Actions | `gh api repos/vllm-project/vllm-ascend/actions/runs --jq '.total_count'` |
| `kubectl` | **在 PATH 里**（本机实测在 `~/.local/bin/kubectl`） | `which kubectl` |
| kubeconfig | `~/kconf/asci/*.yaml`（默认目录，可 `--kubeconfig-dir` 改） | `ls ~/kconf/asci \| wc -l` |
| 网络 | 集群 API 与 GitHub 都要通 | `gh api rate_limit --jq .rate.remaining` |
| python3 | ≥ 3.9（用到 `dict \| None` 之类的新语法） | `python3 -V` |

> `/home/tangjia/.kube/config` 是 **FAKE 占位符**，不要用它测连通性。监听器不读 `KUBECONFIG`
> 环境变量，一律用 `--kubeconfig` 显式指定解析出来的文件。

## 2. 安装

```bash
cd ~/work/general/昇腾失败原因分析
mkdir -p ~/.config/systemd/user
cp deploy/npu-ci-watch.service ~/.config/systemd/user/
systemctl --user daemon-reload
```

**先别急着 enable**，按第 3 节验证一遍再启动 —— 监听器一旦跑起来就会往 `~/.forensics_state`
写台账，若配置有错（例如 `kubectl` 不在 PATH），你会得到一摞「理由还是错的」的失败记录。

## 3. 首次验证（三步，逐级放开）

```bash
# ① 只打印将要做什么：不写台账、不写快照、不调流水线
python3 npu_ci_watch.py --once --dry-run

# ② 真跑一轮（含 15 分钟内的历史失败），确认台账与报告都正常
python3 npu_ci_watch.py --once
python3 -c "import json;d=json.load(open('.forensics_state/ledger.json'));\
print(json.dumps({k:v['state'] for k,v in d['jobs'].items()},ensure_ascii=False,indent=1))"
ls -t npu_ci_reports/ | head -3

# ③ 常驻
systemctl --user enable --now npu-ci-watch
loginctl enable-linger $USER      # 关键：否则 SSH 登出后服务会被停掉
systemctl --user status npu-ci-watch
journalctl --user -u npu-ci-watch -f
```

`loginctl enable-linger` 是唯一容易漏的一步：不开启的话，服务只在你登录期间存在，
而这东西的价值恰恰在于你不在的时候它也在看。

## 4. 日常运维

```bash
systemctl --user status npu-ci-watch        # 是否在跑、最近日志尾
journalctl --user -u npu-ci-watch -f        # 实时日志（已设 PYTHONUNBUFFERED=1）
systemctl --user restart npu-ci-watch       # 改完参数后重启
systemctl --user stop npu-ci-watch          # 停止（主循环在本轮开头检查停止标志，会干净收尾）
systemctl --user disable --now npu-ci-watch # 停并取消开机自启
```

改参数：编辑 `~/.config/systemd/user/npu-ci-watch.service` 的 `ExecStart`（例如把
`--idle-interval 300` 改成 `600` 以进一步省调用），然后 `daemon-reload` + `restart`。
临时补一段窗口用 `python3 npu_ci_watch.py --lookback 2h --once`，不必改服务。

## 5. 运行态文件（`--state-dir`，默认 `.forensics_state/`，已 gitignore）

| 路径 | 内容 | 删掉的后果 |
|---|---|---|
| `ledger.json` | 台账：每个 job 的状态、尝试次数、报告路径 | **会重新分析已处理过的失败**（多花日志 API 调用，重复出报告）。快照字段没了也不会重抢 |
| `snapshots/job_<id>.json` | 失败时刻的集群快照（pod 状态 + 容器日志） | **不可重建**：pod 是一次性的，事后无法再取。这是最该备份的东西 |
| `handoffs/handoff_run_<id>.json` | 第 1 步的结构化交接面 | 阶段 B 会重跑第 1 步（再花一次日志下载） |
| `scans/job_<id>.txt.gz` | 失败步骤时间窗的日志正文（LLM 判决的输入；按 `--scan-retention-days` 保留 14 天） | 保留期内该 job 的 LLM 判决做不了，退回规则结论 |
| `llm_verdicts/job_<id>.json` | LLM 判决缓存（键 = job_id + prompt_version + scan_sha256 + model） | 只是重判一次（要花钱），无正确性问题 |
| `watch.log` | 轮询与动作日志（同 journalctl，但独立留存） | 仅丢日志 |
| `analysis_logs/` | 每次调流水线的 stdout/stderr | 出问题时少一条排查线索 |

只想「重新分析某个 job」：把 `ledger.json` 里那条记录删掉（或改 state 为 `seen`），
**别删 snapshots**，否则集群侧证据就永久丢了。

## 5.1 LLM 判决层（可选，`LLM_ENABLED=1`）

把规则层「未能定性」的 case 交给模型判根因，规则结论并列保留在报告里
（设计口径与实测数据见 `npu_ci_failure_design.md` §15）。**默认关闭**：不设 `LLM_ENABLED`
时，输出与不装这一层时逐字节相同。

开启步骤：

```bash
mkdir -p ~/.config/npu-ci-watch && chmod 700 ~/.config/npu-ci-watch
cat > ~/.config/npu-ci-watch/deepseek.env <<'EOF'
DEEPSEEK_API_KEY=sk-...
# 选填：
#DEEPSEEK_BASE_URL=
#LLM_MODEL=deepseek-flash
EOF
chmod 600 ~/.config/npu-ci-watch/deepseek.env
# unit 里已有 EnvironmentFile=-%h/.config/npu-ci-watch/deepseek.env 与 Environment=LLM_ENABLED=1
systemctl --user daemon-reload && systemctl --user restart npu-ci-watch
```

那个文件是 **systemd 的 EnvironmentFile，不是 shell 脚本**：每行一条 `变量=值`，
不支持 `export`、不支持引号转义、值里**不要加引号**。**它不入任何 Git 仓库。**

| 现象 | 原因 | 动作 |
|---|---|---|
| 日志里 `--llm-judge 已打开，但 DEEPSEEK_API_KEY 未设置`，台账里 job 连续 `error` 最终变 `gave_up` | 凭据没读到 | 这是**故意**的硬失败（逐案静默降级会跑完一轮、花了钱、报告上还看不出来）。⚠️ 代价要知道：阶段 B 每失败一次，本批 job 就 `record_error` 一次，**3 次（`--max-attempts`）后变 `gave_up`（终态，不再重试）**，得手工把 `ledger.json` 里那些记录的 state 改回 `seen`。所以填好凭据后先看一行 `journalctl --user -u npu-ci-watch \| grep 第5步`，确认有 `[第5步] LLM 判决已启用：…` 再放心跑 |
| 报告里出现 `规则判断：…（以上为 LLM 判决，此行仅作对照）` | 正常：模型判了，规则结论并列保留 | 无需处理。**注意这一层的判决质量目前没有真值核验**（§15.5），当线索读 |
| 报告里全是 `LLM 判决不可用（原因：…）已退回规则判决` | 限流/超时/网络 | 看 `journalctl --user -u npu-ci-watch \| grep 第5步` 的那行汇总（`N 成功 / M 降级（原因分布）`）。降级**不会被缓存**，下轮会重判 |
| 想临时关掉判决 | — | `Environment=LLM_ENABLED=0`（或注释掉）后 `daemon-reload && restart`；不用删凭据 |
| 想强制重判某个 job | — | 删 `llm_verdicts/job_<id>.json`；改了提示词（`PROMPT_VERSION`）则会**全部**自动作废重判 |

## 6. 排障

| 现象 | 原因 | 动作 |
|---|---|---|
| 台账里满屏 `snapshot_missed`，`snapshot_note` 说 pod 已回收 | 多半是 `kubectl` 不在服务的 PATH 里（`未找到 kubectl 可执行文件`）而不是真回收 | 看 `journalctl --user -u npu-ci-watch \| grep kubectl`；确认 unit 里的 `Environment=PATH=` 含 `%h/.local/bin` |
| journal 里连续 `gh` 报错、进程仍在跑 | gh 凭据过期或内网不通 | `gh auth status`；修好后**无需重启**，下一轮会自愈（退避上限 10 分钟） |
| 服务在登出后消失 | 没开 linger | `loginctl enable-linger $USER` |
| `journalctl` 看不到输出 | 只在手动跑时正常 | 确认 unit 里有 `PYTHONUNBUFFERED=1` |
| 报告里「集群侧取得 pod 实证 0/N」 | 正常：历史失败的 pod 早已回收，降级为标签可用性核查 | 无需处理；只有 `snapshot_ok` 的快照才能给出 pod 级实证 |
| 某个 job 反复失败后变 `gave_up` | 连续 3 次（`--max-attempts`）分析出错，已留痕不再重试 | 看该记录的 `last_error` 字段，修好后把 state 改回 `seen` |
| 日志首次出现 `🧟 run … 判为僵尸` | 正常：某个 run 创建超 24h 仍从未开始，已跳过（不计活动、不查 jobs） | 无需处理；每个 run_id 只报一次，游标在 `ledger.json` 的 `cursors.stale_runs` |
| 「落在监听范围内」突然变 0 且不报错 | 检查上游 trigger：若文件改成 `schedule` 之外的新事件（如 `push`），会被事件白名单挡掉 | 看 `forensics/watch_state.py` 的 `IN_SCOPE_EVENTS`，按需增补并补断言 |

## 7. 卸载

```bash
systemctl --user disable --now npu-ci-watch
rm ~/.config/systemd/user/npu-ci-watch.service
systemctl --user daemon-reload
```

`~/.forensics_state` 不会被自动清理（里面的快照可能还有用），按需自行删除。
