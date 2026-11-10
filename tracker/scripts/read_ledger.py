"""
read_ledger.py — turn run_ledger.csv into a readable history.

  python read_ledger.py                 # all runs, newest first
  python read_ledger.py --step merge    # only 'merge' runs
  python read_ledger.py --last 5        # last 5 runs
  python read_ledger.py --failed        # only runs whose gate failed
  python read_ledger.py --wide          # expand params/outputs columns

Answers "which parameters produced this output, and did it pass?" at a glance.
"""
import argparse, json, os
from common import LEDGER
import pandas as pd


def _fmt(d_json, keys=None):
    try:
        d = json.loads(d_json)
    except Exception:
        return str(d_json)
    if keys:
        d = {k: d[k] for k in keys if k in d}
    return "  ".join(f"{k}={v}" for k, v in d.items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step")
    ap.add_argument("--last", type=int)
    ap.add_argument("--failed", action="store_true")
    ap.add_argument("--wide", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(LEDGER):
        print("no run_ledger.csv yet — run a step first."); return
    df = pd.read_csv(LEDGER)
    df = df.sort_values("ts", ascending=False)

    if args.step:   df = df[df.step == args.step]
    if args.failed: df = df[~df.gate_passed.astype(bool)]
    if args.last:   df = df.head(args.last)

    if df.empty:
        print("no matching runs."); return

    print(f"\n{'ts':<16} {'step':<20} {'gate':<5} outputs")
    print("─" * 90)
    for _, r in df.iterrows():
        g = "PASS" if r.gate_passed else "FAIL"
        print(f"{r.ts:<16} {r.step:<20} {g:<5} {_fmt(r.outputs)}")
        if args.wide:
            print(f"{'':<16} params : {_fmt(r.params)}")
            print(f"{'':<16} inputs : {_fmt(r.inputs)}")
            print(f"{'':<16} dir    : {r.run_dir}")
    print(f"\n{len(df)} run(s).  ledger: {LEDGER}")

    # quick health summary
    fails = int((~df.gate_passed.astype(bool)).sum())
    if fails:
        print(f"⚠ {fails} failed gate(s) in this view.")


if __name__ == "__main__":
    main()
