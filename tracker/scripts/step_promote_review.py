"""Apply human adjudications to cards, including auditable MERGE_INTO decisions.

The input CSV requires repo_uid and verdict. MERGE_INTO additionally requires
relation_target (the surviving card UID) and a non-empty note. In apply mode,
the retiring source card is moved to cards/_merged/, which is intentionally
invisible to common.all_cards() because that function scans only CARDS' top level.
"""
import argparse
import datetime as dt
import io
import os
import re
from contextlib import nullcontext
from urllib.parse import urlparse

import pandas as pd
import yaml

from common import (
    CARDS,
    FileLock,
    INP,
    NOW,
    PROJECT,
    active_card_transaction,
    all_cards,
    archive_card,
    atomic_write_yaml,
    append_id_remap,
    card_transaction,
    current_run_id,
    gate,
    ledger_append,
    load_card,
    load_card_for_update,
    log_issue,
    new_run_dir,
    save_card,
    slug,
    strip_acronym,
)

YM = NOW.strftime("%Y-%m")
BLOCKS_DIR = os.path.join(PROJECT, "blocks")
SHARED_HOSTS = os.path.join(BLOCKS_DIR, "shared_hosts.yaml")
MERGED_DIR = os.path.join(CARDS, "_merged")

VERDICTS = {
    "ACCEPT",
    "ACCEPT_ALIAS",
    "ACCEPT_RENAMED",
    "ACCEPT_WITH_ALIAS_CLEANUP",
    "ACCEPT_WITH_TYPO_FIX",
    "ACCEPT_WITH_NORMALIZATION",
    "ALIAS_DEMOTE_TO_ORG",
    "REJECT_WRONG_RRID",
    "REVIEW",
    "MERGE_INTO",
    "REJECT_CANDIDATE",
}

SEED_HOSTS = {
    "ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "www.icpsr.umich.edu", "icpsr.umich.edu",
    "jax.org", "www.jax.org", "ebi.ac.uk", "www.ebi.ac.uk", "nih.gov",
}


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    value = str(value).strip()
    return None if value.lower() in {"", "nan", "none", "-"} else value


def _list(value):
    value = _clean(value)
    return [part.strip() for part in re.split(r";", value) if part.strip()] if value else []


def host(url):
    value = _clean(url)
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    try:
        return (urlparse(value).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def load_shared_hosts():
    hosts = {value.removeprefix("www.") for value in SEED_HOSTS}
    if os.path.exists(SHARED_HOSTS):
        with open(SHARED_HOSTS, encoding="utf-8") as handle:
            hosts |= set((yaml.safe_load(handle) or {}).get("shared_hosts", []))
    return hosts


def save_shared_hosts(hosts):
    payload = {
        "shared_hosts": sorted(hosts),
        "_note": "hosts that must not auto-match on homepage alone; require host+name agreement",
        "as_of": YM,
    }
    os.makedirs(BLOCKS_DIR, exist_ok=True)
    transaction = active_card_transaction(CARDS)
    if transaction is not None:
        transaction.write_yaml(SHARED_HOSTS, payload, operation="save_shared_hosts")
    else:
        atomic_write_yaml(SHARED_HOSTS, payload)


def add_alias(identity, alias, source):
    alias = _clean(alias)
    if not alias:
        return False
    canonical = {
        slug(strip_acronym(value)[0])
        for value in (identity.get("name"), identity.get("full_name"), identity.get("abbreviation"))
        if value
    }
    if slug(strip_acronym(alias)[0]) in canonical:
        return False
    aliases = set(identity.get("aliases") or [])
    if alias in aliases:
        return False
    aliases.add(alias)
    provenance = dict(identity.get("name_provenance") or {})
    provenance[alias] = {"source": source, "as_of": YM}
    identity["aliases"] = sorted(aliases)
    identity["name_provenance"] = provenance
    return True


def drop_alias(identity, alias):
    alias = _clean(alias)
    if not alias:
        return False
    aliases = [value for value in (identity.get("aliases") or []) if value != alias]
    changed = len(aliases) != len(identity.get("aliases") or [])
    if aliases:
        identity["aliases"] = aliases
    else:
        identity.pop("aliases", None)
    provenance = {key: value for key, value in (identity.get("name_provenance") or {}).items() if key != alias}
    if provenance:
        identity["name_provenance"] = provenance
    else:
        identity.pop("name_provenance", None)
    return changed


def drop_rrid(card):
    before = len(card.get("links", []))
    card["links"] = [link for link in card.get("links", []) if link.get("id_type") != "rrid"]
    return before != len(card["links"])


def validate_merge_rows(rows, cards):
    """Validate destructive merge decisions before any dry-run mutation or write."""
    errors, valid = [], []
    retiring_this_batch = {_clean(row.get("repo_uid")) for row in rows if _clean(row.get("repo_uid"))}
    target_counts = {}
    for row in rows:
        survivor = _clean(row.get("relation_target"))
        if survivor:
            target_counts[survivor] = target_counts.get(survivor, 0) + 1

    for row in rows:
        retiring = _clean(row.get("repo_uid"))
        survivor = _clean(row.get("relation_target"))
        if not retiring:
            errors.append("MERGE_INTO row is missing repo_uid")
        elif not survivor:
            errors.append(f"{retiring}: MERGE_INTO requires relation_target (survivor uid)")
        elif retiring not in cards:
            errors.append(f"{retiring}: MERGE_INTO source card does not exist")
        elif survivor not in cards:
            errors.append(f"{retiring}: MERGE_INTO target '{survivor}' does not exist")
        elif survivor == retiring:
            errors.append(f"{retiring}: MERGE_INTO target cannot equal source")
        elif survivor in retiring_this_batch:
            errors.append(f"{retiring}: MERGE_INTO target '{survivor}' is also being merged away; resolve in two rounds")
        elif target_counts.get(survivor, 0) > 1:
            errors.append(f"{retiring}: MERGE_INTO target '{survivor}' has multiple retiring sources in this batch; resolve separately")
        elif not _clean(row.get("note")):
            errors.append(f"{retiring}: MERGE_INTO requires a note (destructive action)")
        else:
            valid.append(row)
    return errors, valid


def _merge_preview(retiring_card, survivor_card, row):
    survivor_identity = survivor_card.setdefault("identity", {})
    existing_aliases = set(survivor_identity.get("aliases") or [])
    retiring_identity = retiring_card.get("identity", {}) or {}
    carried_aliases = set()
    if retiring_identity.get("name"):
        carried_aliases.add(retiring_identity["name"])
    carried_aliases.update(value for value in (retiring_identity.get("aliases") or []) if value)
    carried_aliases.update(_list(row.get("add_aliases")))
    new_aliases = sorted((existing_aliases | carried_aliases) - {_clean(survivor_identity.get("name"))})
    survivor_identity["aliases"] = new_aliases

    rrid = _clean(row.get("rrid"))
    links = survivor_card.setdefault("links", [])
    if rrid and not any(link.get("id_type") == "rrid" and _clean(link.get("id_value")) == rrid for link in links):
        links.append({
            "id_type": "rrid",
            "id_value": rrid,
            "url": f"https://scicrunch.org/resolver/RRID:{rrid}",
            "tier": "attestation",
            "as_of": YM,
            "source": f"merge:{_clean(row.get('repo_uid'))}",
        })

    keys = {(link.get("id_type"), link.get("id_value")) for link in links}
    for link in retiring_card.get("links", []) or []:
        key = (link.get("id_type"), link.get("id_value"))
        if link.get("id_value") and key not in keys:
            carried = dict(link)
            source = _clean(carried.get("source")) or ""
            carried["source"] = f"{source} (carried from {_clean(row.get('repo_uid'))} merge)".strip()
            links.append(carried)
            keys.add(key)

    curation = survivor_card.setdefault("curation", {})
    stamp = f"[merged {_clean(row.get('repo_uid'))}→{_clean(row.get('relation_target'))} via MERGE_INTO adjudication: {_clean(row.get('note'))}]"
    curation["notes"] = (str(curation.get("notes") or "") + " " + stamp).strip()
    curation["review_state"] = "human_confirmed"
    curation["reviewed_by"] = "adjudication"
    curation["last_reviewed"] = str(NOW.date())
    return len(new_aliases) - len(existing_aliases), len(links)


def apply_merge_into(row, dry_run=True):
    """Merge a retiring card into its validated survivor, then archive the source."""
    retiring_uid = _clean(row.get("repo_uid"))
    survivor_uid = _clean(row.get("relation_target"))
    retiring_card = load_card(retiring_uid)
    survivor_card = load_card(survivor_uid)
    if retiring_card is None or survivor_card is None:
        detail = (
            f"retiring_exists={retiring_card is not None}; "
            f"survivor_exists={survivor_card is not None}"
        )
        log_issue(
            "promote_review",
            "blocking",
            f"MERGE_INTO {retiring_uid}->{survivor_uid} card missing at apply time",
            detail,
            repo_uid=retiring_uid or "",
            issue_type="merge_card_missing_at_apply",
        )
        raise FileNotFoundError(
            f"MERGE_INTO card set changed after validation: {retiring_uid}->{survivor_uid}; {detail}"
        )

    aliases_added, total_aliases = _merge_preview(retiring_card, survivor_card, row)
    detail = f"merge -> {survivor_uid}; +{aliases_added} aliases; survivor aliases={total_aliases}"
    if dry_run:
        return {"repo_uid": retiring_uid, "verdict": "MERGE_INTO", "detail": f"[DRY] would {detail}"}

    archive_path = os.path.join(MERGED_DIR, f"{retiring_uid}.yaml")
    if os.path.exists(archive_path):
        raise FileExistsError(f"Refusing to overwrite archived merged card: {archive_path}")
    save_card(survivor_uid, survivor_card)
    archive_card(retiring_uid, archive_subdir="_merged")
    append_id_remap(retiring_uid, survivor_uid, match_type="merge_adjudicated")
    return {
        "repo_uid": retiring_uid,
        "verdict": "MERGE_INTO",
        "detail": f"{detail}; archived to _merged/",
    }


def apply_row(card, row, hosts_accum):
    verdict = _clean(row.get("verdict"))
    identity = card.setdefault("identity", {})
    actions = []
    if verdict == "ACCEPT":
        actions.append("noop")
    elif verdict in {"ACCEPT_ALIAS", "ACCEPT_RENAMED"}:
        for alias in _list(row.get("add_aliases")):
            if add_alias(identity, alias, "adjudication"):
                actions.append(f"+alias:{alias}")
    elif verdict == "ACCEPT_WITH_ALIAS_CLEANUP":
        for alias in _list(row.get("add_aliases")):
            if add_alias(identity, alias, "adjudication"):
                actions.append(f"+alias:{alias}")
        for alias in _list(row.get("drop_aliases")):
            if drop_alias(identity, alias):
                actions.append(f"-alias:{alias}")
    elif verdict in {"ACCEPT_WITH_TYPO_FIX", "ACCEPT_WITH_NORMALIZATION"}:
        new_name, old_name = _clean(row.get("canonical_name")), _clean(identity.get("name"))
        if new_name and new_name != old_name:
            if old_name:
                add_alias(identity, old_name, "prior_canonical")
            identity["name"] = new_name
            actions.append(f"rename:{old_name}→{new_name}")
        abbreviation = _clean(row.get("abbreviation"))
        if abbreviation:
            identity["abbreviation"] = abbreviation
            actions.append("set_abbr")
        for alias in _list(row.get("add_aliases")):
            if add_alias(identity, alias, "adjudication"):
                actions.append(f"+alias:{alias}")
        for alias in _list(row.get("drop_aliases")):
            if drop_alias(identity, alias):
                actions.append(f"-alias:{alias}")
    elif verdict == "ALIAS_DEMOTE_TO_ORG":
        organization = _clean(row.get("move_to_org"))
        if organization:
            drop_alias(identity, organization)
            governance = card.setdefault("blocks", {}).setdefault("governance", {})
            governance.setdefault("_meta", {"block_version": "1.0", "source": "adjudication", "tier": "attestation", "as_of": YM, "review_state": "human_confirmed"})
            governance["steward_org"] = organization
            actions.append(f"org:{organization}")
    elif verdict == "REJECT_WRONG_RRID":
        if drop_rrid(card):
            actions.append("drop_rrid")
        homepage_host = host(identity.get("homepage"))
        if homepage_host and homepage_host in hosts_accum.get("_multi", set()):
            hosts_accum["hosts"].add(homepage_host)
            actions.append(f"host→blocklist:{homepage_host}")
        elif homepage_host:
            actions.append(f"host_kept(dedicated):{homepage_host}")
        new_name = _clean(row.get("canonical_name"))
        if new_name and new_name != _clean(identity.get("name")):
            old_name = _clean(identity.get("name"))
            if old_name:
                add_alias(identity, old_name, "prior_canonical")
            identity["name"] = new_name
            actions.append(f"rename:{old_name}→{new_name}")
    elif verdict == "REVIEW":
        relation_type, target = _clean(row.get("relation_type")), _clean(row.get("relation_target"))
        if relation_type and target:
            relations = card.setdefault("blocks", {}).setdefault("relations", {})
            relations.setdefault("_meta", {"block_version": "1.0", "source": "adjudication", "tier": "attestation", "as_of": YM, "review_state": "needs_review"})
            values = relations.setdefault(relation_type, [])
            if not isinstance(values, list):
                values = [values]
                relations[relation_type] = values
            if target not in values:
                values.append(target)
            actions.append(f"relation:{relation_type}→{target}")
    else:
        raise ValueError(f"Unexpected non-merge verdict: {verdict}")

    curation = card.setdefault("curation", {})
    curation["review_state"] = "human_confirmed"
    curation["reviewed_by"] = "adjudication"
    curation["last_reviewed"] = str(NOW.date())
    return actions


def emit_template(cards, out_path):
    rows = []
    for uid, card in cards.items():
        if card.get("curation", {}).get("review_state") == "human_confirmed":
            continue
        rows.append({
            "repo_uid": uid, "target": "", "verdict": "", "canonical_name": "", "abbreviation": "",
            "add_aliases": "", "drop_aliases": "", "move_to_org": "", "rrid": "",
            "relation_type": "", "relation_target": "", "note": card.get("identity", {}).get("name", ""),
        })
    pd.DataFrame(rows).to_csv(out_path, index=False)
    return len(rows)


def load_id_remap(cards=None):
    """Resolve current IDs from persistent remaps plus historical reconcile notes."""
    direct = {}
    path = os.path.join(PROJECT, "id_remap.csv")
    if os.path.exists(path):
        remaps = pd.read_csv(path, dtype=str).fillna("")
        if {"old_uid", "new_uid"}.issubset(remaps.columns):
            for row in remaps.itertuples(index=False):
                direct.setdefault(str(row.old_uid), str(row.new_uid))
    if cards:
        pattern = re.compile(r"\[id reconciled (\S+)→(\S+) via probe name match\]")
        for card in cards.values():
            notes = (card.get("curation", {}) or {}).get("notes", "") or ""
            for old_uid, new_uid in pattern.findall(notes):
                direct.setdefault(old_uid, new_uid)
    resolved = {}
    for old_uid in direct:
        current, seen = old_uid, {old_uid}
        while current in direct and direct[current] not in seen:
            seen.add(current)
            current = direct[current]
        resolved[old_uid] = current
    return resolved


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--adjudications", default="adjudications.csv")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--emit-template", action="store_true")
    parser.add_argument(
        "--include-applied",
        action="store_true",
        help="Reprocess rows carrying an applied_at receipt (normally skipped).",
    )
    args = parser.parse_args()

    cards = all_cards()
    if args.emit_template:
        run_dir = new_run_dir("promote_review")
        count = emit_template(cards, os.path.join(run_dir, "review_template.csv"))
        print(f"✓ emitted blank template with {count} open-queue rows → {run_dir}/review_template.csv")
        return

    adjudications_path = os.path.join(INP, args.adjudications)
    with open(adjudications_path, "rb") as handle:
        adjudications_source_bytes = handle.read()
    all_adjudications = pd.read_csv(
        io.BytesIO(adjudications_source_bytes), dtype=object
    ).fillna("")
    all_adjudications.columns = [column.strip() for column in all_adjudications.columns]
    required = {"repo_uid", "verdict"}
    missing_columns = sorted(required - set(all_adjudications.columns))
    if missing_columns:
        raise ValueError(f"adjudications file is missing required columns: {missing_columns}")
    for column in ("canonical_name", "abbreviation", "add_aliases", "drop_aliases", "move_to_org", "rrid", "relation_type", "relation_target", "note", "applied_at", "applied_run_id"):
        if column not in all_adjudications.columns:
            all_adjudications[column] = ""
    all_adjudications["repo_uid"] = all_adjudications["repo_uid"].map(_clean)
    all_adjudications["verdict"] = all_adjudications["verdict"].map(_clean)
    all_adjudications["_source_index"] = all_adjudications.index
    adjudications = all_adjudications.loc[
        all_adjudications["verdict"].notna()
        & all_adjudications["verdict"].astype(str).str.strip().astype(bool)
    ].copy()
    if not args.include_applied:
        adjudications = adjudications.loc[
            ~adjudications["applied_at"].astype(str).str.strip().astype(bool)
        ].copy()

    remap = load_id_remap(cards)
    translated = 0
    def translate(uid):
        nonlocal translated
        if uid in remap and remap[uid] != uid:
            translated += 1
            return remap[uid]
        return uid
    adjudications["repo_uid"] = adjudications["repo_uid"].map(translate)

    unknown = sorted(set(adjudications["verdict"].dropna()) - VERDICTS)
    typo_missing = adjudications.loc[
        adjudications["verdict"].isin({"ACCEPT_WITH_TYPO_FIX", "ACCEPT_WITH_NORMALIZATION"})
        & ~adjudications["canonical_name"].astype(str).str.strip().astype(bool)
    ]
    card_required = adjudications.loc[adjudications["verdict"] != "REJECT_CANDIDATE"]
    absent = sorted(set(card_required["repo_uid"].dropna()) - set(cards))
    merge_rows = adjudications.loc[adjudications["verdict"] == "MERGE_INTO"].to_dict("records")
    merge_errors, valid_merge_rows = validate_merge_rows(merge_rows, cards)

    hosts = load_shared_hosts()
    hosts_before = len(hosts)
    host_counts = {}
    for card in cards.values():
        value = host(card.get("identity", {}).get("homepage"))
        if value:
            host_counts[value] = host_counts.get(value, 0) + 1
    hosts_accum = {"hosts": hosts, "_multi": {value for value, count in host_counts.items() if count > 1}}

    has_work = len(adjudications) > 0
    checks = {
        "unapplied adjudications loaded": (True, f"{len(adjudications)} row(s) selected"),
        "all verdicts known": (not unknown, f"unknown: {unknown}" if unknown else "ok"),
        "typo_fix has name": (typo_missing.empty, f"{len(typo_missing)} typo rows missing canonical_name"),
        "all repo_uids exist": (not absent, f"absent: {absent[:5]}" if absent else "ok"),
        "MERGE_INTO rows valid": (not merge_errors, f"{len(merge_errors)} invalid merge row(s): {merge_errors[:3]}" if merge_errors else f"{len(valid_merge_rows)} valid merge row(s)"),
    }
    ok = gate(checks)
    if translated:
        print(f" ↻ translated {translated} adjudication row(s) via id_remap.csv (old id → current id)")
    if merge_errors:
        for error in merge_errors:
            possible_uid = error.split(":", 1)[0].strip() if ":" in error else ""
            log_issue(
                "promote_review",
                "blocking",
                "Invalid MERGE_INTO adjudication",
                error,
                repo_uid=possible_uid if possible_uid in cards else "",
                issue_type="invalid_merge_adjudication",
            )

    protected_uids = set()
    for row in adjudications.to_dict("records"):
        uid = _clean(row.get("repo_uid"))
        verdict = _clean(row.get("verdict"))
        if verdict != "REJECT_CANDIDATE" and uid:
            protected_uids.add(uid)
        if verdict == "MERGE_INTO":
            target = _clean(row.get("relation_target"))
            if target:
                protected_uids.add(target)

    applied, per_verdict, action_log = 0, {}, []
    write_context = (
        card_transaction("promote_review")
        if args.apply and ok and has_work
        else nullcontext()
    )
    adjudication_lock = (
        FileLock(
            f"{adjudications_path}.lock",
            purpose="card adjudication apply",
            timeout=5,
        )
        if args.apply and ok and has_work
        else nullcontext()
    )
    with adjudication_lock, write_context:
        if args.apply and ok and has_work:
            # Refuse lost updates: every card touched by this batch must still be
            # byte-equivalent at the data-model level to the card used for validation.
            for uid in sorted(protected_uids):
                load_card_for_update(uid, cards[uid])
            with open(adjudications_path, "rb") as handle:
                if handle.read() != adjudications_source_bytes:
                    raise RuntimeError(
                        "Adjudications changed after planning; reload before applying."
                    )
            # Merge rather than replace blocklist entries committed by an earlier run.
            hosts_accum["hosts"] |= load_shared_hosts()
            hosts_before = len(hosts_accum["hosts"])
        if ok:
            for row in adjudications.to_dict("records"):
                uid, verdict = _clean(row.get("repo_uid")), _clean(row.get("verdict"))
                if verdict == "REJECT_CANDIDATE":
                    action_log.append({
                        "repo_uid": uid or "",
                        "verdict": verdict,
                        "detail": f"audit-only rejection: {_clean(row.get('note')) or _clean(row.get('target')) or 'candidate rejected'}",
                    })
                    applied += 1
                    per_verdict[verdict] = per_verdict.get(verdict, 0) + 1
                    continue
                if verdict == "MERGE_INTO":
                    result = apply_merge_into(row, dry_run=not args.apply)
                    action_log.append(result)
                    applied += 1
                    per_verdict[verdict] = per_verdict.get(verdict, 0) + 1
                    continue
                card = load_card(uid) if args.apply else cards[uid]
                if card is None:
                    raise FileNotFoundError(f"Card vanished after adjudication validation: {uid}")
                actions = apply_row(card, row, hosts_accum)
                action_log.append(
                    {"repo_uid": uid, "verdict": verdict, "detail": "; ".join(actions)}
                )
                applied += 1
                per_verdict[verdict] = per_verdict.get(verdict, 0) + 1
                if args.apply:
                    save_card(uid, card)

        if args.apply and ok and has_work:
            save_shared_hosts(hosts_accum["hosts"])
            applied_at = dt.datetime.now().isoformat(timespec="seconds")
            selected_indices = set(adjudications["_source_index"].astype(int).tolist())
            for source_index in selected_indices:
                all_adjudications.at[source_index, "applied_at"] = applied_at
                all_adjudications.at[source_index, "applied_run_id"] = current_run_id()
            output_frame = all_adjudications.drop(columns=["_source_index"], errors="ignore")
            buffer = io.StringIO(newline="")
            output_frame.to_csv(buffer, index=False)
            transaction = active_card_transaction(CARDS)
            if transaction is None:
                raise RuntimeError("Expected active card transaction while writing adjudication receipts.")
            transaction.write_text(
                adjudications_path,
                buffer.getvalue(),
                operation="mark_card_adjudications_applied",
            )

    run_dir = new_run_dir("promote_review")
    pd.DataFrame(action_log, columns=["repo_uid", "verdict", "detail"]).to_csv(os.path.join(run_dir, "applied_log.csv"), index=False)
    template_cards = all_cards() if args.apply and ok else cards
    template_count = emit_template(template_cards, os.path.join(run_dir, "review_template.csv"))

    print(f"\n decisions applied : {applied}")
    print(f" by verdict : {per_verdict}")
    print(f" shared_hosts : {hosts_before} → {len(hosts_accum['hosts'])} (+{len(hosts_accum['hosts']) - hosts_before})")
    print(f" next-round template: {template_count} open rows")
    if action_log:
        print("\n sample actions:")
        for entry in action_log[:12]:
            print(f" {entry['repo_uid']:<6} {entry['verdict']:<26} {entry['detail'][:80]}")
    if not has_work:
        print("\n No unapplied card/name adjudications were found; valid no-op.")
        if args.apply:
            print(" Apply requested, but no card, host blocklist, or adjudication receipt was changed.")
    elif not args.apply:
        print("\n DRY RUN — inspect applied_log.csv, then re-run with --apply")
    elif not ok:
        print("\n ⛔ gate failed — not applying. Resolve all reported validation errors first.")

    ledger_append(
        "promote_review",
        params={"adjudications": args.adjudications, "apply": args.apply, "include_applied": args.include_applied},
        inputs={"adjudications": adjudications_path},
        outputs={
            "applied": applied,
            "by_verdict": per_verdict,
            "shared_hosts_added": len(hosts_accum["hosts"]) - hosts_before,
            "template_rows": template_count,
            "merge_rows": len(merge_rows),
            "merge_validation_errors": len(merge_errors),
            "no_op": int(not has_work),
        },
        gate_passed=ok,
        run_dir=run_dir,
    )
    print(f"\n✓ {run_dir}")


if __name__ == "__main__":
    main()