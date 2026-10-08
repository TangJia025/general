#!/usr/bin/env python3
"""LLM 客户端：一次 POST + 一个 JSON，用标准库而不是 SDK。

为什么不用 `openai` SDK：这里只需要一次 chat/completions 调用，SDK 买到的只有重试
（5 行自己写就有）。而 SDK 是可被 `pip` 改动掉的第三方包，挂在常驻服务的关键路径上
不划算 —— 本仓库此前的取证链路（`gh`、`kubectl`、`urllib`）也都是标准库或外部二进制。

**实测注意**：DeepSeek 的模型是推理模型，`max_tokens` 给小了会让 token 全花在
`reasoning_content` 上、`content` 返回空字符串。所以
  1) 默认预算给到 4000；
  2) 空 content 是**可恢复**错误（重试一次并把预算翻倍），不是解析器的锅；
  3) 空 content 与 4xx/5xx 一样必须具名上报，不许当异常崩掉整轮报告。

网络异常一律由 transport 抛 `LLMTransportError`，本模块负责分类与重试 ——
这样测试可以注入假 transport，完全不碰网络。
"""
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-flash"
# 推理模型：预算给小了 content 会是空串（实测 max_tokens=60 时 60 个 token 全是 reasoning）
DEFAULT_MAX_TOKENS = 4000
EMPTY_CONTENT_RETRY_CAP = 16000

RETRYABLE = ("http_429", "http_5xx", "bad_response", "timeout", "network")
ERROR_KINDS = RETRYABLE + ("http_4xx", "empty_content")


class LLMTransportError(Exception):
    """transport 层的网络错误；kind ∈ {timeout, network}。"""

    def __init__(self, kind, detail=""):
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}")


@dataclass(frozen=True)
class LLMResult:
    ok: bool
    text: str = None
    error: str = None
    attempts: int = 0
    elapsed: float = 0.0
    usage: dict = field(default_factory=dict)


def urllib_transport(url, headers, payload, timeout):
    """默认 transport：(status, body_text)；网络类异常抛 LLMTransportError。"""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="ignore")
    except (socket.timeout, TimeoutError):
        raise LLMTransportError("timeout", f"{timeout}s 内没有响应")
    except urllib.error.URLError as exc:
        raise LLMTransportError("network", str(exc.reason))


def _classify(status):
    if status == 429:
        return "http_429"
    if status >= 500:
        return "http_5xx"
    return "http_4xx"


class DeepSeekClient:
    """OpenAI 兼容的 chat/completions 客户端（DeepSeek 用这个端点）。"""

    def __init__(self, *, api_key, base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL,
                 max_retries=2, backoff=2.0, temperature=0.0, json_mode=True,
                 transport=None, sleep=time.sleep):
        if not api_key:
            raise ValueError("缺少 api_key（凭据由调用方注入，本模块不读全局配置）")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_retries = max_retries
        self.backoff = backoff
        self.temperature = temperature
        self.json_mode = json_mode
        self.transport = transport or urllib_transport
        self.sleep = sleep

    def judge(self, system, user, *, max_tokens=DEFAULT_MAX_TOKENS, timeout=120.0):
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.temperature,
            "max_tokens": max_tokens,
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        url = f"{self.base_url}/chat/completions"

        started = time.monotonic()
        budget = max_tokens
        # 空 content 重试时把预算翻倍；上限取「实测标定值」与「调用方自己要的额度」的较大者 ——
        # 调用方明确要了 100K 就不该被标定值压回 16K。
        ceiling = max(EMPTY_CONTENT_RETRY_CAP, max_tokens)
        last_error = "network"
        attempts = 0
        for attempt in range(1, self.max_retries + 2):
            attempts = attempt
            try:
                status, body = self.transport(url, headers, dict(payload, max_tokens=budget),
                                              timeout)
            except LLMTransportError as exc:
                last_error = exc.kind
                status, body = None, exc.detail
            else:
                if status == 200:
                    text, usage, error = self._read_body(body)
                    if error is None:
                        return LLMResult(True, text=text, attempts=attempts,
                                         elapsed=time.monotonic() - started, usage=usage)
                    last_error = error
                    if error == "empty_content":
                        # 可恢复：预算可能全被 reasoning 吃掉了
                        budget = min(budget * 2, ceiling)
                else:
                    last_error = _classify(status)
                    if last_error == "http_4xx":
                        return LLMResult(False, error=last_error, attempts=attempts,
                                         elapsed=time.monotonic() - started, usage={})
            if attempt <= self.max_retries:
                self.sleep(self.backoff * attempt)
        return LLMResult(False, error=last_error, attempts=attempts,
                         elapsed=time.monotonic() - started, usage={})

    @staticmethod
    def _read_body(body):
        """→ (content, usage, error)。body 畸形算 bad_response（可重试）。"""
        try:
            data = json.loads(body)
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = message.get("content")
        except Exception as exc:
            return None, {}, f"bad_response"
        usage = data.get("usage") or {}
        if not isinstance(content, str) or not content.strip():
            return None, usage, "empty_content"
        return content, usage, None


class FakeLLMClient:
    """测试用：按剧本出响应，并记录调用次数（守「不开就不调」「缓存命中不重调」）。"""

    def __init__(self, responses=None, text=None, error=None):
        self.responses = list(responses or [])
        self.text = text
        self.error = error
        self.calls = []

    @property
    def call_count(self):
        return len(self.calls)

    def judge(self, system, user, *, max_tokens=DEFAULT_MAX_TOKENS, timeout=120.0):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens,
                           "timeout": timeout})
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, LLMResult):
                return item
            return LLMResult(True, text=item, attempts=1, elapsed=0.01,
                             usage={"prompt_tokens": 1, "completion_tokens": 1})
        if self.error:
            return LLMResult(False, error=self.error, attempts=1, elapsed=0.01)
        return LLMResult(True, text=self.text, attempts=1, elapsed=0.01,
                         usage={"prompt_tokens": 1, "completion_tokens": 1})


class ReplayLLMClient:
    """评测用：从落盘的原始响应里读，重跑不花钱、且输入逐字节可复现。"""

    def __init__(self, raw_by_key):
        self.raw_by_key = dict(raw_by_key or {})
        self.calls = []

    def judge(self, system, user, *, max_tokens=DEFAULT_MAX_TOKENS, timeout=120.0):
        self.calls.append({"system": system, "user": user})
        text = self.raw_by_key.get(user)
        if text is None:
            return LLMResult(False, error="empty_content", attempts=1,
                             elapsed=0.0, usage={})
        return LLMResult(True, text=text, attempts=1, elapsed=0.0)
