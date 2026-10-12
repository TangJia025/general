#!/usr/bin/env python3
"""xlb_notify.py 部署前自检 —— 单独验证 IMAP 这一跳，把「配置错」与「脚本逻辑错」分开。

只读：不发 WeLink、不写快照、不改任何状态。读的是 xlb_notify.py 同一个 config.json。

用法（放在 xlb_notify.py 同目录）：
    python xlb_probe.py

它会回答四个问题：
    1. 这个账号能不能登进这个 IMAP 服务器
    2. 这个账号下都有哪些文件夹 —— 列出的是 IMAP 原始名，也就是 imap_folder 该填的字面值
    3. 目标文件夹能不能选中，里面共多少封邮件
    4. 每个发件人最近 7 天各自能搜到几封

第 4 条是重点：某个发件人是 0 封，就说明那一条链路在这个邮箱里找不到信。
"""

import imaplib
import json
import os
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'xlb_notify.config.json')


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
    try:
        status, boxes = mail.list()
    except Exception as error:
        status, boxes = 'NO', []
        print('    列文件夹失败：%s' % error)
    if status == 'OK' and boxes:
        for raw in boxes:
            text = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw)
            print('   ', text)
    else:
        print('    （列不出来，可能是服务器不支持 LIST）')

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

    since = (datetime.now() - timedelta(days=7)).strftime('%d-%b-%Y')
    senders = [s.strip() for s in raw_senders.split(',') if s.strip()]
    print()
    print('按发件人搜最近 7 天（SENTSINCE %s）：' % since)
    if not senders:
        print('    （alert_sender 是空的）')
    for sender in senders:
        status, data = mail.search(None, '(FROM "%s" SENTSINCE "%s")' % (sender, since))
        count = len(data[0].split()) if status == 'OK' and data and data[0] else 0
        print('    %-30s %d 封' % (sender, count))

    try:
        mail.logout()
    except Exception:
        pass

    print()
    print('自检结束。哪个发件人是 0 封，那一条链路在这个邮箱里就找不到信 ——')
    print('要么发件人地址填错，要么那封信落在了别的文件夹。')


if __name__ == '__main__':
    main()
