#!/usr/bin/env python3
"""
Card QUALITY aggregator — reads the extractor's LLM-judged QC sidecars and reports
quality distributions across all cards. Quality is LLM-judged (no pattern matching):
each real card has qc/<mode>/<id>.qc.json with a rubric:
   {"scores": {"schema_followed":1-5,"grounded":1-5,"clean":1-5,"completeness":1-5},
    "verdict":"PASS|FAIL|QUARANTINE","sentinel":bool,"reasons":[...]}
Sentinels carry {"verdict":"PASS","sentinel":true}.

Reports per (year): #real-card QCs, #sentinel QCs, verdict counts, mean rubric scores,
and the count of LOW-QUALITY real cards (any axis <=3) that deserve regeneration.

Mechanical aggregation of existing LLM judgments only — no content pattern-matching.
"""
import argparse, json, os, glob, time
from collections import Counter

AXES = ("schema_followed", "grounded", "clean", "completeness")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/home/stevens")
    ap.add_argument("--years", nargs="+", required=True)
    ap.add_argument("--low-threshold", type=int, default=3,
                    help="real card with any rubric axis <= this = low quality")
    ap.add_argument("--report", default="")
    ap.add_argument("--lowlist", default="",
                    help="write paths of low-quality real cards here (for regeneration)")
    args = ap.parse_args()

    lines = []
    low_paths = []
    grand = Counter()
    grand_axis_sum = {a: 0 for a in AXES}
    grand_real = 0
    t0 = time.time()

    for year in args.years:
        qc_root = os.path.join(args.root, f"osti_cards_{year}", "qc")
        if not os.path.isdir(qc_root):
            lines.append(f"[{year}] no qc dir"); continue
        verdicts = Counter()
        n_real = n_sent = n_unscored = n_low = n_badjson = 0
        axis_sum = {a: 0 for a in AXES}
        for qf in glob.glob(os.path.join(qc_root, "*", "*.qc.json")):
            try:
                d = json.load(open(qf))
            except Exception:
                n_badjson += 1
                continue
            v = d.get("verdict", "?")
            verdicts[v] += 1
            if d.get("sentinel"):
                n_sent += 1
                continue
            scores = d.get("scores")
            if isinstance(scores, dict) and all(a in scores for a in AXES):
                n_real += 1
                worst = 6
                for a in AXES:
                    try:
                        val = int(scores[a])
                    except Exception:
                        val = 0
                    axis_sum[a] += val
                    worst = min(worst, val)
                if worst <= args.low_threshold:
                    n_low += 1
                    # map qc path -> card path
                    cp = qf.replace(os.sep + "qc" + os.sep, os.sep + "cards" + os.sep)
                    cp = cp.replace(".qc.json", ".card.yaml")
                    low_paths.append(cp)
            else:
                n_unscored += 1  # PASS/None real card without full rubric (older sidecar)
        means = {a: (axis_sum[a] / n_real) if n_real else 0.0 for a in AXES}
        lines.append(
            f"[{year}] real_scored={n_real} sentinel={n_sent} unscored={n_unscored} "
            f"badjson={n_badjson} verdicts={dict(verdicts)} "
            f"mean(schema/grounded/clean/complete)="
            f"{means['schema_followed']:.2f}/{means['grounded']:.2f}/"
            f"{means['clean']:.2f}/{means['completeness']:.2f} "
            f"low_quality(any<= {args.low_threshold})={n_low}"
        )
        grand.update(verdicts)
        grand_real += n_real
        for a in AXES:
            grand_axis_sum[a] += axis_sum[a]

    gmeans = {a: (grand_axis_sum[a] / grand_real) if grand_real else 0.0 for a in AXES}
    lines.append("")
    lines.append(
        f"GRAND real_scored={grand_real} verdicts={dict(grand)} "
        f"mean(schema/grounded/clean/complete)="
        f"{gmeans['schema_followed']:.2f}/{gmeans['grounded']:.2f}/"
        f"{gmeans['clean']:.2f}/{gmeans['completeness']:.2f} "
        f"low_quality_total={len(low_paths)} elapsed={time.time()-t0:.1f}s"
    )
    out = "\n".join(lines)
    print(out)
    if args.report:
        open(args.report, "w").write(out + "\n")
    if args.lowlist and low_paths:
        open(args.lowlist, "w").write("\n".join(low_paths) + "\n")

if __name__ == "__main__":
    main()
