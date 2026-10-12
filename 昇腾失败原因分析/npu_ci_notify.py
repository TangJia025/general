#!/usr/bin/env python3
"""把昇腾失败分析报告投递到中间邮箱，交给内网机的 xlb_notify.py 转发到小鲁班。

链路（三段两跳）：

    本脚本 ──smtp.qq.com:587──> 中间邮箱 ──IMAP──> xlb_notify.py(内网Windows) ──> 小鲁班 WeLink

为什么走 QQ 邮箱而不是直连华为邮件：本机不在公司内网（实测 imailcn56.email.huawei.com /
mail.huawei.com 均不可解析），企业 MX 只有 25 端口可达；而 smtp.qq.com:587 实测可达。
这套「QQ 代发中继」已在 backlog 仓的 workflow-analysis.yml 上跑通，不是新造的路子。

邮件长相是硬契约 —— 消费端是 xlb_notify.py 里写死的解析器，以下三条都是实测结论：
  * 主题必须 `[级别] [集群] 告警名` **双方括号**，且级别必须是 ASCII 关键词
    （FIRING/RESOLVED/CRITICAL/WARNING/INFO/RECOVERED）。写 `[FAILURE]` 会被
    is_alert_subject() 判为 non_alert，整封丢弃。
  * 正文必须发 HTML 表格、或「标签与值分行」的纯文本：parse_alert 是**逐行精确匹配**
    标签（line in label_map，不是 startswith）。`集群: xxx` 写成同一行时标签和值一起
    匹配不上，实测 description/action 全丢。发 HTML 时 _html_to_text 会把 <th>/<td>
    换成换行，天然满足分行要求，所以这里选 HTML（顺带保住人读的排版）。
  * 标签值里不能出现 http，否则整行被丢（parse_alert 显式过滤）—— 链接只能放末尾，
    由 re.search(r'https?://') 抓成 runbook。

凭据一律从环境变量读：本仓（TangJia025/general）是 **PUBLIC**，硬编码会重犯
xlb_notify.py 已经犯过的那次凭据泄露。

    SMTP_USER  必填  QQ 邮箱账号（同时是收件端 XLBN_ALERT_SENDER 要匹配的 From）
    SMTP_PASS  必填  QQ 邮箱授权码
    ALERT_TO   必填  中间邮箱地址
    SMTP_HOST  选填  默认 smtp.qq.com
    SMTP_PORT  选填  默认 587（STARTTLS）

用法：
    npu_ci_notify.py <report.json> [job_id,job_id]   # 由 npu_ci_watch.py --notify-command 调用
    npu_ci_notify.py --dry-run <report.json>         # 只渲染不发信，且不要求凭据

## owner=infra 的失败 job 会顺带建 issue

默认对 `--issue-repo`（默认 TangJia025/general）里 owner=infra 的 case **一个 job 建一个
issue**，链接写进邮件正文。脱敏与查重都在 forensics/gh_issue.py，不在本文件。
`--no-issues` 关掉；`--dry-run` 永远不建，只列出将要建的标题。

⚠️ 已知限制：**issue 链接不会出现在 WeLink 消息里**。消费端 xlb_notify.parse_alert
把「含 http 的行」整行丢掉，且只有正文第一个 URL 能成为 `Runbook:` —— 按需求那个位置
留给 CI 链接。所以 issue 链接只在邮件正文里可点，WeLink 那条消息看不到它。
"""

import argparse
import html
import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText

from forensics import gh_issue

SMTP_HOST_DEFAULT = "smtp.qq.com"
SMTP_PORT_DEFAULT = 587

# 消费端 xlb_notify.py 的 is_alert_subject() 只认这几个 ASCII 关键词，不能改用 FAILURE
ALERT_SUBJECT_KEYWORD = "FIRING"
ALERT_NAME = "昇腾CI失败"

REQUIRED_ENV = ("SMTP_USER", "SMTP_PASS", "ALERT_TO")

# 一封邮件里最多列几个失败 job，其余折叠成计数，避免正文过长
MAX_CASES_IN_BODY = 5


def _plain(text, limit=300):
    """压平空白，用于链接等不适合被剔链接的字段。"""
    collapsed = re.sub(r"\s+", " ", str(text or "")).strip()
    return collapsed[:limit]


def _clean_value(text, limit=240):
    """压平空白并剔除链接 —— 标签值里含 http 会被 parse_alert 整行丢掉。"""
    without_links = re.sub(r"https?://\S+", "", str(text or ""))
    collapsed = re.sub(r"\s+", " ", without_links).strip()
    return collapsed[:limit]


def load_config():
    """读投递配置。返回 (配置字典, 缺失的必填变量名列表)。"""
    config = {
        "smtp_host": os.environ.get("SMTP_HOST", "").strip() or SMTP_HOST_DEFAULT,
        "smtp_port": int(os.environ.get("SMTP_PORT", "").strip() or SMTP_PORT_DEFAULT),
        "smtp_user": os.environ.get("SMTP_USER", "").strip(),
        "smtp_pass": os.environ.get("SMTP_PASS", "").strip(),
        "alert_to": os.environ.get("ALERT_TO", "").strip(),
    }
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
    return config, missing


def load_report(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def select_cases(report, job_ids_text):
    """按 watcher 传来的 job_ids 过滤。返回 (用例列表, 是否退回了全部)。

    过滤后为空时退回全部而不是静默不发：「漏报」比「多报一条」代价大得多。
    """
    cases = [case for case in (report.get("cases") or []) if isinstance(case, dict)]
    wanted = {int(part) for part in job_ids_text.split(",") if part.strip().isdigit()}
    if not wanted:
        return cases, False
    selected = [case for case in cases if case.get("job_id") in wanted]
    if selected:
        return selected, False
    return cases, True


def _cluster_of(case):
    cluster = case.get("cluster") or {}
    return _clean_value(cluster.get("cluster_name"), 60) or "unknown"


def _describe_case(case):
    """把一个用例压成一行描述：桶 / 归属 / 芯片 / 步骤 + job 名。"""
    parts = []
    for label, raw_value, limit in (
        ("桶", case.get("bucket"), 60),
        ("归属", case.get("owner"), 20),
        ("芯片", case.get("chip"), 20),
        ("步骤", case.get("step"), 40),
    ):
        value = _clean_value(raw_value, limit)
        if value:
            parts.append("%s=%s" % (label, value))
    job_name = _clean_value(case.get("job_name"), 80)
    if job_name:
        parts.append(job_name)
    return " | ".join(parts)


def build_subject(cases):
    return "[%s] [%s] %s" % (ALERT_SUBJECT_KEYWORD, _cluster_of(cases[0]), ALERT_NAME)


def build_message_id(cases):
    """写死 Message-ID，让重投幂等 —— 消费端按 Message-ID 去重，随机 ID 会导致重复通知。"""
    run_id = _plain(cases[0].get("run_id"), 30) or "0"
    job_part = "-".join(str(case.get("job_id")) for case in cases[:3]) or "0"
    return "<npu-ci-%s-%s@npu-ci>" % (run_id, job_part)


def build_body(report, cases, issue_urls=None):
    """渲染 HTML 表格。标签与值落在相邻的 <th>/<td>，被 _html_to_text 展成分行文本。

    issue_urls 按 job 顺序排在 CI 链接**之后** —— 消费端取正文第一个 URL 当 Runbook，
    第一个必须还是 CI 链接（需求如此）。issue 链接只给读邮件的人看。
    """
    meta = report.get("meta") or {}
    repo = _clean_value(meta.get("repo"), 80) or "昇腾CI"
    generated_at = _clean_value(meta.get("generated_at"), 40) or "时间未知"

    rows = [
        ("集群", _cluster_of(cases[0])),
        ("告警", ALERT_NAME),
        ("状态", "firing"),
        ("描述", "%s 共 %d 个 job 失败（%s）" % (repo, len(cases), generated_at)),
    ]
    for case in cases[:MAX_CASES_IN_BODY]:
        rows.append(("描述", _describe_case(case)))
    if len(cases) > MAX_CASES_IN_BODY:
        rows.append(("描述", "另有 %d 个 job 未列出" % (len(cases) - MAX_CASES_IN_BODY)))

    for case in cases:
        root_cause = _clean_value((case.get("verdict") or {}).get("root_cause"), 300)
        if root_cause:
            rows.append(("修复建议", root_cause))
            break

    table_rows = "\n".join(
        "  <tr><th>%s</th><td>%s</td></tr>" % (html.escape(label), html.escape(value))
        for label, value in rows
    )

    # 链接单独放末尾：它是 parse_alert 用 re.search 抓出来的 runbook，正文里不能再有别处的 http
    link_block = ""
    for case in cases:
        run_url = _plain(case.get("link"), 300)
        if run_url.startswith("http"):
            link_block = '  <p><a href="%s">%s</a></p>' % (
                html.escape(run_url, quote=True), html.escape(run_url))
            break

    # issue 链接排在 CI 链接之后（见 docstring：第一个 URL 必须仍是 CI 链接）
    issue_block = ""
    for url in issue_urls or []:
        url = _plain(url, 300)
        if url.startswith("http"):
            issue_block += '\n  <p><a href="%s">%s</a></p>' % (
                html.escape(url, quote=True), html.escape(url))

    return (
        '<!DOCTYPE html>\n<html><head><meta charset="UTF-8"></head>\n<body>\n<table>\n'
        + table_rows
        + "\n</table>\n"
        + link_block
        + issue_block
        + "\n  <p>本邮件由昇腾 CI 失败分析工具自动发送，经小鲁班转发到 WeLink。</p>\n"
        + "</body></html>"
    )


def flatten_for_preview(html_body):
    """预览用：近似消费端 xlb_notify._html_to_text 的效果，仅供肉眼核对分行是否正确。

    真值以 xlb_notify.py 为准，本函数不参与发送，只是为了 --dry-run 能看出
    「标签与值是否真的分行了」这个唯一会静默丢字段的点。
    """
    text = re.sub(r"<style[^>]*>.*?</style>", "", html_body, flags=re.I | re.S)
    text = re.sub(r'<a[^>]+href="([^"]+)"[^>]*>([^<]*)</a>', r" \2 (\1) ", text, flags=re.I)
    text = re.sub(r"</?t[hd]\s*>", "\n", text, flags=re.I)
    text = re.sub(r"</?tr\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")):
        text = text.replace(entity, char)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n[ \t]*\n+", "\n", text).strip()


def send_mail(config, subject, body, message_id):
    message = MIMEText(body, "html", "utf-8")
    message["Subject"] = subject
    message["From"] = config["smtp_user"]
    message["To"] = config["alert_to"]
    message["Message-ID"] = message_id
    with smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=30) as server:
        server.starttls()
        server.login(config["smtp_user"], config["smtp_pass"])
        server.send_message(message)


def main():
    parser = argparse.ArgumentParser(description="把昇腾失败分析报告投递到中间邮箱")
    parser.add_argument("report_path", nargs="?", default="", help="分析报告 JSON 路径")
    parser.add_argument("job_ids", nargs="?", default="", help="逗号分隔的 job id，由 watcher 传入")
    parser.add_argument("--dry-run", action="store_true", help="只渲染不发信，且不要求凭据")
    parser.add_argument("--no-issues", action="store_true",
                        help="不建 issue（默认会给 owner=infra 的 job 各建一个）")
    parser.add_argument("--issue-repo", default="TangJia025/general",
                        help="建 issue 的目标仓，默认 TangJia025/general（PUBLIC，正文强制脱敏）")
    parser.add_argument("--issue-state", default="",
                        help="job_id → issue URL 的映射文件，默认 .forensics_state/issues.json")
    arguments = parser.parse_args()

    if not arguments.report_path:
        sys.exit("缺少报告路径参数：npu_ci_notify.py <report.json> [job_id,job_id]")

    config, missing = load_config()
    if missing and not arguments.dry_run:
        sys.exit("缺少必需的环境变量：%s\n"
                 "（本仓是 PUBLIC，凭据不入库；缺凭据时脚本拒绝开跑，不静默降级）"
                 % ", ".join(missing))

    try:
        report = load_report(arguments.report_path)
    except (OSError, ValueError) as error:
        sys.exit("读取报告失败 %s：%s" % (arguments.report_path, error))

    cases, fell_back = select_cases(report, arguments.job_ids)
    if not cases:
        print("报告内无失败用例，无需通知：%s" % arguments.report_path, file=sys.stderr)
        return
    if fell_back:
        print("警告：按 job_ids 过滤后为空，已退回通知报告内全部用例", file=sys.stderr)

    subject = build_subject(cases)
    message_id = build_message_id(cases)
    infra_cases = [case for case in cases
                   if str(case.get("owner") or "").strip().lower() == "infra"]

    if arguments.dry_run:
        # dry-run 不建 issue，只把「将要建哪些」列出来，方便肉眼核对标题与脱敏
        print("收件人    : %s" % (config["alert_to"] or "(未设置 ALERT_TO)"))
        print("主题      : %s" % subject)
        print("Message-ID: %s" % message_id)
        print("将建 issue: %d 个（owner=infra，dry-run 不实际创建）" % len(infra_cases))
        for case in infra_cases:
            print("            %s" % gh_issue.build_issue_title(
                case, gh_issue.harvest_sensitive(case)))
        print("-" * 60)
        print(flatten_for_preview(build_body(report, cases)))
        return

    # 建 issue 在发信之前 —— 链接要进正文。失败只警告，绝不因此不发邮件。
    issue_urls = []
    if infra_cases and not arguments.no_issues:
        state = gh_issue.load_state(arguments.issue_state or None)
        mapping, notes = gh_issue.ensure_infra_issues(
            cases, arguments.issue_repo, state)
        for note in notes:
            print(note, file=sys.stderr)
        try:
            gh_issue.save_state(state, arguments.issue_state or None)
        except OSError as error:
            print("警告：issue 映射落盘失败（下轮可能重复建）：%s" % error, file=sys.stderr)
        # 按 cases 顺序取，保证正文里链接与用例一一对应
        issue_urls = [mapping[str(case.get("job_id"))] for case in cases
                      if str(case.get("job_id")) in mapping]
    elif infra_cases:
        print("按 --no-issues 跳过建 issue（%d 个 infra case）" % len(infra_cases), file=sys.stderr)

    body = build_body(report, cases, issue_urls)

    send_mail(config, subject, body, message_id)
    print("已投递到 %s：%s" % (config["alert_to"], subject))


if __name__ == "__main__":
    main()
