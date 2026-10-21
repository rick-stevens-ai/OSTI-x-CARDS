#!/usr/bin/env python3
"""
code_repo_filter.py  -- value-add filter: tag model/simulation/workflow/agent
cards that reference a resolvable CODE / SOFTWARE-ARTIFACT repository
(known-host allowlist + URL validity; no live probing).

Analogous to data_repo_filter.py but for SOFTWARE artifacts rather than data.
Scope: model, simulation, workflow, agent cards. Deterministic allowlist-based.
Non-invasive: adds derived columns to the existing cards table; does NOT touch
the extraction pipeline or sync_cards.py output.

Sources scanned per card:
  - artifact_urls column
  - url column
  - any *_repository / code_url / code_repo / source_code fields inside the YAML
    (e.g. code_repository, model_repository, software_repository)

Adds columns:
  has_code_repo    INTEGER  (1 if >=1 artifact/url resolves to an allowlisted code host)
  code_repo_hosts  TEXT     (semicolon-joined distinct matched repo hosts, e.g. "github.com; zenodo.org")
  code_repo_urls   TEXT     (semicolon-joined the actual matching URLs)

Idempotent: safe to re-run; recomputes for all in-scope cards each pass.
Backs up the DB to <DB>.bak-coderepo once (if not already present) before writing.
"""
import sqlite3, re, sys, os, shutil
from urllib.parse import urlparse

DB = sys.argv[1] if len(sys.argv) > 1 else \
    "/Users/stevens/code/osti-cards-site/data/osti_cards.sqlite"

IN_SCOPE_MODES = ("model", "simulation", "workflow", "agent")

# --- Curated CODE / SOFTWARE-REPOSITORY host allowlist. Match is on registrable-domain
#     SUFFIX so subdomains (raw.githubusercontent.com, git.sr.ht, …) are covered.
#     zenodo.org is included because it is a very common software-release archive. ---
CODE_REPO_SUFFIXES = {
    # Forges / general code hosting
    "github.com", "githubusercontent.com", "github.io",
    "gitlab.com", "gitlab.io",
    "bitbucket.org", "bitbucket.io",
    "codeberg.org", "sourceforge.net", "sf.net",
    "gitee.com", "savannah.gnu.org", "savannah.nongnu.org",
    "launchpad.net", "sr.ht",
    # Package registries / artifact hubs for software
    "pypi.org", "pypi.python.org", "cran.r-project.org",
    "hub.docker.com", "huggingface.co",
    "zenodo.org",  # software releases archived with DOI
    # Apache git hosting
    "git.apache.org", "gitbox.apache.org",
}

# Path-qualified allowlist entries (matched against the full lowercased URL).
CODE_REPO_PATH_SUFFIXES = {
    "apache.org/repos",      # svn/git repos under apache.org
    "git.code.sf.net",
}

# Hosts that are DEFINITELY NOT code repos (publisher / resolver / identity / data-only)
# — explicit denylist so an ambiguous suffix match can't sneak these in.
DENY_SUFFIXES = {
    "doi.org", "dx.doi.org", "orcid.org", "creativecommons.org",
    "pubs.acs.org", "sciencedirect.com", "nature.com", "springer.com",
    "link.springer.com", "wiley.com", "onlinelibrary.wiley.com", "mdpi.com",
    "frontiersin.org", "biomedcentral.com", "tandfonline.com", "sagepub.com",
    "iop.org", "iopscience.iop.org", "aps.org", "journals.aps.org",
    "ntis.gov", "escholarship.org", "arxiv.org", "researchgate.net",
    "energy.gov", "osti.gov", "ntis.gov",
    "plos.org", "elsevier.com", "cell.com", "pnas.org", "jstor.org",
}

URL_SPLIT = re.compile(r"[;,\s]+")
SCHEME_RE = re.compile(r"^https?://", re.I)
BAREHOST_RE = re.compile(r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/|$)", re.I)
REPORT_NUM_RE = re.compile(r"^[A-Z]{2,}[-/]?\d", re.I)  # PNNL-13396, SAND2011-...

# YAML fields that may hold a code/software repository URL
YAML_FIELD_RE = re.compile(
    r'^\s*(?:[a-z0-9_]*(?:code|software|source)_?(?:repository|repo|url)'
    r'|repository|repo_url|git_url|source_code)\s*:\s*(.+?)\s*$', re.I)
# also catch git.* subdomains generically (git.example.org)
GIT_SUBDOMAIN_RE = re.compile(r"^git\.", re.I)


def norm_host(piece: str):
    """Return (lowercase netloc, full url) for a URL-ish token, or (None, None)."""
    p = piece.strip().strip('"').strip("'")
    if not p or p.upper() == "UNKNOWN":
        return None, None
    if p.lower().startswith("doi:") or (REPORT_NUM_RE.match(p) and "/" not in p and "." not in p):
        return None, None
    if SCHEME_RE.match(p):
        url = p
    elif BAREHOST_RE.match(p):
        url = "http://" + p
    else:
        return None, None
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return None, None
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None, None
    return host, url


def match_repo(host: str, full: str):
    """True if host (or path-qualified full) matches an allowlisted code-repo and not denied."""
    denied = any(host == d or host.endswith("." + d) for d in DENY_SUFFIXES)
    fl = full.lower()
    # path-qualified entries first (these can override a denied bare host)
    for s in CODE_REPO_PATH_SUFFIXES:
        if s in fl:
            return True
    if denied:
        return False
    # suffix match on host
    for s in CODE_REPO_SUFFIXES:
        if host == s or host.endswith("." + s):
            return True
    # generic git.* subdomain (e.g. git.someuniversity.edu)
    if GIT_SUBDOMAIN_RE.match(host):
        return True
    return False


def yaml_repo_candidates(yaml_text: str):
    """Pull values of *_repository / code_url / source_code fields from raw YAML text."""
    out = []
    for line in (yaml_text or "").splitlines():
        m = YAML_FIELD_RE.match(line)
        if m:
            out.append(m.group(1))
    return out


def classify(arts: str, url: str, yaml_text: str):
    cands = []
    for blob in (arts or "", url or ""):
        for piece in URL_SPLIT.split(blob):
            if piece.strip():
                cands.append(piece.strip())
    for val in yaml_repo_candidates(yaml_text):
        for piece in URL_SPLIT.split(val):
            if piece.strip():
                cands.append(piece.strip())
    hosts, urls = [], []
    for p in cands:
        host, full = norm_host(p)
        if not host:
            continue
        if match_repo(host, full):
            if host not in hosts:
                hosts.append(host)
            if full not in urls:
                urls.append(full)
    return hosts, urls


def main():
    # back up the DB once before writing
    bak = DB + ".bak-coderepo"
    if not os.path.exists(bak):
        shutil.copy2(DB, bak)
        print(f"backed up DB -> {bak}")
    else:
        print(f"backup already exists -> {bak} (skipping)")

    con = sqlite3.connect(DB)
    cur = con.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(cards)")}
    for c, t in (("has_code_repo", "INTEGER DEFAULT 0"),
                 ("code_repo_hosts", "TEXT DEFAULT ''"),
                 ("code_repo_urls", "TEXT DEFAULT ''")):
        if c not in cols:
            cur.execute(f"ALTER TABLE cards ADD COLUMN {c} {t}")
    con.commit()

    placeholders = ",".join("?" * len(IN_SCOPE_MODES))
    rows = cur.execute(
        f"SELECT rowid, artifact_urls, url, yaml FROM cards WHERE mode IN ({placeholders})",
        IN_SCOPE_MODES,
    ).fetchall()
    hit = 0
    for rowid, arts, u, yaml_text in rows:
        hosts, urls = classify(arts, u, yaml_text)
        flag = 1 if hosts else 0
        hit += flag
        cur.execute(
            "UPDATE cards SET has_code_repo=?, code_repo_hosts=?, code_repo_urls=? WHERE rowid=?",
            (flag, "; ".join(hosts), "; ".join(dict.fromkeys(urls)), rowid),
        )
    con.commit()
    cur.execute("CREATE INDEX IF NOT EXISTS idx_has_code_repo ON cards(has_code_repo)")
    con.commit()
    print(f"in-scope cards scanned: {len(rows)}  |  with resolvable code repo: {hit}")
    from collections import Counter
    cnt = Counter()
    for (h,) in cur.execute("SELECT code_repo_hosts FROM cards WHERE has_code_repo=1"):
        for x in h.split("; "):
            if x:
                cnt[x] += 1
    print("top matched code-repo hosts:")
    for h, n in cnt.most_common(20):
        print(f"  {n:5d}  {h}")
    con.close()


if __name__ == "__main__":
    main()
