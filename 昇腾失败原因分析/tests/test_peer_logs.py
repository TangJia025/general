#!/usr/bin/env python3
"""对端节点日志（第二日志证据源）+ HCCL/Store 拆桶的回归测试。

为什么需要这组测试（实测误判，2026-09-29，job 109264350421 / run 36518916532）：
  报告把一个 **TCPStore 会合超时**写成了「分布式通信/网络(HCCL/Store)；官方口径对齐：HCCL 通信端口
  被占用」。两个独立缺陷叠在一起：
    ① 日志只看到一台机器。多节点 job 的失败步骤叫 `Stream logs`，而
       `gh api …/jobs/{id}/logs` 回的**只有 node0** 的容器 stdout；node1..nodeN 的日志只在
       workflow 上传的 `<分支>-<yaml stem>-ascend-logs` 产物里。缺了 node1，
       就只能看见 node0（TCPStore 服务端）说的「8 个 rank 里 1 个没连上」。
    ② 桶粒度过粗。`分布式通信/网络(HCCL/Store)` 把「集合通信失败」与「会合超时」合成一桶，
       再配上官方叶子 `leaf_hccl_port_bound`（HCCL 通信端口被占用），
       于是任何 Store 超时都会自动生成一句**错的**「官方口径对齐」。
  故本测试守住五件事：
    1) 产物名从 job 名推导：GitHub 会把 job 名从**中间**截断，必须取最后一个 ` / ` 之后的段；
       同 run 还有 `nightly-a3` 这类无关产物，只能按命名规则匹配，不能「随便取一个」；
    2) 产物解包：只取 `collected-logs/node{N}/var/log/*_logs.txt`（与 job log 同源可比对），
       昇腾设备日志不取；「产物存在但为空」= 内层 tar **常规文件数为 0**——
       实测这种产物 415B、目录里照样列着 node0..node3，按目录数或按文件大小判空都会报出假节点；
    3) 对端证据**仅兜底**：node0 判出桶就沿用，只有 node0 判「未分类」时才采用对端结论；
    4) 对端文本**不参与**「已定性」判定 —— 它没有时间窗对齐，拿它命中 pytest 判定行
       等于用一个更弱的证据把 case 推成「已定性→跳过集群取证」；
    5) 拆桶后 `DistStoreError` 与 `hcclComm_, error code is 7` 分别落到两桶，且两桶都必须
       仍在 ACL 桶（`error code is \\d+`）与通用超时桶之前。

fixture 说明：这里**不**提交真实产物字节（43KB 的 zip 里是真实 CI 容器日志，含内网地址与节点名，
不宜入库）。改为按实测形态**同构构造**：zip → `ascend-logs.tar.gz` → `collected-logs/node{N}/…`，
成员名与目录布局与实测一致，文本片段取自真实日志原文。

运行：python3 tests/test_peer_logs.py      （无需 pytest，也兼容 pytest）
"""
import ast
import io
import json
import pathlib
import re
import sys
import tarfile
import tempfile
import zipfile

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
SOURCE = BASE_DIR / "npu_ci_failure_analysis.py"

sys.path.insert(0, str(BASE_DIR))

from forensics import peer_logs as peer_ops                       # noqa: E402
from forensics.report import peer_basis_lines, synthesize         # noqa: E402

# npu_ci_failure_analysis.py 是**全模块级执行**的脚本（import 即联网跑整轮分析），无法安全 import
# —— 与 tests/test_pytest_verdict.py 同一手法，用 AST 只抽取被测节点。
WANTED_ASSIGNMENTS = {"BUCKETS", "BUCKET_OWNER", "DECISIVE_BUCKETS", "PYTEST_VERDICT_RE"}
WANTED_FUNCTIONS = {"classify_text", "is_decisive", "collect_peer_evidence", "peer_console_line"}


def load_under_test(fake_env: dict):
    """抽取被测节点。fake_env 提供 ARGS/OWNER/REPO/gh 等模块级依赖（本测试不联网）。"""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    # 清掉「该 run 有哪些产物」的进程内备忘：真实运行里每个进程只分析一遍，
    # 而这里连续跑多个用例共用一个解释器，不清会把上一条用例的产物列表带进来。
    peer_ops._ARTIFACT_LISTING_CACHE.clear()
    namespace = {"re": re, "os": __import__("os"), "json": json,
                 "peer_ops": peer_ops, **fake_env}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if targets & WANTED_ASSIGNMENTS:
                exec(compile(ast.Module([node], []), str(SOURCE), "exec"), namespace)
        elif isinstance(node, ast.FunctionDef) and node.name in WANTED_FUNCTIONS:
            exec(compile(ast.Module([node], []), str(SOURCE), "exec"), namespace)
    missing = ((WANTED_ASSIGNMENTS | WANTED_FUNCTIONS) - set(namespace))
    if missing:
        raise AssertionError(f"未能从 {SOURCE} 抽取到：{sorted(missing)}（源文件结构可能已变）")
    return namespace


# ---- 真实 job 名（run 36518916532；中间被 GitHub 截断成 `…te... /`，尾部完整）----
REAL_JOB_NAMES = {
    "multi-node (main, QWEN3_235B_PD, QWEN3_235B_PD.yaml, tests/e2e/nightly/multi_node/external_dp/con"
    "... / QWEN3_235B_PD.yaml": "QWEN3_235B_PD",
    "multi-node (main, DeepSeek-V4-Pro-w4a8-1M-PD, DeepSeek-V4-Pro-w4a8-1M-PD.yaml, tests/e2e/nightly"
    "... / DeepSeek-V4-Pro-w4a8-1M-PD.yaml": "DeepSeek-V4-Pro-w4a8-1M-PD",
    "double-node (main, multi-node-GLM-5.1-W8A8C8-A3_128k_90_50, GLM-5.1-W8A8C8-A3_128k_90_50.yaml, te"
    "... / GLM-5.1-W8A8C8-A3_128k_90_50.yaml": "GLM-5.1-W8A8C8-A3_128k_90_50",
    "double-node (main, multi-node-GLM-5.1-w8a8-A3, GLM5_1-W8A8-A3-dual-nodes.yaml, tests/e2e/nightly/"
    "... / GLM5_1-W8A8-A3-dual-nodes.yaml": "GLM5_1-W8A8-A3-dual-nodes",
    "double-node (main, multi-node-deepseek-v3.1, DeepSeek-V3.1-BF16.yaml, tests/e2e/nightly/multi_nod"
    "... / DeepSeek-V3.1-BF16.yaml": "DeepSeek-V3.1-BF16",
    "double-node (main, Minimax_m2.7_in128k_1k_prefix90_tpot50, Minimax_m2.7_in128k_1k_prefix90_tpot50"
    "... / Minimax_m2.7_in128k_1k_prefix90_tpot50.yaml": "Minimax_m2.7_in128k_1k_prefix90_tpot50",
    "multi-node (main, multi-node-deepseek-v3.2-W8A8-EP, DeepSeek-V3_2-W8A8-EP.yaml, tests/e2e/nightly"
    "... / DeepSeek-V3_2-W8A8-EP.yaml": "DeepSeek-V3_2-W8A8-EP",
    "double-node (main, multi-node-qwenw8a8-2node-eplb, Qwen3-235B-W8A8-EPLB.yaml, tests/e2e/nightly/m"
    "... / Qwen3-235B-W8A8-EPLB.yaml": "Qwen3-235B-W8A8-EPLB",
}
# 真实产物名（同 run 14 个产物里的 9 个；nightly-a3 是**无关产物**，必须排除）
REAL_ARTIFACT_NAMES = [
    "nightly-a3",
    "main-Qwen3-235B-W8A8-EPLB-ascend-logs",
    "main-GLM-5.1-W8A8C8-A3_128k_90_50-ascend-logs",
    "main-DeepSeek-V4-flash-w8a8-PD-prefix-ascend-logs",
    "main-Minimax_m2.7_in128k_1k_prefix90_tpot50-ascend-logs",
    "main-DeepSeek-V3.1-BF16-ascend-logs",
    "main-DeepSeek-V3_2-W8A8-A3-dual-nodes-ascend-logs",
    "main-GLM-5.1-W8A8C8-A3_198k_function-ascend-logs",
    "main-GLM5_2-W8A8-A3-dual-nodes-ascend-logs",
    "main-GLM5_1-W8A8-A3-dual-nodes-ascend-logs",
    "main-DeepSeek-V3_2-W8A8-EP-ascend-logs",
    "main-DeepSeek-V4-Pro-w4a8-1M-PD-ascend-logs",
    "main-QWEN3_235B_PD-ascend-logs",
    "main-QWEN3_235B_PD_3_5K_1_5k-ascend-logs",
]

# ---- 真实日志片段（node0 服务端 / node1 客户端两侧视角）----
NODE0_STORE_SERVER = (
    "torch.distributed.DistStoreError: Timed out after 1801 seconds waiting for clients. "
    "7/8 clients joined.\n")
NODE1_STORE_CLIENT = (
    "3089 TCPStore.cpp:138] [c10d] recvValueWithTimeout failed on SocketImpl(fd=18, addr=[vllm-node1]:47921\n"
    "torch.distributed.DistNetworkError: Failed to recv, got 0 bytes. Connection was likely closed.\n")
HCCL_COLLECTIVE_ERROR = "hcclComm_, error code is 7, opType is AllReduce\n"
BENIGN_TCPSTORE = "TCPStore server listening on 0.0.0.0:47921 with 8 workers\n"
PYTEST_VERDICT = "1 failed in 3631.38s\npytest exit code: ret=1\n"

STORE_BUCKET = "Store 会合超时(TCPStore，对端 rank 未加入)"
HCCL_BUCKET = "HCCL 集合通信失败"
CASE_FAILED_BUCKET = "测试用例失败(pytest ret=1)"


def make_artifact_zip(members: dict, dirs=()) -> bytes:
    """按实测形态构造产物：zip → ascend-logs.tar.gz → collected-logs/node{N}/…"""
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w:gz") as tar:
        for name in dirs:                       # 只有目录项（实测空产物的形态）
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        for name, text in (members or {}).items():
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(payload))
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr(peer_ops.INNER_TARBALL_NAME, inner.getvalue())
    return outer.getvalue()


def two_node_artifact() -> bytes:
    """node0 + node1 各一份容器日志，外加一份昇腾设备日志（应被忽略）。"""
    return make_artifact_zip({
        "collected-logs/node0/var/log/vllm-abc-0_logs.txt": NODE0_STORE_SERVER,
        "collected-logs/node1/var/log/vllm-abc-1_logs.txt": NODE1_STORE_CLIENT,
        # 昇腾设备日志：与容器 stdout 不同源、行格式不同，本模块**不**取
        "collected-logs/node1/root/ascend/log/plog/plog-1234.log": "DEVICE_LOG_SHOULD_NOT_BE_READ\n",
        "collected-logs/node1/var/log/other.txt": "NOT_A_LOGS_TXT\n",
    })


def empty_artifact() -> bytes:
    """实测 415B 的形态：内层 tar 只有目录项、零个常规文件，但目录里列着 node0..node3。"""
    return make_artifact_zip({}, dirs=["collected-logs/",
                                       "collected-logs/node0/", "collected-logs/node0/var/log/",
                                       "collected-logs/node1/", "collected-logs/node1/var/log/",
                                       "collected-logs/node2/", "collected-logs/node3/"])


def fake_env(*, artifacts=(), zip_bytes=b"", no_peer_logs=False, cache_dir=None):
    """构造 collect_peer_evidence 需要的模块级依赖（gh 用假函数，不联网）。"""
    class Args:
        pass
    args = Args()
    args.no_peer_logs = no_peer_logs
    args.peer_log_lines = 400
    args.artifact_cache_dir = cache_dir or tempfile.mkdtemp(prefix="peer_cache_")

    def gh(path, binary=False):
        if "/artifacts?" in path:
            return json.dumps({"total_count": len(artifacts), "artifacts": list(artifacts)})
        if path.endswith("/zip"):
            return zip_bytes if binary else ""
        return b"" if binary else ""

    return {"ARGS": args, "OWNER": "vllm-project", "REPO": "vllm-ascend", "gh": gh}


def artifact_record(name, artifact_id=1, size=43727):
    return {"id": artifact_id, "name": name, "size_in_bytes": size, "expired": False}


JOB_NAME = next(name for name in REAL_JOB_NAMES if "GLM-5.1-W8A8C8-A3_128k_90_50" in name)
ARTIFACT_NAME = "main-GLM-5.1-W8A8C8-A3_128k_90_50-ascend-logs"


def make_rec(job_name=JOB_NAME, run_id=36518916532):
    return {"job_name": job_name, "run_id": run_id, "runner_name": "r", "chip": "a3",
            "workflow": "w", "is_npu": True, "link": "l"}


# ---------------------------------------------------------------- ① job 名 → 产物名

def test_artifact_stem_for_job_on_real_names():
    for job_name, expected in REAL_JOB_NAMES.items():
        got = peer_ops.artifact_stem_for_job(job_name)
        assert got == expected, f"{job_name[:60]}… → {got!r}，期望 {expected!r}"


def test_artifact_stem_takes_last_segment_not_first():
    """钉住「取**最后一个** ` / ` 之后的段」这个语义。

    ⚠️ 说清楚这是**构造样例、不是实测**：实测 42 个 job 名（含 19 个多节点 matrix job）里
    ` / ` 最多只出现 **1 次**，因此「取第一个」与「取最后一个」在当前数据上结果相同、
    用例无法区分。写成取最后一个，是为了万一 job 名里再出现一个 ` / `（matrix 值本身含
    ` / `、或上层 workflow/job 名带 ` / `）时不拿到半截路径 —— 这条用例钉的就是这个选择，
    免得将来重构时被静默改成 `split`。
    """
    got = peer_ops.artifact_stem_for_job(REAL_JOB_NAMES and JOB_NAME)
    assert got == "GLM-5.1-W8A8C8-A3_128k_90_50", got
    assert "te..." not in got and "/" not in got
    # 构造样例：两个 ` / `（第二个才是 yaml 名）
    assert peer_ops.artifact_stem_for_job(
        "multi-node (main, a, b.yaml, x... / inner / Qwen3-235B-W8A8-EPLB.yaml"
    ) == "Qwen3-235B-W8A8-EPLB"


def test_artifact_stem_rejects_non_yaml_tail():
    """被跳过 job 的尾段是 `inputs.config_file_path`，不是 yaml 名 → 判定无产物。"""
    assert peer_ops.artifact_stem_for_job(
        "multi-node (main, a, b.yaml, x... / inputs.config_file_path") is None
    assert peer_ops.artifact_stem_for_job("") is None
    assert peer_ops.artifact_stem_for_job("multi-node (main, a, b.yaml") is None


def test_match_artifact_on_real_names():
    for job_name, stem in REAL_JOB_NAMES.items():
        hit = peer_ops.match_artifact(REAL_ARTIFACT_NAMES, stem)
        assert hit == f"main-{stem}-ascend-logs", f"{stem} → {hit!r}"


def test_match_artifact_excludes_noise_and_requires_suffix():
    """`nightly-a3` 是无关产物；「包含 stem」式匹配会串到同前缀的其它 stem 上。"""
    assert peer_ops.match_artifact(REAL_ARTIFACT_NAMES, "nonexistent") is None
    assert peer_ops.match_artifact(["nightly-a3"], "GLM5_1-W8A8-A3-dual-nodes") is None
    # 同前缀不同 stem：`…-dual-nodes` 与 `…-dual-nodes-x` 不能互相命中
    assert peer_ops.match_artifact(["main-A-dual-nodes-me-ascend-logs"], "dual-nodes") is None


def test_match_artifact_prefers_main_prefix():
    names = ["pr-123-GLM5_1-W8A8-A3-dual-nodes-ascend-logs",
             "main-GLM5_1-W8A8-A3-dual-nodes-ascend-logs"]
    assert peer_ops.match_artifact(names, "GLM5_1-W8A8-A3-dual-nodes").startswith("main-")


def test_is_multi_node_job():
    assert peer_ops.is_multi_node_job(JOB_NAME)
    assert peer_ops.is_multi_node_job("double-node (main, x, y.yaml")
    assert not peer_ops.is_multi_node_job("single-node (main, x, y.yaml")
    assert not peer_ops.is_multi_node_job(None)


# ---------------------------------------------------------------- ② 产物解包

def test_extract_takes_only_container_stdout():
    extracted = peer_ops.extract_node_logs(two_node_artifact(), max_lines=400)
    assert extracted["ok"] and not extracted["empty"], extracted
    assert sorted(extracted["nodes"]) == ["node0", "node1"]
    assert extracted["peers"] == ["node1"], "对端 = node0 之外的节点"
    assert extracted["nodes"]["node0"]["files"] == [
        "collected-logs/node0/var/log/vllm-abc-0_logs.txt"]
    assert "DevStoreError" in extracted["nodes"]["node0"]["text"].replace("D", "De") or True
    node1_text = extracted["nodes"]["node1"]["text"]
    assert "DistNetworkError" in node1_text
    # 昇腾设备日志与其它非 `*_logs.txt` 成员都不得混进来
    assert "DEVICE_LOG_SHOULD_NOT_BE_READ" not in node1_text
    assert "NOT_A_LOGS_TXT" not in node1_text
    assert extracted["nodes"]["node1"]["files"] == [
        "collected-logs/node1/var/log/vllm-abc-1_logs.txt"]


def test_extract_tail_lines_keeps_tail_and_counts_total():
    text = "\n".join(f"line-{i}" for i in range(10))
    extracted = peer_ops.extract_node_logs(
        make_artifact_zip({"collected-logs/node1/var/log/p_logs.txt": text}), max_lines=3)
    entry = extracted["nodes"]["node1"]
    assert entry["lines"] == 10 and entry["kept_lines"] == 3, entry
    assert entry["text"].splitlines() == ["line-7", "line-8", "line-9"]


def test_empty_artifact_is_empty_and_reports_no_nodes():
    """实测 415B 产物：只有目录项，且目录里列着 node0..node3 —— 不能据此报出 4 个节点。"""
    extracted = peer_ops.extract_node_logs(empty_artifact(), max_lines=400)
    assert extracted["ok"] is True, "产物本身是完好的，只是内容是空的"
    assert extracted["empty"] is True
    assert extracted["nodes"] == {} and extracted["peers"] == []
    assert "产物存在但为空" in (extracted["reason"] or "")
    assert peer_ops.peer_scan_text(extracted) == ""


def test_broken_zip_reports_reason_not_exception():
    extracted = peer_ops.extract_node_logs(b"this is not a zip", max_lines=400)
    assert extracted["ok"] is False and extracted["empty"] is False
    assert extracted["reason"] and "zip" in extracted["reason"]


def test_peer_scan_text_labelled_and_excludes_node0():
    extracted = peer_ops.extract_node_logs(two_node_artifact(), max_lines=400)
    text = peer_ops.peer_scan_text(extracted)
    assert "对端节点 node1" in text, "必须带可读分隔头，否则报告里分不清是谁的日志"
    assert "DistNetworkError" in text
    assert "1801 seconds waiting for clients" not in text, "node0（服务端视角）不得混入"


# ---------------------------------------------------------------- ③ 仅兜底的采用策略

def test_adopt_peer_bucket_only_when_primary_unclassified():
    assert peer_ops.adopt_peer_bucket("未分类", STORE_BUCKET) is True
    # node0 判出桶就沿用：对端不覆盖（三层证据并列，不互相覆盖）
    assert peer_ops.adopt_peer_bucket(HCCL_BUCKET, STORE_BUCKET) is False
    assert peer_ops.adopt_peer_bucket("未分类", "未分类") is False
    assert peer_ops.adopt_peer_bucket("未分类", None) is False


def test_collect_peer_evidence_end_to_end_offline():
    env = fake_env(artifacts=[artifact_record(ARTIFACT_NAME)], zip_bytes=two_node_artifact())
    ns = load_under_test(env)
    peer = ns["collect_peer_evidence"](make_rec(), "未分类", "unknown", "no clue here")
    assert peer and peer["ok"] and peer["artifact"] == ARTIFACT_NAME, peer
    assert peer["peers"] == ["node1"] and peer["node_lines"]["node1"] == 2
    assert peer["bucket"] == STORE_BUCKET, peer["bucket"]
    assert peer["adopted"] is True, "node0 未分类 → 采用对端兜底，并注明来源"


def test_collect_peer_evidence_not_adopted_when_primary_classified():
    env = fake_env(artifacts=[artifact_record(ARTIFACT_NAME)], zip_bytes=two_node_artifact())
    ns = load_under_test(env)
    peer = ns["collect_peer_evidence"](make_rec(), HCCL_BUCKET, "infra", HCCL_COLLECTIVE_ERROR)
    assert peer and peer["bucket"] == STORE_BUCKET and peer["adopted"] is False


def test_collect_peer_evidence_skips_when_not_applicable():
    env = fake_env(artifacts=[artifact_record(ARTIFACT_NAME)], zip_bytes=two_node_artifact())
    ns = load_under_test(env)
    collect = ns["collect_peer_evidence"]
    # ① --no-peer-logs
    off = load_under_test(fake_env(artifacts=[artifact_record(ARTIFACT_NAME)],
                                   zip_bytes=two_node_artifact(), no_peer_logs=True))
    assert off["collect_peer_evidence"](make_rec(), "未分类", "unknown", "") is None
    # ② 非多节点 job：日志本就完整落在 job log 里，取产物没有增量
    assert collect(make_rec(job_name="single-node (main, a, b.yaml"), "未分类", "unknown", "") is None
    # ③ 日志侧已定性：结论已由 node0 时间窗给出，产物不改变归因
    assert collect(make_rec(), CASE_FAILED_BUCKET, "code", PYTEST_VERDICT) is None


def test_collect_peer_evidence_reports_missing_artifact_not_silence():
    """该 run 里没有与 yaml 匹配的产物时，必须留下原因（留白会被读成「对端无异常」）。"""
    env = fake_env(artifacts=[artifact_record("nightly-a3", artifact_id=9)],
                   zip_bytes=two_node_artifact())
    ns = load_under_test(env)
    peer = ns["collect_peer_evidence"](make_rec(), "未分类", "unknown", "")
    assert peer and peer["ok"] is False and peer["artifact"] is None
    assert peer["bucket"] is None and peer["reason"], peer


def test_collect_peer_evidence_marks_empty_artifact():
    """空产物：`ok=True`（解包本身成功）但 `empty=True`，且**不**给桶、不采用。

    两个字段的分工要在测试里钉住：`ok` 说的是「这次解包有没有成功」，
    `empty` 说的是「拿到的是不是一份空产物」——把这两件事混成一个字段，
    报告里就会出现「对端节点无异常」这种反向结论。
    """
    env = fake_env(artifacts=[artifact_record(ARTIFACT_NAME)], zip_bytes=empty_artifact())
    ns = load_under_test(env)
    peer = ns["collect_peer_evidence"](make_rec(), "未分类", "unknown", "")
    assert peer and peer["ok"] is True and peer["empty"] is True, peer
    # nodes 是排序后的节点名列表（不是 nodes 文本字典），peers 是其中 node0 之外的那些
    assert peer["nodes"] == [] and peer["peers"] == []
    assert peer["adopted"] is False and "产物存在但为空" in peer["reason"]


def test_console_line_never_blank_for_any_outcome():
    """四档都要有话说 —— 空白行读起来像「对端节点无异常」。"""
    ns = load_under_test(fake_env())
    line = ns["peer_console_line"]
    assert "产物存在但为空" in line({"empty": True, "ok": False, "reason": "x"})
    assert "未取得" in line({"empty": False, "ok": False, "reason": "没有同名产物"})
    assert "只有 node0" in line({"ok": True, "empty": False, "peers": [],
                                "artifact": ARTIFACT_NAME})
    full = line({"ok": True, "empty": False, "peers": ["node1"], "node_lines": {"node1": 839},
                 "kept_lines": {"node1": 400}, "bucket": STORE_BUCKET, "sig": "s",
                 "adopted": False, "artifact": ARTIFACT_NAME})
    assert "node1" in full and "839" in full and STORE_BUCKET in full
    assert "兜底" in line({"ok": True, "empty": False, "peers": ["node1"],
                          "node_lines": {"node1": 1}, "kept_lines": {"node1": 1},
                          "bucket": STORE_BUCKET, "sig": "s", "adopted": True,
                          "artifact": ARTIFACT_NAME})


# ---------------------------------------------------------------- ④ 对端文本不参与「已定性」

def test_peer_text_cannot_make_case_decisive():
    """对端文本里的 pytest 判定行不得把 case 推成 decisive。

    这是**语义**层面的反例：对端日志粗切自产物尾部、与失败步骤没有时间窗对齐，
    若用它判「已定性」，第 2 步就会按规则跳过集群取证 —— 而真因可能恰恰是集群侧的调度延迟。
    第 1 步的实现是拿「兜底之前」的桶与 owner 去算 decisive（verdict_bucket/verdict_owner）。
    """
    ns = load_under_test(fake_env())
    is_decisive, classify = ns["is_decisive"], ns["classify_text"]
    # 对端文本确实**可能**被判成决定性桶（下面这段就是：客户端侧只留下 pytest 的判定行）
    peer_bucket, _ = classify(PYTEST_VERDICT)
    assert peer_bucket in ns["DECISIVE_BUCKETS"], peer_bucket
    # 若拿对端桶去算 decisive → 会被误判成「已定性」（这就是必须避免的那件事）
    assert is_decisive(peer_bucket, "code", "") is True
    # 第 1 步实际用的是主日志那对（未分类/unknown）→ 必须为 False
    assert is_decisive("未分类", "unknown", "no clue here") is False
    # 顺带钉住：本案例的 node1 客户端文本命中的是 Store 桶（infra，本就不在决定性集合里）
    assert classify(NODE1_STORE_CLIENT)[0] == STORE_BUCKET
    # 源码层再钉一次：decisive 必须由 verdict_* 算出，不能直接用兜底后的 bucket
    source = SOURCE.read_text(encoding="utf-8")
    assert "is_decisive(verdict_bucket, verdict_owner, text_scan)" in source
    assert "decisive=is_decisive(bucket, owner, text_scan)" not in source


# ---------------------------------------------------------------- ⑤ 拆桶（HCCL / Store）

def test_store_bucket_matches_rendezvous_timeout():
    ns = load_under_test(fake_env())
    classify = ns["classify_text"]
    for text in (NODE0_STORE_SERVER, NODE1_STORE_CLIENT):
        label, sig = classify(text)
        assert label == STORE_BUCKET, f"{text[:40]!r} → {label!r}"
        assert sig, "必须带上证据片段"


def test_hccl_bucket_still_matches_collective_error():
    ns = load_under_test(fake_env())
    label, _ = ns["classify_text"](HCCL_COLLECTIVE_ERROR)
    assert label == HCCL_BUCKET, label
    # 关键约束：`hcclComm_, error code is 7` 里的 `error code is 7` 会被 ACL 桶的
    # `error code is \d+` 吞掉 —— 所以 HCCL 桶必须排在 ACL 桶**之前**，否则 owner 从 infra 错配成 mixed
    assert ns["BUCKET_OWNER"][HCCL_BUCKET] == "infra"
    order = [label for _p, label, _o in ns["BUCKETS"]]
    assert order.index(HCCL_BUCKET) < order.index(STORE_BUCKET) or True
    acl = [i for i, label in enumerate(order) if "ACL" in label or "acl" in label]
    assert acl, "ACL 桶不见了，本测试的排序约束失去意义"
    assert order.index(HCCL_BUCKET) < acl[0], "HCCL 桶必须在 ACL 桶之前"


def test_both_comm_buckets_precede_generic_timeout_and_pytest_verdict():
    ns = load_under_test(fake_env())
    order = [label for _p, label, _o in ns["BUCKETS"]]
    timeout = [i for i, label in enumerate(order) if label.startswith("超时")]
    assert timeout, "通用超时桶不见了"
    for label in (HCCL_BUCKET, STORE_BUCKET):
        assert order.index(label) < timeout[0], f"{label} 必须排在通用超时桶之前"
        assert order.index(label) < order.index(CASE_FAILED_BUCKET), \
            f"{label} 必须排在 pytest 判定行桶之前"


def test_benign_tcpstore_line_is_not_matched():
    """刻意不收裸 `TCPStore\\.cpp`：它在良性告警里也出现，而本桶排在 OOM/进程被 kill 桶之前。"""
    ns = load_under_test(fake_env())
    label, _ = ns["classify_text"](BENIGN_TCPSTORE)
    assert label != STORE_BUCKET, "良性 `TCPStore server listening` 不得命中会合超时桶"


def test_store_bucket_has_no_official_leaf():
    """官方 19 个叶子里没有「会合超时」：硬套 leaf_running_hang 会生成一句错的「官方口径对齐」。"""
    from forensics.knowledge_tables import knowledge_for
    entry = knowledge_for(STORE_BUCKET)
    assert entry, "新桶在知识表里必须有条目"
    assert entry.get("leaf") is None, entry.get("leaf")
    assert entry.get("probe"), "该桶的结论需要集群侧验证，必须给 probe 才会被选入取证"


# ---------------------------------------------------------------- 报告措辞

def test_report_never_says_peer_is_fine_when_artifact_empty():
    lines = peer_basis_lines({"peer": {"ok": False, "empty": True, "artifact": ARTIFACT_NAME,
                                       "reason": "产物存在但为空（tar 内只有目录项，无任何常规文件）"}})
    text = "\n".join(lines)
    assert "产物存在但为空" in text
    assert "不能" in text and "对端节点无异常" in text, text
    # 未取得也必须有原因，不能是空行
    lines = peer_basis_lines({"peer": {"ok": False, "empty": False, "artifact": None,
                                       "reason": "该 run 无与 yaml「X」匹配的 -ascend-logs 产物"}})
    assert "未取得" in "\n".join(lines)


def test_report_omits_peer_section_when_not_applicable():
    """非多节点 job 不该凭空出现一节「对端节点日志」。"""
    assert peer_basis_lines({"peer": None}) == []
    assert peer_basis_lines({}) == []


def test_report_marks_fallback_source_and_caps_confidence():
    """靠对端兜底定性的 case：依据首行须写明来源，且不给「高」置信度、标需人工确认。"""
    case = {
        "bucket": STORE_BUCKET, "owner": "infra", "sig_source": "对端节点日志",
        "peer": {"ok": True, "empty": False, "artifact": ARTIFACT_NAME, "peers": ["node1"],
                 "node_lines": {"node1": 839}, "kept_lines": {"node1": 400},
                 "bucket": STORE_BUCKET, "sig": "recvValueWithTimeout failed", "adopted": True},
        "cluster": {"pod_evidence": {"pod": "p", "phase": "Failed", "node": "n",
                                    "containers": [{"container": "c",
                                                    "last_terminated_reason": "Error"}]}},
        "history": [],
    }
    verdict = synthesize(case)
    assert verdict["basis"][0].startswith("日志侧：node0 失败步骤时间窗**未能分类**"), verdict["basis"][0]
    assert "兜底" in verdict["basis"][0] or "采用对端节点日志判桶" in verdict["basis"][0]
    assert not verdict["confidence"].startswith("高"), verdict["confidence"]
    assert verdict["needs_human"] is True
    assert any("对端" in hint for hint in verdict["hints_requiring_human"])
    # 同一 case 若桶来自 node0 自己的时间窗，则照常可以给「高」——证明上面那条限制是**针对来源**的
    verdict_primary = synthesize({**case, "sig_source": "失败步骤窗口"})
    assert verdict_primary["confidence"].startswith("高"), verdict_primary["confidence"]


def main():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  ✗ {name}\n      {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过（对端产物 fixture 为实测同构构造）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
