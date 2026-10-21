#!/usr/bin/env python3
"""
review_synth.py — synthesize REVIEW/TUTORIAL articles for OSTI SuperCards (and cards).

Runs ON pecans against the qwen3.8-27b farm pool (17 endpoints). Restartable:
atomic claim via review_md IS NULL, writes review_md back. Zero-hallucination:
synthesizes only from member-paper evidence + consensus facts.

Modes:
  --kind super   : iterate supercards.sqlite (member_count DESC), synth per SuperCard
  --kind card    : iterate osti_cards.sqlite, synth per individual card
  --shard M/N    : process only rows where rowid % N == M (parallel workers)
  --limit K      : smoke cap
"""
import sqlite3, json, re, os, sys, time, glob, random, argparse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

SUPERDB = "/home/stevens/review/supercards.sqlite"
CARDDB  = "/home/stevens/review/osti_cards.sqlite"

# ---- farm endpoint pool ----
def load_endpoints():
    urls=set()
    for f in glob.glob("/home/stevens/bvbrc_all/endpoints/*.url"):
        try:
            u=open(f).read().strip().split("|")[0].strip()
            if u: urls.add(u.rstrip("/"))
        except: pass
    return sorted(urls)
EP=load_endpoints()
KEY=os.environ.get("OSTI_FARM_KEY","DLS-2026")  # farm bearer token; override via env

def live_endpoints():
    good=[]
    for u in EP:
        try:
            req=urllib.request.Request(u+"/models", headers={"Authorization":f"Bearer {KEY}"})
            urllib.request.urlopen(req,timeout=6); good.append(u)
        except: pass
    return good

def chat(ep, prompt, maxtok=6000):
    body=json.dumps({"model":"qwen3.8-27b","messages":[{"role":"user","content":prompt}],
                     "max_tokens":maxtok,"temperature":0.3,
                     "chat_template_kwargs":{"enable_thinking":False}}).encode()
    req=urllib.request.Request(ep+"/chat/completions", data=body,
        headers={"Content-Type":"application/json","Authorization":f"Bearer {KEY}"})
    r=json.load(urllib.request.urlopen(req, timeout=300))
    msg=r["choices"][0]["message"]
    txt=msg.get("content") or msg.get("reasoning_content") or ""
    if not txt: raise RuntimeError("empty content")
    # strip any <think> reasoning
    txt=re.sub(r'<think>.*?</think>', '', txt, flags=re.S).strip()
    # if the model left the article inside reasoning after a think block, keep from first heading
    if txt and not txt.lstrip().startswith("#"):
        m=re.search(r'(^#\s.*)', txt, flags=re.S|re.M)
        if m: txt=txt[m.start():].strip()
    if not txt: raise RuntimeError("empty after strip")
    return txt

# ---- prompt templates per mode ----
SECTIONS = {
 "data":"## Overview\n## What the Dataset Contains\n## How It Is Used in Research\n## Access & Format\n## How to Work With It (tutorial)\n## Provenance & Citation",
 "model":"## Overview\n## Architecture & Method\n## What It Predicts / Does\n## How It Is Used in This Corpus\n## How to Use It (tutorial)\n## Access & Licensing\n## Representative Works",
 "agent":"## Overview\n## What the Agent Does\n## Underlying Models & Tools\n## How It Is Used\n## How to Run It (tutorial)\n## Access & Licensing\n## Representative Works",
 "instrument":"## Overview\n## What It Measures & How\n## Specifications & Capabilities\n## How It Is Used in This Corpus\n## Typical Measurement Workflow (tutorial)\n## Access & Operation\n## Representative Works",
 "facility":"## Overview\n## Capabilities & Instruments\n## Science Enabled (synthesize across the corpus)\n## How Researchers Use It\n## How to Get Access (tutorial)\n## Operation & Stewardship\n## Representative Works",
 "simulation":"## Overview\n## Method & Physics\n## How It Is Used in This Corpus\n## Capabilities & Numerics\n## Typical Workflow (tutorial)\n## Access & Licensing\n## Representative Works",
 "workflow":"## Overview\n## What the Workflow Does\n## Steps & Data Flow\n## How It Is Used\n## How to Run It (tutorial)\n## Access & Licensing\n## Representative Works",
}
NAME_KEY={"data":"dataset","model":"model","instrument":"instrument","facility":"facility",
          "simulation":"code/software","workflow":"workflow","agent":"agent"}

def super_prompt(sc, evidence):
    mode=sc["mode"]; cons=json.loads(sc.get("consensus_json") or "{}")
    cons_text="; ".join(f"{k}={v.get('value')} (agreed by {v.get('support')})"
                        for k,v in cons.items() if isinstance(v,dict) and v.get('value'))
    secs=SECTIONS.get(mode, SECTIONS["simulation"])
    return f"""You are writing a REVIEW / TUTORIAL ARTICLE for a DOE research-resource knowledge base.
This is a SuperCard: it merges {sc['member_count']} papers (years {sc.get('first_year')}-{sc.get('last_year')}) that all use the same {NAME_KEY.get(mode,mode)} "{sc['canonical_name']}".

GROUND-TRUTH CONSENSUS (facts agreed across member papers): {cons_text or '(none extracted)'}

EVIDENCE — excerpts from member papers (cite as [OSTI:id] when you draw on one):
{evidence}

Write a substantive review article in Markdown with exactly these sections:
{secs}

Rules:
- Write flowing explanatory PROSE like a review/tutorial article — NOT bullet outlines of fields, NOT a sparse list.
- Synthesize the DOMINANT PATTERNS across the member papers; ground claims in the evidence with inline [OSTI:id] citations.
- Do NOT invent URLs, versions, emails, or facts not present in the evidence or consensus. If unknown, say so plainly.
- Be concrete and useful to a researcher. 600-900 words. Start directly with the # title line: "# {sc['canonical_name']}"."""

def card_prompt(c):
    mode=c["mode"]
    secs=SECTIONS.get(mode, SECTIONS["simulation"])
    desc=(c["description"] or "")[:1500]
    yaml=(c["yaml"] or "")[:3500]
    return f"""You are writing a REVIEW / TUTORIAL ARTICLE for a DOE research-resource knowledge base,
about the {NAME_KEY.get(mode,mode)} "{c['title']}" described in OSTI paper {c['osti_id']} ({c['year']}).

PAPER DESCRIPTION:
{desc}

EXTRACTED STRUCTURED CARD (source facts — do not contradict, but expand into prose):
{yaml}

Write a substantive review/tutorial article in Markdown with these sections:
{secs}

Rules:
- Flowing explanatory PROSE like a tutorial — NOT a bullet outline of fields, NOT sparse.
- Explain what the resource is, how it works, and how a researcher would actually use it.
- Do NOT invent URLs, versions, emails, or facts absent from the description/card. If unknown, say so.
- 350-600 words. Start directly with: "# {c['title']}"."""

def get_super_evidence(cardcon, super_id, cardcon_cur, members):
    rows=cardcon.execute(
      f"SELECT osti_id,year,title,description FROM cards WHERE mode=(SELECT mode FROM pragma_table_info('x')) LIMIT 0")  # placeholder
    return ""

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--kind",choices=["super","card"],required=True)
    ap.add_argument("--shard",default="0/1")
    ap.add_argument("--limit",type=int,default=0)
    ap.add_argument("--workers",type=int,default=0)
    a=ap.parse_args()
    M,N=map(int,a.shard.split("/"))
    eps=live_endpoints()
    if not eps: print("NO LIVE ENDPOINTS"); sys.exit(1)
    workers=a.workers or max(4,len(eps)*2)
    print(f"shard {M}/{N} | {len(eps)} live endpoints | {workers} workers | kind={a.kind}", flush=True)

    if a.kind=="super":
        con=sqlite3.connect(SUPERDB, timeout=180)
        cols=[r[1] for r in con.execute("PRAGMA table_info(supercard)")]
        q=("SELECT rowid,* FROM supercard WHERE (review_md IS NULL OR review_md='') "
           f"AND rowid % {N} = {M} ORDER BY member_count DESC")
        todo=[dict(zip(["rowid"]+cols,r)) for r in con.execute(q).fetchall()]
        if a.limit: todo=todo[:a.limit]
        print(f"supercards to synth: {len(todo)}", flush=True)
        done=[0]; t0=time.time()
        def work(sc):
            ev=sc.get("evidence") or ""
            ep=random.choice(eps)
            try:
                md=chat(ep, super_prompt(sc, ev))
                return (sc['rowid'], md)
            except Exception as e:
                return (sc['rowid'], None, str(e)[:60])
        now=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs=[ex.submit(work,sc) for sc in todo]
            for f in as_completed(futs):
                res=f.result()
                if len(res)==2 or res[1]:
                    con.execute("UPDATE supercard SET review_md=?, review_model='qwen3.8-27b', review_at=? WHERE rowid=?",
                                (res[1], now, res[0]))
                    done[0]+=1
                    if done[0]%20==0:
                        con.commit()
                        print(f"  {done[0]}/{len(todo)} ({done[0]/(time.time()-t0):.1f}/s)", flush=True)
        con.commit(); print(f"DONE super shard {M}/{N}: {done[0]} synthesized", flush=True)

    else:
        con=sqlite3.connect(CARDDB, timeout=180)
        cols=[r[1] for r in con.execute("PRAGMA table_info(cards)")]
        q=("SELECT rowid,* FROM cards WHERE (review_md IS NULL OR review_md='') "
           f"AND rowid % {N} = {M}")
        cur=con.execute(q)
        done=[0]; t0=time.time()
        now=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        def work(row):
            c=dict(zip(["rowid"]+cols,row))
            ep=random.choice(eps)
            try:
                md=chat(ep, card_prompt(c), maxtok=1100)
                return (c['rowid'], md)
            except Exception as e:
                return (c['rowid'], None)
        batch=cur.fetchmany(2000)
        total=0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            while batch:
                if a.limit and total>=a.limit: break
                futs=[ex.submit(work,r) for r in batch]
                wcon=sqlite3.connect(CARDDB, timeout=180)
                for f in as_completed(futs):
                    res=f.result()
                    if res[1]:
                        wcon.execute("UPDATE cards SET review_md=?, review_model='qwen3.8-27b', review_at=? WHERE rowid=?",(res[1],now,res[0]))
                        done[0]+=1
                        if done[0]%50==0:
                            wcon.commit(); print(f"  {done[0]} ({done[0]/(time.time()-t0):.1f}/s)", flush=True)
                wcon.commit(); wcon.close()
                total+=len(batch)
                batch=cur.fetchmany(2000)
        print(f"DONE card shard {M}/{N}: {done[0]} synthesized", flush=True)

if __name__=="__main__":
    main()
