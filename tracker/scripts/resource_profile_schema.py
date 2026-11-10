"""Canonical NIH Tracker resource-profile schema and evidence parsers.

The resource profile describes what a tracked resource is, the assets it
contains, the subjects it covers, and the organizations publicly associated
with its operation.  It is deliberately separate from the interoperability
block: descriptive registry/website evidence is not treated as proof that an
API or standard implementation has been technically verified.
"""
from __future__ import annotations

import ast
import copy
import html
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urljoin, urlparse

try:  # BeautifulSoup is present in Colab; the parser fails closed if absent.
    from bs4 import BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover - exercised only in minimal runtimes.
    BeautifulSoup = None

SCHEMA_VERSION = "1.0"

TIER_RANK = {
    "": 0,
    "declared": 1,
    "attestation": 2,
    "registry": 3,
    "verified": 4,
    "curated": 5,
}

RESOURCE_TYPE_MAP = {
    "database": "Database",
    "repository": "Data repository",
    "data repository": "Data repository",
    "datarepository": "Data repository",
    "archive": "Data archive",
    "data archive": "Data archive",
    "dataarchive": "Data archive",
    "knowledgebase": "Knowledgebase",
    "knowledge base": "Knowledgebase",
    "knowledge graph": "Knowledge graph",
    "datacatalog": "Data catalog",
    "data catalog": "Data catalog",
    "catalog": "Data catalog",
    "registry": "Registry",
    "data portal": "Data portal",
    "portal": "Data portal",
    "data commons": "Data commons",
    "commons": "Data commons",
    "biobank": "Biobank / biospecimen repository",
    "biospecimen repository": "Biobank / biospecimen repository",
    "softwareapplication": "Software platform",
    "software platform": "Software platform",
    "website": "Web resource",
}

DATA_ASSET_TYPE_MAP = {
    "dataset": "Dataset",
    "data set": "Dataset",
    "research data": "Research data",
    "software": "Software",
    "source code": "Source code",
    "image": "Image",
    "imageobject": "Image",
    "medical image": "Medical imaging data",
    "imaging data": "Imaging data",
    "genomic data": "Genomic data",
    "genomics": "Genomic data",
    "sequence data": "Sequence data",
    "transcriptomic data": "Transcriptomic data",
    "proteomic data": "Proteomic data",
    "metabolomic data": "Metabolomic data",
    "epigenomic data": "Epigenomic data",
    "phenotype data": "Phenotypic data",
    "clinical data": "Clinical data",
    "biospecimen": "Biospecimen",
    "biological specimen": "Biospecimen",
    "survey data": "Survey data",
    "observational data": "Observational data",
    "experimental data": "Experimental data",
    "simulation data": "Simulation data",
    "measurement and test data": "Measurement and test data",
    "text": "Text",
    "publication": "Publication",
    "preprint": "Preprint",
    "protocol": "Protocol",
    "workflow": "Workflow",
    "model": "Model",
    "ontology": "Ontology / controlled vocabulary",
    "controlled vocabulary": "Ontology / controlled vocabulary",
    "knowledge graph": "Knowledge graph",
    "metadata": "Metadata record",
    "datadownload": "Downloadable data file",
}

DESCRIPTION_MAX_CHARS = 1400


def _now_month() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")


def safe_url(value: Any, *, base_url: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if base_url:
        text = urljoin(base_url, text)
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _norm_key(value: Any) -> str:
    text = html.unescape(str(value or "")).strip().lower()
    text = re.sub(r"[_/]+", " ", text)
    text = re.sub(r"[^a-z0-9.+-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_text(value: Any, *, max_chars: int = DESCRIPTION_MAX_CHARS) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(text)
            except Exception:
                continue
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, Mapping):
                return [dict(parsed)]
        return [item.strip() for item in re.split(r"\s*[;|]\s*", text) if item.strip()]
    return [value]


def _names_from(value: Any) -> list[str]:
    result: list[str] = []
    for item in _as_list(value):
        if isinstance(item, Mapping):
            name = (
                item.get("name")
                or item.get("label")
                or item.get("title")
                or item.get("value")
                or item.get("term")
            )
            if name:
                result.append(_clean_text(name, max_chars=300))
        else:
            text = _clean_text(item, max_chars=300)
            if text:
                result.append(text)
    return result


def _canonical_resource_type(value: Any) -> str:
    raw = _clean_text(value, max_chars=160)
    if not raw:
        return ""
    key = _norm_key(raw)
    if key in RESOURCE_TYPE_MAP:
        return RESOURCE_TYPE_MAP[key]
    for alias, canonical in RESOURCE_TYPE_MAP.items():
        if alias in key:
            return canonical
    return raw


def _canonical_data_asset_type(value: Any) -> str:
    raw = _clean_text(value, max_chars=160)
    if not raw:
        return ""
    key = _norm_key(raw)
    if key in DATA_ASSET_TYPE_MAP:
        return DATA_ASSET_TYPE_MAP[key]
    for alias, canonical in DATA_ASSET_TYPE_MAP.items():
        if alias == key or (len(alias) >= 6 and alias in key):
            return canonical
    return raw


def normalize_term(
    value: Any,
    *,
    source: str = "",
    tier: str = "",
    scheme: str = "",
    as_of: str = "",
    category: str = "",
    canonicalizer=None,
) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        item = copy.deepcopy(dict(value))
        raw_name = (
            item.get("name")
            or item.get("label")
            or item.get("title")
            or item.get("value")
            or item.get("term")
        )
    else:
        item = {"name": value}
        raw_name = value
    name = _clean_text(raw_name, max_chars=300)
    if canonicalizer:
        name = canonicalizer(name)
    if not name:
        return None
    result: dict[str, Any] = {"name": name}
    result["scheme"] = str(item.get("scheme") or item.get("vocabulary") or scheme or "").strip()
    result["identifier"] = str(
        item.get("identifier") or item.get("id") or item.get("code") or ""
    ).strip()
    result["url"] = safe_url(item.get("url") or item.get("uri") or item.get("identifier_url"))
    result["category"] = str(item.get("category") or category or "").strip()
    result["source"] = str(item.get("source") or source or "").strip()
    result["tier"] = str(item.get("tier") or tier or "").strip()
    result["as_of"] = str(item.get("as_of") or as_of or "").strip()
    sources = [
        str(entry).strip()
        for entry in _as_list(item.get("sources"))
        if str(entry).strip()
    ]
    if result["source"] and result["source"] not in sources:
        sources.append(result["source"])
    if sources:
        result["sources"] = sorted(set(sources))
    return {key: value for key, value in result.items() if value not in (None, "", [])}


def normalize_description(
    value: Any,
    *,
    source: str = "",
    tier: str = "",
    source_url: str = "",
    as_of: str = "",
    extraction_method: str = "",
    content_sha256: str = "",
) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        item = copy.deepcopy(dict(value))
        raw_text = item.get("text") or item.get("description") or item.get("value")
    else:
        item = {"text": value}
        raw_text = value
    text = _clean_text(raw_text)
    if len(text) < 20:
        return None
    result = {
        "text": text,
        "language": str(item.get("language") or item.get("lang") or "").strip(),
        "source_url": safe_url(item.get("source_url") or item.get("url") or source_url),
        "source": str(item.get("source") or source or "").strip(),
        "tier": str(item.get("tier") or tier or "").strip(),
        "as_of": str(item.get("as_of") or as_of or "").strip(),
        "extraction_method": str(
            item.get("extraction_method") or extraction_method or ""
        ).strip(),
        "content_sha256": str(item.get("content_sha256") or content_sha256 or "").strip(),
    }
    return {key: value for key, value in result.items() if value not in (None, "", [])}


def normalize_organization(
    value: Any,
    *,
    roles: Iterable[str] = (),
    source: str = "",
    tier: str = "",
    as_of: str = "",
) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        item = copy.deepcopy(dict(value))
        raw_name = (
            item.get("name")
            or item.get("organisationName")
            or item.get("organizationName")
            or item.get("label")
        )
        types = {_norm_key(entry) for entry in _as_list(item.get("@type"))}
        if "person" in types and not ({"organization", "organisation"} & types):
            return None
    else:
        item = {"name": value}
        raw_name = value
    name = _clean_text(raw_name, max_chars=300)
    if not name or "@" in name:
        return None
    role_values = []
    for role in [*roles, *_as_list(item.get("roles")), *_as_list(item.get("role")), *_as_list(item.get("responsibilities"))]:
        cleaned = _clean_text(role, max_chars=100).lower().replace("_", " ")
        if cleaned:
            role_values.append(cleaned)
    identifiers = item.get("identifiers") if isinstance(item.get("identifiers"), Mapping) else {}
    ror = str(item.get("ror") or item.get("ror_id") or identifiers.get("ror") or "").strip()
    rrid = str(item.get("rrid") or identifiers.get("rrid") or "").strip()
    result = {
        "name": name,
        "roles": sorted(set(role_values)),
        "url": safe_url(item.get("url") or item.get("homepage") or item.get("sameAs")),
        "ror": ror,
        "rrid": rrid,
        "country": _clean_text(item.get("country") or item.get("addressCountry"), max_chars=120),
        "organization_type": _clean_text(
            item.get("organization_type") or item.get("type") or item.get("institutionType"),
            max_chars=160,
        ),
        "source": str(item.get("source") or source or "").strip(),
        "tier": str(item.get("tier") or tier or "").strip(),
        "as_of": str(item.get("as_of") or as_of or "").strip(),
    }
    return {key: value for key, value in result.items() if value not in (None, "", [])}


def normalize_link(value: Any, *, source: str = "", tier: str = "", as_of: str = "") -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        item = copy.deepcopy(dict(value))
    else:
        item = {"url": value}
    url = safe_url(item.get("url") or item.get("href"))
    if not url:
        return None
    result = {
        "label": _clean_text(item.get("label") or item.get("name") or item.get("type") or "Related link", max_chars=160),
        "type": str(item.get("type") or "related").strip(),
        "url": url,
        "source": str(item.get("source") or source or "").strip(),
        "tier": str(item.get("tier") or tier or "").strip(),
        "as_of": str(item.get("as_of") or as_of or "").strip(),
    }
    return {key: value for key, value in result.items() if value not in (None, "", [])}


def _merge_source_fields(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(existing)
    existing_rank = TIER_RANK.get(str(result.get("tier") or ""), 0)
    incoming_rank = TIER_RANK.get(str(incoming.get("tier") or ""), 0)
    if incoming_rank > existing_rank:
        for key, value in incoming.items():
            if key not in {"source", "sources"} and value not in (None, "", []):
                result[key] = copy.deepcopy(value)
    for list_field in ("roles",):
        merged_values = [
            str(value).strip()
            for value in [
                *_as_list(existing.get(list_field)),
                *_as_list(incoming.get(list_field)),
            ]
            if str(value).strip()
        ]
        if merged_values:
            result[list_field] = sorted(set(merged_values), key=str.casefold)
    if isinstance(existing.get("identifiers"), Mapping) or isinstance(incoming.get("identifiers"), Mapping):
        identifiers: dict[str, Any] = {}
        if isinstance(existing.get("identifiers"), Mapping):
            identifiers.update(copy.deepcopy(dict(existing["identifiers"])))
        if isinstance(incoming.get("identifiers"), Mapping):
            identifiers.update(copy.deepcopy(dict(incoming["identifiers"])))
        if identifiers:
            result["identifiers"] = identifiers
    sources = []
    for record in (existing, incoming):
        sources.extend(_as_list(record.get("sources")))
        if record.get("source"):
            sources.append(record.get("source"))
    sources = sorted({str(value).strip() for value in sources if str(value).strip()})
    if sources:
        result["sources"] = sources
        result["source"] = sources[0]
    if incoming.get("as_of") and str(incoming["as_of"]) > str(result.get("as_of") or ""):
        result["as_of"] = incoming["as_of"]
    if incoming_rank > existing_rank:
        result["tier"] = incoming.get("tier")
    return result


def _dedupe_records(items: Iterable[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in items:
        key = tuple(_norm_key(item.get(field)) for field in keys)
        if not any(key):
            continue
        if key in by_key:
            by_key[key] = _merge_source_fields(by_key[key], item)
        else:
            by_key[key] = copy.deepcopy(item)
    return sorted(by_key.values(), key=lambda item: tuple(_norm_key(item.get(field)) for field in keys))


def description_rank(item: Mapping[str, Any]) -> tuple[int, int, int]:
    source = str(item.get("source") or "").lower()
    method = str(item.get("extraction_method") or "").lower()
    tier = str(item.get("tier") or "")
    source_rank = 0
    if "human_curated" in source or tier == "curated":
        source_rank = 60
    elif source.startswith("website:") and "jsonld" in method:
        source_rank = 55
    elif source.startswith("website:"):
        source_rank = 50
    elif source.startswith("fairsharing:"):
        source_rank = 40
    elif source.startswith("re3data:"):
        source_rank = 35
    elif source:
        source_rank = 20
    return (source_rank, TIER_RANK.get(tier, 0), min(len(str(item.get("text") or "")), DESCRIPTION_MAX_CHARS))


def normalize_resource_profile(block: Any) -> dict[str, Any]:
    work = copy.deepcopy(block) if isinstance(block, Mapping) else {}
    meta = work.get("_meta") if isinstance(work.get("_meta"), Mapping) else {}
    result: dict[str, Any] = {
        "_meta": {
            "block_version": SCHEMA_VERSION,
            "as_of": str(meta.get("as_of") or _now_month()),
            "review_state": str(meta.get("review_state") or "auto"),
            "tier": str(meta.get("tier") or ""),
            "sources": sorted(
                {
                    str(value).strip()
                    for value in [*_as_list(meta.get("sources")), *_as_list(meta.get("source"))]
                    if str(value).strip()
                }
            ),
        },
        "resource_types": [],
        "data_asset_types": [],
        "descriptions": [],
        "organizations": [],
        "subject_areas": [],
        "data_formats": [],
        "taxonomies": [],
        "keywords": [],
        "languages": [],
        "service_roles": [],
        "access_conditions": [],
        "relevant_links": [],
        "maintainer_status": None,
    }
    for value in _as_list(work.get("resource_types")):
        item = normalize_term(value, canonicalizer=_canonical_resource_type)
        if item:
            result["resource_types"].append(item)
    for value in _as_list(work.get("data_asset_types")):
        item = normalize_term(value, canonicalizer=_canonical_data_asset_type)
        if item:
            result["data_asset_types"].append(item)
    for value in _as_list(work.get("descriptions") or work.get("description")):
        item = normalize_description(value)
        if item:
            result["descriptions"].append(item)
    for value in _as_list(work.get("organizations")):
        item = normalize_organization(value)
        if item:
            result["organizations"].append(item)
    for field, scheme in (
        ("subject_areas", ""),
        ("data_formats", ""),
        ("taxonomies", ""),
        ("keywords", ""),
        ("languages", ""),
        ("service_roles", ""),
        ("access_conditions", ""),
    ):
        for value in _as_list(work.get(field)):
            item = normalize_term(value, scheme=scheme)
            if item:
                result[field].append(item)
    for value in _as_list(work.get("relevant_links")):
        item = normalize_link(value)
        if item:
            result["relevant_links"].append(item)
    maintainer_status = work.get("maintainer_status")
    if isinstance(maintainer_status, Mapping):
        result["maintainer_status"] = copy.deepcopy(dict(maintainer_status))
    elif maintainer_status not in (None, ""):
        result["maintainer_status"] = {"value": bool(maintainer_status)}

    result["resource_types"] = _dedupe_records(result["resource_types"], ("name", "scheme"))
    result["data_asset_types"] = _dedupe_records(result["data_asset_types"], ("name", "scheme"))
    result["descriptions"] = _dedupe_records(result["descriptions"], ("text",))
    result["descriptions"].sort(key=description_rank, reverse=True)
    result["organizations"] = _dedupe_records(result["organizations"], ("name",))
    for field in (
        "subject_areas",
        "data_formats",
        "taxonomies",
        "keywords",
        "languages",
        "service_roles",
        "access_conditions",
    ):
        result[field] = _dedupe_records(result[field], ("name", "scheme"))
    result["relevant_links"] = _dedupe_records(result["relevant_links"], ("url",))

    evidence_items = []
    for field in (
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
    ):
        evidence_items.extend(result[field])
    sources = list(result["_meta"].get("sources") or [])
    tiers = []
    maintainer = result.get("maintainer_status")
    if isinstance(maintainer, Mapping):
        if maintainer.get("source"):
            sources.append(maintainer["source"])
        sources.extend(_as_list(maintainer.get("sources")))
        if maintainer.get("tier"):
            tiers.append(maintainer["tier"])
        if maintainer.get("as_of") and str(maintainer["as_of"]) > str(result["_meta"].get("as_of") or ""):
            result["_meta"]["as_of"] = maintainer["as_of"]
    for item in evidence_items:
        if item.get("source"):
            sources.append(item["source"])
        sources.extend(_as_list(item.get("sources")))
        if item.get("tier"):
            tiers.append(item["tier"])
        if item.get("as_of") and str(item["as_of"]) > str(result["_meta"].get("as_of") or ""):
            result["_meta"]["as_of"] = item["as_of"]
    result["_meta"]["sources"] = sorted({str(value).strip() for value in sources if str(value).strip()})
    if tiers:
        result["_meta"]["tier"] = max(tiers, key=lambda value: TIER_RANK.get(str(value), 0))
    return result


def merge_resource_profile(block: Any, evidence: Mapping[str, Any]) -> dict[str, Any]:
    work = normalize_resource_profile(block)
    incoming = normalize_resource_profile(evidence)
    for field in (
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
    ):
        work[field] = list(work.get(field) or []) + list(incoming.get(field) or [])
    if incoming.get("maintainer_status"):
        work["maintainer_status"] = incoming["maintainer_status"]
    meta = work.setdefault("_meta", {})
    incoming_meta = incoming.get("_meta") or {}
    meta["sources"] = sorted(
        {
            str(value).strip()
            for value in [*_as_list(meta.get("sources")), *_as_list(incoming_meta.get("sources"))]
            if str(value).strip()
        }
    )
    meta["as_of"] = max(str(meta.get("as_of") or ""), str(incoming_meta.get("as_of") or ""))
    if TIER_RANK.get(str(incoming_meta.get("tier") or ""), 0) > TIER_RANK.get(str(meta.get("tier") or ""), 0):
        meta["tier"] = incoming_meta.get("tier")
    return normalize_resource_profile(work)


def resource_profile_summary(block: Any) -> dict[str, Any]:
    profile = normalize_resource_profile(block)
    description = (profile.get("descriptions") or [{}])[0]
    organizations = profile.get("organizations") or []
    return {
        "description": str(description.get("text") or ""),
        "description_source": str(description.get("source") or ""),
        "description_source_url": str(description.get("source_url") or ""),
        "resource_type_count": len(profile.get("resource_types") or []),
        "resource_types": "; ".join(item.get("name", "") for item in profile.get("resource_types") or [] if item.get("name")),
        "data_asset_type_count": len(profile.get("data_asset_types") or []),
        "data_asset_types": "; ".join(item.get("name", "") for item in profile.get("data_asset_types") or [] if item.get("name")),
        "subject_area_count": len(profile.get("subject_areas") or []),
        "subject_areas": "; ".join(item.get("name", "") for item in profile.get("subject_areas") or [] if item.get("name")),
        "data_format_count": len(profile.get("data_formats") or []),
        "data_formats": "; ".join(item.get("name", "") for item in profile.get("data_formats") or [] if item.get("name")),
        "organization_count": len(organizations),
        "organizations": "; ".join(item.get("name", "") for item in organizations if item.get("name")),
        "keyword_count": len(profile.get("keywords") or []),
        "keywords": "; ".join(item.get("name", "") for item in profile.get("keywords") or [] if item.get("name")),
        "languages": "; ".join(item.get("name", "") for item in profile.get("languages") or [] if item.get("name")),
        "service_roles": "; ".join(item.get("name", "") for item in profile.get("service_roles") or [] if item.get("name")),
        "maintainer_recorded": int(bool((profile.get("maintainer_status") or {}).get("value"))),
    }


def _mapping_name(value: Any) -> str:
    if isinstance(value, Mapping):
        return _clean_text(value.get("name") or value.get("label") or value.get("title") or value.get("value"), max_chars=300)
    return _clean_text(value, max_chars=300)


def _iter_jsonld_entities(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        if "@graph" in value:
            yield from _iter_jsonld_entities(value.get("@graph"))
        yield value
        for child in value.values():
            if isinstance(child, (Mapping, list)):
                yield from _iter_jsonld_entities(child)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_jsonld_entities(item)


def parse_website_metadata(
    content: bytes | str,
    *,
    requested_url: str,
    final_url: str = "",
    content_sha256: str = "",
    as_of: str = "",
) -> dict[str, Any]:
    """Extract bounded, structured homepage metadata.

    Structured metadata tags, JSON-LD, and explicit link relations are
    preferred.  When they contain no description, the extractor may use the
    first substantive paragraph inside an explicit main/article region. Footer,
    navigation, form, and unstructured organization/contact text are ignored so
    an arbitrary copyright holder or person is not mislabeled as a maintainer.
    """
    if BeautifulSoup is None:
        raise RuntimeError("beautifulsoup4 is required for website metadata extraction")
    raw = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
    page_url = safe_url(final_url) or safe_url(requested_url)
    source = f"website:{content_sha256}" if content_sha256 else f"website:{page_url}"
    stamp = as_of or _now_month()
    soup = BeautifulSoup(raw, "html.parser")
    evidence: dict[str, Any] = {
        "_meta": {"as_of": stamp, "tier": "attestation", "sources": [source]},
        "resource_types": [],
        "data_asset_types": [],
        "descriptions": [],
        "organizations": [],
        "subject_areas": [],
        "data_formats": [],
        "keywords": [],
        "languages": [],
        "service_roles": [],
        "relevant_links": [],
        "standards": [],
        "api_families": [],
        "api_documentation_links": [],
    }
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        term = normalize_term(html_tag.get("lang"), source=source, tier="attestation", scheme="BCP 47", as_of=stamp)
        if term:
            evidence["languages"].append(term)

    meta_values: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = str(tag.get("name") or tag.get("property") or "").strip().lower()
        value = str(tag.get("content") or "").strip()
        if key and value and key not in meta_values:
            meta_values[key] = value
    for key in ("description", "dc.description", "dcterms.description", "og:description", "twitter:description"):
        if key in meta_values:
            description = normalize_description(
                meta_values[key],
                source=source,
                tier="attestation",
                source_url=page_url,
                as_of=stamp,
                extraction_method=f"meta:{key}",
                content_sha256=content_sha256,
            )
            if description:
                evidence["descriptions"].append(description)
    for keyword in _names_from(meta_values.get("keywords")):
        for part in re.split(r"\s*,\s*", keyword):
            term = normalize_term(part, source=source, tier="attestation", scheme="website keywords", as_of=stamp)
            if term:
                evidence["keywords"].append(term)

    jsonld_payloads: list[Any] = []
    for script in soup.find_all("script"):
        if "ld+json" not in str(script.get("type") or "").lower():
            continue
        text = script.string or script.get_text(" ", strip=True)
        if not text:
            continue
        try:
            jsonld_payloads.append(json.loads(text))
        except json.JSONDecodeError:
            continue

    organization_fields = {
        "publisher": "publisher",
        "provider": "provider",
        "creator": "creator",
        "maintainer": "maintainer",
        "funder": "funder",
        "sourceOrganization": "source organization",
        "parentOrganization": "parent organization",
        "producer": "producer",
        "hostingOrganization": "hosting organization",
        "sponsor": "sponsor",
        "fundingSource": "funder",
    }
    for payload in jsonld_payloads:
        for entity in _iter_jsonld_entities(payload):
            entity_types = _names_from(entity.get("@type"))
            for entity_type in entity_types:
                resource_type = _canonical_resource_type(entity_type)
                if (
                    (resource_type and resource_type != entity_type)
                    or _norm_key(entity_type) in RESOURCE_TYPE_MAP
                ):
                    term = normalize_term(resource_type, source=source, tier="attestation", scheme="Schema.org @type", as_of=stamp, canonicalizer=_canonical_resource_type)
                    if term:
                        evidence["resource_types"].append(term)
                asset_type = _canonical_data_asset_type(entity_type)
                if _norm_key(entity_type) in DATA_ASSET_TYPE_MAP:
                    term = normalize_term(asset_type, source=source, tier="attestation", scheme="Schema.org @type", as_of=stamp, canonicalizer=_canonical_data_asset_type)
                    if term:
                        evidence["data_asset_types"].append(term)
            description = normalize_description(
                entity.get("description"),
                source=source,
                tier="attestation",
                source_url=page_url,
                as_of=stamp,
                extraction_method="jsonld:description",
                content_sha256=content_sha256,
            )
            if description:
                evidence["descriptions"].append(description)
            for field, role in organization_fields.items():
                for value in _as_list(entity.get(field)):
                    organization = normalize_organization(
                        value,
                        roles=[role],
                        source=source,
                        tier="attestation",
                        as_of=stamp,
                    )
                    if organization:
                        evidence["organizations"].append(organization)
            for value in _as_list(entity.get("about")):
                term = normalize_term(value, source=source, tier="attestation", scheme="Schema.org about", as_of=stamp)
                if term:
                    evidence["subject_areas"].append(term)
            for value in _as_list(entity.get("keywords")):
                for part in re.split(r"\s*[,;|]\s*", _mapping_name(value)):
                    term = normalize_term(part, source=source, tier="attestation", scheme="Schema.org keywords", as_of=stamp)
                    if term:
                        evidence["keywords"].append(term)
            for field in ("encodingFormat", "fileFormat"):
                for value in _as_list(entity.get(field)):
                    term = normalize_term(value, source=source, tier="attestation", scheme="Schema.org encodingFormat", as_of=stamp)
                    if term:
                        evidence["data_formats"].append(term)
            for value in _as_list(entity.get("conformsTo")):
                if isinstance(value, Mapping):
                    name = _mapping_name(value) or safe_url(value.get("@id") or value.get("url"))
                    url = safe_url(value.get("url") or value.get("@id"), base_url=page_url)
                else:
                    name = _mapping_name(value)
                    url = safe_url(value, base_url=page_url)
                if name:
                    evidence["standards"].append(
                        {
                            "name": name,
                            "url": url,
                            "role": "conformsTo",
                            "declared": True,
                            "verified": False,
                            "source": source,
                            "tier": "attestation",
                            "as_of": stamp,
                            "evidence_url": page_url,
                        }
                    )

    if not evidence["descriptions"]:
        rejected_phrases = (
            "cookie",
            "privacy policy",
            "enable javascript",
            "sign in",
            "log in",
            "skip to",
        )
        chosen = None
        for selector in ("main p", "article p", '[role="main"] p'):
            for node in soup.select(selector):
                if node.find_parent(["nav", "footer", "aside", "form"]):
                    continue
                text = _clean_text(node.get_text(" ", strip=True), max_chars=900)
                lower = text.lower()
                if len(text) < 60 or any(phrase in lower for phrase in rejected_phrases):
                    continue
                link_text = " ".join(
                    anchor.get_text(" ", strip=True) for anchor in node.find_all("a")
                )
                if link_text and len(link_text) / max(1, len(text)) > 0.55:
                    continue
                chosen = (text, selector)
                break
            if chosen:
                break
        if chosen:
            description = normalize_description(
                chosen[0],
                source=source,
                tier="attestation",
                source_url=page_url,
                as_of=stamp,
                extraction_method=f"html:{chosen[1]}:first_paragraph",
                content_sha256=content_sha256,
            )
            if description:
                evidence["descriptions"].append(description)

    for tag in soup.find_all("link"):
        rels = {str(value).lower() for value in _as_list(tag.get("rel"))}
        href = safe_url(tag.get("href"), base_url=page_url)
        media_type = str(tag.get("type") or "").lower()
        if not href:
            continue
        if rels & {"service-desc", "service-doc"}:
            evidence["api_documentation_links"].append(
                {
                    "label": "API service description" if "service-desc" in rels else "API documentation",
                    "type": "api_documentation",
                    "url": href,
                    "source": source,
                    "tier": "attestation",
                    "as_of": stamp,
                }
            )
        if "alternate" in rels and any(token in media_type for token in ("json", "xml", "rdf")):
            evidence["relevant_links"].append(
                {
                    "label": f"Machine-readable metadata ({media_type or 'alternate'})",
                    "type": "machine_readable_metadata",
                    "url": href,
                    "source": source,
                    "tier": "attestation",
                    "as_of": stamp,
                }
            )
        if "describedby" in rels:
            evidence["relevant_links"].append(
                {
                    "label": "Description / schema",
                    "type": "description_or_schema",
                    "url": href,
                    "source": source,
                    "tier": "attestation",
                    "as_of": stamp,
                }
            )

    profile = normalize_resource_profile(evidence)
    profile["standards"] = evidence["standards"]
    profile["api_families"] = sorted(set(evidence["api_families"]))
    profile["api_documentation_links"] = _dedupe_records(
        [item for item in evidence["api_documentation_links"] if item.get("url")],
        ("url",),
    )
    return profile


def _find_values(value: Any, keys: set[str]) -> list[Any]:
    results: list[Any] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _norm_key(key) in keys:
                results.append(child)
            if isinstance(child, (Mapping, list)):
                results.extend(_find_values(child, keys))
    elif isinstance(value, list):
        for child in value:
            results.extend(_find_values(child, keys))
    return results


def parse_fairsharing_record(record: Mapping[str, Any], *, doi: str = "", as_of: str = "") -> dict[str, Any]:
    attributes = record.get("attributes") if isinstance(record.get("attributes"), Mapping) else record
    metadata = attributes.get("metadata") if isinstance(attributes.get("metadata"), Mapping) else {}
    merged: dict[str, Any] = {**dict(metadata), **dict(attributes)}
    resolved_doi = str(doi or merged.get("doi") or "").strip()
    source = f"fairsharing:{resolved_doi}" if resolved_doi else "fairsharing"
    source_url = f"https://fairsharing.org/{resolved_doi}" if resolved_doi else ""
    stamp = str(as_of or merged.get("updatedAt") or merged.get("updated_at") or _now_month())[:10]
    evidence: dict[str, Any] = {
        "_meta": {"as_of": stamp, "tier": "registry", "sources": [source]},
        "resource_types": [],
        "data_asset_types": [],
        "descriptions": [],
        "organizations": [],
        "subject_areas": [],
        "data_formats": [],
        "taxonomies": [],
        "keywords": [],
        "languages": [],
        "service_roles": [],
        "access_conditions": [],
        "relevant_links": [],
        "maintainer_status": None,
        "standards": [],
        "api_families": [],
        "api_documentation_links": [],
    }
    for value in [merged.get("registry"), merged.get("fairsharingRegistry"), merged.get("type")]:
        for name in _names_from(value):
            term = normalize_term(name, source=source, tier="registry", scheme="FAIRsharing record type", as_of=stamp, canonicalizer=_canonical_resource_type)
            if term:
                evidence["resource_types"].append(term)
    for value in _find_values(merged, {"objecttypes", "object types", "object type", "data types", "data type"}):
        for name in _names_from(value):
            term = normalize_term(name, source=source, tier="registry", scheme="FAIRsharing object type", as_of=stamp, canonicalizer=_canonical_data_asset_type)
            if term:
                evidence["data_asset_types"].append(term)
    for value in _find_values(merged, {"description", "summary"}):
        for candidate in _as_list(value):
            description = normalize_description(candidate, source=source, tier="registry", source_url=source_url, as_of=stamp, extraction_method="fairsharing_record")
            if description:
                evidence["descriptions"].append(description)
    for field_keys, scheme in (
        ({"subjects", "subject"}, "FAIRsharing subject"),
        ({"domains", "domain"}, "FAIRsharing domain"),
    ):
        for value in _find_values(merged, field_keys):
            for entry in _as_list(value):
                term = normalize_term(entry, source=source, tier="registry", scheme=scheme, as_of=stamp)
                if term:
                    evidence["subject_areas"].append(term)
    for value in _find_values(merged, {"taxonomies", "taxonomy", "species"}):
        for entry in _as_list(value):
            term = normalize_term(entry, source=source, tier="registry", scheme="FAIRsharing taxonomy", as_of=stamp)
            if term:
                evidence["taxonomies"].append(term)
    for value in _find_values(merged, {"userdefinedtags", "user defined tags", "tags", "keywords"}):
        for entry in _as_list(value):
            term = normalize_term(entry, source=source, tier="registry", scheme="FAIRsharing tag", as_of=stamp)
            if term:
                evidence["keywords"].append(term)

    organization_values = []
    organization_values.extend(_find_values(merged, {"organisationlinks", "organizationlinks", "organisation links", "organization links"}))
    organization_values.extend(_find_values(merged, {"organisations", "organizations"}))
    for value in organization_values:
        for entry in _as_list(value):
            if isinstance(entry, Mapping):
                organization = entry.get("organisation") or entry.get("organization") or entry
                relation = entry.get("relation") or entry.get("role") or entry.get("relationName") or entry.get("relation_name")
                roles = _names_from(relation)
            else:
                organization = entry
                roles = []
            item = normalize_organization(organization, roles=roles, source=source, tier="registry", as_of=stamp)
            if item:
                evidence["organizations"].append(item)
    maintained_values = _find_values(merged, {"ismaintained", "is maintained", "maintained"})
    maintainer_values = _find_values(merged, {"maintainers", "maintainer"})
    maintained = any(bool(value) and str(value).lower() not in {"false", "0", "no"} for value in maintained_values) or any(_as_list(value) for value in maintainer_values)
    if maintained_values or maintainer_values:
        evidence["maintainer_status"] = {
            "value": maintained,
            "source": source,
            "tier": "registry",
            "as_of": stamp,
            "note": "FAIRsharing reports whether the record has a maintainer; personal account details are not republished.",
        }

    for value in _find_values(merged, {"accessmethods", "access methods", "access method"}):
        for name in _names_from(value):
            term = normalize_term(name, source=source, tier="registry", scheme="FAIRsharing access method", as_of=stamp)
            if term:
                evidence["service_roles"].append(term)
            normalized = _norm_key(name)
            if normalized in {"rest", "oai pmh", "oai-pmh", "sparql", "soap", "ftp", "graphql"}:
                evidence["api_families"].append(name)
            elif normalized == "bioschemas":
                evidence["standards"].append(
                    {
                        "name": "Bioschemas",
                        "url": "https://bioschemas.org/",
                        "role": "machine-readable resource metadata",
                        "declared": True,
                        "verified": False,
                        "source": source,
                        "tier": "registry",
                        "as_of": stamp,
                        "evidence_url": source_url,
                    }
                )
    for value in _find_values(merged, {"dataaccesscondition", "data access condition", "access condition"}):
        for name in _names_from(value):
            term = normalize_term(name, source=source, tier="registry", scheme="FAIRsharing data access condition", as_of=stamp)
            if term:
                evidence["access_conditions"].append(term)

    support_values = _find_values(
        merged,
        {
            "supportlinks",
            "support links",
            "support",
            "crossreferences",
            "cross references",
        },
    )
    for value in support_values:
        for entry in _as_list(value):
            if isinstance(entry, Mapping):
                url = safe_url(
                    entry.get("link")
                    or entry.get("url")
                    or entry.get("homepage")
                    or entry.get("href")
                )
                link_type = _mapping_name(
                    entry.get("type") or entry.get("supportType") or entry.get("category")
                )
                label = _mapping_name(entry.get("name") or entry.get("label")) or link_type or "FAIRsharing support link"
            else:
                url = safe_url(entry)
                link_type = ""
                label = "FAIRsharing support link"
            if not url:
                continue
            related = normalize_link(
                {"label": label, "type": link_type or "support", "url": url},
                source=source,
                tier="registry",
                as_of=stamp,
            )
            if related:
                evidence["relevant_links"].append(related)
            normalized_type = _norm_key(f"{link_type} {label}")
            if any(token in normalized_type for token in ("api", "developer", "programmatic", "technical documentation")):
                evidence["api_documentation_links"].append(
                    {
                        "label": label,
                        "type": "api_documentation",
                        "url": url,
                        "source": source,
                        "tier": "registry",
                        "as_of": stamp,
                    }
                )

    associations = _find_values(merged, {
        "recordassociations",
        "record associations",
        "standards",
        "relatedstandards",
        "related standards",
        "dataformatsandterminologies",
        "data formats and terminologies",
    })
    for value in associations:
        for entry in _as_list(value):
            if not isinstance(entry, Mapping):
                name = _mapping_name(entry)
                linked = {}
            else:
                linked = entry.get("record") or entry.get("fairsharingRecord") or entry.get("target") or entry.get("linkedRecord") or entry
                name = _mapping_name(linked)
            registry = _norm_key(linked.get("registry") if isinstance(linked, Mapping) else "")
            relation = _mapping_name(entry.get("relation") if isinstance(entry, Mapping) else "")
            linked_type = _mapping_name(
                linked.get("type") or linked.get("recordType")
                if isinstance(linked, Mapping)
                else ""
            )
            standard_context = " ".join(
                [registry, _norm_key(relation), _norm_key(linked_type), _norm_key(name)]
            )
            if not any(token in standard_context for token in ("standard", "format", "terminology", "model")):
                continue
            if name:
                linked_doi = str(linked.get("doi") or "").strip() if isinstance(linked, Mapping) else ""
                evidence["standards"].append(
                    {
                        "name": name,
                        "url": f"https://fairsharing.org/{linked_doi}" if linked_doi else safe_url(linked.get("homepage") if isinstance(linked, Mapping) else ""),
                        "role": relation or linked_type or "associated standard",
                        "declared": True,
                        "verified": False,
                        "source": source,
                        "tier": "registry",
                        "as_of": stamp,
                        "evidence_url": source_url,
                    }
                )
                if "format" in _norm_key(f"{linked_type} {relation}"):
                    term = normalize_term(
                        name,
                        source=source,
                        tier="registry",
                        scheme="FAIRsharing model/format association",
                        as_of=stamp,
                    )
                    if term:
                        evidence["data_formats"].append(term)
    if source_url:
        evidence["relevant_links"].append(
            {"label": "FAIRsharing registry record", "type": "registry_record", "url": source_url, "source": source, "tier": "registry", "as_of": stamp}
        )
    profile = normalize_resource_profile(evidence)
    profile["standards"] = evidence["standards"]
    profile["api_families"] = sorted({str(value).strip() for value in evidence["api_families"] if str(value).strip()})
    profile["api_documentation_links"] = _dedupe_records(
        [item for item in evidence["api_documentation_links"] if item.get("url")],
        ("url",),
    )
    return profile


def parse_re3data_profile(record: Mapping[str, Any], *, r3d_id: str = "") -> dict[str, Any]:
    resolved_id = str(r3d_id or record.get("r3d_id") or "").strip()
    source = f"re3data:{resolved_id}" if resolved_id else "re3data"
    source_url = str(record.get("record_url") or (f"https://www.re3data.org/repository/{resolved_id}" if resolved_id else ""))
    stamp = str(record.get("fetched_at") or _now_month())[:10]
    evidence: dict[str, Any] = {
        "_meta": {"as_of": stamp, "tier": "registry", "sources": [source]},
        "resource_types": [],
        "data_asset_types": [],
        "descriptions": [],
        "organizations": [],
        "subject_areas": [],
        "data_formats": [],
        "taxonomies": [],
        "keywords": [],
        "languages": [],
        "service_roles": [],
        "access_conditions": [],
        "relevant_links": [],
    }
    base_type = normalize_term(
        "Data repository",
        source=source,
        tier="registry",
        scheme="re3data registry",
        as_of=stamp,
        canonicalizer=_canonical_resource_type,
    )
    if base_type:
        evidence["resource_types"].append(base_type)
    for value in record.get("repository_types") or []:
        term = normalize_term(value, source=source, tier="registry", scheme="re3data repository type", as_of=stamp, canonicalizer=_canonical_resource_type)
        if term:
            evidence["resource_types"].append(term)
    for value in record.get("content_types") or []:
        term = normalize_term(value, source=source, tier="registry", scheme="re3data content type", as_of=stamp, canonicalizer=_canonical_data_asset_type)
        if term:
            evidence["data_asset_types"].append(term)
    description = normalize_description(record.get("description"), source=source, tier="registry", source_url=source_url, as_of=stamp, extraction_method="re3data_description")
    if description:
        evidence["descriptions"].append(description)
    for value in record.get("subjects") or []:
        term = normalize_term(value, source=source, tier="registry", scheme="re3data subject", as_of=stamp)
        if term:
            evidence["subject_areas"].append(term)
    for value in record.get("keywords") or []:
        term = normalize_term(value, source=source, tier="registry", scheme="re3data keyword", as_of=stamp)
        if term:
            evidence["keywords"].append(term)
    for value in record.get("languages") or []:
        term = normalize_term(value, source=source, tier="registry", scheme="re3data repository language", as_of=stamp)
        if term:
            evidence["languages"].append(term)
    for value in record.get("service_roles") or []:
        term = normalize_term(value, source=source, tier="registry", scheme="re3data provider role", as_of=stamp)
        if term:
            evidence["service_roles"].append(term)
    for value in record.get("access_conditions") or []:
        term = normalize_term(value, source=source, tier="registry", scheme="re3data access condition", as_of=stamp)
        if term:
            evidence["access_conditions"].append(term)
    for value in record.get("institutions") or []:
        organization = normalize_organization(value, source=source, tier="registry", as_of=stamp)
        if organization:
            evidence["organizations"].append(organization)
    if source_url:
        evidence["relevant_links"].append(
            {"label": "re3data registry record", "type": "registry_record", "url": source_url, "source": source, "tier": "registry", "as_of": stamp}
        )
    return normalize_resource_profile(evidence)
