#!/usr/bin/env python3
"""Pre-warm the render cache by walking every real-card paper and requesting
its .mmd->PDF render through the live site endpoint (same cache the site reads).
Resumable: the endpoint returns the cached file instantly if already rendered,
and we also skip locally-cached keys up front. Bounded concurrency (Chromium is heavy).

Usage: warm_cache.py [CONCURRENCY=6] [BASE=http://127.0.0.1:8094]
"""
import sqlite3, os, sys, time, glob, hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request

BASE = os.environ.get("BASE", "http://127.0.0.1:8094")
CONC = int(os.environ.get("CONCURRENCY", sys.argv[1] if len(sys.argv) > 1 else 6))
SITE = os.path.expanduser("~/code/osti-cards-site")
SGMMD = "/Volumes/SG-1-8TB/osti/mmd"
CACHE = os.path.join(SITE, "data", "render_cache")

def cache_key(kind, year, osti_id):
    src = f"{SGMMD}/{year}/{osti_id}.mmd"
    try:
        st = os.stat(src)
    except OSError:
        return None, None
    h = hashlib.sha1(f"{kind}:{year}:{osti_id}:{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:16]
    return os.path.join(CACHE, f"{h}.pdf"), src

def warm_one(year, osti_id):
    out, src = cache_key("mmd", year, osti_id)
    if out and os.path.isfile(out) and os.path.getsize(out) > 0:
        return ("skip", year, osti_id, 0.0)
    t0 = time.time()
    url = f"{BASE}/api/renderpdf/mmd/{year}/{osti_id}"
    try:
        with urllib.request.urlopen(url, timeout=180) as r:
            data = r.read()
        dt = time.time() - t0
        if r.status == 200 and len(data) > 500:
            return ("ok", year, osti_id, dt)
        return ("empty", year, osti_id, dt)
    except Exception as e:
        return (f"err:{type(e).__name__}", year, osti_id, time.time() - t0)

def main():
    c = sqlite3.connect(os.path.join(SITE, "data", "osti_cards.sqlite"))
    papers = c.execute("SELECT DISTINCT year, osti_id FROM cards ORDER BY year, osti_id").fetchall()
    lim = int(os.environ.get("LIMIT", "0"))
    if lim:
        papers = papers[:lim]
    total = len(papers)
    print(f"[warm] {total} papers, concurrency={CONC}, base={BASE}", flush=True)
    done = ok = skipped = failed = 0
    tsum = 0.0
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        futs = {ex.submit(warm_one, y, i): (y, i) for y, i in papers}
        for f in as_completed(futs):
            status, year, oid, dt = f.result()
            done += 1
            if status == "ok":
                ok += 1; tsum += dt
            elif status == "skip":
                skipped += 1
            else:
                failed += 1
                if failed <= 20:
                    print(f"[warm] FAIL {status} {year}/{oid}", flush=True)
            if done % 50 == 0 or done == total:
                el = time.time() - t_start
                rate = done / el if el else 0
                eta = (total - done) / rate / 60 if rate else 0
                avg = tsum / ok if ok else 0
                print(f"[warm] {done}/{total} ok={ok} skip={skipped} fail={failed} "
                      f"| {rate:.1f}/s eta={eta:.0f}m avg_render={avg:.1f}s", flush=True)
    print(f"[warm] DONE ok={ok} skip={skipped} fail={failed} "
          f"cached_pdfs={len(glob.glob(os.path.join(CACHE,'*.pdf')))} "
          f"elapsed={ (time.time()-t_start)/60:.1f}m", flush=True)

if __name__ == "__main__":
    main()
