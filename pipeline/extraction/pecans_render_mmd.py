#!/usr/bin/env python3
"""Mass-render OSTI .mmd -> PDF on pecans (headless chromium + local MathJax).
Mirrors the m1 site's render pipeline (same CSS/markdown/MathJax) so output matches.
Walks /home/stevens/osti_parse/mmd/<year>/*.mmd -> osti_parse/mmd_pdf/<year>/<id>.pdf
Resumable (skips existing non-empty PDFs). Parallel via a worker pool; each worker
owns one persistent chromium (reused across renders = far faster than relaunch/paper).

Usage: pecans_render_mmd.py <year|all> [WORKERS]
Env: WORKERS (default 48)
"""
import os, sys, glob, time, traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

HOME = "/home/stevens"
MMD_ROOT = f"{HOME}/osti_parse/mmd"
PDF_ROOT = f"{HOME}/osti_parse/mmd_pdf"
LIBDIR = f"{HOME}/.pw-libs"
MATHJAX_LOCAL = f"{HOME}/.mathjax/tex-mml-chtml.js"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = f"{HOME}/.pw-browsers"
os.environ["LD_LIBRARY_PATH"] = LIBDIR + ":" + os.environ.get("LD_LIBRARY_PATH", "")

# MathJax src: local file if vendored, else CDN
if os.path.isfile(MATHJAX_LOCAL):
    MJ_SRC = "file://" + MATHJAX_LOCAL
else:
    MJ_SRC = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"

MATHJAX = ("<script>MathJax={tex:{inlineMath:[['$','$'],['\\\\(','\\\\)']],"
           "displayMath:[['$$','$$'],['\\\\[','\\\\]']],processEscapes:true},"
           "options:{skipHtmlTags:['script','noscript','style','textarea','pre','code']}};</script>"
           f'<script async src="{MJ_SRC}"></script>')

CSS = """<style>
body{font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Georgia,serif;max-width:820px;margin:0 auto;padding:36px 28px;color:#1a1a1a;background:#fff;}
h1,h2,h3{line-height:1.25;margin:1.4em 0 .5em;} h1{font-size:1.9em;} h2{font-size:1.5em;} h3{font-size:1.2em;}
table{border-collapse:collapse;margin:1em 0;width:100%;} th,td{border:1px solid #ccc;padding:6px 10px;text-align:left;font-size:.95em;}
th{background:#f3f4f6;} img{max-width:100%;height:auto;} pre{background:#f6f8fa;padding:12px;border-radius:6px;overflow:auto;}
code{background:#f0f1f3;padding:2px 5px;border-radius:4px;font-size:.9em;} blockquote{border-left:4px solid #ddd;margin:0;padding-left:16px;color:#555;}
a{color:#2563eb;} hr{border:none;border-top:1px solid #e5e7eb;margin:2em 0;}
.src-banner{background:#eef2ff;border:1px solid #c7d2fe;border-radius:8px;padding:8px 12px;font-size:13px;color:#3730a3;margin-bottom:24px;font-family:-apple-system,sans-serif;}
</style>"""

def build_html(text, year, osti_id):
    import markdown as md
    import re
    # Protect math spans from the markdown processor (which strips \( \) backslashes
    # and turns _{..} into <em>). Extract, placeholder, restore verbatim after.
    math = []
    def _stash(m):
        math.append(m.group(0))
        return f"\x00MATH{len(math)-1}\x00"
    # order matters: display before inline
    patterns = [r"\$\$.*?\$\$", r"\\\[.*?\\\]", r"\\\(.*?\\\)", r"(?<!\$)\$(?!\$).*?(?<!\$)\$(?!\$)"]
    protected = text
    for pat in patterns:
        protected = re.sub(pat, _stash, protected, flags=re.DOTALL)
    body = md.markdown(protected, extensions=["tables", "fenced_code", "toc", "sane_lists"])
    # restore math verbatim
    def _restore(m):
        return math[int(m.group(1))]
    body = re.sub(r"\x00MATH(\d+)\x00", _restore, body)
    banner = f'<div class="src-banner">OSTI {osti_id} · {year} · parsed by Nougat (.mmd)</div>'
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8">'
            f'<title>OSTI {osti_id} (mmd)</title>{CSS}{MATHJAX}</head>'
            f'<body>{banner}{body}</body></html>')

# ---- per-worker persistent chromium ----
_PW = None
_BROWSER = None

def _ensure_browser():
    global _PW, _BROWSER
    if _BROWSER is None:
        from playwright.sync_api import sync_playwright
        _PW = sync_playwright().start()
        _BROWSER = _PW.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
    return _BROWSER

def render_one(args):
    year, src = args
    osti_id = os.path.splitext(os.path.basename(src))[0]
    out_dir = f"{PDF_ROOT}/{year}"
    out = f"{out_dir}/{osti_id}.pdf"
    if os.path.isfile(out) and os.path.getsize(out) > 500:
        return ("skip", year, osti_id, 0.0)
    t0 = time.time()
    try:
        text = open(src, encoding="utf-8", errors="ignore").read()
        if not text.strip():
            return ("empty_src", year, osti_id, 0.0)
        html = build_html(text, year, osti_id)
        os.makedirs(out_dir, exist_ok=True)
        tmp_html = f"{out}.html"
        open(tmp_html, "w", encoding="utf-8").write(html)
        b = _ensure_browser()
        pg = b.new_page()
        try:
            pg.goto("file://" + tmp_html, wait_until="networkidle", timeout=60000)
            try:
                pg.wait_for_function(
                    "window.MathJax && window.MathJax.startup && window.MathJax.startup.document "
                    "&& window.MathJax.startup.document.state()>=10", timeout=12000)
            except Exception:
                pg.wait_for_timeout(1500)
            pg.pdf(path=out, format="A4",
                   margin={"top":"18mm","bottom":"18mm","left":"16mm","right":"16mm"},
                   print_background=True)
        finally:
            pg.close()
            try: os.remove(tmp_html)
            except Exception: pass
        if os.path.isfile(out) and os.path.getsize(out) > 500:
            return ("ok", year, osti_id, time.time() - t0)
        return ("tiny", year, osti_id, time.time() - t0)
    except Exception as e:
        return (f"err:{type(e).__name__}", year, osti_id, time.time() - t0)

def collect(year_arg):
    if year_arg == "all":
        years = sorted(os.path.basename(d) for d in glob.glob(f"{MMD_ROOT}/*") if os.path.isdir(d))
    else:
        years = [year_arg]
    jobs = []
    for y in years:
        for src in sorted(glob.glob(f"{MMD_ROOT}/{y}/*.mmd")):
            jobs.append((y, src))
    return jobs

def main():
    year_arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    workers = int(os.environ.get("WORKERS", sys.argv[2] if len(sys.argv) > 2 else 48))
    jobs = collect(year_arg)
    lim = int(os.environ.get("LIMIT", "0"))
    if lim:
        jobs = jobs[:lim]
    total = len(jobs)
    print(f"[render] year={year_arg} jobs={total} workers={workers} mathjax={'local' if MJ_SRC.startswith('file') else 'cdn'}", flush=True)
    t_start = time.time()
    ok = skip = fail = 0
    tsum = 0.0
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(render_one, j) for j in jobs]
        for f in as_completed(futs):
            status, year, oid, dt = f.result()
            done += 1
            if status == "ok": ok += 1; tsum += dt
            elif status == "skip": skip += 1
            else:
                fail += 1
                if fail <= 25: print(f"[render] FAIL {status} {year}/{oid}", flush=True)
            if done % 100 == 0 or done == total:
                el = time.time() - t_start
                rate = done/el if el else 0
                eta = (total-done)/rate/60 if rate else 0
                avg = tsum/ok if ok else 0
                print(f"[render] {done}/{total} ok={ok} skip={skip} fail={fail} "
                      f"| {rate:.1f}/s eta={eta:.0f}m avg={avg:.1f}s", flush=True)
    print(f"[render] DONE year={year_arg} ok={ok} skip={skip} fail={fail} elapsed={(time.time()-t_start)/60:.1f}m", flush=True)

if __name__ == "__main__":
    main()
