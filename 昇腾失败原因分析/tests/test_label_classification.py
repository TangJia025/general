#!/usr/bin/env python3
"""
runner 标签分类回归测试：--npu-label-pattern（is_npu）与 chip_of()（chip）必须一致。

为什么需要这组测试（设计文档 §2.3.1 的踩坑记录）：
  is_npu 与 chip 由**两个独立正则**判定，一旦不一致就会在报告里自相矛盾——实测出现过
  [gate]（语义是「NPU job 被 skip」）与 chip=a3 同时出现，即 A3 的 NPU job 被标成 CPU 门禁。
  已实测踩过两次同类坑：
    1. 缺 `nightly-` 中缀 → linux-aarch64-nightly-a3-* 整池（524 次，第二大池）被判成 CPU 门禁；
    2. 缺 `910b` 芯片族 → 整族 NPU 标签被判成 CPU 门禁。
  两次都是「用采样到的少量标签验证」漏掉的，所以真值集必须用**权威全量**：
  ascend-gha-runners/docs 的 docs/assets/problem-labels.json（19 仓 102 个标签）。

真值集更新方式：重新拉取上述文件覆盖 tests/problem_labels.json，再跑本测试。
标签种类变化会让本测试失败——这正是它的作用（提示去改 CHIP_FAMILY_TO_CHIP）。

运行：python3 tests/test_label_classification.py      （无需 pytest，也兼容 pytest）
"""
import ast
import json
import pathlib
import re
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
SOURCE = TEST_DIR.parent / "npu_ci_failure_analysis.py"
FIXTURE = TEST_DIR / "problem_labels.json"

# 被测常量与函数：npu_ci_failure_analysis.py 是**全模块级执行**的脚本（import 即联网跑完整
# 分析），无法安全 import，因此用 AST 只抽取被测的那几个节点单独 exec，生产代码零改动。
WANTED_ASSIGNMENTS = {
    "CHIP_FAMILY_TO_CHIP", "KNOWN_CHIPS", "NPU_ARCH", "NPU_CHIP_ALT", "NPU_LABEL_PATTERN",
}
WANTED_FUNCTIONS = {"chip_of"}


def load_under_test():
    """从源文件抽取被测常量与 chip_of，返回其命名空间。"""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    namespace = {"re": re}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if targets & WANTED_ASSIGNMENTS:
                exec(compile(ast.Module([node], []), str(SOURCE), "exec"), namespace)
        elif isinstance(node, ast.FunctionDef) and node.name in WANTED_FUNCTIONS:
            exec(compile(ast.Module([node], []), str(SOURCE), "exec"), namespace)
    missing = (WANTED_ASSIGNMENTS - set(namespace)) | (WANTED_FUNCTIONS - set(namespace))
    if missing:
        raise AssertionError(f"未能从 {SOURCE} 抽取到：{sorted(missing)}（源文件结构可能已变）")
    return namespace


NS = load_under_test()
NPU_LABEL_PATTERN = NS["NPU_LABEL_PATTERN"]
NPU_LABEL = re.compile(NPU_LABEL_PATTERN)
chip_of = NS["chip_of"]
CHIP_FAMILY_TO_CHIP = NS["CHIP_FAMILY_TO_CHIP"]
KNOWN_CHIPS = NS["KNOWN_CHIPS"]


def load_fixture():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    repos = data["repos"]
    all_labels = sorted({label for labels in repos.values() for label in labels})
    # CPU 池：标签中带 `-cpu-` 段（cpu-4-hk / cpu-4-cn12-001 / cpu-4-buildkit-… 等形态）
    cpu_labels = [l for l in all_labels if "-cpu-" in l]
    npu_labels = [l for l in all_labels if "-cpu-" not in l]
    return repos, all_labels, npu_labels, cpu_labels


REPOS, ALL_LABELS, NPU_LABELS, CPU_LABELS = load_fixture()


def test_fixture_is_non_trivial():
    """防止真值集被清空后测试「全绿」的假通过。"""
    assert len(REPOS) >= 10, f"真值集仓库数异常少：{len(REPOS)}"
    assert len(ALL_LABELS) >= 80, f"真值集标签数异常少：{len(ALL_LABELS)}"
    assert NPU_LABELS and CPU_LABELS, "真值集必须同时含 NPU 与 CPU 标签"


def test_all_npu_labels_match():
    """权威真值集里的每个 NPU 标签都必须被 --npu-label-pattern 命中（无漏判）。"""
    missed = [l for l in NPU_LABELS if not NPU_LABEL.search(l)]
    assert not missed, (
        f"{len(missed)}/{len(NPU_LABELS)} 个 NPU 标签未被匹配——"
        f"这些 job 会被误标 [gate] 且漏出排队时长统计：\n  " + "\n  ".join(missed)
    )


def test_all_cpu_labels_excluded():
    """CPU 池标签必须全部排除（无过判）。"""
    hit = [l for l in CPU_LABELS if NPU_LABEL.search(l)]
    assert not hit, f"{len(hit)} 个 CPU 标签被误判为 NPU：\n  " + "\n  ".join(hit)


def test_chip_extractable_for_every_npu_label():
    """一致性核心断言：判为 NPU 的标签必须能提取出芯片。

    否则报告会出现 [NPU] 与 chip=None 并存——即 is_npu 与 chip 两个正则已经分叉。
    """
    inconsistent = [l for l in NPU_LABELS if chip_of([l]) is None]
    assert not inconsistent, (
        f"{len(inconsistent)} 个标签判为 NPU 但 chip_of() 提取不出芯片（两个正则已分叉）：\n  "
        + "\n  ".join(inconsistent)
    )


def test_chip_none_for_cpu_labels():
    """CPU 标签不应被赋予芯片（chip=None 是报告里 [gate] 的判定依据之一）。"""
    bad = [(l, chip_of([l])) for l in CPU_LABELS if chip_of([l]) is not None]
    assert not bad, f"CPU 标签被判出了芯片：{bad}"


def test_chip_result_is_known():
    """chip_of 的返回值必须落在 KNOWN_CHIPS 内（否则 --chips 过滤会失效）。"""
    unknown = sorted({c for l in NPU_LABELS for c in [chip_of([l])] if c not in KNOWN_CHIPS})
    assert not unknown, f"chip_of 返回了不在 KNOWN_CHIPS 中的值：{unknown}"
    for family, chip in CHIP_FAMILY_TO_CHIP.items():
        assert chip in KNOWN_CHIPS, f"芯片族 {family} 归一为 {chip}，但它不在 KNOWN_CHIPS 里"


# 两次实测踩坑的**具名回归用例**：这些标签必须在真值集里被显式覆盖，
# 即使将来真值集被裁剪，也能挡住这两类 bug 回归。
REGRESSION_CASES = [
    # (标签, 期望芯片)  —— 期望芯片为 None 表示应判为非 NPU
    ("linux-aarch64-nightly-a3-16", "a3"),     # 中缀 nightly-：曾整池（524 次）被判成 CPU 门禁
    ("linux-aarch64-a2b3-v-half", "a2"),       # 无卡数后缀 + A2 板型：曾被漏判
    ("linux-aarch64-a2b3-v-quarter", "a2"),
    ("linux-aarch64-910b-1", "910b"),          # 整族曾漏判（CHIP_FAMILY_TO_CHIP 里原本没有 910b）
    ("linux-aarch64-910b-8", "910b"),
    ("linux-aarch64-a3", "a3"),                # 裸芯片名（无卡数后缀）
    ("linux-aarch64-a5", "a5"),
    ("linux-aarch64-310p", "310p"),
    ("linux-aarch64-a2b1-2", "a2"),            # a2b* 板型归一为 a2（与旧版 chip_of 行为一致）
    ("linux-aarch64-a3-offload", "a3"),
    ("linux-amd64-a5-4-sh-002", "a5"),         # a5 昇腾950 跑在 amd64
    ("linux-aarch64-cpu-4-cn12-001", None),    # CPU 池必须排除
    ("linux-aarch64-cpu-4-buildkit-cn12-001", None),  # buildkit 中缀：中缀逻辑最易让 CPU 池漏网
    ("linux-amd64-cpu-4-hk", None),            # -hk 变体只存在于 amd64（aarch64 无此形态）
    ("linux-arm64-cpu-16", None),              # arm64 形态的 CPU 池（arch 第三种取值）
]


# ⚠️ 实测出现在真实 run 里、但**权威真值集里没有**的标签（2026-09-24 扫 32 个真实 run 采到）。
# 说明真值集会滞后于现实（`800it` 这个变体 problem-labels.json 里就没有）。
# 只靠真值集回归会给人虚假的安心——这些形态必须同样判对。
OBSERVED_BUT_NOT_IN_FIXTURE = [
    ("linux-aarch64-a3-800it-16", "a3"),
    ("linux-aarch64-cpu-4", None),   # 无 cn12-001 后缀的 CPU 池变体
]


def _check_cases(cases):
    """返回不符合期望的用例描述列表。expected_chip 为 None 表示应判为非 NPU。"""
    failures = []
    for label, expected_chip in cases:
        matched = bool(NPU_LABEL.search(label))
        actual_chip = chip_of([label])
        if expected_chip is None:
            if matched or actual_chip is not None:
                failures.append(f"{label}: 应判为非 NPU，实际 matched={matched} chip={actual_chip}")
        else:
            if not matched:
                failures.append(f"{label}: 应判为 NPU，实际未匹配")
            elif actual_chip != expected_chip:
                failures.append(f"{label}: 期望芯片 {expected_chip}，实际 {actual_chip}")
    return failures


def test_named_regression_cases():
    failures = _check_cases(REGRESSION_CASES)
    assert not failures, "具名回归用例失败：\n  " + "\n  ".join(failures)


def test_labels_observed_in_real_runs():
    """真值集之外的实测标签也必须判对（防「真值集全绿」的虚假安心）。"""
    failures = _check_cases(OBSERVED_BUT_NOT_IN_FIXTURE)
    assert not failures, "实测标签判定失败：\n  " + "\n  ".join(failures)


def test_regression_cases_are_in_fixture():
    """具名用例必须真的来自权威真值集，避免测试自说自话。"""
    missing = [label for label, _ in REGRESSION_CASES if label not in ALL_LABELS]
    assert not missing, f"下列具名用例不在权威真值集里（真值集已变，请核对）：\n  " + "\n  ".join(missing)


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
    print(f"\n{len(tests) - failed}/{len(tests)} 通过"
          f"（真值集：{len(REPOS)} 仓 {len(ALL_LABELS)} 标签，"
          f"其中 NPU {len(NPU_LABELS)} / CPU {len(CPU_LABELS)}）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
