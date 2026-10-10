#!/usr/bin/env python3
"""第 4 步「读 ascend-gha-runners/docs/issues 知识库」的匹配口径回归测试。

为什么需要这组测试（forensics/issue_knowledge.py）：
    这一层此前**零测试覆盖** —— `tests/` 下没有任何文件引用它。代价是它安静地
    坏掉了，而且坏法很贵：产线 588 份报告里 #227（华为云 pypi 镜像 CDN index 页
    撕裂）被写成根因 241 次（41%），当前渲染的 118 条根因行里 56% 用先例当根因、
    94% 写最自信的「高度吻合」，而人工可核的样本 4/4 全错。

    病灶是「两个低区分度信号被当成了证据」：
      - `vllm` 出现在 57% 的 issue 里，是本仓所有 issue 的共同背景，不是判别特征；
      - `valueerror` / `importerror` 这类通用异常名只说明「都报错了」，不含机制，
        但 IDF 恰恰奖励它们（df 小 → idf 大），于是它们拿满权重把无关复盘顶到第一。
    模块自己的注释早就写明这类词不算机制证据，但**只在「证据强度」环节执行了**，
    「打分」环节没有 —— 于是排名已经错了，标成「弱」救不回来。

    本文件同时钉住两个方向，缺一不可：
      - 上面那些词**不许**再得分（否则假先例复活）；
      - 真机制签名（errorcode / exitcode）与稀有技术栈词（torch_npu / CANN / HCCL）
        **必须**继续得分（否则就是修过头，把真信号一起掐了）。

    所有 fixture 均为**合成**，绝不读 `.forensics_cache/`（那里是内网 issue 原文，
    本仓是 PUBLIC）。

运行：python3 tests/test_issue_knowledge.py      （无需 pytest，也兼容 pytest）
"""
import pathlib
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR.parent))

from forensics.issue_knowledge import (GENERIC_SIGNATURES, IssueIndex,  # noqa: E402
                                       extract_signatures, keywords_for)


def _issue(number, title, body, *, state="OPEN"):
    return {"number": number, "title": title, "body": body, "state": state,
            "labels": [{"name": "bug"}], "createdAt": "2026-09-01T00:00:00Z",
            "closedAt": None, "url": f"https://example.invalid/issues/{number}"}


def _index(issues, comments=None):
    return IssueIndex(issues, comments or {})


# 一份最小的合成语料：一条「靠 vllm 与通用异常名冒充同现象」的伪先例，
# 一条「带具体错误码」的真先例，一条与查询毫无关系的干扰项。
FAKE = _issue(900, "vllm-ascend 构建失败：pypi 镜像 index 页撕裂",
              "## 现象\nvllm 构建期取包失败。\n## 根因\n内网 pypi 镜像的 index 页撕裂。\n")
REAL = _issue(901, "A2 任务算子执行超时（error code 507011）",
              "## 现象\nerror code is 507011，算子执行超时。\n## 根因\n算子实现缺陷。\n")
NOISE = _issue(902, "文档勘误：README 里的链接失效",
               "## 现象\n链接 404。\n## 根因\n文件名改了。\n", state="CLOSED")

# 一条良性 WARNING —— 这就是产线上把 #227 顶起来的那行文本的形状：
# 它含 `vllm` 与 `No module named`，但机制是「可选的加速扩展没编进来」，
# 与「构建失败」无关。
BENIGN_LINE = "Failed to import the DeepSelect extension (vllm._deepselect_C): No module named 'vllm._deepselect_C'"


# ---------------- 1. vllm 不再是判别特征 ----------------

def test_vllm_is_not_extracted_as_a_signature():
    """`vllm` 出现在 57% 的 issue 里，留着它等于把「都是 vllm-ascend 的失败」
    冒充成「同一个现象」。这条正是 #227 的入口。"""
    got = extract_signatures(BENIGN_LINE)
    assert "vllm" not in got, f"vllm 仍是签名：{sorted(got)}"


def test_a_benign_warning_no_longer_yields_a_mechanism_signature():
    """去掉 vllm 之后，这行良性 WARNING 只剩通用异常名 —— 不该产出任何机制签名。"""
    got = extract_signatures(BENIGN_LINE) - GENERIC_SIGNATURES
    assert not got, f"良性 WARNING 仍抽出机制签名：{sorted(got)}"


# ---------------- 2. 通用异常名不给分（打分环节，不只是强度环节）----------------

def test_generic_exception_names_alone_match_nothing():
    """只有通用异常名命中时，不该有任何 issue 被算作候选。

    这条钉的是「打分环节也排除 GENERIC_SIGNATURES」。实测反例：`importerror`
    在真实库里 df=1，单项就拿 3.0×IDF≈288 分，足以把 #227 顶到第一。
    """
    index = _index([_issue(910, "某任务抛 ValueError", "## 现象\nValueError: bad value\n")])
    got = index.match({"valueerror"}, {"valueerror"}, top_k=5)
    assert got == [], f"通用异常名仍然匹配到了 issue：{[(m['issue']['number'], m['score']) for m in got]}"


def test_generic_exception_names_do_not_score_on_the_keyword_path_either():
    """关键词路径是第二条升格路径：`importerror` 是合法 token，实测在真实库里
    又贡献 0.4×IDF≈38 分。只堵签名路不够。"""
    index = _index([_issue(911, "某任务报 ImportError", "## 现象\nImportError: no module\n")])
    got = index.match(set(), {"importerror"}, top_k=5)
    assert got == [], f"通用异常名经关键词路仍然得分：{[(m['issue']['number'], m['score']) for m in got]}"


def test_generic_signatures_never_show_up_in_matched_signatures():
    """命中列表里也不该出现通用异常名 —— 它是给人看「为什么判同现象」的。"""
    index = _index([_issue(912, "任务失败", "## 现象\nRuntimeError: boom\nerror code is 507011\n")])
    got = index.match(extract_signatures("RuntimeError error code is 507011"),
                      set(), top_k=5)
    assert got, "机制签名 errorcode:507011 应当命中"
    matched = set(got[0]["matched_signatures"])
    assert not (matched & GENERIC_SIGNATURES), f"命中列表里混进了通用异常名：{sorted(matched)}"


# ---------------- 3. 反例守卫：真证据必须继续有效（防修过头）----------------

def test_error_codes_still_score_and_still_count_as_mechanism_evidence():
    """`error code 507011` 这类码值是**真机制证据**，不许被上面的排除误伤。
    实测：`errorcode:507011` 仍能把带它的那条复盘排到第一、强度「中」以上。"""
    index = _index([NOISE, FAKE, REAL])
    got = index.match(extract_signatures("aclrtSynchronizeEvent error code is 507011"),
                      keywords_for("error code 507011"), top_k=3)
    assert got, "真机制签名没有命中任何复现"
    best = got[0]
    assert best["issue"]["number"] == 901, \
        f"真先例没排第一，而是 #{best['issue']['number']}（score={best['score']}）"
    assert best["evidence_strength"] in ("强", "中"), best["evidence_strength"]
    assert "errorcode:507011" in best["mechanism_signatures"], best["mechanism_signatures"]


def test_exit_codes_still_score():
    """退出码同样归一成 exitcode:<码>，是真机制证据。"""
    index = _index([NOISE, _issue(913, "容器被 OOMKilled", "## 现象\nexit 137\n## 根因\n超内存\n")])
    got = index.match(extract_signatures("container exit 137"), set(), top_k=3)
    assert got, "exitcode:137 没有命中"
    assert "exitcode:137" in got[0]["mechanism_signatures"], got[0]["mechanism_signatures"]


def test_rare_tech_words_are_still_signatures():
    """`torch_npu` / `CANN` / `HCCL` 与 `vllm` **不是一类**：实测它们的 df 是
    6 / 10 / 4（190 条语料），是稀有且有判别力的词。谁要是顺手把它们也移进
    STOPWORDS「保持一致」，这条会红。"""
    got = extract_signatures("torch_npu CANN HCCL collectives")
    for token in ("torch_npu", "cann", "hccl"):
        assert token in got, f"{token} 不再被当作签名：{sorted(got)}"


def test_rare_tech_words_can_still_win():
    """而且它们真的能凭分数把条目顶上来（不是只留在集合里好看）。"""
    index = _index([NOISE, _issue(914, "CANN HCCL 集合通信超时", "## 现象\nHCCL 通信超时\n## 根因\n网络抖动\n")])
    got = index.match(extract_signatures("HCCL timeout"), keywords_for("HCCL timeout"), top_k=3)
    assert got and got[0]["issue"]["number"] == 914, \
        f"HCCL 没能命中那条复盘：{[(m['issue']['number'], m['score']) for m in got]}"


# ---------------- 4. 端到端：伪先例不再能冒充「强证据」----------------

def test_the_benign_line_no_longer_promotes_a_false_precedent():
    """产线那个误判的完整形态：查询是一行良性 WARNING，语料里有一条到处提
    `vllm` 的伪先例。修复前它靠 `vllm` 拿到「强」，于是第 5 步写「高度吻合」；
    修复后它不该再拿到「强」——「强」是报告写「高度吻合」的唯一门槛。"""
    index = _index([FAKE, NOISE, REAL])
    got = index.match(extract_signatures(BENIGN_LINE), keywords_for(BENIGN_LINE), top_k=3)
    fake = [m for m in got if m["issue"]["number"] == 900]
    if fake:
        assert fake[0]["evidence_strength"] != "强", \
            f"伪先例仍被标成「强」：机制签名={fake[0]['mechanism_signatures']}"
        assert not fake[0]["mechanism_signatures"], fake[0]["mechanism_signatures"]


def test_a_mechanism_signature_still_outranks_a_generic_one():
    """同一份语料里既命中通用异常名又命中真机制签名时，机制签名该拿更强档。"""
    index = _index([_issue(915, "报错", "## 现象\nRuntimeError: boom\n"),
                    _issue(916, "报错", "## 现象\nerror code is 507011\n")])
    got = index.match(extract_signatures("RuntimeError error code is 507011"), set(), top_k=5)
    by_number = {m["issue"]["number"]: m for m in got}
    assert 916 in by_number, "机制签名那条没有命中"
    assert 915 not in by_number, "只有通用异常名的那条不该被算作候选"
    assert "errorcode:507011" in by_number[916]["mechanism_signatures"]


# ---------------- 5. 归一化仍然有效（别在修的时候碰坏）----------------

def test_error_code_normalization_still_works():
    """`error code is 507035` 与 `error code 507035` 必须归一成同一个签名 ——
    否则换个措辞就永远匹配不上（#123 的正文就是 `error code is 507035`）。"""
    assert "errorcode:507035" in extract_signatures("error code is 507035")
    assert "errorcode:507035" in extract_signatures("error code 507035")
    assert "errorcode:507035" in extract_signatures("errorCode: 507035")


def test_stopwords_do_not_leak_into_the_keyword_index():
    """STOPWORDS 对索引侧与查询侧同时生效（`_tokenize` 是唯一真值源）。"""
    index = _index([_issue(917, "夜间任务失败", "## 现象\n夜间任务失败\n")])
    assert "失败" not in index.token_index
    assert "runner" not in index.token_index


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
