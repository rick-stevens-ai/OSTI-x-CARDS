#!/usr/bin/env python3
"""
enrich_apply.py — load recovered-email JSONL (and any other enrichment JSONL) into the
card_enrichment staging table. Emails are per-PAPER, applied to EVERY card of that paper.

Zero-hallucination: values come straight from the miner (parse text) or cross-card
consensus. source tag preserved. Idempotent (REPLACE on (osti_id,mode,field)).

Usage: enrich_apply.py --emails /tmp/email_recovered.jsonl
"""
import sqlite3, os, json, time, argparse

CARDDB = os.path.expanduser("~/code/osti-cards-site/data/osti_cards.sqlite")
STG    = os.path.expanduser("~/code/osti-cards-site/data/card_enrichment.sqlite")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--emails")
    a=ap.parse_args()
    card=sqlite3.connect(f"file:{CARDDB}?mode=ro", uri=True)
    stg=sqlite3.connect(STG)
    stg.executescript("""
      CREATE TABLE IF NOT EXISTS card_enrichment(
        osti_id TEXT, mode TEXT, field TEXT, value TEXT, source TEXT,
        support INTEGER, created_at TEXT, PRIMARY KEY(osti_id, mode, field));
    """)
    now=time.strftime("%Y-%m-%dT%H:%M:%SZ")

    if a.emails and os.path.exists(a.emails):
        # map osti_id -> all its (mode) cards so we attach the email to each
        pid_modes={}
        for osti_id, mode in card.execute("SELECT osti_id, mode FROM cards"):
            pid_modes.setdefault(str(osti_id), set()).add(mode)
        batch=[]; n_pap=0
        for line in open(a.emails):
            try: r=json.loads(line)
            except: continue
            pid=str(r.get("osti_id"))
            best=r.get("best"); allem=r.get("emails",[])
            if not best: continue
            val=best if len(allem)<=1 else best+" ; "+" ; ".join(e for e in allem if e!=best)
            modes=pid_modes.get(pid)
            if not modes: continue
            n_pap+=1
            for mode in modes:
                batch.append((pid, mode, "author_email", val, r.get("source","parse"), len(allem), now))
            if len(batch)>=5000:
                stg.executemany("INSERT OR REPLACE INTO card_enrichment VALUES(?,?,?,?,?,?,?)", batch)
                batch=[]
        if batch:
            stg.executemany("INSERT OR REPLACE INTO card_enrichment VALUES(?,?,?,?,?,?,?)", batch)
        stg.commit()
        print(f"emails applied: {n_pap} papers -> staging")

    # report staging totals
    tot=stg.execute("SELECT COUNT(*) FROM card_enrichment").fetchone()[0]
    print("staging rows total:", tot)
    for src,n in stg.execute("SELECT source,COUNT(*) FROM card_enrichment GROUP BY source"):
        print(f"   {src}: {n}")
    stg.close(); card.close()

if __name__ == "__main__":
    main()
