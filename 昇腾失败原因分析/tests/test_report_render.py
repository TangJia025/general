#!/usr/bin/env python3
"""报告渲染层的 markdown 结构回归测试（forensics/report.py 的 flat_excerpt 与第 4 步）。

为什么需要这组测试：
    历史 issue 的正文是**第三方写的 markdown**，本工具把它摘录 300 字原样拼进报告。
    原先的写法是按**字符数**硬切（`text[:300]`）—— 正文里带 ``` 围栏的日志摘录一旦
    闭合围栏落在第 300 字之后，就会被切掉，在报告里留下一根**悬空的 ```**。后果不是
    「少了一行」，而是**其后整份报告被渲染成一个代码块**：

        #### 根因与修复建议（第 5 步）      ← 连同这一节、修复建议、能力边界
        - **根因**：……                       ← 全被吞进 <pre> 里

    读者看到的正是「根因和上面的日志糊在一起、没有独立章节」。实测 586 份报告里
    40 份（6.8%）中招，且当天占比过半（9 份里 5 份）—— 因为「先例降为线索」之后，
    带正文摘录的 issue 变多，撞上悬空围栏的机会随之变多。

    三个方向都必须钉住，缺一不可：
      - 悬空围栏**不许**再出现（否则报告尾部整段不可读）；
      - 摘录**不许**逃出 `  - 历史根因：` 这个列表项（多行正文会把 #### 变成真标题）；
      - 反方向也要防：只留下**闭合**围栏的正文会关掉文档里本该开着的块，同样是结构破坏。

    所有 fixture 均为**合成**，绝不读 npu_ci_reports/ 或 .forensics_cache/
    （那里是内网 issue 原文与集群信息，本仓是 PUBLIC）。

运行：python3 tests/test_report_render.py      （无需 pytest，也兼容 pytest）
"""
import pathlib
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR.parent))

from forensics.report import flat_excerpt, render_case, render_report  # noqa: E402

# 「开围栏落在 300 字以内、闭围栏落在 300 字以外」——线上真实的坏法就长这样。
# 用 TEST-NET 网段与 example.invalid，不写任何真实地址。
FENCED_ROOT_CAUSE = (
    "节点 `kubelet.log`（192.0.2.10）直接证据：\n"
    "\n"
    "```\n"
    "E0921 03:06:23 Failed to execute PostStartHook: err=\"failed to exec in container: "
    "failed to drain exec process \\\"...\\\" io: failed to drain exec process io in 2s "
    "because io is still held by other processes\"\n"
    "→ Killing container \"job\" (gracefully, grace period 30s)\n"
    "```\n"
    "\n"
    "宿主机上残留的 npu-smi 进程持有 exec 管道 fd，故 postStart 钩子必然超时 —— "
    "这一段足够长，把闭合围栏推到第 300 字之后。\n"
)

# 反方向：只有闭合围栏，没有开围栏（同样能破坏文档状态）
CLOSING_ONLY_ROOT_CAUSE = "复盘记录里贴了半截日志：\n```\n然后就没贴完。\n"


def _case(**overrides):
    """一份最小可渲染的 case；只放渲染层真的会读到的字段。"""
    case = {
        "workflow": "schedule_weekly_test_a3.yaml", "job_name": "Weekly-A3 性能",
        "job_id": 114638523163, "run_id": 38195565830,
        "link": "https://example.invalid/job/114638523163",
        "bucket": "断言失败(代码或精度)", "owner": "code",
        "sig": "=== 1 failed ===", "step": "Run Pytest", "chip": "a3",
        "labels": [], "runner_name": None, "is_npu": True, "peer": None,
        "sig_source": "node0",
        "window": {"started_at": "2026-10-12T02:56:25Z",
                   "completed_at": "2026-10-12T03:19:25Z"},
        "cluster": {"skipped": False, "cluster_name": "EXAMPLE-CLUSTER",
                    "kubeconfig_path": "/tmp/example.kubeconfig", "pod_evidence": None,
                    "availability": None, "candidates": [], "not_obtained": [], "logs": []},
        "history": [], "related_issues": [],
        "verdict": {"root_cause": "样例根因", "owner": "code",
                    "confidence": "中", "conflicts": [], "needs_human": False},
    }
    case.update(overrides)
    return case


def _history(root_cause):
    """按分数列出的历史先例（第 4 步的第一种形态）。"""
    return [{
        "issue": {"number": 257, "title": "CI workflow pod 偶发 exit 137", "url": "https://example.invalid/issues/257",
                  "state": "CLOSED", "labels": [], "is_postmortem": True,
                  "root_cause_source": "body", "sections": {"root_cause": root_cause}},
        "score": 26.41, "evidence_strength": "弱", "matched_signatures": ["exitcode:137"],
        "core_keywords": [], "matched_keywords": ["glm", "yaml"],
    }]


def _related(root_cause):
    """人工策展的历史关联（第 4 步的第二种形态，另一个调用点）。"""
    return [{"number": 238, "title": "找不到缓存模型", "url": "https://example.invalid/issues/238",
             "state": "CLOSED", "root_cause_source": "body", "why": "同一现象换说法",
             "root_cause": root_cause}]


def _render(case):
    """走真实的 render_report —— 要验的是「整份报告的结构」，不是单行。"""
    meta = {"repo": "vllm-project/vllm-ascend", "since": "2026-10-05", "chips": "a2,a3",
            "generated_at": "2026-10-12 11:23:12", "handoff_source": "/tmp/handoff.json",
            "total_jobs": 1}
    return render_report([case], meta, [], {}, {"limitations": [], "errors": []})


def _fence_balance(text):
    """扫一遍得到围栏开合次数；返回 (开, 闭)。markdown 里 ``` 是对称的，开=闭才正常。"""
    opened = sum(1 for line in text.splitlines() if line.startswith("```"))
    return opened


# ---------------- 1. flat_excerpt 本体 ----------------

def test_flat_excerpt_neutralizes_fences():
    """反引号一个都不留 —— 围栏与行内代码同时被中和，摘录再也不可能改变文档状态。"""
    flat = flat_excerpt(FENCED_ROOT_CAUSE)
    assert "`" not in flat, f"摘录里仍残留反引号：{flat!r}"


def test_flat_excerpt_is_single_line():
    """必须压成单行：多行会把摘录甩出 `  - 历史根因：` 列表项，`####` 会变成真标题。"""
    flat = flat_excerpt(FENCED_ROOT_CAUSE)
    assert "\n" not in flat
    assert flat.startswith("节点 'kubelet.log'")


def test_flat_excerpt_marks_truncation():
    """超长要显式标截断，且不切在半个单词上（原先切出 `(gra`）。"""
    flat = flat_excerpt(FENCED_ROOT_CAUSE)
    assert flat.endswith("…（截断）"), f"没有截断标记：{flat!r}"
    assert len(flat) <= 300 + len("…（截断）")


def test_flat_excerpt_keeps_short_text_intact():
    """够短就一个字不动 —— 别把正常的摘录也改了。"""
    assert flat_excerpt("显存不足导致 OOM") == "显存不足导致 OOM"


# ---------------- 2. 回归主钉：整份报告的围栏必须闭合 ----------------

def test_report_fences_stay_balanced_on_fenced_excerpt():
    """主钉：正文带悬空围栏时，整份报告的围栏仍必须闭合。

    改前这里会红：围栏数是奇数，`#### 根因与修复建议（第 5 步）` 及其后全部落在代码块里。
    """
    text = _render(_case(history=_history(FENCED_ROOT_CAUSE)))
    assert _fence_balance(text) % 2 == 0, "围栏数为奇数 —— 有悬空围栏，报告尾部会被吞成代码块"


def test_report_fences_stay_balanced_on_closing_only_excerpt():
    """反方向：只有闭合围栏的正文也不能关掉文档里本该开着的块。"""
    text = _render(_case(history=_history(CLOSING_ONLY_ROOT_CAUSE)))
    assert _fence_balance(text) % 2 == 0


def test_report_fences_stay_balanced_on_curated_related():
    """第二个调用点（人工策展关联）走的是另一行代码，必须单独钉。"""
    text = _render(_case(related_issues=_related(FENCED_ROOT_CAUSE)))
    assert _fence_balance(text) % 2 == 0


# ---------------- 3. 症状钉：根因必须仍是独立章节 ----------------

def test_root_cause_section_survives():
    """用户看到的症状就是这个：`#### 根因与修复建议（第 5 步）` 必须有独立标题行。"""
    text = _render(_case(history=_history(FENCED_ROOT_CAUSE)))
    assert "\n#### 根因与修复建议（第 5 步）\n" in text
    assert "\n## 能力边界与局限\n" in text


def test_excerpt_does_not_escape_its_list_item():
    """摘录必须老实待在 `  - 历史根因：` 一行里，不许甩出新行（那会变成游离段落）。"""
    lines = render_case(_case(history=_history(FENCED_ROOT_CAUSE)), 1)
    hits = [line for line in lines if line.startswith("  - 历史根因：")]
    assert len(hits) == 1, f"历史根因行应恰好 1 行，实得 {len(hits)} 行"
    assert "kubelet.log" in hits[0]
    # 摘录里的 #### 不许变成真标题
    assert not any(line.startswith("####") and "kubelet" in line for line in lines)


def main():
    tests = [(name, func) for name, func in sorted(globals().items())
             if name.startswith("test_") and callable(func)]
    failed = []
    for name, func in tests:
        try:
            func()
            print(f"✅ {name}")
        except Exception as exc:
            failed.append(name)
            detail = str(exc) if isinstance(exc, AssertionError) else f"{type(exc).__name__}: {exc}"
            print(f"❌ {name}: {detail}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} 通过" + (f"，失败：{failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
