#!/usr/bin/env python3
"""从冻结集里**确定性**地挑出人工裁定的样本（首批 30 例）。

为什么单独一个脚本而不是随手挑：冻结集的构成必须可复核 —— 评审第一个问题就是
「这 30 例怎么挑的、有没有专挑简单的」。规则写在这里，谁都能重跑出同一份名单。

抽样口径（与方案一致）：
  - **分层 + 失败家族**双重配额：单按分层会让 42 例 benchmark 压满 defect 层，
    于是「缺陷层」实际只测了一个家族；
  - defect 层 20 例（方案要求 ≥12）：benchmark 家族 12、pytest 家族 8；
  - normal 层 5 例（全取）、undetermined 层 4 例（共 8 例，取一半）；
  - 同层同家族内按 (run_id, job_id) 排序后**等间隔**取，避免整套样本挤在同几个 run 里。

家族由**终局区块的文本**判定（不是规则桶）—— 这只用于抽样，绝不作为评分真值。

运行：
  python3 eval/select_sample.py            # 打印名单
  python3 eval/select_sample.py --json     # 输出 job_id 列表（供其他脚本用）
"""
import argparse
import gzip
import json
import pathlib
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# (分层, 家族, 配额)。家族 None = 该层不分家族，整体等间隔取。
QUOTAS = [
    ("defect", "benchmark性能未达标", 12),
    ("defect", "pytest用例失败", 8),
    ("defect", "其他", 1),
    ("normal", "pytest用例失败", 2),
    ("normal", "其他", 3),
    ("undetermined", "k8s调度/就绪", 4),
]

BENCHMARK_MARKERS = ("Performance verification failed", "Benchmark failed")


def family_of(row, text):
    """终局区块文本 → 失败家族。判不出来就归「其他」，不要硬塞。"""
    block = "\n".join(text.splitlines()[number - 1]
                      for number in row["terminal_lines"] if number >= 1)
    if any(marker in block for marker in BENCHMARK_MARKERS):
        return "benchmark性能未达标"
    if "short test summary info" in block or "FAILED " in block:
        return "pytest用例失败"
    if row.get("route") == "pod" or "pod" in (row.get("rule_bucket") or ""):
        return "k8s调度/就绪"
    return "其他"


def load_pool(cases_path, fixtures_dir):
    pool = []
    for line in open(cases_path, encoding="utf-8"):
        row = json.loads(line)
        text = gzip.open(pathlib.Path(fixtures_dir, f"{row['job_id']}.txt.gz"),
                         "rt", encoding="utf-8").read()
        row["family"] = family_of(row, text)
        pool.append(row)
    return pool


def pick_evenly(items, count):
    """等间隔取 count 个。count >= len(items) 时全取。

    等间隔而不是取前 N：池子按 (run_id, job_id) 排序后前 N 个会挤在同一批 run 里，
    那几个 run 大概率是同一版代码跑出来的，等于把样本的独立性丢了。
    """
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[len(items) // 2]]
    # 均匀落在 [0, len-1] 上；用整数步长避免浮点边界
    step = (len(items) - 1) / (count - 1)
    return [items[round(index * step)] for index in range(count)]


def select(pool, quotas=None):
    """按配额挑样本。返回 (选中行, 每档实际入选数)。"""
    quotas = QUOTAS if quotas is None else quotas
    picked, report = [], []
    for stratum, family, count in quotas:
        candidates = [row for row in pool
                      if row["stratum"] == stratum and row["family"] == family]
        candidates.sort(key=lambda row: (str(row.get("run_id")), str(row["job_id"])))
        chosen = pick_evenly(candidates, count)
        report.append((stratum, family, count, len(candidates), len(chosen)))
        picked.extend(chosen)
    # 自检：配额与选中数一致，且每条都真的落在它该在的那一档
    for stratum, family, count, available, got in report:
        assert got == min(count, available), (stratum, family, count, available, got)
        assert got > 0, f"{stratum}/{family} 一例都没挑到 —— 池子变了，配额该跟着改"
    for row in picked:
        assert row["stratum"] and row["family"], row["job_id"]
    assert len({row["job_id"] for row in picked}) == len(picked), "同一 job 被挑进两次"
    return picked, report


def main():
    parser = argparse.ArgumentParser(description="挑出人工裁定的样本")
    parser.add_argument("--cases", default=str(BASE_DIR / "eval/cases.jsonl"))
    parser.add_argument("--fixtures", default=str(BASE_DIR / "eval/fixtures"))
    parser.add_argument("--json", action="store_true", help="只输出 job_id 列表")
    args = parser.parse_args()

    picked, report = select(load_pool(args.cases, args.fixtures))
    if args.json:
        print(json.dumps([row["job_id"] for row in picked]))
        return 0

    print(f"样本共 {len(picked)} 例")
    for stratum, family, want, available, got in report:
        flag = "" if got == want else f"  ← 池里只有 {available}"
        print(f"  {stratum:13s} {family:14s} 取 {got}/{want}{flag}")
    for row in picked:
        print(f"  {row['job_id']}  {row['stratum']:13s} {row['family']:14s} "
              f"{row['rule_bucket']}  终端行 {row['terminal_lines'][:3]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
