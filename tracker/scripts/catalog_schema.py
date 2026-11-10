"""Core schemas and deterministic identifiers for the NIH Tracker catalog.

The v19 catalog is a derived, immutable build artifact. Reviewed YAML cards and
pinned reference snapshots remain authoritative. This module intentionally has
no network behavior and no card-write behavior.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

CATALOG_SCHEMA = "nih-tracker-catalog/v1"
NODE_SCHEMA = "nih-tracker-catalog-node/v1"
EDGE_SCHEMA = "nih-tracker-catalog-edge/v1"
CLAIM_SCHEMA = "nih-tracker-catalog-claim/v1"
PUBLIC_RESOURCE_SCHEMA = "nih-tracker-public-resource/v1"
MANIFEST_SCHEMA = "nih-tracker-catalog-manifest/v1"

EVIDENCE_STATES = {
    "verified",
    "officially_reported",
    "registry_reported",
    "not_established",
}
RESOLUTION_STATUSES = {
    "selected",
    "supporting",
    "conflicting",
    "superseded",
    "not_established",
}
NODE_TYPES = {
    "Resource",
    "Organization",
    "NIHOrganizationalUnit",
    "AccessSystem",
    "APIService",
    "Standard",
    "IdentifierScheme",
    "GUIDSystem",
    "PolicyDesignation",
    "PublisherPolicy",
    "EvidenceSource",
    "ExternalIdentifier",
    "DataAsset",
    "Award",
    "Publication",
}
EDGE_PREDICATES = {
    "OPERATED_BY",
    "MAINTAINED_BY",
    "HOSTED_BY",
    "PROVIDED_BY",
    "FUNDED_BY",
    "CURRENTLY_FUNDED_BY",
    "HISTORICALLY_SUPPORTED_BY",
    "ASSOCIATED_WITH_ICO",
    "HAS_ACCESS_SYSTEM",
    "EXPOSES_API",
    "IMPLEMENTS_STANDARD",
    "SUPPORTS_IDENTIFIER_SCHEME",
    "SUPPORTS_GUID_SYSTEM",
    "HAS_POLICY_DESIGNATION",
    "MENTIONED_BY_PUBLISHER_POLICY",
    "RECOMMENDED_BY_PUBLISHER_POLICY",
    "REQUIRED_BY_PUBLISHER_POLICY",
    "PREFERRED_BY_PUBLISHER_POLICY",
    "ACCEPTABLE_FALLBACK_FOR_PUBLISHER_POLICY",
    "COMPONENT_OF",
    "CATALOGS",
    "HOSTS_DATA_ASSET",
    "MIGRATED_TO",
    "SUPERSEDES",
    "VERSION_OF",
    "SAME_AS",
    "IDENTIFIED_BY",
}


def canonical_json(value: Any) -> str:
    """Return stable JSON for hashing and byte-deterministic artifacts."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def stable_hash(value: Any, *, length: int = 24) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:length]


def slug(value: Any, *, fallback: str = "item") -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().casefold()).strip("-")
    return text[:80] or fallback


def node_id(node_type: str, natural_key: Any) -> str:
    prefix = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", node_type).lower()
    if node_type == "Resource" and re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]*", str(natural_key or "")):
        return f"resource:{natural_key}"
    readable = slug(natural_key)
    return f"{prefix}:{readable}:{stable_hash([node_type, natural_key], length=12)}"


def edge_id(subject: str, predicate: str, object_id: str) -> str:
    return f"edge:{stable_hash([subject, predicate, object_id])}"


def claim_id(
    subject: str,
    predicate: str,
    *,
    object_id: str = "",
    value: Any = None,
    source_id: str,
) -> str:
    return f"claim:{stable_hash([subject, predicate, object_id, value, source_id])}"


def _is_list_of_strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def validate_node(node: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if not str(node.get("id") or "").strip():
        errors.append("node id is required")
    if node.get("type") not in NODE_TYPES:
        errors.append(f"unsupported node type: {node.get('type')!r}")
    if not str(node.get("label") or "").strip():
        errors.append("node label is required")
    if not isinstance(node.get("properties"), dict):
        errors.append("node properties must be an object")
    if "source_claim_ids" in node and not _is_list_of_strings(node.get("source_claim_ids")):
        errors.append("node source_claim_ids must be a string array")
    return errors


def validate_edge(edge: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in ("id", "subject", "object"):
        if not str(edge.get(field) or "").strip():
            errors.append(f"edge {field} is required")
    if edge.get("predicate") not in EDGE_PREDICATES:
        errors.append(f"unsupported edge predicate: {edge.get('predicate')!r}")
    if not isinstance(edge.get("properties", {}), dict):
        errors.append("edge properties must be an object")
    if "claim_ids" in edge and not _is_list_of_strings(edge.get("claim_ids")):
        errors.append("edge claim_ids must be a string array")
    return errors


def validate_claim(claim: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in ("id", "subject", "predicate", "source_id"):
        if not str(claim.get(field) or "").strip():
            errors.append(f"claim {field} is required")
    if "object_id" not in claim and "value" not in claim:
        errors.append("claim requires object_id or value")
    if claim.get("evidence_state") not in EVIDENCE_STATES:
        errors.append(f"invalid evidence_state: {claim.get('evidence_state')!r}")
    if claim.get("resolution_status") not in RESOLUTION_STATUSES:
        errors.append(f"invalid resolution_status: {claim.get('resolution_status')!r}")
    if "limitations" in claim and not _is_list_of_strings(claim.get("limitations")):
        errors.append("claim limitations must be a string array")
    source_hash = str(claim.get("source_sha256") or "")
    if source_hash and not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        errors.append("claim source_sha256 must be a lowercase SHA-256")
    return errors


def validate_graph(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    claims: Sequence[Mapping[str, Any]],
) -> list[str]:
    errors: list[str] = []
    node_ids = [str(node.get("id") or "") for node in nodes]
    edge_ids = [str(edge.get("id") or "") for edge in edges]
    claim_ids = [str(claim.get("id") or "") for claim in claims]
    for label, identifiers in (("node", node_ids), ("edge", edge_ids), ("claim", claim_ids)):
        duplicates = sorted({item for item in identifiers if identifiers.count(item) > 1})
        if duplicates:
            errors.append(f"duplicate {label} ids: {duplicates[:10]}")
    node_id_set = set(node_ids)
    claim_id_set = set(claim_ids)
    for index, node in enumerate(nodes):
        errors.extend(f"node[{index}]: {message}" for message in validate_node(node))
        missing = sorted(set(node.get("source_claim_ids") or []) - claim_id_set)
        if missing:
            errors.append(f"node[{index}] references missing claims: {missing[:10]}")
    for index, edge in enumerate(edges):
        errors.extend(f"edge[{index}]: {message}" for message in validate_edge(edge))
        if edge.get("subject") not in node_id_set:
            errors.append(f"edge[{index}] subject does not exist: {edge.get('subject')}")
        if edge.get("object") not in node_id_set:
            errors.append(f"edge[{index}] object does not exist: {edge.get('object')}")
        missing = sorted(set(edge.get("claim_ids") or []) - claim_id_set)
        if missing:
            errors.append(f"edge[{index}] references missing claims: {missing[:10]}")
    for index, claim in enumerate(claims):
        errors.extend(f"claim[{index}]: {message}" for message in validate_claim(claim))
        if claim.get("subject") not in node_id_set:
            errors.append(f"claim[{index}] subject does not exist: {claim.get('subject')}")
        object_id_value = str(claim.get("object_id") or "")
        if object_id_value and object_id_value not in node_id_set:
            errors.append(f"claim[{index}] object does not exist: {object_id_value}")
        if claim.get("source_id") not in node_id_set:
            errors.append(f"claim[{index}] source does not exist: {claim.get('source_id')}")
    return errors
