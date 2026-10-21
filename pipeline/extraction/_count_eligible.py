#!/usr/bin/env python3
"""Count papers eligible for render pre-warming."""
import sqlite3, os
SG = "/Volumes/SG-1-8TB/osti/mmd"
c = sqlite3.connect("data/osti_cards.sqlite")
rows = c.execute("SELECT DISTINCT year,osti_id FROM cards").fetchall()
print("distinct real-card papers:", len(rows))
from collections import Counter
print("by year:", dict(sorted(Counter(y for y,_ in rows).items())))
have = [(y,i) for y,i in rows if os.path.isfile(f"{SG}/{y}/{i}.mmd")]
print(f"have .mmd: {len(have)} / {len(rows)}")
import glob
cached = glob.glob("data/render_cache/*.pdf")
print("cached PDFs now:", len(cached))
