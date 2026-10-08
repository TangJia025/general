#!/usr/bin/env python3
"""报告精简版式的回归测试（2026-09-30）。

背景：取证报告把证据链做扎实了，但排版没有取舍 —— 实测
`npu_ci_reports/forensics_report_20260930_110918.md` 一份 1 个 case 的报告 **181 行 / 19097
字节**，其中 45 行是容器日志尾部整段复制（内容是 `HostContext: Well known directory` 这类
INFO）、26 行是**引用来的历史 issue 正文**、15 条硬编码局限全量输出、判断依据与上面段落逐句
重复。四项决议：单一精简版（完整证据留在同名 json）、局限按需触发、容器日志默认不贴、引用
正文降级单行。

这组测试守住五件事：
  1) 引用别人的正文时剥掉标题、压成单行 —— 历史 issue 正文里的 `#### 1. 路径映射不一致`
     会**冒充本报告的章节**（在编辑器大纲里看就是报告的一节），这是本次要修的直接缺陷；
  2) 容器日志只留「怎么取全文 + ≤3 行样例」，且样例优先取**命中本 case 签名的行**
     （快照场景尾部全是 INFO 噪音，贴尾部等于没贴）；对端节点那个 `<details>` 是证据，**不动**；
  3) 历史匹配只展开前 2 条 + 一行汇总；「命中关键词」是 `aarch64, linux, open` 这类通用词，删；
  4) 判断依据按 `basis_tags` 去重（对端/pod 判定/标签可用性三类已在上面的段落里渲染过），
     但**只在对应段落确实渲染了**时才跳过 —— 否则会把独一份的证据也删掉；
  5) 局限条目按本次运行的事实触发：无触发时不留空节，`text` 与 `triggered` 全量进 json。

运行：python3 tests/test_report_slimming.py      （无需 pytest，也兼容 pytest）
"""
import pathlib
import re
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
sys.path.insert(0, str(BASE_DIR))

from forensics import report as report_mod                                # noqa: E402
from forensics.limitations import (LIMITATIONS, limitation_facts,          # noqa: E402
                                   select_limitations)
from forensics.report import (_oneline, _sample_log_lines, render_case,    # noqa: E402
                              render_report, synthesize)

# 本报告自己的节标题：除这些之外，case 段里**不允许**再出现 `#### ` 行
OWN_SECTION_TITLES = {"#### 对端节点日志（第 1 步第二证据源）", "#### 集群侧现场（第 2 步）",
                      "#### 历史问题定位（第 4 步）", "#### 根因与修复建议（第 5 步）"}

# 真实引用形态：历史 issue #146 的正文（含 markdown 标题与多行小标题）
QUOTED_ROOT_CAUSE = (
    "`kubernetes-novolume` 模式移除了共享 PVC，改用 Kubernetes exec API + tar 传输文件。\n"
    "\n"
    "#### 1. 路径映射不一致\n"
    "\n"
    "Runner Pod 的 work 目录路径为 `/home/runner/_work`，而 Job Pod 的 work 卷挂载路径不同。\n"
    "\n"
    "#### 2. 传输中断\n"
    "\n"
    "大文件传输时连接会被重置。"
)

# 真实容器日志形态：快照取自 job 结束前，尾部 40 行全是这类 INFO（实测 job 109709036800）
SNAPSHOT_LOG_TAIL = "\n".join(
    f"[WORKER 2026-09-30 03:0{i % 10}:{i:02d}Z INFO HostContext] Well known directory 'Root': '/home/runner'"
    for i in range(40))

# 命中桶签名的行（本例：ld.so 预加载告警）
SIGNATURE_LINE = ("/usr/local/lib/libtorch_npu.so: _PRELOAD cannot be preloaded "
                  "(cannot open shared object file): ignored.")


def make_history(count: int) -> list:
    """构造 count 条历史匹配，第 1 条带引用正文（含 markdown 标题）。"""
    matches = []
    for position in range(count):
        matches.append({
            "score": 105.55 - position * 10,
            "evidence_strength": "中" if position == 0 else "弱",
            "matched_signatures": ["exitcode:137"] if position == 0 else [],
            "core_keywords": ["shared"] if position == 0 else [],
            "matched_keywords": ["aarch64", "linux", "open", "run"],
            "issue": {
                "number": 192 - position, "title": f"第 {position} 条历史 issue", "state": "OPEN",
                "url": "https://example.invalid/192", "is_postmortem": position == 0,
                "root_cause_source": "comment" if position == 0 else None,
                "sections": {"root_cause": QUOTED_ROOT_CAUSE if position == 0 else ""},
            },
        })
    return matches


def make_case(**overrides) -> dict:
    """与真实 case 同构的最小 case：集群侧拿到 pod（含容器日志）+ 历史匹配。"""
    case = {
        "job_name": "single-node (main, demo)", "workflow": "schedule_weekly_test_a3.yaml",
        "link": "https://github.com/vllm-project/vllm-ascend/actions/runs/36658382517/job/109709036800",
        "job_id": 109709036800, "run_id": 36658382517, "repo": "vllm-project/vllm-ascend",
        "step": "Run Pytest (YAML-driven)", "chip": "a3",
        "labels": ["linux-aarch64-a3-800i-16"], "runner_name": "linux-aarch64-a3-800i-16-runner-abcde",
        "bucket": "自定义算子so缺失(csrc构建)", "owner": "mixed",
        "sig": "_PRELOAD cannot be preloaded (cannot open shared object file): ignored.",
        "window": {"started_at": "2026-09-30T02:58:16Z", "completed_at": "2026-09-30T03:08:27Z"},
        "cluster": {
            "cluster_name": "ascend-cn12-001-cluster", "namespace": "vllm-project",
            "filename": "k.yaml", "kubeconfig_path": "/home/x/k.yaml", "match_kind": "runner 标签唯一匹配",
            "pod_evidence": {"pod": "p-1", "phase": "Running", "node": "mind-third-ci",
                             "start_time": "2026-09-30T02:29:29Z", "created_time": "2026-09-30T02:14:08Z",
                             "containers": [{"container": "runner", "state": "running",
                                             "exit_code": None, "restart_count": 0}]},
            "logs": [{"container": "runner", "source": "当前实例", "ok": True,
                      "text": SNAPSHOT_LOG_TAIL + "\n" + SIGNATURE_LINE}],
        },
        "history": make_history(6),
        "related_issues": [],
    }
    case.update(overrides)
    return case


# ------------------------------------------------- ① 引用正文：剥标题、压单行

def test_oneline_strips_markdown_headings():
    # 用足够大的 limit 看全量：默认 120 是给报告用的截断长度，这里验的是「剥标题」本身
    got = _oneline(QUOTED_ROOT_CAUSE, 400)
    assert "#" not in got, f"引用正文里的标题没被剥掉：{got}"
    assert "\n" not in got, "引用正文必须压成单行"
    assert "路径映射不一致" in got and "传输中断" in got, f"剥标题不该丢内容：{got}"
    # 报告里用的是默认截断长度：引用再长也只占一行
    assert len(_oneline(QUOTED_ROOT_CAUSE)) <= 121, _oneline(QUOTED_ROOT_CAUSE)


def test_oneline_folds_newlines_and_truncates():
    got = _oneline("第一行\n\n第二行", 100)
    assert got == "第一行 / 第二行", got
    long_text = "长" * 200
    got = _oneline(long_text, 120)
    assert len(got) == 121 and got.endswith("…"), f"超长必须截断并加省略号：{len(got)}"
    assert _oneline("") == "" and _oneline(None) == ""


def test_render_case_has_no_borrowed_section_headings():
    """端到端守门：case 段里除本报告自己的节标题外，不得出现任何 `#### ` 行。

    这正是本次要修的缺陷 —— 引用历史正文时把它的 `#### 1. 路径映射不一致` 原样带了出来，
    读者在编辑器大纲里会以为那是本报告的一节。
    """
    rendered = render_case(make_case(), 1)
    borrowed = [line for line in rendered
                if re.match(r"^#{1,6} ", line) and line not in OWN_SECTION_TITLES
                and not line.startswith("### case ")]
    assert not borrowed, f"case 段里出现了冒充章节的标题行：{borrowed}"


# ------------------------------------------------- ② 容器日志：取全文 + ≤3 行样例

def test_sample_log_lines_prefers_signature_hits():
    text = SNAPSHOT_LOG_TAIL + "\n" + SIGNATURE_LINE
    sample = _sample_log_lines(text, SIGNATURE_LINE)
    assert sample and sample[0] == SIGNATURE_LINE, f"有命中行时不该退化成尾部样例：{sample}"
    assert len(sample) <= 3, sample


def test_sample_log_lines_falls_back_to_tail():
    sample = _sample_log_lines(SNAPSHOT_LOG_TAIL, "没有这行")
    assert sample == SNAPSHOT_LOG_TAIL.splitlines()[-3:], sample
    assert len(_sample_log_lines(SNAPSHOT_LOG_TAIL, "")) == 3
    assert _sample_log_lines("", "x") == []


def test_render_case_log_tail_replaced_by_sample_and_commands():
    """容器日志段：不贴整段尾部，给「共 N 行 + 取全文命令 + json 指针 + 样例」。"""
    rendered = render_case(make_case(), 1)
    text = "\n".join(rendered)
    # 整段尾部（40 行）不得出现：只允许 ≤3 行样例
    assert text.count("Well known directory") <= 3, \
        f"容器日志尾部仍被整段贴进报告：{text.count('Well known directory')} 行"
    assert "共 41 行" in text, f"必须给出总行数：{text}"
    assert "cases[0].cluster.logs[0].text" in text, "必须给出 json 检索路径"
    assert "gh api repos/vllm-project/vllm-ascend/actions/jobs/109709036800/logs" in text, \
        "必须给出本 job 控制台日志的取回命令"
    assert "kubectl --kubeconfig /home/x/k.yaml logs p-1 -n vllm-project -c runner" in text, \
        "必须给出集群侧容器日志的取回命令"
    # 旧的折叠块没了（容器日志），且样例是命中签名的行
    assert "<details><summary>容器 runner 日志尾部" not in text, "容器日志的 details 块应已删除"
    assert SIGNATURE_LINE in hit_lines(rendered), "样例应优先取命中签名的行"


def hit_lines(rendered: list) -> list:
    return [line for line in rendered if SIGNATURE_LINE in line]


def test_peer_details_block_is_kept():
    """⚠️ 防误伤：精简删的是**容器日志**的 details，对端节点那个 details 是证据，不许一起删。"""
    case = make_case()
    case["peer"] = {"ok": True, "empty": False, "artifact": "main-demo-ascend-logs",
                    "artifact_id": 123, "peers": ["node1"], "node_lines": {"node1": 839},
                    "kept_lines": {"node1": 400}, "bucket": "Store 会合超时",
                    "sig": "recvValueWithTimeout failed", "adopted": True}
    case["sig_source"] = "对端节点日志"
    text = "\n".join(render_case(case, 1))
    assert "<details><summary>对端节点命中片段原文</summary>" in text, text
    assert "命中片段" in text and "首条异常" not in text, text


def test_render_case_line_budget():
    """行数预算：这份 fixture 的 case 段渲染结果 ≤ 60 行。

    定这个数是为了让「报告又长回去」这件事在回归里立刻可见 —— 精简前同形态的 case 段
    （容器日志整段 + 6 条历史 + 全量依据）是 130 行以上。留约 15% 余量，正常增删细节不会误报。
    """
    rendered = render_case(make_case(), 1)
    assert len(rendered) <= 60, f"case 段 {len(rendered)} 行，超出行数预算：\n" + "\n".join(rendered)


# ------------------------------------------------- ③ 历史：Top-2 + 汇总

def test_history_shows_top2_and_summarises_the_rest():
    rendered = render_case(make_case(), 1)
    text = "\n".join(rendered)
    shown = [line for line in rendered if re.match(r"^- .*#\d+（score=", line)]
    assert len(shown) == 2, f"历史匹配只应展开前 2 条：{shown}"
    assert "#192" in shown[0], shown
    assert "另有 4 条弱命中（最高 #190 score=85.55）" in text, \
        f"其余必须给一行汇总：{text[text.find('另有'):]}"
    assert "cases[0].history" in text, "汇总行必须给 json 检索路径"
    # 通用词噪音不再出现
    assert "命中关键词" not in text, "「命中关键词」是 aarch64/linux 这类通用词，应删"
    assert "命中签名" in text and "命中核心症状词" in text, "机制证据（签名/症状词）必须保留"


def test_history_top2_quote_is_one_line():
    rendered = render_case(make_case(), 1)
    root_cause_lines = [line for line in rendered if "历史根因：" in line]
    assert root_cause_lines, rendered
    for line in root_cause_lines:
        assert line.startswith("  - 历史根因："), line
        assert "####" not in line, line


# ------------------------------------------------- ④ 判断依据去重

def test_basis_tags_align_with_basis():
    verdict = synthesize(make_case())
    assert len(verdict["basis_tags"]) == len(verdict["basis"]), \
        (len(verdict["basis"]), len(verdict["basis_tags"]))


def test_basis_dedup_only_skips_sections_that_were_rendered():
    """去重的两个方向都要守：段落渲染了就跳过重复句；段落没渲染就必须留下。"""
    case = make_case()
    # 把 pod 证据的判定挪到 availability 分支上：这次走的是「标签可用性核查」
    case["cluster"] = {"cluster_name": "c", "availability_cluster": "c",
                       "availability": {"checked": True, "available": True, "match_kind": "标签主干",
                                        "claimed_label": "l", "registered_suffix": "cn12-001",
                                        "suffix_variants": {"chlqk": 4}, "runners_online": 4,
                                        "listeners": 2, "namespaces": ["vllm-project"]}}
    case["verdict"] = synthesize(case)
    rendered = render_case(case, 1)
    text = "\n".join(rendered)
    assert text.count("标签族有效") == 1, f"同一句依据在报告里出现了两次：{text.count('标签族有效')}"
    assert "未找到任何匹配 pod" not in text
    # 段落没渲染时（集群段整个缺失），依据里的集群叙述不能跟着被删掉：
    # 这里换成「judgment 里没被跳过的日志侧基线」作对照，确认依据整体仍在渲染
    empty_cluster = make_case()
    empty_cluster["cluster"] = {}
    empty_cluster["verdict"] = synthesize(empty_cluster)
    text = "\n".join(render_case(empty_cluster, 1))
    assert text.count("日志侧：命中桶") == 1, f"依据没被渲染：{text[text.find('判断依据'):]}"
    # json 侧必须仍是全量：去重只发生在 md（这里 2 条 = 日志侧基线 + 历史先例，两条都该渲染）
    assert len(empty_cluster["verdict"]["basis"]) == 2, empty_cluster["verdict"]["basis"]


def test_peer_basis_is_not_repeated_in_verdict_section():
    case = make_case()
    case["peer"] = {"ok": True, "empty": False, "artifact": "a", "peers": ["node1"],
                    "node_lines": {"node1": 839}, "kept_lines": {"node1": 400},
                    "bucket": "B", "sig": "s", "adopted": True}
    case["sig_source"] = "对端节点日志"
    case["verdict"] = synthesize(case)
    rendered = render_case(case, 1)
    text = "\n".join(rendered)
    # 对端那段是证据，渲染在「对端节点日志」小节里；判断依据里不再抄一遍
    assert text.count("命中桶【B】") == 1, f"对端证据被抄了两遍：{text.count('命中桶【B】')}"
    # 而 json 里的依据仍是全量（含那条被跳过的对端证据）
    assert any("命中桶【B】" in item for item in case["verdict"]["basis"]), case["verdict"]["basis"]


# ------------------------------------------------- ⑤ 局限按需触发

def test_select_limitations_marks_only_triggered():
    case = make_case()
    selected = select_limitations([case])
    by_id = {item["id"]: item["triggered"] for item in selected}
    assert len(selected) == len(LIMITATIONS), "必须返回全量（json 要落全部条目 + 标志）"
    assert by_id["availability_is_snapshot"] is False, "本 fixture 没走标签可用性核查"
    assert by_id["job_log_only_node0"] is False, "本 fixture 没有对端产物"
    assert by_id["history_coverage"] is True, "本 fixture 有历史匹配"
    assert by_id["high_score_not_same_issue"] is True, "本 fixture 有弱证据的中高分匹配"
    assert by_id["pod_recycled"] is False, "本 fixture 拿到了 pod 实证"
    facts = limitation_facts([case])
    assert facts["cluster_queried"] is True, "本 fixture 确实用 kubeconfig 查过集群"
    assert facts["peer_attempted"] is False


def test_limitation_facts_on_empty_run():
    facts = limitation_facts([])
    assert not any(facts.values()), f"空运行不该触发任何局限：{facts}"
    selected = select_limitations([])
    assert not any(item["triggered"] for item in selected)


def test_render_report_never_leaves_empty_limitations_section():
    """无触发时不留空节：必须明确写「未触发」并给完整清单的指针。"""
    text = render_report([], {"repo": "r/x", "generated_at": "t"}, [], {}, {"limitations": select_limitations([])})
    assert "## 能力边界与局限" in text, text
    assert "本次运行未触发任何已知局限条目" in text, text
    assert "npu_ci_forensics_design.md" in text, "必须给完整清单的指针"


def test_render_report_shows_only_triggered_limitations():
    case = make_case()
    case["cluster"]["skipped"] = True
    case["cluster"]["skip_reason"] = "pytest 判定行已定性"
    case["verdict"] = synthesize(case)
    text = render_report([case], {"repo": "r/x", "generated_at": "t"}, [], {},
                         {"limitations": select_limitations([case])})
    body = text.split("## 能力边界与局限")[1]
    assert "按规则跳过集群取证" in body, "触发的条目必须出现"
    assert "登记全名与 pod 实际命名可能对不上" not in body, \
        f"未触发的条目不该出现（本次没做标签主干匹配）：{body}"
    assert "局限共 15 条" in body, body


def test_render_report_prints_json_path_in_header():
    meta = {"repo": "r/x", "generated_at": "t", "json_path": "/tmp/forensics_report_1.json"}
    text = render_report([], meta, [], {}, {"limitations": select_limitations([])})
    assert "/tmp/forensics_report_1.json" in text.split("## 摘要")[0], text


def test_availability_table_groups_clusters_without_kubeconfig():
    plan = [{"cluster": "a", "has_kubeconfig": True, "server": "https://a:5443"},
            {"cluster": "b", "has_kubeconfig": False, "warning": "无 kubeconfig"},
            {"cluster": "c", "has_kubeconfig": False, "warning": "无 kubeconfig"}]
    text = render_report([], {"repo": "r/x", "generated_at": "t"}, plan, {},
                         {"limitations": select_limitations([])})
    table = text.split("## 集群取证可用性")[1].split("##")[0]
    assert table.count("❌") == 0, f"缺 kubeconfig 的集群不该逐行占位：{table}"
    assert "| `a` | ✅ |" in table, table
    assert "无 kubeconfig 的集群 2 个：`b`、`c`" in table, table


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
    print(f"\n{len(tests) - failed}/{len(tests)} 通过（报告精简版式）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
