#!/usr/bin/env python3
"""
enrich_crosscard.py — M-T2: transfer consensus field values from a SuperCard to its
member cards that are MISSING that field. Zero-hallucination: a transferred value was
observed in >=1 sibling card describing the SAME resolved entity.

Writes to a STAGING table card_enrichment (never mutates the cards table):
  card_enrichment(osti_id, mode, field, value, source, support, created_at)
source = 'cross-card'. Only transfers when consensus support >= MIN_SUPPORT and the
target card genuinely lacks the field. Idempotent (REPLACE on (osti_id,mode,field)).
"""
import sqlite3, re, os, json, time

CARDDB = os.path.expanduser("~/code/osti-cards-site/data/osti_cards.sqlite")
SUPERDB= os.path.expanduser("~/code/osti-cards-site/data/supercards.sqlite")
MIN_SUPPORT = 2        # value must appear in >=2 sibling cards to transfer

def kv_has(yaml_text, key):
    m = re.search(r'(?mi)^\s*'+re.escape(key)+r'\s*:\s*(.+)$', yaml_text or '')
    if not m: return False
    v = m.group(1).strip().strip('"\'')
    return v.lower() not in ("unknown","n/a","none","null","",'""',"[]","{}")

def main():
    t0=time.time()
    if not os.path.exists(SUPERDB):
        print("supercards.sqlite missing; run supercards_build.py first"); return
    sup = sqlite3.connect(f"file:{SUPERDB}?mode=ro", uri=True)
    card= sqlite3.connect(f"file:{CARDDB}?mode=ro", uri=True)
    # staging DB lives alongside cards but as its own file to keep cards pristine
    stg = sqlite3.connect(os.path.expanduser("~/code/osti-cards-site/data/card_enrichment.sqlite"))
    stg.executescript("""
      CREATE TABLE IF NOT EXISTS card_enrichment(
        osti_id TEXT, mode TEXT, field TEXT, value TEXT, source TEXT,
        support INTEGER, created_at TEXT,
        PRIMARY KEY(osti_id, mode, field));
    """)
    now=time.strftime("%Y-%m-%dT%H:%M:%SZ")
    n_transfer=0; n_super=0
    rows = sup.execute("SELECT super_id, mode, consensus_json FROM supercard").fetchall()
    for super_id, mode, cjson in rows:
        try: consensus=json.loads(cjson or "{}")
        except: continue
        # fields with enough support
        fillable={f:d for f,d in consensus.items()
                  if d.get("support",0) >= MIN_SUPPORT and d.get("value")}
        if not fillable: continue
        n_super+=1
        members=[r[0] for r in sup.execute(
            "SELECT osti_id FROM supercard_member WHERE super_id=?", (super_id,))]
        if not members: continue
        q=("SELECT osti_id, yaml FROM cards WHERE mode=? AND osti_id IN (%s)"
           % ",".join("?"*len(members)))
        batch=[]
        for osti_id, ya in card.execute(q, [mode]+members):
            for f,d in fillable.items():
                if not kv_has(ya, f):
                    batch.append((osti_id, mode, f, d["value"], "cross-card", d["support"], now))
        if batch:
            stg.executemany(
              "INSERT OR REPLACE INTO card_enrichment VALUES(?,?,?,?,?,?,?)", batch)
            n_transfer+=len(batch)
        if n_super % 2000 == 0:
            stg.commit(); print(f"  {n_super} supercards, {n_transfer} transfers...", flush=True)
    stg.commit()
    # stats
    aff=stg.execute("SELECT COUNT(DISTINCT osti_id) FROM card_enrichment WHERE source='cross-card'").fetchone()[0]
    print(f"cross-card transfers: {n_transfer} field-fills across {aff} distinct cards")
    print(f"from {n_super} supercards with >= {MIN_SUPPORT}-support consensus")
    for f,n in stg.execute("SELECT field,COUNT(*) FROM card_enrichment WHERE source='cross-card' GROUP BY field ORDER BY 2 DESC").fetchall()[:12]:
        print(f"   {f:18s} {n}")
    print(f"({time.time()-t0:.1f}s) -> card_enrichment.sqlite")
    stg.close(); sup.close(); card.close()

if __name__ == "__main__":
    main()
