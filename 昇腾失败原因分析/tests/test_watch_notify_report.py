#!/usr/bin/env python3
"""--notify-command 的报告路径与失败可见性。

为什么这组断言值得写：通知是整条链路的**最后一跳**，也是唯一一跳在告警脚本之外。
它坏掉时不会有任何症状 —— 判决照跑、报告照出、台账照记，只是没有邮件。实际就这么
静默失效过一段时间，两个原因叠在一起：

    1. 传错格式：分析工具对同一 stamp 写 .md（给人看）和 .json（给机器读），但 stdout
       里 `报告:` 那行打印的是 .md；而消费方 npu_ci_notify.py 要 .json，喂 .md 进去
       在 json.load 处 sys.exit(1)。两边各自都对，接起来是断的。
    2. 失败不可见：notify() 不看返回码，capture_output=True 又吞掉 stderr 且从不打印，
       于是消费方 exit(1) 了监听器照样打「通知命令已执行」。

第 1 条靠 report_json_path 的断言守，第 2 条靠「exit!=0 时必须记失败、且不得记已执行」守。

运行：python3 tests/test_watch_notify_report.py      （无需 pytest，也兼容 pytest）
"""
import os
import pathlib
import stat
import sys
import tempfile
import types

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
sys.path.insert(0, str(BASE_DIR))

import npu_ci_watch                                                       # noqa: E402


def _touch(path: str, content: str = "x") -> str:
    pathlib.Path(path).write_text(content, encoding="utf-8")
    return path


def _script(directory: str, name: str, body: str) -> str:
    """写一个可执行的小脚本，代替真的通知命令。"""
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _FakeWatcher:
    """只带 notify() 用到的两样东西：args.notify_command 与可调用的 log。"""

    def __init__(self, notify_command: str):
        self.args = types.SimpleNamespace(notify_command=notify_command)
        self.messages = []
        self.log = self.messages.append

    def notify(self, report_path, job_ids):
        return npu_ci_watch.Watcher.notify(self, report_path, job_ids)

    def joined(self) -> str:
        return "\n".join(self.messages)


def test_report_json_path_prefers_json_sibling():
    with tempfile.TemporaryDirectory() as tmp:
        md = _touch(os.path.join(tmp, "forensics_report_20261012_145256.md"))
        js = _touch(os.path.join(tmp, "forensics_report_20261012_145256.json"), "{}")
        assert npu_ci_watch.report_json_path(md) == js

        # 已经是 .json 就原样返回
        assert npu_ci_watch.report_json_path(js) == js

        # 没有同名 .json 时必须返回 None —— 宁可明说不通知，
        # 也不要把一个明知消费方读不了的路径递过去（那正是这次的故障形态）
        lonely = _touch(os.path.join(tmp, "forensics_report_20261012_999999.md"))
        assert npu_ci_watch.report_json_path(lonely) is None

        # .json 路径本身不存在也算找不到
        assert npu_ci_watch.report_json_path(os.path.join(tmp, "nope.json")) is None

        for empty in (None, ""):
            assert npu_ci_watch.report_json_path(empty) is None


def test_notify_passes_json_not_markdown():
    with tempfile.TemporaryDirectory() as tmp:
        md = _touch(os.path.join(tmp, "r.md"))
        js = _touch(os.path.join(tmp, "r.json"), "{}")
        argv_log = os.path.join(tmp, "argv.txt")
        cmd = _script(tmp, "ok.sh", 'printf "%%s\\n" "$@" > %s\nexit 0\n' % argv_log)

        watcher = _FakeWatcher(cmd)
        watcher.notify(md, [114689497253, 114690552433])

        lines = pathlib.Path(argv_log).read_text(encoding="utf-8").splitlines()
        assert lines[0] == js, "传给通知命令的第一个参数必须是 .json，实际 %r" % (lines[0],)
        assert lines[1] == "114689497253,114690552433", "job_id 要逗号分隔，实际 %r" % (lines[1],)
        assert "通知命令已执行" in watcher.joined()
        # 改用了 JSON 就该说出来，否则下次又没人知道发生了什么
        assert "改用 JSON 报告" in watcher.joined()


def test_notify_failure_is_logged_not_swallowed():
    """核心回归：消费方 exit(1) 时，必须记下失败，且不得记「已执行」。"""
    with tempfile.TemporaryDirectory() as tmp:
        md = _touch(os.path.join(tmp, "r.md"))
        _touch(os.path.join(tmp, "r.json"), "{}")
        cmd = _script(tmp, "fail.sh",
                      'echo "boom: 缺少必需的环境变量 ALERT_TO" >&2\nexit 1\n')

        watcher = _FakeWatcher(cmd)
        watcher.notify(md, [1])

        joined = watcher.joined()
        assert "通知命令失败" in joined, "失败必须留痕，实际日志：%r" % (joined,)
        assert "exit=1" in joined, "要带上退出码，实际日志：%r" % (joined,)
        assert "ALERT_TO" in joined, "要带上 stderr 末行，否则等于没说，实际日志：%r" % (joined,)
        assert "通知命令已执行" not in joined, "失败时绝不能记「已执行」——这正是当初的静默点"


def test_notify_skips_when_no_json_sibling():
    """找不到 .json 时连命令都不该跑 —— 跑也只是让消费方再失败一次。"""
    with tempfile.TemporaryDirectory() as tmp:
        md = _touch(os.path.join(tmp, "only.md"))
        marker = os.path.join(tmp, "ran.txt")
        cmd = _script(tmp, "mark.sh", 'touch %s\nexit 0\n' % marker)

        watcher = _FakeWatcher(cmd)
        watcher.notify(md, [1])

        assert not os.path.exists(marker), "没有 .json 时不该执行通知命令"
        assert "通知未执行" in watcher.joined()


def test_notify_timeout_is_logged():
    with tempfile.TemporaryDirectory() as tmp:
        md = _touch(os.path.join(tmp, "r.md"))
        _touch(os.path.join(tmp, "r.json"), "{}")
        cmd = _script(tmp, "slow.sh", "sleep 5\nexit 0\n")

        watcher = _FakeWatcher(cmd)
        original = npu_ci_watch.subprocess.run

        def fake_run(*args, **kwargs):
            raise npu_ci_watch.subprocess.TimeoutExpired(cmd, 60)
        npu_ci_watch.subprocess.run = fake_run
        try:
            watcher.notify(md, [1])
        finally:
            npu_ci_watch.subprocess.run = original

        assert "超时" in watcher.joined()
        assert "通知命令已执行" not in watcher.joined()


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print("FAIL  %s\n      %s" % (test.__name__, exc))
        else:
            print("PASS  %s" % test.__name__)
    print("\n%d/%d 通过" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
