#!/usr/bin/env python3
"""Source-artifact access + rendering for the OSTI x-cards site.
Serves original PDF, raw .md/.mmd, HTML-rendered .md/.mmd (MathJax), and
on-the-fly .md/.mmd -> PDF (headless chromium) with a disk cache.
All source files served on-demand from canonical m1 paths; nothing copied."""
import os, hashlib, subprocess, re
import markdown as md

SG = "/Volumes/SG-1-8TB/osti"
PDF_DIR = os.path.join(SG, "pdfs")       # <year>/<id>.pdf
MMD_DIR = os.path.join(SG, "mmd")        # <year>/<id>.mmd
MD_DIR  = os.path.join(SG, "md")         # <year>/<id>.md  (Track B; may not exist yet)
CACHE   = os.path.expanduser("~/code/osti-cards-site/data/render_cache")
PRERENDER = os.path.expanduser("~/code/osti-cards-site/data/mmd_pdf")  # synced from pecans: <year>/<id>.pdf
os.makedirs(CACHE, exist_ok=True)

def pdf_path(year, osti_id):
    p = os.path.join(PDF_DIR, year, f"{osti_id}.pdf")
    return p if os.path.isfile(p) else None

def _src(dirbase, year, osti_id, ext):
    for cand in (os.path.join(dirbase, year, f"{osti_id}{ext}"),
                 os.path.join(dirbase, year, osti_id, f"{osti_id}{ext}")):
        if os.path.isfile(cand):
            return cand
    return None

def mmd_path(year, osti_id): return _src(MMD_DIR, year, osti_id, ".mmd")
def md_path(year, osti_id):  return _src(MD_DIR,  year, osti_id, ".md")

def raw_text(kind, year, osti_id):
    p = md_path(year, osti_id) if kind == "md" else mmd_path(year, osti_id)
    if not p: return None
    return open(p, encoding="utf-8", errors="ignore").read()

MATHJAX = """
<script>MathJax={tex:{inlineMath:[['$','$'],['\\\\(','\\\\)']],displayMath:[['$$','$$'],['\\\\[','\\\\]']],processEscapes:true},options:{skipHtmlTags:['script','noscript','style','textarea','pre','code']}};</script>
<script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
"""
CSS = """
<style>
body{font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Georgia,serif;max-width:820px;margin:0 auto;padding:36px 28px;color:#1a1a1a;background:#fff;}
h1,h2,h3{line-height:1.25;margin:1.4em 0 .5em;} h1{font-size:1.9em;} h2{font-size:1.5em;} h3{font-size:1.2em;}
table{border-collapse:collapse;margin:1em 0;width:100%;} th,td{border:1px solid #ccc;padding:6px 10px;text-align:left;font-size:.95em;}
th{background:#f3f4f6;} img{max-width:100%;height:auto;} pre{background:#f6f8fa;padding:12px;border-radius:6px;overflow:auto;}
code{background:#f0f1f3;padding:2px 5px;border-radius:4px;font-size:.9em;} blockquote{border-left:4px solid #ddd;margin:0;padding-left:16px;color:#555;}
a{color:#2563eb;} hr{border:none;border-top:1px solid #e5e7eb;margin:2em 0;}
.src-banner{background:#eef2ff;border:1px solid #c7d2fe;border-radius:8px;padding:8px 12px;font-size:13px;color:#3730a3;margin-bottom:24px;font-family:-apple-system,sans-serif;}
</style>
"""

def render_html(kind, year, osti_id, standalone=True):
    """kind: 'md' (Marker) or 'mmd' (Nougat). Returns full HTML string or None."""
    text = raw_text(kind, year, osti_id)
    if text is None: return None
    # Protect math spans from markdown (which strips \( \) backslashes and turns _{..} into <em>).
    math = []
    def _stash(m):
        math.append(m.group(0)); return f"\x00MATH{len(math)-1}\x00"
    for pat in (r"\$\$.*?\$\$", r"\\\[.*?\\\]", r"\\\(.*?\\\)", r"(?<!\$)\$(?!\$).*?(?<!\$)\$(?!\$)"):
        text = re.sub(pat, _stash, text, flags=re.DOTALL)
    body = md.markdown(text, extensions=["tables", "fenced_code", "toc", "sane_lists"])
    body = re.sub(r"\x00MATH(\d+)\x00", lambda m: math[int(m.group(1))], body)
    label = "Marker (.md)" if kind == "md" else "Nougat (.mmd)"
    banner = f'<div class="src-banner">OSTI {osti_id} · {year} · parsed by {label}</div>'
    if not standalone:
        return banner + body
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>OSTI {osti_id} ({kind})</title>{CSS}{MATHJAX}</head>
<body>{banner}{body}</body></html>"""

def _cache_key(kind, year, osti_id):
    p = md_path(year, osti_id) if kind == "md" else mmd_path(year, osti_id)
    if not p: return None, None
    # key includes source mtime so edits bust the cache
    st = os.stat(p)
    h = hashlib.sha1(f"{kind}:{year}:{osti_id}:{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:16]
    return os.path.join(CACHE, f"{osti_id}.{kind}.{h}.pdf"), p

def render_pdf(kind, year, osti_id):
    """md/mmd -> PDF via headless chromium (MathJax typeset). Cached by source mtime. Returns path or None."""
    # Prefer a pre-rendered PDF synced from pecans (mmd only) -- instant, no local render.
    if kind == "mmd":
        pre = os.path.join(PRERENDER, year, f"{osti_id}.pdf")
        if os.path.isfile(pre) and os.path.getsize(pre) > 500:
            return pre
    out, src = _cache_key(kind, year, osti_id)
    if not src: return None
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    html = render_html(kind, year, osti_id, standalone=True)
    if html is None: return None
    # write temp html, render with playwright chromium
    tmp_html = out + ".html"
    open(tmp_html, "w", encoding="utf-8").write(html)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page()
            pg.goto("file://" + tmp_html, wait_until="networkidle", timeout=60000)
            # give MathJax a beat to typeset
            try: pg.wait_for_function("window.MathJax && window.MathJax.startup && window.MathJax.startup.document && window.MathJax.startup.document.state()>=10", timeout=15000)
            except Exception: pg.wait_for_timeout(2500)
            pg.pdf(path=out, format="A4", margin={"top":"18mm","bottom":"18mm","left":"16mm","right":"16mm"}, print_background=True)
            b.close()
    except Exception as e:
        # fallback: chrome CLI headless
        try:
            chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            subprocess.run([chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                            f"--print-to-pdf={out}", "file://" + tmp_html], timeout=90, capture_output=True)
        except Exception:
            return None
    finally:
        try: os.remove(tmp_html)
        except Exception: pass
    return out if os.path.isfile(out) and os.path.getsize(out) > 0 else None

def artifacts_available(year, osti_id):
    return {"pdf": pdf_path(year, osti_id) is not None,
            "md": md_path(year, osti_id) is not None,
            "mmd": mmd_path(year, osti_id) is not None}
