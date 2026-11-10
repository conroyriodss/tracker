"""Promote B-prefixed cards to matched R-prefixed registry identifiers.

Each run writes a proposal artifact and records a ledger row. B-prefixed cards
without a probe match are retained because some are legitimately outside probe
scope; they are additionally recorded as advisory issues for tracking.
"""
import argparse
import os

import pandas as pd

from common import (
    INP,
    all_cards,
    append_id_remap,
    card_transaction,
    delete_card,
    gate,
    ledger_append,
    load_card_for_update,
    log_issue,
    name_matches,
    new_run_dir,
    require_card_absent,
    resolve_issues_not_in,
    save_card,
    slug,
    strip_acronym,
)


def report_unreconciled(b_cards, promotable, collisions, stale_threshold: int = 3):
    """Upsert one stable issue for every B-card that still exists on disk.

    A dry-run proposal is not reconciliation: the issue remains open until the
    B-prefixed source card is actually promoted or otherwise removed. The
    repo_uid + issue_type key keeps one row across runs; recurring advisories
    escalate after ``stale_threshold`` distinct run IDs and disappear from the
    open queue only when the B-card condition is no longer present.
    """
    promotable_by_uid = (
        {str(row.old_uid): str(row.new_uid) for row in promotable.itertuples(index=False)}
        if not promotable.empty
        else {}
    )
    collision_by_uid = (
        {str(row.old_uid): str(row.new_uid) for row in collisions.itertuples(index=False)}
        if not collisions.empty
        else {}
    )
    active_uids = sorted(b_cards)
    for uid in active_uids:
        card = b_cards[uid]
        name = card.get("identity", {}).get("name", "")
        if uid in collision_by_uid:
            reason = f"matched target {collision_by_uid[uid]} already exists"
        elif uid in promotable_by_uid:
            reason = f"promotion to {promotable_by_uid[uid]} is proposed but not committed"
        else:
            reason = "no probe name-set match"
        log_issue(
            step="reconcile_ids",
            severity="advisory",
            repo_uid=uid,
            issue_type="b_card_unreconciled",
            escalation_after=stale_threshold,
            summary=f"{uid} remains unreconciled after the probe pass",
            detail=(
                f"name={name!r}; reason={reason}. The card may be outside probe scope, "
                "may need a build_names alias round, or may require manual "
                "ACCEPT_RENAMED / MERGE_INTO adjudication."
            ),
        )
    resolved = resolve_issues_not_in(
        step="reconcile_ids",
        issue_type="b_card_unreconciled",
        active_repo_uids=active_uids,
        note="The card now has a non-colliding promotion or is no longer B-prefixed.",
    )
    if resolved:
        print(f"  📋 resolved {resolved} no-longer-active B-card issue(s)")
    return len(active_uids)


def probe_name_index(probe_csv):
    """Return slug and raw-name indexes keyed by CSV registry identifier."""
    probe = pd.read_csv(probe_csv)
    probe.columns = [column.strip() for column in probe.columns]
    slug_idx, name_by_cid = {}, {}
    for csv_id, group in probe.groupby("csv_id"):
        names = group["repository"].dropna()
        if names.empty:
            continue
        name = names.iloc[0]
        base, _ = strip_acronym(name)
        slug_idx[slug(base)] = int(csv_id)
        name_by_cid[int(csv_id)] = name
    return slug_idx, name_by_cid, probe


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", default="repo_api_results_v4_230_strict_endpoints.csv")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--min-conf", type=float, default=0.94)
    args = parser.parse_args()

    probe_path = os.path.join(INP, args.probe)
    slug_to_cid, probe_names, _ = probe_name_index(probe_path)
    cards = all_cards()
    b_cards = {uid: card for uid, card in cards.items() if uid.startswith("B")}
    taken_r = {uid for uid in cards if uid.startswith("R")}

    proposals = []
    for uid, card in b_cards.items():
        best_cid, best_type, best_confidence = None, None, 0.0
        for csv_id, name in probe_names.items():
            matched, basis = name_matches(card, name, fuzzy=args.min_conf)
            if matched:
                confidence = 1.0 if basis in {"exact_name", "acronym"} else args.min_conf
                if confidence > best_confidence:
                    best_cid, best_type, best_confidence = csv_id, f"nameset_{basis}", confidence
        if best_cid is None:
            continue
        new_uid = f"R{best_cid}"
        proposals.append({
            "old_uid": uid,
            "name": card.get("identity", {}).get("name"),
            "new_uid": new_uid,
            "csv_id": best_cid,
            "match_type": best_type,
            "confidence": best_confidence,
            "collision": new_uid in taken_r,
        })

    columns = ["old_uid", "name", "new_uid", "csv_id", "match_type", "confidence", "collision"]
    proposals_frame = pd.DataFrame(proposals, columns=columns)
    clean = proposals_frame.loc[~proposals_frame["collision"]].copy()
    collisions = proposals_frame.loc[proposals_frame["collision"]].copy()
    duplicate_targets = sorted(
        clean.loc[
            clean["new_uid"].duplicated(keep=False), "new_uid"
        ].astype(str).unique().tolist()
    ) if not clean.empty else []
    checks = {
        "probe loaded": (bool(slug_to_cid), f"{len(slug_to_cid)} probe names"),
        "unique promotion targets": (
            not duplicate_targets,
            f"duplicate target(s): {duplicate_targets[:5]}" if duplicate_targets else "ok",
        ),
        "no collision promotions": (
            collisions.empty,
            f"{len(collisions)} B→R would collide with existing R" + (f" e.g. {collisions['new_uid'].head(3).tolist()}" if not collisions.empty else ""),
        ),
        "proposals well-formed": (proposals_frame.empty or proposals_frame["new_uid"].notna().all(), "ok"),
    }
    ok = gate(checks)

    run_dir = new_run_dir("reconcile_ids")
    proposals_frame.to_csv(os.path.join(run_dir, "proposal.csv"), index=False)
    b_count, promotable_count, collision_count = len(b_cards), len(clean), len(collisions)
    print(f"\n B cards : {b_count}")
    print(f" promotable (B→R) : {promotable_count}")
    print(f" collisions (skip) : {collision_count}")
    if not clean.empty:
        print("\n sample promotions:\n", clean[["old_uid", "new_uid", "name", "match_type"]].head(10).to_string(index=False))

    applied = 0
    if args.apply and ok and not clean.empty:
        with card_transaction("reconcile_ids"):
            rows = list(clean.itertuples(index=False))
            locked_cards = {
                row.old_uid: load_card_for_update(row.old_uid, cards[row.old_uid])
                for row in rows
            }
            for new_uid in sorted({row.new_uid for row in rows}):
                require_card_absent(new_uid)
            for row in rows:
                card = locked_cards[row.old_uid]
                card["repo_uid"] = row.new_uid
                links = card.setdefault("links", [])
                if not any(
                    link.get("id_type") == "csv_id"
                    and str(link.get("id_value")) == str(row.csv_id)
                    for link in links
                ):
                    links.append(
                        {
                            "id_type": "csv_id",
                            "id_value": str(row.csv_id),
                            "url": None,
                            "tier": "registry",
                        }
                    )
                curation = card.setdefault("curation", {})
                note = f"[id reconciled {row.old_uid}→{row.new_uid} via probe name match]"
                curation["notes"] = (
                    str(curation.get("notes") or "") + " " + note
                ).strip()
                save_card(row.new_uid, card)
                delete_card(row.old_uid)
                append_id_remap(row.old_uid, row.new_uid, match_type=row.match_type)
                applied += 1
        print(f"\n ✓ transactionally applied {applied} promotions (B files renamed to R).")
    elif args.apply and not ok:
        print("\n ⛔ gate failed — not applying. Resolve collisions first.")
    else:
        print("\n DRY RUN — review proposal.csv, then re-run with --apply.")

    # Record the condition that remains after any committed promotion. This
    # prevents dry-run proposals from prematurely resolving persistent issues.
    remaining_cards = all_cards()
    remaining_b_cards = {
        uid: card for uid, card in remaining_cards.items() if uid.startswith("B")
    }
    unreconciled_count = report_unreconciled(remaining_b_cards, clean, collisions)

    ledger_append(
        "reconcile_ids",
        params={"probe": args.probe, "apply": args.apply, "min_conf": args.min_conf, "matcher": "name_set"},
        inputs={"probe": probe_path},
        outputs={
            "b_cards": b_count,
            "promotable": promotable_count,
            "unreconciled": unreconciled_count,
            "collisions": collision_count,
            "applied": applied,
        },
        gate_passed=ok,
        run_dir=run_dir,
    )
    print(f"\n✓ proposal → {run_dir}")


if __name__ == "__main__":
    main()
