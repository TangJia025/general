#!/usr/bin/env python3
"""冻结集构建：把「喂给判决的输入」冻结下来，供评测反复复现。

**与线上同源**，这是本脚本唯一要紧的性质：
  - 日志取法与流水线完全一致（`gh api repos/{owner}/{repo}/actions/jobs/{id}/logs`）；
  - 扫描窗用**生产代码本身**的 `build_scan_window`（AST 抽取，见 eval/production.py），
    不是另写一遍 —— 另写一遍的话，评测输入与线上就不是同一个东西，准确率也就没有意义；
  - 规则臂的桶用**生产代码本身**的 `classify_text` 现算。

产出：
  eval/cases.jsonl               入库：只有 job_id / sha256 / 桶 / 分层，**不含日志原文**
  eval/fixtures/<job_id>.txt.gz  **gitignored** —— 仓库是 PUBLIC，日志含内网集群名/节点名/
                                 镜像地址。可复现性由 job_id + scan_sha256 保证：按 job_id 重取
                                 日志、重跑本脚本，sha 必须逐字节相同。

用法：
  python3 eval/build_fixtures.py                      # 用本地缓存日志
  python3 eval/build_fixtures.py --fetch-missing      # 缺的用 gh 拉（会联网）
  python3 eval/build_fixtures.py --limit 30
"""
import argparse
import gzip
import json
import pathlib
import subprocess
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from forensics.eval_metrics import (log_format_usable, sha256_text,  # noqa: E402
                                    stratum_of)
from forensics.llm_evidence import find_terminal_lines         # noqa: E402

# 这两类路由**不进评测**：no_log 是「步骤名直接定性、根本不读日志」，aggregate 是
# 「别的 job 挂了所以我也挂」的级联 —— 它们没有「读日志下结论」这件事，与 LLM 无关。
EXCLUDED_ROUTES = ("no_log", "aggregate")


def read_handoffs(directory):
    """读全部 handoff，按 job_id 去重（同一 job 可能出现在多份 handoff 里）。"""
    directory = pathlib.Path(directory)
    candidates, seen = [], {}
    for path in sorted(directory.glob("handoff_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"⚠️  跳过损坏的 handoff {path.name}: {exc}")
            continue
        repo = (payload.get("meta") or {}).get("repo") or ""
        for item in payload.get("classifications") or []:
            job_id = str(item.get("job_id"))
            if not job_id or job_id in seen:
                continue
            seen[job_id] = path.name
            candidates.append({
                "job_id": job_id, "repo": repo,
                "run_id": item.get("run_id"), "job_name": item.get("job_name"),
                "workflow": item.get("workflow"), "step": item.get("step"),
                "bucket_in_handoff": item.get("bucket"), "owner_in_handoff": item.get("owner"),
                "chip": item.get("chip"), "labels": item.get("labels") or [],
                "link": item.get("link"), "sig_source": item.get("sig_source"),
                "duplicate_in_handoff": bool(item.get("duplicate")),
                "is_npu": bool(item.get("is_npu")),
                "failed_step": {"name": item.get("step"),
                                "started_at": item.get("failed_step_started_at"),
                                "completed_at": item.get("failed_step_completed_at")},
                "handoff": path.name,
            })
    return candidates


def read_log(log_dir, job_id, repo, fetch_missing=False):
    """返回 (原始日志文本, 问题码)。问题码非 None 时文本不可用。

    取法与流水线一致：`gh api repos/{owner}/{repo}/actions/jobs/{id}/logs` 返回的是**纯文本**
    （每一行带 `2026-…Z ` 时间戳前缀），不是 zip —— 时间戳前缀正是按步骤时间窗切分的依据。

    **格式闸门**：缓存目录里可能混着另一种格式（行首是 `[2026-10-08 08:10:57] [INFO] …`，
    只有首行有 GHA 前缀）。那种日志会让 `slice_by_step_window` 静默返回 None、悄悄退回
    全局尾部窗口 —— 评测照跑、结果看起来正常，但输入已经不是线上的那一段了。
    所以比例过低一律拒绝，不参与构建。
    """
    path = pathlib.Path(log_dir) / f"{job_id}.txt"
    text = None
    if path.exists():
        text = path.read_text(encoding="utf-8", errors="ignore")
    elif fetch_missing and repo:
        owner_repo = repo if "/" in repo else f"vllm-project/{repo}"
        proc = subprocess.run(["gh", "api", f"repos/{owner_repo}/actions/jobs/{job_id}/logs"],
                              capture_output=True)
        if proc.returncode == 0 and proc.stdout:
            text = proc.stdout.decode("utf-8", errors="ignore")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    if text is None:
        return None, "missing"
    usable, share = log_format_usable(text)
    if not usable:
        return None, f"log_format:行首时间戳仅 {share:.0%}"
    return text, None


def build_case(candidate, text, *, tail_lines, no_step_window=False):
    """用生产窗口算法算扫描窗，返回 (case 行, 扫描窗文本) 或 (None, 跳过原因)。"""
    from eval.production import production
    ns = production()
    step_name = candidate["failed_step"]["name"] or ""
    route, forced_owner, _note = ns["route_for"](step_name)
    if route in EXCLUDED_ROUTES or forced_owner is not None:
        return None, f"route={route}"

    scan_lines, windowed = ns["build_scan_window"](
        text, failed_step=candidate["failed_step"], route=route, tail_lines=tail_lines,
        no_step_window=no_step_window)
    scan_text = "\n".join(scan_lines) or text
    rule_bucket, _rule_sig = ns["classify_text"](scan_text)

    # 分层代理指标：终局判定行**单独**过一遍规则表。只用于采样分层（缺陷层要过采样），
    # **绝不是评分真值** —— 真值只有人工裁定表。
    terminal_numbers = find_terminal_lines(scan_text)
    terminal_text = "\n".join(scan_text.splitlines()[number - 1]
                              for number in terminal_numbers if number >= 1)
    terminal_bucket = ns["classify_text"](terminal_text)[0] if terminal_numbers else None

    # 窗口有没有把判据切掉：原始日志里有终局行、扫描窗里却一条都不剩 → 这是窗口的缺陷，
    # 会让 LLM 在证据里根本找不到判据。这个计数必须长期为 0，涨了就说明窗口算法坏了。
    raw_terminal = find_terminal_lines(text)
    lost_terminal = bool(raw_terminal) and not terminal_numbers

    row = {
        "job_id": candidate["job_id"], "run_id": candidate["run_id"],
        "repo": candidate["repo"], "job_name": candidate["job_name"],
        "workflow": candidate["workflow"], "step": step_name, "route": route,
        "chip": candidate["chip"], "labels": candidate["labels"],
        "link": candidate["link"], "is_npu": candidate["is_npu"],
        "failed_step": candidate["failed_step"],
        # 注意：**不写 rule_sig**（规则命中的证据片段）。cases.jsonl 是要入库的，
        # 而片段取自日志正文，可能带内网集群名/镜像地址（仓库是 PUBLIC）。
        # 规则臂在评测时从 fixture 现算，本来也不需要它。
        "rule_bucket": rule_bucket, "rule_owner": ns["BUCKET_OWNER"].get(rule_bucket),
        "bucket_in_handoff": candidate["bucket_in_handoff"],
        # 规则层是否与当初那次分析一致 —— 窗口/桶表改动会在这里显形（不是错误，是信号）
        "rule_drift": candidate["bucket_in_handoff"] != rule_bucket,
        "terminal_bucket": terminal_bucket,
        "stratum": stratum_of(terminal_bucket, rule_bucket),
        "windowed": bool(windowed), "scan_lines": len(scan_text.splitlines()),
        "raw_lines": len(text.splitlines()), "terminal_lines": terminal_numbers,
        "lost_terminal": lost_terminal,
        "scan_sha256": sha256_text(scan_text), "raw_sha256": sha256_text(text),
        "duplicate_in_handoff": candidate["duplicate_in_handoff"],
        "sig_source": candidate["sig_source"], "handoff": candidate["handoff"],
    }
    return (row, scan_text), None


def write_fixture(out_dir, job_id, scan_text):
    path = pathlib.Path(out_dir) / "fixtures" / f"{job_id}.txt.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    # mtime 固定为 0：gzip 头里带时间戳，不固定的话两次构建的字节不同，
    # 「跑两遍 sha 全等」这条验证就永远过不了（sha 记的是解压后的文本，但定死 mtime
    # 让「文件字节」也可复现，便于 rsync/比对）
    with gzip.GzipFile(filename="", mode="wb", fileobj=open(path, "wb"), mtime=0) as handle:
        handle.write(scan_text.encode("utf-8"))
    return path


def main():
    parser = argparse.ArgumentParser(description="构建 LLM 判决评测的冻结集")
    parser.add_argument("--handoffs", default=str(BASE_DIR / ".forensics_state/handoffs"))
    # 默认目录**故意**不是 /tmp/ci_logs：那里的缓存是另一种格式（harness 自带时间戳、
    # 行首无 GHA 前缀），按它切不出步骤时间窗，会静默退化成全局尾部窗口。
    parser.add_argument("--log-dir", default="/tmp/npu_ci_joblogs",
                        help="线上同源日志缓存目录（gh api jobs/{id}/logs 的纯文本）")
    parser.add_argument("--out", default=str(BASE_DIR / "eval"))
    parser.add_argument("--tail-lines", type=int, default=1200)
    parser.add_argument("--no-step-window", action="store_true")
    parser.add_argument("--fetch-missing", action="store_true", help="缺日志时用 gh 拉（联网）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个候选（0=全部）")
    parser.add_argument("--job-id", action="append", help="只处理指定 job（可重复，用于复现单例）")
    args = parser.parse_args()

    candidates = read_handoffs(args.handoffs)
    # 新例优先：冻结集要反映**当前**流水线的行为，旧 run 是另一版代码跑出来的。
    candidates.sort(key=lambda item: int(item.get("run_id") or 0), reverse=True)
    print(f"handoff 里的失败 job（去重后）：{len(candidates)}（新例优先）")
    if args.limit:
        candidates = candidates[:args.limit]

    # 清掉上一轮的 fixture：冻结集以本次 cases.jsonl 为准，残留的旧文件会让
    # 「fixture 与案例一一对应」这个前提悄悄失效（多出来的文件没人校验）。
    fixtures_dir = pathlib.Path(args.out, "fixtures")
    stale = list(fixtures_dir.glob("*.txt.gz")) if fixtures_dir.exists() else []
    for path in stale:
        path.unlink()
    if stale:
        print(f"清理上一轮 fixture：{len(stale)} 个")

    if args.job_id:
        wanted = {str(item) for item in args.job_id}
        candidates = [item for item in candidates if item["job_id"] in wanted]
        print(f"按 --job-id 过滤后：{len(candidates)}")

    rows, skipped = [], {}
    for candidate in candidates:
        text, problem = read_log(args.log_dir, candidate["job_id"], candidate["repo"],
                                 fetch_missing=args.fetch_missing)
        if text is None:
            skipped[problem] = skipped.get(problem, 0) + 1
            continue
        row, reason = build_case(candidate, text, tail_lines=args.tail_lines,
                                 no_step_window=args.no_step_window)
        if row is None:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        write_fixture(args.out, candidate["job_id"], row[1])
        rows.append(row[0])

    cases_path = pathlib.Path(args.out) / "cases.jsonl"
    cases_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cases_path, "w", encoding="utf-8") as handle:
        for row in sorted(rows, key=lambda item: str(item["job_id"])):
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    strata = {}
    for row in rows:
        strata[row["stratum"]] = strata.get(row["stratum"], 0) + 1
    buckets = {}
    for row in rows:
        buckets[row["rule_bucket"]] = buckets.get(row["rule_bucket"], 0) + 1
    print(f"入库 {len(rows)} 例 → {cases_path}")
    print(f"  分层：{strata}")
    print(f"  窗口切分成功：{sum(1 for r in rows if r['windowed'])}/{len(rows)}")
    print(f"  判据被窗口切掉（lost_terminal，应为 0）：{sum(1 for r in rows if r['lost_terminal'])}")
    print(f"  规则桶与 handoff 不一致（rule_drift）：{sum(1 for r in rows if r['rule_drift'])}")
    missing = skipped.pop("missing", 0)
    print(f"  跳过：{skipped or '无'}；缺日志：{missing}"
          + ("（加 --fetch-missing 可拉取）" if missing and not args.fetch_missing else ""))
    print("  规则桶分布 top5：" + "、".join(
        f"{name}×{count}" for name, count in
        sorted(buckets.items(), key=lambda item: -item[1])[:5]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
