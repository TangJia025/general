#!/usr/bin/env python3
"""xlb_notify.py 部署前自检 —— 单独验证 IMAP 这一跳，把「配置错」与「脚本逻辑错」分开。

只读：不发 WeLink、不写快照、不改任何状态。读的是 xlb_notify.py 同一个 config.json。

用法（放在 xlb_notify.py 同目录）：
    python xlb_probe.py

它会回答五个问题：
    1. 这个账号能不能登进这个 IMAP 服务器
    2. 这个账号下都有哪些文件夹 —— 列出的是 IMAP 原始名，也就是 imap_folder 该填的字面值
    3. 目标文件夹能不能选中，里面共多少封邮件
    4. 每个发件人，把搜索条件**逐级放宽**各能搜到几封 —— 用来定位是哪一级条件把信筛掉的
    5. 生产条件搜不到时，把所有文件夹都翻一遍，直接回答「信到底在哪」

第 5 条是关键：它能区分「发件人地址填错」和「信落在别的文件夹」。
"""

import imaplib
import json
import os
import re
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'xlb_notify.config.json')

# 搜索窗天数。与 xlb_notify.py 的 IMAP_SEARCH_DAYS 默认值一致。
SEARCH_DAYS = 7


def _count(mail, criteria):
    """跑一次 SEARCH，返回命中封数。异常与空响应一律按 0 处理。"""
    try:
        status, data = mail.search(None, criteria)
    except Exception as error:
        print('          （SEARCH 抛异常：%s: %s）' % (type(error).__name__, error))
        return 0
    if status != 'OK' or not data or not data[0]:
        return 0
    return len(data[0].split())


def _folder_names(mail):
    """从 LIST 响应里抠出文件夹名。返回的就是能直接喂给 select() 的字面值。"""
    try:
        status, boxes = mail.list()
    except Exception:
        return []
    if status != 'OK' or not boxes:
        return []
    names = []
    for raw in boxes:
        text = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw)
        # 形如：(\HasNoChildren) "/" "INBOX/&WRaQ6JCuTvY-"  —— 取末尾那个引号串
        match = re.search(r'"([^"]*)"\s*$', text)
        if match:
            names.append(match.group(1))
    return names


def _scan_all_folders(mail, senders, since, original_folder):
    """把每个文件夹都翻一遍，报出这几个发件人的信到底在哪个文件夹。

    这里**只按发件人搜、不带日期** —— 本步要回答的是「信在哪」，不是「最近 7 天在哪」。
    若也带上日期，比窗口老的信用一样搜不到，最后那句「不在这台账号下」就成了假结论。
    命中后再补一次带日期的计数，标出它在不在搜索窗内。
    """
    names = _folder_names(mail)
    if not names:
        print('    （列不出文件夹，跳过）')
        return
    print('    共 %d 个文件夹，逐个搜（本步只按发件人，不带日期）：' % len(names))
    hits = []
    for name in names:
        try:
            status, _ = mail.select(name)
        except Exception:
            continue
        if status != 'OK':
            continue
        for sender in senders:
            any_time = _count(mail, '(FROM "%s")' % sender)
            if not any_time:
                continue
            recent = _count(mail, '(FROM "%s" SENTSINCE "%s")' % (sender, since))
            hits.append((name, sender, recent, any_time))
    if hits:
        for name, sender, recent, any_time in hits:
            if recent:
                mark = '✅ 在窗口内'
            else:
                mark = '⏳ 比窗口老（%d 封都在 %s 之前）' % (any_time, since)
            print('    %-32s %-26s %d 封  %s' % (name, sender, any_time, mark))
    else:
        print('    所有文件夹、所有时间都翻过了，一封都没有。')
        print('    → 这几个发件人的信确实不在这台 IMAP 账号下：要么登错了账号，要么落在了别的邮箱。')
    # 翻完把工作文件夹选回去，免得调用方以为还在原地
    try:
        mail.select(original_folder)
    except Exception:
        pass


def main():
    if not os.path.exists(CONFIG_FILE):
        sys.exit('找不到 %s\n先把 xlb_notify.config.example.json 拷成它并填值。' % CONFIG_FILE)

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as handle:
            config = json.load(handle)
    except ValueError as error:
        # 中文 Windows 的记事本默认存 ANSI(GBK)，那样存出来的文件会在这一步报错
        sys.exit('读取 %s 失败：%s\n（若用记事本编辑过，确认另存时编码选的是 UTF-8）'
                 % (CONFIG_FILE, error))

    server = str(config.get('imap_server', '') or '')
    user = str(config.get('imap_user', '') or '')
    password = str(config.get('imap_password', '') or '')
    folder = str(config.get('imap_folder', '') or '')
    raw_senders = str(config.get('alert_sender', '') or '').replace(';', ',')

    print('服务器 : %s' % server)
    print('账号   : %s' % user)
    print('文件夹 : %s' % folder)
    print('-' * 64)

    # 先拦住明文中文的文件夹名：真 imaplib 会在 _command 里按 ascii 编码而抛 UnicodeEncodeError。
    # 这是最常见的配置错，放到连服务器之前报，别让它变成一个看不懂的编码异常。
    if any(ord(ch) > 127 for ch in folder):
        sys.exit('imap_folder 含非 ASCII 字符：%r\n'
                 'IMAP 文件夹名必须写**修改版 UTF-7**，不能写明文中文。'
                 '「外部邮件」应写 INBOX/&WRaQ6JCuTvY-\n'
                 '（下面本来会列出该账号的真实文件夹名，但连不上就没得列 —— 先改对这一项。）'
                 % folder)

    try:
        mail = imaplib.IMAP4_SSL(server, timeout=15)
    except Exception as error:
        sys.exit('连不上 %s：%s: %s\n（内网通不通？主机名对不对？）'
                 % (server, type(error).__name__, error))

    try:
        mail.login(user, password)
    except imaplib.IMAP4.error as error:
        sys.exit('登录失败：%s\n（账号或密码不对。账号要填能登进中间邮箱的那个，'
                 '可能是工号也可能是别名）' % error)
    print('登录成功')

    print()
    print('该账号下的文件夹（右边这串就是 imap_folder 该填的字面值）：')
    for name in _folder_names(mail):
        print('    %s' % name)

    print()
    try:
        status, data = mail.select(folder)
    except Exception as error:
        sys.exit('select %r 抛异常：%s\n（文件夹名必须是修改版 UTF-7，例如 INBOX/&WRaQ6JCuTvY-）'
                 % (folder, error))
    print('select %r -> %s' % (folder, status))
    if status != 'OK':
        sys.exit('选不到这个文件夹 —— 照上面列出的名字改 imap_folder。')
    total = data[0].decode() if data and data[0] else '?'
    print('该文件夹共 %s 封邮件' % total)

    since = (datetime.now() - timedelta(days=SEARCH_DAYS)).strftime('%d-%b-%Y')
    senders = [s.strip() for s in raw_senders.split(',') if s.strip()]
    print()
    print('按发件人搜，条件逐级放宽（SENTSINCE %s）：' % since)
    if not senders:
        print('    （alert_sender 是空的 —— 配置没填）')
    empty_senders = []
    for sender in senders:
        print('  %s' % sender)
        production_count = 0
        for label, criteria in (
            ('FROM + SENTSINCE  ← 生产用的条件', '(FROM "%s" SENTSINCE "%s")' % (sender, since)),
            ('只 FROM（去掉日期）', '(FROM "%s")' % sender),
            ('只 SENTSINCE（去掉发件人）', '(SENTSINCE "%s")' % since),
        ):
            count = _count(mail, criteria)
            print('      %-34s %d 封' % (label, count))
            if label.startswith('FROM +'):
                production_count = count
        if production_count == 0:
            empty_senders.append(sender)

    # 生产条件搜不到时，把所有文件夹翻一遍 —— 直接回答「信到底在哪」
    if empty_senders:
        print()
        print('以下发件人在当前文件夹搜不到，改为逐个文件夹找：')
        print('  %s' % ', '.join(empty_senders))
        _scan_all_folders(mail, empty_senders, since, folder)

    try:
        mail.logout()
    except Exception:
        pass

    print()
    if not senders:
        print('⚠️ alert_sender 没填，等于没有发件人要搜 —— 上面那些 0 封全是空转出来的。')
        return
    if not empty_senders:
        print('✅ 所有发件人在 %s 都能搜到，IMAP 这一跳是通的。' % folder)
        return
    print('⚠️ 这些发件人的信，生产条件搜不到：%s' % ', '.join(empty_senders))
    print('   （生产条件 = FROM + SENTSINCE，跑起来就是拿它去搜的）')
    print('   对着上面「逐级放宽」那一组看是哪一级筛掉的：')
    print('     只 FROM 有、带日期没有  → 信在，但比搜索窗还老（SENTSINCE 只精确到天）')
    print('     只 FROM 也是 0          → 这个地址在目标文件夹里根本没来过信，')
    print('                               看上面「逐个文件夹找」的结果确认它在不在别的文件夹')


if __name__ == '__main__':
    main()
