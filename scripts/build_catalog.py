#!/usr/bin/env python3
"""
build_catalog.py — bulk-pull OSTI metadata into the catalog, time-range parameterized.

Pulls from the public OSTI JSON API (https://www.osti.gov/api/v1/records) for a set of
DOE labs over a year range, UPSERTing into the `papers` table. Run init_catalog.py first.

Usage:
  build_catalog.py --year-start 2000 --year-end 2026 [--labs ANL,ORNL] [--catalog PATH]

Catalog path: --catalog | $OSTI_CATALOG | data/catalog.sqlite
Contact email in the User-Agent: $OSTI_CONTACT_EMAIL (OSTI etiquette; default generic).

Strategy:
  - Iterate (lab, year), paginate /records with rows=500 by publication_date range.
  - Resumable & idempotent: UPSERT on osti_id; re-running is safe.
  - Rate limit: DELAY between requests, MAX_RETRY with exp backoff on 429/5xx.
"""
import argparse, json, os, sqlite3, time, urllib.request, urllib.error
from datetime import datetime
from pathlib import Path

API = "https://www.osti.gov/api/v1/records"
CONTACT = os.environ.get("OSTI_CONTACT_EMAIL", "osti-xcards@example.org")
UA = f"Mozilla/5.0 (OSTI-x-CARDS catalog builder; {CONTACT})"
TIMEOUT = 60
DELAY = 1.5
MAX_RETRY = 5
ROWS = 500

SC_LABS = [
    "Argonne National Laboratory",
    "Brookhaven National Laboratory",
    "Fermi National Accelerator Laboratory",
    "Lawrence Berkeley National Laboratory",
    "Oak Ridge National Laboratory",
    "Pacific Northwest National Laboratory",
    "Princeton Plasma Physics Laboratory",
    "SLAC National Accelerator Laboratory",
    "Thomas Jefferson National Accelerator Facility",
    "Ames National Laboratory",
]

LAB_MAP = {
    "argonne": "ANL", "brookhaven": "BNL", "fermi national": "FNAL", "fermilab": "FNAL",
    "lawrence berkeley": "LBNL", "lbnl": "LBNL", "oak ridge": "ORNL", "ornl": "ORNL",
    "pacific northwest": "PNNL", "pnnl": "PNNL", "princeton plasma": "PPPL", "pppl": "PPPL",
    "slac": "SLAC", "jefferson lab": "JLab", "thomas jefferson": "JLab", "jlab": "JLab",
    "ames laboratory": "AMES", "ames national": "AMES",
}

def catalog_path(cli=None):
    return Path(cli or os.environ.get("OSTI_CATALOG") or "data/catalog.sqlite").expanduser()

def fetch_page(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(MAX_RETRY):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                total = resp.headers.get("X-Total-Count") or resp.headers.get("x-total-count")
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
                return body, int(total) if total and total.isdigit() else None
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRY - 1:
                back = 2 ** (attempt + 2)
                print(f"    [{e.code}] retry {attempt+1}/{MAX_RETRY} in {back}s", flush=True)
                time.sleep(back); continue
            raise
        except urllib.error.URLError:
            if attempt < MAX_RETRY - 1:
                back = 2 ** (attempt + 2)
                print(f"    [URLError] retry {attempt+1}/{MAX_RETRY} in {back}s", flush=True)
                time.sleep(back); continue
            raise
    raise RuntimeError(f"Exhausted {MAX_RETRY} retries on {url}")

def normalize_lab(orgs_list):
    if not orgs_list:
        return None
    org_str = " ".join(str(o) for o in orgs_list[:3]).lower()
    for key, short in LAB_MAP.items():
        if key in org_str:
            return short
    return None

def upsert_paper(conn, rec, source):
    osti_id = str(rec.get("osti_id") or "")
    if not osti_id:
        return False
    pub = rec.get("publication_date") or rec.get("publicationDate") or ""
    year = None
    if pub:
        if "-" in pub and len(pub) >= 4:
            try: year = int(pub.split("-")[0])
            except: pass
        elif "/" in pub and len(pub) >= 10:
            try: year = int(pub.split("/")[-1][:4])
            except: pass
    orgs = rec.get("research_orgs") or []
    primary_lab = normalize_lab(orgs)
    now = datetime.utcnow().isoformat() + "Z"
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM papers WHERE osti_id=?", (osti_id,))
    exists = cur.fetchone() is not None
    vals = (rec.get("doi"), rec.get("title"), pub or None, year, rec.get("product_type"),
            rec.get("journal_name"), rec.get("journal_volume"), rec.get("journal_issue"),
            json.dumps(orgs, ensure_ascii=False), primary_lab,
            json.dumps(rec.get("sponsor_orgs") or [], ensure_ascii=False),
            json.dumps(rec.get("authors") or [], ensure_ascii=False),
            json.dumps(rec.get("subjects") or [], ensure_ascii=False),
            rec.get("description"), rec.get("doe_contract_number"),
            json.dumps(rec.get("links") or [], ensure_ascii=False), now, source)
    if exists:
        cur.execute("""UPDATE papers SET
            doi=COALESCE(?,doi), title=COALESCE(?,title), publication_date=COALESCE(?,publication_date),
            year=COALESCE(?,year), product_type=COALESCE(?,product_type), journal_name=COALESCE(?,journal_name),
            journal_volume=COALESCE(?,journal_volume), journal_issue=COALESCE(?,journal_issue),
            research_orgs_json=?, primary_lab=COALESCE(?,primary_lab), sponsor_orgs_json=?,
            authors_json=?, subjects_json=?, description=COALESCE(?,description),
            doe_contract_number=COALESCE(?,doe_contract_number), osti_links_json=?,
            catalog_last_seen_ts=?, metadata_source=? WHERE osti_id=?""", vals + (osti_id,))
        return False
    cur.execute("""INSERT INTO papers
        (doi,title,publication_date,year,product_type,journal_name,journal_volume,journal_issue,
         research_orgs_json,primary_lab,sponsor_orgs_json,authors_json,subjects_json,description,
         doe_contract_number,osti_links_json,catalog_last_seen_ts,metadata_source,
         osti_id,catalog_first_seen_ts)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", vals + (osti_id, now))
    return True

def pull_lab_year(conn, lab, year, source_tag):
    lab_q = lab.replace(" ", "+")
    added = updated = 0
    page = 1
    while True:
        url = (f"{API}?research_org={lab_q}&publication_date_start=01/01/{year}"
               f"&publication_date_end=12/31/{year}&rows={ROWS}&page={page}")
        try:
            body, _ = fetch_page(url)
        except Exception as e:
            print(f"  ERROR {lab} {year} page {page}: {e}", flush=True)
            return added, updated, "error"
        if not body:
            break
        for rec in body:
            if upsert_paper(conn, rec, source_tag): added += 1
            else: updated += 1
        conn.commit()
        if len(body) < ROWS:
            break
        page += 1
        time.sleep(DELAY)
    return added, updated, "ok"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year-start", type=int, default=2000)
    ap.add_argument("--year-end", type=int, default=2026)
    ap.add_argument("--labs", type=str, default=None, help="Comma-separated subset (short names, e.g. ANL,ORNL)")
    ap.add_argument("--catalog", default=None)
    ap.add_argument("--run-type", default="initial_catalog")
    args = ap.parse_args()

    conn = sqlite3.connect(catalog_path(args.catalog))
    started = datetime.utcnow().isoformat() + "Z"
    cur = conn.cursor()
    cur.execute("INSERT INTO refresh_runs (started_ts, run_type, params_json) VALUES (?,?,?)",
                (started, args.run_type, json.dumps(vars(args))))
    run_id = cur.lastrowid
    conn.commit()

    labs = SC_LABS
    if args.labs:
        wanted = {x.strip().upper() for x in args.labs.split(",")}
        labs = [L for L in SC_LABS if any(w in L.upper() for w in wanted)
                or normalize_lab([L]) in wanted]

    total_added = total_updated = total_errors = 0
    t0 = time.time()
    for lab in labs:
        for year in range(args.year_start, args.year_end + 1):
            t_ly = time.time()
            print(f"\n[{lab} {year}]", flush=True)
            added, updated, status = pull_lab_year(conn, lab, year, f"osti_api_bulk_run{run_id}")
            if status == "error": total_errors += 1
            total_added += added; total_updated += updated
            print(f"  +{added} new, ~{updated} updated ({time.time()-t_ly:.1f}s); "
                  f"running {total_added}+/{total_updated}~ in {(time.time()-t0)/60:.1f}min", flush=True)
            time.sleep(DELAY)

    cur.execute("UPDATE refresh_runs SET ended_ts=?, records_added=?, records_updated=?, errors=? WHERE run_id=?",
                (datetime.utcnow().isoformat()+"Z", total_added, total_updated, total_errors, run_id))
    conn.commit()
    print(f"\n=== Run {run_id}: +{total_added} new, ~{total_updated} updated, {total_errors} errors, "
          f"{(time.time()-t0)/60:.1f}min ===")
    print(f"  Total papers: {conn.execute('SELECT COUNT(*) FROM papers').fetchone()[0]}")
    conn.close()

if __name__ == "__main__":
    main()
