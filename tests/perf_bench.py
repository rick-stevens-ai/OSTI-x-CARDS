#!/usr/bin/env python3
"""Measure real latency across OSTI cards site routes. N samples each, report min/median/p95/max."""
import time, json, urllib.request, urllib.parse, statistics, io, zipfile

BASE = "http://127.0.0.1:8094"

def timed(path, n=12, warm=2, timeout=60):
    url = BASE + path
    for _ in range(warm):
        try:
            urllib.request.urlopen(url, timeout=timeout).read()
        except Exception:
            pass
    ts, sz = [], 0
    code = 0
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                b = r.read(); code = r.getcode()
            ts.append((time.perf_counter() - t0) * 1000)
            sz = len(b)
        except Exception as e:
            ts.append(float("nan")); code = f"ERR:{e}"
    good = [x for x in ts if x == x]
    if not good:
        return (path, code, 0, None, None, None, None, 0)
    good.sort()
    p95 = good[min(len(good)-1, int(len(good)*0.95))]
    return (path, code, len(good),
            round(min(good),1), round(statistics.median(good),1),
            round(p95,1), round(max(good),1), sz)

# resolve a real id for detail routes
def one_id(mode, year):
    try:
        b = urllib.request.urlopen(BASE + f"/api/search?q=&mode={mode}&year={year}&limit=5", timeout=30).read()
        rows = json.loads(b)
        rows = rows if isinstance(rows, list) else rows.get("results", rows.get("rows", []))
        for r in rows:
            i = str(r.get("osti_id") or r.get("id") or r.get("paper_id") or "")
            if i: return i
    except Exception: pass
    return None

sid = one_id("simulation", "2026") or "2376939"
print(f"[using sample id {sid} for detail routes]\n")

routes = [
    "/api/stats",
    "/api/search?q=simulation&limit=20",
    "/api/search?q=machine+learning+neural&limit=20",
    "/api/search?q=&mode=model&year=2024&limit=20",
    "/api/search?q=quantum&mode=simulation&year=2023&limit=50",
    f"/api/card/2026/simulation/{sid}",
    f"/api/download/2026/simulation/{sid}",
    f"/api/raw/mmd/2026/{sid}",
    f"/api/render/mmd/2026/{sid}",
    f"/api/pdf/2026/{sid}",
    "/api/bulk?q=simulation&mode=simulation&year=2026&max_cards=50",
    "/",  # homepage
]
print(f"{'route':<52}{'code':<6}{'n':<4}{'min':>7}{'med':>7}{'p95':>8}{'max':>8}{'bytes':>10}")
print("-"*103)
for rt in routes:
    n = 6 if ("bulk" in rt or "render" in rt or "pdf" in rt) else 12
    path, code, ng, mn, md, p95, mx, sz = timed(rt, n=n)
    disp = (rt[:50]+"..") if len(rt) > 52 else rt
    print(f"{disp:<52}{str(code):<6}{ng:<4}{mn if mn is not None else '-':>7}{md if md is not None else '-':>7}{p95 if p95 is not None else '-':>8}{mx if mx is not None else '-':>8}{sz:>10}")
print("\n(all times ms; warm cache; localhost)")
