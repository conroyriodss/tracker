"""Canonical interoperability schema helpers for NIH Tracker cards.

The public card model keeps participant GUID systems distinct from repository
PIDs. It also preserves evidence level for API endpoints and standards instead
of treating a missing field as proof that a capability does not exist.
"""
from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

SCHEMA_VERSION = "2.0"

STANDARD_CATALOG = {
    "openapi": {
        "name": "OpenAPI Specification",
        "category": "api_description",
        "url": "https://spec.openapis.org/oas/latest.html",
    },
    "swagger": {
        "name": "OpenAPI Specification",
        "category": "api_description",
        "url": "https://spec.openapis.org/oas/latest.html",
    },
    "graphql": {
        "name": "GraphQL Specification",
        "category": "api_query",
        "url": "https://spec.graphql.org/",
    },
    "oai-pmh": {
        "name": "OAI-PMH",
        "category": "metadata_harvesting",
        "url": "https://www.openarchives.org/OAI/openarchivesprotocol.html",
    },
    "oaipmh": {
        "name": "OAI-PMH",
        "category": "metadata_harvesting",
        "url": "https://www.openarchives.org/OAI/openarchivesprotocol.html",
    },
    "sparql": {
        "name": "SPARQL 1.1",
        "category": "query_language",
        "url": "https://www.w3.org/TR/sparql11-query/",
    },
    "fhir": {
        "name": "HL7 FHIR",
        "category": "health_data_exchange",
        "url": "https://hl7.org/fhir/",
    },
    "hl7 fhir": {
        "name": "HL7 FHIR",
        "category": "health_data_exchange",
        "url": "https://hl7.org/fhir/",
    },
    "ga4gh drs": {
        "name": "GA4GH Data Repository Service (DRS)",
        "category": "data_access_api",
        "url": "https://www.ga4gh.org/product/data-repository-service-drs/",
    },
    "drs": {
        "name": "GA4GH Data Repository Service (DRS)",
        "category": "data_access_api",
        "url": "https://www.ga4gh.org/product/data-repository-service-drs/",
    },
    "ga4gh wes": {
        "name": "GA4GH Workflow Execution Service (WES)",
        "category": "workflow_api",
        "url": "https://www.ga4gh.org/product/workflow-execution-service-wes/",
    },
    "wes": {
        "name": "GA4GH Workflow Execution Service (WES)",
        "category": "workflow_api",
        "url": "https://www.ga4gh.org/product/workflow-execution-service-wes/",
    },
    "ga4gh trs": {
        "name": "GA4GH Tool Registry Service (TRS)",
        "category": "workflow_registry_api",
        "url": "https://www.ga4gh.org/product/tool-registry-service-trs/",
    },
    "trs": {
        "name": "GA4GH Tool Registry Service (TRS)",
        "category": "workflow_registry_api",
        "url": "https://www.ga4gh.org/product/tool-registry-service-trs/",
    },
    "ga4gh beacon": {
        "name": "GA4GH Beacon",
        "category": "genomics_discovery_api",
        "url": "https://www.ga4gh.org/product/beacon-api/",
    },
    "beacon": {
        "name": "GA4GH Beacon",
        "category": "genomics_discovery_api",
        "url": "https://www.ga4gh.org/product/beacon-api/",
    },
    "dcat": {
        "name": "DCAT 3",
        "category": "catalog_metadata",
        "url": "https://www.w3.org/TR/vocab-dcat-3/",
    },
    "datacite": {
        "name": "DataCite Metadata Schema",
        "category": "dataset_metadata",
        "url": "https://schema.datacite.org/",
    },
    "datacite metadata schema": {
        "name": "DataCite Metadata Schema",
        "category": "dataset_metadata",
        "url": "https://schema.datacite.org/",
    },
    "dublin core": {
        "name": "Dublin Core Metadata Terms",
        "category": "metadata",
        "url": "https://www.dublincore.org/specifications/dublin-core/dcmi-terms/",
    },
    "dcmi": {
        "name": "Dublin Core Metadata Terms",
        "category": "metadata",
        "url": "https://www.dublincore.org/specifications/dublin-core/dcmi-terms/",
    },
    "schema.org": {
        "name": "Schema.org",
        "category": "web_metadata",
        "url": "https://schema.org/",
    },
    "bioschemas": {
        "name": "Bioschemas",
        "category": "life_science_web_metadata",
        "url": "https://bioschemas.org/",
    },
    "ro-crate": {
        "name": "RO-Crate",
        "category": "research_object_packaging",
        "url": "https://www.researchobject.org/ro-crate/",
    },
    "bagit": {
        "name": "BagIt",
        "category": "data_packaging",
        "url": "https://www.rfc-editor.org/rfc/rfc8493",
    },
    "dats": {
        "name": "Data Tag Suite (DATS)",
        "category": "dataset_metadata",
        "url": "https://github.com/datatagsuite",
    },
}

API_FAMILY_ALIASES = {
    "rest": "REST",
    "restful": "REST",
    "graphql": "GraphQL",
    "sparql": "SPARQL",
    "oai-pmh": "OAI-PMH",
    "oaipmh": "OAI-PMH",
    "fhir": "FHIR",
    "ga4gh drs": "GA4GH DRS",
    "drs": "GA4GH DRS",
    "ga4gh wes": "GA4GH WES",
    "wes": "GA4GH WES",
    "ga4gh trs": "GA4GH TRS",
    "trs": "GA4GH TRS",
    "beacon": "GA4GH Beacon",
    "ftp": "FTP",
    "sftp": "SFTP",
    "s3": "S3-compatible object storage",
    "bulk download": "Bulk download",
}

PID_CATALOG = {
    "doi": ("DOI", "https://www.doi.org/"),
    "handle": ("Handle", "https://www.handle.net/"),
    "ark": ("ARK", "https://arks.org/"),
    "accession": ("Accession", ""),
    "rrid": ("RRID", "https://www.rrids.org/"),
    "urn": ("URN", "https://www.rfc-editor.org/rfc/rfc8141"),
    "uuid": ("UUID", "https://www.rfc-editor.org/rfc/rfc9562"),
    "identifiers.org": ("Identifiers.org URI", "https://identifiers.org/"),
}

GUID_SEEDS = {
    "R1": [
        {
            "name": "FITBIR Global Unique Identifier (GUID)",
            "scope": "research_participant",
            "format": "random alphanumeric participant identifier",
            "documentation_url": "https://fitbir.nih.gov/content/global-unique-identifier",
            "tool_url": "https://fitbir.nih.gov/content/global-unique-identifier",
            "privacy_characteristics": "PII remains local; one-way hashes are used for matching and the returned GUID is not generated from PII.",
            "source": "official_resource_documentation",
            "tier": "verified",
            "as_of": "2026-09",
        }
    ],
    "R103": [
        {
            "name": "NDA Global Unique Identifier (GUID)",
            "scope": "research_participant",
            "format": "alphanumeric participant identifier",
            "documentation_url": "https://nda.nih.gov/s/guid/nda-guid.html",
            "tool_url": "https://nda.nih.gov/s/guid/nda-guid.html",
            "privacy_characteristics": "De-identified subject identifier used to link participants within and across NDA datasets.",
            "source": "official_resource_documentation",
            "tier": "verified",
            "as_of": "2026-09",
        }
    ],
}


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def safe_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple) or isinstance(value, set):
        return list(value)
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[;|]", value) if item.strip()]
    return [value]


def _norm_key(value: Any) -> str:
    text = re.sub(r"[_/]+", " ", str(value or "").strip().lower())
    text = re.sub(r"[^a-z0-9.+-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_api_family(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    key = _norm_key(text)
    for alias, canonical in API_FAMILY_ALIASES.items():
        if key == _norm_key(alias):
            return canonical
    if "graphql" in key:
        return "GraphQL"
    if "sparql" in key:
        return "SPARQL"
    if "oai" in key and "pmh" in key:
        return "OAI-PMH"
    if "fhir" in key:
        return "FHIR"
    if "rest" in key or "json" in key or "openapi" in key or "swagger" in key:
        return "REST"
    return text


def canonical_standard(value: Any, *, source: str = "", tier: str = "") -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        raw = str(value.get("name") or value.get("standard") or value.get("value") or "").strip()
        item = copy.deepcopy(dict(value))
    else:
        raw = str(value or "").strip()
        item = {"name": raw}
    if not raw:
        return None
    key = _norm_key(raw)
    match = None
    for alias, record in STANDARD_CATALOG.items():
        alias_key = _norm_key(alias)
        if key == alias_key or alias_key in key:
            match = record
            break
    if match:
        item["name"] = match["name"]
        item.setdefault("category", match["category"])
        if not safe_url(item.get("url")):
            item["url"] = match["url"]
    else:
        item["name"] = raw
        item.setdefault("category", "other")
        item["url"] = safe_url(item.get("url"))
    if source and not item.get("source"):
        item["source"] = source
    if tier and not item.get("tier"):
        item["tier"] = tier
    item["declared"] = truthy(item.get("declared"))
    item["verified"] = truthy(item.get("verified"))
    item["url"] = safe_url(item.get("url"))
    return item


def normalize_endpoint(value: Any, *, source: str = "", tier: str = "") -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        item = copy.deepcopy(dict(value))
        url = safe_url(item.get("url") or item.get("endpoint_url") or item.get("endpoint") or item.get("api_url"))
    else:
        url = safe_url(value)
        item = {"url": url}
    if not url:
        return None
    item["url"] = url
    family = canonical_api_family(item.get("family") or item.get("type") or item.get("api_type"))
    if family:
        item["family"] = family
    doc_url = safe_url(item.get("documentation_url") or item.get("docs_url") or item.get("documentation"))
    if doc_url:
        item["documentation_url"] = doc_url
    else:
        item.pop("documentation_url", None)
    item["declared"] = truthy(item.get("declared"))
    item["verified"] = truthy(item.get("verified"))
    if source and not item.get("source"):
        item["source"] = source
    if tier and not item.get("tier"):
        item["tier"] = tier
    return item


def normalize_link(value: Any, *, source: str = "", tier: str = "") -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    item = copy.deepcopy(dict(value))
    url = safe_url(item.get("url"))
    if not url:
        return None
    item["url"] = url
    item["label"] = str(item.get("label") or item.get("type") or "Link").strip()
    item["type"] = str(item.get("type") or "related").strip()
    if source and not item.get("source"):
        item["source"] = source
    if tier and not item.get("tier"):
        item["tier"] = tier
    return item


def normalize_pid_system(value: Any, *, source: str = "", tier: str = "") -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        item = copy.deepcopy(dict(value))
        raw = str(item.get("name") or item.get("value") or item.get("system") or "").strip()
    else:
        raw = str(value or "").strip()
        item = {"name": raw}
    if not raw:
        return None
    key = _norm_key(raw)
    catalog = None
    for alias, record in PID_CATALOG.items():
        if _norm_key(alias) == key or _norm_key(alias) in key:
            catalog = record
            break
    if catalog:
        item["name"] = catalog[0]
        if not safe_url(item.get("url")) and catalog[1]:
            item["url"] = catalog[1]
    else:
        item["name"] = raw
        item["url"] = safe_url(item.get("url"))
    item.setdefault("scope", "repository_items")
    if source and not item.get("source"):
        item["source"] = source
    if tier and not item.get("tier"):
        item["tier"] = tier
    return item


def normalize_guid_system(value: Any, *, source: str = "", tier: str = "") -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    item = copy.deepcopy(dict(value))
    name = str(item.get("name") or item.get("system") or "").strip()
    if not name:
        return None
    item["name"] = name
    item.setdefault("scope", "research_participant")
    for field in ("documentation_url", "tool_url"):
        item[field] = safe_url(item.get(field))
    if not item.get("documentation_url") and not item.get("tool_url"):
        return None
    if source and not item.get("source"):
        item["source"] = source
    if tier and not item.get("tier"):
        item["tier"] = tier
    return item


def _dedupe(items: Iterable[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    selected: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in items:
        key = tuple(str(item.get(field) or "").strip().lower() for field in fields)
        if not any(key):
            continue
        current = selected.get(key)
        if current is None:
            selected[key] = item
            continue
        merged = dict(current)
        for name, value in item.items():
            if value not in (None, "", [], False) or name not in merged:
                merged[name] = value
        selected[key] = merged
    return [selected[key] for key in sorted(selected)]


def normalize_interoperability(block: Any, *, repo_uid: str = "") -> dict[str, Any]:
    source_block = copy.deepcopy(dict(block)) if isinstance(block, Mapping) else {}
    meta = copy.deepcopy(source_block.get("_meta") or {})
    meta["block_version"] = SCHEMA_VERSION
    sources = _as_list(meta.get("sources") or meta.get("source"))

    api_source = source_block.get("api") if isinstance(source_block.get("api"), Mapping) else {}
    families = []
    for value in (
        _as_list(source_block.get("api_families"))
        + _as_list(api_source.get("families"))
        + _as_list(api_source.get("api_families"))
    ):
        family = canonical_api_family(value)
        if family:
            families.append(family)

    endpoints = []
    endpoint_values = (
        _as_list(source_block.get("api_endpoints"))
        + _as_list(source_block.get("endpoints"))
        + _as_list(api_source.get("endpoints"))
    )
    for value in endpoint_values:
        item = normalize_endpoint(value)
        if item:
            endpoints.append(item)
            if item.get("family"):
                families.append(item["family"])

    doc_links = []
    for value in _as_list(api_source.get("documentation_links")) + _as_list(source_block.get("api_documentation_links")):
        item = normalize_link(value)
        if item:
            doc_links.append(item)

    standards = []
    for value in _as_list(source_block.get("standards")) + _as_list(api_source.get("standards")):
        item = canonical_standard(value)
        if item:
            standards.append(item)
    for value in _as_list(source_block.get("standards_declared")) + _as_list(api_source.get("standards_declared")):
        item = canonical_standard({"name": value, "declared": True})
        if item:
            standards.append(item)
    for value in _as_list(source_block.get("standards_verified")) + _as_list(api_source.get("standards_verified")):
        item = canonical_standard({"name": value, "verified": True})
        if item:
            standards.append(item)

    pid_systems = []
    for value in _as_list(source_block.get("pid_systems")) + _as_list(api_source.get("pid_systems")):
        item = normalize_pid_system(value)
        if item:
            pid_systems.append(item)

    guid_systems = []
    for value in _as_list(source_block.get("guid_systems")) + _as_list(api_source.get("guid_systems")):
        item = normalize_guid_system(value)
        if item:
            guid_systems.append(item)
    for value in GUID_SEEDS.get(repo_uid, []):
        item = normalize_guid_system(value)
        if item:
            guid_systems.append(item)
            sources.append(str(item.get("source") or "official_resource_documentation"))

    relevant_links = []
    for value in _as_list(source_block.get("relevant_links")) + _as_list(api_source.get("relevant_links")):
        item = normalize_link(value)
        if item:
            relevant_links.append(item)

    api_verified = truthy(source_block.get("api_verified")) or any(item.get("verified") for item in endpoints)
    api_declared = truthy(source_block.get("api_declared")) or any(item.get("declared") for item in endpoints)
    gated = truthy(source_block.get("gated_reachable"))
    explicit_state = str(source_block.get("api_evidence_state") or "").strip()
    if api_verified and (endpoints or doc_links):
        inferred_state = "verified_documented"
    elif api_verified:
        inferred_state = "verified_undocumented"
    elif gated:
        inferred_state = "gated_reachable"
    elif api_declared or endpoints or doc_links:
        inferred_state = "declared_unverified"
    else:
        inferred_state = "absent"
    state_rank = {
        "absent": 1,
        "declared_unverified": 2,
        "gated_reachable": 3,
        "verified_undocumented": 4,
        "verified_documented": 5,
    }
    state = max(
        [value for value in (explicit_state, inferred_state) if value],
        key=lambda value: state_rank.get(value, 0),
    )

    result = {
        "_meta": meta,
        "api_evidence_state": state,
        "api_verified": bool(api_verified),
        "api_declared": bool(api_declared),
        "gated_reachable": bool(gated),
        "access_class": str(source_block.get("access_class") or api_source.get("access_class") or "").strip() or "not_documented",
        "api": {
            "families": sorted(set(families), key=str.casefold),
            "endpoints": _dedupe(endpoints, ("url",)),
            "documentation_links": _dedupe(doc_links, ("url",)),
        },
        "standards": _dedupe(standards, ("name", "version")),
        "pid_systems": _dedupe(pid_systems, ("name", "scope")),
        "guid_systems": _dedupe(guid_systems, ("name", "scope")),
        "relevant_links": _dedupe(relevant_links, ("url",)),
    }
    meta["sources"] = sorted({str(value).strip() for value in sources if str(value).strip()})
    meta.pop("source", None)
    if not meta.get("as_of"):
        meta["as_of"] = max(
            [
                str(item.get("as_of") or "")
                for collection in (
                    result["api"]["endpoints"],
                    result["standards"],
                    result["pid_systems"],
                    result["guid_systems"],
                )
                for item in collection
            ]
            or [""]
        ) or None
    meta.setdefault("review_state", "auto")
    if not meta.get("tier"):
        evidence_items = (
            result["api"]["endpoints"]
            + result["standards"]
            + result["pid_systems"]
            + result["guid_systems"]
            + result["relevant_links"]
        )
        tiers = {str(item.get("tier") or "") for item in evidence_items}
        if result["api_verified"] or "verified" in tiers:
            meta["tier"] = "verified"
        elif "registry" in tiers:
            meta["tier"] = "registry"
        elif result["api_declared"] or evidence_items:
            meta["tier"] = "declared"
    return result


def interoperability_summary(block: Any, *, repo_uid: str = "") -> dict[str, Any]:
    normalized = normalize_interoperability(block, repo_uid=repo_uid)
    api = normalized["api"]
    return {
        "api_evidence_state": normalized.get("api_evidence_state"),
        "api_verified": normalized.get("api_verified"),
        "api_declared": normalized.get("api_declared"),
        "access_class": normalized.get("access_class"),
        "api_family_count": len(api.get("families") or []),
        "api_endpoint_count": len(api.get("endpoints") or []),
        "api_documentation_count": len(api.get("documentation_links") or []),
        "api_families": "; ".join(api.get("families") or []),
        "standard_count": len(normalized.get("standards") or []),
        "standards": "; ".join(str(item.get("name") or "") for item in normalized.get("standards") or [] if item.get("name")),
        "pid_system_count": len(normalized.get("pid_systems") or []),
        "pid_systems": "; ".join(str(item.get("name") or "") for item in normalized.get("pid_systems") or [] if item.get("name")),
        "guid_system_count": len(normalized.get("guid_systems") or []),
        "guid_systems": "; ".join(str(item.get("name") or "") for item in normalized.get("guid_systems") or [] if item.get("name")),
    }


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_text(node: ET.Element, names: set[str]) -> str:
    for child in node.iter():
        if _local(child.tag) in names and str(child.text or "").strip():
            return str(child.text).strip()
    return ""


def _all_text(node: ET.Element, names: set[str]) -> list[str]:
    values: list[str] = []
    for child in node.iter():
        if _local(child.tag) not in names:
            continue
        value = str(child.text or "").strip()
        if value:
            values.append(value)
    return values


def _direct_texts(node: ET.Element, names: set[str]) -> list[str]:
    values: list[str] = []
    for child in list(node):
        if _local(child.tag) not in names:
            continue
        value = str(child.text or "").strip()
        if value:
            values.append(value)
    return values


def _unique_strings(values: Iterable[Any]) -> list[str]:
    selected: dict[str, str] = {}
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        key = text.casefold()
        if text and key not in selected:
            selected[key] = text
    return [selected[key] for key in sorted(selected)]


def parse_re3data_record(xml_content: bytes | str, *, r3d_id: str = "") -> dict[str, Any]:
    """Parse re3data Schema 4.x or legacy XML into reproducible evidence.

    The parser retains descriptive repository metadata needed by both the
    interoperability and resource-profile builders. It uses local XML names so
    Schema 4.x and older namespaced records can be handled without hard-coding
    one namespace URI.
    """
    raw = xml_content.encode("utf-8") if isinstance(xml_content, str) else xml_content
    root = ET.fromstring(raw)
    source = f"re3data:{r3d_id}" if r3d_id else "re3data"
    result: dict[str, Any] = {
        "r3d_id": r3d_id,
        "name": _first_text(root, {"repositoryName"}),
        "homepage": _first_text(root, {"repositoryURL"}),
        "description": _first_text(root, {"description"}),
        "repository_types": [],
        "content_types": [],
        "subjects": [],
        "keywords": [],
        "languages": [],
        "service_roles": [],
        "access_conditions": [],
        "institutions": [],
        "api_endpoints": [],
        "standards": [],
        "pid_systems": [],
        "identifiers": [],
    }
    for node in root.iter():
        lname = _local(node.tag)
        text_value = re.sub(r"\s+", " ", str(node.text or "")).strip()
        if lname == "api":
            api_type = _first_text(node, {"apiType"})
            api_url = _first_text(node, {"apiUrl", "apiURL"})
            docs = _first_text(node, {"apiDocumentation"})
            endpoint = normalize_endpoint(
                {
                    "url": api_url,
                    "family": api_type,
                    "documentation_url": docs,
                    "declared": True,
                    "verified": False,
                    "source": source,
                    "tier": "registry",
                }
            )
            if endpoint:
                result["api_endpoints"].append(endpoint)
        elif lname == "metadataStandard":
            name = _first_text(node, {"metadataStandardName"}) or text_value
            url = _first_text(node, {"metadataStandardURL", "metadataStandardUrl"})
            standard = canonical_standard(
                {
                    "name": name,
                    "url": url,
                    "declared": True,
                    "verified": False,
                    "source": source,
                    "tier": "registry",
                    "category": "repository_reported_metadata_standard",
                    "role": "repository metadata standard",
                }
            )
            if standard:
                result["standards"].append(standard)
        elif lname == "pidSystem" and text_value:
            pid = normalize_pid_system(
                {
                    "name": text_value,
                    "source": source,
                    "tier": "registry",
                    "scope": "repository_items",
                }
            )
            if pid:
                result["pid_systems"].append(pid)
        elif lname == "repositoryIdentifier":
            value = _first_text(node, {"repositoryIdentifierValue", "value"}) or text_value
            identifier_type = _first_text(node, {"repositoryIdentifierType", "type"})
            if not identifier_type:
                identifier_type = str(
                    node.attrib.get("repositoryIdentifierType")
                    or node.attrib.get("type")
                    or ""
                ).strip()
            if value:
                result["identifiers"].append(
                    {"type": identifier_type, "value": value}
                )
        elif lname == "repositoryType" and text_value:
            result["repository_types"].append(text_value)
        elif lname == "contentType" and text_value:
            result["content_types"].append(text_value)
        elif lname == "subject" and text_value:
            scheme = str(
                node.attrib.get("subjectScheme")
                or node.attrib.get("scheme")
                or ""
            ).strip()
            subject_id = str(
                node.attrib.get("subjectId")
                or node.attrib.get("id")
                or ""
            ).strip()
            result["subjects"].append(
                {
                    "name": text_value,
                    "scheme": scheme,
                    "identifier": subject_id,
                }
            )
        elif lname == "keyword" and text_value:
            result["keywords"].append(text_value)
        elif lname in {"repositoryLanguage", "language"} and text_value:
            result["languages"].append(text_value)
        elif lname in {
            "dataAccessRestriction",
            "dataAccessType",
            "dataUploadRestriction",
            "dataUploadType",
            "databaseAccessRestriction",
            "databaseAccessType",
        } and text_value:
            result["access_conditions"].append(
                {
                    "name": text_value,
                    "category": lname,
                }
            )
        elif lname in {"providerType", "providerRole", "responsibilityType"} and text_value:
            result["service_roles"].append(text_value)
        elif lname in {"institution", "provider"}:
            name = _first_text(
                node,
                {"institutionName", "providerName", "name"},
            )
            if not name:
                continue
            responsibilities = _all_text(
                node,
                {"responsibilityType", "providerRole", "providerType"},
            )
            country = _first_text(
                node,
                {"institutionCountry", "providerCountry", "country"},
            )
            institution_type = _first_text(
                node,
                {"institutionType", "providerType"},
            )
            identifiers: dict[str, str] = {}
            identifier_nodes = [
                child
                for child in node.iter()
                if _local(child.tag) in {"institutionIdentifier", "providerIdentifier"}
            ]
            for identifier_node in identifier_nodes:
                identifier_value = _first_text(
                    identifier_node,
                    {
                        "institutionIdentifierValue",
                        "providerIdentifierValue",
                        "value",
                    },
                ) or re.sub(r"\s+", " ", str(identifier_node.text or "")).strip()
                identifier_type = _first_text(
                    identifier_node,
                    {
                        "institutionIdentifierType",
                        "providerIdentifierType",
                        "type",
                    },
                ) or str(
                    identifier_node.attrib.get("institutionIdentifierType")
                    or identifier_node.attrib.get("providerIdentifierType")
                    or identifier_node.attrib.get("type")
                    or ""
                ).strip()
                if identifier_value:
                    identifiers[identifier_type or "other"] = identifier_value
            organization = {
                "name": name,
                "roles": responsibilities,
                "country": country,
                "organization_type": institution_type,
                "identifiers": identifiers,
            }
            for key, value in identifiers.items():
                normalized_type = _norm_key(key)
                if "ror" in normalized_type:
                    organization["ror"] = value
                if "rrid" in normalized_type:
                    organization["rrid"] = value
            result["institutions"].append(organization)

    result["repository_types"] = _unique_strings(result["repository_types"])
    result["content_types"] = _unique_strings(result["content_types"])
    result["subjects"] = _dedupe(result["subjects"], ("name", "scheme", "identifier"))
    result["keywords"] = _unique_strings(result["keywords"])
    result["languages"] = _unique_strings(result["languages"])
    result["service_roles"] = _unique_strings(result["service_roles"])
    result["access_conditions"] = _dedupe(
        result["access_conditions"], ("name", "category")
    )
    result["institutions"] = _dedupe(
        result["institutions"], ("name", "country")
    )
    result["api_endpoints"] = _dedupe(result["api_endpoints"], ("url",))
    result["standards"] = _dedupe(result["standards"], ("name", "version"))
    result["pid_systems"] = _dedupe(result["pid_systems"], ("name", "scope"))
    result["identifiers"] = _dedupe(result["identifiers"], ("type", "value"))
    return result
