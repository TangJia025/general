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

SNAPSHOT_TTL_DAYS = 7
IMAP_SEARCH_DAYS = 3
DRY_RUN = False

WELINK_URL = 'http://xiaoluban.rnd.huawei.com:80/'
WELINK_AUTH = '***REMOVED***'
DEFAULT_RECEIVER = '***REMOVED***'

ALERT_SENDER = '***REMOVED***'
IMAP_SERVER = 'imailcn56.email.huawei.com'
IMAP_USER = '***REMOVED***'
IMAP_PASSWORD = '***REMOVED***'
IMAP_FOLDER = 'INBOX/&WRaQ6JCuTvY-'

SKIP_ALERTS = ('Watchdog', 'CloudAccount', 'CloudAccountUnreachable')
SKIP_DESCRIPTION_KEYWORDS = ('BSS 账号', '云账号', 'AK/SK')
ALERT_SUBJECT_KEYWORDS = ('FIRING', 'RESOLVED', 'CRITICAL', 'WARNING', 'INFO', 'RECOVERED')

ALERT_ROUTES = [
    (['Runner'],         ['***REMOVED***', '***REMOVED***', '***REMOVED***']),
    (['NodeFilesystem'], ['***REMOVED***', '***REMOVED***', '***REMOVED***', '***REMOVED***']),
    (['NPUCard'],        ['***REMOVED***', '***REMOVED***', '***REMOVED***', '***REMOVED***']),
    (['Listener'],       ['***REMOVED***', '***REMOVED***', '***REMOVED***', '***REMOVED***']),
    ([''],               ['***REMOVED***', '***REMOVED***', '***REMOVED***', '***REMOVED***']),
]

SINGLE_RECEIVER_TEST = False


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


def fetch_alert_emails(mail):
    since = (datetime.now() - timedelta(days=IMAP_SEARCH_DAYS)).strftime('%d-%b-%Y')
    status, data = mail.search(None, f'(FROM "{ALERT_SENDER}" SENTSINCE "{since}")')
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
    log.info('start; sender=%s folder=%s dry_run=%s log=%s',
             ALERT_SENDER, IMAP_FOLDER, DRY_RUN, LOG_FILE)
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
    try:
        process()
    except Exception as e:
        log.exception('script crashed')
        welink_post(f'告警脚本崩溃: {type(e).__name__}: {str(e)[:200]}', DEFAULT_RECEIVER)
