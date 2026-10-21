#!/usr/bin/env python3
"""
email_mine.py — recover >=1 good AUTHOR email per paper from its OWN md parse.

Zero-hallucination: only emails physically present in the parse text are emitted.
Author-email classifier REJECTS boilerplate / ordering / infrastructure addresses
and prefers emails near the author/affiliation block (top of paper).

Input : ids file (osti_ids, one per line)  OR  --all-missing (reads a prebuilt list)
Parse roots: /home/stevens/osti_parse/md_all (flat + year subdirs) + /home/stevens/_recover_md
Output: JSONL {osti_id, emails:[...], best:email, source:"parse"} to --out

Run on pecans. Sharded by the driver.
"""
import re, os, glob, json, sys, argparse

_INDEX = None
def _load_index():
    global _INDEX
    if _INDEX is None:
        ip = "/home/stevens/_parse_index.json"
        _INDEX = json.load(open(ip)) if os.path.exists(ip) else {}
    return _INDEX

EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')

# --- boilerplate / non-author rejects ---------------------------------------
REJECT_LOCAL = {"reports","orders","info","webmaster","help","support","admin",
                "contact","sales","service","office","library","editor","journals",
                "subscriptions","permissions","customerservice","noreply","no-reply",
                "postmaster","root","webadmin","feedback","_info","info_","mail",
                "enquiries","inquiries","general","press","media","hr","careers"}
REJECT_DOMAIN_SUBSTR = ("osti.gov","ntis.gov","ntis.fedworld.gov","adonis.osti.gov",
                        "doe.gov/bridge","springer.com","elsevier.com","wiley.com",
                        "tandfonline.com","sciencedirect.com","example.com",
                        "doi.org","crossref.org")
# generic ordering/admin DOE addresses
REJECT_FULL_SUBSTR = ("@ee.doe.gov", "orders@", "reports@")

def is_boilerplate(e):
    el = e.lower()
    local = el.split("@",1)[0].strip("_.-")   # strip markdown artifact chars
    dom = el.split("@",1)[1] if "@" in el else ""
    if local in REJECT_LOCAL: return True
    if any(s in dom for s in REJECT_DOMAIN_SUBSTR): return True
    if any(s in el for s in REJECT_FULL_SUBSTR): return True
    if el.endswith((".png",".jpg",".gif",".svg")): return True
    if len(local) < 2: return True
    return False

def find_parse(pid):
    idx = _load_index()
    if pid in idx:
        return idx[pid]
    cands = (glob.glob(f"/home/stevens/_recover_md/{pid}.md")
             + glob.glob(f"/home/stevens/osti_parse/md_all/{pid}.md")
             + glob.glob(f"/home/stevens/osti_parse/md_all/{pid}/{pid}.md")
             + glob.glob(f"/home/stevens/osti_parse/mmd/{pid}.mmd"))
    return cands[0] if cands else None

def mine(pid):
    p = find_parse(pid)
    if not p: return None
    try:
        txt = open(p, errors="ignore").read()
    except Exception:
        return None
    head = txt[:12000]           # author/affiliation region = top of paper
    emails, seen = [], set()
    for m in EMAIL.finditer(head):
        e = m.group(0).strip().strip('.')
        el = e.lower()
        if el in seen: continue
        seen.add(el)
        if is_boilerplate(e): continue
        emails.append(e)
    if not emails:
        # fall back to whole doc (some templates put contacts at end)
        for m in EMAIL.finditer(txt[12000:40000]):
            e = m.group(0).strip().strip('.')
            el = e.lower()
            if el in seen: continue
            seen.add(el)
            if is_boilerplate(e): continue
            emails.append(e)
    if not emails: return None
    # best = the one whose local part looks like a person (has a dot or >5 chars, not generic)
    def score(e):
        loc = e.split("@")[0].lower()
        s = 0
        if "." in loc: s += 2          # first.last
        if any(ch.isdigit() for ch in loc): s -= 1
        if loc in REJECT_LOCAL: s -= 5
        s += min(len(loc),12)/12
        return s
    best = max(emails, key=score)
    return {"osti_id": pid, "emails": emails[:6], "best": best, "source": "parse"}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids-file", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    pids = [l.strip() for l in open(a.ids_file) if l.strip()]
    n_found=0
    with open(a.out, "w") as fo:
        for pid in pids:
            r = mine(pid)
            if r:
                fo.write(json.dumps(r)+"\n"); n_found+=1
    sys.stderr.write(f"done {a.ids_file}: {n_found}/{len(pids)} papers got an author email\n")

if __name__ == "__main__":
    main()
