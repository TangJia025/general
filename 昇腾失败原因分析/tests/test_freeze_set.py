#!/usr/bin/env python3
"""冻结集与真值集的完整性守卫（批次 4）。

为什么每条都要守：
  - **证据行号必须落在窗口内**：真值行的 `evidence_lines` 是给裁定人回看的锚点。
    越界的行号（比如按原始日志编号写的）会让回看指向不相干的文本，而**没有人会去数**——
    裁定人只会觉得「看着差不多」就点头了，于是错误真值被签收。
  - **真值集的 job 必须都在冻结集里**：真值行写了一个不在 cases.jsonl 里的 job，
    评测时它会被静默忽略（对不上号），于是「30 例裁定」实际只算了 29 例。
  - **proposed / confirmed 与 adjudicator 必须自洽**：`status=proposed` 却填了裁定人，
    等于把「我起草的」冒充成「人裁定的」—— 这是整个方案里最不能含糊的一处。
  - **抽样必须确定性**：同一池子两次跑出不同样本，冻结集就不能复核。

运行：python3 tests/test_freeze_set.py      （无需 pytest，也兼容 pytest）
"""
import gzip
import json
import pathlib
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
sys.path.insert(0, str(BASE_DIR))

from eval.select_sample import pick_evenly, select     # noqa: E402
from forensics.llm_verdict import FALLBACK_CLASSES, OTHER_CLASS   # noqa: E402

CASES = BASE_DIR / "eval/cases.jsonl"
TRUTHS = BASE_DIR / "eval/truths.jsonl"
CANARIES = BASE_DIR / "eval/canaries"
FIXTURES = BASE_DIR / "eval/fixtures"

REQUIRED_TRUTH_FIELDS = ("job_id", "truth_class", "truth_owner", "mechanism", "evidence_lines",
                         "status", "adjudicator", "proposed_by")
OWNER_ENUM = ("code", "infra", "mixed", "unknown")


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ---------------- 抽样逻辑 ----------------

def test_pick_evenly_is_deterministic_and_spreads():
    items = list(range(100))
    assert pick_evenly(items, 5) == pick_evenly(items, 5)
    # 具体值钉死：抽样口径一旦漂移，已冻结的 30 例名单就与代码对不上（首次跑出的是
    # 74 而不是 75 —— 步长 24.75 取整到偶数，这属于口径本身，改它等于换样本）
    picked = pick_evenly(items, 5)
    assert picked == [0, 25, 50, 74, 99], picked
    assert len(set(picked)) == 5
    gaps = [b - a for a, b in zip(picked, picked[1:])]
    assert max(gaps) - min(gaps) <= 1, f"取样间距不均：{gaps}"


def test_pick_evenly_when_asking_for_more_than_available():
    assert pick_evenly([1, 2, 3], 5) == [1, 2, 3]
    assert pick_evenly([1, 2, 3], 1) == [2], "取一个时取中间那个，不要固定取头"


def test_select_respects_quotas_and_never_duplicates():
    pool = [{"job_id": f"{group}-{index}", "stratum": group, "family": "f", "run_id": str(index)}
            for group in ("defect", "normal") for index in range(6)]
    quotas = [("defect", "f", 3), ("normal", "f", 2)]
    picked, report = select(pool, quotas)
    assert len(picked) == 5
    assert len({row["job_id"] for row in picked}) == 5
    assert report[0][4] == 3 and report[1][4] == 2


def test_select_refuses_to_silently_return_an_empty_stratum():
    """某一档一例都挑不到时必须炸：静默返回空样本会让「缺陷层过了」这类结论失去依据。"""
    pool = [{"job_id": "a", "stratum": "defect", "family": "f", "run_id": "1"}]
    try:
        select(pool, [("normal", "f", 2)])
    except AssertionError:
        return
    raise AssertionError("某档为空竟然没报错")


# ---------------- 真值集 ----------------

def test_truth_files_exist():
    assert CASES.exists(), "冻结集 cases.jsonl 不见了"
    assert TRUTHS.exists(), "真值集 truths.jsonl 不见了"


def test_every_truth_points_at_a_frozen_case():
    cases = {row["job_id"] for row in _read_jsonl(CASES)}
    truths = _read_jsonl(TRUTHS)
    assert truths, "真值集是空的"
    orphans = [row["job_id"] for row in truths if row["job_id"] not in cases]
    assert not orphans, f"真值集里的这些 job 不在冻结集里，评测时会被静默忽略：{orphans}"


def test_every_truth_has_the_fields_an_adjudicator_needs():
    for row in _read_jsonl(TRUTHS):
        for field in REQUIRED_TRUTH_FIELDS:
            assert field in row, f"{row.get('job_id')} 缺字段 {field}"
        assert row["truth_class"].strip(), f"{row['job_id']} 真值类为空"
        assert row["truth_owner"] in OWNER_ENUM, \
            f"{row['job_id']} owner 越界：{row['truth_owner']}"
        assert isinstance(row["evidence_lines"], list) and row["evidence_lines"], \
            f"{row['job_id']} 没有证据行 —— 裁定人无从复核"


def test_the_harness_can_actually_read_every_truth():
    """字段名必须与评测臂**读**的名字一致。

    这一条是补上来的：真值行原先写 `owner`，而 `run_eval.truth_of` 读的是 `truth_owner`，
    于是 owner 那一列整列算在 None 上 —— 数字照样打印出来，量到的却是空气。
    跨文件读同一份数据时，只有「让评测臂自己去读一遍」才能发现这种错位。
    """
    sys.path.insert(0, str(BASE_DIR / "eval"))
    from run_eval import truth_expressible, truth_of      # noqa: PLC0415
    truths = {row["job_id"]: row for row in _read_jsonl(TRUTHS)}
    for job_id, row in truths.items():
        truth_class, truth_owner = truth_of(truths, job_id)
        assert truth_class, f"{job_id}：评测臂读不到 truth_class"
        assert truth_owner, f"{job_id}：评测臂读不到 truth_owner（字段名对不上？）"
        assert truth_owner == row["truth_owner"]
        assert truth_expressible(truths, job_id) is not None, \
            f"{job_id}：评测臂读不到 closed_set_expressible（覆盖度分半会全落 unmarked）"


def test_truth_evidence_lines_are_inside_the_frozen_window():
    """守卫的核心：行号必须能在 fixture 里回看。越界说明按原始日志编号写了。"""
    for row in _read_jsonl(TRUTHS):
        path = FIXTURES / f"{row['job_id']}.txt.gz"
        if not path.exists():
            continue                      # fixture 不入库，取不到就跳过（本机外复核时会走到这里）
        total = len(gzip.open(path, "rt", encoding="utf-8").read().splitlines())
        outside = [line for line in row["evidence_lines"] if not 1 <= line <= total]
        assert not outside, f"{row['job_id']} 的证据行 {outside} 不在窗口 1..{total} 内"


def test_proposed_and_confirmed_are_not_conflated():
    """`status=proposed` 的行不能有裁定人：起草与人裁定必须分得开。"""
    for row in _read_jsonl(TRUTHS):
        if row["status"] == "proposed":
            assert row["adjudicator"] is None, \
                f"{row['job_id']} 标着 proposed 却填了裁定人 {row['adjudicator']}"
        elif row["status"] == "confirmed":
            assert row["adjudicator"] and row["adjudicated_at"], \
                f"{row['job_id']} 标着 confirmed 却没有裁定人与时间"
        else:
            raise AssertionError(f"{row['job_id']} 的 status 取值不认识：{row['status']}")


def test_truth_classes_that_claim_to_be_in_the_closed_set_really_are():
    """`closed_set_expressible=true` 的行，真值类必须真的是生产桶表里的名字 ——
    写错一个字就会让「闭集内一致率」这个子集静默换成分母里的另一个样本。"""
    from eval.production import allowed_classes
    allowed = set(allowed_classes())
    for row in _read_jsonl(TRUTHS):
        if row.get("closed_set_expressible"):
            assert row["truth_class"] in allowed, \
                f"{row['job_id']} 声称真值在闭集内，但 {row['truth_class']!r} 不在桶表里"


def test_rule_agrees_flag_matches_the_actual_comparison():
    cases = {row["job_id"]: row for row in _read_jsonl(CASES)}
    for row in _read_jsonl(TRUTHS):
        case = cases.get(row["job_id"])
        if not case:
            continue
        assert row["rule_bucket"] == case["rule_bucket"], \
            f"{row['job_id']} 记的规则桶与冻结集不一致，规则臂的数字会对不上"
        assert row["rule_agrees"] == (row["rule_bucket"] == row["truth_class"]), \
            f"{row['job_id']} 的 rule_agrees 是手填的，与比较结果不符"


# ---------------- 金丝雀 ----------------

def test_canaries_pin_both_arms_and_are_self_contained():
    files = sorted(CANARIES.glob("*.json")) if CANARIES.exists() else []
    assert files, "一个金丝雀都没有，prompt 纪律就没有回归钉子"
    for path in files:
        canary = json.loads(path.read_text(encoding="utf-8"))
        excerpt = canary["evidence_excerpt"]
        naive = canary["naive_must_get_wrong"]
        disciplined = canary["disciplined_must_get_right"]
        assert naive["verdict_class"] != disciplined["verdict_class"], \
            f"{path.name}：朴素与纪律两臂的期望相同，这个金丝雀区分不了任何东西"
        assert 1 <= disciplined["decisive_line"] <= len(excerpt), \
            f"{path.name}：decisive_line 不在摘录范围内（行号是相对摘录的）"
        # 引用必须在摘录里，否则这条金丝雀没法在服务端闸门下复核
        for line in disciplined.get("evidence_lines_are_a_subset_of", []):
            assert 1 <= line <= len(excerpt), f"{path.name}：引用行 {line} 越出摘录"


def test_canaries_expect_a_class_the_model_could_legally_emit():
    """纪律臂期望的归类必须是**当时的契约允许模型输出的东西**。

    这条是补上去的：`disciplined_must_get_right.verdict_class` 曾写着 `性能未达标(benchmark)`
    —— 一个**不在闭集里**的真值类。旧契约下模型必须落一个桶，所以那条期望等于要求模型
    **违反**自己的输出契约；没有任何测试跑它，于是没人发现。现在契约改为「不贴合就留空」，
    期望值只能是「空串」或闭集内的名字，两者不许混。
    """
    from eval.production import allowed_classes
    allowed = set(allowed_classes()) | set(FALLBACK_CLASSES)
    for path in sorted(CANARIES.glob("*.json")):
        expected = json.loads(path.read_text(encoding="utf-8"))["disciplined_must_get_right"]
        label = expected["verdict_class"]
        assert label == "" or label in allowed, \
            f"{path.name}：期望归类 {label!r} 既不是空串也不在闭集里，模型不可能合法输出它"


def test_canary_projection_matches_what_it_declares():
    """真值不在闭集里时，金丝雀必须把「留空 → 投影成其他」这一档写出来。

    只写 `verdict_class: ""` 是不够的：读的人会以为「模型没说」。留空是一条**结论**
    （闭集里没有这一格），投影与来源是它的机器可读形态，也是 `other_clusters` 的输入。
    """
    for path in sorted(CANARIES.glob("*.json")):
        canary = json.loads(path.read_text(encoding="utf-8"))
        expected = canary["disciplined_must_get_right"]
        if canary["truth"].get("closed_set_expressible", True):
            continue
        assert expected.get("projected_class") == OTHER_CLASS, \
            f"{path.name}：真值不可表达时，纪律臂的投影必须是「{OTHER_CLASS}」"
        assert expected.get("projection_source") == "none", \
            f"{path.name}：投影来源必须是 `none`（模型留空），不能记成模型给了归类"
        assert expected["verdict_class"] == "", \
            f"{path.name}：真值不可表达却给了具体归类 —— 闭集里没有它，这是硬塞最近桶"


def test_canary_projection_source_agrees_with_the_declared_class():
    """`projection_source` / `projected_class` 与 `verdict_class` 三者不许互相矛盾。

    空串 ↔ `none` ↔ `其他`；非空 ↔ `llm` ↔ 原值。这三格是**同一件事的三种写法**，
    只写其中一两个，读的人就会按自己以为的那个去解读（比如把留空读成「模型没说」）。
    """
    for path in sorted(CANARIES.glob("*.json")):
        expected = json.loads(path.read_text(encoding="utf-8"))["disciplined_must_get_right"]
        if "projection_source" not in expected:
            continue
        label = expected["verdict_class"]
        want_source = "none" if label == "" else "llm"
        want_class = OTHER_CLASS if label == "" else label
        assert expected["projection_source"] == want_source, \
            f"{path.name}：verdict_class={label!r} 却记 " \
            f"projection_source={expected['projection_source']!r}"
        assert expected["projected_class"] == want_class, \
            f"{path.name}：verdict_class={label!r} 却记 " \
            f"projected_class={expected['projected_class']!r}"


def main():
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
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
