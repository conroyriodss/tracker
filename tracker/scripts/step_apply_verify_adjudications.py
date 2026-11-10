"""Validate and apply structured identifier-candidate adjudications.

ACCEPT adds a missing identifier link or replaces the exact stale link recorded
when the candidate was reviewed. EDIT_ALIAS changes only the card's identity
aliases. REJECT is an auditable decision and is consumed by verify_external_ids
as a suppression rule; it never mutates a card.

Dry run is the default. In --apply mode every card and adjudication-receipt write
is one lock-protected, rollback-capable transaction. ``--bulk-safe`` partitions
recognized live-state drift into explicit superseded receipts without weakening
the no-implicit-overwrite rule.
"""
from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import io
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from common import (
    ENV,
    FileLock,
    INP,
    NOW,
    PROJECT,
    VERIFY_ADJUDICATIONS_CSV,
    all_cards,
    card_transaction,
    current_run_id,
    gate,
    ledger_append,
    load_card_for_update,
    new_run_dir,
    save_card,
    slug,
    strip_acronym,
    atomic_write_csv_rows,
    atomic_write_json,
)
from review_widgets import VERIFY_FIELDS

DECISIONS = {"ACCEPT", "REJECT", "EDIT_ALIAS"}
YM = NOW.strftime("%Y-%m")


def _clean(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    value = str(value).strip()
    return "" if value.lower() in {"nan", "none", "null", "undefined"} else value


def _resolve_input(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(INP) / path


def _normalized_id(id_type: str, value: str) -> str:
    value = _clean(value)
    if id_type == "rrid" and value:
        match = re.search(r"SCR_\d+", value, flags=re.I)
        if match:
            return f"RRID:{match.group(0).upper()}"
    return value


def _canonical_url(id_type: str, id_value: str, candidate_url: str) -> str:
    if _clean(candidate_url):
        return _clean(candidate_url)
    if not id_value:
        return ""
    if id_type == "rrid":
        return f"https://scicrunch.org/resolver/{id_value}"
    if id_type == "fairsharing_doi":
        return f"https://fairsharing.org/{id_value}"
    if id_type == "re3data":
        return f"https://www.re3data.org/repository/{id_value}"
    return ""


def _norm_url(value: Any) -> str:
    text = _clean(value).rstrip("/").lower()
    return text.removeprefix("http://").removeprefix("https://").removeprefix("www.")


def _same_candidate(link: Mapping[str, Any], id_type: str, id_value: str, url: str) -> bool:
    if _clean(link.get("id_type")).lower() != id_type:
        return False
    link_id = _normalized_id(id_type, _clean(link.get("id_value"))).lower()
    target_id = _normalized_id(id_type, id_value).lower()
    if target_id and link_id != target_id:
        return False
    if not target_id and link_id:
        return False
    target_url = _norm_url(url)
    link_url = _norm_url(link.get("url"))
    return not target_url or link_url == target_url


def _matches_reviewed_existing(
    link: Mapping[str, Any],
    id_type: str,
    existing_id: str,
    existing_url: str,
) -> bool:
    if _clean(link.get("id_type")).lower() != id_type:
        return False
    if existing_id and _normalized_id(id_type, _clean(link.get("id_value"))).lower() != _normalized_id(id_type, existing_id).lower():
        return False
    if existing_url and _norm_url(link.get("url")) != _norm_url(existing_url):
        return False
    return bool(existing_id or existing_url)


def _add_alias(card: dict[str, Any], alias: str, row: Mapping[str, str]) -> bool:
    alias = _clean(alias)
    if not alias:
        return False
    identity = card.setdefault("identity", {})
    current_names = [
        identity.get("name"),
        identity.get("full_name"),
        identity.get("abbreviation"),
        *(identity.get("aliases") or []),
    ]
    candidate_slug = slug(strip_acronym(alias)[0])
    if any(slug(strip_acronym(value)[0]) == candidate_slug for value in current_names if value):
        return False
    aliases = list(identity.get("aliases") or [])
    aliases.append(alias)
    identity["aliases"] = sorted(set(aliases), key=lambda value: value.lower())
    provenance = identity.setdefault("name_provenance", {})
    provenance[alias] = {
        "source": "verify_adjudication",
        "review_key": _clean(row.get("review_key")),
        "reviewed_by": _clean(row.get("reviewer")),
        "as_of": YM,
    }
    return True


def _record_card_event(card: dict[str, Any], row: Mapping[str, str], status: str) -> None:
    curation = card.setdefault("curation", {})
    history = curation.setdefault("verification_adjudications", [])
    if not isinstance(history, list):
        history = []
        curation["verification_adjudications"] = history
    key = _clean(row.get("review_key"))
    event = {
        "review_key": key,
        "decision": _clean(row.get("decision")).upper(),
        "id_type": _clean(row.get("id_type")).lower(),
        "candidate_id": _clean(row.get("candidate_id")),
        "reviewer": _clean(row.get("reviewer")),
        "reviewed_at": _clean(row.get("reviewed_at")),
        "applied_at": dt.datetime.now().isoformat(timespec="seconds"),
        "status": status,
    }
    for index, prior in enumerate(history):
        if isinstance(prior, dict) and _clean(prior.get("review_key")) == key:
            history[index] = event
            break
    else:
        history.append(event)


def _proposed_link(row: Mapping[str, str]) -> dict[str, Any]:
    id_type = _clean(row.get("id_type")).lower()
    id_value = _normalized_id(id_type, _clean(row.get("candidate_id")))
    url = _canonical_url(id_type, id_value, _clean(row.get("candidate_url")))
    return {
        "id_type": id_type,
        "id_value": id_value or None,
        "url": url or None,
        "tier": "attestation",
        "as_of": YM,
        "source": f"verify_adjudication:{_clean(row.get('candidate_source')) or 'review'}",
        "review_key": _clean(row.get("review_key")),
    }


def apply_one(card: dict[str, Any], row: Mapping[str, str]) -> tuple[bool, str]:
    """Apply one validated ACCEPT or EDIT_ALIAS decision to an in-memory card."""
    decision = _clean(row.get("decision")).upper()
    changed = False
    if decision == "EDIT_ALIAS":
        changed = _add_alias(card, _clean(row.get("alias")), row)
        status = "alias_added" if changed else "alias_already_present"
        _record_card_event(card, row, status)
        return True, status  # event itself is an auditable card change

    if decision != "ACCEPT":
        raise ValueError(f"apply_one only accepts card-mutating decisions, got {decision!r}")

    id_type = _clean(row.get("id_type")).lower()
    replacement = _proposed_link(row)
    links = card.setdefault("links", [])
    if not isinstance(links, list):
        raise ValueError("card links field is not a list")

    for link in links:
        if isinstance(link, dict) and _same_candidate(
            link, id_type, _clean(replacement.get("id_value")), _clean(replacement.get("url"))
        ):
            if _clean(row.get("alias")):
                _add_alias(card, _clean(row.get("alias")), row)
            _record_card_event(card, row, "candidate_already_present")
            return True, "candidate_already_present"

    existing_id = _normalized_id(id_type, _clean(row.get("existing_id")))
    existing_url = _clean(row.get("existing_url"))
    if existing_id or existing_url:
        matches = [
            index
            for index, link in enumerate(links)
            if isinstance(link, dict)
            and _matches_reviewed_existing(link, id_type, existing_id, existing_url)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"reviewed existing {id_type} link no longer matches exactly "
                f"(expected one, found {len(matches)})"
            )
        links[matches[0]] = replacement
        status = "exact_link_replaced"
    else:
        current_type = [
            link for link in links
            if isinstance(link, dict) and _clean(link.get("id_type")).lower() == id_type
        ]
        if current_type:
            raise ValueError(
                f"card acquired a different {id_type} link after review; refusing implicit overwrite"
            )
        links.append(replacement)
        status = "missing_link_added"

    if _clean(row.get("alias")):
        _add_alias(card, _clean(row.get("alias")), row)
    _record_card_event(card, row, status)
    return True, status


def _read_adjudications(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        frame = pd.read_csv(path, dtype=str).fillna("")
    except pd.errors.EmptyDataError as exc:
        raise ValueError(f"Adjudication file is empty: {path}") from exc
    frame.columns = [str(column).strip() for column in frame.columns]
    for field in VERIFY_FIELDS:
        if field not in frame.columns:
            frame[field] = ""
    return frame[VERIFY_FIELDS]


def _validate(
    frame: pd.DataFrame,
    cards: Mapping[str, Any],
    include_applied: bool,
    bulk_safe: bool = False,
) -> tuple[list[str], pd.DataFrame]:
    work = frame.copy()
    work["decision"] = work["decision"].map(lambda value: _clean(value).upper())
    work = work.loc[work["decision"].isin(DECISIONS)].copy()
    if not include_applied:
        work = work.loc[~work["applied_at"].astype(str).str.strip().astype(bool)].copy()

    errors: list[str] = []
    duplicate_keys = work.loc[work["review_key"].duplicated(keep=False), "review_key"].tolist()
    if duplicate_keys:
        errors.append(f"duplicate review_key values: {sorted(set(duplicate_keys))[:5]}")

    mutation_rows = work.loc[work["decision"].isin({"ACCEPT", "EDIT_ALIAS"})]
    absent = sorted(set(mutation_rows["repo_uid"]) - set(cards))
    if absent and not bulk_safe:
        errors.append(f"card-mutating decisions reference absent repo_uids: {absent[:8]}")

    for row in work.to_dict("records"):
        key = _clean(row.get("review_key"))
        decision = _clean(row.get("decision")).upper()
        if not key:
            errors.append("one or more decision rows lack review_key")
            continue
        if decision in {"ACCEPT", "REJECT"} and not (
            _clean(row.get("candidate_id")) or _clean(row.get("candidate_url"))
        ):
            errors.append(f"{key}: {decision} lacks candidate_id/candidate_url")
        if decision == "EDIT_ALIAS" and not _clean(row.get("alias")):
            errors.append(f"{key}: EDIT_ALIAS lacks alias")
        if decision in {"ACCEPT", "EDIT_ALIAS"} and not _clean(row.get("repo_uid")):
            errors.append(f"{key}: {decision} lacks repo_uid")
        if decision == "ACCEPT" and not _clean(row.get("id_type")):
            errors.append(f"{key}: ACCEPT lacks id_type")

    accepts = work.loc[work["decision"] == "ACCEPT"].copy()
    if not accepts.empty:
        accepts["candidate_signature"] = accepts.apply(
            lambda row: (
                _normalized_id(_clean(row["id_type"]).lower(), _clean(row["candidate_id"])).lower(),
                _norm_url(row["candidate_url"]),
            ),
            axis=1,
        )
        for (repo_uid, id_type), group in accepts.groupby(["repo_uid", "id_type"]):
            if len(set(group["candidate_signature"])) > 1:
                errors.append(
                    f"multiple different ACCEPT candidates for {repo_uid}/{id_type} in one batch"
                )
    return errors, work


def _csv_text(frame: pd.DataFrame) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=VERIFY_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(frame.fillna("").to_dict("records"))
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--adjudications",
        default=VERIFY_ADJUDICATIONS_CSV,
        help="Absolute path or filename under inputs/ (default: verify_adjudications.csv).",
    )
    parser.add_argument("--apply", action="store_true", help="Commit the validated batch.")
    parser.add_argument(
        "--include-applied",
        action="store_true",
        help="Re-evaluate rows carrying an applied_at receipt (normally skipped).",
    )
    parser.add_argument(
        "--bulk-safe",
        action="store_true",
        help=(
            "Apply every structurally valid row while receipting isolated live-state "
            "conflicts as superseded. Never overwrites a different current identifier."
        ),
    )
    args = parser.parse_args()

    path = _resolve_input(args.adjudications)
    adjudications_source_bytes = path.read_bytes()
    frame = _read_adjudications(path)
    cards = all_cards()
    errors, work = _validate(frame, cards, args.include_applied, args.bulk_safe)

    plan_rows: list[dict[str, str]] = []
    staged_cards: dict[str, dict[str, Any]] = {}
    superseded_count = 0
    for row in work.to_dict("records"):
        key = _clean(row.get("review_key"))
        decision = _clean(row.get("decision")).upper()
        uid = _clean(row.get("repo_uid"))
        if decision == "REJECT":
            plan_rows.append(
                {
                    "review_key": key,
                    "repo_uid": uid,
                    "decision": decision,
                    "status": "record_rejection",
                    "detail": "candidate will be suppressed in future verifier scans",
                }
            )
            continue
        if uid not in cards:
            detail = f"card no longer exists: {uid}"
            if args.bulk_safe:
                status = "superseded:card_missing"
                superseded_count += 1
            else:
                status = "conflict"
                errors.append(f"{key} ({uid}): {detail}")
            plan_rows.append(
                {"review_key": key, "repo_uid": uid, "decision": decision, "status": status, "detail": detail}
            )
            continue
        newly_staged = uid not in staged_cards
        card = staged_cards.setdefault(uid, copy.deepcopy(cards[uid]))
        try:
            _, status = apply_one(card, row)
            plan_rows.append(
                {"review_key": key, "repo_uid": uid, "decision": decision, "status": status, "detail": "validated"}
            )
        except Exception as exc:
            detail = str(exc)
            recognized_drift = (
                "card acquired a different" in detail
                or "reviewed existing" in detail and "no longer matches exactly" in detail
            )
            if args.bulk_safe and recognized_drift:
                status = (
                    "superseded:different_identifier_present"
                    if "card acquired a different" in detail
                    else "superseded:reviewed_existing_changed"
                )
                superseded_count += 1
                # apply_one raises before mutating for both recognized drift cases.
                # Do not rewrite a card that was staged only for this superseded row.
                if newly_staged:
                    staged_cards.pop(uid, None)
                plan_rows.append(
                    {"review_key": key, "repo_uid": uid, "decision": decision, "status": status, "detail": detail}
                )
            else:
                if newly_staged:
                    staged_cards.pop(uid, None)
                errors.append(f"{key} ({uid}): {detail}")
                plan_rows.append(
                    {"review_key": key, "repo_uid": uid, "decision": decision, "status": "conflict", "detail": detail}
                )

    selected_ok = len(work) > 0 or args.bulk_safe
    checks = {
        "cards available": (bool(cards), f"{len(cards)} top-level cards"),
        "explicit decisions selected": (
            selected_ok,
            f"{len(work)} unapplied decision row(s)" if len(work) else "valid no-op",
        ),
        "decision batch structurally valid": (not errors, "; ".join(errors[:5]) if errors else "ok"),
        "isolated live-state conflicts": (
            True,
            f"{superseded_count} superseded row(s) receipted without overwrite"
            if args.bulk_safe
            else "strict mode",
        ),
    }
    ok = gate(checks)

    run_dir = Path(new_run_dir("apply_verify_adjudications"))
    atomic_write_csv_rows(
        run_dir / "decision_plan.csv",
        plan_rows,
        ["review_key", "repo_uid", "decision", "status", "detail"],
    )
    atomic_write_json(
        run_dir / "run_report.json",
        {
            "env": ENV,
            "adjudications": str(path),
            "selected_rows": len(work),
            "cards_staged": len(staged_cards),
            "errors": errors,
            "superseded_rows": superseded_count,
            "bulk_safe": bool(args.bulk_safe),
            "apply_requested": bool(args.apply),
            "gate_passed": ok,
        },
    )

    applied_cards = 0
    receipt_count = 0
    if args.apply and ok and work.empty:
        print("\nValid no-op: no unapplied identifier decisions were found.")
    elif args.apply and ok:
        applied_at = dt.datetime.now().isoformat(timespec="seconds")
        work_keys = set(work["review_key"].astype(str))
        status_by_key = {row["review_key"]: row["status"] for row in plan_rows}
        updated_frame = frame.copy()
        for index, row in updated_frame.iterrows():
            key = _clean(row.get("review_key"))
            if key in work_keys:
                updated_frame.at[index, "applied_at"] = applied_at
                updated_frame.at[index, "applied_run_id"] = current_run_id()
                updated_frame.at[index, "apply_status"] = status_by_key.get(key, "recorded")
                receipt_count += 1

        with FileLock(
            f"{path}.lock",
            purpose="verify adjudication apply",
            timeout=5,
        ):
            if path.read_bytes() != adjudications_source_bytes:
                raise RuntimeError(
                    "Verify adjudications changed after planning; reload before applying."
                )
            with card_transaction("apply_verify_adjudications") as transaction:
                for uid in sorted(staged_cards):
                    load_card_for_update(uid, cards[uid])
                for uid in sorted(staged_cards):
                    save_card(uid, staged_cards[uid])
                    applied_cards += 1
                transaction.write_text(
                    path,
                    _csv_text(updated_frame),
                    operation="mark_verify_adjudications_applied",
                )
        print(
            f"\n✓ transaction committed: {applied_cards} card(s), "
            f"{receipt_count} adjudication receipt(s), "
            f"{superseded_count} superseded row(s) closed without overwrite"
        )
    elif args.apply:
        print("\n⛔ gate failed — no cards or adjudication receipts were written.")
    else:
        print("\nDRY RUN — inspect decision_plan.csv, then rerun with --apply.")

    ledger_append(
        "apply_verify_adjudications",
        params={
            "adjudications": str(path),
            "apply": bool(args.apply),
            "include_applied": bool(args.include_applied),
            "bulk_safe": bool(args.bulk_safe),
        },
        inputs={"adjudications": str(path)},
        outputs={
            "selected_rows": len(work),
            "cards_staged": len(staged_cards),
            "cards_written": applied_cards,
            "receipts_written": receipt_count,
            "validation_errors": len(errors),
            "superseded_rows": superseded_count,
        },
        gate_passed=ok,
        run_dir=str(run_dir),
    )
    print(f"✓ run artifacts → {run_dir}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
