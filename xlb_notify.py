#!/usr/bin/env python3
"""把中间邮箱某个文件夹里的邮件，原样转发到 WeLink。

链路（三段两跳）：

    本脚本 ──smtp.qq.com:587──> 中间邮箱 ──IMAP──> xlb_notify.py(内网Windows) ──> 小鲁班 WeLink

本文件是**第二跳**：内网 Windows 机上跑，读中间邮箱、转发到小鲁班。

## 现在的口径：全量转发，不做内容判断

早先这脚本是个「告警转发器」——靠发件人白名单 + 主题必须含 FIRING/CRITICAL/…
把告警从一堆信里挑出来，再用 parse_alert 解析成结构化字段、按告警名路由到收件人。
**那套逻辑已经整体移除。** 现在只做一件事：文件夹里的每封信，取「主题 + 正文纯文本」
发给 DEFAULT_RECEIVER 里列出的每个收件人，内容不做任何改写、判断或路由。

为什么移除：那套解析对邮件格式是**硬契约**（主题必须双方括号、标签值不能含 http、
标签与值必须分行），契约另一端在另一台机器上的另一个脚本里。任何一端改格式，
这一端就静默丢字段 —— 这个耦合不值得为「挑告警」付。要挑，该在发信那一端挑。

## 收件人

全部发给 `DEFAULT_RECEIVER`（多个用逗号/分号分隔，逐个发）。不再有地址映射，
也不再按告警名路由。

## 三个安全阀

1. **时间窗**：只搜最近 `XLBN_SEARCH_DAYS` 天（默认 3）。**不设这条，首次运行会把
   整个文件夹的历史邮件一次性群发出去。** 要真的全量就把天数调大。
2. **单轮上限**：一轮最多**尝试**发 `XLBN_MAX_PER_RUN` 封（默认 20）。小鲁班有频率
   限制，一轮把整个文件夹打完（每封还要乘以收件人数）会从某一封开始**尾部全失败**；
   而失败的邮件按其定义不记快照，于是下一轮从头再来、又在同一位置撞上限制 ——
   尾部那批**永远发不出去**，前面那批每轮重发。有上限就变成「分多轮慢慢排空」：
   已发的都记了快照会判重跳过，未发的就排到了前面。**判据是「尝试数」不是「成功数」**
   —— 要限的正是请求量，只数成功就等于没限住。
3. **去重快照**：按 Message-ID 记已发过的，7 天 TTL；**每发成功一封就立刻落盘**
   （原子换名写），不是攒到最后一起写 —— 进程中途被杀也不会丢掉这一轮的成绩。
   发送失败的**不记**，下轮重试。少数发件人不带 Message-ID，那种信按正文 sha256
   做 key（不能用 IMAP 序号 —— 序号每轮重新编号，会漏判重也会误判重）。

⚠️ 即便如此，首次上线仍可能一次发出大量邮件（上限 20 封封顶）—— 这个文件夹收的是
**所有外部来信**，没有内容过滤。上线前先 `dry_run` 跑一轮看数量（[DRY-RUN] 日志会逐条
打出收件人与正文摘要）。

## 快照写不进去 = 全量重发

去重完全依赖 `email_alert_snapshot.json`。它读不出来或写不进去，等价于「一封都没发过」，
下一轮会把整批重发一遍 —— 这是「每轮都全量重发」最容易被忽略的机制。所以：

  * 读失败：坏文件**改名留证**（`*.corrupt-<时间戳>`）并打 error 级日志，然后按空快照继续；
  * 写失败：**不吞**。计入 `save_fail`，写进 `done:` 行，并**发一条 WeLink 告警**。

## 凭据

一律从环境变量或同目录 xlb_notify.config.json 读，代码里一个默认值都不留：
`TangJia025/general` 是 **PUBLIC** 仓，本文件此前把 WELINK_AUTH 与邮箱密码硬编码在
第 30/36 行，两个凭据已明文推到公开远端（commit 098d6e5），必须按「已泄露」处理。
缺凭据时**硬失败**而不是降级 —— 静默降级会把「没读到凭据」表现成「发送失败」，
排查方向直接跑偏；而告警脚本静默失效等于监控静默停摆。

    XLBN_WELINK_AUTH       必填  小鲁班发送凭据
    XLBN_IMAP_USER         必填  中间邮箱账号
    XLBN_IMAP_PASSWORD     必填  该邮箱密码/授权码
    XLBN_DEFAULT_RECEIVER  必填  收件人，多个用逗号分隔
    XLBN_WELINK_URL / XLBN_IMAP_SERVER / XLBN_IMAP_FOLDER / XLBN_SEARCH_DAYS  选填
    XLBN_MAX_PER_RUN       选填  一轮最多尝试发几封，默认 20（见上面第 2 条安全阀）
    XLBN_DRY_RUN           选填  设 1/true 则只打日志不发 WeLink（首次联调用）
"""

import os
import re
import sys
import html
import json
import email
import hashlib
import imaplib
import logging
import requests
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from email.header import decode_header


if sys.stdout.encoding and sys.stdout.encoding.lower().startswith('gbk'):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = sys.stdout


# ========== 路径 ==========
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_FILE = os.path.join(BASE_DIR, 'email_alert_snapshot.json')
LOG_FILE = os.path.join(BASE_DIR, 'alert.log')
# 本机凭据文件（不入库，见 .gitignore）。放在脚本同目录而不是用户目录：本脚本的部署形态
# 是「整包拷到内网 Windows 机上跑」，同目录最省事，也不用管 %APPDATA% 之类的差异。
# 模板见同目录 xlb_notify.config.example.json —— 那个是入库的，值全是占位符。
CONFIG_FILE = os.path.join(BASE_DIR, 'xlb_notify.config.json')

SNAPSHOT_TTL_DAYS = 7
# 只搜最近 N 天。这是**安全阀**：去掉它，首次运行会把整个文件夹的历史邮件全部群发。
# 要真全量就把 XLBN_SEARCH_DAYS 调大（或设成 3650）。
DEFAULT_SEARCH_DAYS = 3
# 单次运行最多**尝试**发几封邮件（不是几个 HTTP 请求 —— 实际请求数还要乘以收件人数）。
#
# 为什么必须有这个上限：小鲁班有频率限制。一轮把整个文件夹的信（每封再乘以收件人数）
# 一口气打完，超出配额之后**尾部**全部失败。而失败的邮件按设计不记进快照（好让它下轮重投），
# 于是下一轮从头再来一遍、又在同一位置撞上限制 —— 尾部那批信**永远发不出去**，
# 而前面那批每轮都重发一遍。加个上限就把「一次打爆」换成「分多轮慢慢排空」。
#
# 为什么默认 20：按每 5 分钟跑一轮算，20 封/轮 ≈ 240 封/小时，够用；单轮 HTTP 请求
# 最多 20×收件人数，不容易撞配额。
DEFAULT_MAX_PER_RUN = 20
# WeLink content 是单条文本，实测过长会被截断或拒收。超长时截断并在日志里说明。
MAX_CONTENT_CHARS = 4000

# ========== 凭据与收件人（环境变量优先，其次 xlb_notify.config.json）=========
# 为什么一个默认值都不留：`TangJia025/general` 是 **PUBLIC** 仓。本文件此前把 WELINK_AUTH
# 与邮箱密码硬编码在第 30/36 行，两个凭据已明文推到公开远端（commit 098d6e5），必须按
# 「已泄露」处理（轮换 + 清历史），不是删掉这几行就算完。
# 为什么缺凭据要**硬失败**而不是降级：静默降级会把「没读到凭据」表现成「发送失败」，
# 排查方向直接跑偏；而告警脚本静默失效等于监控静默停摆。同款纪律见 LLM 判决层
# （`npu_ci_forensics.py` 没读到 DEEPSEEK_API_KEY 就 SystemExit）。
#
# 两个来源，按序取值（环境变量 → 配置文件 → 内置默认值）。**值只填在环境变量或
# xlb_notify.config.json 里，代码里永远不填** —— 见上面那段泄露历史。
REQUIRED_ENV = (
    ('XLBN_WELINK_AUTH', 'welink_auth'),
    ('XLBN_IMAP_USER', 'imap_user'),
    ('XLBN_IMAP_PASSWORD', 'imap_password'),
    ('XLBN_DEFAULT_RECEIVER', 'default_receiver'),
)


def _load_config_file():
    """读同目录下的 xlb_notify.config.json。文件不存在就返回空字典（不算错误）。

    读失败**不吞**：JSON 写错、或不小心把带注释的 JSONC 存成 .json，都必须当场报出来。
    否则表现是「凭据全都读不到」→ 报「缺少必需的环境变量」，人会去查环境变量而查不到真因。
    """
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except ValueError as exc:
        raise SystemExit('%s 不是合法 JSON：%s' % (CONFIG_FILE, exc))
    except OSError as exc:
        raise SystemExit('读取 %s 失败：%s' % (CONFIG_FILE, exc))
    if not isinstance(data, dict):
        raise SystemExit('%s 顶层必须是 JSON 对象（键名见 xlb_notify.config.example.json）' % CONFIG_FILE)
    return data


FILE_CONFIG = _load_config_file()


def _setting(name, key, default=''):
    """按「环境变量 → 配置文件 → 默认值」取值。空串视为未设置，以便配置文件兜底。"""
    value = os.environ.get(name, '').strip()
    if not value:
        value = str(FILE_CONFIG.get(key, '') or '').strip()
    return value or default


def load_config():
    """读凭据、账号与收件人。返回 (config, 缺失的必填变量名列表)。"""
    config = {
        'welink_url': _setting('XLBN_WELINK_URL', 'welink_url', 'http://xiaoluban.rnd.huawei.com:80/'),
        'imap_server': _setting('XLBN_IMAP_SERVER', 'imap_server', 'imailcn56.email.huawei.com'),
        'imap_folder': _setting('XLBN_IMAP_FOLDER', 'imap_folder', 'INBOX/&WRaQ6JCuTvY-'),
    }
    for name, key in REQUIRED_ENV:
        config[key] = _setting(name, key)
    # 文件夹名含非 ASCII 就当场拦下：imaplib 在 _command 里用 bytes(arg, self._encoding)
    # 而默认 _encoding 是 ascii，明文中文会抛 UnicodeEncodeError（实测）。不拦的话报错长这样
    # 「'ascii' codec can't encode characters…」，看不出是文件夹名的问题。
    if any(ord(ch) > 127 for ch in config['imap_folder']):
        raise SystemExit(
            'XLBN_IMAP_FOLDER 含非 ASCII 字符：%r\n'
            'IMAP 文件夹名必须写**修改版 UTF-7**，例如「外部邮件」写 INBOX/&WRaQ6JCuTvY-。'
            % config['imap_folder'])
    raw_days = _setting('XLBN_SEARCH_DAYS', 'search_days')
    try:
        config['search_days'] = int(raw_days) if raw_days else DEFAULT_SEARCH_DAYS
    except ValueError:
        raise SystemExit('XLBN_SEARCH_DAYS 必须是整数天数：%r' % raw_days)
    if config['search_days'] <= 0:
        raise SystemExit('XLBN_SEARCH_DAYS 必须为正数（当前 %r）；'
                         '它同时是首次运行的爆炸半径控制' % config['search_days'])
    raw_max = _setting('XLBN_MAX_PER_RUN', 'max_per_run')
    try:
        config['max_per_run'] = int(raw_max) if raw_max else DEFAULT_MAX_PER_RUN
    except ValueError:
        raise SystemExit('XLBN_MAX_PER_RUN 必须是个数（每次运行最多尝试发几封）：%r' % raw_max)
    if config['max_per_run'] <= 0:
        raise SystemExit('XLBN_MAX_PER_RUN 必须为正数（当前 %r）。'
                         '想不限量就填个很大的数（比如 100000），别填 0 —— '
                         '填 0 会被当成「一封都不发」还是「不限量」全靠猜，'
                         '这种含糊比多写一位数字危险' % config['max_per_run'])
    config['dry_run'] = _setting('XLBN_DRY_RUN', 'dry_run').lower() in ('1', 'true', 'yes', 'on')
    missing = [name for name, key in REQUIRED_ENV if not config[key]]
    return config, missing


CONFIG, MISSING_CONFIG = load_config()

WELINK_URL = CONFIG['welink_url']
WELINK_AUTH = CONFIG['welink_auth']
DEFAULT_RECEIVER = CONFIG['default_receiver']
# 收件人按逗号/分号切开。为什么必须切：`welink_post` 每次只发一个 receiver，
# `process()` 靠遍历列表逐个发。不切的话整个字符串会被当成**一个**收件人 ID 发出去 ——
# 小鲁班拿到 "个人id,群id" 这种畸形串，会投给个人、丢掉群，而且返回 200 不报错，
# 日志里只留一行 `send ok to 个人id,群id`，看起来像发成功了。
DEFAULT_RECEIVERS = [s.strip() for s in re.split(r'[,;]', DEFAULT_RECEIVER) if s.strip()]
IMAP_SERVER = CONFIG['imap_server']
IMAP_USER = CONFIG['imap_user']
IMAP_PASSWORD = CONFIG['imap_password']
IMAP_FOLDER = CONFIG['imap_folder']
SEARCH_DAYS = CONFIG['search_days']
MAX_PER_RUN = CONFIG['max_per_run']

# 首次联调时不必改代码（改代码就有顺手 commit 的风险）
DRY_RUN = CONFIG['dry_run']


# ========== 日志配置 ==========
def setup_logging():
    """日志同时写文件(带轮转)和控制台"""
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding='utf-8',
    )
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 防止重复添加 handler
    if not root.handlers:
        root.addHandler(file_handler)
        root.addHandler(console_handler)
    return root


setup_logging()
log = logging.getLogger('alert')


# ========== 发送 ==========
def welink_post(content, receiver, auth=WELINK_AUTH):
    if DRY_RUN:
        log.info('[DRY-RUN] skip send to %s: %s', receiver, content[:60])
        return True
    try:
        r = requests.post(WELINK_URL, json={
            'content': content, 'receiver': receiver, 'auth': auth,
        }, timeout=10)
        if r.ok:
            log.info('send ok to %s: %s', receiver, content[:50])
            return True
        # 失败时记录 receiver + 状态码 + 响应体
        log.error('send fail (%s) to %s: %s', r.status_code, receiver, r.text[:300])
        return False
    except Exception as e:
        # 异常时记录 receiver + 异常类型 + 信息
        log.error('send exception to %s: %s: %s', receiver, type(e).__name__, e)
        return False


def welink_post_all(content, receivers):
    """给每个收件人各发一条请求，任一失败即整体不算成功。

    为什么要逐个发而不是拼成一个字符串：小鲁班的 receiver 收的是单个 ID。
    把多个 ID 拼成 "a,b" 传过去会被当成一个畸形 ID —— 实测表现为只投给第一个、
    其余的静默丢掉，且接口照样返回 200。多个收件人必须发多次。
    """
    all_ok = True
    for receiver in receivers:
        if not welink_post(content, receiver):
            all_ok = False
    return all_ok


# ========== 去重快照 ==========
def load_snapshot():
    """读快照。文件缺失/损坏时返回空表，并把坏文件改名留证。

    为什么要「改名留证」而不是就地忽略：快照是唯一的去重依据，读不出来就等于
    「一封都没发过」—— 下一轮会把整批重新发一遍。这个放大效应很危险，所以坏文件
    必须留下痕迹好查因（原地覆盖就把它抹掉了），改名后下一轮不再反复报同一个错。
    """
    if not os.path.exists(SNAPSHOT_FILE):
        return []
    try:
        with open(SNAPSHOT_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError('快照顶层不是数组，实际是 %s' % type(data).__name__)
    except Exception as e:
        quarantine = '%s.corrupt-%s' % (SNAPSHOT_FILE, datetime.now().strftime('%Y%m%d%H%M%S'))
        try:
            os.replace(SNAPSHOT_FILE, quarantine)
            note = '，已改名为 %s' % os.path.basename(quarantine)
        except OSError as move_error:
            note = '，且改名也失败（%s）' % move_error
        # 用 error 级别：这是「本轮会把整批重发一遍」的直接原因，必须显眼
        log.error('load snapshot failed: %s%s —— 本轮的去重依据为空，会按快照为空重发', e, note)
        return []

    cutoff = (datetime.now() - timedelta(days=SNAPSHOT_TTL_DAYS)).isoformat()
    kept = [e for e in data if isinstance(e, dict) and e.get('sent_at', '') > cutoff]
    if len(kept) != len(data):
        log.info('snapshot: %d 条中 %d 条已过 %d 天 TTL，丢弃',
                 len(data), len(data) - len(kept), SNAPSHOT_TTL_DAYS)
    return kept


def save_snapshot(entries):
    """写快照。返回是否成功 —— 调用方必须看这个返回值。

    先写临时文件再 `os.replace` 换名（同目录内换名是原子的）。原来直接 'w' 打开
    目标文件写，进程一被中途杀掉（Windows 上的计划任务是每 5 分钟拉一个新进程）
    就留下一个截断的 JSON；下一轮读它失败 → 去重依据归零 → 整批重发。
    写入失败**必须**让调用方知道：它想表达的是「这些信已经发过了」，没写进去就等于
    下一轮全部重发，这是本轮「每次都全量重发」的直接机制之一，不能只落一行日志。
    """
    tmp = SNAPSHOT_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(entries, f, ensure_ascii=False)
        os.replace(tmp, SNAPSHOT_FILE)
        return True
    except Exception as e:
        log.error('save snapshot failed: %s: %s', type(e).__name__, e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


# ========== 邮件解析 ==========
def _tidy_lines(text, strip_leading=True, max_blank=0):
    """压掉「空行严重」。

    通知类邮件的 HTML 大量用 `<td>&nbsp;</td>` 做占位/留白行，加上层层嵌套的
    </td></tr></div>，转成文本后留下**成片的、只含一个空格的空行**。原来那句
    `\\n[ \\t]*\\n+ -> \\n` 压不掉它们：空格在换行**之前**，而模式得先看到换行才匹配，
    于是 "\\n \\n" 最后剩下 " \\n" —— 一行纯空白。实测一封普通通知邮件 29 行里有 22 行
    是这种，到 WeLink 就是大片空行。

    这里改成按行处理：每行去首尾空白，连续空行最多保留 max_blank 行（默认 0，即全去掉），
    再去掉首尾空行。

    为什么 HTML 侧 max_blank=0 而不是留一行当段落分隔：留一行会把表格的标签与值拆开 ——
    `<th>集群</th><td>ascend-x</td>` 中间本来就有两个换行，留一行就变成「集群 / 空行 /
    ascend-x」，一屏通知里几十处，又回到「空行严重」。HTML 的结构靠标签表达，多余的
    空行纯粹是噪声。**纯文本侧才用 max_blank=1**：那里空行是唯一的分段手段，全去掉
    会糊成一坨。strip_leading=False 也是给纯文本用的，行首空格可能是有意的对齐。
    """
    kept = []
    for raw_line in text.splitlines():
        line = raw_line.strip() if strip_leading else raw_line.rstrip()
        if not line:
            if max_blank and kept and kept[-1] != '':
                kept.append('')
            continue
        kept.append(re.sub(r'[ \t]+', ' ', line) if strip_leading else line)
    while kept and kept[-1] == '':
        kept.pop()
    return '\n'.join(kept)


# 结构性断行：这些标签本身就表示「这里确实换行」，浏览器渲染出来也是换行。
# 与「源码里的换行」必须区别对待 —— 后者按 HTML 规范只是普通空白，见 _html_to_text。
_HARD_BREAK_PATTERN = (r'<br\s*/?>|</p\s*>|</div\s*>|</tr\s*>|</li\s*>'
                       r'|</h[1-6]\s*>|</table\s*>')


def _html_to_text(raw):
    """HTML 转纯文本，口径是「按浏览器渲染的样子」。

    分三步，顺序不能换：**先切结构性断行，再在每段内部压掉源码空白**。

    关键的一条：HTML 源码里的换行和缩进按规范就是普通空白，浏览器渲染时会折成一个
    空格。模板引擎普遍把一句话分写在多行，比如
    `尊敬的\\n  <span>华为云用户</span>\\n  ，您好！`，渲染出来是**一行**；
    但早先的实现把这些源码换行当成了真换行，于是正文被拆成一词一行 ——
    实测一封 LTS 告警邮件，「尊敬的 华为云用户 ，您好！」被拆成三行。
    所以：结构标签 → 换行，源码空白 → 一个空格。

    同理，标签要**删掉**而不是换成空格：换成空格会把 `<span>紧急</span>` 变成
    「告警级别: 紧急 ;」，而邮件里显示的是「告警级别:紧急;」。表格单元格是例外，
    `</td>`/`</th>` 换成一个空格，否则 `<td>a</td><td>b</td>` 会粘成 `ab`。
    """
    if not raw:
        return ''
    s = re.sub(r'<style[^>]*>.*?</style>', '', raw, flags=re.I | re.S)
    s = re.sub(r'<script[^>]*>.*?</script>', '', s, flags=re.I | re.S)
    # 注释要先整段去掉：`<[^>]+>` 碰到 `<!-- … -->` 只吃到第一个 `>`，注释正文里
    # 一带 `>`（如「集群 A -> 集群 B」这种内部备注，或 Outlook 的 mso 条件注释）
    # 剩下的文字就整段漏进正文了。放在切段之前还有个好处：注释里的 `<br>` 不算断行。
    s = re.sub(r'<!--.*?-->', ' ', s, flags=re.S)
    # href 的引号两种都要认。原来的正则只认双引号，单引号写法（合法 HTML，不少
    # 邮件模板就是这么生成）会**整条 URL 丢掉**、只留下一段锚文本，且不报错 ——
    # 实测确认过。在这个「原样转发」的口径下，丢链接比丢格式严重。
    s = re.sub(r'<a[^>]+href=(["\'])(.*?)\1[^>]*>([^<]*)</a>', r' \3 (\2) ', s, flags=re.I)

    lines = []
    for piece in re.split(_HARD_BREAK_PATTERN, s, flags=re.I):
        piece = re.sub(r'</t[dh]\s*>', ' ', piece, flags=re.I)
        piece = re.sub(r'<[^>]+>', '', piece)
        # 实体放在剥标签之后解：反过来会把正文里转义写出的 `&lt;b&gt;` 变成一个真标签剥掉。
        # `&nbsp;` 解出来是 \xa0，下面一并当空白压掉（实测源里常直接就是 \xa0 字符）。
        # \ufeff（BOM）和 \u200b（零宽空格）也当空白：模板常塞 `&#65279;` 当占位，
        # 留着会变成一行看不见的「空行」，是肉眼查不出来的那种噪声。
        piece = html.unescape(piece)
        lines.append(re.sub(r'[ \t\r\n\f\v\xa0\ufeff\u200b]+', ' ', piece))
    return _tidy_lines('\n'.join(lines), strip_leading=True, max_blank=0)


def _decode_part(part):
    payload = part.get_payload(decode=True)
    if payload is None:
        return part.get_payload() or ''
    charset = part.get_content_charset() or 'utf-8'
    try:
        return payload.decode(charset, errors='replace')
    except Exception:
        return payload.decode('utf-8', errors='replace')


def get_email_body(msg):
    """取正文纯文本。优先 text/plain；只有 HTML 时才转成文本。

    两条路径都要过 `_tidy_lines` —— 空行成片是通知类邮件的通病，纯文本侧的
    multipart/alternative（HTML 旁边那截）同样一堆空行。纯文本用
    strip_leading=False 且 max_blank=1：行首空格可能是监控系统打的表格对齐，
    空行则是它唯一的分段手段，两个都不能像 HTML 那样压平。
    """
    if not msg.is_multipart():
        ctype = msg.get_content_type()
        text = _decode_part(msg)
        if ctype == 'text/html':
            return _html_to_text(text)
        return _tidy_lines(text, strip_leading=False, max_blank=1)

    plain, html = '', ''
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == 'text/plain' and not plain:
            try:
                plain = _tidy_lines(_decode_part(part), strip_leading=False, max_blank=1)
            except Exception:
                pass
        elif ctype == 'text/html' and not html:
            try:
                html = _html_to_text(_decode_part(part))
            except Exception:
                pass
    return plain or html


def decode_subject(msg):
    """解出主题。编码坏掉时保留原文而不是丢掉整封信 —— 主题读不出来不该拦住转发。"""
    raw_subject = msg.get('Subject', '')
    if not raw_subject:
        return '(无主题)'
    try:
        return ''.join(
            part.decode(charset or 'utf-8', errors='replace') if isinstance(part, bytes) else part
            for part, charset in decode_header(raw_subject)
        )
    except Exception:
        return str(raw_subject)


def compose_content(subject, body):
    """主题 + 正文纯文本。不做改写、不解析字段。"""
    subject = re.sub(r'\s+', ' ', subject or '').strip() or '(无主题)'
    body = (body or '').strip()
    content = '%s\n\n%s' % (subject, body) if body else subject
    if len(content) > MAX_CONTENT_CHARS:
        log.warning('content truncated: %d -> %d chars', len(content), MAX_CONTENT_CHARS)
        content = content[:MAX_CONTENT_CHARS] + '\n…（正文过长已截断）'
    return content


# ========== IMAP ==========
def fetch_all_emails(mail):
    """搜文件夹里最近 SEARCH_DAYS 天的**全部**邮件。

    只按时间收窄 —— 不再有发件人白名单、不再有主题闸。时间窗是安全阀，
    见模块 docstring：没有它，首次运行会把整个文件夹的历史邮件群发出去。
    """
    since = (datetime.now() - timedelta(days=SEARCH_DAYS)).strftime('%d-%b-%Y')
    criteria = '(SENTSINCE "%s")' % since
    status, data = mail.search(None, criteria)
    if status != 'OK' or not data or not data[0]:
        return []
    return data[0].split()


def fetch_email_bytes(mail, mid):
    status, data = mail.fetch(mid, '(RFC822)')
    if status != 'OK' or not data:
        return None
    first = data[0]
    # 正常是 (头部, 正文) 的元组。个别服务器会回一个裸字节串，那时 first[1] 是个整数，
    # 交给 message_from_bytes 会抛 TypeError —— 得在这里就判掉，别让主循环走到那一步。
    if not isinstance(first, tuple) or len(first) < 2:
        return None
    return first[1]


def imap_connect():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, timeout=15)
    mail.login(IMAP_USER, IMAP_PASSWORD)
    status = mail.select(IMAP_FOLDER)[0]
    # select 失败是**返回 NO 而不是抛异常**，不显式判掉就会一路走到 search，
    # 那边同样只返回 NO，于是最终表现成「fetched 0 emails」—— 脚本天天报平安，
    # 实际一封没读。这类静默停摆是这套链路最怕的失效模式，所以这里要主动抛。
    if status != 'OK':
        raise RuntimeError('select %r 失败（%s）：文件夹名不对，或账号无权限' %
                           (IMAP_FOLDER, status))
    return mail


# ========== 主流程 ==========
def process():
    log.info('=' * 60)
    # 把 imap_server / imap_user 一并打出来：排查时的第一个问题是「我到底在读哪个邮箱」。
    log.info('start; imap=%s user=%s folder=%s days=%d max_per_run=%d receivers=%s dry_run=%s log=%s',
             IMAP_SERVER, IMAP_USER, IMAP_FOLDER, SEARCH_DAYS, MAX_PER_RUN,
             DEFAULT_RECEIVERS, DRY_RUN, LOG_FILE)
    mail = None
    try:
        mail = imap_connect()
        ids = fetch_all_emails(mail)
        log.info('fetched %d emails in last %d days', len(ids), SEARCH_DAYS)
    except Exception as e:
        log.error('imap connect/search failed: %s: %s', type(e).__name__, e)
        welink_post_all(f'告警脚本连接失败: {type(e).__name__}: {e}', DEFAULT_RECEIVERS)
        return

    snapshot = load_snapshot()
    sent_ids = {e['id'] for e in snapshot}
    # 达到 MAX_PER_RUN 就提前收工。**判据是「尝试了几封」，不是「成了几封」**：
    # 撞频率限制要限的正是尝试次数，只数成功的话失败会被当成「没占额度」，那这条上限
    # 就管不住请求量了。失败的信不记快照，于是下一轮它还在未发集合里、而且排在前面
    # （已发的都判重跳过了），下轮优先重投 —— 这样尾部那批才排得出去。
    attempts = 0
    budget_exhausted = False
    counters = {'new': 0, 'dup': 0, 'parse_fail': 0, 'send_fail': 0, 'save_fail': 0}

    for mid in ids:
        try:
            raw = fetch_email_bytes(mail, mid)
            if not raw:
                counters['parse_fail'] += 1
                continue

            msg = email.message_from_bytes(raw)
            message_id = msg.get('Message-ID')
            if not message_id:
                # 有的发件人不带 Message-ID。兜底**不能**用 IMAP 序号（mid）：
                # 序号是每次会话重新编号的 —— 同一封信在不同轮次编号可能不同（漏判重、
                # 反复重发），而不同的信在 7 天窗口里可能顶到同一个编号（误判重、静默漏发）。
                # 用正文摘要代替，跨轮稳定且每封不同。
                message_id = 'sha256:' + hashlib.sha256(raw).hexdigest()
            if message_id in sent_ids:
                counters['dup'] += 1
                continue

            subject = decode_subject(msg)
            content = compose_content(subject, get_email_body(msg))

            attempts += 1
            all_ok = welink_post_all(content, DEFAULT_RECEIVERS)
            if all_ok:
                counters['new'] += 1
                # DRY_RUN 下 welink_post 直接返回 True，**不能**顺手记进快照 ——
                # 否则「没发」被记成「已发」，联调完接着跑真的那轮会全部判重、一封都不发，
                # 而日志只显示 dup=N。这正是此前 fix/xlb-notify-dryrun-snapshot 修过的坑。
                if not DRY_RUN:
                    # 每封发成功就立刻落盘，不攒到最后一起写。攒着写的代价是：进程在中途
                    # 被杀（Windows 计划任务每 5 分钟一个进程，长跑容易被下一轮叠上来）
                    # 这一轮的全部成绩就丢了，下一轮从头重发。
                    snapshot.append({'id': message_id, 'sent_at': datetime.now().isoformat()})
                    if not save_snapshot(snapshot):
                        counters['save_fail'] += 1
            else:
                counters['send_fail'] += 1
                log.warning('send failed to some receivers, will retry next round: %s (msgid=%s)',
                            subject, message_id)

            if attempts >= MAX_PER_RUN:
                budget_exhausted = True
                break
        except Exception as e:
            log.error('process mail %s failed: %s: %s', mid.decode(), type(e).__name__, e)
            counters['parse_fail'] += 1

    if budget_exhausted:
        # 必须显式说出来。不说的话，后面看日志的人只会看到「这次只发了 20 封」，
        # 会以为剩下的被丢了 —— 上一轮「为什么有的邮件没转出来」就是这么被误判的。
        log.info('已达本次上限 %d 封（已发的都记进快照了），剩余下次运行继续排空',
                 MAX_PER_RUN)

    # DRY_RUN 下把 new 标成 would_send：否则这行会让人以为真发出去了 N 封
    log.info('done: fetched=%d %s=%d dup=%d parse_fail=%d send_fail=%d save_fail=%d '
             'attempts=%d snapshot=%d%s',
             len(ids), 'would_send' if DRY_RUN else 'new', counters['new'],
             counters['dup'], counters['parse_fail'], counters['send_fail'],
             counters['save_fail'], attempts, len(snapshot),
             ' 早停（达上限）' if budget_exhausted else '')

    # save_fail 也进告警：快照写不进去 = 下一轮把这一批**重新发一遍**，
    # 是「每次都全量重发」的直接机制，不能只躺在日志里等人翻。
    problems = []
    if counters['parse_fail']:
        problems.append('解析失败 %d 封' % counters['parse_fail'])
    if counters['send_fail']:
        problems.append('发送失败 %d 次' % counters['send_fail'])
    if counters['save_fail']:
        problems.append('快照写入失败 %d 次（下一轮会重发这一批）' % counters['save_fail'])
    if problems:
        welink_post_all('告警脚本异常: ' + ', '.join(problems), DEFAULT_RECEIVERS)

    if mail:
        try:
            mail.logout()
        except Exception:
            pass


if __name__ == '__main__':
    # 缺凭据硬失败：此时 welink_post 自己也没有凭据可用，连「崩溃告警」都发不出去，
    # 所以必须在进入 process() 之前就拦住，并说清缺哪几个变量。
    if MISSING_CONFIG:
        sys.exit('缺少必需的配置：%s\n'
                 '（两个来源都没给：环境变量 %s，或配置文件 %s。'
                 '本仓是 PUBLIC，凭据不入库；模板见 xlb_notify.config.example.json。'
                 '缺凭据时脚本拒绝开跑，不静默降级）'
                 % (', '.join(MISSING_CONFIG),
                    '/'.join(name for name, _key in REQUIRED_ENV), CONFIG_FILE))
    try:
        process()
    except Exception as e:
        log.exception('script crashed')
        welink_post_all(f'告警脚本崩溃: {type(e).__name__}: {str(e)[:200]}', DEFAULT_RECEIVERS)
