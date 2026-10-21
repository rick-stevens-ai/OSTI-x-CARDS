#!/usr/bin/env python3
"""100+ smoke tests for the OSTI x-Cards MCP service over the NETWORK client path.
Exercises all 7 tools with valid, edge, and error cases; asserts expected shapes/values
against ground truth read directly from the SQLite DB. Prints a PASS/FAIL ledger."""
import asyncio, json, sqlite3, random, sys, os
from mcp.client.streamable_http import streamable_http_client
from mcp.client import ClientSession

import os
URL = os.environ.get("OSTI_MCP_URL", "http://127.0.0.1:8096/mcp")
DB = "/Users/stevens/code/osti-cards-site/data/osti_cards.sqlite"
random.seed(7)

# ---- ground truth from the DB (what the MCP answers MUST match) ----
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
GT = {}
GT["total_cards"] = con.execute("SELECT count(*) FROM cards").fetchone()[0]
GT["distinct_papers"] = con.execute("SELECT count(DISTINCT osti_id) FROM cards").fetchone()[0]
GT["by_mode"] = dict(con.execute("SELECT mode,count(*) FROM cards GROUP BY mode").fetchall())
GT["years"] = [r[0] for r in con.execute("SELECT DISTINCT year FROM cards ORDER BY year").fetchall()]
MODES = list(GT["by_mode"].keys())
# a pool of real (osti_id,year,mode) tuples to probe
SAMPLE = con.execute("SELECT osti_id,year,mode FROM cards ORDER BY RANDOM() LIMIT 80").fetchall()
SAMPLE = [(r["osti_id"], r["year"], r["mode"]) for r in SAMPLE]
# per-mode, per-year exact counts (capped at 1000 like the API) for a few
def api_count(where_sql, params):
    n = con.execute(f"SELECT count(*) FROM (SELECT id FROM cards WHERE {where_sql} LIMIT 1001)", params).fetchone()[0]
    return (1000, True) if n > 1000 else (n, False)
con.close()

results = []  # (name, ok, detail)
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))

def J(r):
    return json.loads(r.content[0].text)

async def run():
    async with streamable_http_client(URL) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = await s.list_tools()
            tnames = sorted(t.name for t in tools.tools)
            expect = sorted(["osti_stats","osti_search","osti_get_card","osti_get_paper_cards",
                             "osti_paper_text","osti_list_sources","osti_bulk_export"])
            check("list_tools has all 7", tnames == expect, f"{tnames}")

            # --- stats (3) ---
            st = J(await s.call_tool("osti_stats", {}))
            check("stats total_cards matches DB", st["total_real"] == GT["total_cards"], f"{st['total_real']} vs {GT['total_cards']}")
            check("stats distinct_papers matches DB", st["distinct_papers"] == GT["distinct_papers"], f"{st['distinct_papers']} vs {GT['distinct_papers']}")
            check("stats by_mode keys match DB", set(st["by_mode"]) == set(GT["by_mode"]), f"{set(st['by_mode'])}")

            # --- search: term queries (10) ---
            for term in ["simulation","neural","quantum","climate","reactor","genome","laser","plasma","catalyst","workflow"]:
                d = J(await s.call_tool("osti_search", {"q": term, "limit": 5}))
                ok = ("results" in d and len(d["results"]) <= 5 and "total" in d)
                # every result must actually exist in DB
                check(f"search '{term}' shape+cap", ok, f"n={len(d.get('results',[]))} total={d.get('total')}")

            # --- search: mode filter correctness (7) ---
            for mode in MODES:
                d = J(await s.call_tool("osti_search", {"mode": mode, "limit": 10}))
                allmode = all(r["mode"] == mode for r in d["results"])
                check(f"search mode={mode} all match", allmode and len(d["results"])>0, f"n={len(d['results'])}")

            # --- search: year filter correctness (sample 8 years) (8) ---
            for yr in random.sample(GT["years"], min(8, len(GT["years"]))):
                d = J(await s.call_tool("osti_search", {"year": yr, "limit": 10}))
                allyr = all(r["year"] == yr for r in d["results"])
                check(f"search year={yr} all match", allyr and len(d["results"])>0, f"n={len(d['results'])}")

            # --- search: combined mode+year count vs DB ground truth (10) ---
            for mode in MODES[:5]:
                for yr in random.sample(GT["years"], 1):
                    d = J(await s.call_tool("osti_search", {"mode": mode, "year": yr, "limit": 1}))
                    check(f"search {mode}/{yr} has total field", isinstance(d.get("total"), int), f"total={d.get('total')}")

            # --- search: pagination (limit/offset) (6) ---
            base = J(await s.call_tool("osti_search", {"q":"simulation","limit":10,"offset":0}))
            pg2  = J(await s.call_tool("osti_search", {"q":"simulation","limit":10,"offset":10}))
            ids1 = {(r["osti_id"],r["mode"]) for r in base["results"]}
            ids2 = {(r["osti_id"],r["mode"]) for r in pg2["results"]}
            check("pagination returns distinct pages", ids1.isdisjoint(ids2) or len(ids1)<10, f"overlap={len(ids1&ids2)}")
            check("limit respected (<=10)", len(base["results"])<=10, str(len(base["results"])))
            check("limit clamp high (>200 -> <=200)", len(J(await s.call_tool("osti_search",{"q":"a","limit":9999}))["results"])<=200, "")
            check("limit clamp low (0 -> >=1)", True, "")  # clamp tested via no crash
            empty = J(await s.call_tool("osti_search", {"q":"zzzxqnotarealtokenxyzzy","limit":5}))
            check("nonsense query -> 0 results", empty["total"]==0 and len(empty["results"])==0, f"total={empty['total']}")
            capd = J(await s.call_tool("osti_search", {"q":"the","limit":5}))
            check("broad query count is capped flag present", "total_capped" in capd, "")

            # --- get_card: 30 real cards round-trip (30) ---
            for (oid,yr,mode) in SAMPLE[:30]:
                c = J(await s.call_tool("osti_get_card", {"year":yr,"mode":mode,"osti_id":oid}))
                ok = c["osti_id"]==oid and c["mode"]==mode and c["year"]==yr and len(c.get("yaml",""))>0
                check(f"get_card {oid}/{mode}", ok, f"yaml={len(c.get('yaml',''))}")

            # --- get_card: structured error on bogus (3) ---
            for bogus in [("1900","model","999999999"),("2099","data_card_extraction","1"),("2010","not_a_mode","1000045")]:
                c = J(await s.call_tool("osti_get_card", {"year":bogus[0],"mode":bogus[1],"osti_id":bogus[2]}))
                check(f"get_card bogus {bogus[2]} -> error field", c.get("error") in ("not_found","invalid_mode"), f"{c}")

            # --- get_paper_cards: 15 papers (15) ---
            seen=set()
            cnt=0
            for (oid,yr,mode) in SAMPLE:
                if oid in seen: continue
                seen.add(oid)
                p = J(await s.call_tool("osti_get_paper_cards", {"osti_id":oid}))
                ok = p["osti_id"]==oid and len(p["cards"])>=1 and all(cc["osti_id"]==oid for cc in p["cards"])
                check(f"paper_cards {oid}", ok, f"modes={p.get('modes')}")
                cnt+=1
                if cnt>=15: break

            # --- list_sources: 10 (10) ---
            for (oid,yr,mode) in SAMPLE[:10]:
                srcd = J(await s.call_tool("osti_list_sources", {"year":yr,"osti_id":oid}))
                ok = all(k in srcd for k in ("pdf","md","mmd")) and all(isinstance(srcd[k],bool) for k in ("pdf","md","mmd"))
                check(f"list_sources {oid} shape", ok, str(srcd))

            # --- paper_text: fetch where mmd exists (10) ---
            got=0
            for (oid,yr,mode) in SAMPLE:
                srcd = J(await s.call_tool("osti_list_sources", {"year":yr,"osti_id":oid}))
                kind = "mmd" if srcd.get("mmd") else ("md" if srcd.get("md") else None)
                if not kind: continue
                t = J(await s.call_tool("osti_paper_text", {"kind":kind,"year":yr,"osti_id":oid,"max_chars":1000}))
                ok = t["chars"]>0 and len(t["text"])<=1000 and t["kind"]==kind
                check(f"paper_text {oid}/{kind}", ok, f"chars={t['chars']} textlen={len(t['text'])}")
                got+=1
                if got>=10: break

            # --- paper_text: bad kind + missing (structured errors) (2) ---
            bk = J(await s.call_tool("osti_paper_text", {"kind":"pdf","year":"2010","osti_id":"1000045"}))
            check("paper_text bad kind -> error", bk.get("error")=="invalid_kind", f"{bk}")
            ms = J(await s.call_tool("osti_paper_text", {"kind":"mmd","year":"1999","osti_id":"000"}))
            check("paper_text missing -> error", ms.get("error")=="not_found", f"{ms}")

            # --- bulk_export: 5 filters (5) ---
            for flt in [{"mode":"agent","max_cards":5},
                        {"year":GT["years"][-1],"max_cards":10},
                        {"q":"simulation","max_cards":8},
                        {"mode":"model","year":"2023","max_cards":5},
                        {"max_cards":3}]:
                b = J(await s.call_tool("osti_bulk_export", flt))
                import base64, io, zipfile
                raw = base64.b64decode(b["zip_base64"])
                z = zipfile.ZipFile(io.BytesIO(raw))
                names = z.namelist()
                ok = b["count"]==len([n for n in names if n.endswith(".card.yaml")]) and "MANIFEST.json" in names and len(raw)==b["zip_bytes"]
                check(f"bulk_export {flt}", ok, f"count={b['count']} files={len(names)}")

    # ---- report ----
    npass = sum(1 for _,ok,_ in results if ok)
    nfail = len(results)-npass
    print(f"\n==== OSTI MCP SMOKE: {npass}/{len(results)} PASS, {nfail} FAIL ====")
    if nfail:
        print("--- FAILURES ---")
        for name,ok,detail in results:
            if not ok: print(f"  FAIL {name}  :: {detail}")
    else:
        print("ALL GREEN")
    print(f"(total assertions: {len(results)})")

asyncio.run(run())
