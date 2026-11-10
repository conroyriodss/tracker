"""Bulk-promote human-approved model recommendations into adjudication files.

The command never mutates cards, crosswalk.csv, or id_remap.csv. It translates
explicit human approvals into the existing identifier and crosswalk
adjudication schemas. In ``--bulk`` mode every pending approval receives an
explicit disposition: promote, already satisfied, deferred, superseded, or
quarantined. Unrelated queue drift and isolated stale rows no longer discard a
valid majority. Existing dry-run/apply commands remain the only path to card or
crosswalk/remap mutation.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from automated_review import (
    AI_APPROVALS_CSV,
    AI_RECOMMENDATIONS_CSV,
    APPROVAL_FIELDS,
    RECOMMENDATION_FIELDS,
    _current_item_state,
    _find_batch,
    _load_batch_items,
    _resolve_project_path,
    _upsert_frame,
    AI_APPROVAL_LOCK,
    AI_RECOMMENDATION_LOCK,
    load_approvals,
    load_recommendations,
)
from common import (
    atomic_write_json,
    all_cards,
    current_run_id,
    ledger_append,
    new_run_dir,
    sha256_file,
)
from review_widgets import normalize_review_file, prepare_decisions, upsert_adjudications, load_adjudications

CONFIRM_TOKEN = "PROMOTE_AI_RECOMMENDATIONS"


def _clean(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null", "undefined"} else text


def _truthy(value: Any) -> bool:
    return _clean(value).lower() in {"1", "true", "yes", "y", "on"}


def _normalized_id(id_type: str, value: Any) -> str:
    text = _clean(value)
    if id_type == "rrid" and text:
        match = re.search(r"SCR_\d+", text, flags=re.I)
        if match:
            return f"RRID:{match.group(0).upper()}"
    return text


def _norm_url(value: Any) -> str:
    text = _clean(value).rstrip("/").lower()
    return text.removeprefix("http://").removeprefix("https://").removeprefix("www.")


def _candidate_url(id_type: str, candidate_id: str, candidate_url: str) -> str:
    if candidate_url:
        return candidate_url
    if id_type == "rrid" and candidate_id:
        return f"https://scicrunch.org/resolver/{candidate_id}"
    if id_type == "fairsharing_doi" and candidate_id:
        return f"https://fairsharing.org/{candidate_id}"
    if id_type == "re3data" and candidate_id:
        return f"https://www.re3data.org/repository/{candidate_id}"
    return ""


def _same_candidate(link: Mapping[str, Any], *, id_type: str, candidate_id: str, candidate_url: str) -> bool:
    if _clean(link.get("id_type")).lower() != id_type:
        return False
    left = _normalized_id(id_type, link.get("id_value")).lower()
    right = _normalized_id(id_type, candidate_id).lower()
    if right:
        return left == right
    if left:
        return False
    return bool(candidate_url) and _norm_url(link.get("url")) == _norm_url(candidate_url)


def _same_existing(link: Mapping[str, Any], *, id_type: str, existing_id: str, existing_url: str) -> bool:
    if _clean(link.get("id_type")).lower() != id_type:
        return False
    if existing_id and _normalized_id(id_type, link.get("id_value")).lower() != _normalized_id(id_type, existing_id).lower():
        return False
    if existing_url and _norm_url(link.get("url")) != _norm_url(existing_url):
        return False
    return bool(existing_id or existing_url)


def _source_note(
    recommendation: Mapping[str, Any],
    approval: Mapping[str, Any],
    *,
    promotion_note: str = "",
) -> str:
    urls = []
    try:
        for source in json.loads(recommendation.get("external_sources_json") or "[]"):
            if isinstance(source, Mapping) and _clean(source.get("url")):
                urls.append(_clean(source.get("url")))
    except Exception:
        pass
    note = (
        "Human-approved automated-review recommendation. "
        f"provider={_clean(recommendation.get('provider'))}; "
        f"model={_clean(recommendation.get('model'))}; "
        f"confidence={_clean(recommendation.get('confidence'))}; "
        f"evidence_grade={_clean(recommendation.get('evidence_grade'))}; "
        f"bundle={_clean(recommendation.get('bundle_id'))}; "
        f"item_sha256={_clean(recommendation.get('item_sha256'))}. "
        f"Rationale: {_clean(recommendation.get('rationale'))}"
    )
    human_note = _clean(approval.get("human_note"))
    if human_note:
        note += f" Human note: {human_note}"
    if promotion_note:
        note += f" Promotion state: {promotion_note}."
    if urls:
        note += " External sources: " + "; ".join(urls[:10])
    return note[:6000]


def _build_decision(
    recommendation: Mapping[str, Any],
    approval: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    promotion_note: str = "",
) -> tuple[str, dict[str, str] | None, str]:
    kind = _clean(recommendation.get("queue_kind"))
    decision = _clean(approval.get("approved_decision")).upper()
    target = _clean(approval.get("approved_target"))
    alias = _clean(approval.get("approved_alias"))
    reviewer = _clean(approval.get("reviewer"))
    context = item.get("queue_context") if isinstance(item.get("queue_context"), Mapping) else {}
    note = _source_note(recommendation, approval, promotion_note=promotion_note)
    reviewed_at = _clean(approval.get("approved_at")) or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    if kind == "card":
        if decision == "DEFER":
            return kind, None, "deferred:no_operational_adjudication"
        if decision not in {"ACCEPT", "REJECT"}:
            raise ValueError(f"Card/name recommendation cannot promote decision {decision!r}.")
        uid = _clean(context.get("repo_uid"))
        allowed_targets = {_clean(value) for value in item.get("allowed_targets", [])}
        if decision == "ACCEPT" and (not uid or target != uid or target not in allowed_targets):
            raise ValueError("Card/name ACCEPT must select the sealed existing card UID.")
        label = _clean(context.get('target'))
        if decision == 'ACCEPT' and (not label or ';' in label or len(label) > 500):
            raise ValueError('This source label requires manual alias handling; no automatic alias translation is allowed.')
        if alias:
            raise ValueError("Card/name approval cannot invent an alias; use the manual workbench to edit aliases.")
        saved = load_adjudications("card")
        for prior in saved.to_dict("records"):
            if _clean(prior.get("review_key")) == _clean(item.get("review_key")) and (
                _clean(prior.get("review_decision")).upper() not in {"", "PENDING"} or _clean(prior.get("verdict"))
            ):
                raise ValueError("A current human card/name adjudication already exists; it will not be overwritten.")
        row = {str(key): _clean(value) for key, value in context.items()}
        row.update(review_decision=decision, note=note, reviewer=reviewer, reviewed_at=reviewed_at,
                   applied_at="", applied_run_id="", add_aliases="", verdict="")
        return kind, row, "promoted:card_adjudication"

    if kind == "verify":
        if decision == "DEFER":
            return kind, None, "deferred:no_operational_adjudication"
        if decision not in {"ACCEPT", "REJECT"}:
            raise ValueError(f"Identifier recommendation cannot promote decision {decision!r}.")
        candidate = _clean(context.get("candidate_id")) or _clean(context.get("candidate_url"))
        if decision == "ACCEPT" and target != candidate:
            raise ValueError("Identifier ACCEPT target no longer matches the current candidate.")
        row = {str(key): _clean(value) for key, value in context.items()}
        row.update(
            {
                "decision": decision,
                "alias": alias,
                "note": note,
                "reviewer": reviewer,
                "reviewed_at": reviewed_at,
                "applied_at": "",
                "applied_run_id": "",
                "apply_status": "",
            }
        )
        return kind, row, "promoted:verify_adjudication"

    if kind == "crosswalk":
        if decision not in {"REPLACE_CROSSWALK_UID", "RECORD_ID_REMAP", "DEFER"}:
            raise ValueError(f"Crosswalk recommendation cannot promote decision {decision!r}.")
        if decision == "RECORD_ID_REMAP" and not _clean(approval.get("human_note")):
            raise ValueError(
                "RECORD_ID_REMAP requires a human-authored lineage note; use the manual "
                "workbench when historical evidence is available."
            )
        allowed_targets = {_clean(value) for value in item.get("allowed_targets", []) if _clean(value)}
        if decision in {"REPLACE_CROSSWALK_UID", "RECORD_ID_REMAP"} and target not in allowed_targets:
            raise ValueError("Crosswalk target is not in the current active candidate set.")
        row = {str(key): _clean(value) for key, value in context.items()}
        row.update(
            {
                "decision": decision,
                "selected_repo_uid": target,
                "note": note,
                "reviewer": reviewer,
                "reviewed_at": reviewed_at,
                "applied_at": "",
                "applied_run_id": "",
                "apply_status": "",
            }
        )
        return kind, row, "promoted:crosswalk_adjudication"
    raise ValueError(f"Unsupported queue kind: {kind!r}")


def _rebase_from_current_queue(
    item: Mapping[str, Any],
    cards: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """Rebind an immutable item to the exact same stable review key in the current queue.

    The stable review key already includes the operational identifier candidate for
    identifier queues and the source-row fingerprint for crosswalk queues. A
    whole-file hash change caused by unrelated rows therefore does not invalidate
    an unchanged item. Zero or multiple current matches remain blocking.
    """
    if item.get("queue_kind") == "card":
        return None, "card/name evidence changed; create a fresh recommendation and approval"
    binding = item.get("source_binding") if isinstance(item.get("source_binding"), Mapping) else {}
    source = _resolve_project_path(_clean(binding.get("source_queue")))
    if not source.is_file():
        return None, f"source queue is missing: {source}"
    try:
        kind, frame = normalize_review_file(source)
    except Exception as exc:
        return None, f"current queue cannot be normalized: {exc}"
    review_key = _clean(item.get("review_key"))
    matches = frame.loc[frame["review_key"].astype(str).eq(review_key)].copy()
    if len(matches) != 1:
        return None, f"current queue contains {len(matches)} row(s) for review_key={review_key}"
    row = {str(key): _clean(value) for key, value in matches.iloc[0].to_dict().items()}
    if _clean(item.get("queue_kind")) != kind:
        return None, f"current queue kind changed from {_clean(item.get('queue_kind'))!r} to {kind!r}"

    if kind == "verify":
        uid = _clean(row.get("repo_uid"))
        current_card = cards.get(uid)
        if not isinstance(current_card, Mapping):
            return None, f"current card is missing: {uid}"
        exported_context = next(
            (
                context
                for context in item.get("card_contexts", [])
                if isinstance(context, Mapping) and _clean(context.get("repo_uid")) == uid
            ),
            None,
        )
        if not isinstance(exported_context, Mapping):
            return None, f"exported card context is missing for {uid}"
        exported_identity = (
            exported_context.get("identity")
            if isinstance(exported_context.get("identity"), Mapping)
            else {}
        )
        current_identity = (
            current_card.get("identity")
            if isinstance(current_card.get("identity"), Mapping)
            else {}
        )
        exported_name = _clean(exported_identity.get("name")).casefold()
        current_name = _clean(current_identity.get("name")).casefold()
        exported_homepage = _norm_url(exported_identity.get("homepage"))
        current_homepage = _norm_url(current_identity.get("homepage"))
        if exported_name and current_name != exported_name:
            return None, f"card canonical name changed after export: {uid}"
        if exported_homepage and current_homepage != exported_homepage:
            return None, f"card canonical homepage changed after export: {uid}"
        queue_name = _clean(row.get("card_name")).casefold()
        if exported_name and queue_name and queue_name != exported_name:
            return None, f"current queue card name no longer matches exported identity: {uid}"

    effective = copy.deepcopy(dict(item))
    effective["queue_context"] = row
    effective_binding = dict(binding)
    effective_binding.update(
        {
            "source_queue_sha256": sha256_file(source) or "",
            "source_row": row.get("source_row", ""),
            "source_run": row.get("source_run", ""),
            "crosswalk_sha256": row.get("source_crosswalk_sha256", ""),
            "registry_file": row.get("source_registry_file", ""),
            "registry_sha256": row.get("source_registry_sha256", ""),
            "id_remap_sha256": row.get("source_id_remap_sha256", ""),
            "source_row_fingerprint": row.get("source_row_fingerprint", ""),
        }
    )
    effective["source_binding"] = effective_binding
    if kind == "verify":
        candidate = row.get("candidate_id") or row.get("candidate_url")
        effective["allowed_targets"] = [candidate] if candidate else []
    else:
        effective["allowed_targets"] = [
            value.strip() for value in row.get("candidate_uids", "").split(";") if value.strip()
        ]
    return effective, "rebased to the unique current row with the same stable review key"


def _verify_live_disposition(
    item: Mapping[str, Any],
    approval: Mapping[str, Any],
    cards: Mapping[str, Any],
) -> tuple[str, str]:
    """Return (disposition, reason) for a verify recommendation against live card state."""
    decision = _clean(approval.get("approved_decision")).upper()
    if decision == "DEFER":
        return "DEFERRED", "verify DEFER has no operational mutation"
    context = item.get("queue_context") if isinstance(item.get("queue_context"), Mapping) else {}
    if decision == "REJECT":
        return "PROMOTE", "REJECT is a non-mutating suppression decision"
    if decision != "ACCEPT":
        return "BLOCKED", f"unsupported verify decision {decision!r}"

    uid = _clean(context.get("repo_uid"))
    card = cards.get(uid)
    if not isinstance(card, Mapping):
        return "BLOCKED", f"card no longer exists: {uid}"
    id_type = _clean(context.get("id_type")).lower()
    candidate_id = _normalized_id(id_type, context.get("candidate_id"))
    candidate_url = _candidate_url(id_type, candidate_id, _clean(context.get("candidate_url")))
    links = card.get("links") if isinstance(card.get("links"), list) else []
    same_type = [
        link for link in links
        if isinstance(link, Mapping) and _clean(link.get("id_type")).lower() == id_type
    ]
    if any(
        _same_candidate(
            link,
            id_type=id_type,
            candidate_id=candidate_id,
            candidate_url=candidate_url,
        )
        for link in same_type
    ):
        return "SATISFIED", f"candidate is already present on card {uid}"

    existing_id = _normalized_id(id_type, context.get("existing_id"))
    existing_url = _clean(context.get("existing_url"))
    if existing_id or existing_url:
        matches = [
            link for link in same_type
            if _same_existing(
                link,
                id_type=id_type,
                existing_id=existing_id,
                existing_url=existing_url,
            )
        ]
        if len(matches) == 1:
            return "PROMOTE", "reviewed existing link still matches exactly"
        return "SUPERSEDED", (
            f"reviewed existing {id_type} link no longer matches exactly "
            f"(expected one, found {len(matches)})"
        )
    if same_type:
        values = sorted({_clean(link.get("id_value")) or _clean(link.get("url")) for link in same_type})
        return "SUPERSEDED", (
            f"card {uid} now has a different {id_type} link; no implicit overwrite "
            f"({'; '.join(value for value in values if value)})"
        )
    return "PROMOTE", "candidate remains a missing identifier on the current card"


def _decision_signature(kind: str, row: Mapping[str, Any]) -> tuple[str, ...]:
    if kind == "card":
        return tuple(_clean(row.get(key)) for key in ("review_key", "review_decision", "repo_uid", "target"))
    if kind == "verify":
        return (
            _clean(row.get("review_key")),
            _clean(row.get("decision")).upper(),
            _clean(row.get("repo_uid")),
            _clean(row.get("id_type")).lower(),
            _clean(row.get("candidate_id")),
            _norm_url(row.get("candidate_url")),
            _clean(row.get("alias")),
        )
    return (
        _clean(row.get("review_key")),
        _clean(row.get("decision")).upper(),
        _clean(row.get("original_repo_uid")),
        _clean(row.get("selected_repo_uid")),
        _clean(row.get("source_row_fingerprint")),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approvals", default=str(AI_APPROVALS_CSV))
    parser.add_argument("--recommendations", default=str(AI_RECOMMENDATIONS_CSV))
    parser.add_argument(
        "--bulk",
        action="store_true",
        help=(
            "Process every pending human approval independently. Rebase exact stable items "
            "across unrelated queue drift and quarantine only irreconcilable rows."
        ),
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    approvals = load_approvals(args.approvals)
    recommendations = load_recommendations(args.recommendations)
    eligible = approvals.loc[
        approvals["approved"].map(_truthy)
        & approvals["promotion_status"].astype(str).str.strip().eq("")
    ].copy()
    cards = all_cards()
    run_dir = Path(new_run_dir("promote_ai_review"))
    plan_rows: list[dict[str, str]] = []
    receipt_by_approval: dict[str, str] = {}
    recommendation_receipt_by_key: dict[str, str] = {}
    hard_errors: list[str] = []
    operational: dict[str, dict[str, dict[str, Any]]] = {"verify": {}, "crosswalk": {}, "card": {}}
    conflicts: dict[tuple[str, str], set[str]] = {}

    by_key = {
        _clean(row.get("recommendation_key")): row
        for row in recommendations.to_dict("records")
        if _clean(row.get("recommendation_key"))
    }

    for approval in eligible.to_dict("records"):
        approval_key = _clean(approval.get("approval_key"))
        rec_key = _clean(approval.get("recommendation_key"))
        recommendation = by_key.get(rec_key)
        row_errors: list[str] = []
        item: Mapping[str, Any] | None = None
        effective_item: Mapping[str, Any] | None = None
        source_state = ""
        disposition = ""
        planned_status = ""
        decision_row: dict[str, str] | None = None
        kind = _clean(approval.get("queue_kind"))

        if recommendation is None:
            row_errors.append("recommendation_key is absent from current recommendation inventory")
        else:
            try:
                batch_dir = _find_batch(
                    _clean(recommendation.get("bundle_id")),
                    _clean(recommendation.get("batch_id")),
                )
                item = _load_batch_items(batch_dir).get(_clean(recommendation.get("review_key")))
                if item is None:
                    row_errors.append("exported item is missing")
                elif _clean(item.get("item_sha256")) != _clean(approval.get("item_sha256")):
                    row_errors.append("approval item_sha256 differs from the exported item")
            except Exception as exc:
                row_errors.append(str(exc))

        if not row_errors and item is not None and recommendation is not None:
            if not args.bulk:
                current, reason = _current_item_state(item)
                if not current:
                    row_errors.append(f"source is stale: {reason}")
                else:
                    effective_item = item
                    source_state = "CURRENT"
            else:
                current, reason = _current_item_state(item)
                if current:
                    effective_item = item
                    source_state = "CURRENT"
                else:
                    rebased, rebase_reason = _rebase_from_current_queue(item, cards)
                    if rebased is not None:
                        effective_item = rebased
                        source_state = "REBASED"
                        reason = rebase_reason
                    else:
                        source_state = "STALE"
                        reason = f"{reason}; {rebase_reason}" if reason else rebase_reason
                        # Non-mutating verify dispositions remain safe when bound to
                        # the sealed item even if the candidate has left the latest queue.
                        decision = _clean(approval.get("approved_decision")).upper()
                        if kind == "verify" and decision in {"REJECT", "DEFER"}:
                            effective_item = item
                        elif kind == "verify" and decision == "ACCEPT":
                            live_disposition, live_reason = _verify_live_disposition(item, approval, cards)
                            if live_disposition == "SATISFIED":
                                disposition = live_disposition
                                planned_status = "satisfied:identifier_already_present"
                                receipt_by_approval[approval_key] = planned_status
                                recommendation_receipt_by_key[rec_key] = planned_status
                                reason = f"{reason}; {live_reason}"
                            elif live_disposition == "SUPERSEDED":
                                disposition = live_disposition
                                planned_status = "superseded:current_card_state"
                                receipt_by_approval[approval_key] = planned_status
                                recommendation_receipt_by_key[rec_key] = planned_status
                                reason = f"{reason}; {live_reason}"
                            else:
                                row_errors.append(f"source is stale and cannot be safely rebound: {reason}")
                        elif kind == "crosswalk" and decision == "DEFER":
                            disposition = "SUPERSEDED"
                            planned_status = "superseded:crosswalk_row_no_longer_current"
                            receipt_by_approval[approval_key] = planned_status
                            recommendation_receipt_by_key[rec_key] = planned_status
                        else:
                            row_errors.append(f"source is stale and cannot be safely rebound: {reason}")

        if not row_errors and effective_item is not None and recommendation is not None and not disposition:
            try:
                if kind == "verify" and args.bulk:
                    disposition, live_reason = _verify_live_disposition(effective_item, approval, cards)
                    source_note = source_state.lower()
                    if source_state == "REBASED":
                        source_note += ": exact stable review_key found in current queue"
                    if disposition == "SATISFIED":
                        planned_status = "satisfied:identifier_already_present"
                        receipt_by_approval[approval_key] = planned_status
                        recommendation_receipt_by_key[rec_key] = planned_status
                    elif disposition == "SUPERSEDED":
                        planned_status = "superseded:current_card_state"
                        receipt_by_approval[approval_key] = planned_status
                        recommendation_receipt_by_key[rec_key] = planned_status
                    elif disposition == "DEFERRED":
                        planned_status = "deferred:no_operational_adjudication"
                        receipt_by_approval[approval_key] = planned_status
                        recommendation_receipt_by_key[rec_key] = planned_status
                    elif disposition == "PROMOTE":
                        kind, decision_row, base_status = _build_decision(
                            recommendation,
                            approval,
                            effective_item,
                            promotion_note=f"{source_note}; {live_reason}",
                        )
                        planned_status = base_status + (":rebased" if source_state == "REBASED" else "")
                    else:
                        row_errors.append(live_reason)
                else:
                    kind, decision_row, base_status = _build_decision(
                        recommendation,
                        approval,
                        effective_item,
                        promotion_note=source_state.lower(),
                    )
                    disposition = "DEFERRED" if decision_row is None else "PROMOTE"
                    planned_status = base_status + (":rebased" if source_state == "REBASED" else "")
                    if decision_row is None:
                        receipt_by_approval[approval_key] = planned_status
                        recommendation_receipt_by_key[rec_key] = planned_status
            except Exception as exc:
                row_errors.append(str(exc))

        if decision_row is not None and not row_errors:
            review_key = _clean(decision_row.get("review_key"))
            signature = _decision_signature(kind, decision_row)
            group_key = (kind, review_key)
            prior = operational[kind].get(review_key)
            if prior is None and group_key not in conflicts:
                operational[kind][review_key] = {
                    "row": decision_row,
                    "signature": signature,
                    "approval_keys": [approval_key],
                    "recommendation_keys": [rec_key],
                    "statuses": {approval_key: planned_status},
                    "recommendation_statuses": {rec_key: planned_status},
                }
            elif prior is not None and prior["signature"] == signature:
                prior["approval_keys"].append(approval_key)
                prior["recommendation_keys"].append(rec_key)
                prior["statuses"][approval_key] = planned_status
                prior["recommendation_statuses"][rec_key] = planned_status
            else:
                approval_keys = set(prior.get("approval_keys", []) if prior else []) | {approval_key}
                conflicts[group_key] = approval_keys
                operational[kind].pop(review_key, None)
                row_errors.append(
                    "multiple human-approved recommendations for the same review_key disagree on the operational decision"
                )

        validation = "PASS"
        if row_errors:
            validation = "QUARANTINED" if args.bulk else "FAIL"
            disposition = "QUARANTINED"
            planned_status = "quarantined:" + (
                "conflicting_approvals" if "disagree" in "; ".join(row_errors) else "stale_or_invalid_binding"
            )
            if args.bulk:
                receipt_by_approval[approval_key] = planned_status
                recommendation_receipt_by_key[rec_key] = planned_status
            else:
                hard_errors.extend(
                    f"{approval_key or '[missing approval key]'}: {error}" for error in row_errors
                )
        elif disposition == "PROMOTE" and source_state == "REBASED":
            validation = "PASS_REBASED"
        elif disposition in {"SATISFIED", "DEFERRED", "SUPERSEDED"}:
            validation = disposition

        plan_rows.append(
            {
                "approval_key": approval_key,
                "recommendation_key": rec_key,
                "review_key": _clean(approval.get("review_key")),
                "queue_kind": kind,
                "decision": _clean(approval.get("approved_decision")),
                "target": _clean(approval.get("approved_target")),
                "source_state": source_state,
                "disposition": disposition,
                "validation": validation,
                "errors": "; ".join(row_errors),
                "planned_status": planned_status,
            }
        )

    # A conflict can be discovered after an earlier row was tentatively planned.
    if conflicts:
        conflicted_keys = {key for keys in conflicts.values() for key in keys}
        for row in plan_rows:
            if row["approval_key"] in conflicted_keys:
                row["validation"] = "QUARANTINED" if args.bulk else "FAIL"
                row["disposition"] = "QUARANTINED"
                row["planned_status"] = "quarantined:conflicting_approvals"
                if "multiple human-approved" not in row["errors"]:
                    row["errors"] = (row["errors"] + "; " if row["errors"] else "") + (
                        "multiple human-approved recommendations for the same review_key disagree"
                    )
                if args.bulk:
                    receipt_by_approval[row["approval_key"]] = row["planned_status"]
                    recommendation_receipt_by_key[row["recommendation_key"]] = row["planned_status"]
                else:
                    hard_errors.append(f"{row['approval_key']}: {row['errors']}")

    decisions_by_kind: dict[str, list[dict[str, str]]] = {
        kind: [entry["row"] for entry in by_review_key.values()]
        for kind, by_review_key in operational.items()
    }
    # Assign promotion receipts to every approval represented by a deduplicated row.
    for kind, by_review_key in operational.items():
        for entry in by_review_key.values():
            for approval_key in entry["approval_keys"]:
                status = entry["statuses"][approval_key]
                receipt_by_approval[approval_key] = status
            for rec_key in entry["recommendation_keys"]:
                recommendation_receipt_by_key[rec_key] = entry["recommendation_statuses"][rec_key]

    plan = pd.DataFrame(plan_rows)
    plan.to_csv(run_dir / "promotion_plan.csv", index=False)
    counts = plan["disposition"].value_counts().to_dict() if len(plan) else {}
    quarantined = int(counts.get("QUARANTINED", 0))
    promotable = sum(len(rows) for rows in decisions_by_kind.values())

    print("\n── SANITY GATE ──")
    print(f" [PASS] approvals loaded                 {len(eligible)} pending")
    if args.bulk:
        print(
            " [PASS] approvals independently dispositioned "
            f"promote={promotable}; satisfied={counts.get('SATISFIED', 0)}; "
            f"deferred={counts.get('DEFERRED', 0)}; superseded={counts.get('SUPERSEDED', 0)}; "
            f"quarantined={quarantined}"
        )
        gate_ok = True
    else:
        gate_ok = not hard_errors
        print(
            f" [{'PASS' if gate_ok else 'FAIL'}] source and recommendation validation "
            f"{'all pass' if gate_ok else f'{len(hard_errors)} error(s)'}"
        )
    print(" ⇒ " + ("ALL PASS" if gate_ok else "FAILED"))
    if len(plan):
        print("\n  PROMOTION PLAN:")
        columns = ["queue_kind", "review_key", "decision", "target", "source_state", "disposition", "validation"]
        print(plan[columns].to_string(index=False))
    if hard_errors:
        print("\n  BLOCKING ERRORS:")
        for error in hard_errors[:30]:
            print("   -", error)
    if quarantined:
        print("\n  QUARANTINED ROWS (do not block valid approvals in --bulk mode):")
        for row in plan.loc[plan["disposition"] == "QUARANTINED"].head(30).to_dict("records"):
            print(f"   - {row['review_key']}: {row['errors']}")

    summary = {
        "schema": "nih-tracker-ai-bulk-promotion/v1",
        "pending": len(eligible),
        "bulk_mode": bool(args.bulk),
        "promotable_operational_rows": promotable,
        "verify_rows": len(decisions_by_kind["verify"]),
        "crosswalk_rows": len(decisions_by_kind["crosswalk"]),
        "card_rows": len(decisions_by_kind["card"]),
        "dispositions": {str(key): int(value) for key, value in counts.items()},
        "quarantined": quarantined,
        "apply_requested": bool(args.apply),
        "gate_passed": bool(gate_ok),
    }
    atomic_write_json(run_dir / "promotion_summary.json", summary)

    if not gate_ok:
        ledger_append(
            "promote_ai_review",
            params={"apply": args.apply, "bulk": args.bulk},
            inputs={"approvals": args.approvals, "recommendations": args.recommendations},
            outputs={**summary, "errors": len(hard_errors)},
            gate_passed=False,
            run_dir=str(run_dir),
        )
        return 2
    if not args.apply:
        print("\nDRY RUN - no adjudication or approval file was changed.")
        ledger_append(
            "promote_ai_review",
            params={"apply": False, "bulk": args.bulk},
            inputs={"approvals": args.approvals, "recommendations": args.recommendations},
            outputs=summary,
            gate_passed=True,
            run_dir=str(run_dir),
        )
        return 0
    if args.confirm != CONFIRM_TOKEN:
        print(f"Apply requires exact confirmation: {CONFIRM_TOKEN}")
        return 2

    inserted_total = 0
    updated_total = 0
    for kind, rows in decisions_by_kind.items():
        if not rows:
            continue
        prepared = prepare_decisions(kind, rows, reviewer="")
        _path, inserted, updated = upsert_adjudications(kind, prepared)
        inserted_total += inserted
        updated_total += updated

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    run_id = current_run_id()
    approval_updates = []
    recommendation_updates = []
    recommendation_by_key = {
        _clean(row.get("recommendation_key")): row for row in recommendations.to_dict("records")
    }
    for approval in approvals.to_dict("records"):
        approval_key = _clean(approval.get("approval_key"))
        status = receipt_by_approval.get(approval_key)
        if not status:
            continue
        updated = dict(approval)
        updated["promotion_status"] = status
        updated["promoted_at"] = now
        updated["promotion_run_id"] = run_id
        approval_updates.append(updated)
        rec_key = _clean(approval.get("recommendation_key"))
        recommendation = recommendation_by_key.get(rec_key)
        if recommendation:
            rec_updated = dict(recommendation)
            rec_updated["promoted_at"] = now
            rec_updated["promoted_by"] = _clean(approval.get("reviewer"))
            rec_updated["promotion_status"] = recommendation_receipt_by_key.get(rec_key, status)
            recommendation_updates.append(rec_updated)

    _upsert_frame(
        Path(args.approvals),
        APPROVAL_FIELDS,
        approval_updates,
        key_field="approval_key",
        lock_path=AI_APPROVAL_LOCK,
        purpose="receipt_ai_review_approvals",
    )
    _upsert_frame(
        Path(args.recommendations),
        RECOMMENDATION_FIELDS,
        recommendation_updates,
        key_field="recommendation_key",
        lock_path=AI_RECOMMENDATION_LOCK,
        purpose="receipt_ai_review_recommendations",
    )
    summary.update(
        {
            "inserted": inserted_total,
            "updated": updated_total,
            "receipted_approvals": len(approval_updates),
        }
    )
    atomic_write_json(run_dir / "promotion_summary.json", summary)
    print(
        f"\nBulk promotion completed: operational rows={inserted_total + updated_total}; "
        f"approvals receipted={len(approval_updates)}; quarantined={quarantined}. "
        "No card or crosswalk source file was changed."
    )
    ledger_append(
        "promote_ai_review",
        params={"apply": True, "bulk": args.bulk},
        inputs={"approvals": args.approvals, "recommendations": args.recommendations},
        outputs=summary,
        gate_passed=True,
        run_dir=str(run_dir),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
