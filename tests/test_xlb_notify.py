#!/usr/bin/env python3
"""xlb_notify.py 的离线自测：不连 IMAP、不发 WeLink。

为什么值得写：这个脚本**在另一台机器上无人值守地跑**（内网 Windows，跑在中间邮箱上），
出问题时没人盯着。它的失效模式几乎都是「看起来一切正常」：
  * 收件人没切开 → 群消息静默丢掉，日志还写 send ok；
  * 去重快照把 DRY_RUN 的「没发」记成「已发」→ 联调完真跑那轮全判重、一封不发；
  * select 失败只返回 NO 不抛异常 → 一路走到「fetched 0 emails」，天天报平安；
  * 文件夹名写错编码 → 报 UnicodeEncodeError，看不出是文件夹名的问题。
这几条都在下面正面断言了。

为什么用**子进程**跑每个用例，而不是直接 import：本模块的配置是在 import 时读的
（CONFIG / MISSING_CONFIG / DEFAULT_RECEIVERS 全是模块级常量），import 一次就定了。
子进程是唯一能干净地换一套环境变量的办法，顺带保证跑测试不会在仓库里留下 alert.log
或快照文件 —— 每个用例都在自己的临时目录里跑一份脚本副本。

运行：python3 -B tests/test_xlb_notify.py      （无需 pytest，也兼容 pytest）
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

TEST_DIR = pathlib.Path(__file__).resolve().parent
BASE_DIR = TEST_DIR.parent
SCRIPT_SOURCE = BASE_DIR / "xlb_notify.py"

# 每个用例共用的基础环境：凭据全是假的，但必须齐 —— 缺了会被入口拦下
BASE_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "HOME": os.environ.get("HOME", ""),
    "XLBN_WELINK_AUTH": "fake-token",
    "XLBN_IMAP_USER": "fake@example.com",
    "XLBN_IMAP_PASSWORD": "fake-pass",
    "XLBN_DEFAULT_RECEIVER": "w0012345, w0067890;w0099999",
}


def _workdir():
    """造一个只放着脚本副本的临时目录 —— 配置与运行态都落在那里，仓库保持干净。"""
    directory = tempfile.mkdtemp(prefix="xlb-notify-test-")
    shutil.copy(str(SCRIPT_SOURCE), os.path.join(directory, "xlb_notify.py"))
    return directory


WORK = _workdir()
CONFIG = os.path.join(WORK, "xlb_notify.config.json")
SNAPSHOT = os.path.join(WORK, "email_alert_snapshot.json")


def _run(code_or_none, env_extra=None, as_file=False):
    """cwd 落在 WORK，所以 import xlb_notify 命中的是那份副本。"""
    env = dict(BASE_ENV)
    env.update(env_extra or {})
    if as_file:
        # 真·入口：像内网机上那样把脚本当文件跑（这样 __file__ 才存在）
        command = [sys.executable, "-B", "xlb_notify.py"]
    else:
        command = [sys.executable, "-B", "-c", code_or_none]
    return subprocess.run(command, env=env, cwd=WORK,
                          capture_output=True, text=True, timeout=120)


def _check(label, proc, expect_rc=None, must_contain=(), must_not_contain=()):
    """断言一次子进程运行的结果。失败信息里带上完整输出，省得为了看现场再跑一遍。"""
    blob = (proc.stdout or "") + (proc.stderr or "")
    problems = []
    if expect_rc is not None and proc.returncode != expect_rc:
        problems.append("退出码 %d，期望 %d" % (proc.returncode, expect_rc))
    for needle in must_contain:
        if needle not in blob:
            problems.append("输出里找不到 %r" % needle)
    for needle in must_not_contain:
        if needle in blob:
            problems.append("输出里不该出现 %r" % needle)
    assert not problems, "%s —— %s\n----- 实际输出 -----\n%s" % (
        label, "；".join(problems), blob.strip()[:1200] or "(空)")


def _write_config(data):
    with open(CONFIG, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)


def _drop(path):
    if os.path.exists(path):
        os.remove(path)


# ---------------------------------------------------------------- 收件人切分


def test_receivers_split_on_comma_and_semicolon():
    """合并的收件人串必须切开。小鲁班的 receiver 只收单个 ID，拼成一个串会被当成
    一个畸形 ID —— 实测只投给第一个，其余静默丢掉，接口照样返回 200。"""
    _drop(CONFIG)
    _check("收件人切分",
           _run("import xlb_notify as m; print(m.DEFAULT_RECEIVERS)"),
           must_contain=("['w0012345', 'w0067890', 'w0099999']",))


# ---------------------------------------------------------------- 缺凭据硬失败


def test_missing_credentials_reported_by_name():
    """缺哪个就报哪个 —— 只报一句「配置不对」会让人去翻环境变量，而真因可能在配置文件。"""
    _drop(CONFIG)
    _check("缺凭据逐项报出",
           _run("import xlb_notify as m; print('缺=', m.MISSING_CONFIG)",
                {"XLBN_WELINK_AUTH": "", "XLBN_IMAP_USER": ""}),
           must_contain=("缺= ['XLBN_WELINK_AUTH', 'XLBN_IMAP_USER']",))


def test_entry_point_refuses_to_run_without_credentials():
    """入口缺凭据就退出且退出码非 0：静默降级会把「没读到凭据」表现成「发送失败」。"""
    _drop(CONFIG)
    _check("入口缺凭据不跑",
           _run(None, {"XLBN_WELINK_AUTH": ""}, as_file=True),
           expect_rc=1,
           must_contain=("XLBN_WELINK_AUTH", "xlb_notify.config.json"))


# ---------------------------------------------------------------- 配置文件


def test_config_file_backs_up_env_and_ignores_note_keys():
    """配置文件能兜住缺的环境变量；_xxx_note 这类说明键必须被容忍（模板里全是）。"""
    _write_config({"welink_auth": "from-file-token", "default_receiver": "g0000001",
                   "search_days": 10, "imap_folder": "INBOX/Test",
                   "_note": "带下划线的说明键"})
    _check("配置文件兜底",
           _run("import xlb_notify as m; print('auth=', m.WELINK_AUTH, '| recv=', "
                "m.DEFAULT_RECEIVERS, '| days=', m.SEARCH_DAYS, '| folder=', m.IMAP_FOLDER)",
                {"XLBN_WELINK_AUTH": "", "XLBN_DEFAULT_RECEIVER": ""}),
           must_contain=("auth= from-file-token", "recv= ['g0000001']", "days= 10",
                         "folder= INBOX/Test"))
    _check("环境变量优先",
           _run("import xlb_notify as m; print('recv=', m.DEFAULT_RECEIVERS)",
                {"XLBN_DEFAULT_RECEIVER": "env0001"}),
           must_contain=("recv= ['env0001']",))
    _drop(CONFIG)


def test_broken_config_file_names_itself():
    """坏 JSON 必须报文件名。否则表现成「凭据全都读不到 → 缺少环境变量」，
    人会去查环境变量，而真因是配置文件存成了 ANSI 或写成了 JSONC。"""
    with open(CONFIG, "w", encoding="utf-8") as handle:
        handle.write("{ 这 不是 json }")
    _check("坏 JSON 报文件名",
           _run("import xlb_notify"),
           expect_rc=1,
           must_contain=("不是合法 JSON", "xlb_notify.config.json"),
           must_not_contain=("缺少必需的配置",))
    _drop(CONFIG)


# ---------------------------------------------------------------- 启动闸门


def test_non_ascii_folder_is_rejected_with_the_fix_spelled_out():
    """文件夹名含非 ASCII 必须当场拦下。imaplib 在 _command 里按 ascii 编码参数，
    明文中文抛的是 UnicodeEncodeError —— 不拦的话报错长这样「'ascii' codec can't
    encode characters…」，看不出问题在文件夹名上。"""
    _drop(CONFIG)
    _check("非 ASCII 文件夹",
           _run("import xlb_notify", {"XLBN_IMAP_FOLDER": "INBOX/外部邮件"}),
           expect_rc=1, must_contain=("非 ASCII", "&WRaQ6JCuTvY-"))


def test_search_days_must_be_a_positive_integer():
    """时间窗是首次运行的爆炸半径控制，不能是 0/负数/非数字。"""
    _drop(CONFIG)
    for bad in ("0", "-1", "abc"):
        _check("search_days=%r 被拒" % bad,
               _run("import xlb_notify", {"XLBN_SEARCH_DAYS": bad}),
               expect_rc=1, must_contain=("XLBN_SEARCH_DAYS",))


# ---------------------------------------------------------------- 内容渲染


def test_compose_content_joins_subject_and_body():
    """纯文本 = 主题 + 空行 + 正文；主题压平内部空白；无正文只发主题；超长截断。"""
    _drop(CONFIG)
    _check("compose_content",
           _run(r"""
import xlb_notify as m
print(repr(m.compose_content('  主题  带  空格 ', 'body  text')))
print(repr(m.compose_content('', '')))
print(repr(m.compose_content('主题', '   \n  ')))
long = m.compose_content('t', 'x' * 5000)
print('len=', len(long), '有截断提示=', '正文过长已截断' in long)
"""),
           must_contain=("'主题 带 空格\\n\\nbody  text'", "'(无主题)'", "'主题'",
                         "len= 4011 有截断提示= True"))


def test_decode_subject_never_loses_the_whole_mail():
    """编码主题要能解；没主题给占位；字符集名字坏掉时保留原文 —— 主题读不出来
    不该拦住转发，正文才是内容。"""
    _drop(CONFIG)
    _check("decode_subject",
           _run(r"""
import email, xlb_notify as m
print(repr(m.decode_subject(email.message_from_string(
    "Subject: =?utf-8?B?5Lit5paH5Li76aKY?=\r\nFrom: a@b\r\n\r\nx"))))
print(repr(m.decode_subject(email.message_from_string("From: a@b\r\n\r\nx"))))
print(repr(m.decode_subject(email.message_from_string(
    "Subject: =?nosuchcharset?B?5Lit?=\r\nFrom: a@b\r\n\r\nx"))))
"""),
           must_contain=("'中文主题'", "'(无主题)'"))


def test_html_source_newlines_do_not_shred_prose():
    """HTML 源码里的换行是**软换行**，不能当成真换行。

    这是 LTS 告警邮件的真实形态：模板把一句话分写在多行（每个着色/加粗的片段
    单独一行），浏览器渲染时折成一个空格。早先的实现把源码换行原样保留，于是
    「尊敬的 华为云用户 ，您好！」被拆成三行、「您在 亚太-香港 区域设置的…」被拆成
    八行 —— 这是用户实测反馈的那次「换行还是有些问题」。

    同时钉住另一半：标签要**删掉**而不是换成空格。换成空格会把
    `<span>紧急</span>` 变成「告警级别: 紧急 ;」，而邮件里是「告警级别:紧急;」。
    """
    _drop(CONFIG)
    _check("源码软换行不拆句",
           _run(r"""
import email, xlb_notify as m
HTML = '''<html><head><style>td{font-size:12px}</style></head>
<body>
<table width="600"><tr><td>
尊敬的
<span style="color:#333">华为云用户</span>
，您好！<br>
您在
<span style="color:#333">亚太-香港</span>
区域设置的告警通知规则
<strong>openeuler-hk-defect-manager错误日志告警</strong>
触发
<span>1 条运维通知</span>
，事件类告警规则为
<strong>openeuler-hk-defect-manager错误日志告警</strong>
。更多信息请登录LTS。<br>
详细信息如下，请您查阅：<br>
<div>告警级别:<span>紧急</span>;</div>
<div>触发条件:$0.err_cnt &gt; 10;</div>
</td></tr></table>
</body></html>'''
body = m.get_email_body(email.message_from_bytes(
    'Content-Type: text/html; charset=utf-8\n\n'.encode('utf-8') + HTML.encode('utf-8')))
print('行数:', len(body.splitlines()))
print('句子没被拆:', '尊敬的 华为云用户 ，您好！' in body)
print('长句没被拆:', '您在 亚太-香港 区域设置的告警通知规则 openeuler-hk-defect-manager错误日志告警 '
      '触发 1 条运维通知 ，事件类告警规则为 openeuler-hk-defect-manager错误日志告警 '
      '。更多信息请登录LTS。' in body)
print('标签没换成空格:', '告警级别:紧急;' in body)
print('实体照常解:', '$0.err_cnt > 10;' in body)
print('真换行还在:', body.splitlines()[0] == '尊敬的 华为云用户 ，您好！'
      and body.splitlines()[1].startswith('您在'))
""", {"PYTHONIOENCODING": "utf-8"}),
           must_contain=("行数: 5", "句子没被拆: True", "长句没被拆: True",
                         "标签没换成空格: True", "实体照常解: True",
                         "真换行还在: True"))


def test_html_body_has_no_blank_line_swamp():
    """通知类邮件转出来必须没有成片的空行。

    实测一封普通通知邮件（嵌套 table + `&nbsp;` 占位行 + 缩进空白）原来出来 29 行、
    其中 22 行是纯空白。根因是原来那句 `\\n[ \\t]*\\n+ -> \\n` 压不掉**只含一个空格的行**
    —— 空格在换行之前，模式得先看到换行才匹配。这条用例就是钉这个：
    空白行必须为 0，且表格的标签与值必须在同一行（`</td>` 不能当断行 —— 单元格是并排
    的两列，不是两行；断行会把「集群 / 空行 / ascend-x」竖过来，一屏几十处）。
    """
    _drop(CONFIG)
    _check("HTML 无空行泛滥",
           _run(r"""
import email, xlb_notify as m
HTML = '''<html><head><style>td{font-size:12px}</style></head>
<body>
<!--[if gte mso 9]><xml><o:OfficeDocumentSettings><o:AllowPNG/></o:OfficeDocumentSettings></xml><![endif]-->
<table width="600" border="0">
  <tr><td>&nbsp;</td></tr>
  <tr><td><div><div><p>告警通知</p></div></div></td></tr>
  <tr><td>&nbsp;</td></tr>
  <tr><td><table>
        <tr><th>集群</th><td>ascend-x</td></tr>
        <tr><th>级别</th><td>CRITICAL</td></tr>
  </table></td></tr>
  <tr><td>&nbsp;</td></tr><tr><td>&nbsp;</td></tr><tr><td>&nbsp;</td></tr>
  <tr><td>见 <a href="http://a/b">链接</a></td></tr>
  <tr><td>&nbsp;</td></tr>
</table>
<!-- 内部备注：集群 A -> 集群 B 的通路，别发出去 -->
</body></html>'''
body = m.get_email_body(email.message_from_bytes(
    'Content-Type: text/html; charset=utf-8\n\n'.encode('utf-8') + HTML.encode('utf-8')))
lines = body.splitlines()
print('行数:', len(lines))
print('空白行数:', sum(1 for line in lines if not line.strip()))
print('标签值同行:', '集群 ascend-x' in body and '级别 CRITICAL' in body)
# 注释必须整段消失，而不是「吃到第一个 `>` 为止」——后者的漏法是：注释正文里
# 一旦出现 `>`（这条模拟的是 `集群 A -> 集群 B` 这种内部备注，也等价于 Outlook 的
# mso 条件注释），`<[^>]+>` 只吃到那个 `>`，**后面的注释正文就当成正文留下了**。
print('注释没漏出:', '内部备注' not in body and '集群 B' not in body)
print('没有残留标签:', '<' not in body and '>' not in body)
print('样式没漏出:', 'font-size' not in body)
"""),
           must_contain=("行数: 4", "空白行数: 0", "标签值同行: True",
                         "注释没漏出: True", "没有残留标签: True",
                         "样式没漏出: True"))


def test_plain_text_body_keeps_alignment_but_collapses_blank_runs():
    """纯文本侧不能像 HTML 那样压平：行首空格可能是监控系统打的表格对齐，
    空行又是它唯一的分段手段。所以只把连续空行压到一个、去掉行尾空白。"""
    _drop(CONFIG)
    _check("纯文本保留对齐",
           _run(r"""
import email, xlb_notify as m
PLAIN = '告警\n\n\n\n集群    ascend-x\n\n\n级别    CRITICAL\n\n\n\n'
body = m.get_email_body(email.message_from_bytes(
    'Content-Type: text/plain; charset=utf-8\n\n'.encode('utf-8') + PLAIN.encode('utf-8')))
print('结果:', repr(body))
""", {"PYTHONIOENCODING": "utf-8"}),
           must_contain=("'告警\\n\\n集群    ascend-x\\n\\n级别    CRITICAL'",))


def test_html_body_conversion_keeps_links_and_decodes_gbk():
    """HTML 转纯文本：表格行要分行、链接不能丢（双引号与单引号两种写法都要认）、
    GBK 正文要能解。注意必须走 message_from_bytes —— 生产就是按字节解析的；
    message_from_string 建出来的 Message，get_payload(decode=True) 会把中文
    变成 \\uXXXX 字面量（email.message._decode_8bit 的行为），那是测试自身的坑。"""
    _drop(CONFIG)
    _check("HTML/GBK 正文",
           _run(r"""
import email, xlb_notify as m
def body(header, text_bytes):
    return m.get_email_body(email.message_from_bytes(header.encode('utf-8') + text_bytes))
HTML = 'Content-Type: text/html; charset=utf-8\n\n'
print('双引号 ->', body(HTML, '<p>见 <a href="http://a/b">链接</a></p>'.encode('utf-8')))
print('单引号 ->', body(HTML, "<p>见 <a href='http://c/d'>链接</a></p>".encode('utf-8')))
print('表格   ->', body(HTML, '<table><tr><th>集群</th><td>ascend-x</td></tr></table>'.encode('utf-8')))
print('纯文本 ->', body('Content-Type: text/plain; charset=utf-8\n\n', '纯粹的正文'.encode('utf-8')))
print('GBK    ->', body('Content-Type: text/plain; charset=gbk\n\n', '中文正文'.encode('gbk')))
""", {"PYTHONIOENCODING": "utf-8"}),
           must_contain=("http://a/b", "http://c/d", "ascend-x", "纯粹的正文", "中文正文"))


# ---------------------------------------------------------------- 快照与 DRY_RUN

# process() 的替身环境：假 IMAP（一封固定邮件）+ 假 requests（记下每次发送）。
# 用例脚本拼在这个前缀后面，最后自己调 m.process()。共用一份是为了让三个用例
# 的替身行为完全一致 —— 否则「真跑」与「失败重投」两个用例的差异会被替身差异盖住。
_PROCESS_HARNESS = r"""
import json, os, logging, xlb_notify as m

MAILS = [
    ('Message-ID: <m1@x>\r\n'
     'Subject: =?utf-8?B?5Lit5paH5Li76aKY?=\r\n'
     'Content-Type: text/plain; charset=utf-8\r\n\r\n正文内容').encode('utf-8'),
]

# 抓住日志正文，用来断言「日志里确实说了」而不只是「代码本意想说」
log_messages = []
class _Capture(logging.Handler):
    def emit(self, record):
        log_messages.append(record.getMessage())
m.log.addHandler(_Capture())

posted = []
def ok_post(url, json=None, timeout=None):
    posted.append(json)
    class R: ok = True; status_code = 200; text = ''
    return R()
m.requests.post = ok_post

class FakeImap:
    def login(self, *a): return ('OK', [b''])
    def select(self, folder): return ('OK', [b'1'])
    def search(self, charset, criteria):
        return ('OK', [b' '.join(str(i + 1).encode() for i in range(len(MAILS)))])
    def fetch(self, mid, spec):
        return ('OK', [(b'head', MAILS[int(mid) - 1])])
    def logout(self): return ('BYE', [b''])
m.imaplib.IMAP4_SSL = lambda *a, **k: FakeImap()

def snapshot_ids():
    if not os.path.exists(m.SNAPSHOT_FILE):
        return []
    return [e['id'] for e in json.load(open(m.SNAPSHOT_FILE, encoding='utf-8'))]
"""


def test_dry_run_skips_send_and_does_not_touch_snapshot():
    """DRY_RUN 下 welink_post 直接返回 True。若顺手记进快照，「没发」就被记成「已发」，
    联调完接着跑真那轮会全部判重、一封都不发，而日志只显示 dup=N。

    注意这里必须在**同一个进程里连跑两轮 process()** 才验得出来：只查「发没发」
    是查不出这个坑的，坑在第二轮 —— 所以断言的是第二轮真的发出去了。
    """
    _drop(CONFIG)
    _drop(SNAPSHOT)
    _check("DRY_RUN 跑完整流程后不污染快照",
           _run(_PROCESS_HARNESS + r"""
m.process()
print('第一轮 DRY_RUN 实际发出:', len(posted))
print('第一轮后快照存在:', os.path.exists(m.SNAPSHOT_FILE))
""", {"XLBN_DRY_RUN": "1"}),
           must_contain=("第一轮 DRY_RUN 实际发出: 0", "第一轮后快照存在: False"))
    _drop(SNAPSHOT)


def test_real_run_sends_to_every_receiver_then_dedupes():
    """非 DRY_RUN：一封邮件要发给每个收件人各一条（不是拼成一个串发一条），
    第二轮同样的 Message-ID 必须判重不再发。"""
    _drop(CONFIG)
    _drop(SNAPSHOT)
    _check("真跑：逐个收件人 + 按 Message-ID 判重",
           _run(_PROCESS_HARNESS + r"""
m.process()
print('第一轮发出:', len(posted), '收件人:', [p['receiver'] for p in posted])
print('内容首行:', posted[0]['content'].splitlines()[0])
print('快照:', json.load(open(m.SNAPSHOT_FILE))[0]['id'])
m.process()
print('第二轮发出（累计）:', len(posted))
"""),
           must_contain=("第一轮发出: 3 收件人: ['w0012345', 'w0067890', 'w0099999']",
                         "内容首行: 中文主题",
                         "快照: <m1@x>",
                         "第二轮发出（累计）: 3"))
    _drop(SNAPSHOT)


def test_mails_without_message_id_get_distinct_stable_keys():
    """没有 Message-ID 的信（有的发件人不带）去重 key 必须**跨轮稳定且每封不同**。

    兜底不能用 IMAP 序号：序号每轮重新编号，同一封可能换号（漏判重、反复重发），
    而不同的信在 TTL 窗口内可能顶到同一个号（误判重、静默漏发）。这条用例断言
    两封无 ID 的信都发得出去、且快照里各占一条 —— 共用一个 key 的实现会在这里挂。
    """
    _drop(CONFIG)
    _drop(SNAPSHOT)
    _check("无 Message-ID 不能共用一个 key",
           _run(_PROCESS_HARNESS + r"""
MAILS.append(b'Subject: no message id\r\n'
             b'Content-Type: text/plain; charset=utf-8\r\n\r\n\xe7\xac\xac\xe4\xba\x8c\xe5\xb0\x81')
m.process()
print('发出总数:', len(posted))
print('快照条数:', len(snapshot_ids()))
print('两个 key 不同:', len(set(snapshot_ids())) == 2)
print('key 前缀:', sorted(snapshot_ids())[-1][:7])
"""),
           must_contain=("发出总数: 6", "快照条数: 2", "两个 key 不同: True",
                         "key 前缀: sha256:"))
    _drop(SNAPSHOT)


def test_batch_cap_bounds_a_single_run():
    """单轮最多尝试 MAX_PER_RUN 封：撞频率限制就是从某一封开始尾部全失败，得把一次打爆
    换成多轮排空。25 封 + 上限 20 → 只尝试 20 封，剩下的绝不碰。"""
    _drop(CONFIG)
    _drop(SNAPSHOT)
    _check("上限生效",
           _run(_PROCESS_HARNESS + r"""
MAILS.clear()
for i in range(25):
    MAILS.append(('Message-ID: <m%d@x>\r\nSubject: s%d\r\n'
                  'Content-Type: text/plain; charset=utf-8\r\n\r\nbody%d'
                  % (i, i, i)).encode('utf-8'))
m.process()
print('尝试发送（3 收件人 x 封数）:', len(posted))
print('快照条数:', len(snapshot_ids()))
""", {"XLBN_MAX_PER_RUN": "20"}),
           must_contain=("尝试发送（3 收件人 x 封数）: 60", "快照条数: 20"))
    _drop(SNAPSHOT)


def test_batch_cap_says_so_in_the_log_and_the_run_drains_across_runs():
    """两件事一起验：
    1. 早停必须在日志里说出来 —— 不说的话，看日志的人只会看到「这次只发了 20 封」，
       会以为剩下的被丢了（上一轮「为什么有的邮件没转出来」就是这么被误判的）。
    2. 下一轮真的接着排空，而且**不重发**上一轮已发的那些。"""
    _drop(CONFIG)
    _drop(SNAPSHOT)
    _check("多轮排空 + 早停有说明",
           _run(_PROCESS_HARNESS + r"""
MAILS.clear()
for i in range(25):
    MAILS.append(('Message-ID: <m%d@x>\r\nSubject: s%d\r\n'
                  'Content-Type: text/plain; charset=utf-8\r\n\r\nbody%d'
                  % (i, i, i)).encode('utf-8'))
m.process()
first = len(posted)
print('第一轮:', first, '快照:', len(snapshot_ids()))
print('日志有早停说明:', any('已达本次上限 20 封' in x and '剩余下次运行继续排空' in x
                             for x in log_messages))
print('done 行标了早停:', any('早停（达上限）' in x for x in log_messages))
m.process()
print('第二轮新增:', len(posted) - first)
print('第二轮后快照:', len(snapshot_ids()))
""", {"XLBN_MAX_PER_RUN": "20"}),
           must_contain=("第一轮: 60 快照: 20", "日志有早停说明: True",
                         "done 行标了早停: True", "第二轮新增: 15", "第二轮后快照: 25"))
    _drop(SNAPSHOT)


def test_batch_cap_does_not_waste_budget_on_already_sent_ids():
    """上限管的是**尝试数**，判重跳过的不占额度 —— 否则一个积压很久的文件夹
    每轮都会被一堆已发的信把额度吃光，新的信永远排不到。"""
    _drop(CONFIG)
    _drop(SNAPSHOT)
    _check("判重不占额度",
           _run(_PROCESS_HARNESS + r"""
import json, datetime
MAILS.clear()
for i in range(25):
    MAILS.append(('Message-ID: <m%d@x>\r\nSubject: s%d\r\n'
                  'Content-Type: text/plain; charset=utf-8\r\n\r\nbody%d'
                  % (i, i, i)).encode('utf-8'))
# 前 23 封假装早就发过了
now = datetime.datetime.now().isoformat()
json.dump([{'id': '<m%d@x>' % i, 'sent_at': now} for i in range(23)],
          open(m.SNAPSHOT_FILE, 'w', encoding='utf-8'))
m.process()
print('尝试发送:', len(posted))
print('快照条数:', len(snapshot_ids()))
""", {"XLBN_MAX_PER_RUN": "20"}),
           must_contain=("尝试发送: 6", "快照条数: 25"))
    _drop(SNAPSHOT)


def test_send_failure_is_not_recorded_so_it_retries():
    """发送失败绝不能记进快照 —— 记了就等于这封信永久丢掉，且日志只有一行 warning。"""
    _drop(CONFIG)
    _drop(SNAPSHOT)
    _check("失败不记快照，下轮重投",
           _run(_PROCESS_HARNESS + r"""
def failing_post(url, json=None, timeout=None):
    posted.append(json)
    class R: ok = False; status_code = 500; text = 'boom'
    return R()
m.requests.post = failing_post
m.process()
print('快照存在:', os.path.exists(m.SNAPSHOT_FILE))
print('有上报异常:', any('告警脚本异常' in p['content'] for p in posted))
"""),
           must_contain=("快照存在: False", "有上报异常: True"))
    _drop(SNAPSHOT)


def test_failed_snapshot_write_is_surfaced_not_swallowed():
    """快照写不进去 = 下一轮把这一批**重新发一遍**，是「每轮全量重发」的直接机制。
    不能只落一行日志：必须计入 save_fail、写进 done 行、并且发一条 WeLink 告警。"""
    _drop(CONFIG)
    _drop(SNAPSHOT)
    _check("快照写失败要上报",
           _run(_PROCESS_HARNESS + r"""
# 把快照指到一个不存在的目录 → 写入必然失败
m.SNAPSHOT_FILE = os.path.join(os.path.dirname(m.SNAPSHOT_FILE), 'no-such-dir', 'x.json')
m.process()
print('新增发送:', len(posted))
print('done 行提到 save_fail:', any('save_fail=1' in x for x in log_messages))
print('发了告警:', any('快照写入失败' in p['content'] for p in posted))
"""),
           must_contain=("新增发送: 6", "done 行提到 save_fail: True", "发了告警: True"))


def test_corrupt_snapshot_is_quarantined_so_it_stops_repeating():
    """坏快照会被改名留证：留着好查因（原地覆盖就抹掉了），改名后下一轮不再反复报同一个错。"""
    _drop(CONFIG)
    _drop(SNAPSHOT)
    _check("坏快照改名留证",
           _run(r"""
import glob, os, xlb_notify as m
open(m.SNAPSHOT_FILE, 'w').write('{ 坏 json')
print('读到:', m.load_snapshot())
quarantined = glob.glob(m.SNAPSHOT_FILE + '.corrupt-*')
print('留下坏文件:', len(quarantined) == 1)
print('原文件已移走:', not os.path.exists(m.SNAPSHOT_FILE))
print('再读一次不再报错:', m.load_snapshot() == [])
"""),
           must_contain=("读到: []", "留下坏文件: True", "原文件已移走: True",
                         "再读一次不再报错: True"))


def test_snapshot_write_is_atomic_and_leaves_no_temp_file():
    """原子换名写：进程被中途杀掉不会留下截断的 JSON（那会让下一轮判重依据归零、
    整批重发）。顺带断言不留 .tmp 残骸。"""
    _drop(CONFIG)
    _drop(SNAPSHOT)
    _check("快照原子写",
           _run(r"""
import glob, json, os, xlb_notify as m
print('保存成功:', m.save_snapshot([{'id': 'a', 'sent_at': m.datetime.now().isoformat()}]))
print('内容:', [e['id'] for e in json.load(open(m.SNAPSHOT_FILE, encoding='utf-8'))])
print('残留 .tmp:', glob.glob(m.SNAPSHOT_FILE + '.tmp'))
"""),
           must_contain=("保存成功: True", "内容: ['a']", "残留 .tmp: []"))
    _drop(SNAPSHOT)


def test_max_per_run_must_be_a_positive_integer():
    _drop(CONFIG)
    for bad in ("0", "-1", "abc"):
        _check("max_per_run=%r 被拒" % bad,
               _run("import xlb_notify", {"XLBN_MAX_PER_RUN": bad}),
               expect_rc=1, must_contain=("XLBN_MAX_PER_RUN",))


def test_snapshot_survives_garbage_and_drops_expired_entries():
    """快照是本机运行态，可能被手改坏。坏文件/非数组要当成空表，超 TTL 的要丢掉。"""
    _drop(CONFIG)
    _check("快照健壮性",
           _run(r"""
import json, xlb_notify as m
open(m.SNAPSHOT_FILE, 'w').write('{ 坏 json')
print('坏文件 ->', m.load_snapshot())
open(m.SNAPSHOT_FILE, 'w').write('{"a": 1}')
print('非数组 ->', m.load_snapshot())
json.dump([{'id': '旧', 'sent_at': '2000-01-01T00:00:00'},
           {'id': '新', 'sent_at': m.datetime.now().isoformat()}],
          open(m.SNAPSHOT_FILE, 'w', encoding='utf-8'), ensure_ascii=False)
print('过滤后 ->', [e['id'] for e in m.load_snapshot()])
m.save_snapshot([{'id': 'x', 'sent_at': m.datetime.now().isoformat()}])
print('回读 ->', [e['id'] for e in m.load_snapshot()])
"""),
           must_contain=("坏文件 -> []", "非数组 -> []", "过滤后 -> ['新']", "回读 -> ['x']"))
    _drop(SNAPSHOT)


# ---------------------------------------------------------------- 旧逻辑确已移除


def test_alert_parsing_whitelist_and_routing_are_gone():
    """整体替换过，旧的名字不该以死代码的形式留下 —— 留着就会被误以为还在生效。"""
    _drop(CONFIG)
    dropped = ("parse_alert", "format_message", "should_skip", "is_alert_subject",
               "get_receivers", "_parse_routes", "ALERT_ROUTES", "ALERT_SENDER",
               "ALERT_SENDERS", "SKIP_ALERTS", "ALERT_SUBJECT_KEYWORDS",
               "single_receiver_test", "SINGLE_RECEIVER_TEST", "_sender_search")
    _check("旧逻辑已移除",
           _run("import xlb_notify as m; print('残留=', [n for n in %r if hasattr(m, n)])"
                % (dropped,)),
           must_contain=("残留= []",))


def test_fetch_all_emails_has_no_sender_filter():
    """搜邮件只按时间窗。留了 FROM 过滤就等于把「所有邮件」偷偷缩回白名单。"""
    _drop(CONFIG)
    _check("无 FROM 过滤",
           _run(r"""
import inspect, xlb_notify as m
src = inspect.getsource(m.fetch_all_emails)
print('有 SENTSINCE:', 'SENTSINCE' in src)
print('有 FROM     :', 'FROM' in src)
"""),
           must_contain=("有 SENTSINCE: True", "有 FROM     : False"))


# ---------------------------------------------------------------- IMAP 层静默停摆


def test_imap_select_failure_raises_instead_of_looking_healthy():
    """select 失败只返回 NO、不抛异常。不显式判掉，后面 search 也返回 NO，
    最终表现成「fetched 0 emails」—— 脚本天天报平安，实际一封没读。"""
    _drop(CONFIG)
    _check("select 失败要抛",
           _run(r"""
import xlb_notify as m
class Fake:
    def login(self, *a): return ('OK', [b''])
    def select(self, folder): return ('NO', [b'no such mailbox'])
m.imaplib.IMAP4_SSL = lambda *a, **k: Fake()
try:
    m.imap_connect()
    print('结果: 没抛异常（静默停摆）')
except RuntimeError as e:
    print('结果: 已抛 ->', e)
"""),
           must_contain=("结果: 已抛", "select", "失败"))
    _check("select 正常时照常返回",
           _run(r"""
import xlb_notify as m
class Fake:
    def login(self, *a): return ('OK', [b''])
    def select(self, folder): return ('OK', [b'3'])
m.imaplib.IMAP4_SSL = lambda *a, **k: Fake()
print('结果:', type(m.imap_connect()).__name__)
"""),
           must_contain=("结果: Fake",))


def test_fetch_email_bytes_rejects_everything_but_a_proper_tuple():
    """个别服务器回裸字节串，那时 first[1] 是个整数（取到字节里的第 2 个数字），
    流到 message_from_bytes 才炸。这里就把形状判掉。"""
    _drop(CONFIG)
    _check("fetch 形状判定",
           _run(r"""
import xlb_notify as m
class Fake:
    def __init__(self, data, status='OK'): self.data, self.status = data, status
    def fetch(self, mid, spec): return (self.status, self.data)
print('正常元组 ->', repr(m.fetch_email_bytes(Fake([(b'head', b'BODY')]), b'1')))
print('裸字节串 ->', repr(m.fetch_email_bytes(Fake([b'raw']), b'1')))
print('空列表   ->', repr(m.fetch_email_bytes(Fake([]), b'1')))
print('状态 NO  ->', repr(m.fetch_email_bytes(Fake([(b'h', b'B')], 'NO'), b'1')))
print('单元素   ->', repr(m.fetch_email_bytes(Fake([(b'head',)]), b'1')))
"""),
           must_contain=("正常元组 -> b'BODY'", "裸字节串 -> None", "空列表   -> None",
                         "状态 NO  -> None", "单元素   -> None"))
    _check("search 只用时间窗、无结果返回空表",
           _run(r"""
import xlb_notify as m
seen = {}
class Fake:
    def search(self, charset, criteria):
        seen['criteria'] = criteria
        return ('OK', [b''])
print('结果:', m.fetch_all_emails(Fake()))
print('条件:', seen['criteria'])
"""),
           must_contain=("结果: []", "SENTSINCE"))


# ---------------------------------------------------------------- 凭据不入库


def test_no_plaintext_credentials_in_the_script():
    """本仓是 PUBLIC。此前 WELINK_AUTH 与邮箱密码就硬编码在这个文件里，已明文推到
    公开远端（commit 098d6e5）。这条断言防止重演：字面量不许赋给名字像凭据的变量。"""
    source = SCRIPT_SOURCE.read_text(encoding="utf-8")
    credential_name = re.compile(r"(?:auth|passw|passwd|password|secret|token|credential)",
                                 re.IGNORECASE)
    assignment = re.compile(r"""^\s*([A-Za-z_]\w*)\s*=\s*(['"])(.+?)\2""")
    hits = []
    for number, line in enumerate(source.splitlines(), 1):
        match = assignment.match(line)
        if match and credential_name.search(match.group(1)):
            hits.append("%d: %s" % (number, line.strip()))
    assert not hits, "把字面量赋给了名字像凭据的变量：\n  " + "\n  ".join(hits)

    # 反向保险：确认凭据确实有入口，而不是「干脆没有凭据这回事」
    for name in ("XLBN_WELINK_AUTH", "XLBN_IMAP_PASSWORD"):
        assert name in source, "源码里找不到凭据入口 %s" % name


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failed = 0
    try:
        for test in tests:
            try:
                test()
            except AssertionError as exc:
                failed += 1
                print("FAIL  %s\n      %s" % (test.__name__, exc))
            else:
                print("PASS  %s" % test.__name__)
        print("\n%d/%d 通过" % (len(tests) - failed, len(tests)))
        if failed:
            print("（临时目录保留在 %s，便于翻现场）" % WORK)
    finally:
        if not failed:
            shutil.rmtree(WORK, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
