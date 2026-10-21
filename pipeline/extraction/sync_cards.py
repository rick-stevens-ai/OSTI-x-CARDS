#!/usr/bin/env python3
"""Sync OSTI x-cards pecans->m1 and (re)build the SQLite FTS5 index.
Real cards only (skips NO_X_FOUND sentinels). Idempotent: safe to run on a timer
while the extraction loop is still producing cards. Runs on m1."""
import os, sys, subprocess, sqlite3, glob, re, json, time
import yaml

HOME = os.path.expanduser("~")
LOCAL_CARDS = os.path.join(HOME, "code/osti-cards-site/data/cards")   # mirror of pecans cards
DB = os.path.join(HOME, "code/osti-cards-site/data/osti_cards.sqlite")
PECANS_GLOB = "/home/stevens/osti_cards_*/cards"   # excludes _capped/_proof via filter below
FILT = ("post-quantum|Warning: Permanently|cesello v|source:|no route|"
        "Connection closed|conda|cryptexd|vulnerable|LIBARCHIVE")

MODES = ["model", "agent", "instrument", "facility", "simulation", "workflow", "data"]
MODE_DIRS = {f"{m}_card_extraction": m for m in MODES}

# Cards from non-year source dirs (e.g. osti_cards_uncovered / gap / good) arrive with a
# source-bucket name instead of a real year. Resolve those to the paper's catalog year so
# they land in the canonical date bins instead of a phantom "uncovered" facet.
OSTI_CATALOG = "/Volumes/SG-1-8TB/osti/catalog/catalog.sqlite"
_YEAR_RE = re.compile(r"^[12][0-9]{3}$")
_cat_con = None
_year_cache = {}

def _catalog():
    global _cat_con
    if _cat_con is None:
        try:
            _cat_con = sqlite3.connect(f"file:{OSTI_CATALOG}?mode=ro", uri=True, timeout=10)
            _cat_con.execute("PRAGMA busy_timeout=8000")
        except Exception as e:
            print(f"[year] catalog unavailable ({e}); non-year buckets kept as-is", flush=True)
            _cat_con = False  # sentinel: tried and failed
    return _cat_con or None

def resolve_year(osti_id, src_bucket):
    """Real year for a card. If src_bucket is already a 4-digit year, keep it.
    Otherwise look the osti_id up in the OSTI catalog; fall back to src_bucket."""
    if _YEAR_RE.match(str(src_bucket)):
        return src_bucket
    if osti_id in _year_cache:
        return _year_cache[osti_id]
    y = src_bucket
    con = _catalog()
    if con is not None:
        try:
            r = con.execute("SELECT year FROM papers WHERE osti_id=?", (osti_id,)).fetchone()
            if r and r[0] and _YEAR_RE.match(str(r[0])[:4]):
                y = str(r[0])[:4]
        except Exception:
            pass
    _year_cache[osti_id] = y
    return y

def clean(s):
    ks = FILT.split("|")
    return "\n".join(l for l in s.splitlines() if not any(k in l for k in ks))

def run(cmd, t=1800):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t)
    return p.returncode, clean((p.stdout or "") + (p.stderr or ""))

def rsync_cards():
    """Pull real card dirs from pecans. Uses cez tar-stream (rsync over cez is flaky).
    Only the canonical osti_cards_<year> dirs (4-digit year), not _capped/_proof."""
    os.makedirs(LOCAL_CARDS, exist_ok=True)
    # discover canonical year dirs on pecans
    rc, out = run("cez ssh pecans 'ls -d /home/stevens/osti_cards_[12][0-9][0-9][0-9] 2>/dev/null'", 120)
    years = []
    for line in out.splitlines():
        m = re.search(r"osti_cards_([12][0-9]{3})$", line.strip())
        if m: years.append(m.group(1))
    years = sorted(set(years))
    print(f"[sync] pecans years: {years}", flush=True)
    # also pull the targeted non-year-named run dirs (uncovered / gap / good)
    extra_dirs = ["uncovered", "gap", "good"]
    rc, uout = run("cez ssh pecans 'ls -d /home/stevens/osti_cards_{uncovered,gap,good} /home/stevens/_recover_out 2>/dev/null'", 60)
    pull_list = [(y, f"osti_cards_{y}") for y in years]
    for ed in extra_dirs:
        if f"osti_cards_{ed}" in uout:
            pull_list.append((ed, f"osti_cards_{ed}"))
    # recovery-run output (md-only re-extraction of sentinel papers)
    if "_recover_out" in uout:
        pull_list.append(("_recover_out", "_recover_out"))
    for y, remote in pull_list:
        # tar just the cards/ subtree, stream to local
        dest = os.path.join(LOCAL_CARDS, y)
        os.makedirs(dest, exist_ok=True)
        cmd = (f"cez ssh pecans 'cd /home/stevens/{remote} && tar -cf - cards 2>/dev/null' "
               f"| tar -xf - -C {dest} 2>/dev/null")
        rc, out = run(cmd, 1800)
        n = len(glob.glob(os.path.join(dest, "cards", "*", "*.card.yaml")))
        print(f"[sync] {y}: {n} card files local", flush=True)
    return [y for y, _ in pull_list]

def is_sentinel(text):
    return bool(re.search(r"NO_[A-Z]+_FOUND", text))

def _regex_field(text, *keys):
    """Scalar field recovery for cards whose YAML failed to parse. Matches
    `key: "value"` or `key: value` on a single line."""
    for k in keys:
        m = re.search(rf'^{re.escape(k)}\s*:\s*"?(.+?)"?\s*$', text, re.M)
        if m:
            v = m.group(1).strip().strip('"').strip()
            if v and v != "UNKNOWN":
                return v
    return None

def parse_card(text):
    """Return (parsed_dict_or_None, title, description, authors_str, emails_str, doi, url, artifact_urls_str, fts_blob)."""
    yaml_ok = True
    try:
        # strip leading comment lines that aren't YAML-breaking; yaml handles # comments fine
        d = yaml.safe_load(text)
        if not isinstance(d, dict):
            d = {}; yaml_ok = False
    except Exception:
        d = {}; yaml_ok = False
    def g(*keys):
        for k in keys:
            v = d.get(k)
            if v not in (None, "", "UNKNOWN"):
                return v
        return None
    # title: various *_name fields. simulation cards use `code_name`.
    NAME_KEYS = ("agent_name","model_name","instrument_name","facility_name",
                 "simulation_name","workflow_name","dataset_name","code_name","name","title")
    title = g(*NAME_KEYS)
    desc = g("short_description","full_description","description","summary")
    if isinstance(desc, str):
        desc = desc.strip()
    # authors
    authors = d.get("authors") or []
    anames, aemails = [], []
    if isinstance(authors, list):
        for a in authors:
            if isinstance(a, dict):
                if a.get("name") and a["name"] != "UNKNOWN": anames.append(str(a["name"]))
                if a.get("email") and a["email"] != "UNKNOWN": aemails.append(str(a["email"]))
            elif isinstance(a, str):
                anames.append(a)
    elif isinstance(authors, str):
        anames.append(authors)
    if not anames:
        db = g("developed_by","created_by","author")
        if isinstance(db, str): anames = [x.strip() for x in db.split(",") if x.strip()]
    doi = g("paper_doi","doi")
    url = g("paper_url","url","homepage")
    # Fallback: YAML failed to parse (unescaped quotes / bad flow mapping). Salvage the
    # scalar fields by regex so the card still renders a real title/description instead of
    # "(Untitled)" with everything blank.
    if not yaml_ok:
        title = title or _regex_field(text, *NAME_KEYS)
        desc = desc or _regex_field(text, "short_description","full_description","description","summary")
        doi = doi or _regex_field(text, "paper_doi","doi")
        url = url or _regex_field(text, "paper_url","url","homepage")
        if not anames:
            db = _regex_field(text, "developed_by","created_by","author")
            if db: anames = [x.strip() for x in db.split(",") if x.strip()]
    # artifact urls
    arts = d.get("artifact_urls") or []
    art_urls = []
    if isinstance(arts, list):
        for a in arts:
            if isinstance(a, dict) and a.get("url"): art_urls.append(str(a["url"]))
            elif isinstance(a, str): art_urls.append(a)
    # FTS blob = flatten the whole thing to text
    fts = text  # raw YAML already carries everything; good enough for FTS
    return (d, title or "", (desc or ""), "; ".join(anames), "; ".join(aemails),
            doi or "", url or "", "; ".join(art_urls), fts)

def build_db(years):
    tmp = DB + ".tmp"
    if os.path.exists(tmp): os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE cards(
        id INTEGER PRIMARY KEY,
        osti_id TEXT, year TEXT, mode TEXT,
        title TEXT, description TEXT,
        authors TEXT, emails TEXT, doi TEXT, url TEXT, artifact_urls TEXT,
        yaml TEXT,
        UNIQUE(osti_id, mode, year)
    );
    CREATE VIRTUAL TABLE cards_fts USING fts5(
        title, description, authors, emails, body,
        content='cards', content_rowid='id'
    );
    """)
    n_real = n_sent = n_err = 0
    for y in years:
        pat = os.path.join(LOCAL_CARDS, y, "cards", "*", "*.card.yaml")
        for fp in glob.glob(pat):
            mode_dir = os.path.basename(os.path.dirname(fp))
            mode = MODE_DIRS.get(mode_dir, mode_dir.replace("_card_extraction",""))
            # OSTI enclave only: skip any non-OSTI card mode (e.g. bvbrc_mapping).
            # BV-BRC lives in its own separate enclave DB; see bvbrc-cards-site.
            if mode not in MODES:
                continue
            osti_id = os.path.basename(fp).split(".")[0]
            try:
                text = open(fp, encoding="utf-8", errors="ignore").read()
            except Exception:
                n_err += 1; continue
            if is_sentinel(text):
                n_sent += 1; continue
            d, title, desc, anames, aemails, doi, url, arts, fts = parse_card(text)
            real_year = resolve_year(osti_id, y)
            try:
                cur = con.execute(
                    "INSERT OR REPLACE INTO cards(osti_id,year,mode,title,description,authors,emails,doi,url,artifact_urls,yaml) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (osti_id, real_year, mode, title, desc, anames, aemails, doi, url, arts, text))
                rid = cur.lastrowid
                con.execute("INSERT INTO cards_fts(rowid,title,description,authors,emails,body) VALUES(?,?,?,?,?,?)",
                            (rid, title, desc, anames, aemails, fts))
                n_real += 1
            except Exception as e:
                n_err += 1
        con.commit()
        print(f"[db] {y} indexed; running real={n_real} sentinel={n_sent} err={n_err}", flush=True)
    con.execute("INSERT INTO cards_fts(cards_fts) VALUES('optimize')")
    con.commit(); con.close()
    os.replace(tmp, DB)
    # The fresh DB inherited the previous file's name; SQLite will try to apply the OLD
    # -wal/-shm against it on next open -> "database disk image is malformed". Clear them.
    for _ext in ("-wal", "-shm"):
        _p = DB + _ext
        if os.path.exists(_p):
            try: os.remove(_p)
            except OSError: pass
    print(f"[db] DONE real={n_real} sentinel={n_sent} err={n_err} -> {DB}", flush=True)
    # write stats sidecar
    con = sqlite3.connect(DB)
    stats = {"total_real": con.execute("SELECT count(*) FROM cards").fetchone()[0], "distinct_papers": con.execute("SELECT count(DISTINCT osti_id) FROM cards").fetchone()[0],
             "by_mode": dict(con.execute("SELECT mode,count(*) FROM cards GROUP BY mode").fetchall()),
             "by_year": dict(con.execute("SELECT year,count(*) FROM cards GROUP BY year").fetchall()),
             "built_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    con.close()
    json.dump(stats, open(DB+".stats.json","w"), indent=2)
    print("[db] stats:", json.dumps(stats))

if __name__ == "__main__":
    t0 = time.time()
    if "--no-rsync" in sys.argv:
        # Discover every local bucket dir that actually holds cards (4-digit years AND
        # non-year source buckets like uncovered/gap/good). resolve_year() maps the
        # non-year buckets to real catalog years at insert time.
        years = sorted({os.path.basename(os.path.dirname(p))
                        for p in glob.glob(os.path.join(LOCAL_CARDS,"*","cards"))})
        print("[sync] skipped; local buckets:", years)
    else:
        years = rsync_cards()
    build_db(years)
    # --- QA post-processing: re-apply derived columns wiped by the DB rebuild ---
    # build_db() os.replace()s a fresh DB, so has_data_repo / has_code_repo / qa_* must be
    # recomputed after every rebuild. Idempotent; each script guards its own ADD COLUMN.
    import subprocess as _sp
    for _script in ("data_repo_filter.py", "code_repo_filter.py", "contacts_backfill.py", "enrich_merge.py", "qa_score.py"):
        _p = os.path.join(BASE, _script) if "BASE" in dir() else os.path.join(os.path.dirname(os.path.abspath(__file__)), _script)
        try:
            _r = _sp.run([sys.executable, _p], cwd=os.path.dirname(os.path.abspath(__file__)),
                         capture_output=True, text=True, timeout=1200)
            print(f"[qa] {_script} rc={_r.returncode} :: {(_r.stdout or _r.stderr).strip().splitlines()[0] if (_r.stdout or _r.stderr).strip() else 'ok'}", flush=True)
        except Exception as _e:
            print(f"[qa] {_script} FAILED: {_e}", flush=True)
    print(f"[all] {time.time()-t0:.1f}s")
