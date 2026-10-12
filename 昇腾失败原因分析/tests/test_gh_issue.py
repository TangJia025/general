#!/usr/bin/env python3
"""infra 失败 job 建 issue 的脱敏与幂等。

为什么这组断言值得写：这个功能会往 **PUBLIC** 仓写东西，而 case 里带着内网标识 ——
集群名、节点名（实测多为私网 IP）、镜像仓库、kubeconfig 路径（含工号）、serviceaccount、
runner 名。**漏一个就是一次真实的泄露**，且发出去撤不回。所以这里既正面断言
「该保留的保留」，也逐类正面断言「该抹的必抹」。

第 3 条（自由文本里的标识）是最容易漏的一类：`verdict.basis` 是 LLM 生成的，
标识不在固定字段里，只做字段白名单会漏 —— 实测真实报告里 `basis` 就嵌着节点名。

运行：python3 -B tests/test_gh_issue.py      （无需 pytest，也兼容 pytest）
"""
import json
import pathlib
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
sys.path.insert(0, str(BASE_DIR))

from forensics import gh_issue                                              # noqa: E402

PUBLIC_LINK = ("https://github.com/vllm-project/vllm-ascend/actions/"
               "runs/38216567152/job/114704511118")


def _case(**overrides):
    """一个仿真的 infra case，带齐真实报告里那些会被写出去的敏感字段。"""
    case = {
        "workflow": ".github/workflows/schedule_nightly_test_a3.yaml",
        "job_name": "multi-node (main, DeepSeek-V4-Pro-perf)",
        "job_id": 114704511118,
        "run_id": 38216567152,
        "link": PUBLIC_LINK,
        "bucket": "HCCL 集合通信失败",
        "owner": "infra",
        "chip": "a3",
        "step": "Stream logs",
        "labels": ["linux-aarch64-a3-800t-0"],
        "runner_name": "linux-aarch64-a3-800t-0-chlqk-runner-gnnj4",
        "sig": "FNAME=eth0 HCCL_CONNECT_TIMEOUT=120",
        "cluster": {
            "cluster_name": "ascend-cn12-001-cluster",
            "kubeconfig_path": ("/home/tangjia/kconf/asci/tangjia_upload_1790164318_"
                                "ascend-cn12-001-cluster-kubeconfig_ascend_kb.yaml"),
            "filename": "tangjia_upload_1790164318_ascend-cn12-001-cluster-kubeconfig_ascend_kb.yaml",
            "candidates": ["ascend-cn12-001-cluster"],
            "queried_namespaces": {"ascend-cn12-001-cluster": ["vllm-project", "vllm-project-vllm-ascend"]},
            "identity": {"identity": "system:serviceaccount:ascend:sa-for-tangjia-00969236"},
            "pod_evidence": {
                "pod": "linux-aarch64-a3-800t-0-chlqk-runner-gnnj4",
                "node": "172.16.1.108",
                "namespace": "vllm-project-vllm-ascend",
                "containers": [{"container": "runner",
                                "image": "swr.cn-southwest-2.myhuaweicloud.com/modelfoundry/x:1"}],
            },
            "logs": [{"text": "cache at ascend-ci-cache-hz1"}],
        },
        "history": [{"issue": {"signatures": ["linux-aarch64-a3-16-ww2np-runner-6fkxd"]}}],
        "verdict": {
            "root_cause": "aicore 超时，节点 172.16.1.108 上的 rank 未同步",
            "basis": [
                "日志侧：命中桶【HCCL 集合通信失败】，owner=infra",
                "集群侧：pod 已找到（phase=Running，节点 `gy005`），快照取自 job 结束前",
                "历史先例：#275 命中带机制的签名 linux-amd64-a5-4-xcknp-runner-mzltm",
            ],
        },
    }
    case.update(overrides)
    return case


# ---------------------------------------------------------------- 脱敏


def test_scrub_redacts_each_internal_shape():
    """逐类正面断言：每一种内网标识都必须被抹掉。"""
    samples = {
        "集群名": "ascend-cn12-001-cluster",
        "集群名(另一处)": "ascend-hk-001-cluster",
        "私网IP(172)": "172.16.1.108",
        "私网IP(10)": "10.0.0.7",
        "私网IP(192)": "192.168.0.109",
        "主机名": "gy005",
        "镜像仓库": "swr.cn-southwest-2.myhuaweicloud.com/modelfoundry/x:1",
        "服务账号": "sa-for-tangjia-00969236",
        "kubeconfig路径": "/home/tangjia/kconf/asci/whatever.yaml",
        "kubeconfig文件名": "tangjia_upload_1790164318_foo.yaml",
        "runner名": "linux-aarch64-a3-800t-0-chlqk-runner-gnnj4",
        "命名空间": "vllm-project-vllm-ascend",
    }
    for name, sample in samples.items():
        scrubbed = gh_issue.scrub(sample)
        assert sample not in scrubbed, "%s 没被抹掉：%r" % (name, scrubbed)


def test_scrub_keeps_public_values():
    """反向断言：公开值不能被误伤 —— 脱敏脱过头会让 issue 变成废纸。"""
    for keep in (
        PUBLIC_LINK,                                  # CI 链接必须完整
        "vllm-project/vllm-ascend",                   # 公开仓
        "linux-aarch64-a3-800t-0",                    # 公开的 runner 标签
        "HCCL 集合通信失败",                            # 归因标签
        ".github/workflows/schedule_nightly_test_a3.yaml",
        "FNAME=eth0 HCCL_CONNECT_TIMEOUT=120",
    ):
        assert gh_issue.scrub(keep) == keep, "公开值被误伤：%r → %r" % (
            keep, gh_issue.scrub(keep))


def test_harvest_skips_values_present_in_public_link():
    """出现公开在 link 里的值不可收 —— 典型是 queried_namespaces 里的 vllm-project。

    照收会在渲染时把 CI 链接打成 github.com/<已脱敏>/vllm-ascend/...，
    链接一坏，issue 的价值就没了。
    """
    literals = gh_issue.harvest_sensitive(_case())
    assert "vllm-project" not in literals, "收进了公开 org 名 vllm-project"
    assert "vllm-project-vllm-ascend" in literals, "内网命名空间名该收却没收到"


def test_harvest_skips_container_names():
    """容器名可能是 runner 这种通用词，收了会把正文里所有 runner 抹掉。"""
    assert "runner" not in gh_issue.harvest_sensitive(_case())


def test_body_scrubs_free_text_identifiers():
    """核心回归：标识藏在 LLM 自由文本里时也必须被抹掉。

    verdict.root_cause / basis 是模型生成的，标识不在固定字段里 ——
    只做字段白名单的实现在这里必挂。
    """
    body = gh_issue.build_issue_body(_case())
    for leaked in ("172.16.1.108", "gy005", "ascend-cn12-001-cluster",
                   "swr.cn-southwest-2", "sa-for-tangjia",
                   "linux-amd64-a5-4-xcknp-runner-mzltm"):
        assert leaked not in body, "自由文本里的 %r 漏出去了" % leaked
    assert "已脱敏" in body, "既然抹了就该留下占位符，否则读者不知道这里有内容被去掉"


def test_body_keeps_actionable_fields():
    """脱敏之后仍要能派活：CI 链接、归因、根因、job id 都得在。"""
    body = gh_issue.build_issue_body(_case())
    assert PUBLIC_LINK in body, "CI 链接丢了，issue 就没法点回原始失败"
    assert "114704511118" in body
    assert "HCCL 集合通信失败" in body
    assert "owner=infra" in body
    assert gh_issue.issue_marker(114704511118) in body, "去重标记必须在正文里"


def test_title_scrubs_bucket():
    case = _case(bucket="集群 ascend-cn12-001-cluster 上的 HCCL 失败")
    title = gh_issue.build_issue_title(case, gh_issue.harvest_sensitive(case))
    assert "ascend-cn12-001-cluster" not in title
    assert "114704511118" in title


# ---------------------------------------------------------------- 幂等


class _FakeGh:
    """假 gh。记录调用，按剧本返回。"""

    def __init__(self, existing=None, create_url="https://github.com/o/r/issues/7",
                 create_fails=False):
        self.existing = existing or []
        self.create_url = create_url
        self.create_fails = create_fails
        self.calls = []

    def __call__(self, args, stdin_text=None, timeout=90.0):
        self.calls.append((list(args), stdin_text))
        if args[:2] == ["issue", "list"]:
            return 0, json.dumps(self.existing), ""
        if args[:2] == ["issue", "create"]:
            if self.create_fails:
                return 1, "", "boom: 没有权限\n"
            return 0, self.create_url + "\n", ""
        return 0, "", ""

    def kinds(self):
        return [args[1] for args, _ in self.calls]


def test_ensure_issue_uses_local_state_without_calling_gh():
    """本地映射命中就不该再问远端 —— 这是快路径，也是省 API 配额的关键。"""
    fake = _FakeGh()
    state = {"114704511118": "https://github.com/o/r/issues/1"}
    url, note = gh_issue.ensure_issue(_case(), "o/r", state, runner=fake)
    assert url == "https://github.com/o/r/issues/1"
    assert fake.calls == [], "本地已有时仍调了 gh：%r" % (fake.kinds(),)


def test_ensure_issue_reuses_remote_issue_found_by_marker():
    """state 丢了（换机器/清目录）时靠正文标记兜住，不能重复建。"""
    fake = _FakeGh(existing=[{"number": 3, "url": "https://github.com/o/r/issues/3",
                              "title": "x", "body": gh_issue.issue_marker(114704511118)}])
    state = {}
    url, note = gh_issue.ensure_issue(_case(), "o/r", state, runner=fake)
    assert url == "https://github.com/o/r/issues/3"
    assert "create" not in fake.kinds(), "已存在却还建了一个"
    assert state["114704511118"] == url, "找到的 URL 应记进 state，下轮走快路径"


def test_ensure_issue_ignores_marker_of_other_job():
    """搜到别人的 issue 不算数 —— 否则两个 job 会共用一个 issue。"""
    fake = _FakeGh(existing=[{"number": 3, "url": "https://github.com/o/r/issues/3",
                              "title": "x", "body": gh_issue.issue_marker(999)}])
    url, note = gh_issue.ensure_issue(_case(), "o/r", {}, runner=fake)
    assert url == "https://github.com/o/r/issues/7", "应新建，而不是复用别的 job 的 issue"


def test_ensure_issue_creates_and_records():
    fake = _FakeGh(create_url="https://github.com/o/r/issues/9")
    state = {}
    url, note = gh_issue.ensure_issue(_case(), "o/r", state, runner=fake)
    assert url == "https://github.com/o/r/issues/9"
    assert state["114704511118"] == url
    assert "create" in fake.kinds()


def test_created_body_goes_through_stdin_and_is_scrubbed():
    """正文走 stdin（避免命令行长度/转义问题），且真正发出去的必须是脱敏后的。"""
    fake = _FakeGh()
    gh_issue.ensure_issue(_case(), "o/r", {}, runner=fake)
    create_call = [c for c in fake.calls if c[0][:2] == ["issue", "create"]][0]
    assert "--body-file" in create_call[0] and create_call[0][-1] == "-"
    body = create_call[1]
    assert body and "172.16.1.108" not in body, "发出去的正文没脱敏"


# ---------------------------------------------------------------- 批量入口


def test_ensure_infra_issues_only_touches_infra():
    """code / mixed / unknown 一概不建 —— 只有 infra 才该进 issue。"""
    fake = _FakeGh()
    cases = [_case(owner="infra", job_id=1),
             _case(owner="code", job_id=2),
             _case(owner="mixed", job_id=3),
             _case(owner="unknown", job_id=4)]
    mapping, notes = gh_issue.ensure_infra_issues(cases, "o/r", {}, runner=fake)
    assert list(mapping) == ["1"], "只该建 infra 的，实际 %r" % (mapping,)
    assert len(fake.calls) == 2, "另三类不该各问一次远端：%r" % (fake.kinds(),)


def test_ensure_infra_issues_never_raises_on_failure():
    """建 issue 失败只记日志。通知链路不能因为附属动作失败而发不出邮件。"""
    fake = _FakeGh(create_fails=True)
    mapping, notes = gh_issue.ensure_infra_issues([_case()], "o/r", {}, runner=fake)
    assert mapping == {}
    assert any("未建 issue" in note for note in notes), notes

    def exploding(args, stdin_text=None, timeout=90.0):
        raise RuntimeError("gh 炸了")

    mapping, notes = gh_issue.ensure_infra_issues([_case()], "o/r", {}, runner=exploding)
    assert mapping == {}, "抛异常必须被吞掉"
    assert any("抛异常" in note for note in notes), notes


def test_state_roundtrip(tmpdir=None):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "sub" / "issues.json"
        gh_issue.save_state({"1": "u1"}, str(path))
        assert gh_issue.load_state(str(path)) == {"1": "u1"}
        # 文件不存在时返回空表而不是抛异常 —— 首次运行就是这个情况
        assert gh_issue.load_state(str(pathlib.Path(tmp) / "nope.json")) == {}


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
