"""Build descriptive resource-profile blocks from exact, source-bound evidence.

Sources are used in descending evidentiary specificity:
  * human-curated CSV rows,
  * structured metadata from the resource homepage,
  * exact FAIRsharing records linked by DOI,
  * exact re3data records linked by r3d identifier,
  * the local FAIRsharing export when richer API metadata is unavailable.

The step does not use lexical search to associate a registry record with a card.
It also does not infer physical data residency from an institution name. Dry run
is the default; --apply writes resource_profile and supported interoperability
updates in one card transaction.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests
import yaml

from common import (
    ENV,
    INP,
    RESOURCE_PROFILE_CACHE_READ_ONLY,
    SHARED_CACHE_ROOT,
    all_cards,
    atomic_write_bytes,
    atomic_write_json,
    card_path,
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
    normalize_interoperability,
    normalize_link as normalize_interop_link,
)
from resource_profile_schema import (
    DESCRIPTION_MAX_CHARS,
    merge_resource_profile,
    normalize_resource_profile,
    parse_fairsharing_record,
    parse_re3data_profile,
    parse_website_metadata,
    resource_profile_summary,
    safe_url,
)

FAIR_CACHE_SCHEMA = "nih-tracker-fairsharing-resource-profile-cache/v1"
WEBSITE_CACHE_SCHEMA = "nih-tracker-website-resource-profile-cache/v1"
DEFAULT_FAIR_CACHE = str(Path(SHARED_CACHE_ROOT) / "fairsharing_resource_profile_cache.json")
DEFAULT_WEBSITE_CACHE = str(Path(SHARED_CACHE_ROOT) / "website_resource_profile_cache.json")
DEFAULT_RE3DATA_CACHE = str(Path(SHARED_CACHE_ROOT) / "re3data_interoperability_cache.json")
DEFAULT_SNAPSHOT_ROOT = str(Path(SHARED_CACHE_ROOT) / "resource_profile_source_snapshots")

AUTO_SOURCE_PREFIXES = (
    "website:",
    "fairsharing:",
    "fairsharing_export:",
    "re3data:",
)
PROFILE_JSON_COLUMNS = {
    "resource_types_json": "resource_types",
    "data_asset_types_json": "data_asset_types",
    "descriptions_json": "descriptions",
    "organizations_json": "organizations",
    "subject_areas_json": "subject_areas",
    "data_formats_json": "data_formats",
    "taxonomies_json": "taxonomies",
    "keywords_json": "keywords",
    "languages_json": "languages",
    "service_roles_json": "service_roles",
    "access_conditions_json": "access_conditions",
    "relevant_links_json": "relevant_links",
}
INTEROP_JSON_COLUMNS = {
    "standards_json": "standards",
    "api_documentation_links_json": "api_documentation_links",
}
GAP_COLUMNS = [
    "repo_uid",
    "name",
    "gap_type",
    "status",
    "source",
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


def _load_cache(path: Path, schema: str) -> dict[str, Any]:
    if not path.is_file():
        return {"schema": schema, "records": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": schema, "records": {}}
    if not isinstance(value, dict) or not isinstance(value.get("records"), dict):
        return {"schema": schema, "records": {}}
    value.setdefault("schema", schema)
    return value


def _cache_age_days(record: dict[str, Any]) -> float:
    try:
        fetched = dt.datetime.fromisoformat(
            str(record.get("fetched_at") or "").replace("Z", "+00:00")
        )
        return (
            dt.datetime.now(dt.timezone.utc) - fetched.astimezone(dt.timezone.utc)
        ).total_seconds() / 86400
    except (ValueError, TypeError):
        return 1e9


def _should_fetch(
    current: dict[str, Any] | None,
    *,
    policy: str,
    max_age_days: int,
) -> bool:
    if policy == "none":
        return False
    if policy == "all":
        return True
    if policy == "missing":
        return current is None
    return current is None or _cache_age_days(current) > max_age_days


def _snapshot_bytes(
    data: bytes,
    *,
    snapshot_root: Path,
    suffix: str,
) -> tuple[str, str]:
    digest = hashlib.sha256(data).hexdigest()
    destination = snapshot_root / digest[:2] / f"{digest}.{suffix}.gz"
    if not destination.is_file():
        compressed = gzip.compress(data, compresslevel=9, mtime=0)
        atomic_write_bytes(destination, compressed)
    return digest, str(destination)


def _linked_values(card: dict[str, Any], id_type: str) -> list[str]:
    values = []
    for link in card.get("links", []) or []:
        if str(link.get("id_type") or "").strip() != id_type:
            continue
        value = str(link.get("id_value") or "").strip()
        if value:
            values.append(value)
    return sorted(set(values), key=str.casefold)


def _linked_fairsharing_dois(card: dict[str, Any]) -> list[str]:
    return [
        value
        for value in _linked_values(card, "fairsharing_doi")
        if value.lower().startswith("10.25504/fairsharing.")
    ]


def _linked_re3data_ids(card: dict[str, Any]) -> list[str]:
    return [
        value.lower()
        for value in _linked_values(card, "re3data")
        if re.fullmatch(r"r3d\d+", value.lower())
    ]


def _json_cell(value: Any, *, field: str, repo_uid: str) -> list[Any]:
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
        raise ValueError(f"Curated resource-profile file lacks repo_uid: {path}")
    result: dict[str, dict[str, Any]] = {}
    for _, row in frame.iterrows():
        uid = str(row.get("repo_uid") or "").strip()
        if not uid:
            continue
        if uid in result:
            raise ValueError(f"Duplicate repo_uid in curated resource-profile file: {uid}")
        source = str(row.get("source") or "human_curated").strip()
        as_of = str(row.get("as_of") or _month()).strip()
        profile: dict[str, Any] = {
            "_meta": {
                "as_of": as_of,
                "tier": "curated",
                "review_state": "human_confirmed",
                "sources": [source],
            }
        }
        for column, target in PROFILE_JSON_COLUMNS.items():
            profile[target] = _json_cell(
                row.get(column), field=column, repo_uid=uid
            )
        maintainer_status = str(row.get("maintainer_status") or "").strip().lower()
        if maintainer_status in {"true", "yes", "1", "false", "no", "0"}:
            profile["maintainer_status"] = {
                "value": maintainer_status in {"true", "yes", "1"},
                "source": source,
                "tier": "curated",
                "as_of": as_of,
            }
        standards = _json_cell(
            row.get("standards_json"), field="standards_json", repo_uid=uid
        )
        docs = _json_cell(
            row.get("api_documentation_links_json"),
            field="api_documentation_links_json",
            repo_uid=uid,
        )
        families = [
            family
            for family in (
                canonical_api_family(item)
                for item in re.split(
                    r"[;|,]", str(row.get("api_families") or "")
                )
            )
            if family
        ]
        result[uid] = {
            "profile": normalize_resource_profile(profile),
            "standards": standards,
            "api_documentation_links": docs,
            "api_families": families,
            "source": source,
            "as_of": as_of,
        }
    return result


def _preserve_nonautomatic_profile(block: Any) -> dict[str, Any]:
    profile = normalize_resource_profile(block)
    preserved: dict[str, Any] = {
        "_meta": {
            "as_of": str((profile.get("_meta") or {}).get("as_of") or _month()),
            "tier": str((profile.get("_meta") or {}).get("tier") or "declared"),
            "review_state": str(
                (profile.get("_meta") or {}).get("review_state") or "auto"
            ),
            "sources": [],
        }
    }
    fields = (
        "resource_types",
        "data_asset_types",
        "descriptions",
        "organizations",
        "subject_areas",
        "data_formats",
        "taxonomies",
        "keywords",
        "languages",
        "service_roles",
        "access_conditions",
        "relevant_links",
    )
    for field in fields:
        preserved[field] = []
        for item in profile.get(field) or []:
            source = str(item.get("source") or "").lower()
            sources = [str(value).lower() for value in item.get("sources") or []]
            automatic = any(
                source.startswith(prefix)
                or any(value.startswith(prefix) for value in sources)
                for prefix in AUTO_SOURCE_PREFIXES
            )
            if not automatic or str(item.get("tier") or "") == "curated":
                preserved[field].append(copy.deepcopy(item))
    status = profile.get("maintainer_status")
    if isinstance(status, dict):
        source = str(status.get("source") or "").lower()
        if not source.startswith(AUTO_SOURCE_PREFIXES) or str(
            status.get("tier") or ""
        ) == "curated":
            preserved["maintainer_status"] = copy.deepcopy(status)
    return normalize_resource_profile(preserved)


def _fairsharing_export_index(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file() or path.stat().st_size == 0:
        return {}
    frame = _read_csv(path)
    result: dict[str, dict[str, Any]] = {}
    doi_columns = [
        column
        for column in frame.columns
        if column.strip().lower() in {
            "fairsharing doi",
            "fairsharing_doi",
            "doi",
            "fairsharingurl",
            "fairsharing url",
        }
    ]
    if not doi_columns:
        return result
    pattern = re.compile(r"(10\.25504/FAIRsharing\.[A-Za-z0-9]+)", re.I)
    for _, row in frame.iterrows():
        raw = " ".join(str(row.get(column) or "") for column in doi_columns)
        match = pattern.search(raw)
        if not match:
            continue
        doi = match.group(1)
        attributes = {
            str(column).strip(): str(value).strip()
            for column, value in row.items()
            if str(value or "").strip()
        }
        attributes.setdefault("doi", doi)
        result[doi.lower()] = {"attributes": attributes}
    return result


def _fairsharing_token(session: requests.Session) -> tuple[str, str]:
    direct = str(
        os.getenv("FAIRSHARING_API_TOKEN")
        or os.getenv("FAIRSHARING_API_KEY")
        or ""
    ).strip()
    if direct:
        return direct, "configured_token"
    username = str(os.getenv("FS_U") or "").strip()
    password = str(os.getenv("FS_PWD") or "").strip()
    if not username or not password:
        return "", "credentials_unavailable"
    try:
        response = session.post(
            "https://api.fairsharing.org/users/sign_in",
            json={"user": {"login": username, "password": password}},
            headers={"Accept": "application/json"},
            timeout=35,
        )
    except requests.RequestException as exc:
        return "", f"sign_in_error:{type(exc).__name__}"
    if not response.ok:
        return "", f"sign_in_http_{response.status_code}"
    try:
        token = str((response.json() or {}).get("jwt") or "").strip()
    except (ValueError, json.JSONDecodeError):
        token = ""
    return (token, "basic_auth") if token else ("", "sign_in_missing_jwt")


def _select_fairsharing_hit(payload: Any, doi: str) -> dict[str, Any] | None:
    hits = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(hits, list):
        return None
    normalized = doi.casefold()
    exact = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        attributes = hit.get("attributes") if isinstance(hit.get("attributes"), dict) else hit
        candidate = str(attributes.get("doi") or "").strip().casefold()
        if candidate == normalized:
            exact.append(hit)
    return exact[0] if len(exact) == 1 else None


def _fetch_fairsharing(
    doi: str,
    *,
    session: requests.Session,
    token: str,
    snapshot_root: Path,
) -> tuple[dict[str, Any] | None, str]:
    if not token:
        return None, "FAIRsharing API credentials are unavailable"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        search = session.post(
            "https://api.fairsharing.org/search/fairsharing_records",
            json={"q": doi},
            headers=headers,
            timeout=45,
        )
    except requests.RequestException as exc:
        return None, f"search {type(exc).__name__}: {exc}"
    if not search.ok:
        return None, f"search HTTP {search.status_code}"
    try:
        search_payload = search.json()
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"search JSON parse: {exc}"
    hit = _select_fairsharing_hit(search_payload, doi)
    if hit is None:
        return None, "search returned no unique exact DOI hit"

    selected_payload: Any = hit
    selected_bytes = json.dumps(
        hit, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    selected_endpoint = "authenticated_search"
    record_id = str(hit.get("id") or "").strip()
    if record_id:
        for endpoint in (
            f"https://api.fairsharing.org/fairsharing_records/{quote(record_id, safe='')}",
            f"https://api.fairsharing.org/fairsharing_records/{quote(record_id, safe='')}.json",
        ):
            try:
                detail = session.get(endpoint, headers=headers, timeout=45)
            except requests.RequestException:
                continue
            if not detail.ok:
                continue
            try:
                detail_payload = detail.json()
            except (ValueError, json.JSONDecodeError):
                continue
            candidate = detail_payload.get("data") if isinstance(detail_payload, dict) else None
            if isinstance(candidate, dict):
                selected_payload = candidate
                selected_bytes = detail.content
                selected_endpoint = endpoint
                break
    digest, snapshot_path = _snapshot_bytes(
        selected_bytes,
        snapshot_root=snapshot_root,
        suffix="fairsharing.json",
    )
    profile = parse_fairsharing_record(selected_payload, doi=doi, as_of=_month())
    return {
        "doi": doi,
        "record_id": record_id,
        "fetched_at": _now(),
        "endpoint": selected_endpoint,
        "response_sha256": digest,
        "snapshot_path": snapshot_path,
        "record": selected_payload,
        "parsed_profile": profile,
    }, ""


def _fetch_website(
    url: str,
    *,
    session: requests.Session,
    snapshot_root: Path,
    timeout: int,
    max_bytes: int,
) -> tuple[dict[str, Any] | None, str]:
    headers = {
        "Accept": "text/html,application/xhtml+xml;q=0.9",
        "User-Agent": "NIH-Tracker/15 resource-profile metadata collector",
    }
    try:
        response = session.get(
            url,
            headers=headers,
            timeout=(10, timeout),
            stream=True,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not response.ok:
        response.close()
        return None, f"HTTP {response.status_code}"
    content_type = str(response.headers.get("content-type") or "").lower()
    if "html" not in content_type and "xhtml" not in content_type:
        response.close()
        return None, f"unsupported content type {content_type!r}"
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                response.close()
                return None, f"HTML exceeded {max_bytes} bytes"
            chunks.append(chunk)
    finally:
        response.close()
    content = b"".join(chunks)
    digest, snapshot_path = _snapshot_bytes(
        content,
        snapshot_root=snapshot_root,
        suffix="homepage.html",
    )
    final_url = safe_url(response.url) or url
    profile = parse_website_metadata(
        content,
        requested_url=url,
        final_url=final_url,
        content_sha256=digest,
        as_of=_month(),
    )
    return {
        "requested_url": url,
        "final_url": final_url,
        "fetched_at": _now(),
        "http_status": response.status_code,
        "content_type": content_type,
        "etag": str(response.headers.get("etag") or ""),
        "last_modified": str(response.headers.get("last-modified") or ""),
        "response_sha256": digest,
        "response_bytes": len(content),
        "snapshot_path": snapshot_path,
        "parsed_profile": profile,
    }, ""


def _source_profile(record: Any) -> dict[str, Any]:
    if isinstance(record, dict) and isinstance(record.get("parsed_profile"), dict):
        return record["parsed_profile"]
    return {}


def _profile_aux(record: Any, field: str) -> list[Any]:
    profile = _source_profile(record)
    value = profile.get(field) if isinstance(profile, dict) else None
    return value if isinstance(value, list) else []


def _merge_interoperability_evidence(
    block: Any,
    *,
    standards: list[Any],
    documentation_links: list[Any],
    families: list[str],
    source: str,
    tier: str,
) -> dict[str, Any]:
    work = normalize_interoperability(block)
    work["standards"] = list(work.get("standards") or [])
    for value in standards:
        item = canonical_standard(value, source=source, tier=tier)
        if item:
            work["standards"].append(item)
    api = work.setdefault(
        "api", {"families": [], "endpoints": [], "documentation_links": []}
    )
    api["families"] = list(api.get("families") or []) + [
        family for family in (canonical_api_family(value) for value in families) if family
    ]
    api["documentation_links"] = list(api.get("documentation_links") or [])
    for value in documentation_links:
        item = normalize_interop_link(value, source=source, tier=tier)
        if item:
            api["documentation_links"].append(item)
    if api["families"] or api["documentation_links"]:
        work["api_declared"] = True
    meta = work.setdefault("_meta", {})
    meta["sources"] = sorted(
        {
            *[str(value).strip() for value in meta.get("sources") or [] if str(value).strip()],
            source,
        }
    )
    return normalize_interoperability(work)


def _profile_coverage(blocks: dict[str, dict[str, Any]]) -> dict[str, int]:
    summaries = {uid: resource_profile_summary(block) for uid, block in blocks.items()}
    return {
        "cards": len(blocks),
        "description_documented": sum(bool(item["description"]) for item in summaries.values()),
        "resource_type_documented": sum(item["resource_type_count"] > 0 for item in summaries.values()),
        "data_asset_type_documented": sum(item["data_asset_type_count"] > 0 for item in summaries.values()),
        "subject_area_documented": sum(item["subject_area_count"] > 0 for item in summaries.values()),
        "organization_documented": sum(item["organization_count"] > 0 for item in summaries.values()),
        "data_format_documented": sum(item["data_format_count"] > 0 for item in summaries.values()),
        "maintainer_status_documented": sum(
            (block.get("maintainer_status") or {}).get("value") is not None
            for block in blocks.values()
        ),
    }


def _gap_rows(
    uid: str,
    card: dict[str, Any],
    profile: dict[str, Any],
    interoperability: dict[str, Any],
) -> list[dict[str, str]]:
    name = str((card.get("identity") or {}).get("name") or "")
    summary = resource_profile_summary(profile)
    rows: list[dict[str, str]] = []
    gap_definitions = [
        (
            "description_missing",
            not summary["description"],
            "No source-bound brief description is recorded.",
            "Confirm a homepage meta/JSON-LD description or add a reviewed description to inputs/resource_profile_curated.csv.",
        ),
        (
            "resource_type_missing",
            summary["resource_type_count"] == 0,
            "No resource type is recorded.",
            "Use an exact FAIRsharing/re3data type or a reviewed resource classification.",
        ),
        (
            "data_asset_type_missing",
            summary["data_asset_type_count"] == 0,
            "No type of data asset or digital object is recorded.",
            "Record the object types actually held or served; do not infer solely from the repository name.",
        ),
        (
            "stewardship_not_documented",
            summary["organization_count"] == 0,
            "No provider, host, owner, funder, or responsible organization is recorded.",
            "Confirm publicly documented organizational responsibility. Do not interpret an organization address as physical data residency.",
        ),
        (
            "subject_areas_missing",
            summary["subject_area_count"] == 0,
            "No subject area is recorded.",
            "Use registry subjects/domains or reviewed controlled terms.",
        ),
        (
            "data_formats_missing",
            summary["data_format_count"] == 0,
            "No data or metadata format is recorded.",
            "Add only formats explicitly documented by the repository or an exact registry record.",
        ),
    ]
    for gap_type, missing, detail, action in gap_definitions:
        if missing:
            rows.append(
                {
                    "repo_uid": uid,
                    "name": name,
                    "gap_type": gap_type,
                    "status": "open",
                    "source": "resource_profile",
                    "detail": detail,
                    "recommended_action": action,
                }
            )
    standards = interoperability.get("standards") or []
    if standards and not any(
        item.get("role") or item.get("version") or item.get("evidence_url")
        for item in standards
    ):
        rows.append(
            {
                "repo_uid": uid,
                "name": name,
                "gap_type": "interoperability_standard_details_missing",
                "status": "open",
                "source": "interoperability",
                "detail": "Standards are named, but implementation role, version, or evidence URL is not documented.",
                "recommended_action": "Link the repository documentation that states how each standard is used; keep declared and verified evidence distinct.",
            }
        )
    return rows


def run(
    *,
    fairsharing_export_path: Path,
    curated_path: Path,
    fairsharing_cache_path: Path,
    website_cache_path: Path,
    re3data_cache_path: Path,
    snapshot_root: Path,
    fairsharing_policy: str,
    fairsharing_max_age_days: int,
    website_policy: str,
    website_max_age_days: int,
    website_max_fetches: int,
    website_timeout: int,
    website_max_bytes: int,
    strict_fairsharing: bool,
    strict_website: bool,
    apply: bool,
) -> tuple[bool, dict[str, Any], Path]:
    network_allowed = str(os.getenv("NIH_TRACKER_EXTERNAL_NETWORK_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}
    if (fairsharing_policy != "none" or website_policy != "none") and not network_allowed:
        raise RuntimeError("FAIRsharing/homepage refresh requires NIH_TRACKER_EXTERNAL_NETWORK_ENABLED=1; use both policies=none for offline/cached-only runs")
    cards = all_cards()
    if not cards:
        raise RuntimeError("No cards were found")
    curated = _load_curated(curated_path)
    fair_export = _fairsharing_export_index(fairsharing_export_path)
    fair_cache_before = sha256_file(fairsharing_cache_path) if fairsharing_cache_path.is_file() else None
    website_cache_before = sha256_file(website_cache_path) if website_cache_path.is_file() else None
    re3data_cache_sha256 = sha256_file(re3data_cache_path) if re3data_cache_path.is_file() else None
    fair_cache = _load_cache(fairsharing_cache_path, FAIR_CACHE_SCHEMA)
    website_cache = _load_cache(website_cache_path, WEBSITE_CACHE_SCHEMA)
    re3data_cache = _load_cache(
        re3data_cache_path,
        "nih-tracker-re3data-interoperability-cache/v1",
    )
    fair_records = fair_cache.setdefault("records", {})
    website_records = website_cache.setdefault("records", {})
    re3data_records = re3data_cache.get("records") if isinstance(re3data_cache.get("records"), dict) else {}

    can_write_cache = not RESOURCE_PROFILE_CACHE_READ_ONLY
    fair_session = requests.Session()
    fair_session.headers.update({"User-Agent": "NIH-Tracker/15 resource-profile enrichment"})
    web_session = requests.Session()
    token, token_basis = ("", "offline_or_not_requested")
    if can_write_cache and fairsharing_policy != "none":
        token, token_basis = _fairsharing_token(fair_session)
    fair_errors: list[dict[str, str]] = []
    website_errors: list[dict[str, str]] = []
    fair_cache_changed = False
    website_cache_changed = False

    linked_dois = sorted(
        {
            doi
            for card in cards.values()
            for doi in _linked_fairsharing_dois(card)
        },
        key=str.casefold,
    )
    if can_write_cache and fairsharing_policy != "none":
        for index, doi in enumerate(linked_dois, start=1):
            key = doi.casefold()
            current = fair_records.get(key) if isinstance(fair_records.get(key), dict) else None
            if not _should_fetch(
                current,
                policy=fairsharing_policy,
                max_age_days=fairsharing_max_age_days,
            ):
                continue
            record, error = _fetch_fairsharing(
                doi,
                session=fair_session,
                token=token,
                snapshot_root=snapshot_root,
            )
            if record:
                fair_records[key] = record
                fair_cache_changed = True
            else:
                fair_errors.append({"doi": doi, "error": error})
            if index < len(linked_dois):
                time.sleep(0.12)

    homepages = []
    for uid, card in cards.items():
        homepage = safe_url((card.get("identity") or {}).get("homepage"))
        if homepage:
            homepages.append((uid, homepage))
    fetch_count = 0
    if can_write_cache and website_policy != "none":
        for uid, homepage in homepages:
            current = website_records.get(homepage) if isinstance(website_records.get(homepage), dict) else None
            if not _should_fetch(
                current,
                policy=website_policy,
                max_age_days=website_max_age_days,
            ):
                continue
            if website_max_fetches > 0 and fetch_count >= website_max_fetches:
                break
            fetch_count += 1
            record, error = _fetch_website(
                homepage,
                session=web_session,
                snapshot_root=snapshot_root,
                timeout=website_timeout,
                max_bytes=website_max_bytes,
            )
            if record:
                website_records[homepage] = record
                website_cache_changed = True
            else:
                website_errors.append(
                    {"repo_uid": uid, "url": homepage, "error": error}
                )
            time.sleep(0.08)

    if fair_cache_changed:
        fair_cache.update(
            {
                "schema": FAIR_CACHE_SCHEMA,
                "updated_at": _now(),
                "authentication_basis": token_basis,
                "source_api": "FAIRsharing authenticated REST/search API",
            }
        )
        atomic_write_json(fairsharing_cache_path, fair_cache)
    if website_cache_changed:
        website_cache.update(
            {
                "schema": WEBSITE_CACHE_SCHEMA,
                "updated_at": _now(),
                "extraction_policy": "HTML metadata, JSON-LD, and explicit link relations only",
            }
        )
        atomic_write_json(website_cache_path, website_cache)

    profiles: dict[str, dict[str, Any]] = {}
    interoperability_blocks: dict[str, dict[str, Any]] = {}
    proposals: list[dict[str, Any]] = []
    gaps: list[dict[str, str]] = []
    changed_cards: dict[str, dict[str, Any]] = {}
    used_sources: dict[str, Any] = {}

    for uid, card in cards.items():
        card_blocks = card.get("blocks") or {}
        old_profile = card_blocks.get("resource_profile") or {}
        old_interop = card_blocks.get("interoperability") or {}
        profile = _preserve_nonautomatic_profile(old_profile)
        interop = normalize_interoperability(old_interop, repo_uid=uid)
        source_records: dict[str, Any] = {}

        for r3d_id in _linked_re3data_ids(card):
            record = re3data_records.get(r3d_id) if isinstance(re3data_records.get(r3d_id), dict) else None
            if not record:
                continue
            profile = merge_resource_profile(profile, parse_re3data_profile(record, r3d_id=r3d_id))
            source_records[f"re3data:{r3d_id}"] = {
                "response_sha256": record.get("response_sha256"),
                "fetched_at": record.get("fetched_at"),
                "api_endpoint": record.get("api_endpoint"),
            }

        for doi in _linked_fairsharing_dois(card):
            export_record = fair_export.get(doi.casefold())
            if export_record:
                export_profile = parse_fairsharing_record(
                    export_record,
                    doi=doi,
                    as_of=_month(),
                )
                profile = merge_resource_profile(profile, export_profile)
                interop = _merge_interoperability_evidence(
                    interop,
                    standards=export_profile.get("standards") or [],
                    documentation_links=export_profile.get("api_documentation_links") or [],
                    families=export_profile.get("api_families") or [],
                    source=f"fairsharing_export:{doi}",
                    tier="registry",
                )
            cached = fair_records.get(doi.casefold()) if isinstance(fair_records.get(doi.casefold()), dict) else None
            if cached:
                fair_profile = _source_profile(cached)
                if not fair_profile and isinstance(cached.get("record"), dict):
                    fair_profile = parse_fairsharing_record(
                        cached["record"], doi=doi, as_of=str(cached.get("fetched_at") or "")[:10]
                    )
                profile = merge_resource_profile(profile, fair_profile)
                interop = _merge_interoperability_evidence(
                    interop,
                    standards=fair_profile.get("standards") or [],
                    documentation_links=fair_profile.get("api_documentation_links") or [],
                    families=fair_profile.get("api_families") or [],
                    source=f"fairsharing:{doi}",
                    tier="registry",
                )
                source_records[f"fairsharing:{doi}"] = {
                    "response_sha256": cached.get("response_sha256"),
                    "snapshot_path": cached.get("snapshot_path"),
                    "fetched_at": cached.get("fetched_at"),
                    "endpoint": cached.get("endpoint"),
                }

        homepage = safe_url((card.get("identity") or {}).get("homepage"))
        website_record = website_records.get(homepage) if homepage and isinstance(website_records.get(homepage), dict) else None
        if website_record:
            website_profile = _source_profile(website_record)
            profile = merge_resource_profile(profile, website_profile)
            interop = _merge_interoperability_evidence(
                interop,
                standards=website_profile.get("standards") or [],
                documentation_links=website_profile.get("api_documentation_links") or [],
                families=website_profile.get("api_families") or [],
                source=str(
                    ((website_profile.get("_meta") or {}).get("sources") or ["website"])[0]
                ),
                tier="attestation",
            )
            source_records["website"] = {
                "requested_url": website_record.get("requested_url"),
                "final_url": website_record.get("final_url"),
                "response_sha256": website_record.get("response_sha256"),
                "snapshot_path": website_record.get("snapshot_path"),
                "fetched_at": website_record.get("fetched_at"),
            }

        curated_record = curated.get(uid)
        if curated_record:
            profile = merge_resource_profile(profile, curated_record["profile"])
            interop = _merge_interoperability_evidence(
                interop,
                standards=curated_record.get("standards") or [],
                documentation_links=curated_record.get("api_documentation_links") or [],
                families=curated_record.get("api_families") or [],
                source=str(curated_record.get("source") or "human_curated"),
                tier="curated",
            )
            source_records["curated"] = {
                "source": curated_record.get("source"),
                "as_of": curated_record.get("as_of"),
            }

        profile = normalize_resource_profile(profile)
        interop = normalize_interoperability(interop, repo_uid=uid)
        profiles[uid] = profile
        interoperability_blocks[uid] = interop
        gaps.extend(_gap_rows(uid, card, profile, interop))
        summary = resource_profile_summary(profile)
        profile_changed = normalize_resource_profile(old_profile) != profile
        interop_changed = normalize_interoperability(old_interop, repo_uid=uid) != interop
        proposals.append(
            {
                "repo_uid": uid,
                "name": str((card.get("identity") or {}).get("name") or ""),
                "profile_changed": profile_changed,
                "interoperability_changed": interop_changed,
                **summary,
                "source_count": len((profile.get("_meta") or {}).get("sources") or []),
                "confirmed_fairsharing_dois": "; ".join(_linked_fairsharing_dois(card)),
                "confirmed_re3data_ids": "; ".join(_linked_re3data_ids(card)),
                "website_cached": bool(website_record),
            }
        )
        if profile_changed or interop_changed:
            changed_cards[uid] = copy.deepcopy(card)
        used_sources[uid] = {
            "card_sha256": sha256_file(card_path(uid)),
            "sources": source_records,
        }

    fair_cache_after = sha256_file(fairsharing_cache_path) if fairsharing_cache_path.is_file() else None
    website_cache_after = sha256_file(website_cache_path) if website_cache_path.is_file() else None
    invalid_descriptions = [
        f"{uid}: {len(str(item.get('text') or ''))}"
        for uid, profile in profiles.items()
        for item in profile.get("descriptions") or []
        if len(str(item.get("text") or "")) > DESCRIPTION_MAX_CHARS
    ]
    unsafe_organizations = [
        f"{uid}: {item.get('name')}"
        for uid, profile in profiles.items()
        for item in profile.get("organizations") or []
        if "@" in str(item.get("name") or "")
    ]
    checks = {
        "cards loaded": (bool(cards), f"{len(cards)} cards"),
        "FAIRsharing export readable": (
            fairsharing_export_path.is_file() and bool(fair_export),
            f"{len(fair_export)} DOI-indexed rows",
        ),
        "all output UIDs active": (set(profiles) == set(cards), f"{len(profiles)} profiles"),
        "description bounds valid": (not invalid_descriptions, f"{len(invalid_descriptions)} invalid"),
        "organization privacy guard": (not unsafe_organizations, f"{len(unsafe_organizations)} email-like names"),
        "FAIRsharing metadata fetch": (
            not strict_fairsharing or not fair_errors,
            f"{len(fair_errors)} fetch error(s); {'blocking' if strict_fairsharing else 'advisory'}",
        ),
        "website metadata fetch": (
            not strict_website or not website_errors,
            f"{len(website_errors)} fetch error(s); {'blocking' if strict_website else 'advisory'}",
        ),
    }
    ok = gate(checks)
    run_dir = Path(new_run_dir("build_resource_profiles"))
    pd.DataFrame(proposals).to_csv(run_dir / "resource_profile_proposal.csv", index=False)
    error_gaps = [
        {
            "repo_uid": "",
            "name": "",
            "gap_type": "fairsharing_fetch_error",
            "status": "open",
            "source": row.get("doi", ""),
            "detail": row.get("error", ""),
            "recommended_action": "Retry with valid FAIRsharing credentials; cached/export evidence was preserved.",
        }
        for row in fair_errors
    ] + [
        {
            "repo_uid": row.get("repo_uid", ""),
            "name": str((cards.get(row.get("repo_uid", ""), {}).get("identity") or {}).get("name") or ""),
            "gap_type": "website_fetch_error",
            "status": "open",
            "source": row.get("url", ""),
            "detail": row.get("error", ""),
            "recommended_action": "Check the canonical homepage or add source-supported metadata to the curated input.",
        }
        for row in website_errors
    ]
    gap_frame = pd.DataFrame(gaps + error_gaps)
    for column in GAP_COLUMNS:
        if column not in gap_frame.columns:
            gap_frame[column] = ""
    gap_frame = gap_frame[GAP_COLUMNS + [column for column in gap_frame.columns if column not in GAP_COLUMNS]]
    gap_frame.to_csv(run_dir / "resource_profile_gaps.csv", index=False)
    (run_dir / "resource_profiles.yaml").write_text(
        yaml.safe_dump(profiles, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )
    coverage = _profile_coverage(profiles)
    atomic_write_json(run_dir / "coverage.json", coverage)
    source_snapshot = {
        "schema": "nih-tracker-resource-profile-run-snapshot/v1",
        "generated_at": _now(),
        "environment": ENV,
        "fairsharing_export": {
            "path": str(fairsharing_export_path),
            "sha256": sha256_file(fairsharing_export_path),
        },
        "curated_input": {
            "path": str(curated_path),
            "sha256": sha256_file(curated_path) if curated_path.is_file() else None,
        },
        "fairsharing_cache": {
            "path": str(fairsharing_cache_path),
            "before_sha256": fair_cache_before,
            "after_sha256": fair_cache_after,
        },
        "website_cache": {
            "path": str(website_cache_path),
            "before_sha256": website_cache_before,
            "after_sha256": website_cache_after,
        },
        "re3data_cache": {
            "path": str(re3data_cache_path),
            "sha256": re3data_cache_sha256,
        },
        "records_used": used_sources,
    }
    atomic_write_json(run_dir / "source_snapshot.json", source_snapshot)

    if apply and ok and changed_cards:
        with card_transaction("build_resource_profiles"):
            locked = {
                uid: load_card_for_update(uid, cards[uid])
                for uid in sorted(changed_cards)
            }
            for uid in sorted(changed_cards):
                next_card = copy.deepcopy(locked[uid])
                next_card.setdefault("blocks", {})["resource_profile"] = profiles[uid]
                next_card["blocks"]["interoperability"] = interoperability_blocks[uid]
                save_card(uid, next_card)
        print(
            f"\n  Applied {len(changed_cards)} resource-profile/interoperability update(s) transactionally."
        )
    elif apply and not ok:
        print("\n  Gate failed; no card was changed.")
    elif apply:
        print("\n  Apply requested; valid no-op because no card differs.")
    else:
        print("\n  DRY RUN - inspect resource_profile_proposal.csv and resource_profile_gaps.csv.")

    outputs = {
        **coverage,
        "changed": len(changed_cards),
        "applied": len(changed_cards) if apply and ok else 0,
        "fairsharing_linked_dois": len(linked_dois),
        "fairsharing_cache_records": len(fair_records),
        "fairsharing_fetch_errors": len(fair_errors),
        "website_homepages": len(homepages),
        "website_cache_records": len(website_records),
        "website_fetches_this_run": fetch_count,
        "website_fetch_errors": len(website_errors),
        "gap_rows": len(gap_frame),
        "source_snapshot": str(run_dir / "source_snapshot.json"),
    }
    ledger_append(
        "build_resource_profiles",
        params={
            "fairsharing_export": fairsharing_export_path.name,
            "curated": curated_path.name,
            "fairsharing_policy": fairsharing_policy,
            "fairsharing_max_age_days": fairsharing_max_age_days,
            "website_policy": website_policy,
            "website_max_age_days": website_max_age_days,
            "website_max_fetches": website_max_fetches,
            "website_max_bytes": website_max_bytes,
            "strict_fairsharing": strict_fairsharing,
            "strict_website": strict_website,
            "apply": apply,
        },
        inputs={
            "fairsharing_export": sha256_file(fairsharing_export_path),
            "curated": sha256_file(curated_path) if curated_path.is_file() else None,
            "fairsharing_cache_before": fair_cache_before,
            "website_cache_before": website_cache_before,
            "re3data_cache": re3data_cache_sha256,
        },
        outputs=outputs,
        gate_passed=ok,
        run_dir=str(run_dir),
    )
    print("\n  COVERAGE")
    for key, value in coverage.items():
        print(f"  {key:32s} {value}")
    print(f"  changed cards                    {len(changed_cards)}")
    print(f"  FAIRsharing fetch errors         {len(fair_errors)}")
    print(f"  website fetch errors             {len(website_errors)}")
    print(f"\nResource-profile run: {run_dir}")
    return ok, outputs, run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fairsharing-export", default="FAIRSHARING_EXPORT.csv")
    parser.add_argument("--curated", default="resource_profile_curated.csv")
    parser.add_argument("--fairsharing-cache", default=DEFAULT_FAIR_CACHE)
    parser.add_argument("--website-cache", default=DEFAULT_WEBSITE_CACHE)
    parser.add_argument("--re3data-cache", default=DEFAULT_RE3DATA_CACHE)
    parser.add_argument("--snapshot-root", default=DEFAULT_SNAPSHOT_ROOT)
    parser.add_argument(
        "--fairsharing-policy",
        choices=("none", "missing", "stale", "all"),
        default="missing" if ENV == "prod" else "none",
    )
    parser.add_argument("--fairsharing-max-age-days", type=int, default=30)
    parser.add_argument(
        "--website-policy",
        choices=("none", "missing", "stale", "all"),
        default="missing" if ENV == "prod" else "none",
    )
    parser.add_argument("--website-max-age-days", type=int, default=90)
    parser.add_argument(
        "--website-max-fetches",
        type=int,
        default=0,
        help="Maximum homepage fetches per run; 0 means no explicit cap.",
    )
    parser.add_argument("--website-timeout", type=int, default=25)
    parser.add_argument("--website-max-bytes", type=int, default=2_000_000)
    parser.add_argument("--strict-fairsharing", action="store_true")
    parser.add_argument("--strict-website", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    ok, _, _ = run(
        fairsharing_export_path=Path(INP) / args.fairsharing_export,
        curated_path=Path(INP) / args.curated,
        fairsharing_cache_path=Path(args.fairsharing_cache).expanduser(),
        website_cache_path=Path(args.website_cache).expanduser(),
        re3data_cache_path=Path(args.re3data_cache).expanduser(),
        snapshot_root=Path(args.snapshot_root).expanduser(),
        fairsharing_policy=args.fairsharing_policy,
        fairsharing_max_age_days=max(1, args.fairsharing_max_age_days),
        website_policy=args.website_policy,
        website_max_age_days=max(1, args.website_max_age_days),
        website_max_fetches=max(0, args.website_max_fetches),
        website_timeout=max(5, args.website_timeout),
        website_max_bytes=max(100_000, args.website_max_bytes),
        strict_fairsharing=args.strict_fairsharing,
        strict_website=args.strict_website,
        apply=args.apply,
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
