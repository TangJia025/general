import subprocess, json, re, gzip
def gh(*a, binary=False):
    r = subprocess.run(["gh","api",*a],capture_output=True)
    return r.stdout if binary else r.stdout.decode(errors="ignore")
# 从上一步结果挑 OTHER 的 job
lines = [l for l in open("/tmp/classify_result.txt").read().splitlines() if " OTHER " in l]
pairs = []
for l in lines:
    parts = l.split()
    run, jid = parts[0], parts[1]
    pairs.append((run, jid, l))
# 每个 job 提取关键错误行（去重、去时间戳）
seen = set()
for run, jid, l in pairs:
    log = gh(f"repos/vllm-project/vllm-ascend/actions/jobs/{jid}/logs", binary=True)
    if not log: continue
    if log[:2]==b'\x1f\x8b':
        try: log = gzip.decompress(log)
        except Exception: continue
    text = log.decode('utf-8', errors='ignore')
    # 找失败相关行
    hits = []
    for line in text.splitlines():
        s = re.sub(r'^[\dT:.\-]+Z? ?', '', line)
        if re.search(r'(error|Error|ERROR|failed|FAILED|exit code|Traceback|Exception|assert|E )', s) and not re.search(r'warning|##\[group\]|##\[endgroup\]', s):
            s2 = re.sub(r'\x1b\[[0-9;]*m','',s).strip()[:130]
            if s2 and s2 not in seen:
                hits.append(s2)
        if len(hits)>=3: break
    seen.update(hits)
    print(f"### {run} {jid} {l.split('] ')[-1][:45]}")
    for h in hits: print("   ", h)
    print()
