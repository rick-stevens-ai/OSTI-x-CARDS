#!/usr/bin/env python3
"""
Idempotent card verify-scanner for the OSTI x-card farm.

Walks osti_cards_<year>/cards/<mode>/<id>.card.yaml and classifies every card:
  ok        -> parseable real card (YAML dict with content)
  sentinel  -> valid NO_X_FOUND result (a legitimate "no card" per Rick's rule)
  empty     -> 0-byte or whitespace-only  (INVALID -> regenerate)
  malformed -> not a sentinel AND not parseable YAML / not a dict / truncated (INVALID)
  orphan    -> card exists but its source .mmd is gone (flag; do not delete)

Also reports, per (year,mode):
  missing   -> source .mmd present but no card file at all (the genuine remaining gap)

Self-healing: with --delete-invalid, removes empty/malformed cards (and their qc sidecar)
so the resumable extractor regenerates them on the next pass. NEVER deletes ok/sentinel.
Idempotent: re-running after a clean pass reports 0 invalid and changes nothing.

Mechanical validation only (JSON/YAML parse, existence, byte-floor) -- no pattern-matching
judgment of card *content* (that is the extractor's LLM QC job).

Usage:
  python3 verify_cards.py --root /home/stevens --years 2019 2022 2023 2024 2026
  python3 verify_cards.py --root /home/stevens --years 2026 --delete-invalid
"""
import argparse, json, os, sys, glob, time

try:
    import yaml  # type: ignore
    HAVE_YAML = True
except Exception:
    HAVE_YAML = False

SENTINELS = (
    "NO_MODEL_FOUND", "NO_AGENT_FOUND", "NO_INSTRUMENT_FOUND",
    "NO_FACILITY_FOUND", "NO_SIMULATION_FOUND", "NO_WORKFLOW_FOUND",
    "NO_DATASET_FOUND", "NO_BVBRC_MAPPING", "NO_X_FOUND",
)
BYTE_FLOOR = 2  # anything <=2 non-ws bytes is empty

def is_sentinel(first_line: str) -> bool:
    s = first_line.strip()
    return any(s.startswith(tok) for tok in SENTINELS)

def classify(path: str):
    try:
        raw = open(path, "r", encoding="utf-8", errors="replace").read()
    except Exception as e:
        return ("malformed", f"read-error:{e}")
    stripped = raw.strip()
    if len(stripped) <= BYTE_FLOOR:
        return ("empty", "0-byte-or-whitespace")
    first = stripped.splitlines()[0] if stripped else ""
    if is_sentinel(first):
        return ("sentinel", first[:60])
    # real card: must parse as YAML mapping with at least one content key
    if HAVE_YAML:
        try:
            doc = yaml.safe_load(raw)
        except Exception as e:
            return ("malformed", f"yaml-parse:{str(e)[:50]}")
        if not isinstance(doc, dict) or not doc:
            return ("malformed", "not-a-mapping")
        return ("ok", f"{len(doc)}-keys")
    else:
        # no yaml lib: fall back to a structural sanity check (has 'key:' lines, not truncated mid-token)
        if ":" in stripped and not stripped.endswith(("|", ">", ":")):
            return ("ok", "struct-ok(no-yaml-lib)")
        return ("malformed", "struct-fail(no-yaml-lib)")

def source_ids(root: str, year: str):
    mmd = glob.glob(os.path.join(root, "osti_parse", "mmd", year, "*.mmd"))
    return {os.path.basename(p)[:-4] for p in mmd}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/home/stevens")
    ap.add_argument("--years", nargs="+", required=True)
    ap.add_argument("--delete-invalid", action="store_true",
                    help="remove empty/malformed cards + qc sidecar so resume regenerates them")
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    grand = {"ok": 0, "sentinel": 0, "empty": 0, "malformed": 0, "orphan": 0, "missing": 0}
    deleted = 0
    report_lines = []
    t0 = time.time()

    for year in args.years:
        cards_root = os.path.join(args.root, f"osti_cards_{year}", "cards")
        qc_root = os.path.join(args.root, f"osti_cards_{year}", "qc")
        if not os.path.isdir(cards_root):
            report_lines.append(f"[{year}] NO cards dir ({cards_root}) -- skipped")
            continue
        src = source_ids(args.root, year)
        modes = sorted(d for d in os.listdir(cards_root)
                       if os.path.isdir(os.path.join(cards_root, d)))
        year_counts = {k: 0 for k in grand}
        present_by_mode = {}
        for mode in modes:
            mdir = os.path.join(cards_root, mode)
            ids_here = set()
            for fn in os.listdir(mdir):
                if not fn.endswith(".card.yaml"):
                    continue
                pid = fn[:-len(".card.yaml")]
                ids_here.add(pid)
                fpath = os.path.join(mdir, fn)
                klass, _detail = classify(fpath)
                if klass in ("empty", "malformed") and pid not in src:
                    klass = "orphan"  # invalid AND no source -> can't regenerate; flag, don't delete
                year_counts[klass] += 1
                grand[klass] += 1
                if klass in ("empty", "malformed") and args.delete_invalid:
                    try:
                        os.remove(fpath)
                        qc = os.path.join(qc_root, mode, pid + ".qc.json")
                        if os.path.exists(qc):
                            os.remove(qc)
                        deleted += 1
                    except Exception:
                        pass
            present_by_mode[mode] = ids_here
            # missing = source ids with no card in this mode
            miss = len(src - ids_here) if src else 0
            year_counts["missing"] += miss
            grand["missing"] += miss
        report_lines.append(
            f"[{year}] modes={len(modes)} src_mmd={len(src)} | "
            f"ok={year_counts['ok']} sentinel={year_counts['sentinel']} "
            f"empty={year_counts['empty']} malformed={year_counts['malformed']} "
            f"orphan={year_counts['orphan']} missing_slots={year_counts['missing']}"
        )

    report_lines.append("")
    report_lines.append(
        f"GRAND ok={grand['ok']} sentinel={grand['sentinel']} "
        f"empty={grand['empty']} malformed={grand['malformed']} "
        f"orphan={grand['orphan']} missing_slots={grand['missing']} "
        f"| invalid(empty+malformed)={grand['empty']+grand['malformed']} "
        f"deleted={deleted} yaml_lib={HAVE_YAML} elapsed={time.time()-t0:.1f}s"
    )
    out = "\n".join(report_lines)
    print(out)
    if args.report:
        open(args.report, "w").write(out + "\n")

if __name__ == "__main__":
    main()
