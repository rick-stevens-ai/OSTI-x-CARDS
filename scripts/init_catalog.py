#!/usr/bin/env python3
"""
Initialize the OSTI corpus catalog SQLite DB.
Idempotent: safe to run repeatedly.

Catalog path resolution (first that is set wins):
  1. --catalog CLI arg
  2. $OSTI_CATALOG env var
  3. ./data/catalog.sqlite (repo-local default)
"""
import argparse, os, sqlite3
from pathlib import Path

def catalog_path(cli=None):
    p = cli or os.environ.get("OSTI_CATALOG") or "data/catalog.sqlite"
    return Path(p).expanduser()

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    osti_id TEXT PRIMARY KEY,
    doi TEXT,
    title TEXT,
    publication_date TEXT,
    year INTEGER,
    product_type TEXT,
    journal_name TEXT,
    journal_volume TEXT,
    journal_issue TEXT,
    research_orgs_json TEXT,
    primary_lab TEXT,
    sponsor_orgs_json TEXT,
    authors_json TEXT,
    subjects_json TEXT,
    description TEXT,
    doe_contract_number TEXT,
    osti_links_json TEXT,
    catalog_first_seen_ts TEXT,
    catalog_last_seen_ts TEXT,
    metadata_source TEXT,
    has_pdf INTEGER DEFAULT 0,
    canonical_pdf_path TEXT,
    canonical_source TEXT,
    canonical_size INTEGER,
    canonical_sha256 TEXT,
    needs_pdf_fetch INTEGER DEFAULT 0,
    needs_ocr INTEGER DEFAULT 1,
    md_path TEXT,
    mmd_path TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(year);
CREATE INDEX IF NOT EXISTS idx_papers_lab ON papers(primary_lab);
CREATE INDEX IF NOT EXISTS idx_papers_has_pdf ON papers(has_pdf);
CREATE INDEX IF NOT EXISTS idx_papers_needs_pdf ON papers(needs_pdf_fetch);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);

CREATE TABLE IF NOT EXISTS file_instances (
    instance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    osti_id TEXT,
    source TEXT,
    path TEXT UNIQUE,
    size INTEGER,
    sha256 TEXT,
    extracted_title TEXT,
    title_match_score REAL,
    first_seen_ts TEXT,
    last_verified_ts TEXT,
    is_canonical INTEGER DEFAULT 0,
    canonical_decision_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fi_osti ON file_instances(osti_id);
CREATE INDEX IF NOT EXISTS idx_fi_source ON file_instances(source);
CREATE INDEX IF NOT EXISTS idx_fi_canonical ON file_instances(is_canonical);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    osti_id TEXT,
    decision_type TEXT,
    chosen_instance_id INTEGER,
    rejected_instance_ids_json TEXT,
    rationale TEXT,
    method TEXT,
    confidence REAL,
    inputs_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_dec_osti ON decisions(osti_id);
CREATE INDEX IF NOT EXISTS idx_dec_type ON decisions(decision_type);

CREATE TABLE IF NOT EXISTS refresh_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts TEXT,
    ended_ts TEXT,
    run_type TEXT,
    params_json TEXT,
    records_added INTEGER DEFAULT 0,
    records_updated INTEGER DEFAULT 0,
    pdfs_added INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS pdf_fetch_log (
    fetch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    osti_id TEXT,
    run_id INTEGER,
    ts TEXT,
    url TEXT,
    http_status INTEGER,
    bytes INTEGER,
    sha256 TEXT,
    saved_path TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_pfl_osti ON pdf_fetch_log(osti_id);

-- Papers whose PURL fetch failed; picked up by the DOI-based recovery worker.
CREATE TABLE IF NOT EXISTS recovery_queue (
    osti_id TEXT PRIMARY KEY,
    reason TEXT,
    enqueued_ts TEXT,
    enqueued_run_id INTEGER,
    status TEXT DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    last_attempt_ts TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_rq_status ON recovery_queue(status);
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=None, help="catalog.sqlite path (else $OSTI_CATALOG or data/catalog.sqlite)")
    args = ap.parse_args()
    cat = catalog_path(args.catalog)
    cat.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cat)
    conn.executescript(SCHEMA)
    conn.commit()
    print(f"Initialized catalog at {cat}")
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print(f"  Tables: {tables}")
    print(f"  Size: {cat.stat().st_size} bytes")
    conn.close()

if __name__ == "__main__":
    main()
