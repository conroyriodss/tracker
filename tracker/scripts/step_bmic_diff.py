"""
step_bmic_diff.py
Diff an uploaded BMIC export against the current cards/, matching each BMIC row
against the FULL name set of every card (name, abbreviation, full_name, aliases).
A renamed or typo-fixed card therefore no longer shows as a phantom add+drop.

Run:  python step_bmic_diff.py --bmic BMIC_download.csv
Writes runs/bmic_diff/<ts>/: added.csv, dropped.csv, name_collisions.csv
"""
import argparse, os
from common import (INP, slug, strip_acronym, all_cards, gate,
                    ledger_append, new_run_dir, index_cards_by_name)
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bmic", default="BMIC_download.csv")
    args = ap.parse_args()

    bmic_path = os.path.join(INP, args.bmic)
    bmic = pd.read_csv(bmic_path); bmic.columns = [c.strip() for c in bmic.columns]
    namecol = "Repository_Name" if "Repository_Name" in bmic.columns else bmic.columns[2]

    cards = all_cards()
    name_idx, collisions = index_cards_by_name(cards)

    matched_uids = set()
    added_rows = []
    for _, r in bmic.iterrows():
        nm = r[namecol]
        base = slug(strip_acronym(nm)[0])
        _, acr = strip_acronym(nm)
        acr_s = slug(acr) if acr else ""
        hit = name_idx.get(base) or (name_idx.get(acr_s) if acr_s else None)
        if hit:
            matched_uids.add(hit)
        else:
            added_rows.append({"name": nm})

    added = pd.DataFrame(added_rows)
    dropped = pd.DataFrame([{"repo_uid": u, "name": c.get("identity", {}).get("name")}
                            for u, c in cards.items() if u not in matched_uids])

    checks = {
        "bmic rows loaded":       (len(bmic) > 0, f"{len(bmic)} rows from {args.bmic}"),
        "added+matched≈bmic":     (len(added) + len(matched_uids) >= len(bmic) * 0.95,
                                   f"added {len(added)} + matched {len(matched_uids)} vs {len(bmic)}"),
        "no card-name collisions":(len(collisions) == 0,
                                   f"{len(collisions)} name→multi-card"
                                   + (f" e.g. {collisions[:2]}" if collisions else "")),
    }
    ok = gate(checks)

    run_dir = new_run_dir("bmic_diff")
    added.to_csv(os.path.join(run_dir, "added.csv"), index=False)
    dropped.to_csv(os.path.join(run_dir, "dropped.csv"), index=False)
    pd.DataFrame(collisions, columns=["name_slug", "uid_a", "uid_b"]).to_csv(
        os.path.join(run_dir, "name_collisions.csv"), index=False)

    print(f"\n  ADDED   : {len(added):>4}  (in BMIC, matches no card name)")
    print(f"  DROPPED : {len(dropped):>4}  (card matches no BMIC row)")
    print(f"  MATCHED : {len(matched_uids):>4}")
    if len(added):
        print("\n  sample ADDED:\n   ", "\n    ".join(added.name.head(8).astype(str)))
    if len(dropped):
        print("\n  DROPPED:\n", dropped.head(10).to_string(index=False))
    if collisions:
        print(f"\n  ⚠ {len(collisions)} name collisions (two cards share a name) — see name_collisions.csv")

    ledger_append("bmic_diff",
                  params={"bmic": args.bmic, "matcher": "name_set"},
                  inputs={"bmic": bmic_path},
                  outputs={"bmic_rows": len(bmic), "added": len(added),
                           "dropped": len(dropped), "matched": len(matched_uids),
                           "name_collisions": len(collisions)},
                  gate_passed=ok, run_dir=run_dir)
    print(f"\n✓ worklist → {run_dir}")


if __name__ == "__main__":
    main()
