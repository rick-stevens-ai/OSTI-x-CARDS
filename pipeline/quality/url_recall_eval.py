#!/usr/bin/env python3
# URL recall eval: how many tool/service/repo URLs present in the PDF are reflected in the x-card?
# Ground truth = union of (a) PDF link annotations (hrefs) + (b) regex over PDF text layer.
# Card set = union of ALL urls across the card YAML (every field). Recall = card ∩ truth / truth.
import sqlite3, os, re, json, sys, random, subprocess, glob, urllib.parse
import fitz  # PyMuPDF

DB="/Users/stevens/code/osti-cards-site/data/osti_cards.sqlite"
PDFROOT="/Volumes/SG-1-8TB/osti/pdfs"
N=40
random.seed(1337)

URL_RE=re.compile(r'(?:https?://|www\.)[^\s<>"\')\]}]+', re.I)
# domains we care about for "tools/services/repos/data" (signal set)
SIGNAL=re.compile(r'(github\.com|gitlab\.|bitbucket\.|zenodo\.|figshare\.|osf\.io|'
    r'huggingface\.co|sourceforge\.|readthedocs\.|pypi\.org|conda\.|dockerhub|hub\.docker|'
    r'kaggle\.|dataverse|dryad|ncbi\.nlm|ebi\.ac\.uk|protocols\.io|codeocean|'
    r'\.gov/|nersc\.|anl\.gov|ornl\.gov|lanl\.gov|/data|/download|/software|/code|/repo)', re.I)

def norm(u):
    u=u.strip().strip('.,;:)]}>"\'')
    if u.lower().startswith('www.'): u='http://'+u
    p=urllib.parse.urlsplit(u.lower())
    host=p.netloc
    path=p.path.rstrip('/')
    return host+path

def pdf_truth(pdf):
    urls=set()
    try:
        d=fitz.open(pdf)
    except Exception as e:
        return None,None
    # (a) link annotations — the hrefs Nougat can't see
    link_urls=set()
    for pg in d:
        for l in pg.get_links():
            uri=l.get("uri")
            if uri and uri.lower().startswith(("http","www")):
                link_urls.add(uri)
    # (b) text-layer regex
    text_urls=set()
    for pg in d:
        for m in URL_RE.finditer(pg.get_text("text")):
            text_urls.add(m.group(0))
    d.close()
    return link_urls, text_urls

def card_urls(yaml):
    return set(m.group(0) for m in URL_RE.finditer(yaml))

def find_pdf(osti_id, year):
    for cand in (f"{PDFROOT}/{year}/{osti_id}.pdf", *glob.glob(f"{PDFROOT}/*/{osti_id}.pdf")):
        if os.path.isfile(cand): return cand
    return None

c=sqlite3.connect(f"file:{DB}?mode=ro",uri=True)
# sample carded papers (one card per paper is enough; we union card urls per paper)
rows=c.execute("SELECT DISTINCT osti_id, year FROM cards ORDER BY RANDOM() LIMIT 600").fetchall()
agg={"papers":0,"pdf_ok":0,
     "truth_link":0,"truth_text":0,"truth_signal":0,
     "card_has_signal":0,"recall_signal_num":0,"recall_signal_den":0,
     "linkonly_total":0,"linkonly_captured":0}
examples=[]
done=0
for osti_id,year in rows:
    if done>=N: break
    pdf=find_pdf(osti_id,year)
    if not pdf: continue
    lk,tx=pdf_truth(pdf)
    if lk is None: continue
    # union all card urls for this paper (all modes)
    cu=set()
    for (ymls,) in c.execute("SELECT yaml FROM cards WHERE osti_id=?", (osti_id,)):
        cu|=card_urls(ymls)
    agg["papers"]+=1; agg["pdf_ok"]+=1; done+=1
    # normalize
    truth_all={norm(u) for u in (lk|tx)}
    signal={norm(u) for u in (lk|tx) if SIGNAL.search(u)}
    linkonly={norm(u) for u in lk}  # hrefs
    cun={norm(u) for u in cu}
    agg["truth_link"]+=len(lk); agg["truth_text"]+=len(tx); agg["truth_signal"]+=len(signal)
    if signal:
        cap=len(signal & cun)
        agg["recall_signal_num"]+=cap; agg["recall_signal_den"]+=len(signal)
        if cap>0: agg["card_has_signal"]+=1
    agg["linkonly_total"]+=len(linkonly)
    agg["linkonly_captured"]+=len(linkonly & cun)
    if len(examples)<6 and signal:
        examples.append({"osti_id":osti_id,"year":year,
            "signal_in_pdf":sorted(signal)[:6],
            "captured_in_card":sorted(signal & cun)[:6],
            "card_url_count":len(cun)})

print("=== URL RECALL EVAL (%d papers w/ PDF) ===" % agg["pdf_ok"])
den=agg["recall_signal_den"] or 1
print(f"signal URLs in PDFs (tool/repo/data/service): {agg['truth_signal']}")
print(f"  captured in cards:                          {agg['recall_signal_num']}")
print(f"  => URL RECALL (signal) = {100*agg['recall_signal_num']/den:.1f}%")
print(f"papers w/ >=1 signal URL whose card caught >=1: {agg['card_has_signal']}/{agg['pdf_ok']}")
lo=agg["linkonly_total"] or 1
print(f"PDF hyperlink hrefs total: {agg['linkonly_total']}, reflected in card: {agg['linkonly_captured']} ({100*agg['linkonly_captured']/lo:.1f}%)")
print(f"avg truth URLs/paper: link={agg['truth_link']/agg['pdf_ok']:.1f} text={agg['truth_text']/agg['pdf_ok']:.1f}")
print("\n=== EXAMPLES ===")
for e in examples:
    print(json.dumps(e,indent=0))
