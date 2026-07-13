#!/usr/bin/env python3
"""Smoke test: start each of the 104 challenges, wait for healthy, curl 200, stop.

Usage: python3 scripts/smoke_test.py --base http://127.0.0.1:4444 --key xben_xxx
"""
import argparse, json, subprocess, sys, time, urllib.request

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:4444")
    ap.add_argument("--key", required=True)
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    H = {"X-API-Key": args.key, "Content-Type": "application/json"}
    # get challenge list
    req = urllib.request.Request(f"{args.base}/api/challenges", headers=H)
    challs = json.loads(urllib.request.urlopen(req, timeout=30).read())
    built = [c for c in challs if c.get("supported")]
    print(f"Testing {len(built)} challenges...", flush=True)

    ok = fail = skip = 0
    for i, c in enumerate(built):
        b = c["benchmark"]
        # start
        try:
            req = urllib.request.Request(f"{args.base}/api/challenges/{b}/start", method="POST", headers=H, data=b"{}")
            r = json.loads(urllib.request.urlopen(req, timeout=args.timeout).read())
            url = r["url"]
            aid = r["attempt_id"]
        except Exception as e:
            print(f"  [{i+1}/{len(built)}] {b}: START FAIL ({e})", flush=True)
            fail += 1
            continue
        # wait for healthy (poll the reverse-proxy URL)
        healthy = False
        for _ in range(args.timeout // 5):
            try:
                resp = urllib.request.urlopen(url, timeout=10)
                if resp.getcode() < 500:
                    healthy = True
                    break
            except:
                pass
            time.sleep(5)
        # stop
        try:
            req = urllib.request.Request(f"{args.base}/api/challenges/{b}/stop", method="POST", headers=H, data=b"{}")
            urllib.request.urlopen(req, timeout=30).read()
        except:
            pass
        if healthy:
            print(f"  [{i+1}/{len(built)}] {b}: OK", flush=True)
            ok += 1
        else:
            print(f"  [{i+1}/{len(built)}] {b}: UNHEALTHY (url={url})", flush=True)
            fail += 1

    print(f"\n=== SMOKE TEST DONE: ok={ok} fail={fail} skip={skip} (of {len(built)}) ===", flush=True)
    return 0 if fail == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
