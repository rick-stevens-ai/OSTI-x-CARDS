#!/usr/bin/env python3
"""
supercards_build.py — build the SuperCards collection from entities.sqlite + the card DB.

A SuperCard = one canonical record per real-world resource (tool/dataset/instrument/
facility/model/workflow/agent), synthesized by EVIDENCE-WEIGHTED CONSENSUS over every
member card. Zero-hallucination: asserts only values present in >=1 member card.

Output: supercards.sqlite (sibling DB, keeps per-paper cards pristine).
  supercard(super_id, mode, canonical_name, member_count, first_year, last_year,
            labs, n_with_url, n_with_email, consensus_json)
  supercard_member(super_id, osti_id)

consensus_json holds, per field: {value, support, dissent:[...]} — conflicts preserved.
"""
import sqlite3, re, os, json, collections, time

CARDDB = os.path.expanduser("~/code/osti-cards-site/data/osti_cards.sqlite")
ENTDB  = os.path.expanduser("~/code/osti-cards-site/data/entities.sqlite")
OUT    = os.path.expanduser("~/code/osti-cards-site/data/supercards.sqlite")

# fields worth consolidating per mode (scalar consensus)
CONSENSUS_FIELDS = {
    "data": ["dataset_type","provider","license","access","version","format"],
    "model": ["model_type","developed_by","base_model","license","version"],
    "instrument": ["instrument_type","operated_by","manufacturer","model_number","location"],
    "facility": ["facility_type","operator","location","capabilities","access"],
    "simulation": ["code_type","developed_by","version","license","language"],
    "workflow": ["workflow_type","developed_by","provider","language","license"],
    "agent": ["agent_type","developed_by","provider","architecture","license"],
}
URL_RE = re.compile(r'https?://[^\s"\'<>)\]]+')
EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')

def kv_get(yaml_text, key):
    m = re.search(r'(?mi)^\s*'+re.escape(key)+r'\s*:\s*(.+)$', yaml_text or '')
    if not m: return None
    v = m.group(1).strip().strip('"\'')
    if v.lower() in ("unknown","n/a","none","null","",'""',"[]","{}"): return None
    return v

def main():
    t0=time.time()
    if not os.path.exists(ENTDB):
        print("entities.sqlite missing; run entity_resolve.py first"); return
    ent = sqlite3.connect(f"file:{ENTDB}?mode=ro", uri=True)
    card = sqlite3.connect(f"file:{CARDDB}?mode=ro", uri=True)
    if os.path.exists(OUT): os.remove(OUT)
    out = sqlite3.connect(OUT)
    out.executescript("""
      CREATE TABLE supercard(super_id TEXT PRIMARY KEY, mode TEXT, canonical_name TEXT,
        member_count INTEGER, first_year INTEGER, last_year INTEGER, labs TEXT,
        n_with_url INTEGER, n_with_email INTEGER, urls TEXT, emails TEXT, consensus_json TEXT);
      CREATE TABLE supercard_member(super_id TEXT, osti_id TEXT);
      CREATE INDEX idx_sm_super ON supercard_member(super_id);
      CREATE INDEX idx_sc_mode ON supercard(mode, member_count);
    """)

    # iterate entities with >=2 members (true SuperCards); singletons optional later
    ents = ent.execute("SELECT entity_id, mode, canonical_name, member_count FROM entity "
                       "WHERE member_count>=2 ORDER BY member_count DESC").fetchall()
    n_sc=0
    for entity_id, mode, canon, mc in ents:
        members = [r[0] for r in ent.execute(
            "SELECT osti_id FROM entity_member WHERE entity_id=?", (entity_id,))]
        if not members: continue
        # pull member card yamls + year (lab parsed from yaml; no primary_lab column)
        q = ("SELECT osti_id, year, yaml FROM cards WHERE mode=? AND osti_id IN (%s)"
             % ",".join("?"*len(members)))
        rows = card.execute(q, [mode]+members).fetchall()
        if not rows: continue
        fields = CONSENSUS_FIELDS.get(mode, [])
        fvals = {f: collections.Counter() for f in fields}
        urls=collections.Counter(); emails=collections.Counter()
        years=[]; labs=collections.Counter(); n_url=0; n_em=0
        for osti_id, year, ya in rows:
            if year:
                try: years.append(int(year))
                except: pass
            lab = kv_get(ya, "primary_lab") or kv_get(ya, "operated_by") or kv_get(ya, "operator")
            if lab: labs[lab[:60]]+=1
            for f in fields:
                v = kv_get(ya, f)
                if v: fvals[f][v.lower()[:120]]+=1
            us=set(URL_RE.findall(ya or '')); 
            if us: n_url+=1
            for u in us: urls[u]+=1
            es=set(e for e in EMAIL_RE.findall(ya or '') if not e.lower().endswith((".png",".jpg")))
            if es: n_em+=1
            for e in es: emails[e]+=1
        consensus={}
        for f in fields:
            if fvals[f]:
                top=fvals[f].most_common()
                consensus[f]={"value":top[0][0],"support":top[0][1],
                              "dissent":[{"value":v,"support":n} for v,n in top[1:4]]}
        super_id=f"super-{mode}-{entity_id}"
        out.execute("INSERT INTO supercard VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (super_id, mode, canon, len(rows),
             min(years) if years else None, max(years) if years else None,
             json.dumps([l for l,_ in labs.most_common(5)]),
             n_url, n_em,
             json.dumps([u for u,_ in urls.most_common(8)]),
             json.dumps([e for e,_ in emails.most_common(8)]),
             json.dumps(consensus)))
        out.executemany("INSERT INTO supercard_member VALUES(?,?)",
            [(super_id, r[0]) for r in rows])
        n_sc+=1
        if n_sc % 2000 == 0:
            out.commit(); print(f"  {n_sc} supercards...", flush=True)
    out.commit()
    tot_mem=out.execute("SELECT SUM(member_count) FROM supercard").fetchone()[0]
    print(f"SuperCards: {n_sc}  covering {tot_mem} member cards")
    print(f"-> {OUT}  ({time.time()-t0:.1f}s)")
    for mode,n,mm in out.execute("SELECT mode,COUNT(*),SUM(member_count) FROM supercard GROUP BY mode ORDER BY 3 DESC"):
        print(f"   {mode:11s} {n:6d} supercards / {mm} members")
    out.close(); ent.close(); card.close()

if __name__ == "__main__":
    main()
