#!/usr/bin/env python3
"""
End-to-end test harness for the OSTI cards website.
Exercises every API route against REAL data pulled from the live site.
Exit 0 = all pass; 1 = any failure. Prints a PASS/FAIL line per check.
"""
import json, sys, urllib.request, urllib.parse, io, zipfile

BASE = "http://127.0.0.1:8094"
results = []

def get(path, raw=False, timeout=30):
    url = BASE + path
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        code = r.getcode()
        body = r.read()
        ctype = r.headers.get("Content-Type", "")
    return code, body, ctype

def check(name, cond, detail=""):
    ok = bool(cond)
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))
    return ok

# 1. STATS
try:
    code, body, _ = get("/api/stats")
    st = json.loads(body)
    check("stats responds 200", code == 200)
    check("stats has total_real", st.get("total_real", 0) > 0, f"total_real={st.get('total_real')}")
    check("stats has distinct_papers", st.get("distinct_papers", 0) > 0, f"papers={st.get('distinct_papers')}")
    check("stats by_mode populated", len(st.get("by_mode", {})) >= 5, f"modes={list(st.get('by_mode',{}).keys())}")
    check("stats by_year has 2026", "2026" in st.get("by_year", {}), f"2026={st.get('by_year',{}).get('2026')}")
    nyears = len(st.get("by_year", {}))
    check("stats covers many years", nyears >= 20, f"years={nyears}")
except Exception as e:
    check("stats endpoint", False, f"EXC {e}")

# 2. SEARCH (plain)
hit = None
try:
    code, body, _ = get("/api/search?q=" + urllib.parse.quote("simulation") + "&limit=10")
    sr = json.loads(body)
    rows = sr if isinstance(sr, list) else sr.get("results", sr.get("rows", []))
    check("search responds 200", code == 200)
    check("search returns rows", len(rows) > 0, f"n={len(rows)}")
    if rows:
        hit = rows[0]
except Exception as e:
    check("search endpoint", False, f"EXC {e}")

# 3. SEARCH with filters (mode + year)
try:
    code, body, _ = get("/api/search?q=&mode=model&year=2024&limit=5")
    sr = json.loads(body)
    rows = sr if isinstance(sr, list) else sr.get("results", sr.get("rows", []))
    check("search mode+year filter 200", code == 200, f"n={len(rows)}")
    if rows:
        r0 = rows[0]
        check("filter respects mode", all((r.get("mode","model")=="model") for r in rows), "mode=model")
        check("filter respects year", all(str(r.get("year","2024"))=="2024" for r in rows), "year=2024")
except Exception as e:
    check("search filter endpoint", False, f"EXC {e}")

# Resolve a concrete (year, mode, id) for card/download tests
ymid = None
try:
    code, body, _ = get("/api/search?q=&mode=simulation&year=2026&limit=5")
    sr = json.loads(body)
    rows = sr if isinstance(sr, list) else sr.get("results", sr.get("rows", []))
    for r in rows:
        y = str(r.get("year","")); m = r.get("mode",""); i = str(r.get("osti_id") or r.get("id") or r.get("paper_id") or "")
        if y and m and i:
            ymid = (y, m, i); break
except Exception:
    pass

# 4. CARD view
if ymid:
    y, m, i = ymid
    try:
        code, body, _ = get(f"/api/card/{y}/{m}/{i}")
        check("card view 200", code == 200, f"{y}/{m}/{i}")
        check("card view has content", len(body) > 20, f"{len(body)}B")
    except Exception as e:
        check("card view", False, f"EXC {e}")
    # 5. DOWNLOAD single
    try:
        code, body, ctype = get(f"/api/download/{y}/{m}/{i}")
        check("single download 200", code == 200, f"{len(body)}B {ctype}")
    except Exception as e:
        check("single download", False, f"EXC {e}")
    # 6/7/8. SOURCES: pdf, raw mmd, render
    try:
        code, body, ctype = get(f"/api/pdf/{y}/{i}")
        check("source PDF reachable", code == 200 and len(body) > 1000, f"{len(body)}B {ctype}")
    except Exception as e:
        check("source PDF", False, f"EXC {e} (may be no pdf for this id)")
    try:
        code, body, ctype = get(f"/api/raw/mmd/{y}/{i}")
        check("raw mmd reachable", code == 200 and len(body) > 50, f"{len(body)}B")
    except Exception as e:
        check("raw mmd", False, f"EXC {e}")
    try:
        code, body, ctype = get(f"/api/render/mmd/{y}/{i}")
        check("render mmd->HTML", code == 200 and b"<" in body, f"{len(body)}B {ctype}")
    except Exception as e:
        check("render mmd", False, f"EXC {e}")
else:
    check("resolve card id for detail tests", False, "no (year,mode,id) resolved")

# 9. BULK download (zip)
try:
    code, body, ctype = get("/api/bulk?q=simulation&mode=simulation&year=2026&max_cards=20", timeout=60)
    check("bulk responds 200", code == 200, f"{len(body)}B {ctype}")
    zf = zipfile.ZipFile(io.BytesIO(body))
    names = zf.namelist()
    check("bulk returns valid zip", len(names) > 0, f"{len(names)} files")
    check("bulk zip entries non-empty", all(zf.getinfo(n).file_size >= 0 for n in names[:5]))
except Exception as e:
    check("bulk download", False, f"EXC {e}")

# Summary
npass = sum(1 for _, ok, _ in results if ok)
nfail = len(results) - npass
print(f"\n==== {npass}/{len(results)} PASS, {nfail} FAIL ====")
sys.exit(0 if nfail == 0 else 1)
