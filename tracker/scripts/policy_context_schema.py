"""Normalization helpers for NIH repository-policy and dbGaP context blocks."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

SCHEMA_VERSION = "1.0"


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = _clean(value).lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def empty_policy_context() -> dict[str, Any]:
    return {
        "_meta": {
            "block_version": SCHEMA_VERSION,
            "as_of": "",
            "tier": "policy",
            "sources": [],
        },
        "nih_cadr": {
            "status": "not_evaluated",
            "listed_name": "",
            "access_system": "",
            "nih_designated_genomic_data_repository": None,
            "url": "",
            "source_url": "",
            "source_page_last_updated": "",
            "source_sha256": "",
            "match_basis": "",
        },
        "dbgap_external_data_source": {
            "status": "not_established",
            "listed_name": "",
            "uses_dbgap_access_system": None,
            "specific_registration_established": None,
            "evidence_basis": "",
            "source_url": "",
            "definition_source_urls": [],
            "source_as_of": "",
            "note": "",
        },
        "dbgap_public_studies": {
            "association_status": "not_evaluated",
            "study_count": 0,
            "accessions": [],
            "study_names": [],
            "match_terms": [],
            "match_fields": [],
            "source_file": "",
            "source_sha256": "",
            "source_as_of": "",
            "limitation": (
                "Public study-title, description, or collection text does not by itself establish "
                "dbGaP External Data Source registration."
            ),
        },
    }


def normalize_policy_context(value: Any) -> dict[str, Any]:
    result = empty_policy_context()
    if not isinstance(value, Mapping):
        return result
    meta = value.get("_meta") if isinstance(value.get("_meta"), Mapping) else {}
    result["_meta"].update(
        {
            "block_version": _clean(meta.get("block_version")) or SCHEMA_VERSION,
            "as_of": _clean(meta.get("as_of")),
            "tier": _clean(meta.get("tier")) or "policy",
            "sources": sorted(
                {
                    _clean(item)
                    for item in (meta.get("sources") or [])
                    if _clean(item)
                }
            ),
        }
    )

    cadr = value.get("nih_cadr") if isinstance(value.get("nih_cadr"), Mapping) else {}
    result["nih_cadr"].update(
        {
            "status": _clean(cadr.get("status")) or "not_evaluated",
            "listed_name": _clean(cadr.get("listed_name")),
            "access_system": _clean(cadr.get("access_system")),
            "nih_designated_genomic_data_repository": _bool_or_none(
                cadr.get("nih_designated_genomic_data_repository")
            ),
            "url": _clean(cadr.get("url")),
            "source_url": _clean(cadr.get("source_url")),
            "source_page_last_updated": _clean(cadr.get("source_page_last_updated")),
            "source_sha256": _clean(cadr.get("source_sha256")),
            "match_basis": _clean(cadr.get("match_basis")),
        }
    )

    eds = (
        value.get("dbgap_external_data_source")
        if isinstance(value.get("dbgap_external_data_source"), Mapping)
        else {}
    )
    result["dbgap_external_data_source"].update(
        {
            "status": _clean(eds.get("status")) or "not_established",
            "listed_name": _clean(eds.get("listed_name")),
            "uses_dbgap_access_system": _bool_or_none(
                eds.get("uses_dbgap_access_system")
            ),
            "specific_registration_established": _bool_or_none(
                eds.get("specific_registration_established")
            ),
            "evidence_basis": _clean(eds.get("evidence_basis")),
            "source_url": _clean(eds.get("source_url")),
            "definition_source_urls": list(
                dict.fromkeys(
                    _clean(item)
                    for item in (eds.get("definition_source_urls") or [])
                    if _clean(item)
                )
            ),
            "source_as_of": _clean(eds.get("source_as_of")),
            "note": _clean(eds.get("note")),
        }
    )

    studies = (
        value.get("dbgap_public_studies")
        if isinstance(value.get("dbgap_public_studies"), Mapping)
        else {}
    )
    try:
        count = max(0, int(studies.get("study_count") or 0))
    except (TypeError, ValueError):
        count = 0
    result["dbgap_public_studies"].update(
        {
            "association_status": _clean(studies.get("association_status"))
            or "not_evaluated",
            "study_count": count,
            "accessions": list(
                dict.fromkeys(
                    _clean(item)
                    for item in (studies.get("accessions") or [])
                    if _clean(item)
                )
            ),
            "study_names": list(
                dict.fromkeys(
                    _clean(item)
                    for item in (studies.get("study_names") or [])
                    if _clean(item)
                )
            ),
            "match_terms": list(
                dict.fromkeys(
                    _clean(item)
                    for item in (studies.get("match_terms") or [])
                    if _clean(item)
                )
            ),
            "match_fields": list(
                dict.fromkeys(
                    _clean(item)
                    for item in (studies.get("match_fields") or [])
                    if _clean(item)
                )
            ),
            "source_file": _clean(studies.get("source_file")),
            "source_sha256": _clean(studies.get("source_sha256")),
            "source_as_of": _clean(studies.get("source_as_of")),
            "limitation": _clean(studies.get("limitation"))
            or result["dbgap_public_studies"]["limitation"],
        }
    )
    return result


def policy_context_summary(value: Any) -> dict[str, Any]:
    block = normalize_policy_context(value)
    cadr = block["nih_cadr"]
    eds = block["dbgap_external_data_source"]
    studies = block["dbgap_public_studies"]
    return {
        "nih_cadr_status": cadr["status"],
        "is_nih_cadr": int(cadr["status"] == "listed"),
        "nih_cadr_access_system": cadr["access_system"],
        "nih_designated_genomic_data_repository": (
            "yes"
            if cadr["nih_designated_genomic_data_repository"] is True
            else "no"
            if cadr["nih_designated_genomic_data_repository"] is False
            else ""
        ),
        "dbgap_eds_status": eds["status"],
        "dbgap_uses_access_system": (
            "yes"
            if eds["uses_dbgap_access_system"] is True
            else "no"
            if eds["uses_dbgap_access_system"] is False
            else ""
        ),
        "dbgap_specific_registration": (
            "yes"
            if eds["specific_registration_established"] is True
            else "no"
            if eds["specific_registration_established"] is False
            else ""
        ),
        "dbgap_public_study_count": studies["study_count"],
    }


def copy_policy_context(value: Any) -> dict[str, Any]:
    return deepcopy(normalize_policy_context(value))
