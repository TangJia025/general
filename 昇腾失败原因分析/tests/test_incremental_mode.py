#!/usr/bin/env python3
"""定向模式（--run-id/--job-id）的副作用边界测试。

为什么这组测试值得存在（实测踩坑）：定向模式存在的意义是「只消费一个失败」，
但它跑的是**同一个脚本**，脚本里那些「每轮分析都该更新」的落盘动作会顺手执行：
    1. infra 快照写回 —— 有两处 save_infra_store，给一处加锁、漏掉另一处时，
       一次单 job 运行在 3 秒内把 infra_snapshot.json 里本仓的聚合数据覆盖成了 1 条；
    2. 精简版报告章节 —— write_summary() 会按 `仓库@芯片` 整章覆盖 npu_ci_failure_report.md。

这两个后果都不是「本次没数据」，而是**销毁其它运行/其它仓库采集来的数据**，所以要用断言守住
「定向模式一个字都不写」。测试手法：用不存在的 run id 跑一次真正的脚本 —— 此时它一条 gh 调用
就会失败退回，既不依赖网络是否可用，也仍然会走完整个收尾路径（正是要测的那一段）。

运行：python3 tests/test_incremental_mode.py      （无需 pytest，也兼容 pytest）
"""
import json
import pathlib
import subprocess
import sys
import tempfile

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
SCRIPT = BASE_DIR / "npu_ci_failure_analysis.py"

# 不存在的 run：脚本对它的两次 gh 调用都会失败并打印告警后继续，不会真的联网取到数据
BOGUS_RUN = 1


def _run(args):
    completed = subprocess.run([sys.executable, str(SCRIPT)] + args,
                               capture_output=True, text=True, cwd=str(BASE_DIR), timeout=180)
    return completed


def _paths():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="inc_test_"))
    return {
        "dir": tmp,
        "infra_store": tmp / "infra_snapshot.json",
        "summary_file": tmp / "summary.md",
        "handoff": tmp / "handoff.json",
        "report_dir": tmp / "reports",
    }


def test_incremental_writes_no_side_effects():
    """定向模式：不写 infra 快照、不写精简版报告章节，但仍要产出 handoff。"""
    paths = _paths()
    got = _run(["--run-id", str(BOGUS_RUN), "--job-id", "1",
                "--infra-store", str(paths["infra_store"]),
                "--summary-file", str(paths["summary_file"]),
                "--report-dir", str(paths["report_dir"]),
                "--emit-json", str(paths["handoff"])])
    assert got.returncode == 0, f"定向模式退出码 {got.returncode}\n{got.stderr[-1500:]}"
    assert not paths["infra_store"].exists(), \
        "定向模式写了 infra 快照 —— 会把整仓聚合覆盖成单 job 结果"
    assert not paths["summary_file"].exists(), \
        "定向模式写了精简版报告章节 —— 会整章覆盖 npu_ci_failure_report.md"
    # 交接面本身要正常产出：下游（集群取证/历史归因）靠它
    assert paths["handoff"].exists(), "定向模式没产出 --emit-json 交接面"
    payload = json.loads(paths["handoff"].read_text(encoding="utf-8"))
    assert payload["meta"]["repo"] == "vllm-project/vllm-ascend"
    assert payload["failed_jobs"] == [] and payload["classifications"] == []


def test_incremental_skips_workflow_static_filter():
    """定向模式必须跳过 workflow 静态筛选：那一步要为约 100 个 workflow 文件各发一次 gh api。

    判据取「输出里没有 Step1 静态筛出的候选清单」——它是那一步独有的产物。
    """
    paths = _paths()
    got = _run(["--run-id", str(BOGUS_RUN),
                "--infra-store", str(paths["infra_store"]),
                "--summary-file", str(paths["summary_file"]),
                "--report-dir", str(paths["report_dir"])])
    assert "跳过 workflow 静态筛选" in got.stdout, got.stdout[-1500:]
    assert "Step1 静态筛出 NPU CI 候选" not in got.stdout, \
        "定向模式仍跑了 workflow 静态筛选"


def test_job_id_requires_run_id():
    """--job-id 单独给出时必须报错：job 归属哪个 run 无法自行推断。"""
    got = _run(["--job-id", "123"])
    assert got.returncode != 0, "只给 --job-id 竟然正常退出了"
    assert "--run-id" in (got.stdout + got.stderr), (got.stdout + got.stderr)[-500:]


def main():
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = []
    for name, func in tests:
        try:
            func()
            print(f"✅ {name}")
        except AssertionError as exc:
            failed.append(name)
            print(f"❌ {name}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} 通过"
          + (f"，失败：{failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
