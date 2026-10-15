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

## 两个安全阀，都是为了让「首次运行」不至于闯祸

1. **时间窗**：只搜最近 `XLBN_SEARCH_DAYS` 天（默认 3）。**不设这条，首次运行会把
   整个文件夹的历史邮件一次性群发出去。** 要真的全量就把天数调大。
2. **去重快照**：按 Message-ID 记已发过的，7 天 TTL。发送失败的**不记**，下轮重试。
   少数发件人不带 Message-ID，那种信按正文 sha256 做 key（不能用 IMAP 序号 ——
   序号每轮重新编号，会漏判重也会误判重，见 process() 里的注释）。

⚠️ 即便如此，首次运行仍可能一次发出大量邮件 —— 这个文件夹收的是**所有外部来信**，
没有内容过滤。上线前先 `dry_run` 跑一轮看数量（[DRY-RUN] 日志会逐条打出收件人与正文摘要）。

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
    XLBN_DRY_RUN           选填  设 1/true 则只打日志不发 WeLink（首次联调用）
"""

import os
import re
import sys
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
    if not os.path.exists(SNAPSHOT_FILE):
        return []
    try:
        with open(SNAPSHOT_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        cutoff = (datetime.now() - timedelta(days=SNAPSHOT_TTL_DAYS)).isoformat()
        return [e for e in data if isinstance(e, dict) and e.get('sent_at', '') > cutoff]
    except Exception as e:
        log.error('load snapshot failed: %s', e)
        return []


def save_snapshot(entries):
    try:
        with open(SNAPSHOT_FILE, 'w', encoding='utf-8') as f:
            json.dump(entries, f, ensure_ascii=False)
    except Exception as e:
        log.error('save snapshot failed: %s', e)


# ========== 邮件解析 ==========
def _html_to_text(raw):
    if not raw:
        return ''
    s = re.sub(r'<style[^>]*>.*?</style>', '', raw, flags=re.I | re.S)
    s = re.sub(r'<script[^>]*>.*?</script>', '', s, flags=re.I | re.S)
    # href 的引号两种都要认。原来的正则只认双引号，单引号写法（合法 HTML，不少
    # 邮件模板就是这么生成）会**整条 URL 丢掉**、只留下一段锚文本，且不报错 ——
    # 实测确认过。在这个「原样转发」的口径下，丢链接比丢格式严重。
    s = re.sub(r'<a[^>]+href=(["\'])(.*?)\1[^>]*>([^<]*)</a>', r' \3 (\2) ', s, flags=re.I)
    s = re.sub(r'</?th\s*>', '\n', s, flags=re.I)
    s = re.sub(r'</?td\s*>', '\n', s, flags=re.I)
    s = re.sub(r'</?tr\s*>', '\n', s, flags=re.I)
    s = re.sub(r'<br\s*/?>', '\n', s, flags=re.I)
    s = re.sub(r'</?p\s*>', '\n', s, flags=re.I)
    s = re.sub(r'</?div\s*>', '\n', s, flags=re.I)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = s.replace('&nbsp;', ' ').replace('&amp;', '&')
    s = s.replace('&lt;', '<').replace('&gt;', '>')
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\n[ \t]*\n+', '\n', s)
    return s.strip()


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
    """取正文纯文本。优先 text/plain；只有 HTML 时才转成文本。"""
    if not msg.is_multipart():
        ctype = msg.get_content_type()
        text = _decode_part(msg)
        return _html_to_text(text) if ctype == 'text/html' else text

    plain, html = '', ''
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == 'text/plain' and not plain:
            try:
                plain = _decode_part(part)
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
    log.info('start; imap=%s user=%s folder=%s days=%d receivers=%s dry_run=%s log=%s',
             IMAP_SERVER, IMAP_USER, IMAP_FOLDER, SEARCH_DAYS,
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
    snapshot_add = []
    counters = {'new': 0, 'dup': 0, 'parse_fail': 0, 'send_fail': 0}

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

            all_ok = welink_post_all(content, DEFAULT_RECEIVERS)
            if all_ok:
                counters['new'] += 1
                # DRY_RUN 下 welink_post 直接返回 True，**不能**顺手记进快照 ——
                # 否则「没发」被记成「已发」，联调完接着跑真的那轮会全部判重、一封都不发，
                # 而日志只显示 dup=N。这正是此前 fix/xlb-notify-dryrun-snapshot 修过的坑。
                if not DRY_RUN:
                    snapshot_add.append(message_id)
            else:
                counters['send_fail'] += 1
                log.warning('send failed to some receivers, will retry next round: %s (msgid=%s)',
                            subject, message_id)
        except Exception as e:
            log.error('process mail %s failed: %s: %s', mid.decode(), type(e).__name__, e)
            counters['parse_fail'] += 1

    if snapshot_add:
        now = datetime.now().isoformat()
        # 快照只记 id + 发送时间。早先还捎带了主题，但没有任何代码读它 ——
        # 本文件夹收的是所有外部来信，主题里可能带内部信息，少落一份是一份。
        for sent_id in snapshot_add:
            snapshot.append({'id': sent_id, 'sent_at': now})
        save_snapshot(snapshot)

    # DRY_RUN 下把 new 标成 would_send：否则这行会让人以为真发出去了 N 封
    log.info('done: fetched=%d %s=%d dup=%d parse_fail=%d send_fail=%d snapshot=%d',
             len(ids), 'would_send' if DRY_RUN else 'new', counters['new'],
             counters['dup'], counters['parse_fail'], counters['send_fail'], len(snapshot))

    if counters['parse_fail'] > 0 or counters['send_fail'] > 0:
        welink_post_all(
            f'告警脚本异常: 解析失败 {counters["parse_fail"]} 封, 发送失败 {counters["send_fail"]} 次',
            DEFAULT_RECEIVERS,
        )

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
