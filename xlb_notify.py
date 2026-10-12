import os
import re
import sys
import json
import imaplib
import email
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
IMAP_SEARCH_DAYS = 3
# DRY_RUN / SINGLE_RECEIVER_TEST 不在这里定值 —— 见下面 CONFIG 加载后那两行

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
# Windows 上配持久环境变量要开「系统属性 → 高级 → 环境变量」或敲 setx，改 JSON 更省事；
# Linux/systemd 上反过来，EnvironmentFile 更顺手。两边都支持，不用二选一。
#
#   XLBN_WELINK_AUTH       必填  小鲁班发送凭据
#   XLBN_IMAP_USER         必填  告警邮箱账号
#   XLBN_IMAP_PASSWORD     必填  告警邮箱密码
#   XLBN_ALERT_SENDER      必填  告警邮件来源地址（IMAP 只在 FROM 里搜它）。
#                                多个用逗号分隔 —— 当前有两条链路：backlog 的
#                                workflow-analysis 与昇腾失败分析，各自用不同的 QQ 账号发信。
#   XLBN_DEFAULT_RECEIVER  必填  兜底收件人
#   XLBN_ALERT_ROUTES      选填  JSON：[[["Runner"], ["z123", "l456"]], ...]，顺序即优先级
#                                （关键词也可写成裸字符串：["Runner", [...]]）
#   XLBN_WELINK_URL / XLBN_IMAP_SERVER / XLBN_IMAP_FOLDER  选填（服务端点，用默认值）
#   XLBN_DRY_RUN           选填  设 1/true 则只打日志不发 WeLink（首次联调用）

REQUIRED_ENV = (
    ('XLBN_WELINK_AUTH', 'welink_auth'),
    ('XLBN_IMAP_USER', 'imap_user'),
    ('XLBN_IMAP_PASSWORD', 'imap_password'),
    ('XLBN_ALERT_SENDER', 'alert_sender'),
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


def _parse_routes(raw):
    """路由表从环境变量或配置文件读 —— 表里全是同事工号，而本仓是公开的，故不入代码。

    **返回顺序即优先级**（`get_receivers` 首个命中即返回），所以 JSON 里的顺序必须与原来
    一致。注意原来那条 `([''], [...])` 是靠「空串是任何字符串的子串」充当兜底的，已不再
    需要：`get_receivers` 末尾的 `return [DEFAULT_RECEIVER]` 就是兜底，且挪不错位置。

    raw 既可以是 JSON 字符串（环境变量），也可以是已解析好的列表（配置文件）——
    在 JSON 文件里把路由表再写成一层转义字符串实在太难写对了。
    """
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise SystemExit('XLBN_ALERT_ROUTES 不是合法 JSON：%s' % exc)
    else:
        parsed = raw
    if not isinstance(parsed, list):
        raise SystemExit('XLBN_ALERT_ROUTES 必须是列表：[[关键词...], [收件人...]]')
    routes = []
    for entry in parsed:
        if not isinstance(entry, list) or len(entry) != 2:
            raise SystemExit('XLBN_ALERT_ROUTES 每项必须是 [关键词, 收件人列表]：%r' % (entry,))
        keywords, receivers = entry[0], entry[1]
        # 单个关键词允许写成裸字符串，省得为双层方括号（关键词也要包一层 list）踩坑
        if isinstance(keywords, str):
            keywords = [keywords]
        if not isinstance(keywords, list) or not isinstance(receivers, list):
            raise SystemExit('XLBN_ALERT_ROUTES 每项必须是 [关键词列表, 收件人列表]：%r' % (entry,))
        routes.append((list(keywords), list(receivers)))
    return routes


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
    # 路由表：环境变量是 JSON 字符串，配置文件里是已解析的列表，两者都交给 _parse_routes
    routes_raw = os.environ.get('XLBN_ALERT_ROUTES', '').strip() or FILE_CONFIG.get('alert_routes') or ''
    config['alert_routes'] = _parse_routes(routes_raw)
    config['dry_run'] = _setting('XLBN_DRY_RUN', 'dry_run').lower() in ('1', 'true', 'yes', 'on')
    config['single_receiver_test'] = str(
        FILE_CONFIG.get('single_receiver_test', '') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    missing = [name for name, key in REQUIRED_ENV if not config[key]]
    return config, missing


CONFIG, MISSING_CONFIG = load_config()

WELINK_URL = CONFIG['welink_url']
WELINK_AUTH = CONFIG['welink_auth']
DEFAULT_RECEIVER = CONFIG['default_receiver']
ALERT_SENDER = CONFIG['alert_sender']
# 发件人白名单。逗号/分号分隔多个 —— 两条链路各自用不同 QQ 账号发信时，
# 只填一个会让另一条的邮件被 IMAP 搜不到：`FROM <string>` 按 RFC 3501 是**子串**匹配，
# 而 `1079854335@qq.com` 与 `574451426@qq.com` 互不为子串，所以只写前者搜不到后者。
# 单个值的行为与加这层之前逐字节一致（已实测）。
ALERT_SENDERS = [s.strip() for s in re.split(r'[,;]', ALERT_SENDER) if s.strip()]
IMAP_SERVER = CONFIG['imap_server']
IMAP_USER = CONFIG['imap_user']
IMAP_PASSWORD = CONFIG['imap_password']
IMAP_FOLDER = CONFIG['imap_folder']
ALERT_ROUTES = CONFIG['alert_routes']

# 两个开关也支持从配置文件/环境变量读 —— 首次联调时不必改代码（改代码就有顺手 commit 的风险）
DRY_RUN = CONFIG['dry_run']
SINGLE_RECEIVER_TEST = CONFIG['single_receiver_test']

SKIP_ALERTS = ('Watchdog', 'CloudAccount', 'CloudAccountUnreachable')
SKIP_DESCRIPTION_KEYWORDS = ('BSS 账号', '云账号', 'AK/SK')
ALERT_SUBJECT_KEYWORDS = ('FIRING', 'RESOLVED', 'CRITICAL', 'WARNING', 'INFO', 'RECOVERED')


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


def get_receivers(alertname):
    if SINGLE_RECEIVER_TEST:
        return [DEFAULT_RECEIVER]
    for keywords, receivers in ALERT_ROUTES:
        if any(kw in alertname for kw in keywords):
            return receivers
    return [DEFAULT_RECEIVER]


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


def parse_alert(subject, body):
    info = {'alertname': 'Unknown', 'cluster': 'unknown', 'severity': 'info',
            'status': 'firing', 'description': '', 'action': '', 'runbook': ''}

    m = re.match(r'\[(\w+)\]\s*\[([^\]]+)\]\s*(.+)', subject)
    if m:
        info['severity'] = m.group(1).lower()
        info['cluster'] = m.group(2).strip()
        info['alertname'] = m.group(3).strip()
        if 'resolved' in subject.lower() or 'recovered' in subject.lower():
            info['status'] = 'resolved'

    lines = [l.strip() for l in body.split('\n') if l.strip()]
    label_map = {'集群': 'cluster', '告警': 'alias', '状态': 'status',
                 '描述': 'description', '修复建议': 'action'}
    section_titles = {'告警详情', '修复建议', '恢复详情'}
    i = 0
    while i < len(lines):
        line = lines[i]
        if line == '描述' and i + 1 < len(lines):
            desc_parts = []
            j = i + 1
            while j < len(lines) and lines[j] not in label_map and lines[j] not in section_titles:
                v = lines[j].strip().strip('|').strip()
                if v and 'http' not in v and len(v) > 5:
                    desc_parts.append(v)
                j += 1
            if desc_parts:
                d = ' | '.join(desc_parts)
                info['description'] = d if not info['description'] else info['description'] + ' | ' + d
            i = j
            continue
        if line in label_map and i + 1 < len(lines):
            nxt = lines[i + 1].strip().strip('|').strip()
            if nxt and nxt not in label_map and nxt not in section_titles \
                    and 'http' not in nxt and len(nxt) > 1:
                key = label_map[line]
                if key == 'alias':
                    info['alertname'] = nxt
                elif key == 'cluster':
                    info['cluster'] = nxt
                else:
                    info[key] = nxt
            i += 2
        else:
            i += 1

    mm = re.search(r'https?://[^\s)]+', body)
    if mm:
        info['runbook'] = mm.group(0).rstrip('.,)>').rstrip('"')

    return info


def format_message(info):
    if info['status'] == 'resolved':
        return f"【RESOLVED】{info['alertname']}\n集群: {info['cluster']}\n告警已恢复"
    msg = f"【{info['severity'].upper()}】{info['alertname']}\n集群: {info['cluster']}"
    if info['description']:
        msg += f"\n{info['description']}"
    if info['action']:
        msg += f"\n建议: {info['action']}"
    if info['runbook']:
        msg += f"\nRunbook: {info['runbook']}"
    return msg


def _html_to_text(raw):
    if not raw:
        return ''
    s = re.sub(r'<style[^>]*>.*?</style>', '', raw, flags=re.I | re.S)
    s = re.sub(r'<script[^>]*>.*?</script>', '', s, flags=re.I | re.S)
    s = re.sub(r'<a[^>]+href="([^"]+)"[^>]*>([^<]*)</a>', r' \2 (\1) ', s, flags=re.I)
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


def should_skip(info):
    if any(s in info['alertname'] for s in SKIP_ALERTS):
        return True
    if any(k in info.get('description', '') for k in SKIP_DESCRIPTION_KEYWORDS):
        return True
    return False


def is_alert_subject(subject):
    return any(k in subject for k in ALERT_SUBJECT_KEYWORDS)


def _sender_search(senders):
    """把多个发件人拼成 IMAP 的 OR 表达式。

    OR 只接受两个操作数，所以 N 个发件人要靠右嵌套：
        OR FROM "a" OR FROM "b" FROM "c"
    最外层 OR 吃 `FROM "a"` 和「剩下的整个 OR」，最后一个发件人是内层第二操作数。
    实测这串里的分隔符不能少：`OR`、关键字、字面量之间必须有空格。
    """
    expression = 'FROM "%s"' % senders[-1]
    for sender in reversed(senders[:-1]):
        expression = 'OR FROM "%s" %s' % (sender, expression)
    return expression


def fetch_alert_emails(mail):
    since = (datetime.now() - timedelta(days=IMAP_SEARCH_DAYS)).strftime('%d-%b-%Y')
    # SENTSINCE 与前面那串 OR 之间是隐式 AND（IMAP 里并列的搜索键就是 AND），
    # 这样能写成单层括号，不必给 OR 再套一层。
    criteria = '(%s SENTSINCE "%s")' % (_sender_search(ALERT_SENDERS), since)
    status, data = mail.search(None, criteria)
    if status != 'OK' or not data or not data[0]:
        return []
    return data[0].split()


def fetch_email_bytes(mail, mid):
    status, data = mail.fetch(mid, '(RFC822)')
    if status != 'OK' or not data or not data[0]:
        return None
    return data[0][1] if len(data[0]) > 1 else None


def imap_connect():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, timeout=15)
    mail.login(IMAP_USER, IMAP_PASSWORD)
    mail.select(IMAP_FOLDER)
    return mail


def process():
    log.info('=' * 60)
    log.info('start; senders=%s folder=%s dry_run=%s log=%s',
             ALERT_SENDERS, IMAP_FOLDER, DRY_RUN, LOG_FILE)
    mail = None
    try:
        mail = imap_connect()
        ids = fetch_alert_emails(mail)
        log.info('fetched %d emails in last %d days', len(ids), IMAP_SEARCH_DAYS)
    except Exception as e:
        log.error('imap connect/search failed: %s: %s', type(e).__name__, e)
        welink_post(f'告警脚本连接失败: {type(e).__name__}: {e}', DEFAULT_RECEIVER)
        return

    snapshot = load_snapshot()
    sent_ids = {e['id'] for e in snapshot}
    snapshot_add = []
    counters = {'new': 0, 'skip': 0, 'dup': 0, 'parse_fail': 0, 'send_fail': 0, 'non_alert': 0}

    for mid in ids:
        try:
            raw = fetch_email_bytes(mail, mid)
            if not raw:
                counters['parse_fail'] += 1
                continue

            msg = email.message_from_bytes(raw)
            message_id = msg.get('Message-ID') or mid.decode()
            if message_id in sent_ids:
                counters['dup'] += 1
                continue

            subject = ''
            raw_subject = msg.get('Subject', '')
            if raw_subject:
                try:
                    subject = ''.join(
                        p.decode(c or 'utf-8', errors='replace') if isinstance(p, bytes) else p
                        for p, c in decode_header(raw_subject)
                    )
                except Exception:
                    pass

            if not is_alert_subject(subject):
                counters['non_alert'] += 1
                continue

            info = parse_alert(subject, get_email_body(msg))

            if should_skip(info):
                log.info('skip: %s', info['alertname'])
                counters['skip'] += 1
                snapshot_add.append((message_id, info['alertname']))
                continue

            receivers = get_receivers(info['alertname'])
            log.info('alert=%s severity=%s receivers=%s',
                     info['alertname'], info['severity'], receivers)
            all_ok = True
            for receiver in receivers:
                if not welink_post(format_message(info), receiver):
                    all_ok = False

            if all_ok:
                counters['new'] += 1
                snapshot_add.append((message_id, info['alertname']))
            else:
                counters['send_fail'] += 1
                log.warning('send failed to some receivers, will retry next round: %s (msgid=%s)',
                            info['alertname'], message_id)
        except Exception as e:
            log.error('process mail %s failed: %s: %s', mid.decode(), type(e).__name__, e)
            counters['parse_fail'] += 1

    if snapshot_add:
        now = datetime.now().isoformat()
        for mid, _name in snapshot_add:
            snapshot.append({'id': mid, 'sent_at': now})
        save_snapshot(snapshot)

    log.info('done: fetched=%d new=%d skip=%d dup=%d non_alert=%d parse_fail=%d send_fail=%d snapshot=%d',
             len(ids), counters['new'], counters['skip'], counters['dup'],
             counters['non_alert'], counters['parse_fail'], counters['send_fail'],
             len(snapshot))

    if counters['parse_fail'] > 0 or counters['send_fail'] > 0:
        welink_post(
            f'告警脚本异常: 解析失败 {counters["parse_fail"]} 封, 发送失败 {counters["send_fail"]} 次',
            DEFAULT_RECEIVER,
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
        welink_post(f'告警脚本崩溃: {type(e).__name__}: {str(e)[:200]}', DEFAULT_RECEIVER)
