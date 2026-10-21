#!/usr/bin/env python3
"""
enrich_merge.py — fold card_enrichment staging into the live card DB.

Runs AFTER build_db (like the other QA post-processors). For each staged enrichment:
  - author_email -> fill the cards.emails column if empty (bracketed provenance in yaml note)
  - technical fields (license/version/location/...) -> appended into the card yaml as a
    bracketed [enriched:<source>] line IF the card lacks that field.

NON-DESTRUCTIVE: never overwrites an existing real value; only fills genuine gaps.
Adds an `enriched` INTEGER column flag + `enriched_fields` TEXT列 listing what was added.
Idempotent: re-running recomputes from staging; the bracketed-line guard prevents dupes.
"""
import sqlite3, os, re, time, json

DB  = os.path.expanduser("~/code/osti-cards-site/data/osti_cards.sqlite")
STG = os.path.expanduser("~/code/osti-cards-site/data/card_enrichment.sqlite")

def kv_has(yaml_text, key):
    m = re.search(r'(?mi)^\s*'+re.escape(key)+r'\s*:\s*(.+)$', yaml_text or '')
    if not m: return False
    v = m.group(1).strip().strip('"\'')
    return v.lower() not in ("unknown","n/a","none","null","",'""',"[]","{}")

def main():
    if not os.path.exists(STG):
        print("no staging DB; skip"); return
    con=sqlite3.connect(DB, timeout=120)
    # schema additions
    cols={r[1] for r in con.execute("PRAGMA table_info(cards)")}
    if "enriched" not in cols:
        con.execute("ALTER TABLE cards ADD COLUMN enriched INTEGER DEFAULT 0")
    if "enriched_fields" not in cols:
        con.execute("ALTER TABLE cards ADD COLUMN enriched_fields TEXT")
    con.commit()

    stg=sqlite3.connect(f"file:{STG}?mode=ro", uri=True)
    # pull all enrichments grouped by (osti_id, mode)
    bym={}
    for osti_id, mode, field, value, source, support in stg.execute(
        "SELECT osti_id, mode, field, value, source, support FROM card_enrichment"):
        bym.setdefault((str(osti_id),mode),[]).append((field,value,source,support))
    stg.close()

    n_email=0; n_field=0; n_cards=0
    for (osti_id, mode), items in bym.items():
        rows=con.execute("SELECT rowid, yaml, emails FROM cards WHERE osti_id=? AND mode=?",
                         (osti_id, mode)).fetchall()
        if not rows: continue
        for rowid, ya, emails in rows:
            ya = ya or ""
            added=[]
            new_emails = emails
            for field, value, source, support in items:
                if field=="author_email":
                    # fill emails column if empty
                    if not (emails and "@" in emails):
                        best = value.split(" ; ")[0]
                        new_emails = value
                        # add a bracketed note line into yaml for provenance
                        if "author_email:" not in ya:
                            ya += f'\nauthor_email: "[enriched:{source}] {best}"'
                        added.append("author_email")
                        n_email+=1
                else:
                    if not kv_has(ya, field):
                        tag=f'\n{field}: "[enriched:{source} support={support}] {value}"'
                        ya += tag
                        added.append(field)
                        n_field+=1
            if added:
                con.execute("UPDATE cards SET yaml=?, emails=?, enriched=1, enriched_fields=? WHERE rowid=?",
                            (ya, new_emails, ",".join(sorted(set(added))), rowid))
                n_cards+=1
    con.commit()
    print(f"enrich_merge: {n_cards} cards enriched  (emails_filled={n_email}, field_fills={n_field})")
    # report
    tot=con.execute("SELECT COUNT(*) FROM cards WHERE enriched=1").fetchone()[0]
    print("cards with enriched=1 total:", tot)
    con.close()

if __name__ == "__main__":
    main()
