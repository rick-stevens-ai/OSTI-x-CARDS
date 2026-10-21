#!/usr/bin/env python3
"""
entity_resolve.py — shared entity-resolution engine for enrichment + SuperCards.

Reads the live card DB (read-only), extracts the primary named entity per card,
normalizes names, detects acronym<->expansion aliases from in-card co-occurrence,
and clusters cards into entity groups.

Output: entities.sqlite (sibling DB) with:
  entity(entity_id, mode, canonical_name, norm_name, member_count)
  entity_member(entity_id, osti_id, raw_name)
  alias(norm_short, norm_long, mode)        -- acronym <-> expansion

ZERO farm impact. Deterministic blocking/clustering (LLM adjudication is a later pass).
"""
import sqlite3, re, os, collections, sys, time

DB = os.path.expanduser("~/code/osti-cards-site/data/osti_cards.sqlite")
OUT = os.path.expanduser("~/code/osti-cards-site/data/entities.sqlite")

NAME_KEY = {"data":"dataset_name","model":"model_name","instrument":"instrument_name",
            "facility":"facility_name","simulation":"code_name","workflow":"workflow_name",
            "agent":"agent_name"}

STOP = {"unknown","n/a","none","null","",".","-","the","a","various","multiple","custom",
        "in-house","in house","proprietary","see text"}

def norm(s):
    s = (s or "").strip().strip('"\'').lower()
    s = re.sub(r'\bv?\d+(\.\d+)+\w*\b', '', s)      # strip version tokens
    s = re.sub(r'[^a-z0-9 .+&/-]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip(' .-')
    return s

# acronym<->expansion detector: "Advanced Photon Source (APS)" or "APS (Advanced Photon Source)"
PAREN = re.compile(r'([A-Za-z][A-Za-z0-9 .&/-]{3,}?)\s*\(([A-Z][A-Za-z0-9&/-]{1,12})\)')
PAREN2 = re.compile(r'\b([A-Z][A-Za-z0-9&/-]{1,12})\s*\(([A-Za-z][A-Za-z0-9 .&/-]{3,}?)\)')

def acronym_ok(short, longs):
    # first letters of long words ~ short (loose)
    letters = [w[0] for w in re.findall(r'[A-Za-z]+', longs)]
    return len(short) >= 2 and len(letters) >= 2

def main():
    t0=time.time()
    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    if os.path.exists(OUT): os.remove(OUT)
    out = sqlite3.connect(OUT)
    out.executescript("""
      CREATE TABLE entity(entity_id INTEGER PRIMARY KEY, mode TEXT, canonical_name TEXT,
                          norm_name TEXT, member_count INTEGER);
      CREATE TABLE entity_member(entity_id INTEGER, osti_id TEXT, raw_name TEXT);
      CREATE TABLE alias(norm_short TEXT, norm_long TEXT, mode TEXT);
      CREATE INDEX idx_em_ent ON entity_member(entity_id);
      CREATE INDEX idx_em_osti ON entity_member(osti_id);
      CREATE INDEX idx_ent_norm ON entity(mode, norm_name);
    """)

    groups = collections.defaultdict(list)   # (mode,norm) -> [(osti_id, raw_name)]
    raw_names = collections.defaultdict(collections.Counter)  # (mode,norm) -> Counter(raw)
    aliases = collections.Counter()          # (mode, norm_short, norm_long) -> n
    rows=0
    for mode, osti_id, ya in src.execute(
        "SELECT mode, osti_id, yaml FROM cards WHERE mode IN "
        "('data','model','instrument','facility','simulation','workflow','agent')"):
        rows += 1
        nk = NAME_KEY.get(mode)
        if not nk: continue
        m = re.search(r'(?mi)^\s*'+re.escape(nk)+r'\s*:\s*(.+)$', ya or '')
        if not m: continue
        raw = m.group(1).strip().strip('"\'')
        nn = norm(raw)
        if nn in STOP or len(nn) < 2: continue
        groups[(mode,nn)].append((str(osti_id), raw))
        raw_names[(mode,nn)][raw]+=1
        # alias detection from the card body (both name field and description)
        for rx in (PAREN, PAREN2):
            for mm in rx.finditer(ya or ''):
                g1,g2 = mm.group(1), mm.group(2)
                # decide which is short
                a,b = (g2,g1) if len(g2)<len(g1) else (g1,g2)
                short,longs = (a,b) if a.isupper() or len(a)<=12 else (b,a)
                if acronym_ok(short, longs):
                    aliases[(mode, norm(short), norm(longs))]+=1

    # write entities
    eid=0; mem_rows=[]; ent_rows=[]
    for (mode,nn), members in groups.items():
        eid+=1
        canon = raw_names[(mode,nn)].most_common(1)[0][0]
        ent_rows.append((eid, mode, canon, nn, len(members)))
        for osti_id, raw in members:
            mem_rows.append((eid, osti_id, raw))
    out.executemany("INSERT INTO entity VALUES(?,?,?,?,?)", ent_rows)
    out.executemany("INSERT INTO entity_member VALUES(?,?,?)", mem_rows)
    al_rows=[(s,l,mode) for (mode,s,l),n in aliases.items() if n>=1 and s!=l]
    out.executemany("INSERT INTO alias VALUES(?,?,?)", [(s,l,m) for (s,l,m) in al_rows])
    out.commit()

    multi = sum(1 for r in ent_rows if r[4]>1)
    print(f"cards scanned: {rows}")
    print(f"entities: {len(ent_rows)}  multi-mention: {multi}  "
          f"cards in multi: {sum(r[4] for r in ent_rows if r[4]>1)}")
    print(f"aliases detected: {len(al_rows)}")
    print(f"-> {OUT}  ({time.time()-t0:.1f}s)")
    out.close(); src.close()

if __name__ == "__main__":
    main()
