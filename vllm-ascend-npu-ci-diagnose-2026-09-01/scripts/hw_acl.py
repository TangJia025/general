import subprocess, json, re, gzip
def gh(*a, binary=False):
    r = subprocess.run(["gh","api",*a],capture_output=True)
    return r.stdout if binary else r.stdout.decode(errors="ignore")
runs = [l.split("\t")[1] for l in open("/tmp/sample_runs.tsv").read().splitlines() if l]
found = {}
for run in runs:
    try: jobs = json.loads(gh(f"repos/vllm-project/vllm-ascend/actions/runs/{run}/jobs?per_page=100"))['jobs']
    except Exception: continue
    for j in jobs:
        if j['conclusion']!='failure': continue
        log = gh(f"repos/vllm-project/vllm-ascend/actions/jobs/{j['id']}/logs", binary=True)
        if not log: continue
        if log[:2]==b'\x1f\x8b':
            try: log=gzip.decompress(log)
            except Exception: continue
        text = log.decode('utf-8', errors='ignore')
        m = re.search(r'(ERR\d{5}|error code is \d{6}|NPU function error[^\n]{0,80}|HCCL\w*[^\n]{0,60})', text)
        if m and run not in found:
            found[run] = (j['name'][:35], re.sub(r'\x1b\[[0-9;]*m','',m.group(0))[:110])
for run,(n,sig) in found.items():
    print(f"{run}  {n}  →  {sig}")
