"""Build operational and submission-status blocks from BMIC plus reviewed overrides.

BMIC's ``Open Data Submission`` field is treated as a broad-intake criterion.
A negative value means the repository is not open to a broad set of submitters;
it does not, by itself, prove that every project-specific intake route is closed.
Reviewed overrides may establish paused, closed, migrated, retired, or other
resource-specific operational states with an authoritative source URL.

Dry run is the default. ``--apply`` writes ``blocks.repository_status`` inside
one lock-protected, snapshot-backed card transaction.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import yaml

from common import (
    INP,
    PROJECT,
    all_cards,
    card_name_set,
    card_transaction,
    gate,
    ledger_append,
    load_card_for_update,
    new_run_dir,
    save_card,
    sha256_file,
    slug,
    strip_acronym,
)
from repository_status_schema import (
    OPERATIONAL_STATUSES,
    SUBMISSION_STATUSES,
    normalize_repository_status,
    repository_status_summary,
)

DEFAULT_BMIC = str(Path(INP) / "BMIC_download.csv")
DEFAULT_CURATED = str(Path(INP) / "repository_status_curated.csv")
DEFAULT_REFERENCE_OVERRIDES = str(Path(PROJECT) / "reference" / "repository_status_overrides.csv")

COLUMN_ALIASES = {
    "repository_name": ["Repository_Name", "Repository Name", "Name", "Repository"],
    "description": ["Repository_Description", "Repository Description", "Description"],
    "open_data_submission": ["Open_Data_Submission", "Open Data Submission"],
    "data_submission_policy": ["Data_Submission_Policy", "Data Submission Policy"],
    "deposit_timeframe": [
        "Open_Time_Frame_for_Data_Deposit",
        "Open Time Frame for Data Deposit",
        "Open Timeframe for Data Deposit",
    ],
    "access_to_data": ["Access_to_Data", "Access to Data", "Data Access"],
    "access_type": ["Access_Type", "Access Type"],
    "current_nih_funding": [
        "Current_NIH_Funding_Support",
        "Current NIH Funding Support",
    ],
    "sustained_support": ["Sustained_Support", "Sustained Support"],
    "last_reviewed": ["Last_Reviewed", "Last Reviewed"],
}

CURATED_COLUMNS = {
    "repo_uid",
    "operational_status",
    "submission_status",
    "source_url",
    "source_as_of",
    "note",
}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _now_month() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")


def _bool(value: Any) -> bool | None:
    text = _clean(value).casefold()
    if text in {"yes", "y", "true", "1", "open"}:
        return True
    if text in {"no", "n", "false", "0", "closed"}:
        return False
    return None


def _is_url(value: Any) -> bool:
    text = _clean(value)
    if not text:
        return False
    parsed = urlparse(text)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _column(frame: pd.DataFrame, key: str) -> str | None:
    by_fold = {str(column).strip().casefold(): str(column) for column in frame.columns}
    for candidate in COLUMN_ALIASES[key]:
        hit = by_fold.get(candidate.casefold())
        if hit:
            return hit
    return None


def _value(row: pd.Series, columns: dict[str, str | None], key: str) -> str:
    column = columns.get(key)
    return _clean(row.get(column, "")) if column else ""


def _identity_status(card: dict[str, Any]) -> tuple[str, str, str]:
    raw = (card.get("identity") or {}).get("status")
    if isinstance(raw, dict):
        value = _clean(raw.get("value"))
        tier = _clean(raw.get("tier"))
        as_of = _clean(raw.get("as_of"))
    else:
        value, tier, as_of = _clean(raw), "", ""
    normalized = value.casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "operational": "active",
        "current": "active",
        "active": "active",
        "inactive": "inactive",
        "retired": "retired",
        "deprecated": "retired",
        "migrated": "migrated",
        "under_review": "under_review",
        "paused": "temporarily_paused",
        "temporarily_paused": "temporarily_paused",
    }
    return aliases.get(normalized, "unknown"), tier, as_of


def _access_status(access_type: str) -> str:
    text = access_type.casefold()
    has_open = "open" in text
    has_controlled = any(token in text for token in ("controlled", "restricted", "request"))
    if has_open and has_controlled:
        return "mixed"
    if has_open:
        return "open"
    if "controlled" in text:
        return "controlled"
    if "restricted" in text or "request" in text:
        return "restricted"
    return "not_documented"


def _submission_from_bmic(open_submission: bool | None) -> tuple[str, bool | None, str]:
    if open_submission is True:
        return (
            "accepting_broad_submissions",
            True,
            "BMIC reports open data submission, meaning broad investigator intake.",
        )
    if open_submission is False:
        return (
            "accepting_limited_or_programmatic_submissions",
            None,
            "BMIC does not report broad open submission; limited or programmatic intake may still exist.",
        )
    return "not_documented", None, "BMIC open-submission status is not documented."


def _name_index(cards: dict[str, dict[str, Any]]) -> dict[str, set[str]]:
    """Return every normalized card name/acronym mapped to all active UIDs.

    Keeping sets rather than a first-hit index prevents a globally duplicated
    alias from being silently assigned to the wrong card.  Ambiguity is
    evaluated only when a source row actually uses the duplicated term.
    """
    index: dict[str, set[str]] = {}
    for uid, card in cards.items():
        names, acronyms = card_name_set(card)
        for value in names | acronyms:
            index.setdefault(value, set()).add(uid)
    return index


def _distinct_nonempty(values: list[Any]) -> list[str]:
    """Return stable, whitespace-normalized distinct values."""
    return list(dict.fromkeys(_clean(value) for value in values if _clean(value)))


def _coalesce_bmic_rows(
    rows: list[tuple[int, pd.Series]],
    columns: dict[str, str | None],
) -> tuple[pd.Series, list[str], list[str]]:
    """Coalesce duplicate BMIC rows only when decision-critical fields agree.

    Multiple BMIC rows can legitimately describe one repository (for example,
    duplicate exports or complementary descriptive fields).  They are safe to
    coalesce only when their broad-deposit, deposit-timeframe, access, funding,
    and sustained-support assertions do not contradict one another.
    """
    if not rows:
        raise ValueError("Cannot coalesce an empty BMIC row group")

    conflict_fields: list[str] = []
    bool_fields = (
        "open_data_submission",
        "deposit_timeframe",
        "current_nih_funding",
        "sustained_support",
    )
    for key in bool_fields:
        observed = {
            value
            for _source_row, row in rows
            if (value := _bool(_value(row, columns, key))) is not None
        }
        if len(observed) > 1:
            conflict_fields.append(key)

    access_states = {
        status
        for _source_row, row in rows
        if (status := _access_status(_value(row, columns, "access_type")))
        != "not_documented"
    }
    if len(access_states) > 1:
        conflict_fields.append("access_type")

    merged = rows[0][1].copy()
    coalesced_fields: list[str] = []
    description_column = columns.get("description")
    reviewed_column = columns.get("last_reviewed")
    for column in merged.index:
        values = _distinct_nonempty([row.get(column, "") for _line, row in rows])
        if not values:
            merged[column] = ""
            continue
        if len(values) > 1:
            coalesced_fields.append(str(column))
        if column == description_column:
            merged[column] = max(values, key=len)
        elif column == reviewed_column:
            # ISO dates sort chronologically; for non-ISO text this remains a
            # deterministic latest-looking choice and all source rows are kept.
            merged[column] = max(values)
        else:
            merged[column] = values[0]
    return merged, coalesced_fields, conflict_fields


def _match_bmic(
    cards: dict[str, dict[str, Any]],
    bmic: pd.DataFrame,
    name_column: str,
    columns: dict[str, str | None],
) -> tuple[dict[str, tuple[list[int], pd.Series, str, list[str]]], list[dict[str, Any]]]:
    name_index = _name_index(cards)
    grouped: dict[str, list[tuple[int, pd.Series]]] = {}
    row_state: list[dict[str, Any]] = []

    for index, row in bmic.iterrows():
        source_row = int(index) + 2
        name = _clean(row.get(name_column, ""))
        base, acronym = strip_acronym(name)
        terms = [slug(base)]
        if acronym:
            terms.append(slug(acronym))
        candidates: set[str] = set()
        for term in terms:
            if term:
                candidates.update(name_index.get(term, set()))
        uid = next(iter(candidates)) if len(candidates) == 1 else ""
        if uid:
            grouped.setdefault(uid, []).append((source_row, row))
        row_state.append(
            {
                "source_row": source_row,
                "repository_name": name,
                "matched_repo_uid": uid,
                "candidate_repo_uids": "; ".join(sorted(candidates)),
                "candidate_count": len(candidates),
            }
        )

    matches: dict[str, tuple[list[int], pd.Series, str, list[str]]] = {}
    group_results: dict[str, dict[str, Any]] = {}
    for uid, rows in grouped.items():
        source_rows = [source_row for source_row, _row in rows]
        merged, coalesced_fields, conflict_fields = _coalesce_bmic_rows(rows, columns)
        if conflict_fields:
            group_results[uid] = {
                "status": "CONFLICT",
                "match_basis": "contradictory_bmic_rows_same_card",
                "source_rows": source_rows,
                "coalesced_fields": coalesced_fields,
                "conflict_fields": conflict_fields,
            }
            continue
        basis = (
            "exact_card_name_set"
            if len(rows) == 1
            else "coalesced_compatible_bmic_rows"
        )
        matches[uid] = (source_rows, merged, basis, coalesced_fields)
        group_results[uid] = {
            "status": "MATCHED" if len(rows) == 1 else "COALESCED",
            "match_basis": basis,
            "source_rows": source_rows,
            "coalesced_fields": coalesced_fields,
            "conflict_fields": [],
        }

    report: list[dict[str, Any]] = []
    for state in row_state:
        uid = state["matched_repo_uid"]
        candidate_count = int(state.pop("candidate_count"))
        if candidate_count > 1:
            result = {
                "status": "AMBIGUOUS",
                "match_basis": "ambiguous_exact_card_name_set",
                "source_rows": [state["source_row"]],
                "coalesced_fields": [],
                "conflict_fields": [],
            }
        elif not uid:
            result = {
                "status": "UNMATCHED",
                "match_basis": "",
                "source_rows": [state["source_row"]],
                "coalesced_fields": [],
                "conflict_fields": [],
            }
        else:
            result = group_results[uid]
        report.append(
            {
                **state,
                "status": result["status"],
                "match_basis": result["match_basis"],
                "contributing_source_rows": "; ".join(
                    str(value) for value in result["source_rows"]
                ),
                "coalesced_fields": "; ".join(result["coalesced_fields"]),
                "conflict_fields": "; ".join(result["conflict_fields"]),
            }
        )
    return matches, report



def _host(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"https://{text}")
    return (parsed.hostname or "").casefold().removeprefix("www.")


def _card_homepage(card: dict[str, Any]) -> str:
    value = (card.get("identity") or {}).get("homepage")
    if isinstance(value, list):
        value = value[0] if value else ""
    return _clean(value)


def _load_reference_overrides(
    path: Path, cards: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]]]:
    if not path.is_file() or path.stat().st_size == 0:
        return {}, []
    frame = pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")
    frame.columns = [str(column).strip() for column in frame.columns]
    required = {
        "repo_uid", "match_terms", "homepage", "operational_status",
        "submission_status", "source_url", "source_as_of", "note",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Reference status overrides lack columns: {sorted(missing)}")
    name_index = _name_index(cards)
    host_index: dict[str, set[str]] = {}
    for uid, card in cards.items():
        host = _host(_card_homepage(card))
        if host:
            host_index.setdefault(host, set()).add(uid)
    output: dict[str, dict[str, str]] = {}
    report: list[dict[str, Any]] = []
    for index, raw in frame.iterrows():
        row = {str(key): _clean(value) for key, value in raw.items()}
        candidates: set[str] = set()
        basis = ""
        configured_uid = row.get("repo_uid", "")
        if configured_uid:
            if configured_uid in cards:
                candidates = {configured_uid}
                basis = "reference_uid"
            else:
                basis = "inactive_reference_uid"
        if not candidates:
            terms = [
                _clean(value)
                for value in re.split(r"\s*[;|]\s*", row.get("match_terms", ""))
                if _clean(value)
            ]
            for term in terms:
                candidates.update(name_index.get(slug(term), set()))
            if candidates:
                basis = "exact_card_name_set"
        if not candidates:
            host = _host(row.get("homepage", ""))
            host_candidates = host_index.get(host, set()) if host else set()
            if len(host_candidates) == 1:
                candidates = set(host_candidates)
                basis = "unique_homepage_host"
            elif len(host_candidates) > 1:
                candidates = set(host_candidates)
                basis = "shared_homepage_host"
        uid = next(iter(candidates)) if len(candidates) == 1 else ""
        status = "MATCHED" if uid else "AMBIGUOUS" if candidates else "UNMATCHED"
        if uid:
            if uid in output:
                raise ValueError(f"Multiple reference status override rows match {uid}")
            operational = row.get("operational_status") or "unknown"
            submission = row.get("submission_status") or "not_documented"
            if operational not in OPERATIONAL_STATUSES:
                raise ValueError(f"Invalid reference operational_status for {uid}: {operational!r}")
            if submission not in SUBMISSION_STATUSES:
                raise ValueError(f"Invalid reference submission_status for {uid}: {submission!r}")
            if not row.get("source_url") or not row.get("source_as_of"):
                raise ValueError(f"Reference status override for {uid} lacks source URL/date")
            output[uid] = row
        report.append(
            {
                "source_row": int(index) + 2,
                "match_terms": row.get("match_terms", ""),
                "homepage": row.get("homepage", ""),
                "configured_repo_uid": configured_uid,
                "matched_repo_uid": uid,
                "status": status,
                "match_basis": basis,
                "candidate_repo_uids": "; ".join(sorted(candidates)),
                "operational_status": row.get("operational_status", ""),
                "submission_status": row.get("submission_status", ""),
                "source_url": row.get("source_url", ""),
            }
        )
    return output, report

def _load_curated(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file() or path.stat().st_size == 0:
        return {}
    frame = pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")
    frame.columns = [str(column).strip() for column in frame.columns]
    missing = CURATED_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"Curated repository-status file lacks columns: {sorted(missing)}")
    output: dict[str, dict[str, str]] = {}
    for row in frame.to_dict("records"):
        uid = _clean(row.get("repo_uid"))
        if not uid or uid.startswith("#"):
            continue
        if uid in output:
            raise ValueError(f"Duplicate curated repository-status row for {uid}")
        operational = _clean(row.get("operational_status")) or "unknown"
        submission = _clean(row.get("submission_status")) or "not_documented"
        if operational not in OPERATIONAL_STATUSES:
            raise ValueError(f"Invalid operational_status for {uid}: {operational!r}")
        if submission not in SUBMISSION_STATUSES:
            raise ValueError(f"Invalid submission_status for {uid}: {submission!r}")
        if operational != "unknown" or submission != "not_documented":
            if not _clean(row.get("source_url")) or not _clean(row.get("source_as_of")):
                raise ValueError(
                    f"Curated repository-status row for {uid} requires source_url and source_as_of"
                )
        output[uid] = {key: _clean(value) for key, value in row.items()}
    return output


def run(*, bmic_path: Path, curated_path: Path, reference_overrides_path: Path, apply: bool) -> tuple[bool, dict[str, Any], Path]:
    run_dir = Path(new_run_dir("build_repository_status"))
    cards = all_cards()
    bmic = pd.read_csv(bmic_path, dtype=str, keep_default_na=False).fillna("")
    bmic.columns = [str(column).strip() for column in bmic.columns]
    columns = {key: _column(bmic, key) for key in COLUMN_ALIASES}
    name_column = columns["repository_name"]
    if not name_column:
        raise ValueError(
            "BMIC file lacks a recognized repository-name column. "
            f"Observed columns: {list(bmic.columns)}"
        )
    curated = _load_curated(curated_path)
    reference_overrides, reference_report = _load_reference_overrides(
        reference_overrides_path, cards
    )
    bmic_sha = sha256_file(bmic_path) or ""
    curated_sha = sha256_file(curated_path) if curated_path.is_file() else None
    reference_sha = sha256_file(reference_overrides_path) if reference_overrides_path.is_file() else None
    matched, match_report = _match_bmic(cards, bmic, name_column, columns)

    blocks: dict[str, dict[str, Any]] = {}
    proposal_rows: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    changed: list[str] = []
    invalid_curated = sorted(set(curated) - set(cards))

    for uid, card in cards.items():
        identity_operational, identity_tier, identity_as_of = _identity_status(card)
        operational = {
            "status": identity_operational,
            "label": identity_operational.replace("_", " ").title(),
            "note": "",
            "source_url": "",
            "source_as_of": identity_as_of,
            "source": "card:identity.status" if identity_operational != "unknown" else "",
            "tier": identity_tier,
        }
        submissions = {
            "status": "not_documented",
            "accepting_user_contributed_data": None,
            "open_data_submission": None,
            "open_time_frame_for_data_deposit": None,
            "policy_url": "",
            "policy_text": "",
            "note": "",
            "source_url": "",
            "source_as_of": "",
            "source": "",
            "tier": "",
        }
        access = {"status": "not_documented", "access_type": "", "access_url": ""}
        support = {"current_nih_funding_support": None, "sustained_support": None}
        bmic_record = {
            "matched": False,
            "repository_name": "",
            "source_file": bmic_path.name,
            "source_sha256": bmic_sha,
            "source_row": None,
            "source_rows": [],
            "coalesced": False,
            "coalesced_fields": [],
            "last_reviewed": "",
            "match_basis": "",
        }
        sources: list[str] = []
        as_of_values = [identity_as_of]

        if uid in matched:
            source_rows, row, match_basis, coalesced_fields = matched[uid]
            source_row = source_rows[0]
            open_submission = _bool(_value(row, columns, "open_data_submission"))
            deposit_timeframe = _bool(_value(row, columns, "deposit_timeframe"))
            submission_status, accepting, submission_note = _submission_from_bmic(open_submission)
            policy = _value(row, columns, "data_submission_policy")
            access_url = _value(row, columns, "access_to_data")
            access_type = _value(row, columns, "access_type")
            last_reviewed = _value(row, columns, "last_reviewed")
            submissions.update(
                {
                    "status": submission_status,
                    "accepting_user_contributed_data": accepting,
                    "open_data_submission": open_submission,
                    "open_time_frame_for_data_deposit": deposit_timeframe,
                    "policy_url": policy if _is_url(policy) else "",
                    "policy_text": "" if _is_url(policy) else policy,
                    "note": submission_note,
                    "source_url": policy if _is_url(policy) else "",
                    "source_as_of": last_reviewed,
                    "source": f"bmic:{bmic_sha}",
                    "tier": "attestation",
                }
            )
            access.update(
                {
                    "status": _access_status(access_type),
                    "access_type": access_type,
                    "access_url": access_url if _is_url(access_url) else "",
                }
            )
            support.update(
                {
                    "current_nih_funding_support": _bool(
                        _value(row, columns, "current_nih_funding")
                    ),
                    "sustained_support": _bool(_value(row, columns, "sustained_support")),
                }
            )
            bmic_record.update(
                {
                    "matched": True,
                    "repository_name": _value(row, columns, "repository_name"),
                    "source_row": source_row,
                    "source_rows": source_rows,
                    "coalesced": len(source_rows) > 1,
                    "coalesced_fields": coalesced_fields,
                    "last_reviewed": last_reviewed,
                    "match_basis": match_basis,
                }
            )
            sources.append(f"bmic:{bmic_sha}")
            as_of_values.append(last_reviewed)

        if uid in reference_overrides:
            row = reference_overrides[uid]
            operational_status = row.get("operational_status") or operational["status"]
            submission_status = row.get("submission_status") or submissions["status"]
            operational.update(
                {
                    "status": operational_status,
                    "label": row.get("operational_label") or operational_status.replace("_", " ").title(),
                    "note": row.get("note", ""),
                    "source_url": row.get("source_url", ""),
                    "source_as_of": row.get("source_as_of", ""),
                    "source": "reference:repository_status_overrides.csv",
                    "tier": "curated",
                }
            )
            accepting_override = _bool(row.get("accepting_user_contributed_data"))
            if accepting_override is None:
                if submission_status == "accepting_broad_submissions":
                    accepting_override = True
                elif submission_status in {"not_accepting_new_submissions", "temporarily_paused", "not_applicable"}:
                    accepting_override = False
            submissions.update(
                {
                    "status": submission_status,
                    "accepting_user_contributed_data": accepting_override,
                    "policy_url": row.get("submission_policy_url", "") or submissions["policy_url"],
                    "policy_text": row.get("submission_policy_text", "") or submissions["policy_text"],
                    "note": row.get("submission_note", "") or row.get("note", ""),
                    "source_url": row.get("source_url", ""),
                    "source_as_of": row.get("source_as_of", ""),
                    "source": "reference:repository_status_overrides.csv",
                    "tier": "curated",
                }
            )
            if row.get("access_status"):
                access.update(
                    {
                        "status": row.get("access_status", ""),
                        "note": row.get("access_note", ""),
                        "source_url": row.get("source_url", ""),
                        "source_as_of": row.get("source_as_of", ""),
                    }
                )
            sources.append(f"reference_repository_status:{reference_sha or ''}")
            as_of_values.append(row.get("source_as_of", ""))

        if uid in curated:
            row = curated[uid]
            operational_status = row["operational_status"] or operational["status"]
            submission_status = row["submission_status"] or submissions["status"]
            operational.update(
                {
                    "status": operational_status,
                    "label": _clean(row.get("operational_label"))
                    or operational_status.replace("_", " ").title(),
                    "note": row.get("note", ""),
                    "source_url": row.get("source_url", ""),
                    "source_as_of": row.get("source_as_of", ""),
                    "source": "curated:repository_status_curated.csv",
                    "tier": "curated",
                }
            )
            accepting_override = _bool(row.get("accepting_user_contributed_data"))
            if accepting_override is None:
                if submission_status == "accepting_broad_submissions":
                    accepting_override = True
                elif submission_status in {
                    "not_accepting_new_submissions",
                    "temporarily_paused",
                    "not_applicable",
                }:
                    accepting_override = False
            submissions.update(
                {
                    "status": submission_status,
                    "accepting_user_contributed_data": accepting_override,
                    "policy_url": row.get("submission_policy_url", "")
                    or submissions["policy_url"],
                    "policy_text": row.get("submission_policy_text", "")
                    or submissions["policy_text"],
                    "note": row.get("submission_note", "") or row.get("note", ""),
                    "source_url": row.get("source_url", ""),
                    "source_as_of": row.get("source_as_of", ""),
                    "source": "curated:repository_status_curated.csv",
                    "tier": "curated",
                }
            )
            if row.get("access_status"):
                access.update(
                    {
                        "status": row.get("access_status", ""),
                        "note": row.get("access_note", ""),
                        "source_url": row.get("source_url", ""),
                        "source_as_of": row.get("source_as_of", ""),
                    }
                )
            sources.append(f"curated_repository_status:{curated_sha or ''}")
            as_of_values.append(row.get("source_as_of", ""))

        notices: list[str] = []
        if submissions["status"] == "accepting_limited_or_programmatic_submissions":
            notices.append(
                "Not open to broad submission does not establish that all project-specific intake is closed."
            )
        if operational["status"] == "active" and submissions["status"] in {
            "not_accepting_new_submissions",
            "temporarily_paused",
        }:
            notices.append(
                "The resource may remain operational for discovery or access while new deposits are closed or paused."
            )

        block = normalize_repository_status(
            {
                "_meta": {
                    "block_version": "1.0",
                    "as_of": max([value for value in as_of_values if value] or [_now_month()]),
                    "tier": "curated" if uid in curated or uid in reference_overrides else "attestation",
                    "review_state": "human_confirmed" if uid in curated or uid in reference_overrides else "auto",
                    "sources": sources,
                },
                "operational": operational,
                "submissions": submissions,
                "access": access,
                "support": support,
                "bmic": bmic_record,
                "notices": notices,
            }
        )
        blocks[uid] = block
        current = normalize_repository_status((card.get("blocks") or {}).get("repository_status"))
        is_changed = current != block
        if is_changed:
            changed.append(uid)
        summary = repository_status_summary(block)
        proposal_rows.append(
            {
                "repo_uid": uid,
                "name": _clean((card.get("identity") or {}).get("name")),
                **summary,
                "changed": str(is_changed).lower(),
            }
        )
        if block["operational"]["status"] == "unknown":
            gaps.append({"repo_uid": uid, "gap_type": "operational_status_not_documented"})
        if block["submissions"]["status"] == "not_documented":
            gaps.append({"repo_uid": uid, "gap_type": "submission_status_not_documented"})
        if block["submissions"]["status"] == "accepting_limited_or_programmatic_submissions":
            gaps.append({"repo_uid": uid, "gap_type": "specific_submission_path_not_documented"})

    match_frame = pd.DataFrame(match_report)
    proposal_frame = pd.DataFrame(proposal_rows)
    gaps_frame = pd.DataFrame(gaps)
    match_frame.to_csv(run_dir / "bmic_status_match_report.csv", index=False)
    reference_frame = pd.DataFrame(reference_report)
    reference_frame.to_csv(run_dir / "repository_status_override_match_report.csv", index=False)
    proposal_frame.to_csv(run_dir / "repository_status_proposal.csv", index=False)
    gaps_frame.to_csv(run_dir / "repository_status_gaps.csv", index=False)
    (run_dir / "repository_status_blocks.yaml").write_text(
        yaml.safe_dump(blocks, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    conflict_rows = match_frame.loc[
        match_frame.get("status", pd.Series(dtype=str)).eq("CONFLICT")
    ] if not match_frame.empty else pd.DataFrame()
    conflict_count = (
        int(conflict_rows["matched_repo_uid"].replace("", pd.NA).dropna().nunique())
        if not conflict_rows.empty
        else 0
    )
    coalesced_count = int(
        match_frame.loc[
            match_frame.get("status", pd.Series(dtype=str)).eq("COALESCED"),
            "matched_repo_uid",
        ].replace("", pd.NA).dropna().nunique()
    ) if not match_frame.empty else 0
    bmic_ambiguous = int((match_frame.get("status", pd.Series(dtype=str)) == "AMBIGUOUS").sum())
    reference_ambiguous = int((reference_frame.get("status", pd.Series(dtype=str)) == "AMBIGUOUS").sum())
    checks = {
        "cards loaded": (len(cards) > 0, f"{len(cards)} cards"),
        "BMIC rows loaded": (len(bmic) > 0, f"{len(bmic)} rows"),
        "BMIC repository-name column": (bool(name_column), str(name_column)),
        "BMIC assignments unambiguous": (bmic_ambiguous == 0, f"{bmic_ambiguous} ambiguous row(s)"),
        "BMIC assignments conflict-free": (conflict_count == 0, f"{conflict_count} conflict(s)"),
        "curated UIDs active": (not invalid_curated, ", ".join(invalid_curated) or "all active"),
        "reference overrides unambiguous": (reference_ambiguous == 0, f"{reference_ambiguous} ambiguous"),
        "status blocks normalized": (len(blocks) == len(cards), f"{len(blocks)} blocks"),
    }
    ok = gate(checks)

    applied = 0
    if apply and ok and changed:
        with card_transaction("build_repository_status"):
            locked = {uid: load_card_for_update(uid, cards[uid]) for uid in changed}
            for uid in changed:
                next_card = copy.deepcopy(locked[uid])
                next_card.setdefault("blocks", {})["repository_status"] = blocks[uid]
                save_card(uid, next_card)
        applied = len(changed)
        print(f"\n  Applied {applied} repository-status update(s) transactionally.")
    elif apply and not ok:
        print("\n  Gate failed; no card was changed.")
    elif apply:
        print("\n  Apply requested; valid no-op because repository status is current.")
    else:
        print("\n  DRY RUN - inspect repository_status_proposal.csv and gap/match reports.")

    coverage = {
        "cards": len(cards),
        "bmic_matched": sum(1 for block in blocks.values() if block["bmic"]["matched"]),
        "bmic_coalesced_cards": coalesced_count,
        "reference_overrides_matched": len(reference_overrides),
        "reference_overrides_unmatched": int((reference_frame.get("status", pd.Series(dtype=str)) == "UNMATCHED").sum()),
        "operational_active": sum(1 for block in blocks.values() if block["operational"]["status"] == "active"),
        "operational_nonactive_known": sum(
            1
            for block in blocks.values()
            if block["operational"]["status"] not in {"active", "unknown"}
        ),
        "accepting_broad_submissions": sum(
            1
            for block in blocks.values()
            if block["submissions"]["status"] == "accepting_broad_submissions"
        ),
        "limited_or_programmatic": sum(
            1
            for block in blocks.values()
            if block["submissions"]["status"]
            == "accepting_limited_or_programmatic_submissions"
        ),
        "not_accepting_or_paused": sum(
            1
            for block in blocks.values()
            if block["submissions"]["status"]
            in {"not_accepting_new_submissions", "temporarily_paused"}
        ),
        "submission_not_documented": sum(
            1
            for block in blocks.values()
            if block["submissions"]["status"] == "not_documented"
        ),
        "changed": len(changed),
        "applied": applied,
    }
    pd.Series(coverage).to_json(run_dir / "coverage.json", indent=2)
    ledger_append(
        "build_repository_status",
        params={"bmic": bmic_path.name, "curated": curated_path.name, "reference_overrides": reference_overrides_path.name, "apply": apply},
        inputs={"bmic": bmic_sha, "curated": curated_sha, "reference_overrides": reference_sha},
        outputs=coverage,
        gate_passed=ok,
        run_dir=str(run_dir),
    )
    print("\n  REPOSITORY STATUS COVERAGE")
    for key, value in coverage.items():
        print(f"  {key:36s} {value}")
    print(f"\nRepository-status run: {run_dir}")
    return ok, coverage, run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bmic", default=DEFAULT_BMIC)
    parser.add_argument("--curated", default=DEFAULT_CURATED)
    parser.add_argument("--reference-overrides", default=DEFAULT_REFERENCE_OVERRIDES)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    bmic_path = Path(args.bmic).expanduser()
    if not bmic_path.is_absolute() and not bmic_path.is_file():
        bmic_path = Path(INP) / bmic_path
    curated_path = Path(args.curated).expanduser()
    if not curated_path.is_absolute() and not curated_path.is_file():
        curated_path = Path(INP) / curated_path
    reference_overrides_path = Path(args.reference_overrides).expanduser()
    ok, _, _ = run(
        bmic_path=bmic_path,
        curated_path=curated_path,
        reference_overrides_path=reference_overrides_path,
        apply=args.apply,
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
