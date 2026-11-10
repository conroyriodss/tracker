"""Deterministic writers for v19 catalog artifacts."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from catalog_schema import canonical_json


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: Any, *, pretty: bool = True) -> bytes:
    if pretty:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n").encode("utf-8")
    return (canonical_json(value) + "\n").encode("utf-8")


def jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    return b"".join((canonical_json(record) + "\n").encode("utf-8") for record in records)


def resources_csv_bytes(resources: list[dict[str, Any]]) -> bytes:
    fields = [
        "repo_uid",
        "name",
        "abbreviation",
        "resource_types",
        "purpose",
        "nih_relationship",
        "nih_icos",
        "operational_status",
        "submission_model",
        "access_pathway",
        "data_modalities",
        "organism_scope",
        "human_data_sensitivity",
        "model_systems",
        "taxonomies",
        "subject_areas",
        "programmatic_access",
        "api_status",
        "pid_level",
        "guid_system_count",
        "nih_cadr_status",
        "last_reviewed",
        "next_review_due",
        "homepage",
    ]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for resource in resources:
        writer.writerow(
            {
                "repo_uid": resource.get("repo_uid", ""),
                "name": resource.get("name", ""),
                "abbreviation": resource.get("abbreviation", ""),
                "resource_types": "; ".join(item.get("label", "") for item in resource.get("resource_types", [])),
                "purpose": resource.get("purpose", ""),
                "nih_relationship": "; ".join(item.get("label", "") for item in resource.get("nih_relationship", {}).get("categories", [])),
                "nih_icos": "; ".join(item.get("label", "") for item in resource.get("nih_relationship", {}).get("icos", [])),
                "operational_status": resource.get("operational", {}).get("label", ""),
                "submission_model": resource.get("deposit", {}).get("label", ""),
                "access_pathway": resource.get("access", {}).get("label", ""),
                "data_modalities": "; ".join(item.get("label", "") for item in resource.get("best_fit", {}).get("modalities", [])),
                "organism_scope": "; ".join(item.get("label", "") for item in resource.get("best_fit", {}).get("organism_scopes", [])),
                "human_data_sensitivity": resource.get("best_fit", {}).get("human_data_sensitivity", {}).get("label", ""),
                "model_systems": "; ".join(resource.get("best_fit", {}).get("model_systems", [])),
                "taxonomies": "; ".join(resource.get("best_fit", {}).get("taxonomies", [])),
                "subject_areas": "; ".join(resource.get("best_fit", {}).get("subject_areas", [])),
                "programmatic_access": resource.get("interoperability", {}).get("programmatic_access", {}).get("label", ""),
                "api_status": resource.get("interoperability", {}).get("api_status", ""),
                "pid_level": resource.get("interoperability", {}).get("persistent_identifiers", {}).get("label", ""),
                "guid_system_count": len(resource.get("interoperability", {}).get("guid_systems", [])),
                "nih_cadr_status": resource.get("policy", {}).get("nih_cadr_status", ""),
                "last_reviewed": resource.get("evidence", {}).get("last_reviewed", ""),
                "next_review_due": resource.get("evidence", {}).get("next_review_due", ""),
                "homepage": resource.get("links", {}).get("homepage", ""),
            }
        )
    return buffer.getvalue().encode("utf-8")


def jsonld_payload(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    graph: list[dict[str, Any]] = []
    for node in nodes:
        graph.append(
            {
                "@id": f"urn:nih-tracker:{node['id']}",
                "@type": f"https://conroyriodss.github.io/tracker/vocab/{node['type']}",
                "name": node["label"],
                **{key: value for key, value in (node.get("properties") or {}).items() if value not in (None, "", [], {})},
            }
        )
    for edge in edges:
        graph.append(
            {
                "@id": f"urn:nih-tracker:{edge['id']}",
                "@type": "https://conroyriodss.github.io/tracker/vocab/Relationship",
                "subject": {"@id": f"urn:nih-tracker:{edge['subject']}"},
                "predicate": edge["predicate"],
                "object": {"@id": f"urn:nih-tracker:{edge['object']}"},
            }
        )
    return {
        "@context": {
            "name": "https://schema.org/name",
            "subject": {"@id": "https://www.w3.org/1999/02/22-rdf-syntax-ns#subject", "@type": "@id"},
            "predicate": "https://www.w3.org/1999/02/22-rdf-syntax-ns#predicate",
            "object": {"@id": "https://www.w3.org/1999/02/22-rdf-syntax-ns#object", "@type": "@id"}
        },
        "@graph": graph,
    }


def artifact_payloads(
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    history: list[dict[str, Any]],
    vocabularies: dict[str, Any],
) -> dict[str, bytes]:
    return {
        "nodes.jsonl": jsonl_bytes(nodes),
        "edges.jsonl": jsonl_bytes(edges),
        "claims.jsonl": jsonl_bytes(claims),
        "resources.json": json_bytes(resources),
        "resources.jsonl": jsonl_bytes(resources),
        "resources.csv": resources_csv_bytes(resources),
        "change_history.jsonl": jsonl_bytes(history),
        "vocabularies.json": json_bytes(vocabularies),
        "catalog.jsonld": json_bytes(jsonld_payload(nodes, edges)),
    }


def write_payloads(directory: Path, payloads: dict[str, bytes]) -> dict[str, dict[str, Any]]:
    directory.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict[str, Any]] = {}
    for name in sorted(payloads):
        data = payloads[name]
        path = directory / name
        temporary = directory / f".{name}.tmp"
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(data)
        temporary.replace(path)
        manifest[name] = {"sha256": sha256_bytes(data), "bytes": len(data)}
    return manifest
