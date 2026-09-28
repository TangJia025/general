# 部署：上游 CI 失败监听器（`npu-ci-watch`）

把 [npu_ci_watch.py](../npu_ci_watch.py) 装成本机 **systemd user 服务**，常驻监听
`vllm-project/vllm-ascend` 的 nightly / weekly a2·a3 测试 workflow，一失败就抢集群快照、
job 结束后补日志分类与报告。设计理由（两阶段为何不可合并）见
[npu_ci_forensics_design.md](../npu_ci_forensics_design.md) 的「监听器」章节。

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
| `watch.log` | 轮询与动作日志（同 journalctl，但独立留存） | 仅丢日志 |
| `analysis_logs/` | 每次调流水线的 stdout/stderr | 出问题时少一条排查线索 |

只想「重新分析某个 job」：把 `ledger.json` 里那条记录删掉（或改 state 为 `seen`），
**别删 snapshots**，否则集群侧证据就永久丢了。

## 6. 排障

| 现象 | 原因 | 动作 |
|---|---|---|
| 台账里满屏 `snapshot_missed`，`snapshot_note` 说 pod 已回收 | 多半是 `kubectl` 不在服务的 PATH 里（`未找到 kubectl 可执行文件`）而不是真回收 | 看 `journalctl --user -u npu-ci-watch \| grep kubectl`；确认 unit 里的 `Environment=PATH=` 含 `%h/.local/bin` |
| journal 里连续 `gh` 报错、进程仍在跑 | gh 凭据过期或内网不通 | `gh auth status`；修好后**无需重启**，下一轮会自愈（退避上限 10 分钟） |
| 服务在登出后消失 | 没开 linger | `loginctl enable-linger $USER` |
| `journalctl` 看不到输出 | 只在手动跑时正常 | 确认 unit 里有 `PYTHONUNBUFFERED=1` |
| 报告里「集群侧取得 pod 实证 0/N」 | 正常：历史失败的 pod 早已回收，降级为标签可用性核查 | 无需处理；只有 `snapshot_ok` 的快照才能给出 pod 级实证 |
| 某个 job 反复失败后变 `gave_up` | 连续 3 次（`--max-attempts`）分析出错，已留痕不再重试 | 看该记录的 `last_error` 字段，修好后把 state 改回 `seen` |

## 7. 卸载

```bash
systemctl --user disable --now npu-ci-watch
rm ~/.config/systemd/user/npu-ci-watch.service
systemctl --user daemon-reload
```

`~/.forensics_state` 不会被自动清理（里面的快照可能还有用），按需自行删除。
