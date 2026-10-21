#!/usr/bin/env python3
"""
contacts_backfill.py — connect author emails to OSTI x-CARDs from the UMP contacts store.

Source of truth for emails: ~/Dropbox/XFER/osti-contacts/contacts.db
  (36,026 contacts linked to 67,119 papers via contact_paper, with per-paper
   is_corresponding flags, name variants, labs, affiliations.)

For every card whose `emails` field is empty/UNKNOWN and whose osti_id matches a
paper in contacts.db, attach that paper's author emails as a derived, bracketed
field so provenance is honest. Corresponding authors are listed first.

DERIVED COLUMNS added (never touches extraction / sync_cards extraction logic):
  - contacts_emails      TEXT  : "Name <email> [corresponding]; ..." (derived set)
  - contacts_email_count INT   : number of emails connected from contacts.db
  - contacts_backfilled  INT   : 1 if this card got emails from contacts.db
  - contacts_version     TEXT

Also OPTIONALLY fills the native `emails` column when it was empty, tagging the
value with a [derived: UMP contacts.db] marker so it's visibly non-extracted.

Idempotent: full recompute keyed by osti_id; re-runnable; ADD COLUMN guarded.
Self-healing: call main() from sync_cards.py post-rebuild loop.
"""
import os, sqlite3, sys

CARD_DB = os.path.expanduser("~/code/osti-cards-site/data/osti_cards.sqlite")
CONTACTS_DB = os.path.expanduser("~/Dropbox/XFER/osti-contacts/contacts.db")
VERSION = "v1"

EMPTYISH = {"", "UNKNOWN", "NONE", "N/A", "[]", "NOT PROVIDED", "NOT SPECIFIED", "NULL"}

def emptyish(v):
    if v is None:
        return True
    return str(v).strip().upper() in EMPTYISH

def norm_oid(oid):
    s = str(oid).strip()
    try:
        return str(int(s))
    except Exception:
        return s

def ensure_cols(con):
    have = {d[1] for d in con.execute("PRAGMA table_info(cards)")}
    for col, typ in [("contacts_emails", "TEXT"),
                     ("contacts_email_count", "INTEGER"),
                     ("contacts_backfilled", "INTEGER"),
                     ("contacts_version", "TEXT")]:
        if col not in have:
            con.execute(f"ALTER TABLE cards ADD COLUMN {col} {typ}")
    con.execute("CREATE INDEX IF NOT EXISTS idx_cards_contacts_backfilled "
                "ON cards(contacts_backfilled)")

def load_paper_emails(ccon):
    """osti_id(str) -> list of (email, name, is_corresponding, lab), corresponding first."""
    out = {}
    q = """SELECT cp.osti_id, cp.email, c.primary_name, cp.is_corresponding, c.primary_lab
           FROM contact_paper cp JOIN contact c ON c.email = cp.email"""
    for oid, email, name, iscorr, lab in ccon.execute(q):
        out.setdefault(norm_oid(oid), []).append(
            (email, name or "", int(iscorr or 0), lab or ""))
    # sort: corresponding first, then by name
    for k in out:
        out[k].sort(key=lambda t: (-t[2], t[1].lower()))
    return out

def fmt_entry(email, name, iscorr):
    tag = " [corresponding]" if iscorr else ""
    return f"{name} <{email}>{tag}".strip() if name else f"<{email}>{tag}"

def main(card_db=CARD_DB, contacts_db=CONTACTS_DB, fill_native=True, verbose=True):
    if not os.path.exists(contacts_db):
        print(f"[contacts_backfill] contacts.db not found at {contacts_db} — skipping", file=sys.stderr)
        return {"error": "no_contacts_db"}
    ccon = sqlite3.connect(f"file:{contacts_db}?mode=ro", uri=True)
    paper_emails = load_paper_emails(ccon)
    ccon.close()

    con = sqlite3.connect(card_db, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    ensure_cols(con)

    rows = con.execute("SELECT rowid, osti_id, emails FROM cards").fetchall()
    n_total = len(rows)
    n_match = n_backfill = n_native_filled = 0
    updates = []
    for rid, oid, em in rows:
        key = norm_oid(oid)
        entries = paper_emails.get(key)
        if not entries:
            updates.append((None, 0, 0, VERSION, rid))
            continue
        n_match += 1
        derived = "; ".join(fmt_entry(e, nm, ic) for (e, nm, ic, lb) in entries)
        cnt = len(entries)
        backfilled = 1
        n_backfill += 1
        native = None
        if fill_native and emptyish(em):
            plain = "; ".join(f"{nm} <{e}>".strip() if nm else e for (e, nm, ic, lb) in entries)
            native = f"{plain}  [derived: UMP contacts.db]"
            n_native_filled += 1
        updates.append((derived, cnt, backfilled, VERSION, rid, native))

    # apply
    cur = con.cursor()
    for u in updates:
        if len(u) == 6:
            derived, cnt, bf, ver, rid, native = u
            if native is not None:
                cur.execute("UPDATE cards SET contacts_emails=?, contacts_email_count=?, "
                            "contacts_backfilled=?, contacts_version=?, emails=? WHERE rowid=?",
                            (derived, cnt, bf, ver, native, rid))
            else:
                cur.execute("UPDATE cards SET contacts_emails=?, contacts_email_count=?, "
                            "contacts_backfilled=?, contacts_version=? WHERE rowid=?",
                            (derived, cnt, bf, ver, rid))
        else:
            derived, cnt, bf, ver, rid = u
            cur.execute("UPDATE cards SET contacts_emails=?, contacts_email_count=?, "
                        "contacts_backfilled=?, contacts_version=? WHERE rowid=?",
                        (derived, cnt, bf, ver, rid))
    con.commit()
    con.close()
    res = {"total": n_total, "matched_paper": n_match,
           "backfilled": n_backfill, "native_emails_filled": n_native_filled}
    if verbose:
        print(f"[contacts_backfill {VERSION}] total={n_total} matched={n_match} "
              f"backfilled={n_backfill} native_filled={n_native_filled}")
    return res

if __name__ == "__main__":
    main()
