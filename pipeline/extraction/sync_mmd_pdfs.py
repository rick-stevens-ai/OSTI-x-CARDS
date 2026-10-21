#!/usr/bin/env python3
"""Pull pecans-rendered mmd->PDF files to the m1 site (data/mmd_pdf/<year>/<id>.pdf).
Uses cez tar-stream (same transport as sync_cards). INCREMENTAL: only streams files
newer than the last successful pull (marker file), so a 13G/112K-file tree is not
re-transferred every run. Safe to run on a timer. Bounded by SYNC_TIMEOUT.
"""
import os, subprocess, sys, time

SITE = os.path.expanduser("~/code/osti-cards-site")
DEST = os.path.join(SITE, "data", "mmd_pdf")
MARKER = os.path.join(SITE, "data", ".mmd_pdf_last_sync")
os.makedirs(DEST, exist_ok=True)
REMOTE = "/home/stevens/osti_parse/mmd_pdf"
# hard ceiling so this can never hang a cron slot; full re-stream of 13G ~ many min,
# incremental deltas are small. 1500s leaves headroom under any 1800s/3600s cap.
SYNC_TIMEOUT = int(os.environ.get("SYNC_TIMEOUT", "1500"))

def _last_sync_mins():
    """Return 'find -mmin' window (minutes) since last successful sync, or None for full."""
    if not os.path.isfile(MARKER):
        return None  # first run -> full pull
    try:
        age_s = time.time() - os.path.getmtime(MARKER)
        # add a 20-min overlap so nothing straddling the boundary is missed
        return int(age_s / 60) + 20
    except Exception:
        return None

def main():
    t0 = time.time()
    mins = _last_sync_mins()
    if mins is None:
        find_expr = "-type f -name '*.pdf'"
        mode = "FULL"
    else:
        find_expr = f"-type f -name '*.pdf' -mmin -{mins}"
        mode = f"INCR(<{mins}m)"
    # Build a file list on the remote side with find, then tar only those files.
    # Using tar --files-from keeps the stream to just the delta.
    cmd = (
        f"cez ssh pecans 'cd {REMOTE} 2>/dev/null && "
        f"find . {find_expr} 2>/dev/null > /tmp/_mmdpdf_delta.lst; "
        f"tar -cf - --exclude=\"._*\" -T /tmp/_mmdpdf_delta.lst 2>/dev/null || true' "
        f"| tar -C {DEST} -xf - 2>/dev/null"
    )
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=SYNC_TIMEOUT)
        rc = r.returncode
        stderr = r.stderr
    except subprocess.TimeoutExpired:
        rc = 124
        stderr = f"sync exceeded SYNC_TIMEOUT={SYNC_TIMEOUT}s"
    # count local total
    n = 0
    for root, _, files in os.walk(DEST):
        n += sum(1 for f in files if f.endswith(".pdf"))
    dt = time.time() - t0
    print(f"[sync-pdf] mode={mode} pulled; total mmd PDFs on m1 = {n} ({dt:.0f}s) rc={rc}")
    if rc == 0:
        # touch marker only on success
        open(MARKER, "a").close()
        os.utime(MARKER, None)
    else:
        if stderr:
            print("[sync-pdf] stderr tail:", str(stderr)[-400:])
        sys.exit(1)

if __name__ == "__main__":
    main()
