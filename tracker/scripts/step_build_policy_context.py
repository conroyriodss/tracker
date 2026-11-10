"""Build NIH CADR and dbGaP context blocks from pinned authoritative sources.

The step keeps three assertions separate:

* NIH CADR listing: exact official list membership as of a source snapshot.
* dbGaP External Data Source (EDS): confirmed through CADR subtype status or an
  explicit curated authoritative record. Absence is recorded as "not established",
  not as proof that the resource is not an EDS.
* dbGaP public-study association: exact text matches in a pinned public study
  export. This is contextual evidence only and never establishes EDS status.

Dry run is the default. ``--apply`` writes ``blocks.policy_context`` in one
locked, snapshot-backed card transaction.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
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
    atomic_write_json,
    card_transaction,
    gate,
    ledger_append,
    load_card_for_update,
    new_run_dir,
    save_card,
    sha256_file,
)
from policy_context_schema import normalize_policy_context, policy_context_summary

DEFAULT_CADR_SNAPSHOT = str(Path(PROJECT) / "reference" / "nih_cadr_registry.csv")
DEFAULT_DBGAP_STUDIES = str(Path(PROJECT) / "reference" / "dbGaP_Studies.csv")
DEFAULT_DBGAP_DEFINITION = str(Path(PROJECT) / "reference" / "dbgap_eds_definition.json")
DEFAULT_CURATED_EDS = str(Path(INP) / "dbgap_external_data_sources.csv")

REQUIRED_CADR_COLUMNS = {
    "official_name",
    "access_system",
    "nih_designated_genomic_data_repository",
    "url",
    "repo_uid",
    "match_terms",
    "source_page_last_updated",
    "source_url",
}
REQUIRED_STUDY_COLUMNS = {"accession", "name", "description", "Collections"}
REQUIRED_CURATED_COLUMNS = {
    "repo_uid",
    "status",
    "listed_name",
    "source_url",
    "source_as_of",
    "uses_dbgap_access_system",
    "note",
}

ALLOWED_CURATED_EDS_STATUSES = {
    "registered_eds",
    "former_eds",
    "not_current",
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")




def _load_dbgap_definition(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Pinned dbGaP EDS definition is missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Pinned dbGaP EDS definition must be a JSON object")
    interpretation = payload.get("interpretation")
    source_urls = payload.get("source_urls")
    if not isinstance(interpretation, dict):
        raise ValueError("Pinned dbGaP EDS definition lacks interpretation object")
    if not isinstance(source_urls, list) or not all(str(item).strip() for item in source_urls):
        raise ValueError("Pinned dbGaP EDS definition lacks source_urls")
    required_true = {
        "external_entity_required",
        "qualifying_eds_is_nih_cadr",
        "specific_study_designation_occurs_in_dbgap_registration_system",
    }
    failed = sorted(
        key for key in required_true if interpretation.get(key) is not True
    )
    if failed:
        raise ValueError(
            "Pinned dbGaP EDS definition does not support required interpretation: "
            + ", ".join(failed)
        )
    return payload

def _read_csv(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")
    frame.columns = [str(column).strip() for column in frame.columns]
    return frame


def _norm(value: Any) -> str:
    text = str(value or "").casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _host(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"https://{text}")
    hostname = (parsed.hostname or "").casefold().removeprefix("www.")
    aliases = {
        "ncbi.nlm.nih.gov": "ncbi.nlm.nih.gov",
        "www.ncbi.nlm.nih.gov": "ncbi.nlm.nih.gov",
    }
    return aliases.get(hostname, hostname)


def _identity_names(card: dict[str, Any]) -> list[str]:
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    values: list[str] = []
    for key in ("name", "full_name", "abbreviation"):
        value = str(identity.get(key) or "").strip()
        if value:
            values.append(value)
    aliases = identity.get("aliases") if isinstance(identity.get("aliases"), list) else []
    values.extend(str(value).strip() for value in aliases if str(value).strip())
    return list(dict.fromkeys(values))


def _homepage(card: dict[str, Any]) -> str:
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    value = identity.get("homepage") or ""
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def _parse_bool(value: Any) -> bool | None:
    text = str(value or "").strip().casefold()
    if text in {"y", "yes", "true", "1"}:
        return True
    if text in {"n", "no", "false", "0"}:
        return False
    return None


def _active_uid_remap(cards: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Resolve retired UIDs through the project remap ledger to active cards."""
    path = Path(PROJECT) / "id_remap.csv"
    if not path.is_file() or not path.stat().st_size:
        return {}
    frame = pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")
    if not {"old_uid", "new_uid"}.issubset(frame.columns):
        raise ValueError("id_remap.csv lacks old_uid/new_uid columns")
    direct: dict[str, str] = {}
    for row in frame.to_dict("records"):
        old_uid = str(row.get("old_uid") or "").strip()
        new_uid = str(row.get("new_uid") or "").strip()
        if not old_uid or not new_uid:
            continue
        if old_uid in direct and direct[old_uid] != new_uid:
            raise ValueError(f"Conflicting id_remap targets for {old_uid}: {direct[old_uid]}, {new_uid}")
        direct[old_uid] = new_uid
    resolved: dict[str, str] = {}
    for old_uid in direct:
        current = old_uid
        seen: set[str] = set()
        while current in direct:
            if current in seen:
                raise ValueError(f"Cycle in id_remap.csv involving {current}")
            seen.add(current)
            current = direct[current]
        if current in cards:
            resolved[old_uid] = current
    return resolved


def _cadr_matches(
    cards: dict[str, dict[str, Any]],
    cadr: pd.DataFrame,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    by_uid: dict[str, dict[str, Any]] = {}
    report: list[dict[str, Any]] = []
    card_hosts = {uid: _host(_homepage(card)) for uid, card in cards.items()}
    uid_remap = _active_uid_remap(cards)
    for row_number, row in cadr.iterrows():
        official_name = str(row["official_name"]).strip()
        configured_uid = str(row["repo_uid"]).strip()
        official_host = _host(row["url"])
        matched_uid = ""
        basis = ""
        status = "UNMATCHED"
        candidates: list[str] = []
        resolved_configured_uid = ""
        if configured_uid:
            if configured_uid in cards:
                matched_uid = configured_uid
                resolved_configured_uid = configured_uid
                basis = "reference_uid"
                status = "MATCHED"
            elif configured_uid in uid_remap:
                matched_uid = uid_remap[configured_uid]
                resolved_configured_uid = matched_uid
                basis = "reference_uid_remapped"
                status = "MATCHED"
            else:
                basis = "stale_reference_uid"
                status = "STALE_REFERENCE"
        else:
            host_candidates = [
                uid for uid, card_host in card_hosts.items()
                if official_host and card_host and official_host == card_host
            ]
            candidates.extend(host_candidates)
            if len(host_candidates) == 1:
                matched_uid = host_candidates[0]
                basis = "unique_homepage_host"
                status = "MATCHED"
            elif len(host_candidates) > 1:
                status = "AMBIGUOUS"
                basis = "shared_homepage_host"
        if matched_uid:
            if matched_uid in by_uid:
                status = "CONFLICT"
                basis = "multiple_cadr_rows_same_card"
            else:
                by_uid[matched_uid] = {**row.to_dict(), "match_basis": basis}
        report.append(
            {
                "source_row": int(row_number) + 2,
                "official_name": official_name,
                "configured_repo_uid": configured_uid,
                "resolved_configured_repo_uid": resolved_configured_uid,
                "matched_repo_uid": matched_uid,
                "status": status,
                "match_basis": basis,
                "candidate_repo_uids": "; ".join(sorted(set(candidates))),
                "official_url": str(row["url"]).strip(),
            }
        )
    return by_uid, report


def _term_is_usable(term: str) -> bool:
    cleaned = " ".join(str(term or "").split()).strip()
    if not cleaned:
        return False
    normalized = _norm(cleaned)
    if len(normalized) < 5:
        return False
    if len(normalized.split()) == 1 and len(normalized) < 7:
        # Short single-token acronyms are often ambiguous in study text.
        return cleaned.isupper() and len(cleaned) >= 4
    return True


def _contains_term(text: str, term: str) -> bool:
    if not text or not term:
        return False
    if term.isupper() and len(term) <= 6:
        return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text))
    return _norm(term) in _norm(text)


def _study_associations(
    cards: dict[str, dict[str, Any]],
    studies: pd.DataFrame,
    cadr_matches: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Find conservative public-study text associations.

    Study text is normalized once per source row and term patterns are compiled
    once per card.  This keeps the 222-card x ~3,600-study pass practical while
    retaining exact acronym-boundary and normalized-phrase semantics.
    """
    output: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    study_records: list[dict[str, str]] = []
    for study in studies[["accession", "name", "description", "Collections"]].itertuples(index=False):
        name = str(study.name).strip()
        description = str(study.description).strip()
        collections = str(study.Collections).strip()
        study_records.append(
            {
                "accession": str(study.accession).strip(),
                "name": name,
                "description": description,
                "collections": collections,
                "name_normalized": _norm(name),
                "description_normalized": _norm(description),
                "collections_normalized": _norm(collections),
            }
        )

    for uid, card in cards.items():
        terms = _identity_names(card)
        cadr_row = cadr_matches.get(uid)
        if cadr_row:
            terms.extend(
                value.strip()
                for value in str(cadr_row.get("match_terms") or "").split(";")
                if value.strip()
            )
            terms.append(str(cadr_row.get("official_name") or "").strip())
        terms = [term for term in dict.fromkeys(terms) if _term_is_usable(term)]
        term_specs: list[tuple[str, str, re.Pattern[str] | None, bool]] = []
        for term in terms:
            normalized_term = _norm(term)
            acronym_pattern = None
            if term.isupper() and len(term) <= 6:
                acronym_pattern = re.compile(
                    rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"
                )
            # Descriptions are long and contain more incidental text than study
            # titles or collection names. Restrict description matching to
            # multiword, reasonably specific phrases; short acronyms remain
            # eligible only in titles and collection fields.
            description_eligible = (
                len(normalized_term) >= 12 and len(normalized_term.split()) >= 2
            )
            term_specs.append(
                (term, normalized_term, acronym_pattern, description_eligible)
            )

        matches: dict[str, dict[str, Any]] = {}
        matched_terms: set[str] = set()
        matched_fields: set[str] = set()
        for study in study_records:
            hits: list[str] = []
            hit_fields: set[str] = set()
            for term, normalized_term, acronym_pattern, description_eligible in term_specs:
                title_match = (
                    bool(acronym_pattern.search(study["name"]))
                    if acronym_pattern is not None
                    else bool(normalized_term and normalized_term in study["name_normalized"])
                )
                collection_match = (
                    bool(acronym_pattern.search(study["collections"]))
                    if acronym_pattern is not None
                    else bool(
                        normalized_term
                        and normalized_term in study["collections_normalized"]
                    )
                )
                description_match = bool(
                    description_eligible
                    and normalized_term
                    and normalized_term in study["description_normalized"]
                )
                if title_match or collection_match or description_match:
                    hits.append(term)
                    if title_match:
                        hit_fields.add("name")
                    if collection_match:
                        hit_fields.add("collections")
                    if description_match:
                        hit_fields.add("description")
            if not hits:
                continue
            accession = study["accession"]
            matches[accession] = {
                "accession": accession,
                "name": study["name"],
                "collections": study["collections"],
                "match_terms": sorted(set(hits), key=str.casefold),
                "match_fields": sorted(hit_fields),
            }
            matched_terms.update(hits)
            matched_fields.update(hit_fields)
        ordered = sorted(matches.values(), key=lambda item: item["accession"])
        output[uid] = {
            "count": len(ordered),
            "accessions": [item["accession"] for item in ordered[:25]],
            "study_names": [item["name"] for item in ordered[:10]],
            "match_terms": sorted(matched_terms, key=str.casefold),
            "match_fields": sorted(matched_fields),
        }
        card_name = (_identity_names(card) or [""])[0]
        for item in ordered:
            rows.append(
                {
                    "repo_uid": uid,
                    "card_name": card_name,
                    **item,
                    "match_terms": "; ".join(item["match_terms"]),
                    "match_fields": "; ".join(item["match_fields"]),
                }
            )
    return output, rows


def _load_curated(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file() or path.stat().st_size == 0:
        return {}
    frame = _read_csv(path)
    missing = REQUIRED_CURATED_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"Curated dbGaP EDS file lacks columns: {sorted(missing)}")
    result: dict[str, dict[str, Any]] = {}
    for row in frame.to_dict("records"):
        uid = str(row.get("repo_uid") or "").strip()
        if not uid or uid.startswith("#"):
            continue
        if uid in result:
            raise ValueError(f"Duplicate curated dbGaP EDS row for {uid}")
        status = str(row.get("status") or "").strip()
        if status not in ALLOWED_CURATED_EDS_STATUSES:
            raise ValueError(
                f"Invalid curated dbGaP EDS status for {uid}: {status!r}; "
                f"expected one of {sorted(ALLOWED_CURATED_EDS_STATUSES)}"
            )
        if not str(row.get("source_url") or "").strip():
            raise ValueError(f"Curated dbGaP EDS row for {uid} lacks source_url")
        if not str(row.get("source_as_of") or "").strip():
            raise ValueError(f"Curated dbGaP EDS row for {uid} lacks source_as_of")
        result[uid] = row
    return result


def run(
    *,
    cadr_snapshot: Path,
    dbgap_studies: Path,
    dbgap_definition: Path,
    curated_eds: Path,
    apply: bool,
) -> tuple[bool, dict[str, Any], Path]:
    run_dir = Path(new_run_dir("build_policy_context"))
    cards = all_cards()
    cadr = _read_csv(cadr_snapshot)
    studies = _read_csv(dbgap_studies)
    definition = _load_dbgap_definition(dbgap_definition)
    definition_urls = [str(item).strip() for item in definition["source_urls"]]
    definition_sha = sha256_file(dbgap_definition)
    definition_as_of = str(definition.get("verified_at") or "").strip()
    curated = _load_curated(curated_eds)

    missing_cadr = REQUIRED_CADR_COLUMNS - set(cadr.columns)
    missing_study = REQUIRED_STUDY_COLUMNS - set(studies.columns)
    if missing_cadr:
        raise ValueError(f"NIH CADR snapshot lacks columns: {sorted(missing_cadr)}")
    if missing_study:
        raise ValueError(f"dbGaP study snapshot lacks columns: {sorted(missing_study)}")

    cadr_sha = sha256_file(cadr_snapshot)
    studies_sha = sha256_file(dbgap_studies)
    curated_sha = sha256_file(curated_eds) if curated_eds.is_file() else None
    source_as_of = max(
        [str(value).strip() for value in cadr["source_page_last_updated"] if str(value).strip()]
        or [""]
    )
    release_dates = pd.to_datetime(studies.get("Release Date", pd.Series(dtype=str)), errors="coerce")
    study_as_of = (
        release_dates.max().date().isoformat()
        if not release_dates.empty and pd.notna(release_dates.max())
        else ""
    )

    cadr_by_uid, cadr_report = _cadr_matches(cards, cadr)
    study_by_uid, study_rows = _study_associations(cards, studies, cadr_by_uid)

    blocks: dict[str, dict[str, Any]] = {}
    changed: list[str] = []
    proposal_rows: list[dict[str, Any]] = []
    for uid, card in cards.items():
        cadr_row = cadr_by_uid.get(uid)
        if cadr_row:
            is_genomic = _parse_bool(cadr_row.get("nih_designated_genomic_data_repository"))
            cadr_block = {
                "status": "listed",
                "listed_name": str(cadr_row.get("official_name") or "").strip(),
                "access_system": str(cadr_row.get("access_system") or "").strip(),
                "nih_designated_genomic_data_repository": is_genomic,
                "url": str(cadr_row.get("url") or "").strip(),
                "source_url": str(cadr_row.get("source_url") or "").strip(),
                "source_page_last_updated": str(
                    cadr_row.get("source_page_last_updated") or ""
                ).strip(),
                "source_sha256": cadr_sha or "",
                "match_basis": str(cadr_row.get("match_basis") or "").strip(),
            }
            is_dbgap_itself = (
                uid == "R207"
                or _norm(cadr_block["listed_name"])
                == _norm("Database of Genotypes and Phenotypes (dbGaP)")
            )
            if is_dbgap_itself:
                eds_block = {
                    "status": "not_applicable_dbgap_repository",
                    "listed_name": cadr_block["listed_name"],
                    "uses_dbgap_access_system": True,
                    "specific_registration_established": None,
                    "evidence_basis": "dbgap_repository_is_not_external_to_itself",
                    "source_url": cadr_block["source_url"],
                    "definition_source_urls": definition_urls,
                    "source_as_of": max(cadr_block["source_page_last_updated"], definition_as_of),
                    "note": (
                        "dbGaP is an NIH CADR, but the dbGaP submission guide defines an "
                        "External Data Source as a non-dbGaP entity."
                    ),
                }
            else:
                eds_block = {
                    "status": "considered_eds_via_cadr_definition",
                    "listed_name": cadr_block["listed_name"],
                    "uses_dbgap_access_system": "dbgap access system" in cadr_block[
                        "access_system"
                    ].casefold(),
                    "specific_registration_established": None,
                    "evidence_basis": "official_nih_cadr_listing_and_dbgap_definition",
                    "source_url": cadr_block["source_url"],
                    "definition_source_urls": definition_urls,
                    "source_as_of": max(cadr_block["source_page_last_updated"], definition_as_of),
                    "note": (
                        "The dbGaP submission guide describes a non-dbGaP External Data "
                        "Source that meets NIH standards as an NIH CADR. This establishes "
                        "EDS class membership, not use by a particular dbGaP study."
                    ),
                }
        else:
            cadr_block = {
                "status": "not_listed",
                "listed_name": "",
                "access_system": "",
                "nih_designated_genomic_data_repository": None,
                "url": "",
                "source_url": str(cadr["source_url"].iloc[0]).strip() if len(cadr) else "",
                "source_page_last_updated": source_as_of,
                "source_sha256": cadr_sha or "",
                "match_basis": "not_in_snapshot",
            }
            eds_block = {
                "status": "not_established",
                "listed_name": "",
                "uses_dbgap_access_system": None,
                "specific_registration_established": None,
                "evidence_basis": "no_authoritative_local_record",
                "source_url": "",
                "definition_source_urls": definition_urls,
                "source_as_of": max(source_as_of, definition_as_of),
                "note": (
                    "Absence from the NIH CADR snapshot does not prove that a resource "
                    "is not a dbGaP External Data Source or Trusted Partner."
                ),
            }

        if uid in curated:
            row = curated[uid]
            eds_block = {
                "status": str(row.get("status") or "registered_eds").strip(),
                "listed_name": str(row.get("listed_name") or "").strip(),
                "uses_dbgap_access_system": _parse_bool(
                    row.get("uses_dbgap_access_system")
                ),
                "specific_registration_established": True,
                "evidence_basis": "curated_authoritative_source",
                "source_url": str(row.get("source_url") or "").strip(),
                "definition_source_urls": definition_urls,
                "source_as_of": str(row.get("source_as_of") or "").strip(),
                "note": str(row.get("note") or "").strip(),
            }

        study = study_by_uid.get(
            uid,
            {
                "count": 0,
                "accessions": [],
                "study_names": [],
                "match_terms": [],
                "match_fields": [],
            },
        )
        block = normalize_policy_context(
            {
                "_meta": {
                    "block_version": "1.0",
                    "as_of": max(source_as_of, study_as_of, definition_as_of),
                    "tier": "policy",
                    "sources": [
                        f"nih_cadr_snapshot:{cadr_sha}",
                        f"dbgap_studies_snapshot:{studies_sha}",
                        f"dbgap_eds_definition:{definition_sha}",
                        *([f"curated_dbgap_eds:{curated_sha}"] if uid in curated and curated_sha else []),
                    ],
                },
                "nih_cadr": cadr_block,
                "dbgap_external_data_source": eds_block,
                "dbgap_public_studies": {
                    "association_status": (
                        "observed_in_public_study_export" if study["count"] else "not_observed"
                    ),
                    "study_count": study["count"],
                    "accessions": study["accessions"],
                    "study_names": study["study_names"],
                    "match_terms": study["match_terms"],
                    "match_fields": study["match_fields"],
                    "source_file": dbgap_studies.name,
                    "source_sha256": studies_sha or "",
                    "source_as_of": study_as_of,
                    "limitation": (
                        "Public study-title, description, or collection text does not by itself establish "
                        "dbGaP External Data Source registration."
                    ),
                },
            }
        )
        blocks[uid] = block
        current = normalize_policy_context((card.get("blocks") or {}).get("policy_context"))
        is_changed = current != block
        if is_changed:
            changed.append(uid)
        summary = policy_context_summary(block)
        proposal_rows.append(
            {
                "repo_uid": uid,
                "name": (_identity_names(card) or [""])[0],
                **summary,
                "changed": str(is_changed).lower(),
            }
        )

    cadr_report_frame = pd.DataFrame(cadr_report)
    study_frame = pd.DataFrame(study_rows)
    proposal_frame = pd.DataFrame(proposal_rows)
    cadr_report_frame.to_csv(run_dir / "nih_cadr_match_report.csv", index=False)
    study_frame.to_csv(run_dir / "dbgap_public_study_associations.csv", index=False)
    proposal_frame.to_csv(run_dir / "policy_context_proposal.csv", index=False)
    (run_dir / "policy_context_blocks.yaml").write_text(
        yaml.safe_dump(blocks, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    conflict_count = int((cadr_report_frame.get("status", pd.Series(dtype=str)) == "CONFLICT").sum())
    ambiguous_count = int((cadr_report_frame.get("status", pd.Series(dtype=str)) == "AMBIGUOUS").sum())
    invalid_curated = sorted(set(curated) - set(cards))
    checks = {
        "cards loaded": (len(cards) > 0, f"{len(cards)} cards"),
        "NIH CADR snapshot loaded": (len(cadr) > 0, f"{len(cadr)} official rows"),
        "dbGaP study snapshot loaded": (len(studies) > 0, f"{len(studies)} studies"),
        "dbGaP EDS definition loaded": (bool(definition_urls), f"{len(definition_urls)} official source URL(s)"),
        "CADR card assignments conflict-free": (conflict_count == 0, f"{conflict_count} conflicts"),
        "curated EDS UIDs active": (not invalid_curated, ", ".join(invalid_curated) or "all active"),
        "policy blocks normalized": (len(blocks) == len(cards), f"{len(blocks)} blocks"),
    }
    ok = gate(checks)

    if apply and ok and changed:
        with card_transaction("build_policy_context"):
            locked = {uid: load_card_for_update(uid, cards[uid]) for uid in changed}
            for uid in changed:
                next_card = copy.deepcopy(locked[uid])
                next_card.setdefault("blocks", {})["policy_context"] = blocks[uid]
                save_card(uid, next_card)
        print(f"\n  Applied {len(changed)} policy-context update(s) transactionally.")
    elif apply and not ok:
        print("\n  Gate failed; no card was changed.")
    elif apply:
        print("\n  Apply requested; valid no-op because no policy-context block differs.")
    else:
        print("\n  DRY RUN - inspect policy_context_proposal.csv and source-bound reports.")

    coverage = {
        "cards": len(cards),
        "nih_cadr_listed": sum(1 for block in blocks.values() if block["nih_cadr"]["status"] == "listed"),
        "dbgap_eds_class_or_registered": sum(
            1
            for block in blocks.values()
            if block["dbgap_external_data_source"]["status"]
            in {"considered_eds_via_cadr_definition", "registered_eds"}
        ),
        "dbgap_registered_eds": sum(
            1
            for block in blocks.values()
            if block["dbgap_external_data_source"]["status"] == "registered_eds"
        ),
        "dbgap_access_system": sum(1 for block in blocks.values() if block["dbgap_external_data_source"]["uses_dbgap_access_system"] is True),
        "public_study_association": sum(1 for block in blocks.values() if block["dbgap_public_studies"]["study_count"] > 0),
        "public_study_association_rows": len(study_frame),
        "unmatched_cadr_rows": int((cadr_report_frame.get("status", pd.Series(dtype=str)) == "UNMATCHED").sum()),
        "ambiguous_cadr_rows": ambiguous_count,
        "changed": len(changed),
        "applied": len(changed) if apply and ok else 0,
    }
    atomic_write_json(run_dir / "coverage.json", coverage)
    atomic_write_json(
        run_dir / "source_snapshot.json",
        {
            "schema": "nih-tracker-policy-context-sources/v1",
            "created_at": _now(),
            "nih_cadr": {
                "path": str(cadr_snapshot.resolve()),
                "sha256": cadr_sha,
                "rows": len(cadr),
                "source_page_last_updated": source_as_of,
            },
            "dbgap_studies": {
                "path": str(dbgap_studies.resolve()),
                "sha256": studies_sha,
                "rows": len(studies),
                "latest_release_date": study_as_of,
            },
            "dbgap_eds_definition": {
                "path": str(dbgap_definition.resolve()),
                "sha256": definition_sha,
                "verified_at": definition_as_of,
                "source_urls": definition_urls,
            },
            "curated_eds": {
                "path": str(curated_eds.resolve()),
                "sha256": curated_sha,
                "rows": len(curated),
            },
        },
    )
    ledger_append(
        "build_policy_context",
        params={
            "apply": apply,
            "curated_eds": curated_eds.name,
            "dbgap_definition": dbgap_definition.name,
        },
        inputs={
            "nih_cadr_snapshot": cadr_sha,
            "dbgap_studies": studies_sha,
            "dbgap_eds_definition": definition_sha,
            "curated_eds": curated_sha,
        },
        outputs=coverage,
        gate_passed=ok,
        run_dir=str(run_dir),
    )
    print("\n  POLICY CONTEXT COVERAGE")
    for key, value in coverage.items():
        print(f"  {key:34s} {value}")
    print(f"\nPolicy-context run: {run_dir}")
    return ok, coverage, run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cadr-snapshot", default=DEFAULT_CADR_SNAPSHOT)
    parser.add_argument("--dbgap-studies", default=DEFAULT_DBGAP_STUDIES)
    parser.add_argument("--dbgap-definition", default=DEFAULT_DBGAP_DEFINITION)
    parser.add_argument("--curated-eds", default=DEFAULT_CURATED_EDS)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    ok, _, _ = run(
        cadr_snapshot=Path(args.cadr_snapshot).expanduser(),
        dbgap_studies=Path(args.dbgap_studies).expanduser(),
        dbgap_definition=Path(args.dbgap_definition).expanduser(),
        curated_eds=Path(args.curated_eds).expanduser(),
        apply=args.apply,
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
