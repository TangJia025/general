import subprocess, json, re, gzip
def gh(*a, binary=False):
    r = subprocess.run(["gh","api",*a],capture_output=True)
    return r.stdout if binary else r.stdout.decode(errors="ignore")
runs = [l.split("\t")[1] for l in open("/tmp/sample_runs.tsv").read().splitlines() if l]
SIGS = [
    (r'EngineDeadError|EngineCore encountered a fatal error|RuntimeError: cancelled', "ENGINE_DEAD"),
    (r'shm_broadcast[^\n]{0,60}60 seconds', "SHM_BROADCAST_TIMEOUT"),
    (r'aisbench cases failed', "AISBENCH_ASSERT"),
    (r'Acceptance length regression', "ACCEPTANCE_REGRESSION"),
    (r'ERR99999|error code is 507\d{3}|NPU function error', "NPU_HW_ACL"),
    (r'exit code 137|SIGKILL|Killed|OutOfMemoryError|NPU out of memory|DeviceSideOutOfMemory', "OOM_KILL"),
    (r'httpcore\.(ConnectError|ReadError)|httpx\.(ConnectError|ReadError)|Connection refused|Connection reset|RemoteProtocolError', "NET_CONNECT"),
    (r'No available shared memory broadcast', "SHM_NO_BLOCK"),
    (r'Failed to fetch PR title from GitHub API|not our ref|upload-pack', "GIT_REF"),
    (r'Selected tests are required', "LABEL_GATE"),
    (r'Mypy|mypy', "MYPY"),
    (r'AssertionError', "ASSERT"),
]
def classify(text):
    for pat, name in SIGS:
        if re.search(pat, text, re.I):
            return name
    return "OTHER"
out = []
for run in runs:
    try:
        jobs = json.loads(gh(f"repos/vllm-project/vllm-ascend/actions/runs/{run}/jobs?per_page=100"))['jobs']
    except Exception:
        continue
    for j in jobs:
        if j['conclusion'] != 'failure':
            continue
        labels = j.get('labels') or []
        is_npu = any(re.search(r'linux-(?:aarch64|amd64)-(?:a\d[\w-]*|310p|nightly)[\w-]*-\d', l) for l in labels)
        log = gh(f"repos/vllm-project/vllm-ascend/actions/jobs/{j['id']}/logs", binary=True)
        if not log: continue
        if log[:2]==b'\x1f\x8b':
            try: log = gzip.decompress(log)
            except Exception: pass
        text = log.decode('utf-8', errors='ignore')
        # 只取失败 step 之后的尾部（失败 step 通常是最后几个）
        lines = text.splitlines()
        tail = "\n".join(lines[-1500:])
        cls = classify(tail)
        out.append((run, j['id'], "NPU" if is_npu else "cpu", j['name'][:40], cls))
for run,jid,tag,name,cls in out:
    print(f"{run} [{tag}] {cls:22s} {name}")
from collections import Counter
print("\n=== 分布 ===")
for k,v in Counter(c[4] for c in out).most_common():
    print(f"  {v:3d}  {k}")
print(f"总失败job {len(out)}，其中 NPU {sum(1 for c in out if c[2]=='NPU')} / CPU {sum(1 for c in out if c[2]=='cpu')}")
