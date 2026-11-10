"""Canonical operational and submission-status schema for NIH Tracker cards.

The block separates whether a resource is operating from whether it accepts new
third-party deposits.  BMIC's ``Open Data Submission`` field means broad intake;
a false value does not prove that every submission pathway is closed.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

SCHEMA_VERSION = "1.1"

OPERATIONAL_STATUSES = {
    "active",
    "temporarily_paused",
    "under_review",
    "migrating",
    "migrated",
    "retired",
    "inactive",
    "unknown",
}
SUBMISSION_STATUSES = {
    "accepting_broad_submissions",
    "accepting_limited_or_programmatic_submissions",
    "not_accepting_new_submissions",
    "temporarily_paused",
    "not_applicable",
    "not_documented",
}
ACCESS_STATUSES = {"open", "controlled", "mixed", "restricted", "temporarily_paused", "migrated", "unavailable", "not_documented"}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = _clean(value).casefold()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]
    return list(dict.fromkeys(_clean(item) for item in values if _clean(item)))


def empty_repository_status() -> dict[str, Any]:
    return {
        "_meta": {
            "block_version": SCHEMA_VERSION,
            "as_of": "",
            "tier": "attestation",
            "review_state": "auto",
            "sources": [],
        },
        "operational": {
            "status": "unknown",
            "label": "Operational status not documented",
            "note": "",
            "source_url": "",
            "source_as_of": "",
            "source": "",
            "tier": "",
        },
        "submissions": {
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
        },
        "access": {
            "status": "not_documented",
            "access_type": "",
            "access_url": "",
            "note": "",
            "source_url": "",
            "source_as_of": "",
        },
        "support": {
            "current_nih_funding_support": None,
            "sustained_support": None,
        },
        "bmic": {
            "matched": False,
            "repository_name": "",
            "source_file": "",
            "source_sha256": "",
            "source_row": None,
            "source_rows": [],
            "coalesced": False,
            "coalesced_fields": [],
            "last_reviewed": "",
            "match_basis": "",
        },
        "notices": [],
    }


def normalize_repository_status(value: Any) -> dict[str, Any]:
    result = empty_repository_status()
    if not isinstance(value, Mapping):
        return result

    meta = value.get("_meta") if isinstance(value.get("_meta"), Mapping) else {}
    result["_meta"].update(
        {
            "block_version": _clean(meta.get("block_version")) or SCHEMA_VERSION,
            "as_of": _clean(meta.get("as_of")),
            "tier": _clean(meta.get("tier")) or "attestation",
            "review_state": _clean(meta.get("review_state")) or "auto",
            "sources": _list(meta.get("sources")),
        }
    )

    operational = value.get("operational") if isinstance(value.get("operational"), Mapping) else {}
    operational_status = _clean(operational.get("status")) or "unknown"
    if operational_status not in OPERATIONAL_STATUSES:
        operational_status = "unknown"
    result["operational"].update(
        {
            "status": operational_status,
            "label": _clean(operational.get("label")) or operational_status.replace("_", " ").title(),
            "note": _clean(operational.get("note")),
            "source_url": _clean(operational.get("source_url")),
            "source_as_of": _clean(operational.get("source_as_of")),
            "source": _clean(operational.get("source")),
            "tier": _clean(operational.get("tier")),
        }
    )

    submissions = value.get("submissions") if isinstance(value.get("submissions"), Mapping) else {}
    submission_status = _clean(submissions.get("status")) or "not_documented"
    if submission_status not in SUBMISSION_STATUSES:
        submission_status = "not_documented"
    accepting = _bool_or_none(submissions.get("accepting_user_contributed_data"))
    if accepting is None:
        if submission_status == "accepting_broad_submissions":
            accepting = True
        elif submission_status in {"not_accepting_new_submissions", "temporarily_paused", "not_applicable"}:
            accepting = False
    result["submissions"].update(
        {
            "status": submission_status,
            "accepting_user_contributed_data": accepting,
            "open_data_submission": _bool_or_none(submissions.get("open_data_submission")),
            "open_time_frame_for_data_deposit": _bool_or_none(
                submissions.get("open_time_frame_for_data_deposit")
            ),
            "policy_url": _clean(submissions.get("policy_url")),
            "policy_text": _clean(submissions.get("policy_text")),
            "note": _clean(submissions.get("note")),
            "source_url": _clean(submissions.get("source_url")),
            "source_as_of": _clean(submissions.get("source_as_of")),
            "source": _clean(submissions.get("source")),
            "tier": _clean(submissions.get("tier")),
        }
    )

    access = value.get("access") if isinstance(value.get("access"), Mapping) else {}
    access_status = _clean(access.get("status")) or "not_documented"
    if access_status not in ACCESS_STATUSES:
        access_status = "not_documented"
    result["access"].update(
        {
            "status": access_status,
            "access_type": _clean(access.get("access_type")),
            "access_url": _clean(access.get("access_url")),
            "note": _clean(access.get("note")),
            "source_url": _clean(access.get("source_url")),
            "source_as_of": _clean(access.get("source_as_of")),
        }
    )

    support = value.get("support") if isinstance(value.get("support"), Mapping) else {}
    result["support"].update(
        {
            "current_nih_funding_support": _bool_or_none(
                support.get("current_nih_funding_support")
            ),
            "sustained_support": _bool_or_none(support.get("sustained_support")),
        }
    )

    bmic = value.get("bmic") if isinstance(value.get("bmic"), Mapping) else {}
    source_row = bmic.get("source_row")
    try:
        source_row = int(source_row) if source_row not in (None, "") else None
    except (TypeError, ValueError):
        source_row = None
    source_rows: list[int] = []
    raw_source_rows = bmic.get("source_rows")
    if isinstance(raw_source_rows, (list, tuple, set)):
        for source_row_value in raw_source_rows:
            try:
                integer = int(source_row_value)
            except (TypeError, ValueError):
                continue
            if integer not in source_rows:
                source_rows.append(integer)
    if source_row is not None and source_row not in source_rows:
        source_rows.insert(0, source_row)
    result["bmic"].update(
        {
            "matched": bool(_bool_or_none(bmic.get("matched")) is True),
            "repository_name": _clean(bmic.get("repository_name")),
            "source_file": _clean(bmic.get("source_file")),
            "source_sha256": _clean(bmic.get("source_sha256")),
            "source_row": source_row,
            "source_rows": source_rows,
            "coalesced": bool(_bool_or_none(bmic.get("coalesced")) is True),
            "coalesced_fields": _list(bmic.get("coalesced_fields")),
            "last_reviewed": _clean(bmic.get("last_reviewed")),
            "match_basis": _clean(bmic.get("match_basis")),
        }
    )
    result["notices"] = _list(value.get("notices"))
    return result


def repository_status_summary(value: Any) -> dict[str, Any]:
    block = normalize_repository_status(value)
    operational = block["operational"]
    submissions = block["submissions"]
    access = block["access"]
    support = block["support"]
    return {
        "operational_status": operational["status"],
        "operational_status_label": operational["label"],
        "submission_status": submissions["status"],
        "accepting_user_contributed_data": (
            "yes"
            if submissions["accepting_user_contributed_data"] is True
            else "no"
            if submissions["accepting_user_contributed_data"] is False
            else ""
        ),
        "bmic_open_data_submission": (
            "yes"
            if submissions["open_data_submission"] is True
            else "no"
            if submissions["open_data_submission"] is False
            else ""
        ),
        "open_time_frame_for_data_deposit": (
            "yes"
            if submissions["open_time_frame_for_data_deposit"] is True
            else "no"
            if submissions["open_time_frame_for_data_deposit"] is False
            else ""
        ),
        "access_status": access["status"],
        "current_nih_funding_support": (
            "yes"
            if support["current_nih_funding_support"] is True
            else "no"
            if support["current_nih_funding_support"] is False
            else ""
        ),
        "sustained_support": (
            "yes"
            if support["sustained_support"] is True
            else "no"
            if support["sustained_support"] is False
            else ""
        ),
        "bmic_status_matched": int(block["bmic"]["matched"]),
    }


def copy_repository_status(value: Any) -> dict[str, Any]:
    return deepcopy(normalize_repository_status(value))
