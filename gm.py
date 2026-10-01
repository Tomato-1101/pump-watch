"""gmgn CLI を 429 対応で叩く共通ヘルパー。"""
import json, re, subprocess, sys, time

def call(args, retries=8):
    for i in range(retries):
        p = subprocess.run(["gmgn", *args, "--raw"], capture_output=True, text=True)
        out = p.stdout.strip()
        if p.returncode == 0 and out.startswith(("{", "[")):
            try:
                return json.loads(out)
            except json.JSONDecodeError:
                pass
        err = (p.stderr + out)
        m = re.search(r"~(\d+)s remaining", err)
        wait = int(m.group(1)) + 3 if m else 20
        print(f"  [retry {i+1}] wait {wait}s: {err[:160]}", file=sys.stderr, flush=True)
        time.sleep(wait)
    raise RuntimeError("gmgn call failed: " + " ".join(args))
