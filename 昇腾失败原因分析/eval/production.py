#!/usr/bin/env python3
"""从流水线脚本里抽出评测要复用的生产逻辑（AST 抽取，不 import）。

**为什么非要这么绕**：`npu_ci_failure_analysis.py` 是**全模块级执行**的脚本 —— `import` 它
就会跑完整个流水线（联网拉 job、写报告）。所以只能按 AST 抽节点、在干净命名空间里 exec，
这是仓库里既有的手法（`tests/test_pytest_verdict.py:33-57`）。

**为什么值得绕**：评测必须用**生产代码本身**的窗口算法（`build_scan_window`）与规则表
（`BUCKETS`/`classify_text`）。在评测里另写一遍窗口逻辑或另抄一份桶表，测的就不是线上的行为，
算出来的准确率与「规则基线 48%」也就不可比 —— 整个评测的意义就没了。

少了任何一个节点都**报错而不是静默跳过**：源文件结构变了就是结构变了，
静默跳过会让评测悄悄退化成「用我临时写的简化版窗口」。
"""
import ast
import datetime
import pathlib
import re

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
SOURCE = BASE_DIR / "npu_ci_failure_analysis.py"

WANTED_ASSIGNMENTS = {"BUCKETS", "BUCKET_OWNER", "DECISIVE_BUCKETS", "STEP_ROUTES",
                      "NOISE_PATTERNS", "TS_RE", "PYTEST_VERDICT_RE"}
WANTED_FUNCTIONS = {"classify_text", "route_for", "slice_by_step_window",
                    "build_scan_window", "is_decisive"}

# 生产默认值：与 npu_ci_failure_analysis.py 的 CLI 默认保持一致（改那边要一起改）
DEFAULT_TAIL_LINES = 1200
DEFAULT_NO_STEP_WINDOW = False


def load_production(source=SOURCE):
    """返回含所需符号的命名空间；缺任何一个都抛 AssertionError。"""
    source = pathlib.Path(source)
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    namespace = {"re": re}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if targets & WANTED_ASSIGNMENTS:
                exec(compile(ast.Module([node], []), str(source), "exec"), namespace)
        elif isinstance(node, ast.FunctionDef) and node.name in WANTED_FUNCTIONS:
            exec(compile(ast.Module([node], []), str(source), "exec"), namespace)
    # TS_RE 用 re.compile 构造、datetime 是模块级 import：抽出来的函数体里要用到，
    # 必须在这里补齐，否则 build_scan_window 一跑就 NameError。
    namespace.setdefault("datetime", datetime)
    if "TS_RE" not in namespace:
        namespace["TS_RE"] = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})')
    missing = (WANTED_ASSIGNMENTS - set(namespace)) | (WANTED_FUNCTIONS - set(namespace))
    if missing:
        raise AssertionError(
            f"未能从 {source} 抽取到：{sorted(missing)}（源文件结构可能已变，评测必须停下）")
    return namespace


NS = None


def production():
    """进程内单例（AST 抽取不便宜，评测里会反复用）。"""
    global NS
    if NS is None:
        NS = load_production()
    return NS


def allowed_classes():
    """`verdict_class` 的闭集 = 规则层全部桶标签 + 两个兜底类。

    必须与 `llm_verdict.parse_llm_verdict` 校验用的闭集**是同一份** —— 两处各写一份，
    模型选了个解析器不认的类就会整批静默降级。
    """
    labels = [label for _pattern, label, _owner in production()["BUCKETS"]]
    for extra in ("其他", "未分类"):
        if extra not in labels:
            labels.append(extra)
    return tuple(labels)
