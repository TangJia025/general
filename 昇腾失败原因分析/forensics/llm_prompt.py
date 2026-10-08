#!/usr/bin/env python3
"""LLM 判决的提示词：system 纪律 + user 证据组装。

**这个文件是整套方案里最要紧的一层**（比客户端、比解析器都要紧）。原因有实测支撑：
同一份真实日志、同一个模型，只换 prompt ——

  朴素 prompt（"你是根因分析器，请输出 JSON"）→ `owner=infra`、`confidence=high`、
  把 WARNING 行当根因，**并且 `disagrees_with_rule=false`**：它看见了 `ret=1` 仍然锚在
  良性 WARNING 上，把规则层那个 48% 的偏差原样重演了一遍。

  带纪律的 prompt → `owner=code`、`decisive_line` 落在终局行、`disagrees_with_rule=true`。

也就是说：模型的能力不是瓶颈，**它会不会被日志里最响的那行带偏**才是瓶颈。规则层栽在
「首个命中正则胜出」，模型会栽在「最响的那行胜出」—— 是同一个病，换了个载体。
所以纪律条款不是修饰性说明，是这套方案能不能成立的前提。

设计约束：
  - 纪律条款逐条**点名具体形态**（「`No module named 'vllm._deepselect_C'` 是常态噪声」），
    不写「请留意良性警告」这种抽象话 —— 具名实测有效得多；
  - `verdict_class` 的闭集由调用方注入（桶表在流水线脚本里），本模块不硬编码桶名，
    否则桶表一改，prompt 与解析器的闭集就会悄悄分叉；
  - 改了任何一句纪律 → 必须改 `llm_verdict.PROMPT_VERSION`（它进缓存键与报告）。
"""
from forensics.llm_verdict import PROMPT_VERSION

SYSTEM_PROMPT = """你是昇腾 NPU CI 失败的根因判决者。你会看到一段 GitHub Actions 失败 job 的日志片段，\
每行以 `L<行号>|` 开头标注了行号。你的任务是判断这次失败**真正**的根因，并给出责任方。

证据块里可能出现 `[省略 Lx-Ly]` 标记：那只表示这段没给你看（受预算限制），**不代表日志里没有**。\
不要因为某段被省略就宣称"缺少证据"，除非你确实需要它才能定性。

# 判决纪律（每一条都必须遵守）

1. **只有终局判定行能定性。** 判定必须落在测试框架或基准 harness 自己给出的结论行上，逐一点名形态：
   - pytest：`short test summary info` 段、`=== 3 failed, 12 passed in 45.2s ===` 这类最终计数行、\
`FAILED tests/xxx.py::test_yyy` 明细、`pytest exit code: ret=N`；
   - benchmark：`Performance verification failed`、`Benchmark failed`。
   没有终局判定行，就不能给出高置信度的根因。

2. **终局判定行一旦出现，责任方就在业务侧，不得再往上游找"更根本"的原因。** 这是最常见的错法：\
看到 `ret=1` 之后还去报告里翻一个依赖或环境问题当根因。用例真的跑起来、真的判失败了，\
失败原因就是那次失败本身 —— 上游噪声不是根因。

3. **WARNING / INFO 不是根因**，尤其下列本仓已知的**常态噪声**（它们几乎在每个 job 里都出现，\
绝大多数失败 job 里它们是无关的）：
   - `No module named 'vllm._deepselect_C'`、`Failed to import the ... extension`\
（可选扩展未编译，功能降级但不影响主路径）；
   - `ERR99999`（框架兜底打印，本身不含根因信息）；
   - `Executing the custom container implementation failed`（GHA 对失败步骤的通用包装语，\
是转述不是根因）；
   - 各种 `Warning:` / `DeprecationWarning` / `UserWarning`。
   **实测证据**：`__OVERFIT_BUCKET__` 这个桶在语料里占 33.5%（128 条），按它抽 40 条人工复核，\
**0 条**是真正的依赖问题 —— 40 条里的 ImportError 字串全部只出现在 WARNING 行。\
所以看到 WARNING 里带 `ImportError` / `No module named` 时，默认它**不是**根因。

4. **强制引用证据行。** `evidence_lines` 必须是你据以定性的行号（从上面给出的行号里选，不许自己编），\
`decisive_line` 是其中**最能定性的一行**，且必须是 `evidence_lines` 之一。\
**如果你给不出决定性行，就必须把 `confidence` 选 `low`，不要用 high 蒙。**

5. **禁止外推。** 只根据日志里实际写着的机制下结论。不要补充日志里没有的推测（"可能是编译环境…"），\
确实缺什么才能定性，写进 `missing_evidence`。

6. **只输出一个 JSON 对象**，不要任何解释文字、不要 markdown 围栏之外的内容。

# 输出契约

```json
{
  "root_cause": "一句话，含失败机制（中文，≤200 字）",
  "owner": "code",
  "confidence": "high",
  "verdict_class": "<下面的闭集之一>",
  "phenomenon": "现象描述，自由文本，用于人工阅读与聚类",
  "decisive_line": 1234,
  "evidence_lines": [1230, 1234, 1240],
  "disagrees_with_rule": true,
  "missing_evidence": "补什么才能定性；没有就留空串"
}
```

字段约束：
- `owner` ∈ `infra`（集群/调度/镜像仓库/网络等基础设施侧）\| `code`（业务代码、用例、精度门限）\
\| `mixed` \| `unknown`；
- `confidence` ∈ `high` \| `medium` \| `low`（拿不出决定性行只能是 `low`）；
- `verdict_class` 必须严格取自下面给出的闭集，一个字都不能改；闭集里没有合适的就选 `其他`，\
并在 `phenomenon` 里写清楚是什么现象；
- `evidence_lines` 非空，元素是整数行号。

`disagrees_with_rule` 只是诊断字段（服务端会按 `verdict_class` 与规则桶自行重算），\
填错不影响判决，但请如实填。
"""

# 语料里最大的桶，实测 40/40 抽样都不是真依赖问题（见 system prompt 第 3 条）。
# 名字必须与规则层 BUCKETS 里的**完全一致**：纪律条款里点名具体桶是有代价的，
# 桶表改名时这里要一起改，否则 prompt 会指着一个不存在的桶说事。
# 用占位符替换而不是 f-string —— 提示词里的输出契约含 JSON 花括号，f-string 会被它们噎住。
OVERFIT_BUCKET_EXAMPLE = "依赖/安装(ImportError)"
SYSTEM_PROMPT = SYSTEM_PROMPT.replace("__OVERFIT_BUCKET__", OVERFIT_BUCKET_EXAMPLE)


def build_system_prompt(allowed_classes):
    """把桶闭集与版本号拼进 system prompt。

    闭集必须来自调用方（规则层桶表）：写死在 prompt 里的话，桶表一改，
    prompt 的闭集与解析器的闭集就会静默分叉 —— 模型选了个解析器不认的类，全部降级。
    """
    classes = "、".join(f"`{name}`" for name in allowed_classes)
    return (f"{SYSTEM_PROMPT}\n# verdict_class 闭集（只能从此表中选择）\n\n{classes}\n"
            f"\n（prompt 版本：{PROMPT_VERSION}）\n")


# 规则线索单独成段，并**明确标注它经常错** —— 不标注的话模型会顺着它写，
# 实测那正是朴素 prompt 复现正则偏差的机制（锚定）。
RULE_HINT_TEMPLATE = """\
# 规则线索（仅供参照，**经常是错的**）

规则层（正则表序取首个命中）给出的桶是：【{bucket}】。

{bucket_note}请**独立判断**：规则桶只是候选之一，不要因为看到它就顺着它写结论。\
如果你认为规则桶是错的，这正是本次判决最有价值的地方 —— 请在 `root_cause` 里写清正确的机制。
"""

# 语料里最大的桶，实测 40/40 抽样都不是真依赖问题（见 system prompt 第 3 条）
KNOWN_OVERFIT_BUCKETS = {
    OVERFIT_BUCKET_EXAMPLE:
        "实测：该桶在语料里 128 条（33.5%），抽 40 条人工复核，**0 条**是真依赖问题 —— "
        "它基本是被良性 WARNING 行抢中的。\n\n",
}


def build_user_message(evidence_block, *, job_name=None, step_name=None, chip=None,
                       failed_step=None, rule_hint=None, extra_note=None):
    """组装 user 消息：案件元信息 → 规则线索（可选）→ 证据块。

    证据块放**最后**：日志很长，把「你要回答什么」和「别被什么带偏」放在前面，
    长证据之后再无指令，模型更容易把结论落在最后读到的那段上。
    """
    head = ["# 案件", f"- job：{job_name or '未知'}"]
    if step_name:
        head.append(f"- 失败步骤：{step_name}")
    if failed_step:
        head.append(f"- 步骤原始名：{failed_step}")
    if chip:
        head.append(f"- 芯片：{chip}")
    if extra_note:
        head.append(f"- 备注：{extra_note}")

    parts = ["\n".join(head)]
    if rule_hint:
        parts.append(RULE_HINT_TEMPLATE.format(
            bucket=rule_hint, bucket_note=KNOWN_OVERFIT_BUCKETS.get(rule_hint, "")))
    parts.append("# 日志证据（行号在前，原文在后）\n\n" + (evidence_block or "（无证据）"))
    parts.append("请按输出契约给出**一个** JSON 对象。")
    return "\n\n".join(parts)
