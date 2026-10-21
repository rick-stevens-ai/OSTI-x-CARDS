#!/usr/bin/env python3
"""Watch pecans uncovered+year card dirs; run sync_cards.py each time real-card count grows by >=1000.
Keeps the website live-updated per batch. Idempotent, restartable."""
import subprocess, time, os, sys, re

SITE = os.path.expanduser("~/code/osti-cards-site")
STATE = os.path.expanduser("~/code/osti-cards-site/.last_synced_count")
BATCH = 5000

def real_card_count_on_pecans():
    """Count real (pass) cards from the ledgers via a clean counter script on pecans. Fast."""
    cmd = "cesh pecans 'python3 /home/stevens/count_pass.py' 2>/dev/null | grep -v cesello:"
    try:
        out = subprocess.run(["bash","-lc",cmd], capture_output=True, text=True, timeout=120).stdout
        for line in out.splitlines():
            line=line.strip()
            if line.isdigit():
                return int(line)
    except Exception as e:
        print(f"[watch] count err: {e}", flush=True)
    return None

def run_sync():
    print(f"[watch] {time.strftime('%H:%M:%S')} running sync_cards.py ...", flush=True)
    py = os.path.join(SITE, ".venv/bin/python")
    r = subprocess.run([py, os.path.join(SITE,"sync_cards.py")],
                       capture_output=True, text=True, timeout=5400)
    tail = "\n".join(r.stdout.splitlines()[-6:])
    print(f"[watch] sync done rc={r.returncode}\n{tail}", flush=True)
    if r.returncode != 0:
        print(f"[watch] sync STDERR: {r.stderr[-400:]}", flush=True)

def main():
    last = 0
    if os.path.exists(STATE):
        try: last = int(open(STATE).read().strip())
        except: last = 0
    print(f"[watch] start, last_synced={last}", flush=True)
    while True:
        n = real_card_count_on_pecans()
        if n is None:
            time.sleep(120); continue
        print(f"[watch] {time.strftime('%H:%M:%S')} pecans real cards={n} (last synced {last}, +{n-last})", flush=True)
        if n - last >= BATCH:
            run_sync()
            last = n
            open(STATE,"w").write(str(last))
        time.sleep(180)  # poll every 3 min

if __name__ == "__main__":
    main()
