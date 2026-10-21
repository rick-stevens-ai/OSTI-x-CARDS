#!/usr/bin/env python3
"""
enrich_pipeline.py — self-driving orchestrator for the enrichment + SuperCards pipeline.

Runs the dependency chain, each step gated on its input existing, idempotent, resumable.
Designed to be re-invoked repeatedly (cron/loop) through a long autonomy window: it
checks what's done and advances the next incomplete step, then exits.

Chain:
  1. entity_resolve.py        -> data/entities.sqlite
  2. supercards_build.py      -> data/supercards.sqlite
  3. enrich_crosscard.py      -> data/card_enrichment.sqlite (cross-card field fills)
  (emails are applied separately by enrich_apply.py when the JSONL lands)

Writes a state file data/_pipeline_state.json with per-step status + timestamps.
"""
import os, subprocess, json, time, sys

BASE = os.path.expanduser("~/code/osti-cards-site")
PY   = os.path.join(BASE, ".venv/bin/python")
DATA = os.path.join(BASE, "data")
STATE= os.path.join(DATA, "_pipeline_state.json")

STEPS = [
    ("entity_resolve",  "entity_resolve.py",   os.path.join(DATA,"entities.sqlite")),
    ("supercards_build","supercards_build.py", os.path.join(DATA,"supercards.sqlite")),
    ("enrich_crosscard","enrich_crosscard.py", os.path.join(DATA,"card_enrichment.sqlite")),
]

def load_state():
    return json.load(open(STATE)) if os.path.exists(STATE) else {}

def save_state(s):
    json.dump(s, open(STATE,"w"), indent=2)

def run_step(name, script):
    log=os.path.join(DATA, f"_{name}.log")
    t0=time.time()
    with open(log,"w") as lf:
        r=subprocess.run([PY, os.path.join(BASE,script)], stdout=lf, stderr=subprocess.STDOUT)
    return r.returncode, time.time()-t0, log

def main():
    force = "--force" in sys.argv
    s=load_state()
    for name, script, out in STEPS:
        done = os.path.exists(out) and s.get(name,{}).get("status")=="ok"
        if done and not force:
            print(f"[skip] {name} (output exists)"); continue
        print(f"[run ] {name} ...", flush=True)
        rc, dur, log = run_step(name, script)
        s[name]={"status":"ok" if rc==0 else "fail","rc":rc,"dur":round(dur,1),
                 "at":time.strftime("%Y-%m-%dT%H:%M:%SZ"),"log":log}
        save_state(s)
        tail=open(log).read().strip().splitlines()[-3:] if os.path.exists(log) else []
        print(f"[{'ok' if rc==0 else 'FAIL'}] {name} rc={rc} {dur:.0f}s")
        for t in tail: print("     ", t)
        if rc!=0:
            print(f"[stop] {name} failed; fix and re-run"); break
    print("[pipeline] pass complete")

if __name__ == "__main__":
    main()
