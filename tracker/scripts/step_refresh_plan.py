"""
step_refresh_plan.py
For every card and every requested block, decide what this quarter needs:
  keep_fresh | build_missing | rebuild_stale(N mo) | skip_confirmed | no_builder

Run:  python step_refresh_plan.py --blocks identity,interoperability --stale-months 12
Params:
  --blocks LIST        comma list of blocks to consider
  --stale-months N     rebuild blocks with as_of older than N months (default 12)
  --force              ignore freshness; plan to rebuild every requested block
  --no-protect         allow planning over human_confirmed blocks (default: protect them)

Writes runs/refresh_plan/<ts>/refresh_plan.csv and logs a ledger row.
The plan is advisory — it says WHAT to rebuild; the block notebooks DO the rebuild.
"""
import argparse, os
from common import (all_cards, block_asof, block_state, months_since,
                    gate, ledger_append, new_run_dir)
import pandas as pd

# blocks that currently have a working builder; others are reported as no_builder
BUILDERS_AVAILABLE = {"identity", "interoperability", "resource_profile", "policy_context", "repository_status", "publisher_context"}


def plan_action(card, block, stale_months, force, protect):
    st = block_state(card, block)
    if protect and st == "human_confirmed" and not force:
        return "skip_confirmed"
    present = (block == "identity") or (block in (card.get("blocks", {}) or {}))
    if block not in BUILDERS_AVAILABLE:
        return "no_builder"
    if force:
        return "rebuild_forced"
    if not present:
        return "build_missing"
    age = months_since(block_asof(card, block))
    if age > stale_months:
        return f"rebuild_stale({age}mo)"
    return "keep_fresh"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", default="identity,interoperability")
    ap.add_argument("--stale-months", type=int, default=12)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-protect", action="store_true")
    args = ap.parse_args()

    blocks = [b.strip() for b in args.blocks.split(",") if b.strip()]
    protect = not args.no_protect
    cards = all_cards()

    rows = []
    for uid, card in cards.items():
        for block in blocks:
            action = plan_action(card, block, args.stale_months, args.force, protect)
            rows.append({
                "repo_uid": uid, "block": block,
                "as_of": block_asof(card, block),
                "months_old": months_since(block_asof(card, block)),
                "review_state": block_state(card, block),
                "action": action,
            })
    plan = pd.DataFrame(rows)

    todo = plan[~plan.action.isin(["keep_fresh", "skip_confirmed", "no_builder"])]

    checks = {
        "cards considered > 0":  (len(cards) > 0, f"{len(cards)} cards"),
        "plan rows == cards×blocks": (len(plan) == len(cards) * len(blocks),
                                      f"{len(plan)} == {len(cards)}×{len(blocks)}"),
        "todo is well-formed":   (todo.repo_uid.notna().all(), f"{len(todo)} actionable"),
    }
    ok = gate(checks)

    run_dir = new_run_dir("refresh_plan")
    plan.to_csv(os.path.join(run_dir, "refresh_plan.csv"), index=False)
    todo.to_csv(os.path.join(run_dir, "todo.csv"), index=False)

    print("\n  action counts:")
    for a, n in plan.action.value_counts().items():
        print(f"    {a:<22} {n}")
    print("\n  by block × action:")
    print(plan.groupby(["block", "action"]).size().to_string())
    print(f"\n  → {len(todo)} block-rebuilds needed this run (of {len(plan)} considered)")

    ledger_append("refresh_plan",
                  params={"blocks": blocks, "stale_months": args.stale_months,
                          "force": args.force, "protect": protect},
                  inputs={},   # reads cards/, not a single hashable file
                  outputs={"cards": len(cards), "plan_rows": len(plan),
                           "todo": len(todo),
                           "counts": plan.action.value_counts().to_dict()},
                  gate_passed=ok, run_dir=run_dir)
    print(f"\n✓ plan → {run_dir}")


if __name__ == "__main__":
    main()
