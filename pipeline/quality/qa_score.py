#!/usr/bin/env python3.13
"""
qa_score.py  --  Structural (non-LLM) quality scorer for the OSTI x-CARD corpus.

This is STAGE 4 (SCORE) of the QA pipeline described in QA_RUBRIC.md, restricted
to the deterministic / structural sub-scores that do NOT require an LLM:
    - completeness  (required-field fill rate, per mode, §4)
    - provenance    (weighted presence of the §3 provenance block)
    - qa_funding    (did we capture a real funding/agency/award value?)
    - qa_entity_count (# distinct named entities detected for the mode)
    - qa_missing_fields (csv of required fields that were UNKNOWN/empty)
    - qa_tier       (HIGH / MEDIUM / LOW by the structural thresholds below)

Grounding (the LLM-judged sub-score) is deliberately LEFT OUT of this pass;
the qa_grounding column is not written here. Tiering therefore uses the
structural thresholds only:

    HIGH   : completeness >= 0.85 AND provenance >= 0.80
    MEDIUM : completeness >= 0.60 AND provenance >= 0.50
    LOW    : everything else

Design notes:
  * NON-DESTRUCTIVE. Backs up the DB first, only ADDs qa_* columns, never
    touches the existing card content.
  * IDEMPOTENT. Re-running recomputes every row; qa_version='v1'.
  * ROBUST PARSING. ~0% of these cards parse as strict YAML (LaTeX/backslash
    bodies). We therefore use a line-based `key: value` extractor as the
    primary path, with an optional YAML parse as a best-effort bonus. A parse
    error on any single row never crashes the run.
"""

import os
import re
import sqlite3
import shutil
import sys
import time
from datetime import datetime, timezone

DB   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "osti_cards.sqlite")
BAK  = DB + ".bak-qascore"
QA_VERSION = "v4"
BATCH = 2000

# Optional strict YAML (best-effort only; corpus mostly won't parse).
try:
    import yaml as _yaml
    _HAVE_YAML = True
except Exception:
    _HAVE_YAML = False


# --------------------------------------------------------------------------
# Required-field lists per mode (QA_RUBRIC.md §4).
# Fields marked "(if any)" in the rubric are CONDITIONAL -- they only apply
# when the card actually has that artifact -- so they are excluded from the
# completeness denominator (counting them would unfairly penalize a dataset
# that legitimately has no repository_url). All others are required.
#
# Each required field maps to a list of acceptable YAML key alternatives;
# a field is "filled" if ANY alternative key is present-and-non-UNKNOWN.
# --------------------------------------------------------------------------
REQUIRED = {
    "data": {
        "dataset_name":      ["dataset_name"],
        "dataset_type":      ["dataset_type"],
        "short_description": ["short_description"],
        "full_description":  ["full_description"],
        "provider":          ["provider", "organization", "provided_by"],
        "license":           ["license"],
        "access":            ["access", "availability", "access_availability"],
        "size_format":       ["size", "format", "size_format", "file_formats", "data_modality"],
        "version":           ["version"],
    },
    "model": {
        "model_name":        ["model_name"],
        "model_type":        ["model_type"],
        "short_description": ["short_description"],
        "full_description":  ["full_description"],
        "developed_by":      ["developed_by"],
        "base_model":        ["base_model"],
        "license":           ["license"],
        "version":           ["version"],
    },
    "instrument": {
        "instrument_name":   ["instrument_name"],
        "instrument_type":   ["instrument_type", "instrument_scale"],
        "role_in_paper":     ["role_in_paper"],
        "short_description": ["short_description"],
        "full_description":  ["full_description"],
        "operated_by":       ["operated_by"],
        "manufacturer":      ["manufacturer"],
        "model_number":      ["model_number"],
        "location":          ["location", "facility", "lab"],
    },
    "facility": {
        "facility_name":     ["facility_name"],
        "facility_type":     ["facility_type"],
        "role_in_paper":     ["role_in_paper"],
        "short_description": ["short_description"],
        "full_description":  ["full_description"],
        "operator":          ["operator"],
        "location":          ["location"],
        "capabilities":      ["capabilities"],
        "access":            ["access"],
    },
    "simulation": {
        "code_name":         ["code_name"],
        "code_type":         ["code_type"],
        "role_in_paper":     ["role_in_paper"],
        "short_description": ["short_description"],
        "full_description":  ["full_description"],
        "developed_by":      ["developed_by"],
        "version":           ["version"],
        "license":           ["license"],
        "inputs_physics":    ["inputs", "physics", "inputs_physics"],
    },
    "workflow": {
        "workflow_name":     ["workflow_name"],
        "workflow_type":     ["workflow_type"],
        "short_description": ["short_description"],
        "full_description":  ["full_description"],
        "developed_by":      ["developed_by"],
        "provider":          ["provider", "organization"],
        "steps_components":  ["steps", "components", "steps_components"],
        "language":          ["language"],
        "license":           ["license"],
    },
    "agent": {
        "agent_name":        ["agent_name"],
        "agent_type":        ["agent_type"],
        "short_description": ["short_description"],
        "full_description":  ["full_description"],
        "developed_by":      ["developed_by"],
        "provider":          ["provider", "organization"],
        "capabilities":      ["capabilities"],
        "architecture":      ["architecture"],
        "license":           ["license"],
    },
}

# Primary name-key per mode, used for entity counting.
NAME_KEY = {
    "data": "dataset_name",
    "model": "model_name",
    "instrument": "instrument_name",
    "facility": "facility_name",
    "simulation": "code_name",
    "workflow": "workflow_name",
    "agent": "agent_name",
}
# Detection flag per mode.
DETECT_KEY = {m: f"{('code' if m=='simulation' else ('dataset' if m=='data' else m))}_detected"
              for m in NAME_KEY}
# fix: simulation uses 'simulation_detected', data uses 'dataset_detected'
DETECT_KEY["simulation"] = "simulation_detected"
DETECT_KEY["data"] = "dataset_detected"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


# --------------------------------------------------------------------------
# Line-based key: value extractor (robust to un-parseable YAML).
# Returns a dict mapping top-level-ish keys -> raw value string (first match
# wins per key).  Also returns the raw text for substructure probing.
# --------------------------------------------------------------------------
_KV_RE = re.compile(r'^\s*([A-Za-z0-9_]+)\s*:\s*(.*)$')

def line_scan(text):
    kv = {}
    for line in text.splitlines():
        m = _KV_RE.match(line)
        if not m:
            continue
        key = m.group(1)
        val = m.group(2).strip()
        if key not in kv:          # first occurrence wins
            kv[key] = val
    return kv


def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        v = v[1:-1]
    return v.strip()


def is_filled(raw_val):
    """A value is 'filled' if non-empty, not bare UNKNOWN/N/A/null/none/[].
    A bracketed [derived: ...] value COUNTS as filled."""
    if raw_val is None:
        return False
    v = _unquote(raw_val)
    if v == "":
        return False
    # Block openers ("|" , ">" , "{" , "[") that start a multi-line/compound
    # value are considered filled only if there is content on the line.
    low = v.lower().strip()
    if low in ("unknown", "n/a", "na", "none", "null", "~", "[]", "{}", '""', "''", "[unknown]"):
        return False
    # provider: {organization: "UNKNOWN", ...} -> look inside
    # handled by caller via inline-dict probing; here treat a block scalar
    # opener as "needs deeper check" -> caller passes the resolved value.
    return True


def inline_dict_value(raw_val, subkeys):
    """For a value like {organization: "X", url: "UNKNOWN"} return the first
    sub-value for any of subkeys, else None."""
    for sk in subkeys:
        m = re.search(sk + r'\s*:\s*("?)([^",}\n]*)\1', raw_val)
        if m:
            return m.group(2).strip()
    return None


def field_filled(kv, alts, raw_text):
    """Is this required field filled under any of its alternative keys?"""
    for key in alts:
        if key in kv:
            val = kv[key]
            # inline dict? e.g. provider: {organization: "X"}
            if val.startswith("{"):
                # require organization/url/name-ish sub-value to be real
                sub = inline_dict_value(val, ["organization", "url", "name", "value"])
                if sub is not None and is_filled(sub):
                    return True
                # if the dict had no recognized subkey but has real content, accept
                if sub is None and re.search(r':\s*"?(?!UNKNOWN)[^",}\s][^",}]*', val):
                    return True
                continue
            # block scalar opener (| or >) -> content is on following lines;
            # treat as filled (there is a body).
            if val in ("|", ">", "|-", ">-", "|+", ">+"):
                return True
            # list opener on same line: [ ... ]
            if is_filled(val):
                return True
        else:
            # key may appear as a nested key inside provider{} etc.
            # cheap fallback: search "key:" anywhere with a real value
            m = re.search(r'(?m)^\s*' + re.escape(key) + r'\s*:\s*(.+)$', raw_text)
            if m and is_filled(m.group(1)):
                return True
    return False


def compute_completeness(mode, kv, raw_text):
    req = REQUIRED.get(mode)
    if not req:
        return None, []
    total = len(req)
    missing = []
    filled = 0
    for field, alts in req.items():
        if field_filled(kv, alts, raw_text):
            filled += 1
        else:
            missing.append(field)
    comp = filled / total if total else 0.0
    return comp, missing


def has_authors_with_name(kv, raw_text):
    # authors: block followed by - {name: "..."} OR 'authors' column handled separately
    if re.search(r'(?m)name\s*:\s*"(?!UNKNOWN)[^"]+"', raw_text):
        return True
    return False


def has_author_email(raw_text, emails_col):
    if emails_col and EMAIL_RE.search(emails_col):
        return True
    # real email inside an email: field (not UNKNOWN)
    if re.search(r'email\s*:\s*"?(?!UNKNOWN)[^",}\s]*@', raw_text):
        return True
    if EMAIL_RE.search(raw_text):
        return True
    return False


def has_affiliation(raw_text):
    if re.search(r'affiliation\s*:\s*"(?!UNKNOWN)[^"]+"', raw_text):
        return True
    if re.search(r'(?m)^\s*(affiliations|primary_lab)\s*:\s*"?(?!UNKNOWN)\S', raw_text):
        return True
    return False


# ---- actionable-URL detector (Rick's HIGH gate component #1) --------------
# "The primary issue is if the x-card has some URL that can be operated on by
#  an agent."  An actionable URL points at an operable artifact/resource — a
#  repo, dataset landing page, software/tool site, API, model card, facility
#  portal — NOT a bare DOI citation, license text, or publisher paywall stub.
_URL_RE = re.compile(r'https?://[^\s"\'<>)\]}]+', re.I)
_ACTIONABLE_KEYS = (
    "repository_url", "repo_url", "code_url", "source_url", "github",
    "dataset_url", "data_url", "download_url", "landing_page", "url",
    "homepage", "website", "documentation_url", "docs_url", "api_url",
    "endpoint", "model_url", "huggingface", "zenodo", "project_url",
)
# domains that are citations/paywalls/boilerplate, not operable artifacts
_NONACTIONABLE_HOST = (
    "doi.org", "dx.doi.org", "osti.gov/biblio", "osti.gov/servlets",
    "creativecommons.org", "ntis.gov", "adsabs.harvard.edu",
    "crossref.org", "orcid.org", "ands.org.au",
)
# hosts that are strongly operable
_ACTIONABLE_HOST = (
    "github.com", "gitlab.com", "bitbucket.org", "zenodo.org",
    "huggingface.co", "figshare.com", "dryad", "osf.io", "sourceforge",
    "nersc.gov", "anl.gov", "ornl.gov", "pnnl.gov", "lbl.gov", "llnl.gov",
    "readthedocs", "pypi.org", "conda", "dataverse", "kaggle.com",
    "ncbi.nlm.nih.gov", "ebi.ac.uk", "materialsproject.org",
)

def actionable_url(kv, raw_text):
    """Return a URL string if the card carries an agent-operable URL, else None.
    Non-destructive heuristic: prefer URLs under known actionable keys; accept
    any http(s) URL whose host is clearly operable; reject pure citation hosts."""
    cand = []
    # 1) URLs sitting under an actionable key
    for k in _ACTIONABLE_KEYS:
        for m in re.finditer(r'(?mi)^\s*'+re.escape(k)+r'\s*:\s*"?([^",}\n]+)', raw_text):
            v = m.group(1).strip().strip('"\'')
            if v.lower().startswith("http") and is_filled(v):
                cand.append(v)
    # 2) any URL in the body
    body_urls = _URL_RE.findall(raw_text)
    def host(u):
        u2 = re.sub(r'^https?://', '', u, flags=re.I).lower()
        return u2.split('/')[0]
    # reject obvious non-actionable
    def ok(u):
        h = host(u); full = u.lower()
        if any(b in full for b in _NONACTIONABLE_HOST):
            return False
        if any(a in h for a in _ACTIONABLE_HOST):
            return True
        return None  # unknown host — only count if under an actionable key
    for u in cand:
        if ok(u) is not False:
            return u
    for u in body_urls:
        if ok(u) is True:
            return u
    return None


FUND_KEYS = ("funding", "funding_text", "funded_by", "agency", "award",
             "grant", "sponsor", "acknowledgement", "acknowledgements",
             "funding_acknowledgement")

def funding_signal(kv, raw_text):
    """Return 1 if a real (non-UNKNOWN) funding/agency/award value is captured."""
    # funded_by inside provider inline dict
    m = re.search(r'funded_by\s*:\s*"?([^",}\n]*)', raw_text)
    if m and is_filled(m.group(1)):
        return 1
    for k in FUND_KEYS:
        # as a top-level-ish key with an inline value
        m = re.search(r'(?mi)^\s*' + k + r'\s*:\s*(.+)$', raw_text)
        if m:
            v = m.group(1).strip()
            if v in ("|", ">"):          # block text present
                return 1
            if v.startswith("{") or v.startswith("["):
                # list/dict of funding entries -> check for a real agency/award
                nxt = raw_text[m.end():m.end()+600]
                blk = v + nxt
                if re.search(r'(agency|award|name)\s*:\s*"?(?!UNKNOWN)[^",}\s]', blk):
                    return 1
                continue
            if is_filled(v):
                return 1
        else:
            # list form:  funding:\n  - agency: "DOE"
            m2 = re.search(r'(?mi)^\s*' + k + r'\s*:\s*$', raw_text)
            if m2:
                blk = raw_text[m2.end():m2.end()+600]
                if re.search(r'(agency|award|name)\s*:\s*"?(?!UNKNOWN)[^",}\s]', blk):
                    return 1
    return 0


def compute_provenance(kv, raw_text, mode, title_col, authors_col, emails_col):
    # v3 weights: core bibliographic metadata (osti_id + title + authors + affiliation)
    # sums to 0.80 and clears the HIGH gate ON ITS OWN. Funding and author-email are
    # explicit BONUSES toward 1.0 — a nice-to-have, never a quality gate (per Rick:
    # "funding acknowledgement should not be a trigger for quality difference").
    score = 0.0
    # osti_id 0.15 -- always present (row key)
    score += 0.15
    # title 0.15
    title_ok = bool((title_col and title_col.strip())) or ("title" in kv and is_filled(kv["title"]))
    if title_ok:
        score += 0.15
    # authors block with >=1 name 0.25
    if (authors_col and authors_col.strip()) or has_authors_with_name(kv, raw_text):
        score += 0.25
    # >=1 affiliation 0.25
    if has_affiliation(raw_text):
        score += 0.25
    # funding present: BONUS 0.15 (demoted from gate — not required for HIGH)
    fund = funding_signal(kv, raw_text)
    if fund:
        score += 0.15
    # >=1 author email: BONUS 0.05
    if has_author_email(raw_text, emails_col):
        score += 0.05
    return round(min(score, 1.0), 4), fund


def compute_entity_count(mode, kv, raw_text):
    name_key = NAME_KEY.get(mode)
    if not name_key:
        return 0
    # count distinct non-UNKNOWN name values
    vals = set()
    for m in re.finditer(r'(?m)^\s*' + re.escape(name_key) + r'\s*:\s*(.+)$', raw_text):
        v = _unquote(m.group(1))
        if is_filled(v):
            vals.add(v.lower())
    if vals:
        return len(vals)
    # detected flag true but no explicit name -> count as 1
    det = DETECT_KEY.get(mode)
    if det and re.search(r'(?m)^\s*' + re.escape(det) + r'\s*:\s*true', raw_text, re.I):
        return 1
    return 0


def assign_tier(comp, prov):
    # legacy weighted gate — retained for callers that still pass (comp,prov)
    if comp is None:
        return "LOW"
    if comp >= 0.85 and prov >= 0.75:
        return "HIGH"
    if comp >= 0.60 and prov >= 0.50:
        return "MEDIUM"
    return "LOW"


def assign_tier_v4(has_url, has_names, has_email, comp, url_ok=None):
    """Rick's HIGH definition (2026-10-17):
       HIGH = validated/actionable URL + names + emails + key technical info.
       Funding is NOT a factor. All four components must be present.
       'key technical info present' == completeness >= 0.70.
       'validated URL' == an actionable URL that is ALSO live-verified
         (url_ok=True). When url_ok is None the validator has not run yet, so we
         accept presence-of-actionable-URL alone (graceful pre-validation mode);
         once url_validate.py has populated qa_url_status, pass url_ok and HIGH
         requires a live URL.
       MEDIUM = solid card missing one HIGH component.
       LOW    = sparse / non-rubric / missing most technical info."""
    if comp is None:
        return "LOW"
    tech_ok = comp >= 0.70
    url_component = bool(has_url) if url_ok is None else (bool(has_url) and bool(url_ok))
    if url_component and has_names and has_email and tech_ok:
        return "HIGH"
    comps = sum([bool(has_url), bool(has_names), bool(has_email)])
    if comp >= 0.50 or (comps >= 2 and comp >= 0.35):
        return "MEDIUM"
    return "LOW"


def score_row(mode, title, authors, emails, yaml_text, url_status=None):
    raw = yaml_text or ""
    kv = line_scan(raw)

    if mode == "bvbrc_mapping" or mode not in REQUIRED:
        # Not a rubric card type. Score structurally: no required fields ->
        # completeness is undefined; provenance still from columns. Tier LOW.
        prov, fund = compute_provenance(kv, raw, mode, title, authors, emails)
        return {
            "qa_tier": "LOW",
            "qa_completeness": 0.0,
            "qa_provenance": prov,
            "qa_funding": fund,
            "qa_entity_count": 0,
            "qa_missing_fields": "ALL(non-rubric-mode)",
            "qa_has_url": 0, "qa_has_names": 0, "qa_has_email": 0, "qa_url": "",
        }

    comp, missing = compute_completeness(mode, kv, raw)
    prov, fund = compute_provenance(kv, raw, mode, title, authors, emails)
    ents = compute_entity_count(mode, kv, raw)
    # ---- Rick's four HIGH components (2026-10-17) ----
    url = actionable_url(kv, raw)
    has_url = bool(url)
    has_names = bool((authors and authors.strip())) or has_authors_with_name(kv, raw)
    has_email = has_author_email(raw, emails)
    # validated-URL: only pass url_ok once the validator has populated status
    url_ok = None if url_status is None else (url_status == "ok")
    tier = assign_tier_v4(has_url, has_names, has_email, comp, url_ok=url_ok)
    return {
        "qa_tier": tier,
        "qa_completeness": round(comp, 4),
        "qa_provenance": prov,
        "qa_funding": fund,
        "qa_entity_count": ents,
        "qa_missing_fields": ",".join(missing),
        "qa_has_url": 1 if has_url else 0,
        "qa_has_names": 1 if has_names else 0,
        "qa_has_email": 1 if has_email else 0,
        "qa_url": url or "",
    }


# --------------------------------------------------------------------------
# Schema setup
# --------------------------------------------------------------------------
QA_COLUMNS = [
    ("qa_tier",         "TEXT"),
    ("qa_completeness", "REAL"),
    ("qa_provenance",   "REAL"),
    ("qa_funding",      "INTEGER"),
    ("qa_entity_count", "INTEGER"),
    ("qa_missing_fields", "TEXT"),
    ("qa_has_url",       "INTEGER DEFAULT 0"),
    ("qa_has_names",     "INTEGER DEFAULT 0"),
    ("qa_has_email",     "INTEGER DEFAULT 0"),
    ("qa_url",           "TEXT"),
    ("qa_version",      "TEXT DEFAULT 'v1'"),
    ("qa_scored_at",    "TEXT"),
]

def ensure_schema(conn):
    existing = {r[1] for r in conn.execute("PRAGMA table_info(cards)")}
    for name, decl in QA_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE cards ADD COLUMN {name} {decl}")
            print(f"  + added column {name} {decl}")
    conn.commit()


def backup_db():
    if os.path.exists(BAK):
        print(f"Backup already exists, skipping: {BAK}")
        return
    print(f"Backing up DB -> {BAK} ...")
    shutil.copy2(DB, BAK)
    print(f"  backup done ({os.path.getsize(BAK):,} bytes)")


def main():
    if not os.path.exists(DB):
        sys.exit(f"DB not found: {DB}")

    backup_db()

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    ensure_schema(conn)

    total = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    print(f"Scoring {total:,} cards (version={QA_VERSION}) ...")

    now = datetime.now(timezone.utc).isoformat()
    have_cols = {r[1] for r in conn.execute("PRAGMA table_info(cards)")}
    url_sel = ", qa_url_status" if "qa_url_status" in have_cols else ""
    read = conn.cursor()
    read.execute(f"SELECT id, mode, title, authors, emails, yaml{url_sel} FROM cards")

    write = conn.cursor()
    n = 0
    t0 = time.time()
    batch = []
    UPDATE = ("UPDATE cards SET qa_tier=?, qa_completeness=?, qa_provenance=?, "
              "qa_funding=?, qa_entity_count=?, qa_missing_fields=?, "
              "qa_has_url=?, qa_has_names=?, qa_has_email=?, qa_url=?, "
              "qa_version=?, qa_scored_at=? WHERE id=?")

    while True:
        rows = read.fetchmany(5000)
        if not rows:
            break
        for row in rows:
            if url_sel:
                rid, mode, title, authors, emails, yaml_text, url_status = row
            else:
                rid, mode, title, authors, emails, yaml_text = row
                url_status = None
            try:
                s = score_row(mode, title, authors, emails, yaml_text, url_status=url_status)
            except Exception as e:
                # never let one row crash the run
                s = {"qa_tier": "LOW", "qa_completeness": 0.0, "qa_provenance": 0.0,
                     "qa_funding": 0, "qa_entity_count": 0,
                     "qa_missing_fields": f"SCORE_ERROR:{type(e).__name__}",
                     "qa_has_url": 0, "qa_has_names": 0, "qa_has_email": 0, "qa_url": ""}
            batch.append((s["qa_tier"], s["qa_completeness"], s["qa_provenance"],
                          s["qa_funding"], s["qa_entity_count"], s["qa_missing_fields"],
                          s.get("qa_has_url",0), s.get("qa_has_names",0),
                          s.get("qa_has_email",0), s.get("qa_url",""),
                          QA_VERSION, now, rid))
            n += 1
            if len(batch) >= BATCH:
                write.executemany(UPDATE, batch)
                conn.commit()
                batch.clear()
                if n % 20000 == 0 or n == total:
                    rate = n / (time.time() - t0)
                    print(f"  scored {n:,}/{total:,}  ({rate:,.0f} rows/s)")
    if batch:
        write.executemany(UPDATE, batch)
        conn.commit()
        batch.clear()

    print(f"Scored {n:,} rows in {time.time()-t0:,.1f}s")

    print("Creating index on qa_tier ...")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_qa_tier ON cards(qa_tier)")
    conn.commit()
    conn.close()

    report()


def report():
    """Read-only verification + stats."""
    uri = f"file:{DB}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    print("\n================ VERIFICATION & STATS ================")
    total = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    nulls = conn.execute("SELECT COUNT(*) FROM cards WHERE qa_tier IS NULL").fetchone()[0]
    print(f"total rows: {total:,}   rows with NULL qa_tier: {nulls:,}")

    print("\nTier distribution:")
    dist = {}
    for tier, cnt in conn.execute(
            "SELECT qa_tier, COUNT(*) FROM cards GROUP BY qa_tier ORDER BY COUNT(*) DESC"):
        dist[tier] = cnt
        print(f"  {tier:8s} {cnt:>8,}  ({100*cnt/total:5.1f}%)")

    print("\nPer-mode: mean completeness / mean provenance / funding-capture rate:")
    print(f"  {'mode':14s} {'n':>8s} {'mean_comp':>10s} {'mean_prov':>10s} {'fund_rate':>10s}")
    for mode, n, mc, mp, fr in conn.execute(
            "SELECT mode, COUNT(*), AVG(qa_completeness), AVG(qa_provenance), "
            "AVG(qa_funding) FROM cards GROUP BY mode ORDER BY mode"):
        print(f"  {mode:14s} {n:>8,} {mc:>10.3f} {mp:>10.3f} {fr:>10.3f}")

    print("\nPer-mode tier breakdown:")
    for mode in [r[0] for r in conn.execute("SELECT DISTINCT mode FROM cards ORDER BY mode")]:
        parts = []
        for tier in ("HIGH", "MEDIUM", "LOW"):
            c = conn.execute("SELECT COUNT(*) FROM cards WHERE mode=? AND qa_tier=?",
                             (mode, tier)).fetchone()[0]
            parts.append(f"{tier}={c:,}")
        print(f"  {mode:14s} " + "  ".join(parts))
    conn.close()
    print("======================================================")


if __name__ == "__main__":
    main()
