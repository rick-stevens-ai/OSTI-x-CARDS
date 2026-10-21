#!/usr/bin/env python3
"""OSTI x-Cards MCP server — exposes the OSTI-paper-derived x-card corpus to models/agents.

Wraps the SAME SQLite FTS5 index + source-artifact store that powers the website
(http://<tailnet-host>:8094), so MCP tools behave identically to the HTTP API.
mcp SDK 2.x (MCPServer). Transport: streamable-http (network) or stdio (local dev).
"""
import os, sqlite3, json, re, io, zipfile, base64
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, "code/osti-cards-site")
DB = os.path.join(BASE, "data/osti_cards.sqlite")

import sys
sys.path.insert(0, BASE)
import sources  # same artifact resolver the website uses

app = MCPServer(
    "osti-xcards",
    version="1.0.0",
    instructions=(
        "OSTI x-Cards corpus: structured cards extracted from ~77K DOE/OSTI papers "
        "(2000-2026), 7 card modes (model, agent, instrument, facility, simulation, "
        "workflow, data). Use osti_search to find cards (FTS5 full-text + mode/year "
        "filters), osti_get_card for a full card, osti_paper_text to read the parsed "
        "paper (.md/.mmd), osti_list_sources to see what artifacts exist, and "
        "osti_stats for corpus-wide counts. osti_id is the stable paper key."
    ),
)

MODES = ["model","agent","instrument","facility","simulation","workflow","data"]

def _db():
    if not os.path.exists(DB):
        raise RuntimeError("Index not built yet — run sync_cards.py")
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con

def _fts(q):
    toks = re.findall(r"[A-Za-z0-9_]+", q or "")
    return " ".join(f'"{t}"*' for t in toks) if toks else None


@app.tool(description="Corpus-wide statistics: total cards, distinct papers, breakdown by "
                      "card mode and by year. No args. Call this first to understand scope.")
def osti_stats() -> dict:
    sp = DB + ".stats.json"
    if os.path.exists(sp):
        return json.load(open(sp))
    con = _db()
    s = {"total_cards": con.execute("SELECT count(*) FROM cards").fetchone()[0],
         "distinct_papers": con.execute("SELECT count(DISTINCT osti_id) FROM cards").fetchone()[0],
         "by_mode": dict(con.execute("SELECT mode,count(*) FROM cards GROUP BY mode").fetchall()),
         "by_year": dict(con.execute("SELECT year,count(*) FROM cards GROUP BY year").fetchall())}
    con.close()
    return s


@app.tool(description="Search x-cards by full-text query (FTS5 prefix match over title/"
                      "description/authors/emails/body) with optional mode and year filters. "
                      "Returns matching cards (osti_id, year, mode, title, description snippet, "
                      "authors, doi, url). Pagination via limit/offset. Empty q lists by filter. "
                      f"Valid modes: {', '.join(MODES)}.")
def osti_search(q: str = "", mode: str = "", year: str = "",
                limit: int = 25, offset: int = 0) -> dict:
    limit = max(1, min(limit, 200))
    con = _db()
    where, params = [], []
    base = "FROM cards c"
    if q:
        m = _fts(q)
        if m:
            base = "FROM cards_fts f JOIN cards c ON c.id=f.rowid"
            where.append("cards_fts MATCH ?"); params.append(m)
    if mode:
        where.append("c.mode=?"); params.append(mode)
    if year:
        where.append("c.year=?"); params.append(year)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    CAP = 1000
    total = con.execute(f"SELECT count(*) FROM (SELECT c.id {base}{w} LIMIT {CAP+1})", params).fetchone()[0]
    capped = total > CAP
    if capped: total = CAP
    order = "ORDER BY rank" if (q and _fts(q)) else "ORDER BY c.year DESC, c.osti_id"
    rows = con.execute(
        f"SELECT c.osti_id,c.year,c.mode,c.title,substr(c.description,1,300) AS description,"
        f"c.authors,c.doi,c.url {base}{w} {order} LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    con.close()
    return {"total": total, "total_capped": capped, "limit": limit, "offset": offset,
            "results": [dict(r) for r in rows]}


@app.tool(description="Fetch one full x-card (all fields incl. complete YAML body) by its "
                      "year, mode, and osti_id. Use osti_search first to find these keys.")
def osti_get_card(year: str, mode: str, osti_id: str) -> dict:
    if mode and mode not in MODES:
        return {"error": "invalid_mode", "detail": f"mode '{mode}' not one of {MODES}"}
    con = _db()
    r = con.execute("SELECT * FROM cards WHERE osti_id=? AND mode=? AND year=?",
                    (osti_id, mode, year)).fetchone()
    con.close()
    if not r:
        return {"error": "not_found",
                "detail": f"no card for osti_id={osti_id} mode={mode} year={year}"}
    return dict(r)


@app.tool(description="List ALL cards (every mode) for a single paper by osti_id. Returns one "
                      "entry per mode present, with title/doi/url and the full YAML for each.")
def osti_get_paper_cards(osti_id: str) -> dict:
    con = _db()
    rows = con.execute("SELECT osti_id,year,mode,title,doi,url,yaml FROM cards WHERE osti_id=? "
                       "ORDER BY mode", (osti_id,)).fetchall()
    con.close()
    if not rows:
        return {"error": "not_found", "detail": f"no cards for osti_id {osti_id}"}
    return {"osti_id": osti_id, "modes": [r["mode"] for r in rows],
            "cards": [dict(r) for r in rows]}


@app.tool(description="Read the parsed full text of the baseline paper. kind='md' (Marker) or "
                      "'mmd' (Nougat). Returns the raw markdown text the cards were derived from. "
                      "Use to ground/verify a card against the source paper.")
def osti_paper_text(kind: str, year: str, osti_id: str, max_chars: int = 40000) -> dict:
    if kind not in ("md", "mmd"):
        return {"error": "invalid_kind", "detail": "kind must be 'md' or 'mmd'"}
    text = sources.raw_text(kind, year, osti_id)
    if text is None:
        return {"error": "not_found",
                "detail": f".{kind} not found for {year}/{osti_id} (may not be parsed yet)"}
    truncated = len(text) > max_chars
    return {"osti_id": osti_id, "year": year, "kind": kind,
            "chars": len(text), "truncated": truncated,
            "text": text[:max_chars]}


@app.tool(description="List which source artifacts exist for a paper: original PDF, Marker .md, "
                      "Nougat .mmd. Returns availability flags so an agent knows what it can fetch.")
def osti_list_sources(year: str, osti_id: str) -> dict:
    return sources.artifacts_available(year, osti_id)


@app.tool(description="Bulk export: return all cards matching a filter (q/mode/year) as a base64 "
                      "zip of YAML files (plus MANIFEST.json). For agents that want the raw card "
                      "set. max_cards caps the export.")
def osti_bulk_export(q: str = "", mode: str = "", year: str = "", max_cards: int = 2000) -> dict:
    max_cards = max(1, min(max_cards, 20000))
    con = _db()
    where, params = [], []
    base = "FROM cards c"
    if q:
        m = _fts(q)
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
        z.writestr("MANIFEST.json", json.dumps({"count": len(rows),
                   "filter": {"q": q, "mode": mode, "year": year}}, indent=2))
    buf.seek(0)
    data = buf.getvalue()
    return {"count": len(rows), "zip_bytes": len(data),
            "zip_base64": base64.b64encode(data).decode()}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", default="streamable-http",
                    choices=["stdio", "sse", "streamable-http"])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8096)
    a = ap.parse_args()
    if a.transport == "stdio":
        app.run(transport="stdio")
    else:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                "127.0.0.1:*", "localhost:*", "[::1]:*",
                "<tailnet-funnel-host>:8443",
            ],
            allowed_origins=[
                "https://<tailnet-funnel-host>:8443",
            ],
        )
        app.run(transport=a.transport, host=a.host, port=a.port,
                transport_security=security)
