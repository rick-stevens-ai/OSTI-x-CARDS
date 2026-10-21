#!/usr/bin/env python3
"""
citation_fetch.py — look up citation counts for OSTI x-card papers via OpenAlex.

Zero-LLM, deterministic. Reads data/_citation_dois.jsonl ({osti_id,doi}),
batches DOIs (50/req) to the OpenAlex works endpoint, records cited_by_count,
publication_year, openalex_id, and title. Resumable (skips DOIs already in the
output DB). Writes data/citations.sqlite.

Top-1000 is then just: SELECT ... ORDER BY cited_by_count DESC LIMIT 1000.
"""
import sqlite3, urllib.request, urllib.parse, urllib.error, json, time, sys, os

HERE=os.path.dirname(os.path.abspath(__file__))
WORK=os.path.join(HERE,"data","_citation_dois.jsonl")
OUT=os.path.join(HERE,"data","citations.sqlite")
MAILTO="rick.stevens.ai@gmail.com"
BATCH=50
GAP=0.18            # ~5-6/s steady, stays under OpenAlex 429 threshold
TIMEOUT=25
MAXRETRY=6

def norm(doi):
    d=doi.strip().lower()
    for p in ("https://doi.org/","http://doi.org/","doi:"):
        if d.startswith(p): d=d[len(p):]
    return d

def db():
    c=sqlite3.connect(OUT)
    c.execute("""CREATE TABLE IF NOT EXISTS citations(
        doi TEXT PRIMARY KEY, osti_id TEXT, cited_by_count INTEGER,
        publication_year INTEGER, openalex_id TEXT, title TEXT, checked_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS misses(doi TEXT PRIMARY KEY, osti_id TEXT, checked_at TEXT)""")
    c.commit(); return c

def fetch_batch(dois):
    # OpenAlex: filter=doi:d1|d2|... (per_page up to 50)
    flt="doi:"+"|".join(dois)
    qs=urllib.parse.urlencode({"filter":flt,"per_page":BATCH,
        "select":"id,doi,cited_by_count,publication_year,title","mailto":MAILTO})
    url="https://api.openalex.org/works?"+qs
    req=urllib.request.Request(url, headers={"User-Agent":f"osti-xcard/1.0 {MAILTO}"})
    for attempt in range(MAXRETRY):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.load(r).get("results",[])
        except urllib.error.HTTPError as e:
            if e.code==429:
                # respect Retry-After; escalate wait hard on repeated 429
                ra=e.headers.get("Retry-After")
                wait=int(ra) if (ra and ra.isdigit()) else min(60, 5*(attempt+1))
                time.sleep(wait)
                continue
            if attempt==MAXRETRY-1: raise
            time.sleep(2**attempt)
        except Exception as e:
            if attempt==MAXRETRY-1: raise
            time.sleep(2**attempt)
    return []

def main():
    limit=int(sys.argv[sys.argv.index("--limit")+1]) if "--limit" in sys.argv else None
    rows=[]
    with open(WORK) as f:
        for line in f:
            j=json.loads(line); rows.append((j["osti_id"], norm(j["doi"])))
    # dedup by doi, keep first osti_id
    seen={}
    for oid,doi in rows:
        if doi and doi not in seen: seen[doi]=oid
    c=db()
    done=set(r[0] for r in c.execute("SELECT doi FROM citations"))
    done|=set(r[0] for r in c.execute("SELECT doi FROM misses"))
    todo=[(doi,oid) for doi,oid in seen.items() if doi not in done]
    if limit: todo=todo[:limit]
    print(f"total unique DOIs={len(seen)} already={len(done)} todo={len(todo)}", flush=True)
    t0=time.time(); n=0; hits=0
    for i in range(0,len(todo),BATCH):
        chunk=todo[i:i+BATCH]
        bmap={d:o for d,o in chunk}
        try:
            res=fetch_batch([d for d,_ in chunk])
        except Exception as e:
            print(f"  batch {i} ERR {e}", flush=True); time.sleep(3); continue
        got=set()
        for w in res:
            wd=norm((w.get("doi") or ""))
            if wd in bmap:
                c.execute("INSERT OR REPLACE INTO citations VALUES(?,?,?,?,?,?,datetime('now'))",
                    (wd,bmap[wd],w.get("cited_by_count"),w.get("publication_year"),
                     w.get("id"),(w.get("title") or "")[:300]))
                got.add(wd); hits+=1
        for d,o in chunk:
            if d not in got:
                c.execute("INSERT OR REPLACE INTO misses VALUES(?,?,datetime('now'))",(d,o))
        c.commit(); n+=len(chunk)
        if n % 500 < BATCH:
            rate=n/max(time.time()-t0,1)
            print(f"  {n}/{len(todo)}  hits={hits}  ({rate:.1f}/s)", flush=True)
        time.sleep(GAP)
    print(f"DONE n={n} hits={hits} elapsed={time.time()-t0:.0f}s -> {OUT}", flush=True)
    # show current top 15
    print("--- current TOP 15 ---", flush=True)
    for doi,oid,cc,yr,ti in c.execute(
        "SELECT doi,osti_id,cited_by_count,publication_year,title FROM citations ORDER BY cited_by_count DESC LIMIT 15"):
        print(f"  {cc:>7}  {yr}  {oid}  {doi}  {ti[:70]}", flush=True)
    c.close()

if __name__=="__main__":
    main()
