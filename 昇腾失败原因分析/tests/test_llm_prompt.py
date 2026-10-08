#!/usr/bin/env python3
"""提示词纪律的金丝雀测试（**不出网、不调模型**）。

为什么守这些：整套方案的成败几乎全在 prompt 上。同一份真实日志、同一个模型，
只换 prompt 就从「判错且自报与规则一致」变成「判对且指出规则错」——
而 prompt 是最容易被后来者当成「注释」顺手精简掉的东西（"这几条太啰嗦了删了吧"）。
所以纪律条款必须有断言钉住：**删掉任一条，这里就红**。

同时守一个容易静默分叉的地方：`verdict_class` 闭集必须来自调用方（桶表），
prompt 与解析器必须看到**同一份**闭集 —— 写死在 prompt 里的话，桶表一改，
模型选了个解析器不认的类，整批判决全部降级，而且不报错。

运行：python3 tests/test_llm_prompt.py      （无需 pytest，也兼容 pytest）
"""
import pathlib
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
sys.path.insert(0, str(BASE_DIR))

from forensics import llm_judge as lj          # noqa: E402
from forensics import llm_prompt as lp         # noqa: E402
from forensics import llm_verdict as lv        # noqa: E402

ALLOWED = ("依赖/安装(ImportError)", "测试用例失败(pytest ret=1)",
           "昇腾NPU硬件错误(507xxx/ERR99999+设备)", "性能未达标(benchmark)")


def _system():
    return lp.build_system_prompt(ALLOWED)


# ---------------- 纪律条款的金丝雀 ----------------

def test_six_disciplines_are_present_and_numbered():
    system = _system()
    for number in range(1, 7):
        assert f"\n{number}. **" in system, f"第 {number} 条纪律不见了"


def test_terminal_line_discipline_survives():
    system = _system()
    assert "终局判定行" in system, "「只有终局判定行能定性」这条纪律被删了"
    for shape in ("short test summary info", "Performance verification failed",
                  "Benchmark failed", "pytest exit code: ret="):
        assert shape in system, f"终局行形态 {shape} 没有被点名"


def test_warning_is_not_root_cause_discipline_survives():
    system = _system()
    assert "WARNING" in system, "「WARNING/INFO 不是根因」这条纪律被删了"
    assert "不是根因" in system


def test_repo_specific_noise_is_named_not_abstract():
    """具名噪声比「请留意良性警告」有效得多 —— 这是实测出来的，不是风格偏好。"""
    system = _system()
    for trap in ("No module named 'vllm._deepselect_C'",
                 "Failed to import the",
                 "ERR99999",
                 "Executing the custom container implementation failed"):
        assert trap in system, f"本仓陷阱 {trap} 没有被具名列出"


def test_measured_evidence_against_the_overfit_bucket_is_written_down():
    """40/40 抽样都不是真依赖问题 —— 这个数字要写在 prompt 里，模型才会真的不信 WARNING。"""
    system = _system()
    assert "0 条" in system and "40" in system
    assert "WARNING 行" in system


def test_forced_citation_and_low_confidence_escape_hatch():
    system = _system()
    assert "强制引用证据行" in system
    assert "decisive_line" in system and "evidence_lines" in system
    assert "low" in system, "「拿不出决定性行必须选 low」这条退路不见了"
    assert "禁止外推" in system
    assert "missing_evidence" in system


def test_no_markdown_explanation_outside_json():
    assert "只输出一个 JSON 对象" in _system()


# ---------------- 输出契约与闭集 ----------------

def test_contract_lists_every_field_the_parser_reads():
    """契约里必须写明解析器会读的每个字段 —— 少写一个，模型就少填一个。

    只断言字段**出现**，不断言哪些必填：必填与否由解析器决定（`test_llm_verdict_parse.py`
    守），这里守的是「契约与解析器读的是同一批字段名」。
    """
    system = _system()
    for field in ("root_cause", "owner", "confidence", "verdict_class", "phenomenon",
                  "decisive_line", "evidence_lines", "disagrees_with_rule",
                  "missing_evidence"):
        assert field in system, f"输出契约缺字段 {field}"


def test_contract_marks_verdict_class_as_optional():
    """桶是事后归类：契约必须写明「贴合才填、不贴合留空」，否则模型会硬套一个最近的桶。"""
    system = _system()
    assert "选填" in system
    assert "留空" in system
    assert "不要挑一个最接近的" in system


def test_owner_and_confidence_enums_match_the_parser():
    system = _system()
    for owner in lv.OWNER_ENUM:
        assert owner in system, owner
    for confidence in lv.CONFIDENCE_ENUM:
        assert confidence in system, confidence


def _closed_set_section(system):
    return system.split("# verdict_class 闭集", 1)[1]


def test_closed_set_comes_from_the_caller_not_hardcoded():
    """纪律条款里点名了一个具体桶（实测反证要用真名），所以只能对**闭集段**断言 ——
    在整篇里找桶名，等于把纪律条款的引用当成了闭集。"""
    section = _closed_set_section(_system())
    for name in ALLOWED:
        assert name in section, name
    other = _closed_set_section(lp.build_system_prompt(("只有这一个桶",)))
    assert "只有这一个桶" in other
    assert ALLOWED[1] not in other, "闭集是写死的，没有跟着调用方走"


def test_unknown_bucket_is_not_silently_rejected():
    """闭集里没有合适项时要有出路，否则模型会硬套一个最近的桶（正是规则层的老毛病）。

    出路有两条：显式填 `其他`，或**留空**。两条都要在契约里写明 —— 只写「填 其他」
    的话，模型会把它当成又一个必须填的桶，锚定偏差原样复现。
    """
    assert "其他" in _system()
    assert "留空" in _system()


def test_phenomenon_has_a_clustering_friendly_writing_rule():
    """`phenomenon` 会被跨 job 聚类（用来发现该新增哪些归类），措辞漂移会让聚类失效。"""
    system = _system()
    assert "同一个现象" in system and "同一句话" in system, "没写「同现象同措辞」，聚类不可复核"


def test_prompt_version_is_stamped():
    assert lv.PROMPT_VERSION in _system()


# ---------------- 用户消息组装 ----------------

def test_rule_hint_is_marked_as_unreliable():
    user = lp.build_user_message("L1|x", rule_hint="依赖/安装(ImportError)")
    assert "经常是错的" in user
    assert "独立判断" in user


def test_overfit_bucket_hint_carries_the_measured_warning():
    user = lp.build_user_message("L1|x", rule_hint="依赖/安装(ImportError)")
    assert "0 条" in user and "40 条" in user, "最大桶的实测反证没带上，锚定会被原样复现"


def test_unknown_bucket_hint_has_no_borrowed_evidence():
    user = lp.build_user_message("L1|x", rule_hint="某个没测过的桶")
    assert "某个没测过的桶" in user
    assert "0 条" not in user, "把最大桶的实测数字套到了别的桶上"


def test_evidence_block_comes_after_the_hint_and_instruction_is_last():
    user = lp.build_user_message("L1|证据行", rule_hint="依赖/安装(ImportError)",
                                 job_name="job-1", step_name="Run tests")
    assert user.index("依赖/安装(ImportError)") < user.index("L1|证据行")
    assert user.rstrip().endswith("请按输出契约给出**一个** JSON 对象。"), \
        "长证据之后没有收尾指令 —— 模型容易把结论落在最后读到的那段噪声上"


def test_case_meta_is_rendered_and_missing_fields_are_tolerated():
    user = lp.build_user_message("L1|x", job_name="job-1", chip="A2")
    assert "job-1" in user and "A2" in user
    bare = lp.build_user_message("L1|x")
    assert "未知" in bare, "缺 job 名不该崩，也不该留空标签"


def test_empty_evidence_is_rendered_explicitly():
    assert "（无证据）" in lp.build_user_message(None)


# ---------------- 与编排层的一致性 ----------------

def test_judge_passes_the_same_closed_set_it_validates_against():
    """编排层组装 prompt 与解析校验用的必须是同一份闭集，否则会出现模型照 prompt 选了、
    解析器却不认的类 —— 而且失败方式是「整批静默降级」。"""
    from forensics.llm_client import FakeLLMClient
    import json
    payload = json.dumps({"root_cause": "x", "owner": "code", "confidence": "low",
                          "verdict_class": ALLOWED[1], "evidence_lines": [1]},
                         ensure_ascii=False)
    client = FakeLLMClient(text=payload)
    outcome = lj.judge_case("L1|=== 1 failed, 0 passed in 1.0s ===", client=client,
                            allowed_classes=ALLOWED)
    assert outcome.used, outcome.fallback_reason
    for name in ALLOWED:
        assert name in client.calls[0]["system"], name


def main():
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = []
    for name, func in tests:
        try:
            func()
            print(f"✅ {name}")
        except Exception as exc:
            # 连 AssertionError 以外的异常也计为失败：崩在某个用例上会**掩盖后面所有用例**，
            # 「套件整体崩溃」在证伪里看起来像「没红」，比一条失败危险得多。
            failed.append(name)
            detail = str(exc) if isinstance(exc, AssertionError) else f"{type(exc).__name__}: {exc}"
            print(f"❌ {name}: {detail}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} 通过" + (f"，失败：{failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
