"""Normalize and enrich card API, standards, PID, and GUID evidence.

The step is evidence-preserving. It normalizes legacy interoperability blocks,
adds exact probe evidence, incorporates confirmed re3data record metadata, and
adds a small set of resource-specific GUID facts backed by official sources.
Dry run is the default; --apply writes changed cards in one card transaction.
"""
from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yaml

from common import (
    ENV,
    INP,
    PROJECT,
    RE3DATA_CACHE_READ_ONLY,
    SHARED_CACHE_ROOT,
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
from interoperability_schema import (
    canonical_api_family,
    canonical_standard,
    normalize_endpoint,
    normalize_guid_system,
    normalize_interoperability,
    normalize_link,
    normalize_pid_system,
    parse_re3data_record,
    safe_url,
    truthy,
)

CACHE_SCHEMA = "nih-tracker-re3data-interoperability-cache/v2"
DEFAULT_CACHE = str(Path(SHARED_CACHE_ROOT) / "re3data_interoperability_cache.json")
JSON_COLUMNS = {
    "api_endpoints_json": "api_endpoints",
    "documentation_links_json": "documentation_links",
    "standards_json": "standards",
    "pid_systems_json": "pid_systems",
    "guid_systems_json": "guid_systems",
    "relevant_links_json": "relevant_links",
}
GAP_COLUMNS = [
    "repo_uid",
    "name",
    "gap_type",
    "status",
    "r3d_id",
    "detail",
    "recommended_action",
]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _month() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")


def _read_csv(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str).fillna("")
    frame.columns = [str(column).strip() for column in frame.columns]
    return frame


def _card_csv_id(uid: str, card: dict[str, Any]) -> str:
    for link in card.get("links", []) or []:
        if str(link.get("id_type") or "").strip() == "csv_id":
            value = str(link.get("id_value") or "").strip()
            if value:
                return value
    match = re.fullmatch(r"R(\d+)", str(uid))
    return match.group(1) if match else ""


def _values(row: pd.Series, aliases: tuple[str, ...]) -> list[str]:
    lookup = {str(column).strip().lower(): str(value or "").strip() for column, value in row.items()}
    return [lookup[name] for name in aliases if lookup.get(name)]


def _first(row: pd.Series, aliases: tuple[str, ...]) -> str:
    values = _values(row, aliases)
    return values[0] if values else ""


def _probe_evidence(group: pd.DataFrame) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "api_endpoints": [],
        "documentation_links": [],
        "standards": [],
        "pid_systems": [],
        "guid_systems": [],
        "relevant_links": [],
        "families": [],
        "sources": ["probe:v4"],
    }
    states: list[str] = []
    access_values: list[str] = []
    verified = False
    declared = False
    gated = False
    standard_names = {
        "openapi": "OpenAPI Specification",
        "swagger": "OpenAPI Specification",
        "graphql": "GraphQL Specification",
        "sparql": "SPARQL 1.1",
        "oai_pmh": "OAI-PMH",
        "oaipmh": "OAI-PMH",
        "fhir": "HL7 FHIR",
        "drs": "GA4GH DRS",
        "wes": "GA4GH WES",
        "trs": "GA4GH TRS",
        "beacon": "GA4GH Beacon",
    }
    for _, row in group.iterrows():
        state = _first(row, ("api_evidence_state", "evidence_state"))
        if state:
            states.append(state)
        verified = verified or truthy(_first(row, ("api_verified", "verified_api", "verified")))
        declared = declared or truthy(_first(row, ("api_declared", "declared_api", "declared")))
        gated = gated or truthy(_first(row, ("gated_reachable", "api_gated", "gated")))
        access = _first(row, ("access_class", "api_access_class", "endpoint_access_class"))
        if access:
            access_values.append(access)

        for column, raw_value in row.items():
            value = str(raw_value or "").strip()
            if not value:
                continue
            key = re.sub(r"[^a-z0-9]+", "_", str(column).strip().lower()).strip("_")
            url = safe_url(value)
            if url:
                documentation_like = any(
                    token in key for token in ("documentation", "docs", "swagger", "openapi", "schema")
                )
                if documentation_like:
                    link = normalize_link(
                        {
                            "label": str(column).replace("_", " "),
                            "type": "api_documentation",
                            "url": url,
                            "source": "probe:v4",
                            "tier": "verified" if verified else "declared",
                            "as_of": _month(),
                        }
                    )
                    if link:
                        evidence["documentation_links"].append(link)
                endpoint_like = any(
                    token in key
                    for token in ("api", "endpoint", "graphql", "sparql", "oai", "fhir", "drs", "wes", "trs", "beacon")
                )
                if endpoint_like and (not documentation_like or "endpoint" in key):
                    family_hint = "" if key in {"api", "api_url", "api_endpoint", "endpoint", "endpoint_url"} else key
                    endpoint = normalize_endpoint(
                        {
                            "url": url,
                            "family": family_hint,
                            "declared": declared or not verified,
                            "verified": verified,
                            "source": "probe:v4",
                            "tier": "verified" if verified else "declared",
                            "access_class": access or "not_documented",
                            "as_of": _month(),
                        }
                    )
                    if endpoint:
                        evidence["api_endpoints"].append(endpoint)
                        if endpoint.get("family"):
                            evidence["families"].append(endpoint["family"])
                continue

            if key in {"api_family", "api_families", "api_type", "api_types"}:
                for item in re.split(r"[;|,]", value):
                    family = canonical_api_family(item)
                    if family:
                        evidence["families"].append(family)
            if ("standard" in key or "protocol" in key) and value.strip().lower() not in {
                "1", "0", "true", "false", "yes", "no", "y", "n", "on", "off"
            }:
                for item in re.split(r"[;|]", value):
                    standard = canonical_standard(
                        {
                            "name": item.strip(),
                            "declared": declared or not verified,
                            "verified": verified,
                            "source": "probe:v4",
                            "tier": "verified" if verified else "declared",
                            "as_of": _month(),
                        }
                    )
                    if standard:
                        evidence["standards"].append(standard)
            if truthy(value):
                for token, standard_name in standard_names.items():
                    if token in key:
                        standard = canonical_standard(
                            {
                                "name": standard_name,
                                "declared": declared or not verified,
                                "verified": verified,
                                "source": "probe:v4",
                                "tier": "verified" if verified else "declared",
                                "as_of": _month(),
                            }
                        )
                        if standard:
                            evidence["standards"].append(standard)
                        family = canonical_api_family(token)
                        if family:
                            evidence["families"].append(family)

    rank = {
        "verified_documented": 5,
        "verified_undocumented": 4,
        "gated_reachable": 3,
        "declared_unverified": 2,
        "absent": 1,
    }
    evidence["api_evidence_state"] = max(states, key=lambda value: rank.get(value, 0)) if states else ""
    evidence["api_verified"] = verified
    evidence["api_declared"] = declared
    evidence["gated_reachable"] = gated
    evidence["access_class"] = access_values[0] if access_values else ""
    return evidence


def _parse_json_cell(value: str, *, field: str, repo_uid: str) -> list[Any]:
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{repo_uid} {field} is not valid JSON: {exc}") from exc
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    raise ValueError(f"{repo_uid} {field} must decode to a list or object")


def _load_curated(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file() or path.stat().st_size == 0:
        return {}
    frame = _read_csv(path)
    if "repo_uid" not in frame.columns:
        raise ValueError(f"Curated interoperability file lacks repo_uid: {path}")
    result: dict[str, dict[str, Any]] = {}
    for _, row in frame.iterrows():
        uid = str(row.get("repo_uid") or "").strip()
        if not uid:
            continue
        if uid in result:
            raise ValueError(f"Duplicate repo_uid in curated interoperability file: {uid}")
        record: dict[str, Any] = {
            "sources": [str(row.get("source") or "human_curated").strip()],
            "as_of": str(row.get("as_of") or _month()).strip(),
            "api_evidence_state": str(row.get("api_evidence_state") or "").strip(),
            "api_verified": truthy(row.get("api_verified")),
            "api_declared": truthy(row.get("api_declared")),
            "gated_reachable": truthy(row.get("gated_reachable")),
            "access_class": str(row.get("access_class") or "").strip(),
            "families": [],
        }
        for column, target in JSON_COLUMNS.items():
            record[target] = _parse_json_cell(str(row.get(column) or ""), field=column, repo_uid=uid)
        families = str(row.get("api_families") or "")
        record["families"] = [
            family for family in (canonical_api_family(item) for item in re.split(r"[;|,]", families)) if family
        ]
        result[uid] = record
    return result


def _load_cache(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema": CACHE_SCHEMA, "records": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": CACHE_SCHEMA, "records": {}}
    if not isinstance(value, dict) or not isinstance(value.get("records"), dict):
        return {"schema": CACHE_SCHEMA, "records": {}}
    return value


def _cache_needs_profile_upgrade(record: dict[str, Any] | None) -> bool:
    if not isinstance(record, dict):
        return True
    return str(record.get("profile_schema_version") or "") != "1.0"


def _cache_age_days(record: dict[str, Any]) -> float:
    try:
        fetched = dt.datetime.fromisoformat(str(record.get("fetched_at") or "").replace("Z", "+00:00"))
        return (dt.datetime.now(dt.timezone.utc) - fetched.astimezone(dt.timezone.utc)).total_seconds() / 86400
    except (ValueError, TypeError):
        return 1e9


def _fetch_re3data(r3d_id: str, session: requests.Session) -> tuple[dict[str, Any] | None, str]:
    endpoints = [
        f"https://www.re3data.org/api/v40/repository/{r3d_id}",
        f"https://www.re3data.org/api/v1/repository/{r3d_id}",
    ]
    errors = []
    for endpoint in endpoints:
        try:
            response = session.get(endpoint, headers={"Accept": "application/xml"}, timeout=35)
        except requests.RequestException as exc:
            errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")
            continue
        if response.ok:
            try:
                parsed = parse_re3data_record(response.content, r3d_id=r3d_id)
            except Exception as exc:
                errors.append(f"{endpoint}: parse {type(exc).__name__}: {exc}")
                continue
            parsed["record_url"] = f"https://www.re3data.org/repository/{r3d_id}"
            parsed["profile_schema_version"] = "1.0"
            parsed["api_endpoint"] = endpoint
            parsed["http_status"] = response.status_code
            parsed["content_type"] = response.headers.get("content-type", "")
            parsed["response_sha256"] = hashlib.sha256(response.content).hexdigest()
            parsed["raw_xml_base64"] = base64.b64encode(response.content).decode("ascii")
            parsed["fetched_at"] = _now()
            return parsed, ""
        errors.append(f"{endpoint}: HTTP {response.status_code}")
    return None, "; ".join(errors)


def _confirmed_re3data_ids(card: dict[str, Any]) -> list[str]:
    ids = []
    for link in card.get("links", []) or []:
        if str(link.get("id_type") or "").strip() != "re3data":
            continue
        value = str(link.get("id_value") or "").strip().lower()
        if re.fullmatch(r"r3d\d+", value):
            ids.append(value)
    return sorted(set(ids))


def _merge_evidence(block: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    work = normalize_interoperability(block)
    meta = work.setdefault("_meta", {})
    meta["as_of"] = max(str(meta.get("as_of") or ""), str(evidence.get("as_of") or _month()))
    meta["review_state"] = str(meta.get("review_state") or "auto")
    sources = list(meta.get("sources") or []) + list(evidence.get("sources") or [])
    meta["sources"] = sorted({str(value).strip() for value in sources if str(value).strip()})

    api = work.setdefault("api", {"families": [], "endpoints": [], "documentation_links": []})
    api["families"] = list(api.get("families") or []) + list(evidence.get("families") or [])
    api["endpoints"] = list(api.get("endpoints") or []) + list(evidence.get("api_endpoints") or [])
    api["documentation_links"] = list(api.get("documentation_links") or []) + list(evidence.get("documentation_links") or [])
    work["standards"] = list(work.get("standards") or []) + list(evidence.get("standards") or [])
    work["pid_systems"] = list(work.get("pid_systems") or []) + list(evidence.get("pid_systems") or [])
    work["guid_systems"] = list(work.get("guid_systems") or []) + list(evidence.get("guid_systems") or [])
    work["relevant_links"] = list(work.get("relevant_links") or []) + list(evidence.get("relevant_links") or [])

    for field in ("api_verified", "api_declared", "gated_reachable"):
        work[field] = truthy(work.get(field)) or truthy(evidence.get(field))
    for field in ("api_evidence_state", "access_class"):
        if evidence.get(field):
            work[field] = evidence[field]
    return work


def _normalize_evidence(evidence: dict[str, Any], *, source: str, tier: str) -> dict[str, Any]:
    result = copy.deepcopy(evidence)
    result["api_endpoints"] = [
        item for item in (normalize_endpoint(value, source=source, tier=tier) for value in result.get("api_endpoints", [])) if item
    ]
    result["documentation_links"] = [
        item for item in (normalize_link(value, source=source, tier=tier) for value in result.get("documentation_links", [])) if item
    ]
    result["standards"] = [
        item for item in (canonical_standard(value, source=source, tier=tier) for value in result.get("standards", [])) if item
    ]
    result["pid_systems"] = [
        item for item in (normalize_pid_system(value, source=source, tier=tier) for value in result.get("pid_systems", [])) if item
    ]
    result["guid_systems"] = [
        item for item in (normalize_guid_system(value, source=source, tier=tier) for value in result.get("guid_systems", [])) if item
    ]
    result["relevant_links"] = [
        item for item in (normalize_link(value, source=source, tier=tier) for value in result.get("relevant_links", [])) if item
    ]
    return result


def _coverage(blocks: dict[str, dict[str, Any]]) -> dict[str, int]:
    return {
        "cards": len(blocks),
        "api_documented": sum(bool((block.get("api") or {}).get("endpoints") or (block.get("api") or {}).get("documentation_links")) for block in blocks.values()),
        "api_verified": sum(bool(block.get("api_verified")) for block in blocks.values()),
        "standards_documented": sum(bool(block.get("standards")) for block in blocks.values()),
        "pid_systems_documented": sum(bool(block.get("pid_systems")) for block in blocks.values()),
        "guid_systems_documented": sum(bool(block.get("guid_systems")) for block in blocks.values()),
    }


def run(
    *,
    probe_path: Path,
    curated_path: Path,
    cache_path: Path,
    re3data_policy: str,
    re3data_max_age_days: int,
    apply: bool,
    strict_re3data: bool,
) -> tuple[bool, dict[str, Any], Path]:
    network_allowed = str(os.getenv("NIH_TRACKER_EXTERNAL_NETWORK_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}
    if re3data_policy != "none" and not network_allowed:
        raise RuntimeError("re3data cache refresh requires NIH_TRACKER_EXTERNAL_NETWORK_ENABLED=1; use --re3data-policy none for offline/cached-only runs")
    cards = all_cards()
    if not cards:
        raise RuntimeError("No cards were found")
    probe = _read_csv(probe_path)
    if "csv_id" not in probe.columns:
        raise ValueError(f"Probe file lacks csv_id: {probe_path}")
    curated = _load_curated(curated_path)
    cache_before_sha256 = sha256_file(cache_path) if cache_path.is_file() else None
    cache = _load_cache(cache_path)
    cache_records = cache.setdefault("records", {})
    cache_changed = False
    fetch_errors: list[dict[str, str]] = []

    re3_ids = sorted({r3d_id for card in cards.values() for r3d_id in _confirmed_re3data_ids(card)})
    can_write_cache = not RE3DATA_CACHE_READ_ONLY
    session = requests.Session()
    session.headers.update({"User-Agent": "NIH-Tracker/15 interoperability enrichment"})
    if re3data_policy != "none":
        for index, r3d_id in enumerate(re3_ids, start=1):
            current = cache_records.get(r3d_id) if isinstance(cache_records.get(r3d_id), dict) else None
            should_fetch = (
                re3data_policy == "all"
                or current is None
                or _cache_needs_profile_upgrade(current)
                or (re3data_policy == "stale" and _cache_age_days(current) > re3data_max_age_days)
                or (re3data_policy == "missing" and current is None)
            )
            if not should_fetch or not can_write_cache:
                continue
            parsed, error = _fetch_re3data(r3d_id, session)
            if parsed:
                cache_records[r3d_id] = parsed
                cache_changed = True
            else:
                fetch_errors.append({"r3d_id": r3d_id, "error": error})
            if index < len(re3_ids):
                time.sleep(0.15)
    if cache_changed:
        cache["schema"] = CACHE_SCHEMA
        cache["updated_at"] = _now()
        cache["source_api"] = "re3data API v4.0 with v1 fallback"
        atomic_write_json(cache_path, cache)

    proposals: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    blocks: dict[str, dict[str, Any]] = {}
    changed_cards: dict[str, dict[str, Any]] = {}
    for uid, card in sorted(cards.items()):
        old_block = copy.deepcopy((card.get("blocks") or {}).get("interoperability") or {})
        block = normalize_interoperability(old_block, repo_uid=uid)
        csv_id = _card_csv_id(uid, card)
        if csv_id:
            group = probe.loc[probe["csv_id"].astype(str).str.strip() == str(csv_id)]
            if not group.empty:
                block = _merge_evidence(block, _normalize_evidence(_probe_evidence(group), source="probe:v4", tier="verified"))
        if uid in curated:
            source = str((curated[uid].get("sources") or ["human_curated"])[0])
            block = _merge_evidence(block, _normalize_evidence(curated[uid], source=source, tier="verified"))

        for r3d_id in _confirmed_re3data_ids(card):
            record = cache_records.get(r3d_id)
            if not isinstance(record, dict):
                review_rows.append({
                    "repo_uid": uid,
                    "issue": "re3data_metadata_not_cached",
                    "r3d_id": r3d_id,
                    "detail": "Confirmed re3data link exists but rich metadata is not cached.",
                })
                continue
            evidence = {
                "sources": [f"re3data:{r3d_id}"],
                "as_of": str(record.get("fetched_at") or _month())[:10],
                "api_endpoints": record.get("api_endpoints") or [],
                "standards": record.get("standards") or [],
                "pid_systems": record.get("pid_systems") or [],
                "relevant_links": [
                    {
                        "label": "re3data registry record",
                        "type": "registry_record",
                        "url": record.get("record_url") or f"https://www.re3data.org/repository/{r3d_id}",
                        "source": f"re3data:{r3d_id}",
                        "tier": "registry",
                    }
                ],
            }
            block = _merge_evidence(block, _normalize_evidence(evidence, source=f"re3data:{r3d_id}", tier="registry"))

        block = normalize_interoperability(block, repo_uid=uid)
        blocks[uid] = block
        api_block = block.get("api") or {}
        if block.get("api_evidence_state") != "absent" and not (
            api_block.get("endpoints") or api_block.get("documentation_links")
        ):
            review_rows.append(
                {
                    "repo_uid": uid,
                    "name": str((card.get("identity") or {}).get("name") or ""),
                    "issue": "api_documentation_missing",
                    "detail": "API evidence exists, but no endpoint or documentation URL is recorded.",
                    "recommended_action": "Add evidence to inputs/interoperability_curated.csv or the authoritative probe input.",
                }
            )
        if not block.get("standards"):
            review_rows.append(
                {
                    "repo_uid": uid,
                    "name": str((card.get("identity") or {}).get("name") or ""),
                    "issue": "standards_not_documented",
                    "detail": "No interoperability or metadata standard is recorded.",
                    "recommended_action": "Verify official API/schema documentation before adding a standard.",
                }
            )
        pid_culture = ((card.get("blocks") or {}).get("pid_culture") or {}).get("pid_culture")
        if pid_culture and not block.get("pid_systems"):
            review_rows.append(
                {
                    "repo_uid": uid,
                    "name": str((card.get("identity") or {}).get("name") or ""),
                    "issue": "pid_system_details_missing",
                    "detail": f"The summary PID culture is {pid_culture!r}, but no named PID system is recorded.",
                    "recommended_action": "Record the item-level identifier system and an authoritative source URL.",
                }
            )
        changed = old_block != block
        before = normalize_interoperability(old_block)
        proposals.append({
            "repo_uid": uid,
            "name": str((card.get("identity") or {}).get("name") or ""),
            "changed": changed,
            "api_evidence_state": block.get("api_evidence_state"),
            "api_verified": block.get("api_verified"),
            "api_declared": block.get("api_declared"),
            "api_families": "; ".join((block.get("api") or {}).get("families") or []),
            "api_endpoints_before": len((before.get("api") or {}).get("endpoints") or []),
            "api_endpoints_after": len((block.get("api") or {}).get("endpoints") or []),
            "standards_before": len(before.get("standards") or []),
            "standards_after": len(block.get("standards") or []),
            "pid_systems_after": len(block.get("pid_systems") or []),
            "guid_systems_after": len(block.get("guid_systems") or []),
            "confirmed_re3data_ids": "; ".join(_confirmed_re3data_ids(card)),
        })
        if changed:
            new_card = copy.deepcopy(card)
            new_card.setdefault("blocks", {})["interoperability"] = block
            changed_cards[uid] = new_card

    endpoint_errors = []
    guid_errors = []
    for uid, block in blocks.items():
        for endpoint in (block.get("api") or {}).get("endpoints") or []:
            if not safe_url(endpoint.get("url")):
                endpoint_errors.append(f"{uid}: {endpoint!r}")
        for guid in block.get("guid_systems") or []:
            if not safe_url(guid.get("documentation_url")) and not safe_url(guid.get("tool_url")):
                guid_errors.append(f"{uid}: {guid.get('name')}")

    checks = {
        "cards loaded": (bool(cards), f"{len(cards)} cards"),
        "probe loaded": (not probe.empty, f"{len(probe)} rows"),
        "all output UIDs active": (set(blocks) == set(cards), f"{len(blocks)} blocks"),
        "API endpoint URLs valid": (not endpoint_errors, f"{len(endpoint_errors)} invalid"),
        "GUID evidence is linked": (not guid_errors, f"{len(guid_errors)} missing source URL"),
        "re3data metadata fetch": (
            not strict_re3data or not fetch_errors,
            f"{len(fetch_errors)} fetch error(s); {'blocking' if strict_re3data else 'advisory'}",
        ),
    }
    ok = gate(checks)
    run_dir = Path(new_run_dir("build_interoperability"))
    cache_after_sha256 = sha256_file(cache_path) if cache_path.is_file() else None
    cache_snapshot = {
        "schema": "nih-tracker-re3data-interoperability-run-snapshot/v1",
        "generated_at": _now(),
        "cache_path": str(cache_path),
        "cache_before_sha256": cache_before_sha256,
        "cache_after_sha256": cache_after_sha256,
        "record_ids": re3_ids,
        "records": {
            r3d_id: copy.deepcopy(cache_records[r3d_id])
            for r3d_id in re3_ids
            if isinstance(cache_records.get(r3d_id), dict)
        },
    }
    atomic_write_json(run_dir / "re3data_cache_snapshot.json", cache_snapshot)
    pd.DataFrame(proposals).to_csv(run_dir / "interoperability_proposal.csv", index=False)
    gap_rows = review_rows + [
        {
            "repo_uid": "",
            "name": "",
            "issue": "re3data_fetch_error",
            "r3d_id": row.get("r3d_id", ""),
            "detail": row.get("error", ""),
            "recommended_action": "Retry after checking network/service status; existing card evidence was preserved.",
        }
        for row in fetch_errors
    ]
    normalized_gap_rows = []
    for row in gap_rows:
        item = dict(row)
        item["gap_type"] = str(item.get("issue") or "evidence_gap")
        item["status"] = str(item.get("status") or "open")
        normalized_gap_rows.append(item)
    gap_frame = pd.DataFrame(normalized_gap_rows)
    for column in GAP_COLUMNS:
        if column not in gap_frame.columns:
            gap_frame[column] = ""
    gap_frame = gap_frame[GAP_COLUMNS + [column for column in gap_frame.columns if column not in GAP_COLUMNS]]
    gap_frame.to_csv(run_dir / "interoperability_gaps.csv", index=False)
    (run_dir / "interoperability_blocks.yaml").write_text(
        yaml.safe_dump(blocks, sort_keys=True, allow_unicode=True), encoding="utf-8"
    )
    coverage = _coverage(blocks)
    atomic_write_json(run_dir / "coverage.json", coverage)
    if apply and ok and changed_cards:
        with card_transaction("build_interoperability"):
            locked = {uid: load_card_for_update(uid, cards[uid]) for uid in sorted(changed_cards)}
            for uid in sorted(changed_cards):
                next_card = copy.deepcopy(locked[uid])
                next_card.setdefault("blocks", {})["interoperability"] = blocks[uid]
                save_card(uid, next_card)
        print(f"\n  Applied {len(changed_cards)} interoperability card update(s) transactionally.")
    elif apply and not ok:
        print("\n  Gate failed; no card was changed.")
    elif apply:
        print("\n  Apply requested; valid no-op because no card differs.")
    else:
        print("\n  DRY RUN - inspect interoperability_proposal.csv and interoperability_gaps.csv.")

    outputs = {
        **coverage,
        "changed": len(changed_cards),
        "applied": len(changed_cards) if apply and ok else 0,
        "re3data_confirmed_ids": len(re3_ids),
        "re3data_cached_records": len(cache_records),
        "re3data_fetch_errors": len(fetch_errors),
        "re3data_cache_before_sha256": cache_before_sha256,
        "re3data_cache_after_sha256": cache_after_sha256,
        "re3data_cache_snapshot": str(run_dir / "re3data_cache_snapshot.json"),
        "gap_rows": len(normalized_gap_rows),
    }
    ledger_append(
        "build_interoperability",
        params={
            "probe": probe_path.name,
            "curated": curated_path.name,
            "apply": apply,
            "re3data_policy": re3data_policy,
            "re3data_max_age_days": re3data_max_age_days,
            "strict_re3data": strict_re3data,
        },
        inputs={
            "probe": sha256_file(probe_path),
            "curated": sha256_file(curated_path) if curated_path.is_file() else None,
            "re3data_cache_before": cache_before_sha256,
        },
        outputs=outputs,
        gate_passed=ok,
        run_dir=str(run_dir),
    )
    print("\n  COVERAGE")
    for key, value in coverage.items():
        print(f"  {key:28s} {value}")
    print(f"  changed cards                {len(changed_cards)}")
    print(f"  re3data cache errors         {len(fetch_errors)}")
    print(f"\nInteroperability run: {run_dir}")
    return ok, outputs, run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", default="repo_api_results_v4_230_strict_endpoints.csv")
    parser.add_argument("--curated", default="interoperability_curated.csv")
    parser.add_argument("--re3data-cache", default=DEFAULT_CACHE)
    parser.add_argument(
        "--re3data-policy",
        choices=("none", "missing", "stale", "all"),
        default="missing" if ENV == "prod" else "none",
        help="Cache refresh policy. Nonproduction defaults to none/read-only.",
    )
    parser.add_argument("--re3data-max-age-days", type=int, default=30)
    parser.add_argument("--strict-re3data", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    ok, _, _ = run(
        probe_path=Path(INP) / args.probe,
        curated_path=Path(INP) / args.curated,
        cache_path=Path(args.re3data_cache).expanduser(),
        re3data_policy=args.re3data_policy,
        re3data_max_age_days=max(1, args.re3data_max_age_days),
        apply=args.apply,
        strict_re3data=args.strict_re3data,
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
