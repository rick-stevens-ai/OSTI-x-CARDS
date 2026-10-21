#!/usr/bin/env python3
"""Count real/sentinel/quarantine cards + distinct papers for an OSTI year on pecans.
ARG_MAX-proof (walks the tree in Python). Prints: real=<n> tot=<n> quar=<n> papers=<n>
Usage: osti_year_count.py <year>
"""
import os, sys, re, glob

SENT = re.compile(rb"NO_[A-Z]+_FOUND")

def main():
    year = sys.argv[1]
    outdir = f"/home/stevens/osti_cards_{year}"
    carddir = f"{outdir}/cards"
    tot = 0
    real = 0
    papers = set()
    for root, _, files in os.walk(carddir):
        for fn in files:
            if not fn.endswith(".card.yaml"):
                continue
            tot += 1
            p = os.path.join(root, fn)
            try:
                with open(p, "rb") as fh:
                    head = fh.read(400)  # sentinel is always the first line
            except OSError:
                continue
            if not SENT.search(head):
                real += 1
                # paper id = basename up to first dot
                pid = fn.split(".")[0]
                papers.add(pid)
    quar = 0
    qdir = f"{outdir}/quarantine"
    for root, _, files in os.walk(qdir):
        quar += sum(1 for fn in files if fn.endswith(".fail.json"))
    print(f"real={real} tot={tot} quar={quar} papers={len(papers)}")

if __name__ == "__main__":
    main()
