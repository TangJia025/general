#!/usr/bin/env python3
"""「按日志归因到业务侧 → 提前退出集群取证」的回归测试。

为什么需要这组测试（实测踩坑，2026-09-28）：
  多节点 job 的测试执行步骤名叫 `Stream logs`（harness 在此跑 pytest 并流式输出）。
  它看起来像「日志回传步骤」，于是被 STEP_ROUTES 归为 no_log/infra ——**不读日志、直接判
  基础设施**，再让第 2 步去集群找 runner pod 是否被驱逐。实测该桶在历史语料里占 19%，
  抽样一读就发现里面有用例真失败：
      `FAILED tests/...::test_external_dp` + `1 failed in 3631.38s` + `pytest exit code: ret=1`
  另一类更硬（同一批日志的 Nightly-A3 PR 17618）：
      `ERROR: file or directory not found: tests/e2e/nightly/multi_node/scripts/test_multi_node.py`
      `collected 0 items` + `pytest exit code: ret=4` —— 一条用例都没跑，是脚本与代码版本错配。
  两类都被两层包装（`Some tests failed` → `Executing the custom container implementation failed`）
  转述成「测试失败」，极易被判成产品缺陷或平台故障。故本测试守住四件事：
    1) `Stream logs` 必须**读日志**（不再走 no_log）；产物上传类仍走 no_log；
    2) pytest 判定行必须**先于**通用包装桶命中（顺序即优先级，BUCKETS 的排列是校准结果）；
    3) 命中即标 decisive，且**绝不进 cluster_todo**（这就是「提前退出、不排查基础设施」）；
    4) 下游真的据此跳过取证：select_cases 不占名额、step3_skip_cluster 不发任何集群调用、
       报告里写「按规则跳过」而**不是**「未取证」（后者是「查了没查到」，语义相反）。

运行：python3 tests/test_pytest_verdict.py      （无需 pytest，也兼容 pytest）
"""
import ast
import pathlib
import re
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
SOURCE = BASE_DIR / "npu_ci_failure_analysis.py"

sys.path.insert(0, str(BASE_DIR))

# npu_ci_failure_analysis.py 是**全模块级执行**的脚本（import 即联网跑整轮分析），
# 无法安全 import —— 与 tests/test_label_classification.py 同一手法，用 AST 只抽取被测节点。
WANTED_ASSIGNMENTS = {"BUCKETS", "BUCKET_OWNER", "DECISIVE_BUCKETS", "STEP_ROUTES", "PYTEST_VERDICT_RE"}
WANTED_FUNCTIONS = {"classify_text", "is_decisive", "route_for"}


def load_under_test():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    namespace = {"re": re}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if targets & WANTED_ASSIGNMENTS:
                exec(compile(ast.Module([node], []), str(SOURCE), "exec"), namespace)
        elif isinstance(node, ast.FunctionDef) and node.name in WANTED_FUNCTIONS:
            exec(compile(ast.Module([node], []), str(SOURCE), "exec"), namespace)
    # PYTEST_VERDICT_RE 由 re.compile 构造，不是 ast.Assign 的字面量也能 eval —— 但它在源码里
    # 是赋值语句，上面已覆盖。缺任何一项都说明源文件结构已变，必须报错而不是静默跳过。
    missing = (WANTED_ASSIGNMENTS - set(namespace)) | (WANTED_FUNCTIONS - set(namespace))
    if missing:
        raise AssertionError(f"未能从 {SOURCE} 抽取到：{sorted(missing)}（源文件结构可能已变）")
    return namespace


NS = load_under_test()
BUCKETS = NS["BUCKETS"]
BUCKET_OWNER = NS["BUCKET_OWNER"]
DECISIVE_BUCKETS = set(NS["DECISIVE_BUCKETS"])
classify_text = NS["classify_text"]
is_decisive = NS["is_decisive"]
route_for = NS["route_for"]
PYTEST_VERDICT_RE = NS["PYTEST_VERDICT_RE"]

ENTRY_MISSING_BUCKET = "测试未执行(入口/用例集不存在，脚本与代码错配)"
CASE_FAILED_BUCKET = "测试用例失败(pytest ret=1)"

# ---- 真实日志片段（取自 2026-09-28 上游 nightly 失败 job 的失败步骤时间窗尾部）----
# 保留原样措辞与顺序：这些正是「外层包装抄在最里层真因之后」的现场，顺序本身就是被测对象。
LOG_ENTRY_MISSING = """\
2026-09-28T13:11:40.1234567Z INFO: run.sh: EXTERNAL_DP test path = tests/e2e/nightly/multi_node/scripts/test_multi_node.py
2026-09-28T13:11:41.2234567Z ============================= test session starts ==============================
2026-09-28T13:11:41.3234567Z ERROR: file or directory not found: tests/e2e/nightly/multi_node/scripts/test_multi_node.py
2026-09-28T13:11:41.4234567Z
2026-09-28T13:11:41.5234567Z collected 0 items
2026-09-28T13:11:41.6234567Z pytest exit code: ret=4
2026-09-28T13:11:45.0000000Z FAIL_TAG_Minimax_m2.7_in128k_1k_prefix90_tpot50.yaml ✗ ERROR: Some tests failed
2026-09-28T13:11:46.0000000Z Error: Executing the custom container implementation failed
2026-09-28T13:11:47.0000000Z ##[error]Process completed with exit code 1.
"""

LOG_CASE_FAILED = """\
2026-05-19T02:31:40.1234567Z =================================== FAILURES ===================================
2026-05-19T02:31:40.2234567Z FAILED tests/e2e/nightly/single_node/ops/singlecard_ops/test_external_dp.py::test_external_dp
2026-05-19T02:31:41.3234567Z =========================== short test summary info ============================
2026-05-19T02:31:41.4234567Z 1 failed, 14 warnings in 3631.38s
2026-05-19T02:31:41.5234567Z pytest exit code: ret=1
2026-05-19T02:31:42.0000000Z Error: Executing the custom container implementation failed
2026-05-19T02:31:43.0000000Z ##[error]Process completed with exit code 1.
"""

# ret=1 且尾部带断言 traceback —— 实测的**普通**形态（有断言时先命中【断言失败】桶）。
# 若只按桶名判 decisive，这类 case 会绕过提前退出，故必须同时被 is_decisive 判为已定性。
LOG_CASE_FAILED_WITH_ASSERT = """\
2026-05-19T02:31:40.1234567Z >       assert actual == expected
2026-05-19T02:31:40.2234567Z E       AssertionError: tensor(0.5312) != tensor(0.5100)
2026-05-19T02:31:40.3234567Z FAILED tests/e2e/nightly/single_node/ops/singlecard_ops/test_precision.py::test_mlp
2026-05-19T02:31:41.5234567Z pytest exit code: ret=1
"""

# 反例：容器真的被 OOM 杀死，日志里也有 pytest 判定行（收尾阶段 pytest 报错退出）。
# 这种**不能**判成业务侧：责任方尚未落在 code，集群侧证据仍可能是关键。
LOG_OOM_WITH_VERDICT = """\
2026-05-19T02:31:40.1234567Z ERROR:root:torch.OutOfMemoryError: NPU out of memory. Tried to allocate 2.00 GiB
2026-05-19T02:31:40.2234567Z pytest exit code: ret=1
2026-05-19T02:31:41.0000000Z Error: Executing the custom container implementation failed
"""

# 产物上传失败：仍属「无需读日志」的平台侧收尾问题，必须保持 no_log
LOG_UPLOAD_FAILED = """\
2026-05-19T02:31:40.1234567Z ##[error]Upload artifact failed with 500
"""


def _route(step_name):
    return route_for(step_name)[0]


# ---------- 1. 路由：Stream logs 必须读日志 ----------

def test_stream_logs_reads_logs():
    """`Stream logs` 是多节点 job 的测试执行步骤，必须走 window_tail（读日志）。"""
    assert _route("Stream logs") == "window_tail", (
        "`Stream logs` 又回到 no_log 了 —— 那会不读日志直接判 infra，"
        "把用例真失败记成平台故障并去集群找 pod（实测错判占历史语料 19%）"
    )


def test_upload_steps_stay_no_log():
    """产物上传/归档类仍走 no_log：它们失败在测试之后，与业务代码无关。"""
    for step in ("Upload test logs", "Upload failed", "Upload artifact",
                 "Upload benchmark results", "Set up job", "Initialize containers"):
        assert _route(step) == "no_log", f"{step} 应保持 no_log 路径，实际 {_route(step)}"


# ---------- 2. 分类：pytest 判定行必须赢过外层包装 ----------

def test_entry_missing_classified_as_code():
    bucket, sig = classify_text(LOG_ENTRY_MISSING)
    assert bucket == ENTRY_MISSING_BUCKET, f"入口不存在应判「测试未执行」，实际：{bucket}"
    assert BUCKET_OWNER[bucket] == "code", "责任方必须是业务侧（code）"
    assert "file or directory not found" in sig, f"证据片段没抓到判据行：{sig}"


def test_case_failed_classified_as_code():
    bucket, sig = classify_text(LOG_CASE_FAILED)
    assert bucket == CASE_FAILED_BUCKET, f"ret=1 应判「测试用例失败」，实际：{bucket}"
    assert BUCKET_OWNER[bucket] == "code"


def test_verdict_line_beats_generic_wrapper():
    """顺序断言：pytest 判定行的桶必须排在通用包装桶之前。

    否则 `Executing the custom container implementation failed` / `failed to run script step`
    会把真判定覆盖成 unknown/infra —— 这正是改前的行为。
    """
    labels = [label for _, label, _ in BUCKETS]
    assert labels.index(ENTRY_MISSING_BUCKET) < labels.index("脚本步骤通用包装失败(需按失败步骤细化)")
    assert labels.index(CASE_FAILED_BUCKET) < labels.index("脚本步骤通用包装失败(需按失败步骤细化)")
    # `exit code 255` 是「K8s 强制终止」的通用包装，同属必须被真判据压住的那些
    assert labels.index(ENTRY_MISSING_BUCKET) < labels.index("步骤被强制终止(exit 255，非根因)")


def test_bucket_order_keeps_real_root_causes_first():
    """反向断言：真根因桶（硬件/网络/OOM）仍**先于** pytest 判定行。

    若把判定行提到最前面，一个「OOM 导致用例失败」的 job 会被写成业务侧用例失败 ——
    那是把硬件问题读成业务问题，比原来的错判更糟。
    """
    labels = [label for _, label, _ in BUCKETS]
    # 「HCCL 集合通信失败」「Store 会合超时」原为一个合并桶「分布式通信/网络(HCCL/Store)」，
    # 两者同属真根因层，故与 OOM/调度并列断言（旧桶名已不存在，写旧名会让本断言直接 ValueError）
    for real_cause in ("OOM/显存不足", "HCCL 集合通信失败",
                       "Store 会合超时(TCPStore，对端 rank 未加入)",
                       "多节点pod调度/就绪失败(k8s侧)", "进程被kill(OOM/超内存)"):
        assert labels.index(real_cause) < labels.index(ENTRY_MISSING_BUCKET), \
            f"{real_cause} 应排在 pytest 判定行之前"
        assert labels.index(real_cause) < labels.index(CASE_FAILED_BUCKET)


def test_oom_is_not_business_side():
    """反例：OOM 日志里带 pytest 判定行时，不得判成业务侧。"""
    bucket, _ = classify_text(LOG_OOM_WITH_VERDICT)
    assert bucket == "OOM/显存不足", f"应命中 OOM 桶，实际：{bucket}"
    owner = BUCKET_OWNER[bucket]
    assert not is_decisive(bucket, owner, LOG_OOM_WITH_VERDICT), \
        "mixed/infra 的桶不得被标成「日志侧已定性为业务侧」——那会跳过集群取证，把硬件问题写成业务问题"


def test_upload_failure_not_decisive():
    """反例：产物上传失败没有 pytest 判定行，不构成已定性。"""
    bucket, _ = classify_text(LOG_UPLOAD_FAILED)
    assert not is_decisive(bucket, BUCKET_OWNER.get(bucket, "infra"), LOG_UPLOAD_FAILED)


# ---------- 3. 已定性判定：两条路径都要覆盖 ----------

def test_entry_missing_is_decisive():
    bucket, _ = classify_text(LOG_ENTRY_MISSING)
    assert is_decisive(bucket, BUCKET_OWNER[bucket], LOG_ENTRY_MISSING)


def test_case_failed_is_decisive_even_with_assertion_bucket():
    """ret=1 且尾部有断言 traceback：先命中【断言失败】桶，但**仍须**判为已定性。

    这是最容易漏的一条 —— 只按桶名判 decisive 时，实测最常见的「用例失败」形态
    （带断言输出）会绕过提前退出，照样占取证名额、白跑一次集群查询。
    """
    bucket, _ = classify_text(LOG_CASE_FAILED_WITH_ASSERT)
    assert bucket == "断言失败(代码或精度)", f"该样本应先命中断言桶，实际：{bucket}"
    owner = BUCKET_OWNER[bucket]
    assert owner == "code" and bucket not in DECISIVE_BUCKETS, "前置条件：该桶不在 DECISIVE_BUCKETS 里"
    assert is_decisive(bucket, owner, LOG_CASE_FAILED_WITH_ASSERT), \
        "桶判 code + 日志里有 pytest 判定行 → 就是已定性，无需集群侧旁证"


def test_decisive_buckets_are_all_code():
    """DECISIVE_BUCKETS 只应装业务侧的桶（跳过集群取证的前提是责任方已明确）。"""
    for bucket in DECISIVE_BUCKETS:
        assert BUCKET_OWNER.get(bucket) == "code", f"{bucket} 的 owner 不是 code，不应进 DECISIVE_BUCKETS"


# ---------- 4. 端到端：提前退出真的生效 ----------

def _payload_with(decisive_items, other_items):
    classifications = []
    for job_name, bucket, owner, decisive in list(decisive_items) + list(other_items):
        classifications.append({
            "job_name": job_name, "step": "Stream logs", "bucket": bucket, "owner": owner,
            "decisive": decisive, "duplicate": False, "sig": "", "workflow": "wf",
            "link": f"https://example.invalid/job/{hash(job_name) % 10**9}",
        })
    return {"classifications": classifications, "cluster_todo": []}


def test_decisive_cases_are_kept_but_do_not_consume_slots():
    """已定性的 case 必须进报告，但不占 --max-cases 名额。"""
    import npu_ci_forensics as nf

    payload = _payload_with(
        decisive_items=[("jobA", ENTRY_MISSING_BUCKET, "code", True),
                        ("jobB", CASE_FAILED_BUCKET, "code", True)],
        other_items=[(f"job{i}", "超时", "mixed", False) for i in range(5)],
    )
    picked = nf.select_cases(payload, max_cases=2)
    names = [item["job_name"] for item in picked]
    assert "jobA" in names and "jobB" in names, f"已定性的 case 被静默剔除了：{names}"
    assert len(picked) == 4, f"名额 2 应给待取证者（另加 2 个已定性的）：{names}"
    assert names == ["jobA", "jobB"] + names[2:], "已定性的应排在最前（先读先办）"


def test_skip_cluster_result_makes_no_cluster_calls():
    """跳过路径返回的字典键必须与取证路径同构，且不带任何「查过集群」的痕迹。"""
    import npu_ci_forensics as nf

    skipped = nf.step3_skip_cluster({"bucket": ENTRY_MISSING_BUCKET})
    assert skipped["skipped"] is True
    assert skipped["pod_evidence"] is None and skipped["candidates"] == []
    assert skipped["not_obtained"] == [], "跳过不是「未取证」，不能留下未取证说明"
    for key in ("cluster_name", "kubeconfig_path", "availability", "logs", "queried_labels"):
        assert key in skipped, f"缺键 {key}：报告层会 KeyError"


def _cluster_section(text):
    """取出「#### 集群侧现场（第 2 步）」那一节（到下一个 #### 为止）。"""
    start = text.index("#### 集群侧现场（第 2 步）")
    rest = text[start:]
    nxt = rest.find("#### ", len("#### 集群侧现场（第 2 步）"))
    return rest if nxt == -1 else rest[:nxt]


def test_report_says_skipped_not_missing():
    """报告措辞：跳过的 case 里不得出现「未取证」「未解析出候选集群」字样。"""
    from forensics.report import render_case, synthesize


    case = {
        "job_name": "Nightly-A3 (PR) 17618", "workflow": "schedule_nightly_test_a3.yaml",
        "link": "https://example.invalid/job/1", "step": "Stream logs", "chip": "a3",
        "labels": [], "runner_name": "runner-x", "bucket": ENTRY_MISSING_BUCKET,
        "owner": "code", "sig": "file or directory not found",
        "cluster": {"skipped": True, "skip_reason": "日志侧已定性为业务侧（桶【X】，owner=code）",
                    "pod_evidence": None, "availability": None, "candidates": [],
                    "not_obtained": [], "logs": []},
        "history": [], "related_issues": [],
    }
    case["verdict"] = synthesize(case)
    text = "\n".join(render_case(case, 1))
    # ⚠️ 必须**只看第 2 步那一节**：判断依据（synthesize 的 basis）里也有「按规则跳过」，
    # 只看全文的话，即使 render_case 的跳过分支整个失效也照样变绿（已实测踩到这一点）。
    section = _cluster_section(text)
    assert "按规则跳过" in section, f"集群侧那一节没有写明跳过：\n{section}"
    assert "未取证" not in section, "跳过被写成了「未取证」——那是「查了没查到」，语义相反"
    assert "未解析出候选集群" not in section
    assert any("按规则跳过" in item for item in case["verdict"]["basis"]), \
        "判断依据里必须有「按规则跳过」这一条，否则读者以为集群侧默不作声"
    # 判据是测试框架自己的输出 → 置信度不应是「仅日志侧正则」那一档
    assert "决定性判据" in case["verdict"]["confidence"], case["verdict"]["confidence"]


def _conclusion_case(**overrides):
    """结论段（第 5 步）的最小 case：只带 synthesize/render_case 真正会读的键。"""
    from forensics.report import synthesize

    case = {
        "job_name": "Nightly-A3 (PR) 17618", "workflow": "schedule_nightly_test_a3.yaml",
        "link": "https://example.invalid/job/1", "step": "Stream logs", "chip": "a3",
        "labels": [], "runner_name": "runner-x", "bucket": ENTRY_MISSING_BUCKET,
        "owner": "code", "sig": "file or directory not found",
        "cluster": {"skipped": True, "skip_reason": "日志侧已定性为业务侧",
                    "pod_evidence": None, "availability": None, "candidates": [],
                    "not_obtained": [], "logs": []},
        "history": [], "related_issues": [],
    }
    case.update(overrides)
    case["verdict"] = synthesize(case)
    return case


def _conclusion_section(case):
    """渲染 case，只取「根因与修复建议」那一节的结论行（到「判断依据」为止）。

    必须切段：判断依据里也有桶名（`日志侧：命中桶【…】`），只看全文的话，
    根因行即使整条退回桶名也照样变绿。
    """
    from forensics.report import render_case

    text = "\n".join(render_case(case, 1))
    start = text.index("#### 根因与修复建议（第 5 步）")
    rest = text[start:]
    nxt = rest.find("**判断依据：**")
    return rest if nxt == -1 else rest[:nxt]


def test_root_cause_line_is_not_the_bucket():
    """给人读的「根因」行不得是桶名 —— 桶是投影，只用于统计/去重/派活。

    那行曾经就是桶名本身，于是「正则表序首个命中」被当成了结论。实测代价：冻结集上
    规则层自报结论时 80% 是错的（6/30），且同一种真因（aisbench 性能未达标）在不同
    噪声分布下拿到两个不同的桶，连带把 owner 判错、把问题派给错的队伍。
    """
    section = _conclusion_section(_conclusion_case())
    root_line = [line for line in section.splitlines() if line.startswith("- **根因**")]
    assert len(root_line) == 1, section
    assert ENTRY_MISSING_BUCKET not in root_line[0], f"根因行还是桶名：{root_line[0]}"
    assert "未能定性" in root_line[0], root_line[0]


def test_classification_line_exists_and_is_labelled_as_non_conclusion():
    """桶要留在报告里（人工复核要看），但必须显式标成「非结论」。"""
    section = _conclusion_section(_conclusion_case())
    class_line = [line for line in section.splitlines() if line.startswith("- **归类**")]
    assert len(class_line) == 1, section
    assert ENTRY_MISSING_BUCKET in class_line[0], class_line[0]
    assert "rule_regex" in class_line[0] and "非结论" in class_line[0], class_line[0]


def test_classification_line_never_renders_a_bare_none():
    """残缺 verdict stub 也会走 render_case（test_watch_state 就是这么用的）。"""
    case = {"job_name": "j", "workflow": "w", "link": "l", "step": "s",
            "labels": [], "cluster": {}, "history": [], "related_issues": [],
            "verdict": {"owning": "code"}}
    section = _conclusion_section(case)
    class_line = [line for line in section.splitlines() if line.startswith("- **归类**")]
    assert class_line and "None" not in class_line[0], class_line


def test_class_source_separates_peer_fallback_from_windowed_hits():
    """对端日志兜底来的桶是产物的尾部粗切、无时间窗对齐，证据强度低一档。"""
    from forensics.report import class_source
    assert class_source({"bucket": "X", "sig_source": "对端节点日志"}) == "peer_regex"
    assert class_source({"bucket": "X", "sig_source": "失败步骤窗口"}) == "rule_regex"
    assert class_source({"bucket": "X"}) == "rule_regex"
    assert class_source({"bucket": "未分类", "sig_source": "失败步骤窗口"}) == "none"
    assert class_source({}) == "none"


def test_basis_echoes_the_matched_line_so_a_keyword_hit_is_visible():
    """依据里要能看到正则**命中的原文** —— 读者才能自己判断是不是关键词抢中的。"""
    verdict = _conclusion_case()["verdict"]
    assert any("日志侧正则命中行：file or directory not found" in line
               for line in verdict["basis"]), verdict["basis"]


def test_empty_sig_leaves_no_dangling_evidence_line():
    """合成 case 常常没有 sig；留一行空的「命中行：」比不写更糟。"""
    verdict = _conclusion_case(bucket="未分类", sig="")["verdict"]
    assert not any("正则命中行" in line for line in verdict["basis"]), verdict["basis"]


# ---------------- 先例：是线索，不是结论 ----------------
#
# 这一组守的是「先例不得改写给人读的那一行」。产线实测：588 份报告里 #227 一条先例
# 被写成根因 241 次（41%），当前渲染的 118 条根因行里 56% 用先例当根因、其中 94% 写
# 最自信的「高度吻合」，而人工可核的样本 4/4 全错。原因不是检索质量，是结构：
# 「检索到同现象的 issue」推不出「本次失败就是这个原因」—— 知识库是有偏样本
# （只有值得写复盘的才进去），CI 失败有几千次。故此处**不许**再出现先例当结论；
# 但同时要守住「先例信息没丢」（§4 节、依据段、建议区都还在），否则就是另一头出错。

def _strong_precedent(number=227, strength="强"):
    """一条「强证据」先例的最小形态（字段与 IssueIndex.match 的返回同构）。"""
    return {
        "issue": {
            "number": number, "state": "OPEN", "url": f"https://example.invalid/issues/{number}",
            "title": "华为云 pypi 镜像 CDN 节点 index 页撕裂（12h TTL）导致 CI 构建失败",
            "is_postmortem": True, "root_cause_source": "body",
            "sections": {"root_cause": "CDN 节点 index 页撕裂。",
                         "fix": "重试取包", "prevention": "加长 TTL"},
            "platform_signals": ["内网源/镜像仓库可用性相关"],
        },
        "score": 352.06,
        "matched_signatures": ["vllm"],
        "mechanism_signatures": ["vllm"],
        "matched_keywords": ["vllm"],
        "core_keywords": [],
        "evidence_strength": strength,
        "has_root_cause": True,
    }


def _precedent_case(**overrides):
    overrides.setdefault("history", [_strong_precedent()])
    overrides.setdefault("owner", "code")
    # ⚠️ 这里**不能**沿用 _conclusion_case 的 skipped 集群：synthesize 的置信度 if/elif 链里
    # `cluster.skipped and bucket != "未分类"` 排在先例档**之前**，skipped 的 case 永远进不到
    # 先例那一档，于是「先例不抬置信度」这条断言会因为压根没走到那段代码而**假绿**
    # （实测：把先例档注入回去，skipped 版本仍然 28/28 全绿）。必须用「未跳过、未取到 pod」
    # 的形态，先例档才是可达的。
    overrides.setdefault("cluster", {
        "skipped": False, "skip_reason": None, "cluster_name": "EXAMPLE-CLUSTER",
        "kubeconfig_path": "/tmp/example.kubeconfig", "pod_evidence": None,
        "availability": None, "candidates": [], "not_obtained": [], "logs": [],
    })
    return _conclusion_case(**overrides)


def test_a_strong_precedent_does_not_become_the_root_cause():
    """「与历史先例 #N 高度吻合」不许再出现在根因行 —— 那是用先例代替取证。"""
    case = _precedent_case()
    root_line = [line for line in _conclusion_section(case).splitlines()
                 if line.startswith("- **根因**")]
    assert len(root_line) == 1, root_line
    assert "先例" not in root_line[0], root_line[0]
    assert "吻合" not in root_line[0], root_line[0]
    assert "未能定性" in root_line[0], root_line[0]


def test_a_precedent_does_not_raise_the_confidence_label():
    """置信度描述「归类有多可信」，不是「先例有多像」。混进来会造成措辞自相矛盾：
    根因行已写「未能定性」，置信度却说「中高（命中同签名的历史先例）」。"""
    verdict = _precedent_case()["verdict"]
    assert "先例" not in verdict["confidence"], verdict["confidence"]
    assert verdict["needs_human"] is True, "先例不是证据，人工复核信号不得丢失"


def test_the_precedent_survives_in_the_history_section_and_basis():
    """降级 ≠ 删除：先例必须仍在第 4 步逐条列出、仍在依据段里。"""
    from forensics.report import render_case

    case = _precedent_case()
    text = "\n".join(render_case(case, 1))
    assert "#### 历史问题定位（第 4 步）" in text
    assert "#227" in text, "先例从报告里整个消失了 —— 那是另一头出错"
    assert any("历史先例：#227" in line for line in case["verdict"]["basis"]), \
        case["verdict"]["basis"]
    assert case["verdict"]["precedent"]["number"] == 227


def test_the_precedents_fix_is_listed_but_labelled_as_not_adopted():
    """先例的修复记录仍列出（复盘正文是真实资产），但必须标出未采信。"""
    verdict = _precedent_case()["verdict"]
    fix_lines = [s for s in verdict["suggestions"] if "修复记录" in s]
    assert fix_lines, f"先例的修复记录整段丢了：{verdict['suggestions']}"
    assert "未" in fix_lines[0] and "采信" in fix_lines[0], fix_lines[0]
    assert "重试取包" in fix_lines[0], fix_lines[0]


def test_the_platform_lead_is_a_hint_not_an_evidence_conflict():
    """先例指向平台、日志侧判 code —— 这条值一次核对，不值一条把置信度打到「低」的
    「证据冲突」（先例不是证据层，它压不动置信度）。"""
    verdict = _precedent_case()["verdict"]
    assert not any("先例" in item for item in verdict["conflicts"]), verdict["conflicts"]
    assert any("平台侧动作" in item for item in verdict["hints_requiring_human"]), \
        verdict["hints_requiring_human"]
    assert "三层证据" not in verdict["confidence"], verdict["confidence"]


def test_a_weak_precedent_also_stays_out_of_the_root_cause():
    """「中」档（主题相近）同样不许进根因行 —— 两档一起删，不是只删强的那档。"""
    case = _precedent_case(history=[_strong_precedent(strength="中")])
    root_line = [line for line in _conclusion_section(case).splitlines()
                 if line.startswith("- **根因**")]
    assert "先例" not in root_line[0] and "未能定性" in root_line[0], root_line[0]
    assert "先例" not in case["verdict"]["confidence"], case["verdict"]["confidence"]


def test_a_precedent_below_the_score_floor_is_not_adopted_at_all():
    """分数低于门槛的条目本就不该被采信（弱线索另有提示），根因行也不该提到它。"""
    low = _strong_precedent(number=999)
    low["score"] = 12.0
    case = _precedent_case(history=[low])
    root_line = [line for line in _conclusion_section(case).splitlines()
                 if line.startswith("- **根因**")]
    assert "999" not in root_line[0], root_line[0]
    assert case["verdict"]["precedent"] is None, case["verdict"]["precedent"]


def test_pipeline_end_to_end_only_queries_cluster_for_non_decisive():
    """端到端：真跑一遍 npu_ci_forensics.main()，断言集群取证**只**被待取证的 case 触发。

    前面几个测试测的是各个零件，这一条测**接线**：main() 里那个三元表达式写反、
    或 select_cases 把已定性的 case 漏掉，都会在这里暴露。用 spy 包住 step3_cluster_forensics
    记录「谁去查了集群」——「提前退出」的定义就是这份名单里没有已定性的 job。
    全程离线：--handoff（不跑第 1 步）+ --offline + 本地 Cluster.md + 空 kubeconfig 目录。
    """
    import json
    import tempfile

    import npu_ci_forensics as nf

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pytest_verdict_e2e_"))
    for name, content in (("issues.json", {"issues": []}), ("comments.json", {"comments": {}})):
        (tmp / name).write_text(json.dumps(content), encoding="utf-8")
    (tmp / "Cluster.md").write_text("# 集群\n\n（本测试不需要真实集群）\n", encoding="utf-8")

    def classification(job_name, bucket, owner, decisive):
        return {"job_name": job_name, "workflow": "schedule_nightly_test_a3.yaml",
                "step": "Stream logs", "bucket": bucket, "owner": owner, "sig": "",
                "link": "https://example.invalid/job/1", "chip": "a3", "is_npu": True,
                "labels": [], "runner_name": "runner-x", "job_id": 1, "run_id": 2,
                "duplicate": False, "scanned": True, "windowed": True, "decisive": decisive,
                "failed_step_started_at": "2026-09-28T13:11:00Z",
                "failed_step_completed_at": "2026-09-28T13:11:41Z"}

    handoff = tmp / "handoff.json"
    handoff.write_text(json.dumps({
        "meta": {"repo": "vllm-project/vllm-ascend", "since": "2026-09-21", "chips": "a2,a3"},
        "failed_jobs": [], "cluster_todo": [],
        "classifications": [classification("Nightly-A3 (PR) 17618", ENTRY_MISSING_BUCKET, "code", True),
                            classification("Nightly-A2 调度失败", "多节点pod调度/就绪失败(k8s侧)", "infra", False)],
        "buckets": [],
    }), encoding="utf-8")

    queries = []
    original = nf.step3_cluster_forensics

    def spy(case, registry, sessions, args, errors, snapshots=None):
        queries.append(case.get("job_name"))
        return original(case, registry, sessions, args, errors, snapshots)

    nf.step3_cluster_forensics = spy
    argv = ["npu_ci_forensics.py", "--handoff", str(handoff), "--offline",
            "--cluster-md", str(tmp / "Cluster.md"),
            "--kubeconfig-dir", str(tmp / "kconf"), "--cache-dir", str(tmp),
            "--report-dir", str(tmp / "reports")]
    saved_argv = sys.argv
    sys.argv = argv
    try:
        code = nf.main()
    finally:
        sys.argv = saved_argv
        nf.step3_cluster_forensics = original

    assert code == 0, f"端到端跑失败，退出码 {code}"
    assert queries == ["Nightly-A2 调度失败"], f"已定性的 case 也去查集群了：{queries}"
    reports = sorted((tmp / "reports").glob("forensics_report_*.md"))
    assert reports, "没产出报告"
    text = reports[-1].read_text(encoding="utf-8")
    assert "按规则跳过" in text, "报告里没有写明跳过"
    assert ENTRY_MISSING_BUCKET in text, "已定性的 case 没出现在报告里（被静默丢弃了）"


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
    print(f"\n{len(tests) - failed}/{len(tests)} 通过（错误日志样本 {5} 份）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
