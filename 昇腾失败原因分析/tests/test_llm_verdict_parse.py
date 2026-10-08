#!/usr/bin/env python3
"""LLM 判决解析与校验的回归测试：具名失败原因、空 content 不崩、引用行号闸门。

为什么守这三条：
  - **具名原因**：降级率与原因分布是线上健康指标，一句笼统的「解析失败」等于没有信号；
  - **空 content 不是异常**：DeepSeek 是推理模型，实测 max_tokens 给小了会让 token 全花在
    reasoning 上、content 返回空字符串。当成异常崩掉会杀掉整轮报告，而当一类解析失败处理
    只是降级一条 case —— 这是实测踩出来的，不是假想；
  - **引用行号闸门**：模型可以编，编的行号必须过不了校验。这是「强制引用证据行」能否
    真正抑制幻觉的判据，也是幻觉率指标的分母来源。

运行：python3 tests/test_llm_verdict_parse.py      （无需 pytest，也兼容 pytest）
"""
import pathlib
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
sys.path.insert(0, str(BASE_DIR))

from forensics import llm_verdict as lv    # noqa: E402

ALLOWED = ("依赖/安装(ImportError)", "测试用例失败(pytest ret=1)",
           "昇腾NPU硬件错误(507xxx/ERR99999+设备)")

GOOD = {
    "root_cause": "业务侧用例真失败：test_single_node 断言 aisbench 精度不达标",
    "owner": "code",
    "confidence": "high",
    "verdict_class": "测试用例失败(pytest ret=1)",
    "phenomenon": "用例真失败（精度/逻辑）",
    "decisive_line": 10,
    "evidence_lines": [10, 11],
    "disagrees_with_rule": True,
    "missing_evidence": "缺用例级 traceback",
}


def _json(**overrides):
    payload = dict(GOOD)
    payload.update(overrides)
    import json
    return json.dumps(payload, ensure_ascii=False)


def _reason(text):
    try:
        lv.parse_llm_verdict(text, allowed_classes=ALLOWED)
    except lv.VerdictParseError as exc:
        return exc.reason
    raise AssertionError(f"预期抛 VerdictParseError，实际通过了：{text[:80]}")


# ---------------- 正常路径 ----------------

def test_valid_json_passes():
    parsed = lv.parse_llm_verdict(_json(), allowed_classes=ALLOWED)
    assert parsed["owner"] == "code" and parsed["decisive_line"] == 10
    assert parsed["evidence_lines"] == [10, 11]
    assert parsed["verdict_class"] == "测试用例失败(pytest ret=1)"


def test_json_fence_is_stripped():
    text = "```json\n" + _json() + "\n```"
    assert lv.parse_llm_verdict(text, allowed_classes=ALLOWED)["owner"] == "code"


def test_json_with_surrounding_prose_is_recovered():
    text = "分析如下：\n" + _json() + "\n以上。"
    assert lv.parse_llm_verdict(text, allowed_classes=ALLOWED)["owner"] == "code"


def test_optional_diagnostic_fields_default_to_empty():
    minimal = '{"root_cause":"x","owner":"code","confidence":"medium",' \
              '"verdict_class":"未分类","evidence_lines":[3]}'
    parsed = lv.parse_llm_verdict(minimal, allowed_classes=ALLOWED)
    assert parsed["phenomenon"] == "" and parsed["decisive_line"] is None
    assert parsed["missing_evidence"] == ""


def test_fallback_classes_always_allowed():
    for cls in ("其他", "未分类"):
        payload = {"root_cause": "x", "owner": "unknown", "confidence": "low",
                   "verdict_class": cls, "evidence_lines": [1]}
        import json
        assert lv.parse_llm_verdict(json.dumps(payload, ensure_ascii=False),
                                    allowed_classes=ALLOWED)["verdict_class"] == cls


# ---------------- 具名失败原因 ----------------

def test_empty_content_is_a_parse_error_not_a_crash():
    assert _reason("") == "empty_content"
    assert _reason("   \n  ") == "empty_content"
    assert _reason(None) == "empty_content"


def test_not_json_reported():
    assert _reason("模型认为这是依赖问题") == "not_json"
    assert _reason("[1, 2, 3]") == "not_json"


def test_missing_field_reported_by_name():
    import json
    for field in ("root_cause", "owner", "confidence", "verdict_class", "evidence_lines"):
        payload = dict(GOOD)
        del payload[field]
        try:
            lv.parse_llm_verdict(json.dumps(payload, ensure_ascii=False), allowed_classes=ALLOWED)
        except lv.VerdictParseError as exc:
            assert exc.reason == f"missing_field:{field}", exc.reason
        else:
            raise AssertionError(f"缺 {field} 竟然通过了")


def test_bad_enum_reported_per_field():
    assert _reason(_json(owner="backend")) == "bad_enum:owner"
    assert _reason(_json(confidence="very-high")) == "bad_enum:confidence"
    assert _reason(_json(verdict_class="模型自创的桶")) == "bad_enum:verdict_class"


def test_evidence_lines_empty_or_wrong_type_is_rejected():
    assert _reason(_json(evidence_lines=[])) == "missing_field:evidence_lines"
    assert _reason(_json(evidence_lines=["L10"])) == "bad_type:evidence_lines"


def test_root_cause_too_long_is_truncated_not_rejected():
    parsed = lv.parse_llm_verdict(_json(root_cause="很" * 5000), allowed_classes=ALLOWED)
    assert len(parsed["root_cause"]) == lv.MAX_ROOT_CAUSE_CHARS


# ---------------- 引用行号闸门 ----------------

def test_citation_outside_window_is_a_hard_problem():
    parsed = lv.parse_llm_verdict(_json(evidence_lines=[10, 99999]), allowed_classes=ALLOWED)
    problems = lv.validate_citations(parsed, allowed_lines={1, 10, 11})
    assert "cited_line_not_in_evidence:99999" in problems
    assert lv.hard_citation_problems(problems) == ["cited_line_not_in_evidence:99999"], \
        "引用窗口外的行号必须触发降级"


def test_citations_all_inside_window_are_clean():
    parsed = lv.parse_llm_verdict(_json(), allowed_classes=ALLOWED)
    assert lv.validate_citations(parsed, allowed_lines={1, 10, 11}) == []


def test_decisive_line_not_cited_is_soft_only():
    parsed = lv.parse_llm_verdict(_json(decisive_line=11, evidence_lines=[10]),
                                 allowed_classes=ALLOWED)
    problems = lv.validate_citations(parsed, allowed_lines={10, 11})
    assert "decisive_line_not_cited" in problems
    assert lv.hard_citation_problems(problems) == [], "未自洽只是软问题，不该作废整条判决"


def test_decisive_line_outside_window_is_hard():
    parsed = lv.parse_llm_verdict(_json(decisive_line=99999), allowed_classes=ALLOWED)
    problems = lv.validate_citations(parsed, allowed_lines={10, 11})
    assert lv.hard_citation_problems(problems) == ["decisive_line_not_in_evidence:99999"]


def test_weak_decisive_line_flags_warning_or_info():
    assert lv.weak_decisive_line("WARNING ... Failed to import the extension", "high") is True
    assert lv.weak_decisive_line("INFO initializing", "medium") is True
    assert lv.weak_decisive_line("FAILED tests/... AssertionError", "high") is False
    assert lv.weak_decisive_line("WARNING whatever", "low") is False, \
        "低置信度已经承认没判据了，不再重复计入弱决定性行"


def test_weak_decisive_line_when_missing():
    assert lv.weak_decisive_line(None, "high") is True, \
        "高置信却拿不出决定性行 —— 这正是要被统计的病征"


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
