#!/usr/bin/env python3
"""LLM 客户端的重试与错误分类回归（**全部用注入的假 transport，不出网**）。

为什么守这些：
  - **4xx 必须不重试**：4xx 是 prompt 或凭据的 bug，重试只是三次烧钱买同一个错误；
  - **429/5xx/超时可重试**：这三类是外部抖动，是重试真正值钱的地方；
  - **空 content 是可恢复错误**：DeepSeek 是推理模型，`max_tokens` 给小了 token 会全花在
    `reasoning_content` 上、`content` 返回空串。一次翻倍预算重试能救回来一批判决，
    而把空串当异常抛出去会杀掉整轮报告 —— 这是实测踩出来的，不是假想；
  - **错误必须具名**：`http_429` 要退避、`http_4xx` 要改 prompt、`empty_content` 要加预算，
    动作完全不同。打成一句「LLM 不可用」这几种就都看不见了。

运行：python3 tests/test_llm_client.py      （无需 pytest，也兼容 pytest）
"""
import json
import pathlib
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
sys.path.insert(0, str(BASE_DIR))

from forensics import llm_client as lc    # noqa: E402


def _body(content="{}", usage=None, **extra):
    payload = {"choices": [{"message": {"content": content}}],
               "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5}}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


class Transport:
    """按剧本出 (status, body) 或抛 LLMTransportError；记录每次 payload。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "headers": dict(headers),
                           "payload": dict(payload), "timeout": timeout})
        item = self.script.pop(0) if self.script else self.script_default
        if isinstance(item, Exception):
            raise item
        return item


def _client(script, default=(500, "{}"), **kwargs):
    """default 是剧本用完后一直返回的那条 —— 测试「最终错误」时要显式给，
    否则默认的 500 会把想验证的那个错误盖掉（这正是第一版测试踩的坑）。"""
    transport = Transport(script)
    transport.script_default = default
    kwargs.setdefault("max_retries", 2)
    client = lc.DeepSeekClient(api_key="sk-test", transport=transport,
                              sleep=lambda seconds: None, **kwargs)
    return client, transport


# ---------------- 正常路径 ----------------

def test_success_returns_text_and_usage():
    client, transport = _client([(200, _body('{"a":1}'))])
    result = client.judge("sys", "user")
    assert result.ok and result.text == '{"a":1}' and result.attempts == 1
    assert result.usage["prompt_tokens"] == 10
    assert transport.calls[0]["url"].endswith("/chat/completions")
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer sk-test"
    assert transport.calls[0]["payload"]["model"] == lc.DEFAULT_MODEL
    assert transport.calls[0]["payload"]["messages"][0] == {"role": "system",
                                                            "content": "sys"}


def test_reasoning_model_needs_a_real_token_budget():
    """实测：预算给小了 content 是空串。默认必须给足，否则线上会大批降级。"""
    assert lc.DEFAULT_MAX_TOKENS >= 4000


def test_json_mode_can_be_turned_off():
    client, transport = _client([(200, _body())], json_mode=False)
    client.judge("s", "u")
    assert "response_format" not in transport.calls[0]["payload"]
    client, transport = _client([(200, _body())])
    client.judge("s", "u")
    assert transport.calls[0]["payload"]["response_format"] == {"type": "json_object"}


def test_missing_api_key_is_refused_loudly():
    try:
        lc.DeepSeekClient(api_key="")
    except ValueError:
        return
    raise AssertionError("没有 api_key 竟然构造成功了")


# ---------------- 重试与分类 ----------------

def test_429_is_retried_then_succeeds():
    client, transport = _client([(429, "{}"), (200, _body('{"ok":1}'))])
    result = client.judge("s", "u")
    assert result.ok and result.attempts == 2 and len(transport.calls) == 2


def test_5xx_is_retried_up_to_the_limit():
    client, transport = _client([(500, "{}"), (503, "{}"), (502, "{}")])
    result = client.judge("s", "u")
    assert not result.ok and result.error == "http_5xx" and result.attempts == 3


def test_4xx_is_not_retried():
    """4xx 是 prompt/凭据的 bug，重试纯烧钱 —— 这里是「不重试」的守门测试。"""
    client, transport = _client([(400, '{"error":"bad request"}'), (200, _body("x"))])
    result = client.judge("s", "u")
    assert not result.ok and result.error == "http_4xx" and result.attempts == 1
    assert len(transport.calls) == 1, "4xx 被重试了"


def test_timeout_is_retried_and_named():
    client, transport = _client([lc.LLMTransportError("timeout", "120s"),
                                 lc.LLMTransportError("timeout", "120s"),
                                 lc.LLMTransportError("timeout", "120s")])
    result = client.judge("s", "u", timeout=120.0)
    assert not result.ok and result.error == "timeout" and result.attempts == 3
    assert transport.calls[0]["timeout"] == 120.0


def test_network_error_is_named_network_not_timeout():
    failure = lc.LLMTransportError("network", "dns")
    client, _ = _client([failure], default=failure)
    result = client.judge("s", "u")
    assert result.error == "network" and result.attempts == 3


def test_malformed_body_is_a_named_retryable_error():
    client, _ = _client([(200, "<html>502</html>"), (200, "<html>502</html>"),
                         (200, "<html>502</html>")])
    assert client.judge("s", "u").error == "bad_response"


def test_error_attempts_are_reported_for_cost_accounting():
    client, _ = _client([(500, "{}")])
    result = client.judge("s", "u")
    assert result.attempts == 3 and result.elapsed >= 0.0


# ---------------- 空 content：可恢复 ----------------

def test_empty_content_is_retried_with_a_doubled_budget():
    """推理模型把预算全花在 reasoning 上 —— 翻倍预算重试能救回来，这是实测结论。"""
    client, transport = _client([(200, _body("")), (200, _body('{"ok":1}'))])
    result = client.judge("s", "u", max_tokens=1000)
    assert result.ok and result.attempts == 2
    assert transport.calls[0]["payload"]["max_tokens"] == 1000
    assert transport.calls[1]["payload"]["max_tokens"] == 2000, "重试没有加预算"


def test_empty_content_budget_growth_is_capped_but_respects_the_requested_floor():
    """翻倍增长必须有上限（否则重试会把预算吹到天上），但调用方明确要的额度不能被压低。"""
    empty = (200, _body(""))
    client, transport = _client([], default=empty)
    assert client.judge("s", "u", max_tokens=1000).error == "empty_content"
    assert [call["payload"]["max_tokens"] for call in transport.calls] == [1000, 2000, 4000]

    client, transport = _client([], default=empty)
    client.judge("s", "u", max_tokens=100_000)
    assert max(call["payload"]["max_tokens"] for call in transport.calls) == 100_000, \
        "调用方要了 100K，被标定上限压回去了"


def test_empty_content_is_named_not_crashed():
    client, _ = _client([], default=(200, _body("   ")))
    result = client.judge("s", "u")
    assert not result.ok and result.error == "empty_content"


def test_null_content_is_empty_content():
    body = (200, json.dumps({"choices": [{"message": {"content": None}}]}))
    client, _ = _client([], default=body)
    assert client.judge("s", "u").error == "empty_content"


# ---------------- 辅助客户端 ----------------

def test_fake_client_scripts_responses_and_counts_calls():
    fake = lc.FakeLLMClient(responses=["a", lc.LLMResult(False, error="timeout")])
    assert fake.judge("s", "u").text == "a"
    assert fake.judge("s", "u").error == "timeout"
    assert fake.call_count == 2 and fake.calls[0]["system"] == "s"


def test_replay_client_misses_are_named_not_silent():
    replay = lc.ReplayLLMClient({"user-1": "text"})
    assert replay.judge("s", "user-1").text == "text"
    missed = replay.judge("s", "user-2")
    assert not missed.ok and missed.error == "empty_content"


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
    print(f"\n{len(tests) - len(failed)}/{len(tests)} 通过" + (f"，失败：{failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
