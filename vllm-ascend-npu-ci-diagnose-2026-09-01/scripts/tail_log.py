import subprocess, gzip, sys
jid = sys.argv[1]
r = subprocess.run(["gh","api",f"repos/vllm-project/vllm-ascend/actions/jobs/{jid}/logs"],capture_output=True)
log = r.stdout
if log[:2]==b'\x1f\x8b':
    try: log = gzip.decompress(log)
    except Exception: pass
text = log.decode('utf-8', errors='ignore')
lines = [l for l in text.splitlines() if l.strip()]
print(f"总行数 {len(lines)}")
# 打印含错误/失败的尾部 40 行（去时间戳）
import re
cnt=0
for l in lines[-400:]:
    s = re.sub(r'^\S+Z ', '', l)
    s = re.sub(r'\x1b\[[0-9;]*m','',s)
    if re.search(r'(FAILED|Error|error|Traceback|Exception|exit code|E\s+\w|assert|##\[error\])', s) and 'warning' not in s.lower():
        print(s[:160]); cnt+=1
        if cnt>=40: break
if cnt==0:
    print("(尾部 400 行内无 error/failed 行，打印最后 15 行原始)")
    for l in lines[-15:]:
        print(re.sub(r'^\S+Z ','',l)[:160])
