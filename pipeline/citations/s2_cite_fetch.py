#!/usr/bin/env python3
"""
s2_cite_fetch.py — fetch citation counts via Semantic Scholar batch (run ON pecans).
Reads _citation_remaining.jsonl ({osti_id,doi}), POSTs up to 500 DOIs/req,
writes s2_citations.jsonl ({doi,osti_id,citationCount,year}). Resumable.
"""
import json, time, os, sys, urllib.request, urllib.error
HERE=os.path.dirname(os.path.abspath(__file__))
WORK=os.path.join(HERE,"_citation_remaining.jsonl")
OUT=os.path.join(HERE,"s2_citations.jsonl")
MISS=os.path.join(HERE,"s2_misses.jsonl")
BATCH=500
GAP=1.2            # unkeyed S2 ~1 req/s
URL="https://api.semanticscholar.org/graph/v1/paper/batch?fields=citationCount,year,externalIds"

def norm(d):
    d=d.strip().lower()
    for p in ("https://doi.org/","http://doi.org/","doi:"):
        if d.startswith(p): d=d[len(p):]
    return d

def post(dois):
    body=json.dumps({"ids":[f"DOI:{d}" for d in dois]}).encode()
    req=urllib.request.Request(URL, data=body,
        headers={"Content-Type":"application/json","User-Agent":"osti-xcard/1.0"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code==429:
                time.sleep(min(60,5*(attempt+1))); continue
            if attempt==5: raise
            time.sleep(2**attempt)
        except Exception:
            if attempt==5: raise
            time.sleep(2**attempt)
    return None

def main():
    rows=[]
    with open(WORK) as f:
        for line in f:
            j=json.loads(line); rows.append((norm(j["doi"]), j["osti_id"]))
    seen={}
    for d,o in rows:
        if d and d not in seen: seen[d]=o
    done=set()
    if os.path.exists(OUT):
        for line in open(OUT):
            try: done.add(json.loads(line)["doi"])
            except: pass
    if os.path.exists(MISS):
        for line in open(MISS):
            try: done.add(json.loads(line)["doi"])
            except: pass
    todo=[(d,o) for d,o in seen.items() if d not in done]
    print(f"unique={len(seen)} done={len(done)} todo={len(todo)}", flush=True)
    fo=open(OUT,"a"); fm=open(MISS,"a")
    t0=time.time(); n=0; hits=0
    for i in range(0,len(todo),BATCH):
        chunk=todo[i:i+BATCH]
        bmap={d:o for d,o in chunk}
        res=None
        try: res=post([d for d,_ in chunk])
        except Exception as e:
            print(f"  batch {i} ERR {e}", flush=True); time.sleep(5); continue
        got=set()
        for p in (res or []):
            if not p: continue
            doi=(p.get("externalIds") or {}).get("DOI")
            if not doi: continue
            dn=norm(doi)
            if dn in bmap:
                fo.write(json.dumps({"doi":dn,"osti_id":bmap[dn],
                    "citationCount":p.get("citationCount"),"year":p.get("year")})+"\n")
                got.add(dn); hits+=1
        for d,o in chunk:
            if d not in got: fm.write(json.dumps({"doi":d,"osti_id":o})+"\n")
        fo.flush(); fm.flush(); n+=len(chunk)
        rate=n/max(time.time()-t0,1)
        print(f"  {n}/{len(todo)} hits={hits} ({rate:.0f}/s)", flush=True)
        time.sleep(GAP)
    print(f"DONE n={n} hits={hits} elapsed={time.time()-t0:.0f}s -> {OUT}", flush=True)

if __name__=="__main__": main()
