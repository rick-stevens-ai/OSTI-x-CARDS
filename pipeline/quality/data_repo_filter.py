#!/usr/bin/env python3
"""
data_repo_filter.py  -- value-add filter: tag data cards that reference a
resolvable DATA REPOSITORY (known-host allowlist + URL validity; no live probing).

Scope (Rick, 2026-10-09): DATA cards only, deterministic allowlist-based, fast.
Non-invasive: adds derived columns to the existing cards table; does NOT touch
the extraction pipeline or sync_cards.py output.

Adds columns:
  has_data_repo   INTEGER  (1 if >=1 artifact/url resolves to an allowlisted data repo host)
  data_repo_hosts TEXT     (semicolon-joined distinct matched repo hosts, e.g. "zenodo.org; rcsb.org")
  data_repo_urls  TEXT     (semicolon-joined the actual matching URLs)

Idempotent: safe to re-run; recomputes for all data cards each pass.
"""
import sqlite3, re, sys
from urllib.parse import urlparse

DB = sys.argv[1] if len(sys.argv) > 1 else \
    "/Users/stevens/code/osti-cards-site/data/osti_cards.sqlite"

# --- Curated DATA-REPOSITORY host allowlist (grounded in actual card host distribution
#     + standard data repositories). Match is on registrable-domain SUFFIX so
#     subdomains (adc.arm.gov, data.desi.lbl.gov, img.jgi.doe.gov) are covered. ---
DATA_REPO_SUFFIXES = {
    # General-purpose data/artifact repositories
    "zenodo.org", "figshare.com", "dryad.org", "datadryad.org", "osf.io",
    "dataverse.org", "harvard.edu/dataverse", "pangaea.de", "icpsr.umich.edu",
    "data.mendeley.com", "mendeley.com", "dataone.org", "re3data.org",
    "opendata.aws", "kaggle.com", "huggingface.co",
    # Life sciences / bio
    "rcsb.org", "ncbi.nlm.nih.gov", "ebi.ac.uk", "ddbj.nig.ac.jp",
    "massive.ucsd.edu", "proteomecentral.proteomexchange.org", "proteomexchange.org",
    "metabolomicsworkbench.org", "genome.jgi.doe.gov", "img.jgi.doe.gov",
    "jgi.doe.gov", "mg-rast.org", "ebi.ac.uk/metagenomics", "patricbrc.org",
    "bv-brc.org", "bvbrc.org", "uniprot.org", "pdb.org", "wwpdb.org",
    "emdataresource.org", "geo.ncbi.nlm.nih.gov",
    # Chem / materials / crystallography
    "ccdc.cam.ac.uk", "materialsproject.org", "nomad-lab.eu", "nomad-coe.eu",
    "crystallography.net", "cod.crystallography.net",
    # Physics / astro / nuclear
    "hepdata.net", "nndc.bnl.gov", "sdss.org", "legacysurvey.org",
    "desi.lbl.gov", "cosmos.esa.int", "irsa.ipac.caltech.edu", "mast.stsci.edu",
    "cxc.harvard.edu", "heasarc.gsfc.nasa.gov", "zooniverse.org",
    # Earth / climate / energy (DOE + federal data portals)
    "arm.gov", "adc.arm.gov", "cdiac.esd.ornl.gov", "cdiac.ornl.gov",
    "esgf.llnl.gov", "esgf-node.llnl.gov", "daac.ornl.gov", "earthdata.nasa.gov",
    "ngee-arctic.ornl.gov", "ess-dive.lbl.gov", "edx.netl.doe.gov",
    "osti.gov/dataexplorer", "datadiscovery.nrel.gov", "openei.org",
    # US gov statistical / open-data
    "data.gov", "eia.gov", "census.gov", "epa.gov/data", "noaa.gov",
    "ncei.noaa.gov", "usgs.gov", "sciencebase.gov", "nist.gov/srd",
    # HPC centers hosting released datasets
    "portal.nersc.gov", "nersc.gov",
}

# Hosts that are DEFINITELY NOT data repos (publisher landing / resolver / identity)
# — explicit denylist so an ambiguous suffix match can't sneak these in.
DENY_SUFFIXES = {
    "doi.org", "dx.doi.org", "orcid.org", "creativecommons.org",
    "pubs.acs.org", "sciencedirect.com", "nature.com", "springer.com",
    "link.springer.com", "wiley.com", "onlinelibrary.wiley.com", "mdpi.com",
    "frontiersin.org", "biomedcentral.com", "tandfonline.com", "sagepub.com",
    "iop.org", "iopscience.iop.org", "aps.org", "journals.aps.org",
    "ntis.gov", "escholarship.org", "arxiv.org", "researchgate.net",
    "energy.gov", "osti.gov",   # osti.gov landing (dataexplorer handled in allowlist)
    "plos.org", "elsevier.com", "cell.com", "pnas.org", "jstor.org",
}

URL_SPLIT = re.compile(r"[;,\s]+")
SCHEME_RE = re.compile(r"^https?://", re.I)
BAREHOST_RE = re.compile(r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/|$)", re.I)
REPORT_NUM_RE = re.compile(r"^[A-Z]{2,}[-/]?\d", re.I)  # PNNL-13396, SAND2011-...


def norm_host(piece: str):
    """Return lowercase netloc for a URL-ish token, or None if it's not a real URL."""
    p = piece.strip()
    if not p or p.upper() == "UNKNOWN":
        return None, None
    if p.lower().startswith("doi:") or REPORT_NUM_RE.match(p) and "/" not in p and "." not in p:
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
        # Py3.13 urlparse raises on malformed URLs (e.g. bad IPv6 bracket)
        return None, None
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None, None
    return host, url


def match_repo(host: str, full: str):
    """True if host (or path-qualified full) matches an allowlisted data-repo and not denied."""
    # denylist first
    for d in DENY_SUFFIXES:
        if host == d or host.endswith("." + d):
            # allow the explicit allowlist path-qualified exceptions (e.g. osti.gov/dataexplorer)
            break
    # allowlist: suffix match on host, OR path-qualified entries matched against full url
    for s in DATA_REPO_SUFFIXES:
        if "/" in s:  # path-qualified entry like "osti.gov/dataexplorer"
            if s in full.lower():
                return True
        else:
            if host == s or host.endswith("." + s):
                # re-check deny for bare-host collisions (osti.gov denied unless path-qualified above)
                denied = any(host == d or host.endswith("." + d) for d in DENY_SUFFIXES)
                if denied:
                    continue
                return True
    return False


def classify(arts: str, url: str):
    cands = []
    for blob in (arts or "", url or ""):
        for piece in URL_SPLIT.split(blob):
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
            urls.append(full)
    return hosts, urls


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    # add columns if missing
    cols = {r[1] for r in cur.execute("PRAGMA table_info(cards)")}
    for c, t in (("has_data_repo", "INTEGER DEFAULT 0"),
                 ("data_repo_hosts", "TEXT DEFAULT ''"),
                 ("data_repo_urls", "TEXT DEFAULT ''")):
        if c not in cols:
            cur.execute(f"ALTER TABLE cards ADD COLUMN {c} {t}")
    con.commit()

    rows = cur.execute(
        "SELECT rowid, artifact_urls, url FROM cards WHERE mode LIKE '%data%'"
    ).fetchall()
    hit = 0
    for rowid, arts, u in rows:
        hosts, urls = classify(arts, u)
        flag = 1 if hosts else 0
        hit += flag
        cur.execute(
            "UPDATE cards SET has_data_repo=?, data_repo_hosts=?, data_repo_urls=? WHERE rowid=?",
            (flag, "; ".join(hosts), "; ".join(dict.fromkeys(urls)), rowid),
        )
    con.commit()
    # index for fast filtering
    cur.execute("CREATE INDEX IF NOT EXISTS idx_has_data_repo ON cards(has_data_repo)")
    con.commit()
    print(f"data cards scanned: {len(rows)}  |  with resolvable data repo: {hit}")
    # top matched hosts for sanity
    top = cur.execute(
        "SELECT data_repo_hosts FROM cards WHERE has_data_repo=1"
    ).fetchall()
    from collections import Counter
    cnt = Counter()
    for (h,) in top:
        for x in h.split("; "):
            if x:
                cnt[x] += 1
    print("top matched repo hosts:")
    for h, n in cnt.most_common(20):
        print(f"  {n:5d}  {h}")
    con.close()


if __name__ == "__main__":
    main()
