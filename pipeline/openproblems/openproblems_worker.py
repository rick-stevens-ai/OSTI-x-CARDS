#!/usr/bin/env python3
"""
openproblems_worker.py — extract open problems per OSTI paper on the qwen farm.

Runs ON pecans (farm-local). For each paper in the ledger with status 'pending':
  1. read parsed md (/home/stevens/osti_parse/md_all/<id>.md, or the pushed set)
  2. prompt qwen3.8-27b: list open problems (mentioned|implied), categorize
     (Theory | Modeling and Simulation | Experiment | Observation), score on the
     ATLAS 4-dimension impact rubric (S35 E30 X20 A15)
  3. write problems JSON -> out/json/<id>.json
  4. compile a per-paper LaTeX PDF (math rendered) -> out/pdf/<id>.pdf
  5. mark ledger done (atomic claim via status='pending' -> 'running' -> 'done'/'failed')

Restartable: atomic claim, resume on 'pending'/stale-'running'. Sharded: --shard M/N
(rowid % N == M). Zero-hallucination: grounded in paper text only.
"""
import os, sys, json, re, sqlite3, time, subprocess, argparse, glob, urllib.request, random

ROOT = os.path.expanduser("~/osti-openproblems")
MD_DIRS = ["/home/stevens/osti_parse/md_all", "/home/stevens/op_md"]  # op_md = pushed from m1
LEDGER = os.path.join(ROOT, "ledger.sqlite")
OUT_JSON = os.path.join(ROOT, "out/json")
OUT_PDF  = os.path.join(ROOT, "out/pdf")
OUT_TEX  = os.path.join(ROOT, "out/tex")
LOGDIR   = os.path.join(ROOT, "logs")
for d in (OUT_JSON, OUT_PDF, OUT_TEX, LOGDIR): os.makedirs(d, exist_ok=True)

KEY = os.environ.get("OSTI_FARM_KEY", "DLS-2026")
MODEL = os.environ.get("OP_MODEL", "qwen3.8-27b")
MAX_CHARS = int(os.environ.get("OP_MAX_CHARS", "45000"))
TECTONIC = os.environ.get("TECTONIC", os.path.expanduser("~/.local/bin/tectonic"))
STALE_SEC = 1800

def endpoints():
    urls=set()
    for f in glob.glob("/home/stevens/bvbrc_all/endpoints/*.url"):
        try:
            u=open(f).read().strip().split("|")[0].strip()
            if u: urls.add(u.rstrip("/"))
        except: pass
    return sorted(urls)
EP = endpoints()

def live_eps():
    good=[]
    for u in EP:
        try:
            req=urllib.request.Request(u+"/models", headers={"Authorization":f"Bearer {KEY}"})
            urllib.request.urlopen(req, timeout=6); good.append(u)
        except: pass
    return good

def chat(ep, prompt, maxtok=8000):
    body=json.dumps({"model":MODEL,"messages":[{"role":"user","content":prompt}],
        "max_tokens":maxtok,"temperature":0.2,
        "chat_template_kwargs":{"enable_thinking":False}}).encode()
    req=urllib.request.Request(ep+"/chat/completions", data=body,
        headers={"Content-Type":"application/json","Authorization":f"Bearer {KEY}"})
    r=json.load(urllib.request.urlopen(req, timeout=300))
    msg=r["choices"][0]["message"]
    txt=msg.get("content") or msg.get("reasoning_content") or ""
    txt=re.sub(r'<think>.*?</think>','',txt,flags=re.S).strip()
    if not txt: raise RuntimeError("empty content")
    return txt

def find_md(pid):
    for d in MD_DIRS:
        p=os.path.join(d, f"{pid}.md")
        if os.path.exists(p) and os.path.getsize(p) > 200:
            return p
    return None

def read_title(md_text, pid):
    # first real heading/line; skip markdown image lines (![](...)) and strip md image/link syntax
    for ln in md_text.splitlines():
        s=ln.strip().lstrip("#").strip()
        # drop markdown images entirely, then unwrap [text](url) -> text
        s=re.sub(r'!\[[^\]]*\]\([^)]*\)', '', s)
        s=re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', s).strip()
        # skip lines that were only an image, or are HTML/figure junk
        if len(s) >= 8 and not s.lower().startswith(("<img", "![", "figure", "table")):
            return s[:300]
    return f"OSTI {pid}"

PROMPT_TMPL = r"""You are a scientific research analyst building an "open problems" catalogue from a DOE OSTI paper.

PAPER OSTI ID: {pid}
TITLE: {title}

PAPER FULL TEXT (parsed):
<<<
{body}
>>>

TASK. Produce a list of OPEN PROBLEMS that are either (a) EXPLICITLY MENTIONED in the paper
(future work, unresolved questions, limitations the authors name) or (b) IMPLIED by the content
(a natural next question, a gap the methodology exposes). Aim for 3-8 well-formed problems; fewer
is fine if the paper is thin. Each problem must be a precise, self-contained scientific question,
NOT a vague topic. Ground everything in the paper; invent no results or numbers.

For EACH problem provide:
1. "statement": precise open problem (1-3 sentences). Use LaTeX inline math for quantities, e.g.
   $\chi_2^B$, $T_{{pc}}(\mu_B)$.
2. "origin": "mentioned" or "implied".
3. "category": EXACTLY ONE of "Theory", "Modeling and Simulation", "Experiment", "Observation".
   Theory=analytic/mathematical/conceptual; Modeling and Simulation=computational/numerical;
   Experiment=controlled laboratory measurement; Observation=observing an uncontrolled natural system.
4. "category_rationale": one sentence.
5. "math": a LaTeX DISPLAY-math snippet capturing the core quantity/relation, else "".
6. ATLAS IMPACT SCORES, integers 0-5: "scientific_significance" (w35), "enabling_value" (w30),
   "cross_field_reach" (w20), "advance_magnitude" (w15).
7. "score_rationale": one sentence.

Return STRICT JSON ONLY:
{{"problems":[{{"statement":"...","origin":"mentioned|implied","category":"...",
"category_rationale":"...","math":"...","scientific_significance":0,"enabling_value":0,
"cross_field_reach":0,"advance_magnitude":0,"score_rationale":"..."}}]}}
Output JSON only."""

def _protect_latex_escapes(s):
    # CRITICAL: \beta = \b + "eta", and \b is a VALID JSON escape (backspace 0x08),
    # so json.loads SUCCEEDS but silently destroys the LaTeX command (\beta -> 0x08+"eta").
    # Same collision for \frac(\f) \rho(\r) \tau/\theta(\t) \nu/\nabla(\n) \upsilon(\u).
    # We can't blanket-double these: a real JSON newline (\n + letter) is lexically
    # identical to \nu. SAFE SCOPE: only inside $...$ math spans, where real whitespace
    # escapes never occur but LaTeX commands do. Double the backslash there so the
    # command survives json.loads as a literal.
    def fix_span(m):
        inner=m.group(0)
        inner=re.sub(r'\\([bfnrt])(?=[a-zA-Z])', r'\\\\\1', inner)
        inner=re.sub(r'\\u(?![0-9a-fA-F]{4})(?=[a-zA-Z])', r'\\\\u', inner)
        return inner
    return re.sub(r'\$[^$]*\$', fix_span, s)

def _repair_json_escapes(s):
    # LLMs emit raw LaTeX backslashes (\sum, \tau, \upsilon, \uparrow) inside JSON
    # strings = invalid JSON escapes. Walk left-to-right: a backslash that begins a
    # VALID escape (\" \\ \/ \b \f \n \r \t or \uXXXX) is passed through together with
    # the char(s) it escapes (so a real \\ pair is consumed atomically); any other
    # backslash is doubled. This avoids mis-handling genuine \\ followed by a letter.
    out=[]; i=0; n=len(s)
    while i<n:
        c=s[i]
        if c=='\\' and i+1<n:
            nxt=s[i+1]
            if nxt in '"\\/bfnrt':
                out.append(s[i:i+2]); i+=2; continue
            if nxt=='u' and i+6<=n and re.match(r'[0-9a-fA-F]{4}', s[i+2:i+6]):
                out.append(s[i:i+6]); i+=6; continue
            out.append('\\\\'); i+=1; continue
        out.append(c); i+=1
    return ''.join(out)

def parse_json(t):
    t=re.sub(r'<think>.*?</think>','',t,flags=re.S).strip()
    m=re.search(r'```(?:json)?\s*(.*?)```',t,flags=re.S)
    if m: t=m.group(1).strip()
    i=t.find('{'); j=t.rfind('}')
    if i<0 or j<0: raise ValueError("no json object")
    frag=t[i:j+1]
    # protect LaTeX commands whose prefix is a valid JSON escape (\beta, \frac, \nu...)
    # BEFORE json.loads silently decodes them to control chars inside $...$ spans.
    frag=_protect_latex_escapes(frag)
    try:
        return json.loads(frag)
    except json.JSONDecodeError:
        return json.loads(_repair_json_escapes(frag))

W={"scientific_significance":35,"enabling_value":30,"cross_field_reach":20,"advance_magnitude":15}
def weighted(p):
    try: return sum(W[k]*int(p.get(k,0))/5 for k in W)
    except: return 0.0

def _looks_like_math(inner):
    # a real inline-math span has a TeX command or math operator, and is short-ish;
    # reject currency / prose like "17 million (real 2010 USD)".
    if not inner.strip(): return False
    if re.match(r'^\s*\d[\d,\.]*\s*[a-zA-Z]', inner):  # "17 million", "200 USD"
        return False
    return bool(re.search(r'[\\^_{}]', inner)) or bool(re.search(r'[=<>+\-*/]', inner))

_ESC_PLAIN=False  # when True, esc() escapes ALL $ as text (no inline math) — used by the compile fallback

_CTRL_RE=re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

def esc(s):
    if s is None: return ""
    # Strip raw control bytes first: LLMs sometimes emit LaTeX \beta/\f.. as JSON \b \f \r,
    # which json.loads decodes to literal control chars (0x08 etc.) that break tectonic
    # ("invalid character"). Drop them (newline/tab preserved).
    s=_CTRL_RE.sub('', str(s))
    parts=re.split(r'(\$[^$]*\$)', s)
    out=[]
    def esc_text(seg):
        for a,b in [('\\','\\textbackslash{}'),('&','\\&'),('%','\\%'),('#','\\#'),
                    ('_','\\_'),('{','\\{'),('}','\\}'),('~','\\textasciitilde{}'),
                    ('^','\\textasciicircum{}'),('$','\\$')]:
            seg=seg.replace(a,b)
        return seg
    for seg in parts:
        if (not _ESC_PLAIN) and seg.startswith('$') and seg.endswith('$') and len(seg)>=2 and _looks_like_math(seg[1:-1]):
            out.append(seg)              # keep as inline math
        else:
            out.append(esc_text(seg))    # escape as text (incl. lone/currency/unbalanced $)
    return ''.join(out)

CATCOLOR={"Theory":"catTheory","Modeling and Simulation":"catModel",
          "Experiment":"catExp","Observation":"catObs"}

def build_tex(pid, year, title, problems, drop_math=False):
    def pblock(i,p):
        cat=p.get("category","Theory"); col=CATCOLOR.get(cat,"catTheory")
        ws=weighted(p); math=(p.get("math") or "").strip()
        # guard: only emit display math that is balanced and free of stray $ / \( ;
        # drop_math forces text-only (fallback after a failed compile).
        mathtex=""
        if (not drop_math) and math and math.count("$")==0 and "\\(" not in math \
           and math.count("{")==math.count("}"):
            mathtex=f"\\vspace{{2pt}}\\begin{{mathcap}}\\[{math}\\]\\end{{mathcap}}"
        scores=(f"S\\,{p.get('scientific_significance',0)}/5 \\quad E\\,{p.get('enabling_value',0)}/5 "
                f"\\quad X\\,{p.get('cross_field_reach',0)}/5 \\quad A\\,{p.get('advance_magnitude',0)}/5")
        return (f"\\begin{{opbox}}{{Problem {i} \\textnormal{{\\small(\\textcolor{{{col}}}"
                f"{{\\textbf{{{esc(cat)}}}}} -- {esc(p.get('origin',''))})}}}}\n"
                f"{esc(p.get('statement',''))}\n{mathtex}\n\\vspace{{3pt}}\n"
                f"{{\\small\\textit{{Category rationale.}} {esc(p.get('category_rationale',''))}}}\n"
                f"\\vspace{{3pt}}\n\n{{\\footnotesize\\textbf{{ATLAS impact}} (weighted {ws:.1f}/100):"
                f"\\quad {scores}\\\\\n\\textit{{Rationale.}} {esc(p.get('score_rationale',''))}}}\n"
                f"\\end{{opbox}}\n")
    blocks="\n".join(pblock(i,p) for i,p in enumerate(problems,1))
    return (PREAMBLE.replace("@@OID@@",esc(pid)).replace("@@TITLE@@",esc(title))
            .replace("@@YEAR@@",esc(str(year))).replace("@@YEARSEP@@",(", "+esc(str(year))) if str(year).strip() else "").replace("@@N@@",str(len(problems)))
            .replace("@@BLOCKS@@",blocks))

PREAMBLE=r"""\documentclass[11pt,letterpaper]{article}
\usepackage[T1]{fontenc}\usepackage[utf8]{inputenc}
\usepackage[margin=0.85in,headheight=14pt]{geometry}
\usepackage{amsmath,amssymb,amsfonts}
\usepackage{lmodern}\usepackage{microtype}
\usepackage{xcolor}\usepackage[most]{tcolorbox}
\usepackage{fancyhdr}\usepackage{enumitem}\usepackage[hidelinks]{hyperref}
\definecolor{ink}{HTML}{14243A}\definecolor{accent}{HTML}{8C4F2B}\definecolor{pale}{HTML}{EEF3FA}
\definecolor{catTheory}{HTML}{6A2E8C}\definecolor{catModel}{HTML}{1B6E8C}
\definecolor{catExp}{HTML}{8C4F2B}\definecolor{catObs}{HTML}{2E6E3A}
\setlength{\parindent}{0pt}\setlength{\parskip}{3pt}\setlength{\emergencystretch}{2em}
\pagestyle{fancy}\fancyhf{}
\fancyhead[L]{\small\color{ink}\textbf{OSTI Open-Problems Atlas}}
\fancyhead[R]{\small\color{ink}OSTI:@@OID@@}
\fancyfoot[C]{\small\color{ink}\thepage}\renewcommand{\headrulewidth}{0.4pt}
\newtcolorbox{opbox}[1]{enhanced,breakable,colback=pale,colframe=ink,
 fonttitle=\bfseries\color{white},coltitle=white,
 attach boxed title to top left={xshift=8pt,yshift=-8pt},
 boxed title style={colback=ink,sharp corners},title={#1},arc=1.5mm,boxrule=0.7pt,top=10pt}
\newtcolorbox{mathcap}{colback=white,colframe=accent!60,boxrule=0.4pt,arc=0.5mm,
 left=4pt,right=4pt,top=2pt,bottom=2pt}
\begin{document}
\begin{center}
{\LARGE\bfseries\color{ink} Open Problems}\\[4pt]
{\large\color{accent} @@TITLE@@}\\[6pt]
{\small\color{ink} OSTI @@OID@@ -- @@YEAR@@ -- @@N@@ open problems extracted}\\[2pt]
{\footnotesize Categories: \textcolor{catTheory}{Theory} -- \textcolor{catModel}{Modeling \& Simulation} -- \textcolor{catExp}{Experiment} -- \textcolor{catObs}{Observation}\quad|\quad ATLAS impact weights: S35 E30 X20 A15}
\end{center}
\vspace{4pt}\hrule\vspace{8pt}
@@BLOCKS@@
\vfill
{\footnotesize\color{ink}\textit{Open problems extracted (mentioned or implied) from the source paper by LLM; categorized Theory / Modeling \& Simulation / Experiment / Observation; scored on the ATLAS impact rubric. Grounded in source text; no results claimed.}}
\end{document}
"""

def compile_pdf(pid, tex):
    texpath=os.path.join(OUT_TEX, f"{pid}.tex")
    open(texpath,"w").write(tex)
    # tectonic: single-binary, self-contained, no two-pass hassle
    r=subprocess.run([TECTONIC, "-o", OUT_PDF, texpath],
                     capture_output=True, text=True, timeout=180)
    pdf=os.path.join(OUT_PDF, f"{pid}.pdf")
    return (r.returncode==0 and os.path.exists(pdf)), (r.stderr[-400:] if r.returncode else "")

# ---------------- ledger ----------------
def ledger():
    c=sqlite3.connect(LEDGER, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS papers(
        osti_id TEXT PRIMARY KEY, status TEXT DEFAULT 'pending',
        n_problems INTEGER, claimed_at REAL, done_at REAL, worker TEXT, err TEXT)""")
    return c

def seed(idfile):
    c=ledger()
    ids=[x.strip() for x in open(idfile) if x.strip()]
    c.executemany("INSERT OR IGNORE INTO papers(osti_id) VALUES (?)",[(i,) for i in ids])
    c.commit()
    n=c.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    print(f"seeded; ledger has {n} papers"); c.close()

def claim(c, shard, nshard, worker):
    now=time.time()
    c.execute("BEGIN IMMEDIATE")
    row=c.execute("""SELECT osti_id FROM papers
        WHERE (status='pending' OR (status='running' AND claimed_at < ?))
        AND (CAST(osti_id AS INTEGER) % ?)=? LIMIT 1""",
        (now-STALE_SEC, nshard, shard)).fetchone()
    if not row: c.execute("COMMIT"); return None
    pid=row[0]
    c.execute("UPDATE papers SET status='running',claimed_at=?,worker=? WHERE osti_id=?",
              (now, worker, pid))
    c.execute("COMMIT"); return pid

def run(shard, nshard, limit):
    worker=f"{os.uname().nodename}:{shard}/{nshard}:{os.getpid()}"
    live=live_eps()
    if not live: print("NO LIVE ENDPOINTS"); sys.exit(2)
    print(f"[{worker}] {len(live)} live endpoints; model={MODEL}")
    c=ledger(); done=0
    while True:
        if limit and done>=limit: break
        pid=claim(c, shard, nshard, worker)
        if pid is None: print(f"[{worker}] no more work"); break
        try:
            md=find_md(pid)
            if not md:
                c.execute("UPDATE papers SET status='failed',err='no-md',done_at=? WHERE osti_id=?",(time.time(),pid)); c.commit(); continue
            text=open(md, errors="replace").read()
            title=read_title(text, pid)
            prompt=PROMPT_TMPL.format(pid=pid, title=title, body=text[:MAX_CHARS])
            # bounded retry: LLM at temp>0 occasionally emits malformed JSON (missing
            # comma/delimiter) that no escape-repair can fix — re-roll on a fresh endpoint.
            data=None; last_err=None
            for attempt in range(3):
                ep=random.choice(live)
                try:
                    out=chat(ep, prompt)
                    data=parse_json(out)
                    break
                except Exception as pe:
                    last_err=pe; continue
            if data is None:
                c.execute("UPDATE papers SET status='failed',err=?,done_at=? WHERE osti_id=?",
                          (str(last_err)[:200], time.time(), pid)); c.commit(); continue
            probs=data.get("problems",[])
            if not isinstance(probs,list) or not probs:
                # zero open problems is a VALID content outcome for non-research docs
                # (memorials, errata, front-matter) — record as 'skip', not 'failed',
                # so it doesn't pollute the failure count at corpus scale.
                c.execute("UPDATE papers SET status='skip',err='no-problems',done_at=? WHERE osti_id=?",(time.time(),pid)); c.commit(); continue
            # year from md dir metadata unknown here -> leave blank; m1 join fills it
            rec={"paper":{"osti_id":pid,"title":title},"problems":probs,"model":MODEL}
            json.dump(rec, open(os.path.join(OUT_JSON,f"{pid}.json"),"w"), indent=1)
            tex=build_tex(pid, "", title, probs)
            ok,err=compile_pdf(pid, tex)
            if not ok:
                # fallback: retry text-only — drop display math AND escape ALL inline $
                # (handles unbalanced/stray $ in statement text, e.g. "$17/\text{kW}$)").
                global _ESC_PLAIN
                _ESC_PLAIN=True
                try:
                    tex2=build_tex(pid, "", title, probs, drop_math=True)
                finally:
                    _ESC_PLAIN=False
                ok,err2=compile_pdf(pid, tex2)
                if ok:
                    c.execute("UPDATE papers SET status='done',n_problems=?,done_at=?,err='math-dropped' WHERE osti_id=?",
                              (len(probs), time.time(), pid)); c.commit()
                    done+=1; continue
                c.execute("UPDATE papers SET status='failed',err=?,n_problems=?,done_at=? WHERE osti_id=?",
                          ("pdf2:"+err2[:200], len(probs), time.time(), pid)); c.commit(); continue
            c.execute("UPDATE papers SET status='done',n_problems=?,done_at=?,err=NULL WHERE osti_id=?",
                      (len(probs), time.time(), pid)); c.commit()
            done+=1
            if done%25==0: print(f"[{worker}] done={done} last={pid} probs={len(probs)}")
        except Exception as e:
            c.execute("UPDATE papers SET status='failed',err=?,done_at=? WHERE osti_id=?",
                      (str(e)[:200], time.time(), pid)); c.commit()
    c.close(); print(f"[{worker}] exit done={done}")

if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--seed")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--nshard", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    a=ap.parse_args()
    if a.seed: seed(a.seed)
    else: run(a.shard, a.nshard, a.limit)
