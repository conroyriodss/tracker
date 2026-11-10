"""Resolve graph claims into concise public NIH Tracker resource views."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from catalog_claims import clean, parse_date
from catalog_schema import PUBLIC_RESOURCE_SCHEMA
from catalog_vocab import VocabularyRegistry

EVIDENCE_RANK = {
    "not_established": 0,
    "registry_reported": 1,
    "officially_reported": 2,
    "verified": 3,
}


def _claim_sort_key(claim: dict[str, Any]) -> tuple[int, dt.date, str]:
    date_value = parse_date(claim.get("reviewed_at") or claim.get("source_date")) or dt.date.min
    return (EVIDENCE_RANK.get(str(claim.get("evidence_state")), 0), date_value, str(claim.get("id")))


def _best(claims: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [claim for claim in claims if claim.get("resolution_status") in {"selected", "supporting", "not_established"}]
    return max(eligible, key=_claim_sort_key) if eligible else None


def _values(claims: list[dict[str, Any]]) -> list[Any]:
    selected: dict[str, Any] = {}
    for claim in sorted(claims, key=_claim_sort_key, reverse=True):
        if "value" not in claim:
            continue
        value = claim.get("value")
        key = str(value).casefold()
        if key not in selected:
            selected[key] = value
    return list(selected.values())


def _node_summary(node: dict[str, Any], evidence_state: str = "not_established") -> dict[str, Any]:
    return {
        "id": node["id"],
        "label": node["label"],
        "type": node["type"],
        "properties": node.get("properties") or {},
        "evidence_state": evidence_state,
    }


def _relationship_evidence(edge: dict[str, Any], claims_by_id: dict[str, dict[str, Any]]) -> str:
    states = [
        str(claims_by_id[identifier].get("evidence_state"))
        for identifier in edge.get("claim_ids") or []
        if identifier in claims_by_id
    ]
    return max(states, key=lambda state: EVIDENCE_RANK.get(state, 0), default="not_established")


def _latest_date(claims: list[dict[str, Any]], fields: tuple[str, ...]) -> str:
    candidates: list[tuple[dt.date, str]] = []
    for claim in claims:
        for field in fields:
            raw = clean(claim.get(field))
            parsed = parse_date(raw)
            if parsed:
                candidates.append((parsed, raw))
                break
    if not candidates:
        return ""
    return max(candidates, key=lambda item: item[0])[1]


def _earliest_due(claims: list[dict[str, Any]]) -> str:
    candidates = [(parse_date(claim.get("review_due")), clean(claim.get("review_due"))) for claim in claims]
    candidates = [item for item in candidates if item[0] is not None]
    return min(candidates, key=lambda item: item[0])[1] if candidates else ""


def _public_evidence(claim: dict[str, Any] | None) -> dict[str, Any]:
    if not claim:
        return {
            "state": "not_established",
            "source_url": "",
            "source_date": "",
            "reviewed_at": "",
            "review_due": "",
            "claim_id": "",
        }
    return {
        "state": claim.get("evidence_state") or "not_established",
        "source_url": claim.get("source_url") or "",
        "source_date": claim.get("source_date") or "",
        "reviewed_at": claim.get("reviewed_at") or "",
        "review_due": claim.get("review_due") or "",
        "claim_id": claim.get("id") or "",
    }


def _controlled_value(
    claims_by_predicate: dict[str, list[dict[str, Any]]],
    predicate: str,
    vocabulary: str,
    vocabularies: VocabularyRegistry,
) -> dict[str, Any]:
    claim = _best(claims_by_predicate.get(predicate, []))
    term_id = str(claim.get("value")) if claim else "not_established"
    if term_id not in vocabularies.ids(vocabulary):
        term_id = "not_established"
    return {
        "id": term_id,
        "label": vocabularies.label(vocabulary, term_id),
        "evidence": _public_evidence(claim),
    }


def resolve_public_resources(
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    vocabularies: VocabularyRegistry,
    catalog_as_of: str,
) -> list[dict[str, Any]]:
    nodes_by_id = {node["id"]: node for node in nodes}
    claims_by_id = {claim["id"]: claim for claim in claims}
    claims_by_subject: dict[str, list[dict[str, Any]]] = defaultdict(list)
    claims_by_subject_predicate: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    edges_by_subject: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for claim in claims:
        claims_by_subject[claim["subject"]].append(claim)
        claims_by_subject_predicate[claim["subject"]][claim["predicate"]].append(claim)
    for edge in edges:
        edges_by_subject[edge["subject"]].append(edge)

    resources: list[dict[str, Any]] = []
    for resource_node in sorted(
        (node for node in nodes if node.get("type") == "Resource"),
        key=lambda node: str((node.get("properties") or {}).get("repo_uid") or node.get("id")),
    ):
        resource_id = resource_node["id"]
        properties = resource_node.get("properties") or {}
        by_predicate = claims_by_subject_predicate[resource_id]
        resource_claims = claims_by_subject[resource_id]
        relation_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in edges_by_subject[resource_id]:
            target = nodes_by_id.get(edge["object"])
            if not target:
                continue
            relation_groups[edge["predicate"]].append(
                _node_summary(target, _relationship_evidence(edge, claims_by_id))
            )
        for records in relation_groups.values():
            records.sort(key=lambda record: (record["label"].casefold(), record["id"]))

        description_claim = _best(by_predicate.get("description", []))
        description = clean(description_claim.get("value")) if description_claim else ""
        resource_types = [str(value) for value in _values(by_predicate.get("resource_type", []))]
        if resource_types == ["not_established"] or not resource_types:
            resource_types = ["not_established"]
        resource_type_terms = [
            {"id": term_id, "label": vocabularies.label("resource_type", term_id)}
            for term_id in resource_types
            if term_id in vocabularies.ids("resource_type")
        ]
        data_asset_types = [clean(value) for value in _values(by_predicate.get("data_asset_type", [])) if clean(value)]
        subject_areas = [clean(value) for value in _values(by_predicate.get("subject_area", [])) if clean(value)]
        data_formats = [clean(value) for value in _values(by_predicate.get("data_format", [])) if clean(value)]
        taxonomies = [clean(value) for value in _values(by_predicate.get("taxonomy", [])) if clean(value)]
        model_systems = [clean(value) for value in _values(by_predicate.get("model_system", [])) if clean(value)]
        modalities = [str(value) for value in _values(by_predicate.get("data_modality", []))]
        if not modalities:
            modalities = ["not_established"]
        modality_terms = [
            {"id": term_id, "label": vocabularies.label("data_modality", term_id)}
            for term_id in modalities
            if term_id in vocabularies.ids("data_modality")
        ]
        organism_scopes = [str(value) for value in _values(by_predicate.get("organism_scope", []))]
        if not organism_scopes:
            organism_scopes = ["not_established"]
        organism_terms = [
            {"id": term_id, "label": vocabularies.label("organism_scope", term_id)}
            for term_id in organism_scopes
            if term_id in vocabularies.ids("organism_scope")
        ]
        human_data_context = _controlled_value(
            by_predicate, "human_data_sensitivity", "human_data_sensitivity", vocabularies
        )
        nih_relationships = [str(value) for value in _values(by_predicate.get("nih_relationship", []))]
        if not nih_relationships:
            nih_relationships = ["not_established"]
        nih_terms = [
            {"id": term_id, "label": vocabularies.label("nih_relationship", term_id)}
            for term_id in nih_relationships
            if term_id in vocabularies.ids("nih_relationship")
        ]

        api_services = relation_groups.get("EXPOSES_API", [])
        standard_nodes = relation_groups.get("IMPLEMENTS_STANDARD", [])
        pid_nodes = relation_groups.get("SUPPORTS_IDENTIFIER_SCHEME", [])
        guid_nodes = relation_groups.get("SUPPORTS_GUID_SYSTEM", [])
        organizations = []
        for predicate in ("OPERATED_BY", "MAINTAINED_BY", "HOSTED_BY", "PROVIDED_BY", "FUNDED_BY"):
            for item in relation_groups.get(predicate, []):
                copy_item = dict(item)
                copy_item["role"] = predicate.lower()
                organizations.append(copy_item)
        organization_keys: set[tuple[str, str]] = set()
        deduped_organizations: list[dict[str, Any]] = []
        for item in organizations:
            key = (item["id"], item["role"])
            if key not in organization_keys:
                organization_keys.add(key)
                deduped_organizations.append(item)

        programmatic = _controlled_value(
            by_predicate, "programmatic_access", "programmatic_access", vocabularies
        )
        pid_level = "not_established"
        scopes = " ".join(clean(item.get("properties", {}).get("scope")).casefold() for item in pid_nodes)
        if pid_nodes:
            if any(token in scopes for token in ("dataset", "item", "repository_items")):
                pid_level = "dataset"
            elif "collection" in scopes:
                pid_level = "collection"
            else:
                pid_level = "repository"
        pid_profile = {
            "id": pid_level,
            "label": vocabularies.label("pid_level", pid_level),
            "schemes": pid_nodes,
        }
        verified_api = any(bool(item.get("properties", {}).get("verified")) for item in api_services)
        documented_api = any(
            item.get("properties", {}).get("documentation_url") or item.get("properties", {}).get("endpoint_url")
            for item in api_services
        )
        api_state = (
            "api_tested"
            if verified_api
            else "api_documented"
            if documented_api
            else "api_reported"
            if api_services
            else "no_public_api_reported"
            if programmatic["id"] == "no_public_api_reported"
            else "not_established"
        )
        metadata_standards = [
            item for item in standard_nodes
            if any(token in clean(item.get("properties", {}).get("category")).casefold() for token in ("metadata", "catalog", "web"))
        ]
        exchange_standards = [
            item for item in standard_nodes
            if any(token in clean(item.get("properties", {}).get("category")).casefold() for token in ("exchange", "data_access", "health", "query", "api"))
        ]
        semantic_standards = [
            item for item in standard_nodes
            if any(token in clean(item.get("label")).casefold() for token in ("ontology", "vocabulary", "common data element", "semantic"))
        ]
        workflow_support = [
            item for item in api_services
            if any(token in clean(item.get("properties", {}).get("family")).casefold() for token in ("wes", "trs", "workflow", "cloud"))
        ]

        name_claim = _best(by_predicate.get("name", []))
        homepage_claim = _best(by_predicate.get("homepage", []))
        operational = _controlled_value(
            by_predicate, "operational_status", "operational_status", vocabularies
        )
        submission = _controlled_value(
            by_predicate, "submission_model", "submission_model", vocabularies
        )
        access = _controlled_value(
            by_predicate, "access_pathway", "access_pathway", vocabularies
        )
        cadr_claim = _best(by_predicate.get("nih_cadr_status", []))
        genomic_claim = _best(by_predicate.get("nih_designated_genomic_data_repository", []))
        eds_claim = _best(by_predicate.get("dbgap_external_data_source_status", []))
        public_study_claim = _best(by_predicate.get("dbgap_public_study_count", []))
        aliases = [clean(value) for value in _values(by_predicate.get("alias", [])) if clean(value)]
        abbreviation_claim = _best(by_predicate.get("abbreviation", []))
        identifiers = relation_groups.get("IDENTIFIED_BY", [])
        icos = relation_groups.get("ASSOCIATED_WITH_ICO", [])
        access_systems = relation_groups.get("HAS_ACCESS_SYSTEM", [])
        designations = relation_groups.get("HAS_POLICY_DESIGNATION", [])
        publisher_edges: list[dict[str, Any]] = []
        for predicate in (
            "REQUIRED_BY_PUBLISHER_POLICY",
            "RECOMMENDED_BY_PUBLISHER_POLICY",
            "PREFERRED_BY_PUBLISHER_POLICY",
            "ACCEPTABLE_FALLBACK_FOR_PUBLISHER_POLICY",
            "MENTIONED_BY_PUBLISHER_POLICY",
        ):
            for item in relation_groups.get(predicate, []):
                copied = dict(item)
                copied["relationship"] = predicate.lower()
                publisher_edges.append(copied)

        evidence_states = [claim.get("evidence_state") for claim in resource_claims]
        evidence_counts = {state: evidence_states.count(state) for state in EVIDENCE_RANK}
        last_reviewed = _latest_date(resource_claims, ("reviewed_at", "source_date")) or clean(properties.get("reviewed_at"))
        review_due = _earliest_due(resource_claims)
        catalog_date = parse_date(catalog_as_of)
        due_date = parse_date(review_due)
        evidence_currency = (
            "review_due"
            if catalog_date and due_date and due_date < catalog_date
            else "current"
            if review_due
            else "not_established"
        )
        external_links = {
            "homepage": clean(homepage_claim.get("value")) if homepage_claim else "",
            "deposit": "",
            "access": "",
            "api_documentation": "",
        }
        for item in api_services:
            doc = clean(item.get("properties", {}).get("documentation_url"))
            if doc:
                external_links["api_documentation"] = doc
                break
        # The status block may provide policy/access URLs only through evidence sources.
        if submission["evidence"]["source_url"]:
            external_links["deposit"] = submission["evidence"]["source_url"]
        if access["evidence"]["source_url"]:
            external_links["access"] = access["evidence"]["source_url"]

        resource = {
            "schema": PUBLIC_RESOURCE_SCHEMA,
            "id": resource_id,
            "repo_uid": clean(properties.get("repo_uid")),
            "name": clean(name_claim.get("value")) if name_claim else resource_node["label"],
            "abbreviation": clean(abbreviation_claim.get("value")) if abbreviation_claim else clean(properties.get("abbreviation")),
            "aliases": aliases or list(properties.get("aliases") or []),
            "resource_types": resource_type_terms,
            "purpose": description,
            "best_fit": {
                "data_asset_types": data_asset_types,
                "subject_areas": subject_areas,
                "modalities": modality_terms,
                "organism_scopes": organism_terms,
                "human_data_sensitivity": human_data_context,
                "taxonomies": taxonomies,
                "model_systems": model_systems,
                "data_formats": data_formats,
                "not_intended_for": "Not established",
            },
            "nih_relationship": {
                "categories": nih_terms,
                "icos": icos,
                "current_funding": (_best(by_predicate.get("current_nih_funding_support", [])) or {}).get("value"),
                "sustained_or_historical_support": (_best(by_predicate.get("sustained_support", [])) or {}).get("value"),
            },
            "operational": operational,
            "deposit": submission,
            "access": access,
            "interoperability": {
                "persistent_identifiers": pid_profile,
                "structured_metadata": {
                    "status": "standardized" if metadata_standards else "not_established",
                    "standards": metadata_standards,
                },
                "programmatic_access": programmatic,
                "api_status": api_state,
                "api_services": api_services,
                "semantic_interoperability": {
                    "status": "documented" if semantic_standards else "not_established",
                    "standards": semantic_standards,
                },
                "workflow_portability": {
                    "status": "documented" if workflow_support else "not_established",
                    "services": workflow_support,
                },
                "data_exchange": exchange_standards,
                "metadata_export": metadata_standards,
                "all_standards": standard_nodes,
                "guid_systems": guid_nodes,
            },
            "policy": {
                "nih_cadr_status": clean(cadr_claim.get("value")) if cadr_claim else "not_established",
                "nih_designated_genomic_data_repository": genomic_claim.get("value") if genomic_claim else None,
                "dbgap_external_data_source_status": clean(eds_claim.get("value")) if eds_claim else "not_established",
                "dbgap_public_study_count": int(public_study_claim.get("value") or 0) if public_study_claim else 0,
                "access_systems": access_systems,
                "designations": designations,
                "disclaimer": "This profile is an informational aid. It does not determine compliance with NIH data-sharing policies, consent restrictions, applicable law, Institutional Review Board requirements, or funding-opportunity-specific conditions.",
            },
            "stewardship": deduped_organizations,
            "publisher_policies": sorted(publisher_edges, key=lambda item: (item["label"].casefold(), item["relationship"])),
            "identifiers": identifiers,
            "links": external_links,
            "evidence": {
                "last_reviewed": last_reviewed,
                "next_review_due": review_due,
                "currency": evidence_currency,
                "claim_count": len(resource_claims),
                "state_counts": evidence_counts,
                "claim_ids": sorted(claim["id"] for claim in resource_claims),
                "name": _public_evidence(name_claim),
                "description": _public_evidence(description_claim),
            },
            "facets": {
                "resource_type": [item["id"] for item in resource_type_terms],
                "nih_relationship": [item["id"] for item in nih_terms],
                "nih_ico": [item["label"] for item in icos],
                "operational_status": [operational["id"]],
                "submission_model": [submission["id"]],
                "access_pathway": [access["id"]],
                "data_modality": [item["id"] for item in modality_terms],
                "organism_scope": [item["id"] for item in organism_terms],
                "human_data_sensitivity": [human_data_context["id"]],
                "subject_area": subject_areas,
                "taxonomy": taxonomies,
                "model_system": model_systems,
                "programmatic_access": [programmatic["id"]],
                "pid_level": [pid_level],
                "publisher_posture": sorted({item["relationship"] for item in publisher_edges}),
                "nih_cadr": ["listed" if clean(cadr_claim.get("value")) == "listed" else "not_listed_or_not_established"] if cadr_claim else ["not_established"],
            },
            "search_text": " ".join(
                item
                for item in [
                    resource_node["label"],
                    clean(properties.get("abbreviation")),
                    " ".join(aliases),
                    description,
                    " ".join(data_asset_types),
                    " ".join(subject_areas),
                    " ".join(item["label"] for item in modality_terms),
                    " ".join(item["label"] for item in organism_terms),
                    human_data_context["label"],
                    " ".join(taxonomies),
                    " ".join(model_systems),
                    " ".join(item["label"] for item in icos),
                    " ".join(item["label"] for item in resource_type_terms),
                ]
                if item
            ).casefold(),
            "catalog_as_of": catalog_as_of,
        }
        resources.append(resource)
    return resources
