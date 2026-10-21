#!/usr/bin/env python3
"""OSTI x-cards browsing/search API (FastAPI + SQLite FTS5).
Serves real OSTI-paper-derived x-cards. Browse, search, download single, bulk-download zip."""
import os, sqlite3, json, io, zipfile, re
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import sources

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, "code/osti-cards-site")
DB = os.path.join(BASE, "data/osti_cards.sqlite")

app = FastAPI(title="OSTI x-Cards")

# ---- durable access logging (captures REAL client IP through Tailscale Funnel) ----
import time as _time, threading as _threading
from starlette.requests import Request as _Request
ACCESS_DB = os.path.join(BASE, "data/access_log.sqlite")
_log_lock = _threading.Lock()

def _init_access_db():
    c = sqlite3.connect(ACCESS_DB)
    c.execute("""CREATE TABLE IF NOT EXISTS access(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, epoch REAL,
        client_ip TEXT, xff TEXT, method TEXT, path TEXT, status INTEGER,
        ms REAL, user_agent TEXT, referer TEXT, via_funnel INTEGER)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_access_ip ON access(client_ip)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_access_epoch ON access(epoch)")
    c.commit(); c.close()
_init_access_db()

def _real_ip(req: _Request):
    xff = req.headers.get("x-forwarded-for", "")
    if xff:  # Funnel/proxy: first hop is the true client
        return xff.split(",")[0].strip(), xff
    return (req.client.host if req.client else ""), ""

@app.middleware("http")
async def _access_logger(request: _Request, call_next):
    t0 = _time.time()
    status = 500
    try:
        resp = await call_next(request)
        status = resp.status_code
        return resp
    finally:
        try:
            ip, xff = _real_ip(request)
            row = (_time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()), t0, ip, xff,
                   request.method, request.url.path, status, round((_time.time()-t0)*1000, 1),
                   request.headers.get("user-agent", "")[:300],
                   request.headers.get("referer", "")[:300], 1 if xff else 0)
            with _log_lock:
                c = sqlite3.connect(ACCESS_DB, timeout=5)
                c.execute("""INSERT INTO access(ts,epoch,client_ip,xff,method,path,status,ms,user_agent,referer,via_funnel)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?)""", row)
                c.commit(); c.close()
        except Exception:
            pass

@app.get("/api/access/stats")
def access_stats(hours: int = 24, limit: int = 50):
    cutoff = _time.time() - hours*3600
    c = sqlite3.connect(ACCESS_DB); c.row_factory = sqlite3.Row
    total = c.execute("SELECT COUNT(*) FROM access WHERE epoch>=?", (cutoff,)).fetchone()[0]
    uniq = c.execute("SELECT COUNT(DISTINCT client_ip) FROM access WHERE epoch>=?", (cutoff,)).fetchone()[0]
    funnel = c.execute("SELECT COUNT(*) FROM access WHERE epoch>=? AND via_funnel=1", (cutoff,)).fetchone()[0]
    top = [dict(r) for r in c.execute(
        """SELECT client_ip, COUNT(*) hits, MAX(ts) last_seen, SUM(via_funnel) funnel_hits
           FROM access WHERE epoch>=? AND client_ip<>'' GROUP BY client_ip
           ORDER BY hits DESC LIMIT ?""", (cutoff, limit)).fetchall()]
    c.close()
    return {"window_hours": hours, "total_requests": total, "unique_ips": uniq,
            "via_funnel": funnel, "top_ips": top}

def db():
    if not os.path.exists(DB):
        raise HTTPException(503, "Index not built yet — run sync_cards.py")
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def fts_query(q):
    # sanitize user text into a safe FTS5 MATCH expression (prefix-match each token)
    toks = re.findall(r"[A-Za-z0-9_]+", q or "")
    if not toks: return None
    return " ".join(f'"{t}"*' for t in toks)

@app.get("/api/stats")
def stats():
    # Always report live DB counts — a stale sidecar (written mid-build with a
    # pre-dedup counter) must never override the real row count. Sidecar kept
    # only as a fallback if the DB query fails.
    try:
        con = db()
        s = {"total_real": con.execute("SELECT count(*) FROM cards").fetchone()[0],
             "distinct_papers": con.execute("SELECT count(DISTINCT osti_id) FROM cards").fetchone()[0],
             "by_mode": dict(con.execute("SELECT mode,count(*) FROM cards GROUP BY mode").fetchall()),
             "by_year": dict(con.execute("SELECT year,count(*) FROM cards GROUP BY year").fetchall())}
        con.close()
        return s
    except Exception:
        sp = DB + ".stats.json"
        if os.path.exists(sp):
            return JSONResponse(json.load(open(sp)))
        raise

@app.get("/api/search")
def search(q: str = "", mode: str = "", year: str = "", data_repo: int = 0,
          code_repo: int = 0, qa_tier: str = "",
          limit: int = Query(50, le=500), offset: int = 0):
    con = db()
    # Defensive column presence check — several derived/QA columns are added by
    # out-of-band jobs (data_repo_filter.py, code_repo_filter.py, the qa scorer)
    # and may not exist yet. Only reference columns that are actually present so
    # the site never 500s mid-migration.
    have = {r[1] for r in con.execute("PRAGMA table_info(cards)")}
    def colsel(name, default="''"):
        return f"COALESCE(c.{name},{default}) AS {name}" if name in have else f"{default} AS {name}"
    where, params = [], []
    base = "FROM cards c"
    if q:
        m = fts_query(q)
        if m:
            base = "FROM cards_fts f JOIN cards c ON c.id=f.rowid"
            where.append("cards_fts MATCH ?"); params.append(m)
    if mode:
        where.append("c.mode=?"); params.append(mode)
    if year:
        where.append("c.year=?"); params.append(year)
    if data_repo and "has_data_repo" in have:
        # value-add filter: data cards that reference a resolvable data repository
        where.append("c.has_data_repo=1")
    if code_repo and "has_code_repo" in have:
        # value-add filter: software cards that reference a resolvable code repository
        where.append("c.has_code_repo=1")
    if qa_tier and "qa_tier" in have:
        # quality-tier filter (HIGH / MEDIUM / LOW); scorer may not have run yet
        where.append("c.qa_tier=?"); params.append(qa_tier)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    # Capped count: counting ALL matching rows for a broad prefix term (e.g. 'simulation*'
    # matches ~70k rows) costs seconds. Count at most COUNT_CAP rows; show "CAP+" past that.
    COUNT_CAP = 1000
    total = con.execute(
        f"SELECT count(*) FROM (SELECT c.id {base}{w} LIMIT {COUNT_CAP + 1})",
        params).fetchone()[0]
    total_capped = total > COUNT_CAP
    if total_capped:
        total = COUNT_CAP
    order = "ORDER BY rank" if q and fts_query(q) else "ORDER BY c.year DESC, c.osti_id"
    rows = con.execute(
        f"SELECT c.osti_id,c.year,c.mode,c.title,substr(c.description,1,300) AS description,"
        f"c.authors,c.doi,c.url,"
        f"{colsel('has_data_repo','0')},"
        f"{colsel('data_repo_hosts')},"
        f"{colsel('data_repo_urls')},"
        f"{colsel('has_code_repo','0')},"
        f"{colsel('code_repo_hosts')},"
        f"{colsel('code_repo_urls')},"
        f"{colsel('qa_tier')},"
        f"{colsel('qa_completeness','0')},"
        f"{colsel('qa_provenance','0')},"
        f"{colsel('qa_funding','0')} {base}{w} {order} LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    con.close()
    return {"total": total, "total_capped": total_capped, "limit": limit, "offset": offset,
            "results": [dict(r) for r in rows]}

@app.get("/api/card/{year}/{mode}/{osti_id}")
def card(year: str, mode: str, osti_id: str):
    con = db()
    r = con.execute("SELECT * FROM cards WHERE osti_id=? AND mode=? AND year=?",
                    (osti_id, mode, year)).fetchone()
    con.close()
    if not r: raise HTTPException(404, "card not found")
    return dict(r)

@app.get("/api/download/{year}/{mode}/{osti_id}")
def download(year: str, mode: str, osti_id: str):
    con = db()
    r = con.execute("SELECT yaml FROM cards WHERE osti_id=? AND mode=? AND year=?",
                    (osti_id, mode, year)).fetchone()
    con.close()
    if not r: raise HTTPException(404, "card not found")
    fn = f"{osti_id}.{mode}.card.yaml"
    return PlainTextResponse(r["yaml"], headers={
        "Content-Disposition": f'attachment; filename="{fn}"'})

@app.get("/api/bulk")
def bulk(q: str = "", mode: str = "", year: str = "", max_cards: int = Query(5000, le=50000)):
    """Zip all cards matching the current filter. Streams a .zip."""
    con = db()
    where, params = [], []
    base = "FROM cards c"
    if q:
        m = fts_query(q)
        if m:
            base = "FROM cards_fts f JOIN cards c ON c.id=f.rowid"
            where.append("cards_fts MATCH ?"); params.append(m)
    if mode: where.append("c.mode=?"); params.append(mode)
    if year: where.append("c.year=?"); params.append(year)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    rows = con.execute(f"SELECT c.osti_id,c.year,c.mode,c.yaml {base}{w} LIMIT ?",
                       params + [max_cards]).fetchall()
    con.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in rows:
            z.writestr(f"{r['year']}/{r['mode']}/{r['osti_id']}.{r['mode']}.card.yaml", r["yaml"])
        manifest = {"count": len(rows), "filter": {"q": q, "mode": mode, "year": year}}
        z.writestr("MANIFEST.json", json.dumps(manifest, indent=2))
    buf.seek(0)
    tag = "-".join([x for x in [year, mode, (q.replace(' ','_')[:20] if q else "")] if x]) or "all"
    return StreamingResponse(buf, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="osti-xcards-{tag}.zip"'})

# ---- Source artifacts: original PDF, raw/rendered .md & .mmd ----
@app.get("/api/sources/{year}/{osti_id}")
def sources_available(year: str, osti_id: str):
    return sources.artifacts_available(year, osti_id)

@app.get("/api/pdf/{year}/{osti_id}")
def orig_pdf(year: str, osti_id: str, download: int = 0):
    p = sources.pdf_path(year, osti_id)
    if not p: raise HTTPException(404, "original PDF not found")
    disp = "attachment" if download else "inline"
    return FileResponse(p, media_type="application/pdf", headers={
        "Content-Disposition": f'{disp}; filename="{osti_id}.pdf"'})

@app.get("/api/raw/{kind}/{year}/{osti_id}")
def raw_source(kind: str, year: str, osti_id: str, download: int = 0):
    if kind not in ("md", "mmd"): raise HTTPException(400, "kind must be md or mmd")
    text = sources.raw_text(kind, year, osti_id)
    if text is None: raise HTTPException(404, f".{kind} not found (may not be parsed yet)")
    hdr = {}
    if download:
        hdr["Content-Disposition"] = f'attachment; filename="{osti_id}.{kind}"'
    return PlainTextResponse(text, headers=hdr)

@app.get("/api/render/{kind}/{year}/{osti_id}", response_class=HTMLResponse)
def render_source(kind: str, year: str, osti_id: str):
    if kind not in ("md", "mmd"): raise HTTPException(400, "kind must be md or mmd")
    html = sources.render_html(kind, year, osti_id, standalone=True)
    if html is None: raise HTTPException(404, f".{kind} not found (may not be parsed yet)")
    return HTMLResponse(html)

@app.get("/api/renderpdf/{kind}/{year}/{osti_id}")
def render_source_pdf(kind: str, year: str, osti_id: str, download: int = 0):
    if kind not in ("md", "mmd"): raise HTTPException(400, "kind must be md or mmd")
    out = sources.render_pdf(kind, year, osti_id)
    if not out: raise HTTPException(404, f".{kind} not found or render failed")
    disp = "attachment" if download else "inline"
    return FileResponse(out, media_type="application/pdf", headers={
        "Content-Disposition": f'{disp}; filename="{osti_id}.{kind}.pdf"'})

SUPERDB = os.path.join(BASE, "data/supercards.sqlite")

def superdb():
    if not os.path.exists(SUPERDB):
        raise HTTPException(503, "SuperCards not built yet — run supercards_build.py")
    con = sqlite3.connect(f"file:{SUPERDB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con

@app.get("/api/supercards/stats")
def supercard_stats():
    con = superdb()
    s = {"total": con.execute("SELECT count(*) FROM supercard").fetchone()[0],
         "total_members": con.execute("SELECT COALESCE(SUM(member_count),0) FROM supercard").fetchone()[0],
         "by_mode": dict(con.execute("SELECT mode,count(*) FROM supercard GROUP BY mode").fetchall())}
    con.close()
    return s

@app.get("/api/supercards/search")
def supercard_search(q: str = "", mode: str = "", min_members: int = 2,
                     limit: int = Query(50, le=500), offset: int = 0):
    con = superdb()
    where, params = ["member_count>=?"], [min_members]
    if q:
        where.append("canonical_name LIKE ?"); params.append(f"%{q}%")
    if mode:
        where.append("mode=?"); params.append(mode)
    w = " WHERE " + " AND ".join(where)
    total = con.execute(f"SELECT count(*) FROM supercard{w}", params).fetchone()[0]
    rows = con.execute(
        f"SELECT super_id,mode,canonical_name,member_count,first_year,last_year,"
        f"labs,n_with_url,n_with_email,urls,emails {('FROM supercard'+w)} "
        f"ORDER BY member_count DESC LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
    con.close()
    return {"total": total, "limit": limit, "offset": offset,
            "results": [dict(r) for r in rows]}

@app.get("/api/supercard/{super_id}")
def supercard_detail(super_id: str):
    con = superdb()
    r = con.execute("SELECT * FROM supercard WHERE super_id=?", (super_id,)).fetchone()
    if not r:
        con.close(); raise HTTPException(404, "supercard not found")
    members = [row[0] for row in con.execute(
        "SELECT osti_id FROM supercard_member WHERE super_id=?", (super_id,)).fetchall()]
    con.close()
    d = dict(r); d["members"] = members
    return d

app.mount("/", StaticFiles(directory=os.path.join(BASE, "static"), html=True), name="static")
