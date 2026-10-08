#!/usr/bin/env python3
"""评测指标：全部是纯函数，输入输出都是普通结构（可 JSON 序列化），便于手算校对与复现。

**为什么指标要单独一层**：本方案最容易骗自己的地方就是评测口径。三条纪律写在这里：

1. **真值为 None 的 case 一律不计入准确率**（`scored()`）—— 把「没裁定」当成「判对了」
   或「判错了」都会凭空造出一个数字；
2. **幻觉率的分母是「模型引用的行」，不是「日志总行数」** —— 后者会让幻觉率恒等于 0.0x，
   看起来很好而实际毫无信息；
3. **准确率必须带区间**。N<60 时点估计本身不成立，`bootstrap_ci` 是给出「这个数字有多虚」
   的唯一诚实做法；不报区间的准确率在评审里站不住。

record / pair 的结构约定（全部是 dict / tuple，不定义类）：

    pair   = (pred_class, truth_class)          # truth 为 None 表示尚未人工裁定
    record = {
        "job_id": str, "arm": str,
        "pred_class": str, "truth_class": str | None,
        "pred_owner": str, "truth_owner": str | None,
        "stratum": str,                          # defect / normal / …
        "cited": [int],                           # 模型引用的行号
        "allowed": [int],                         # 该 case 证据窗口内的行号
        "used": bool,                             # 判决是否用了 LLM（False = 降级）
        "weak_decisive": bool,                    # 决定性行是 WARNING/INFO 或缺失
        "fallback_reason": str | None,
        "usage": {"prompt_tokens": int, "completion_tokens": int},
        "elapsed": float,
    }
"""
import gzip
import hashlib
import json
import math
import pathlib
import random
import re

from forensics.llm_verdict import weak_decisive_line

DEFECT_STRATUM = "defect"
NORMAL_STRATUM = "normal"
UNDETERMINED_STRATUM = "undetermined"


def stratum_of(terminal_bucket, rule_bucket):
    """采样分层：**只看终局判定行能否被规则层命中，绝不是真值。**

    为什么要分层：语料里「终局行类别 ≠ 规则桶」的 case 只占少数（实测约 1/3），
    按比例随机抽 30 例，缺陷层只有 ~10 例，bootstrap 区间宽到 ±30pp，等于没测。
    所以缺陷层要**过采样**（首批 ≥12 例），而分层依据必须能自动算出来 ——
    这里用「终局判定行单独过一遍规则表」当代理指标。

    为什么它不能当真值：拿启发式当评分真值，测出来的只是「LLM 跟我的启发式像不像」，
    没有任何说服力。评分真值只有一个人工裁定表（先冻结真值、再跑 LLM，裁定者看不到 LLM 输出）。
    """
    if not terminal_bucket:
        return UNDETERMINED_STRATUM
    return DEFECT_STRATUM if terminal_bucket != rule_bucket else NORMAL_STRATUM


def scored(pairs):
    """只保留有人工真值的 pair —— 未裁定的不能算进准确率。"""
    return [(pred, truth) for pred, truth in pairs if truth is not None]


def agreement(pairs):
    """现象归因一致率。无有效样本返回 None（而不是 0.0 —— 「没数据」不是「全错」）。"""
    usable = scored(pairs)
    if not usable:
        return None
    return sum(1 for pred, truth in usable if pred == truth) / len(usable)


def agreement_detail(pairs):
    usable = scored(pairs)
    agree = sum(1 for pred, truth in usable if pred == truth)
    return {"n": len(usable), "agree": agree,
            "rate": (agree / len(usable)) if usable else None,
            "unadjudicated": len(pairs) - len(usable)}


def _confusion(pairs):
    """{"真值=>预测": 计数}。用字符串键是为了能直接写进 metrics.json。"""
    counts = {}
    for pred, truth in scored(pairs):
        key = f"{truth}=>{pred}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def confusion(pairs):
    return _confusion(pairs)


def owner_confusion(pairs):
    return _confusion(pairs)


def hallucination_rate(records):
    """引用行里落在证据窗口外的比例 —— 「强制引用证据行」这条纪律是否生效的直接读数。

    分母是**被引用的行**：分母换成日志总行数，这个数字会永远接近 0，等于没测。
    没有引用（全部降级）时返回 None。
    """
    total, outside = 0, 0
    for record in records:
        allowed = set(record.get("allowed") or [])
        for number in record.get("cited") or []:
            total += 1
            if number not in allowed:
                outside += 1
    return (outside / total) if total else None


def weak_decisive_rate(records):
    """把 WARNING/INFO 当决定性依据的比例（只看真正用了 LLM 的 case）。

    这正是规则层误判的病征（首个命中正则胜出 → 良性 WARNING 抢到决策权），
    LLM 若原样复现，它会比准确率更早暴露出来。
    """
    used = [record for record in records if record.get("used")]
    if not used:
        return None
    return sum(1 for record in used if record.get("weak_decisive")) / len(used)


def degrade_rate(records):
    if not records:
        return None
    return sum(1 for record in records if not record.get("used")) / len(records)


def fallback_reasons(records):
    counts = {}
    for record in records:
        if not record.get("used"):
            reason = record.get("fallback_reason") or "unknown"
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def cohens_kappa(pairs):
    """双人裁定的 Kappa。**这是任何准确率的天花板** —— 不报 κ 的准确率站不住。

    两个裁定者对每个 case 各给一个标签；`pairs` = [(标签A, 标签B), ...]。
    机会一致率为 1（例如两人都只给一个标签）时 Kappa 无定义，此时两人全一致取 1.0、否则 0.0。
    """
    usable = [(a, b) for a, b in pairs if a is not None and b is not None]
    if not usable:
        return None
    n = len(usable)
    observed = sum(1 for a, b in usable if a == b) / n
    labels = {label for pair in usable for label in pair}
    expected = 0.0
    for label in labels:
        count_a = sum(1 for a, _ in usable if a == label) / n
        count_b = sum(1 for _, b in usable if b == label) / n
        expected += count_a * count_b
    if math.isclose(1.0 - expected, 0.0):
        return 1.0 if math.isclose(observed, 1.0) else 0.0
    return (observed - expected) / (1.0 - expected)


def percentile(values, quantile):
    """线性插值分位数（与 numpy 默认口径一致，便于与别处对账）。空输入返回 None。"""
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def bootstrap_ci(values, *, n=2000, seed=0):
    """自助法 95% 区间（百分位法）。给定 seed 完全确定 —— 同一批数据两次跑必须同数。"""
    usable = [float(value) for value in values if value is not None]
    if not usable:
        return (None, None)
    if len(usable) == 1:
        return (usable[0], usable[0])
    rng = random.Random(seed)
    size = len(usable)
    means = []
    for _ in range(n):
        means.append(sum(usable[rng.randrange(size)] for _ in range(size)) / size)
    return (percentile(means, 0.025), percentile(means, 0.975))


def agreement_ci(pairs, *, n=2000, seed=0):
    usable = scored(pairs)
    values = [1.0 if pred == truth else 0.0 for pred, truth in usable]
    return bootstrap_ci(values, n=n, seed=seed)


def by_stratum(rows, key="stratum"):
    """分层准确率。**必须分层**：简单样本会把整体准确率抬到 90%，而缺陷层没被测量。"""
    groups = {}
    for row in rows:
        groups.setdefault(row.get(key) or "unknown", []).append(row)
    out = {}
    for name, items in sorted(groups.items()):
        pairs = [(row.get("pred_class"), row.get("truth_class")) for row in items]
        detail = agreement_detail(pairs)
        # n 是**这一层的样本数**，scored 是其中已裁定的条数 —— 两者分开写，
        # 合并成一个键会让「这一层有几条」和「算了几条」悄悄混为一谈。
        out[name] = {"n": len(items), "scored": detail["n"], "agree": detail["agree"],
                     "rate": detail["rate"], "unadjudicated": detail["unadjudicated"]}
    return out


def cost_summary(records):
    """成本与延迟：上线决策看的不是准确率一个数，还有「多少钱一次、多久一条」。"""
    calls = [record for record in records if record.get("usage")]
    prompt = sum(int((record.get("usage") or {}).get("prompt_tokens") or 0)
                 for record in calls)
    completion = sum(int((record.get("usage") or {}).get("completion_tokens") or 0)
                     for record in calls)
    elapsed = [record.get("elapsed") for record in records
               if record.get("elapsed") is not None]
    return {"calls": len(calls), "prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "elapsed_p50": percentile(elapsed, 0.5), "elapsed_p95": percentile(elapsed, 0.95),
            "elapsed_total": (sum(elapsed) if elapsed else None)}


def coverage_split(records):
    """把已裁定的样本按「闭集里到底有没有正确的那只桶」劈成两半。

    为什么非劈不可：整体一致率把性质完全不同的两种错误混成一个数 ——
      - **可表达**（闭集里有正确桶，规则却选了别的）：排序/逻辑缺陷。
        这正是第一阶段要修的东西，换判决器就能改善；
      - **不可表达**（闭集里根本没有正确桶，例如「性能未达标(benchmark)」）：
        **覆盖**缺陷。换判决器也表达不出来，得先往桶表里加桶 ——
        把这类算进「上 LLM 的收益」，就是拿覆盖缺口给判决器记功。
    两半的分母都写出来，读者自己看哪个才是主要缺口。
    """
    groups = {"expressible": [], "not_expressible": [], "unmarked": []}
    for record in records:
        mark = record.get("truth_expressible")
        key = "expressible" if mark is True else \
              "not_expressible" if mark is False else "unmarked"
        groups[key].append(record)
    return {name: agreement_detail([(row.get("pred_class"), row.get("truth_class"))
                                   for row in items])
            for name, items in groups.items()}


def arm_metrics(records, *, n=2000, seed=0):
    """一个臂的全部指标。三臂（规则 / 模型A / 模型B）走**同一个函数** ——
    口径差一点，比出来的差值就没有意义（而「规则臂复现 ~48%」正是靠这个函数校准的）。"""
    pairs = [(record.get("pred_class"), record.get("truth_class")) for record in records]
    owner_pairs = [(record.get("pred_owner"), record.get("truth_owner"))
                   for record in records]
    return {
        "n": len(records),
        "agreement": agreement_detail(pairs),
        "agreement_ci": list(agreement_ci(pairs, n=n, seed=seed)),
        "owner_agreement": agreement(owner_pairs),
        "owner_ci": list(agreement_ci(owner_pairs, n=n, seed=seed)),
        "confusion": confusion(pairs),
        "owner_confusion": owner_confusion(owner_pairs),
        "hallucination_rate": hallucination_rate(records),
        "weak_decisive_rate": weak_decisive_rate(records),
        "degrade_rate": degrade_rate(records),
        "fallback_reasons": fallback_reasons(records),
        "by_stratum": by_stratum(records),
        "coverage_split": coverage_split(records),
        "cost": cost_summary(records),
    }


def first_match_line(text, buckets):
    """规则层命中的**那一行**（桶表按序取首个命中，与 `classify_text` 同序）。"""
    for pattern, label, _owner in buckets:
        match = re.search(pattern, text, re.I)
        if match:
            start = text.rfind("\n", 0, match.start()) + 1
            end = text.find("\n", match.end())
            return label, text[start:end if end >= 0 else len(text)]
    return None, ""


def rule_weak_decisive(text, buckets):
    """规则层的决定性证据是不是 WARNING/INFO 抢中的 —— 这正是它误判的病征本身。

    **必须拿整行去判**。`classify_text` 返回的是命中处 ±30 字的片段，而 GHA 日志每行带
    31 字符的 `2026-…Z ` 时间戳前缀：从前缀之后算起的 30 字回看刚好落在时间戳里，
    「WARNING」不在片段中 —— 用那个片段判，这个指标会**恒为 0**，报表上看着一切正常。
    （第一版就是这么写的，属于「指标自己在骗人」，比没有指标更糟。）
    """
    label, line = first_match_line(text, buckets)
    return label, weak_decisive_line(line, "high")


def render_arm_table(arms):
    """把多个臂渲染成一张 markdown 表 —— 评测报告的主体。"""
    header = ("| 臂 | n（已裁定） | 现象归因一致率（95% CI） | owner 准确率 | 缺陷层一致率 "
              "| 幻觉率 | 弱决定性行率 | 降级率 | 输入 token |")
    rule = "|---|---|---|---|---|---|---|---|---|"
    lines = [header, rule]
    for name, metrics in arms.items():
        agreement = metrics.get("agreement") or {}
        defect = (metrics.get("by_stratum") or {}).get(DEFECT_STRATUM) or {}
        rate = agreement.get("rate")
        ci = metrics.get("agreement_ci") or [None, None]
        rate_text = "—（无真值）" if rate is None else \
            f"{rate:.1%}（{ci[0]:.1%}~{ci[1]:.1%}）"
        owner = metrics.get("owner_agreement")
        defect_text = "—" if defect.get("rate") is None else \
            f"{defect['rate']:.1%}（n={defect.get('n')}）"
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            name, agreement.get("n", 0), rate_text,
            "—" if owner is None else f"{owner:.1%}", defect_text,
            _percent(metrics.get("hallucination_rate")),
            _percent(metrics.get("weak_decisive_rate")),
            _percent(metrics.get("degrade_rate")),
            (metrics.get("cost") or {}).get("prompt_tokens", "—")))
    return "\n".join(lines)


def _percent(value):
    return "—" if value is None else f"{value:.1%}"


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# GHA 原始作业日志的行首格式：`2026-10-08T08:10:57.6240163Z `（首行还带 BOM）。
# 按失败步骤时间窗切分完全依赖它 —— 换一种日志格式，`slice_by_step_window` 会静默返回 None、
# 悄悄回退成全局尾部窗口，而评测结果看起来「照常跑完了」。实测差异：
#   gh api repos/{o}/{r}/actions/jobs/{id}/logs  → 逐行带该前缀（线上取法）
#   某些缓存/导出格式                              → 行首是 `[2026-10-08 08:10:57] [INFO] …`
_LOG_TIMESTAMP_RE = re.compile(r'^﻿?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\b')


# 低于这个占比就认定「不是线上那种日志」。线上取法实测≈1.0，另一种格式≈0.0005。
# 阈值取得很宽（0.5）：目的是挡住**另一种格式**，不是精筛。
MIN_TIMESTAMP_SHARE = 0.5


def timestamps_per_line(text):
    """带 GHA 行首时间戳的行占比。线上取法≈1.0；另一种格式≈0（只有首行有）。"""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return 0.0
    return sum(1 for line in lines if _LOG_TIMESTAMP_RE.match(line)) / len(lines)


def log_format_usable(text, *, min_share=MIN_TIMESTAMP_SHARE):
    """这份日志能不能按步骤时间窗切分？→ (是否可用, 占比)。

    不可用的日志会让 `slice_by_step_window` 静默返回 None、悄悄退回全局尾部窗口 ——
    评测照跑、指标照出，但输入已经不是线上的那一段。宁可拒收，也不要安静的错输入。
    """
    share = timestamps_per_line(text)
    return share >= min_share, share


def verify_fixtures(cases, fixtures_dir):
    """校验冻结集没被动过：按 job_id 找到 fixture，sha 必须等于 cases.jsonl 里记的值。

    返回问题列表（空 = 一致）。**这是评测能不能复现的前提**：fixture 被改了而 sha 没改，
    两次评测的数字就不可比，而报告里看不出来。
    """
    directory = pathlib.Path(fixtures_dir)
    problems = []
    for case in cases:
        job_id = case.get("job_id")
        path = directory / f"{job_id}.txt.gz"
        if not path.exists():
            problems.append(f"缺 fixture：{job_id}")
            continue
        with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as handle:
            text = handle.read()
        actual = sha256_text(text)
        if case.get("scan_sha256") and actual != case["scan_sha256"]:
            problems.append(f"fixture 被改动：{job_id}（{actual[:12]} ≠ "
                            f"{case['scan_sha256'][:12]}）")
    return problems


def read_jsonl(path):
    path = pathlib.Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows
