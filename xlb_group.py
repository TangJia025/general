import requests
import sys

def send_msg(content, receiver, sender=None):
    url = 'http://xiaoluban.rnd.huawei.com:80/'
    data = {'content': content, 'receiver': receiver, 'auth': auth}
    if sender:
        data['sender'] = sender
    res = requests.post(url=url, json=data)
    if not res.ok:
        print(res.text)

if __name__ == '__main__':
    auth = '' # 你跟小鲁班申请的token
    receiver_uid = '' # 你要发送的群id

    if len(sys.argv) < 2:
        print("please input para: done or confirm")
        sys.exit(1)

    action = sys.argv[1]

    if action == 'done':
        message = '你的任务已完成，亲'
    elif action == 'confirm':
        message = '你的任务需要你介入了，亲'
    else:
        message = '你好'

    send_msg(message, receiver_uid)
