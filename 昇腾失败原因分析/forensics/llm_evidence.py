#!/usr/bin/env python3
"""LLM 判决的证据打包：把归因扫描窗切成**带行号**的证据块。

为什么单独一层：这里藏着两个最隐蔽的缺陷 ——

1. **截断把「终局行」切掉。** 日志的结论在尾部（pytest 的 `short test summary info`、
   benchmark 的 `Performance verification failed`）。一旦按预算从头截，判据就没了，
   模型只能去抓中间的噪声行 —— 实测 48% 的归因准确率正是这么来的：证据明明在窗口里
   （12 条错判中 11 条的真值行就在被扫描的尾部 1200 行内），只是被排到了良性 WARNING 后面。

2. **行号错位。** 模型被要求引用行号（这是抑制幻觉的核心闸门），行号若与它实际看到的
   文本对不上，「引用窗口外行号」的校验就会误判，闸门形同虚设。

两者都必须是可注入、可证伪的纯函数，所以放在 forensics/ 下（本仓的两个流水线脚本是
全模块级执行、import 即联网跑整轮，测试无法 import；纯逻辑只有放这里才能被测试直接跑）。
"""
import hashlib
import re
from dataclasses import dataclass

# 实测标定：1200 行窗口 = 36K~64K prompt tokens（3 份真实日志实测，chars/token ≈ 2.6）。
# 只用于预算判断，不追求精确 —— 预算本身是软的：终局行的保留优先级高于预算。
CHARS_PER_TOKEN = 2.6
DEFAULT_BUDGET_TOKENS = 60_000

# 「终局行」= 由测试框架 / 基准 harness 自己给出的判定，是本系统的定性依据（见 prompt 纪律）。
# 用 re.search 而非 match：GHA 原始日志每行带 `2026-…Z ` 时间戳前缀，锚定行首会全部漏掉。
TERMINAL_PATTERNS = [
    r'=+\s*short test summary info\s*=+',
    r'\bno tests ran\b',
    r'=+[^\n]*\b\d+ (?:failed|passed|error|skipped)[^\n]*=+',
    r'\bPerformance verification failed\b',
    r'\bBenchmark failed\b',
    r'\bpytest exit code: ret=\d+',
]
_TERMINAL_RES = [re.compile(pattern) for pattern in TERMINAL_PATTERNS]


def estimate_tokens(text):
    """按实测标定估算 token 数（chars/token ≈ 2.6）。"""
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def find_terminal_lines(text):
    """返回终局行的 1-based 行号列表。

    终局行不只在尾部：benchmark harness 的判定常出现在尾部之前的汇总段，
    所以这里扫全文而不是只看末尾若干行。
    """
    return [number for number, line in enumerate(text.splitlines(), 1)
            if any(regex.search(line) for regex in _TERMINAL_RES)]


@dataclass(frozen=True)
class Evidence:
    """交给 LLM 的证据块。lines 是 (1-based 行号, 原文) 且严格递增。"""
    lines: tuple
    total_lines: int
    terminal_lines: tuple
    sha256: str

    def line_numbers(self):
        return frozenset(number for number, _ in self.lines)

    def truncated(self):
        """被省略的区间 [(起, 止), ...]，由 lines + total_lines 现算，不会与 lines 不一致。"""
        spans, previous = [], 0
        for number, _ in self.lines:
            if number > previous + 1:
                spans.append((previous + 1, number - 1))
            previous = number
        if previous < self.total_lines:
            spans.append((previous + 1, self.total_lines))
        return tuple(spans)


def _sha_of(lines, total_lines):
    """对「模型实际看到的内容」取摘要：确定性、无时间戳、无集合迭代序泄漏。"""
    digest = hashlib.sha256()
    digest.update(f"total={total_lines}\n".encode("utf-8"))
    for number, text in lines:
        digest.update(f"{number}\x1f{text}\n".encode("utf-8"))
    return digest.hexdigest()


def _split_keep(raw, budget_chars):
    """按预算从两端累积，返回 (头部保留行数, 尾部保留行数)；中间那段被省略。

    从**两端**而不是从头：结论在尾部，从头截必然先丢判据。且实测「去掉噪声行」会让
    窗口反而变大 11%（1200 行上限会往回吃更多内容）—— 上限才是约束，噪声不是。
    """
    half = budget_chars // 2
    head, used = 0, 0
    while head < len(raw) and used + len(raw[head]) + 1 <= half:
        used += len(raw[head]) + 1
        head += 1
    tail, used = 0, 0
    while tail < len(raw) - head and used + len(raw[-1 - tail]) + 1 <= half:
        used += len(raw[-1 - tail]) + 1
        tail += 1
    return head, tail


def build_evidence(scan_text, *, budget_tokens=DEFAULT_BUDGET_TOKENS):
    """把扫描窗文本打成带行号的证据块。

    不变式：**任何预算下终局行都不会被丢弃**。超预算时从中间挖（留 `[省略 Lx-Ly]` 标记），
    且被省略区域里的终局行会被强制找回 —— 预算为终局行让路，因为丢了它这次判决就没了依据。
    """
    raw = scan_text.splitlines()
    total_lines = len(raw)
    terminal_lines = tuple(find_terminal_lines(scan_text))
    if estimate_tokens(scan_text) <= budget_tokens:
        lines = tuple((number, line) for number, line in enumerate(raw, 1))
        return Evidence(lines, total_lines, terminal_lines, _sha_of(lines, total_lines))

    head_count, tail_count = _split_keep(raw, int(budget_tokens * CHARS_PER_TOKEN))
    keep = set(range(1, head_count + 1)) | set(range(total_lines - tail_count + 1, total_lines + 1))
    keep |= {number for number in terminal_lines if 1 <= number <= total_lines}
    lines = tuple((number, raw[number - 1]) for number in sorted(keep))
    return Evidence(lines, total_lines, terminal_lines, _sha_of(lines, total_lines))


def render_evidence_block(evidence):
    """渲染成 `L<行号>|<原文>`，被省略处插入 `[省略 Lx-Ly]` 标记。

    行号必须与原文一起回显：只给行号读者无法核验，「强制引用证据行」就退化成了形式主义。
    """
    out, previous = [], 0
    for number, text in evidence.lines:
        if number > previous + 1:
            out.append(f"[省略 L{previous + 1}-L{number - 1}]")
        out.append(f"L{number}|{text}")
        previous = number
    if previous < evidence.total_lines:
        out.append(f"[省略 L{previous + 1}-L{evidence.total_lines}]")
    return "\n".join(out)
