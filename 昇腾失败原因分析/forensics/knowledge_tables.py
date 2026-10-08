"""桶 → 官方分类叶子 → 修复建议：第 5 步输出的知识真值表。

为什么用旁表而不是给 BUCKETS 加第 4 个字段：
  npu_ci_failure_analysis.py 的 BUCKETS 是 (正则, 标签, owner) 三元组，且**顺序本身是校准结果**
  （「首个匹配生效」，例如 HCCL 必须排在 ACL 之前、依赖解析必须排在 Python 运行时错误之前）。
  在匹配循环里加字段会诱使人调整顺序，从而破坏来之不易的判定精度。
  故此处按**桶标签**索引，与顺序解耦；标签是按正则命中的产物，天然稳定。

leaf 字段对齐 ascend-gha-runners/docs 的官方决策树（docs/assets/problem-tree.json，19 个叶子）。
注意：该树的叶子节点 **text 为空**，只有 title，所以它只能提供「官方分类名」用于对齐口径，
不能提供根因描述或修复建议——建议内容来自本表 + 历史 issue 知识库。
"""
from __future__ import annotations

# 官方决策树叶子 id → 官方分类名（用于报告里标注「官方口径」，避免与平台方各说各话）
OFFICIAL_LEAF_TITLES = {
    "leaf_err_image_pull": "ErrImagePull（镜像拉取失败）",
    "leaf_invalid_image_name": "InvalidImageName（镜像名非法）",
    "leaf_create_container": "CreateContainerConfigError（容器创建失败）",
    "leaf_scheduling": "FailedScheduling（调度失败）",
    "leaf_binding": "FailedBinding（PVC 不存在）",
    "leaf_container_crash": "Container crash（容器崩溃）",
    "leaf_oom": "OOMKilled（内存不足）",
    "leaf_user_script": "UserScriptError（用户脚本报错）",
    "leaf_wait_resource": "等待资源（队列可能已满）",
    "leaf_wait_label": "runs-on 标签可能不存在",
    "leaf_runner_offline": "Runner 可能未上线",
    "leaf_wait_liqo_offload": "任务卡在虚拟节点（Liqo 接管异常）",
    "leaf_build": "构建 / 打包失败",
    "leaf_other": "其它问题",
    "leaf_hccl_port_bound": "HCCL 通信端口被占用",
    "leaf_incomplete_snapshot": "模型缓存缺失 / 不完整",
    "leaf_running_hang": "任务长时间卡住 / 超时（引擎进程挂起）",
    "leaf_upload_log_failed": "上传日志 / 产物失败（需加网络白名单）",
    "leaf_infra_no_scale_down": "弹性节点不缩容",
}

# 桶标签 → {leaf: 官方叶子 id, owner: 责任方, action: [修复建议（有序）], probe: 集群侧可验证手段或 None}
#
# action 的写法约定：**先写「怎么确认」，再写「怎么修」**。
# 因为本工具的价值在于把「谁的责任」说清楚，而多数桶的正则只能证明「症状在谁那儿」，
# 不等于「责任在谁那儿」（见 memory: 错误信号所在层 ≠ 责任方所在层）。
BUCKET_KNOWLEDGE = {
    "假失败(draft PR 阻断)": {
        "leaf": None, "owner": "假失败",
        "action": ["非真实失败：draft PR 触发的阻断检查，不计入根因分布。",
                   "核对失败 job 所属 PR 是否仍为 draft；是则忽略。"],
        "probe": None,
    },
    "多节点pod调度/就绪失败(k8s侧)": {
        "leaf": "leaf_scheduling", "owner": "infra",
        "action": ["集群侧确认：pod 是否 Pending、事件里的原因（Insufficient / 节点选择器不匹配 / 污点未容忍）。",
                   "若为资源不足：确认该 runner 标签对应的弹性节点组是否已扩容、是否触顶配额。",
                   "若为节点选择器/污点：多为部署配置写死 nodeName 或漏配 toleration，属平台侧配置缺陷。",
                   "若 pod 根本不 Pending（早已 Running 后被驱逐）：转查节点驱逐事件（TaintManagerEviction 类）。"],
        "probe": "pod_scheduling",
        "related_issues": [
            {"number": 263, "why": "同现象：弹性新节点注册 1 秒即接 workflow pod，"
                                   "FailedMount 后被 TaintManagerEviction 秒删；"
                                   "与「Waiting for pods ready」失败的机制一致"},
            {"number": 255, "why": "同现象：runner pod 写死 nodeName 绕过调度器，"
                                   "pod 直接 Failed 终态，表现为等待就绪失败"},
        ],
    },
    # 以下两桶原为一个合并桶「分布式通信/网络(HCCL/Store)」，按机制拆开（见 npu_ci_failure_analysis.py 的
    # BUCKETS 注释）：合并时两桶共用 leaf_hccl_port_bound，导致每次 Store 会合超时都被写成
    # 「官方口径对齐：HCCL 通信端口被占用」。
    "HCCL 集合通信失败": {
        "leaf": "leaf_hccl_port_bound", "owner": "infra",
        "action": ["先区分「端口被占用」与「网络不通」：报错含 bind/address already in use 属前者。",
                   "端口被占用：同节点上批任务共用固定 HCCL 端口，属平台侧隔离不足；确认是否有并发任务共用节点。",
                   "若只有 Connection reset / broken pipe：查节点间网络与集合通信超时配置"
                   "（`HCCL_EXEC_TIMEOUT` / `HCCL_CONNECT_TIMEOUT` 是否与该用例规模匹配）。",
                   "反例警惕：error code 507035 曾被误判为平台硬件问题，实为业务方算子问题——"
                   "有 507xxx 不等于平台责任，务必先落 507 具体码值再定性。"],
        "probe": "pod_node",
    },
    # 官方 19 个叶子里**没有**「会合超时」这一类，故 leaf 留空：硬套 leaf_running_hang
    # （任务长时间卡住／引擎进程挂起）会把「对端 rank 根本没加入」误述成「引擎挂了」——
    # 机制相反（前者是进程没起来，后者是起来了卡住）。宁可不对齐官方口径，也不再生成一句错的。
    "Store 会合超时(TCPStore，对端 rank 未加入)": {
        "leaf": None, "owner": "infra",
        "action": ["先算差额：`Timed out after N seconds waiting for clients. X/Y clients joined.` 里"
                   "Y-X 就是没加入的 rank 数；N 是会合超时阈值（实测 1801s ≈ 配置的 1800s）。",
                   "再定位缺席的 rank 在**哪台机器**：多节点 job 里每个 rank 属于哪个 node 由拓扑决定"
                   "（实测 DP 场景：node0 跑 DP0–DP3、node1 跑 DP4–DP7）。"
                   "⚠️ job log 只覆盖 node0，对端节点的日志在 `<分支>-<yaml stem>-ascend-logs` 产物里"
                   "（`collected-logs/node1/var/log/*_logs.txt`），必须取产物才能看到缺席方那一侧。",
                   "客户端侧（对端节点）的典型形态是 `DistNetworkError: Failed to recv, got 0 bytes."
                   " Connection was likely closed.` —— 这是**结果**不是原因：服务端等满超时先退出，"
                   "客户端再去连就只连到已关闭的连接。不要把连接被拒读成网络故障。",
                   "最后查那台节点的**启动延迟**：pod 调度慢、镜像拉取慢、上一轮任务未释放资源，"
                   "都会让对端 rank 迟到而错过会合窗口；属平台侧资源调度问题。",
                   "修法方向：平台侧缩短对端节点的调度/启动时间，或在用例侧提高会合超时阈值"
                   "（后者只是掩盖，不能代替查延迟）。"],
        "probe": "pod_node",
    },
    "模型缓存未命中(离线模式 local_files_only)": {
        "leaf": "leaf_incomplete_snapshot", "owner": "code",
        "action": ["确认是「业务方自己设了 local_files_only=True 且缓存为空」，还是「平台缓存清理把已下载模型删了」。",
                   "前者：改 workflow / 测试用例，去掉 local_files_only 或先预热缓存，责任在业务方。",
                   "后者：平台侧清理脚本（按 atime 判定冷数据）有缺陷，会把仍在用的模型删掉，责任在平台。",
                   "⚠️ 该桶当前默认判 code，但历史上（issue #238「找不到缓存模型」）真因是平台清理脚本缺陷——"
                   "落库前需人工确认是哪一种，本桶的正则只能证明症状。"],
        "probe": None,
        # 策展关联：词面匹配**永远**连不上这两条——本桶说「模型缓存未命中」，
        # #238 说「找不到缓存模型」，换了说法、无稀有词重叠（#238 只有 11 分，排在 44 名）。
        # 这类已知同现象必须人工登记，不能让词面匹配去「碰运气」。
        "related_issues": [
            {"number": 238, "why": "同现象：A2 夜间任务找不到缓存模型 DeepSeek-V2。"
                                   "真因是平台定时老化脚本按「超过 90 天未使用」把模型标记为待删除，"
                                   "属平台侧缺陷而非业务方配置——与本桶默认判 code 相反，必须人工裁定"},
        ],
    },
    "昇腾NPU硬件错误(507xxx/ERR99999+设备)": {
        "leaf": None, "owner": "infra",
        "action": ["记录完整错误码与绑定的 Device/RankID（Device 非 -1 才是真绑定设备）。",
                   "在对应集群核对报错节点：npu-smi 信息、是否有器件降频/掉卡记录。",
                   "确认是否同节点反复出现同一码值；是则报硬件维修，并临时屏蔽该节点。"],
        "probe": "pod_node",
    },
    "CANN运行时参数非法(107xxx)": {
        "leaf": None, "owner": "mixed",
        "action": ["107xxx 多为算子入参/形状不合法，先定位是业务方调用参数还是框架侧校验。",
                   "核对 CANN 版本与报错算子的支持范围（版本不匹配常表现为参数非法）。",
                   "需业务方与平台方共同确认：错误信号在 CANN 层，但错值可能来自业务侧输入。"],
        "probe": None,
    },
    "昇腾算子执行错误(ACL)": {
        "leaf": None, "owner": "mixed",
        "action": ["确认 ACL 报错是否由业务方自定义算子触发（csrc/ 下有变更时优先怀疑）。",
                   "核对算子 so 是否为当前 CANN 版本重新编译（版本错配是高发原因）。",
                   "反例警惕：ACL 层报错不等于平台责任，需回溯到具体算子再定责。"],
        "probe": None,
    },
    "依赖解析/构建失败(含链式噪音)": {
        "leaf": "leaf_build", "owner": "mixed",
        "action": ["看窗口**头部**（依赖解析的真错误在最前面），忽略其后链式噪音。",
                   "若为内网源不可达：属平台侧镜像/仓库可用性问题。",
                   "若为版本约束冲突（No matching distribution）：属业务方依赖声明问题。",
                   "若为 pip 超时偶发：重跑一次以区分偶发与稳定失败。"],
        "probe": None,
    },
    "编译失败(C++/MLIR)": {
        "leaf": "leaf_build", "owner": "code",
        "action": ["按报错文件/行号定位业务方代码变更。",
                   "MLIR 相关失败常与 CANN 版本相关，需核对 CI 镜像内 CANN 版本与代码期望是否一致。"],
        "probe": None,
    },
    "自定义算子so缺失(csrc构建)": {
        "leaf": "leaf_build", "owner": "mixed",
        "action": ["确认 csrc 是否被正确编译并安装到运行路径（构建日志里找 .so 产物）。",
                   "CI 里若有 CSRC 变更触发的策略检查，核对是否被跳过导致未重编。"],
        "probe": None,
    },
    "进程被kill(OOM/超内存)": {
        "leaf": "leaf_oom", "owner": "mixed",
        "action": ["区分容器内存超限（cgroup OOM，exit 137）与宿主/节点内存不足。",
                   "集群侧确认 pod 的 lastState.terminated.reason 是否为 OOMKilled、exitCode 是否 137。",
                   "exit 137 也可能是外部 drain/驱逐（见 issue #257 cn12 workflow pod exit-137，"
                   "真因是 kubelet drain 超时而非内存）——必须看 terminated.reason 才能区分。"],
        "probe": "pod_container_state",
        "related_issues": [
            {"number": 257, "why": "同症状（exit 137）但真因不是内存：postStart 钩子 npu-smi "
                                   "泄漏 exec 管道 fd，导致 containerd drain 超时后 SIGKILL。"
                                   "是「137 不等于 OOM」的判例，必须看 lastState.terminated.reason"},
        ],
    },
    "分布式通信/编排(Ray)": {
        "leaf": None, "owner": "code",
        "action": ["Ray 报错多为业务方编排逻辑/资源声明问题（如 num_gpus 声明与实际不符）。",
                   "确认 head/worker 是否都成功启动；单 worker 启动失败会表现为整体超时。"],
        "probe": None,
    },
    "内网镜像/仓库下载失败": {
        "leaf": "leaf_err_image_pull", "owner": "infra",
        "action": ["确认是镜像拉取失败还是包下载失败，两者平台侧处置不同。",
                   "镜像拉取失败：集群侧确认 pod 容器状态 waiting.reason 是否为 ErrImagePull/ImagePullBackOff，"
                   "并核对镜像名与 tag 是否真实存在。",
                   "包下载失败：确认内网源（pypi/镜像站）当时是否可达、是否有白名单未放行。"],
        "probe": "pod_container_state",
    },
    "GitHub API 调用失败": {
        "leaf": "leaf_upload_log_failed", "owner": "infra",
        "action": ["确认是速率限制（rate limit）、网络出口问题，还是 runner 侧 token 失效。",
                   "集群侧确认 runner pod 到 api.github.com 的出网是否正常（需白名单）。"],
        "probe": None,
    },
    "模型/包下载失败(外网)": {
        "leaf": "leaf_incomplete_snapshot", "owner": "mixed",
        "action": ["确认是外网不可达（平台出网策略）还是目标地址本身失效（业务方写错）。",
                   "历史案例：模型缓存被平台清理脚本按 atime 误删，表现为「找不到缓存模型」，责任在平台。",
                   "建议业务方改用内网镜像源，减少对外网依赖。"],
        "probe": None,
        "related_issues": [
            {"number": 238, "why": "同现象：模型「找不到」的真因是平台老化脚本按 90 天未使用标记待删除，"
                                   "而非外网不可达——两个桶都会出现这个症状，别只往出网策略上想"},
        ],
    },
    "超时": {
        "leaf": "leaf_running_hang", "owner": "mixed",
        "action": ["区分「任务真卡住」与「任务正常但超阈值」：看日志停更位置。",
                   "集群侧确认 pod 是否仍在 Running、CPU/内存是否有活动。",
                   "若为引擎进程挂起：常见于 NPU 通信挂起，需采集 py-spy 栈（见 issue #187 定位手段）。",
                   "**注意与「HCCL 集合通信失败」「Store 会合超时(TCPStore，对端 rank 未加入)」互斥**："
                   "前者的端口占用会表现为通信卡死，后者是进程没凑齐就等满超时。"
                   "该两桶排在【超时】之前，故真属那两类的日志不会落到本桶——落到本桶的才是「无更具体判据的超时」。"],
        "probe": "pod_container_state",
        "related_issues": [
            {"number": 187, "why": "同现象：任务长时间卡死。该 issue 给出了定位手段"
                                   "（py-spy 采栈确认引擎进程挂起），可直接复用"},
        ],
    },
    "OOM/显存不足": {
        "leaf": "leaf_oom", "owner": "mixed",
        "action": ["区分宿主内存 OOM 与 NPU 显存 OOM，两者修复方向完全不同。",
                   "显存不足：调小 batch/并行度，或核对模型规模与该 runner 卡数是否匹配。",
                   "宿主 OOM：集群侧确认 exitCode 137 + terminated.reason=OOMKilled。"],
        "probe": "pod_container_state",
    },
    "磁盘不足": {
        "leaf": None, "owner": "infra",
        "action": ["确认是容器可写层、挂载的 PVC，还是节点本地盘写满。",
                   "集群侧确认 pod 是否被驱逐（ephemeral-storage eviction）。",
                   "平台侧需清理节点残留（历史失败任务的大文件、镜像层）并加配额。"],
        "probe": "pod_container_state",
    },
    "依赖/安装(ImportError)": {
        "leaf": "leaf_user_script", "owner": "code",
        "action": ["按 traceback 定位缺失模块，确认是依赖未声明还是装错版本。",
                   "若为可选依赖（如某后端未编译）：确认 CI 是否应带该编译开关。"],
        "probe": None,
    },
    "断言失败(代码或精度)": {
        "leaf": "leaf_user_script", "owner": "code",
        "action": ["按断言所在的文件/行号定位业务方代码。",
                   "若为精度断言（数值对比超差）：确认是否 NPU 与 GPU 的数值差异预期，"
                   "必要时放宽阈值或核实是否为真实精度回归。",
                   "排除用例本身写死绝对路径/本地文件导致的伪断言失败。"],
        "probe": None,
    },
    "静态检查(pre-commit/ShellCheck)": {
        "leaf": "leaf_user_script", "owner": "code",
        "action": ["本地跑同样命令复现：pre-commit run --all-files。",
                   "多为格式/空白/换行问题，自动修复即可。"],
        "probe": None,
    },
    "静态类型检查失败(mypy)": {
        "leaf": "leaf_user_script", "owner": "code",
        "action": ["本地复现：mypy <报错文件>。",
                   "核对是否 CI 的 mypy 版本/配置与本地不一致。"],
        "probe": None,
    },
    "CI 策略检查(CSRC 变更)": {
        "leaf": "leaf_user_script", "owner": "code",
        "action": ["确认本次变更是否真的动了 csrc/；未动则可豁免该检查。",
                   "若动了 csrc：需按仓库要求同步更新对应策略文件。"],
        "probe": None,
    },
    "vLLM引擎崩溃(级联，真因在上游)": {
        "leaf": None, "owner": "unknown",
        "action": ["此为**级联**桶：引擎崩溃是被上游真因触发的症状，本身不是根因。",
                   "必须往前翻日志，找到第一个真实异常（常见：ImportError / 断言 / CANN 报错）。",
                   "若确实找不到前序异常，才考虑按引擎自身缺陷排查。"],
        "probe": None,
    },
    "Python运行时错误": {
        "leaf": "leaf_user_script", "owner": "code",
        "action": ["按 traceback 末行的异常类型与文件行号定位。",
                   "注意排除被上游真因触发的次生异常（日志里最早的那个才是）。"],
        "probe": None,
    },
    "测试参数缺失(config未传入)": {
        "leaf": "leaf_user_script", "owner": "code",
        "action": ["确认 CI 调起测试时是否传了 config（pytest 参数化缺 config_filename）。",
                   "属 workflow 定义问题，修 .github/workflows/ 里的调用方式。"],
        "probe": None,
    },
    "昇腾框架异常兜底(ERR99999，非硬件信号)": {
        "leaf": None, "owner": "unknown",
        "action": ["ERR99999 是昇腾对「任意未捕获应用层异常」的通用包装，**不是硬件信号**。",
                   "判别依据：Device:-1, RankID:-1 表示未绑定 NPU 设备，即应用层异常。",
                   "必须往前找真实异常；找不到时本工具不臆断责任方（故 owner=unknown）。"],
        "probe": None,
    },
    "步骤被强制终止(exit 255，非根因)": {
        "leaf": None, "owner": "infra",
        "action": ["exit 255 通常是 runner 侧执行包装器异常终止，属症状不是根因。",
                   "集群侧确认 runner pod 当时是否被驱逐/重启（看 lastState.terminated）。",
                   "历史案例：节点 drain 或 TaintManagerEviction 会中断正在执行的 step。"],
        "probe": "pod_container_state",
    },
    "脚本步骤通用包装失败(需按失败步骤细化)": {
        "leaf": "leaf_other", "owner": "unknown",
        "action": ["日志未能定位根因，需人工按失败步骤名细化排查。",
                   "若失败步骤名属集群编排类（Launch cluster / Wait for pods ready / Decode kubeconfig），"
                   "**必须**走集群取证，日志侧看不到根因。"],
        "probe": "pod_scheduling",
    },
    # ---- 日志侧已定性的桶（DECISIVE_BUCKETS）----
    # 这两条的共同点：pytest 自己打印的判定行已给出责任方，**不需要集群侧旁证**，
    # 故 probe 一律为 None，且 action 第一句就写明「不要再往下查基础设施」——
    # 早先的实现把 `Stream logs` 归为「Runner 与 GitHub 通信问题」并去集群找 pod 是否被驱逐，
    # 方向完全反了（实测历史样本里就有用例真失败被这么处理）。
    "测试未执行(入口/用例集不存在，脚本与代码错配)": {
        "leaf": "leaf_user_script", "owner": "code",
        "action": ["确认方式：日志里 `ERROR: file or directory not found: <路径>` + `collected 0 items` "
                   "+ `pytest exit code: ret=4` —— 一条用例都没跑，失败在**收集阶段**。",
                   "根因是**测试脚本与被测代码版本错配**（实测：run.sh 取自 main、被测代码取自 PR 分支，"
                   "main 刚改了用例入口路径而本分支尚未包含该改动），不是产品缺陷、也不是基础设施问题——"
                   "容器起了、pytest 正常执行了。",
                   "修法：让被测分支 rebase 到含该改动的提交；或让 CI 编排保证「脚本与代码同源」"
                   "（两侧 ref 一致）。**无需**集群侧取证（按规则已跳过）。"],
        "probe": None,
    },
    "测试用例失败(pytest ret=1)": {
        "leaf": "leaf_user_script", "owner": "code",
        "action": ["直接看日志里的 pytest 汇总行：`FAILED <文件>::<用例>` 与 `N failed, M passed in ...`，"
                   "按用例定位业务代码。**无需**集群侧取证：判据来自测试进程自身的退出码，"
                   "pod/节点状态即便查到也只能说明「容器当时活着」。",
                   "若同一用例在多次运行中**随机**失败（非稳定复现），才转向资源/环境方向"
                   "（此时再考虑集群侧或 runner 侧证据），并在本表补充该模式。"],
        "probe": None,
    },
}

# 步骤被直接定性（no_log 路径）时动态生成的桶名前缀 —— 这类桶不在 BUCKETS 里，
# 但同属「未读日志即定性」的步骤级结论，报告里需要给出对应的排查指引。
def knowledge_for(bucket_label: str) -> dict:
    """取桶的知识条目；未知桶（含动态生成的「步骤直接定性:*」）返回兜底条目。"""
    if bucket_label in BUCKET_KNOWLEDGE:
        return BUCKET_KNOWLEDGE[bucket_label]
    if bucket_label.startswith("步骤直接定性:"):
        # ⚠️ 本兜底**不再覆盖** `Stream logs`：它曾被视为「日志回传步骤」而落到 no_log，
        #    实则多节点 job 里它就是跑测试的那一步，已改走 window_tail 读日志（见 STEP_ROUTES）。
        #    新增步骤名时务必确认它真的属于「无需读日志即可定性」，否则会把真因挡在日志之外。
        step_name = bucket_label.split(":", 1)[1]
        return {
            "leaf": None, "owner": None,
            "action": [f"失败步骤「{step_name}」属无需读日志即可定性的步骤（runner 初始化/产物上传类），"
                       f"此类步骤失败**位于测试通过之后或之前**，通常是平台侧收尾问题，不代表业务代码有问题。",
                       "集群侧确认 runner pod 是否被驱逐/重启；若测试步骤全绿而仅此步骤失败，测试结论仍有效。"],
            "probe": "pod_container_state",
        }
    return {"leaf": None, "owner": None,
            "action": ["无预置建议：该桶未登记在知识表中，需人工排查后补充。"], "probe": None}


def official_leaf_title(leaf_id: str | None) -> str | None:
    """把官方叶子 id 译成官方分类名（报告里标注口径用）。"""
    if not leaf_id:
        return None
    return OFFICIAL_LEAF_TITLES.get(leaf_id, leaf_id)
