"""Mechanical hygiene for repository identity name sets.

The normalization is shared with ``step_build_names.py`` so enrichment and cleanup
cannot alternately add and remove the same aliases. A valid no-op is success: once
cards are normalized, repeated dry-run and apply executions report zero changes and
pass the sanity gate.

Run (dry):  python step_clean_aliases.py
Apply:      python step_clean_aliases.py --apply
"""
from __future__ import annotations

import argparse
import copy

import pandas as pd

from common import (
    all_cards,
    card_transaction,
    gate,
    ledger_append,
    load_card_for_update,
    new_run_dir,
    save_card,
)
from identity_hygiene import clean_identity, identity_hygiene_issues


SAMPLE_COLUMNS = ["repo_uid", "name", "promoted_abbr", "dropped"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    cards = all_cards()
    expected_cards = copy.deepcopy(cards)
    changed = 0
    total_dropped = 0
    total_promoted = 0
    sample: list[dict[str, str]] = []
    changed_cards = {}

    for uid, card in cards.items():
        identity = card.get("identity", {}) or {}
        normalized, dropped, promoted = clean_identity(identity)
        if normalized == identity:
            continue
        changed += 1
        total_dropped += len(dropped)
        total_promoted += int(bool(promoted))
        if len(sample) < 15:
            sample.append(
                {
                    "repo_uid": uid,
                    "name": normalized.get("name", "")[:34],
                    "promoted_abbr": promoted or "",
                    "dropped": "; ".join(dropped)[:160],
                }
            )
        card["identity"] = normalized
        changed_cards[uid] = card

    hygiene_failures = {
        uid: identity_hygiene_issues(card.get("identity", {}) or {})
        for uid, card in cards.items()
    }
    hygiene_failures = {
        uid: issues for uid, issues in hygiene_failures.items() if issues
    }
    canonical_names_present = all(
        card.get("identity", {}).get("name") for card in cards.values()
    )
    outcome = (
        f"{changed} card(s) require normalization"
        if changed
        else "no changes required; identity aliases are already normalized"
    )
    checks = {
        "cards considered": (len(cards) > 0, f"{len(cards)} cards"),
        "canonical preserved": (
            canonical_names_present,
            "every card still has identity.name",
        ),
        "identity hygiene valid": (
            not hygiene_failures,
            "normalized output is valid"
            if not hygiene_failures
            else f"invalid cards: {list(hygiene_failures)[:5]}",
        ),
        "cleanup outcome": (True, outcome),
    }
    ok = gate(checks)

    applied = 0
    if args.apply and ok and changed_cards:
        with card_transaction("clean_aliases"):
            for uid in sorted(changed_cards):
                load_card_for_update(uid, expected_cards[uid])
            for uid in sorted(changed_cards):
                save_card(uid, changed_cards[uid])
                applied += 1
        print(f"\n  Transactionally applied {applied} alias cleanup(s).")
    elif args.apply and ok:
        print("\n  No card writes required; the current identity state is already normalized.")
    elif args.apply:
        print("\n  Gate failed; no card changes were written.")

    run_dir = new_run_dir("clean_aliases")
    pd.DataFrame(sample, columns=SAMPLE_COLUMNS).to_csv(
        run_dir + "/alias_cleanup_sample.csv", index=False
    )

    print(f"\n  cards adjusted             : {changed}")
    print(f"  redundant aliases dropped  : {total_dropped}")
    print(f"  acronyms promoted           : {total_promoted}")
    if sample:
        print("\n  sample:")
        for row in sample:
            print(
                f"    {row['repo_uid']:<6} {row['name']:<34} "
                f"+abbr={row['promoted_abbr'] or '-':<8} -dropped: {row['dropped']}"
            )
    else:
        print("\n  sample: no changes")
    if not args.apply:
        print("\n  DRY RUN - inspect alias_cleanup_sample.csv; zero changes is a valid result.")

    ledger_append(
        "clean_aliases",
        params={"apply": args.apply},
        inputs={},
        outputs={
            "cards": len(cards),
            "adjusted": changed,
            "applied": applied,
            "aliases_dropped": total_dropped,
            "abbr_promoted": total_promoted,
            "no_op": changed == 0,
            "hygiene_failures": len(hygiene_failures),
        },
        gate_passed=ok,
        run_dir=run_dir,
    )
    print(f"\nCompleted: {run_dir}")


if __name__ == "__main__":
    main()
