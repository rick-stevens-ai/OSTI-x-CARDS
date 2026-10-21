#!/usr/bin/env python3
"""
url_validate.py — live-verify the actionable URLs qa_score attached (qa_url column).

Writes qa_url_status (ok / dead / timeout / blocked / skip) + qa_url_checked_at.
A HIGH card's URL component is only truly satisfied when qa_url_status='ok'.

Concurrent (thread pool), polite (per-host throttle), cached (never re-checks a URL
whose status is fresh < TTL days), resumable (only checks NULL/stale rows).
Zero mutation of card content — only the qa_url_* columns.
HEAD first, fall back to ranged GET; treats 2xx/3xx/401/403 as 'ok' (resource exists,
may be auth/paywalled but is a real operable endpoint), 404/410/DNS/conn as dead.
"""
import sqlite3, os, sys, time, threading, collections
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
import urllib.request, urllib.error, ssl

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "osti_cards.sqlite")
TTL_DAYS = 30
WORKERS = 40
TIMEOUT = 6
UA = "Mozilla/5.0 (compatible; osti-cards-url-validator/1.0)"
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

_host_lock = collections.defaultdict(threading.Lock)
_host_last = collections.defaultdict(float)
MIN_GAP = 0.25  # sec between hits to the same host

def _throttle(host):
    with _host_lock[host]:
        dt = time.time() - _host_last[host]
        if dt < MIN_GAP:
            time.sleep(MIN_GAP - dt)
        _host_last[host] = time.time()

def classify(url):
    from urllib.parse import urlparse
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return "skip"
    if not host:
        return "skip"
    _throttle(host)
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
            if method == "GET":
                req.add_header("Range", "bytes=0-2047")
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ctx) as r:
                code = r.status
                if 200 <= code < 400:
                    return "ok"
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 405, 429, 406):
                return "ok"      # exists, just gated/method-restricted
            if e.code in (404, 410):
                return "dead"
            if method == "GET":
                return "blocked"
        except (urllib.error.URLError, TimeoutError, ssl.SSLError):
            if method == "GET":
                return "timeout"
        except Exception:
            if method == "GET":
                return "dead"
    return "dead"

def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit")+1])
    con = sqlite3.connect(DB, timeout=120)
    cols = {r[1] for r in con.execute("PRAGMA table_info(cards)")}
    if "qa_url_status" not in cols:
        con.execute("ALTER TABLE cards ADD COLUMN qa_url_status TEXT")
    if "qa_url_checked_at" not in cols:
        con.execute("ALTER TABLE cards ADD COLUMN qa_url_checked_at TEXT")
    con.commit()

    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(time.time() - TTL_DAYS*86400))
    # unique URLs needing a check (dedupe across cards -> check each URL once)
    q = ("SELECT DISTINCT qa_url FROM cards WHERE qa_url IS NOT NULL AND qa_url!='' "
         "AND (qa_url_status IS NULL OR qa_url_checked_at < ?)")
    urls = [r[0] for r in con.execute(q, (cutoff,)).fetchall()]
    if limit:
        urls = urls[:limit]
    print(f"URLs to validate: {len(urls):,}  (workers={WORKERS})")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    import socket as _sock
    _sock.setdefaulttimeout(TIMEOUT + 3)   # hard floor: DNS/connect can't hang past urlopen timeout
    results = {}
    t0 = time.time(); done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(classify, u): u for u in urls}
        try:
            # overall wall-clock budget: generous backstop (socket timeout already
            # prevents individual hangs). ~0.6s/URL + 10min floor.
            budget = max(900, int(len(urls) * 0.6))
            for f in as_completed(futs, timeout=budget):
                u = futs[f]
                try: results[u] = f.result()
                except Exception: results[u] = "dead"
                done += 1
                # INCREMENTAL commit: a hang after this point can't lose prior work
                con.execute("UPDATE cards SET qa_url_status=?, qa_url_checked_at=? WHERE qa_url=?",
                            (results[u], now, u))
                if done % 200 == 0:
                    con.commit()
                    rate = done/(time.time()-t0)
                    print(f"  {done:,}/{len(urls):,}  ({rate:.0f}/s)", flush=True)
        except FuturesTimeout:
            # abandon still-hung futures; mark their URLs 'timeout', keep everything already done
            for f, u in futs.items():
                if u not in results:
                    results[u] = "timeout"
                    con.execute("UPDATE cards SET qa_url_status=?, qa_url_checked_at=? WHERE qa_url=?",
                                (results[u], now, u))
            print(f"  wall-clock budget hit; abandoned {sum(1 for u in futs.values() if results.get(u)=='timeout')} hung URLs", flush=True)
        ex.shutdown(wait=False, cancel_futures=True)
    con.commit()
    by_status = collections.Counter(results.values())
    print("status breakdown (unique URLs):", dict(by_status))
    # card-level rollup
    rows = dict(con.execute("SELECT qa_url_status, COUNT(*) FROM cards "
                            "WHERE qa_url_status IS NOT NULL GROUP BY qa_url_status").fetchall())
    print("status breakdown (cards):", rows)
    con.close()

if __name__ == "__main__":
    main()
