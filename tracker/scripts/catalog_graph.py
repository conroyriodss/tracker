"""Compile reviewed NIH Tracker cards into typed nodes, edges, and claims."""
from __future__ import annotations

import copy
import re
from collections import defaultdict
from typing import Any

from catalog_claims import EvidenceContext, clean, context_from_item, safe_url
from catalog_claims import claim_record as make_claim_record
from catalog_schema import edge_id, node_id, stable_hash
from catalog_vocab import (
    VocabularyRegistry,
    infer_human_data_sensitivity,
    infer_modalities,
    infer_organism_scopes,
    map_access_status,
    map_operational_status,
    map_submission_status,
    normalize_resource_type,
)
from interoperability_schema import normalize_interoperability
from policy_context_schema import normalize_policy_context
from publisher_context_schema import normalize_publisher_context
from repository_status_schema import normalize_repository_status
from resource_profile_schema import normalize_resource_profile


def _list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    return [value]


def _identity_scalar(value: Any) -> tuple[Any, str, str, str]:
    if isinstance(value, dict) and "value" in value:
        return (
            value.get("value"),
            clean(value.get("tier")),
            clean(value.get("as_of")),
            clean(value.get("source")),
        )
    return value, "", "", ""


def _curation_date(card: dict[str, Any]) -> str:
    curation = card.get("curation") if isinstance(card.get("curation"), dict) else {}
    return clean(
        curation.get("last_reviewed")
        or curation.get("reviewed_at")
        or curation.get("updated_at")
        or curation.get("as_of")
    )


def _meta(block: dict[str, Any]) -> dict[str, Any]:
    return block.get("_meta") if isinstance(block.get("_meta"), dict) else {}


def _meta_source(meta: dict[str, Any], fallback: str) -> str:
    sources = _list(meta.get("sources") or meta.get("source"))
    return clean(sources[0]) if sources else fallback


def _source_url_from_name(source: str) -> str:
    text = clean(source)
    if safe_url(text):
        return text
    if text.startswith("re3data:"):
        identifier = text.split(":", 1)[1]
        return f"https://www.re3data.org/repository/{identifier}" if identifier else ""
    if text.startswith("fairsharing:"):
        identifier = text.split(":", 1)[1]
        return f"https://fairsharing.org/{identifier}" if identifier else ""
    return ""


def _best_context(
    item: dict[str, Any] | None,
    *,
    uid: str,
    card_sha256: str,
    meta: dict[str, Any] | None = None,
    fallback_url: str = "",
    fallback_tier: str = "",
    reviewed_at: str = "",
    verified: bool = False,
    method: str = "card_to_catalog",
) -> EvidenceContext:
    meta = meta or {}
    source = clean((item or {}).get("source") if isinstance(item, dict) else "")
    if not source:
        source = _meta_source(meta, f"reviewed_card:{uid}")
    source_url = safe_url((item or {}).get("source_url") if isinstance(item, dict) else "")
    source_url = source_url or _source_url_from_name(source) or safe_url(fallback_url)
    return context_from_item(
        item,
        fallback_source=source,
        fallback_url=source_url,
        fallback_date=clean(meta.get("as_of")),
        fallback_tier=clean(meta.get("tier")) or fallback_tier,
        fallback_sha256=card_sha256,
        reviewed_at=reviewed_at,
        method=method,
        verified=verified,
    )


def _term_name(item: Any) -> str:
    if isinstance(item, dict):
        return clean(item.get("name") or item.get("label") or item.get("value"))
    return clean(item)


def _dedupe_strings(values: list[Any]) -> list[str]:
    selected: dict[str, str] = {}
    for value in values:
        text = clean(value)
        if text and text.casefold() not in selected:
            selected[text.casefold()] = text
    return [selected[key] for key in sorted(selected)]


class CatalogBuilder:
    """In-memory deterministic graph compiler.

    Nodes, edges, and claims are keyed by stable IDs. Adding the same assertion
    twice merges source claim references rather than generating duplicate rows.
    """

    def __init__(self, vocabularies: VocabularyRegistry) -> None:
        self.vocabularies = vocabularies
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.claims: dict[str, dict[str, Any]] = {}
        self.resource_uids: list[str] = []
        self.card_sha256: dict[str, str] = {}
        self._claim_ids_by_subject: dict[str, set[str]] = defaultdict(set)

    def add_node(
        self,
        *,
        node_id_value: str,
        node_type: str,
        label: str,
        properties: dict[str, Any] | None = None,
    ) -> str:
        if not node_id_value or not label:
            raise ValueError("node id and label are required")
        record = self.nodes.get(node_id_value)
        if record is None:
            self.nodes[node_id_value] = {
                "id": node_id_value,
                "type": node_type,
                "label": label,
                "properties": copy.deepcopy(properties or {}),
                "source_claim_ids": [],
            }
            return node_id_value
        if record["type"] != node_type:
            raise ValueError(f"Node type conflict for {node_id_value}: {record['type']} vs {node_type}")
        if label and len(label) > len(str(record.get("label") or "")):
            record["label"] = label
        for key, value in sorted((properties or {}).items()):
            if value in (None, "", [], {}):
                continue
            current = record["properties"].get(key)
            if current in (None, "", [], {}):
                record["properties"][key] = copy.deepcopy(value)
            elif isinstance(current, list) and isinstance(value, list):
                merged = current + [item for item in value if item not in current]
                record["properties"][key] = merged
        return node_id_value

    def add_source(self, evidence: EvidenceContext) -> str:
        node = evidence.source_node()
        self.add_node(
            node_id_value=node["id"],
            node_type=node["type"],
            label=node["label"],
            properties=node["properties"],
        )
        return node["id"]

    def add_claim(
        self,
        *,
        subject: str,
        predicate: str,
        evidence: EvidenceContext,
        object_id: str = "",
        value: Any = None,
        resolution_status: str = "selected",
        limitations: list[str] | None = None,
    ) -> str:
        self.add_source(evidence)
        claim = make_claim_record(
            subject=subject,
            predicate=predicate,
            evidence=evidence,
            object_id=object_id,
            value=value,
            resolution_status=resolution_status,
            limitations=limitations,
        )
        existing = self.claims.get(claim["id"])
        if existing is None:
            self.claims[claim["id"]] = claim
        else:
            existing["limitations"] = sorted(
                set(existing.get("limitations") or []) | set(claim.get("limitations") or [])
            )
        self._claim_ids_by_subject[subject].add(claim["id"])
        if subject in self.nodes:
            self.nodes[subject]["source_claim_ids"] = sorted(self._claim_ids_by_subject[subject])
        return claim["id"]

    def add_edge(
        self,
        *,
        subject: str,
        predicate: str,
        object_id: str,
        claim_id_value: str = "",
        properties: dict[str, Any] | None = None,
    ) -> str:
        identifier = edge_id(subject, predicate, object_id)
        record = self.edges.get(identifier)
        if record is None:
            record = {
                "id": identifier,
                "subject": subject,
                "predicate": predicate,
                "object": object_id,
                "properties": copy.deepcopy(properties or {}),
                "claim_ids": [],
            }
            self.edges[identifier] = record
        if claim_id_value and claim_id_value not in record["claim_ids"]:
            record["claim_ids"].append(claim_id_value)
            record["claim_ids"].sort()
        return identifier

    def add_relation_claim(
        self,
        *,
        subject: str,
        predicate: str,
        object_id: str,
        evidence: EvidenceContext,
        claim_predicate: str,
        properties: dict[str, Any] | None = None,
        limitations: list[str] | None = None,
    ) -> str:
        claim_id_value = self.add_claim(
            subject=subject,
            predicate=claim_predicate,
            object_id=object_id,
            evidence=evidence,
            limitations=limitations,
        )
        self.add_edge(
            subject=subject,
            predicate=predicate,
            object_id=object_id,
            claim_id_value=claim_id_value,
            properties=properties,
        )
        return claim_id_value

    def compile_card(self, uid: str, card: dict[str, Any], *, card_sha256: str) -> None:
        identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
        name = clean(identity.get("name")) or uid
        reviewed_at = _curation_date(card)
        resource_id = node_id("Resource", uid)
        aliases = _dedupe_strings(_list(identity.get("aliases")))
        self.add_node(
            node_id_value=resource_id,
            node_type="Resource",
            label=name,
            properties={
                "repo_uid": uid,
                "kind": clean(card.get("kind")) or "resource",
                "abbreviation": clean(identity.get("abbreviation")),
                "aliases": aliases,
                "card_sha256": card_sha256,
                "review_state": clean((card.get("curation") or {}).get("review_state")),
                "reviewed_at": reviewed_at,
            },
        )
        self.resource_uids.append(uid)
        self.card_sha256[uid] = card_sha256
        card_context = EvidenceContext(
            source=f"reviewed_card:{uid}",
            source_url=safe_url(identity.get("homepage")),
            source_date=reviewed_at,
            tier="curated" if clean((card.get("curation") or {}).get("review_state")) == "human_confirmed" else "attestation",
            method="reviewed_card",
            source_sha256=card_sha256,
            reviewed_at=reviewed_at,
        )
        self.add_claim(subject=resource_id, predicate="name", value=name, evidence=card_context)
        if clean(identity.get("abbreviation")):
            self.add_claim(
                subject=resource_id,
                predicate="abbreviation",
                value=clean(identity.get("abbreviation")),
                evidence=card_context,
            )
        homepage = safe_url(identity.get("homepage"))
        if homepage:
            self.add_claim(subject=resource_id, predicate="homepage", value=homepage, evidence=card_context)
        for alias in aliases:
            self.add_claim(subject=resource_id, predicate="alias", value=alias, evidence=card_context)

        blocks = card.get("blocks") if isinstance(card.get("blocks"), dict) else {}
        self._compile_links(resource_id, uid, card.get("links"), card_context)
        organism_scopes = self._compile_resource_profile(
            resource_id,
            uid,
            blocks.get("resource_profile"),
            blocks.get("bmic"),
            card_sha256,
            reviewed_at,
            homepage,
            card.get("kind"),
        )
        self._compile_interoperability(
            resource_id,
            uid,
            blocks.get("interoperability"),
            card_sha256,
            reviewed_at,
            homepage,
        )
        access_term = self._compile_status(
            resource_id,
            uid,
            blocks.get("repository_status"),
            identity,
            card_sha256,
            reviewed_at,
            homepage,
        )
        self._compile_human_data_context(
            resource_id,
            organism_scopes,
            access_term,
            card_context,
        )
        self._compile_policy(
            resource_id,
            uid,
            blocks.get("policy_context"),
            card_sha256,
            reviewed_at,
        )
        self._compile_publishers(
            resource_id,
            uid,
            blocks.get("publisher_context"),
            card_sha256,
            reviewed_at,
        )
        self._finalize_scope_claims(resource_id, uid, blocks, card_context)

    def _compile_links(
        self,
        resource_id: str,
        uid: str,
        links: Any,
        card_context: EvidenceContext,
    ) -> None:
        for raw in _list(links):
            if not isinstance(raw, dict):
                continue
            id_type = clean(raw.get("id_type")).casefold()
            id_value = clean(raw.get("id_value"))
            url = safe_url(raw.get("url"))
            if not id_type or (not id_value and not url):
                continue
            evidence = context_from_item(
                raw,
                fallback_source=f"reviewed_card:{uid}",
                fallback_url=url,
                fallback_date=card_context.source_date,
                fallback_tier=clean(raw.get("tier")) or card_context.tier,
                fallback_sha256=card_context.source_sha256,
                reviewed_at=card_context.reviewed_at,
                method="card_link",
                verified=clean(raw.get("status")).casefold() == "verified",
            )
            if id_type == "nih_ic":
                ico_id = node_id("NIHOrganizationalUnit", id_value)
                self.add_node(
                    node_id_value=ico_id,
                    node_type="NIHOrganizationalUnit",
                    label=id_value,
                    properties={"abbreviation": id_value},
                )
                self.add_relation_claim(
                    subject=resource_id,
                    predicate="ASSOCIATED_WITH_ICO",
                    object_id=ico_id,
                    evidence=evidence,
                    claim_predicate="nih_ico_association",
                )
                continue
            natural = [id_type, id_value or url]
            identifier_id = node_id("ExternalIdentifier", natural)
            label = f"{id_type}: {id_value or url}"
            self.add_node(
                node_id_value=identifier_id,
                node_type="ExternalIdentifier",
                label=label,
                properties={
                    "identifier_type": id_type,
                    "identifier_value": id_value,
                    "url": url,
                    "status": clean(raw.get("status")),
                },
            )
            self.add_relation_claim(
                subject=resource_id,
                predicate="IDENTIFIED_BY",
                object_id=identifier_id,
                evidence=evidence,
                claim_predicate="external_identifier",
            )

    def _compile_resource_profile(
        self,
        resource_id: str,
        uid: str,
        raw_block: Any,
        bmic_block: Any,
        card_sha256: str,
        reviewed_at: str,
        homepage: str,
        kind: Any,
    ) -> list[str]:
        profile = normalize_resource_profile(raw_block)
        meta = _meta(profile)
        fallback = _best_context(
            None,
            uid=uid,
            card_sha256=card_sha256,
            meta=meta,
            fallback_url=homepage,
            fallback_tier="registry",
            reviewed_at=reviewed_at,
            method="resource_profile",
        )
        descriptions = profile.get("descriptions") or []
        for index, item in enumerate(descriptions):
            if not isinstance(item, dict) or not clean(item.get("text")):
                continue
            evidence = _best_context(
                item,
                uid=uid,
                card_sha256=card_sha256,
                meta=meta,
                fallback_url=homepage,
                fallback_tier=fallback.tier,
                reviewed_at=reviewed_at,
                method="resource_description",
            )
            self.add_claim(
                subject=resource_id,
                predicate="description",
                value=clean(item.get("text")),
                evidence=evidence,
                resolution_status="selected" if index == 0 else "supporting",
            )
        resource_type_names = [_term_name(item) for item in profile.get("resource_types") or []]
        if clean(kind):
            resource_type_names.append(clean(kind))
        resource_types = normalize_resource_type(resource_type_names, self.vocabularies)
        for term_id in resource_types:
            self.add_claim(
                subject=resource_id,
                predicate="resource_type",
                value=term_id,
                evidence=fallback,
                resolution_status="selected" if term_id != "not_established" else "not_established",
            )
        for predicate, key_name in (
            ("data_asset_type", "data_asset_types"),
            ("subject_area", "subject_areas"),
            ("data_format", "data_formats"),
            ("taxonomy", "taxonomies"),
            ("keyword", "keywords"),
            ("language", "languages"),
            ("service_role", "service_roles"),
        ):
            for item in profile.get(key_name) or []:
                name = _term_name(item)
                if not name:
                    continue
                evidence = _best_context(
                    item if isinstance(item, dict) else None,
                    uid=uid,
                    card_sha256=card_sha256,
                    meta=meta,
                    fallback_url=homepage,
                    fallback_tier=fallback.tier,
                    reviewed_at=reviewed_at,
                    method=f"resource_profile:{predicate}",
                )
                self.add_claim(subject=resource_id, predicate=predicate, value=name, evidence=evidence)
        bmic = bmic_block if isinstance(bmic_block, dict) else {}
        bmic_terms: list[str] = []
        for predicate, field_name in (("subject_area", "subject_area"), ("model_system", "model_system")):
            value, tier, as_of, source = _identity_scalar(bmic.get(field_name))
            text = clean(value)
            if not text:
                continue
            bmic_terms.append(text)
            evidence = EvidenceContext(
                source=source or f"reviewed_card:{uid}",
                source_url=homepage,
                source_date=as_of,
                tier=tier or "attestation",
                method=f"bmic:{predicate}",
                source_sha256=card_sha256,
                reviewed_at=reviewed_at,
            )
            self.add_claim(subject=resource_id, predicate=predicate, value=text, evidence=evidence)

        taxonomy_terms = [_term_name(item) for item in profile.get("taxonomies") or []]
        modality_inputs = [
            *resource_type_names,
            *[_term_name(item) for item in profile.get("data_asset_types") or []],
            *[_term_name(item) for item in profile.get("subject_areas") or []],
            *[_term_name(item) for item in profile.get("keywords") or []],
            *taxonomy_terms,
            *bmic_terms,
            *[clean(item.get("text")) for item in descriptions if isinstance(item, dict)],
        ]
        for term_id in infer_modalities(modality_inputs, self.vocabularies):
            self.add_claim(
                subject=resource_id,
                predicate="data_modality",
                value=term_id,
                evidence=fallback,
                resolution_status="selected" if term_id != "not_established" else "not_established",
                limitations=["Modality terms are deterministically inferred from source-bound descriptive terms; review before policy use."] if term_id != "not_established" else [],
            )
        organism_inputs = [
            *taxonomy_terms,
            *bmic_terms,
            *[_term_name(item) for item in profile.get("data_asset_types") or []],
            *[_term_name(item) for item in profile.get("subject_areas") or []],
            *[_term_name(item) for item in profile.get("keywords") or []],
            *[clean(item.get("text")) for item in descriptions if isinstance(item, dict)],
        ]
        organism_scopes = infer_organism_scopes(organism_inputs, self.vocabularies)
        for term_id in organism_scopes:
            self.add_claim(
                subject=resource_id,
                predicate="organism_scope",
                value=term_id,
                evidence=fallback,
                resolution_status="selected" if term_id != "not_established" else "not_established",
                limitations=[
                    "Organism and model-system facets are deterministically inferred from source-bound taxonomy and descriptive terms; asset-level scope may differ."
                ] if term_id != "not_established" else [],
            )

        for item in profile.get("organizations") or []:
            if not isinstance(item, dict):
                continue
            name = clean(item.get("name"))
            if not name:
                continue
            org_id = node_id("Organization", item.get("ror") or [name, item.get("country")])
            self.add_node(
                node_id_value=org_id,
                node_type="Organization",
                label=name,
                properties={
                    "ror": clean(item.get("ror")),
                    "url": safe_url(item.get("url")),
                    "country": clean(item.get("country")),
                    "roles": _dedupe_strings(_list(item.get("roles") or item.get("role"))),
                },
            )
            evidence = _best_context(
                item,
                uid=uid,
                card_sha256=card_sha256,
                meta=meta,
                fallback_url=homepage,
                fallback_tier=fallback.tier,
                reviewed_at=reviewed_at,
                method="resource_profile:organization",
            )
            roles = [clean(role).casefold() for role in _list(item.get("roles") or item.get("role"))]
            predicates: list[str] = []
            if any("maintain" in role for role in roles):
                predicates.append("MAINTAINED_BY")
            if any(role in {"operator", "owner"} or "operat" in role for role in roles):
                predicates.append("OPERATED_BY")
            if any("host" in role for role in roles):
                predicates.append("HOSTED_BY")
            if any("fund" in role for role in roles):
                predicates.append("FUNDED_BY")
            if any("provider" in role for role in roles) or not predicates:
                predicates.append("PROVIDED_BY")
            for edge_predicate in sorted(set(predicates)):
                self.add_relation_claim(
                    subject=resource_id,
                    predicate=edge_predicate,
                    object_id=org_id,
                    evidence=evidence,
                    claim_predicate="organization",
                    properties={"roles": roles},
                )
        return organism_scopes

    def _compile_interoperability(
        self,
        resource_id: str,
        uid: str,
        raw_block: Any,
        card_sha256: str,
        reviewed_at: str,
        homepage: str,
    ) -> None:
        block = normalize_interoperability(raw_block, repo_uid=uid)
        meta = _meta(block)
        fallback = _best_context(
            None,
            uid=uid,
            card_sha256=card_sha256,
            meta=meta,
            fallback_url=homepage,
            fallback_tier="declared",
            reviewed_at=reviewed_at,
            method="interoperability",
        )
        api = block.get("api") if isinstance(block.get("api"), dict) else {}
        endpoints = api.get("endpoints") or []
        docs = api.get("documentation_links") or []
        endpoint_families: set[str] = set()
        for item in endpoints:
            if not isinstance(item, dict):
                continue
            endpoint_url = safe_url(item.get("url"))
            family = clean(item.get("family")) or "Programmatic service"
            endpoint_families.add(family)
            api_id = node_id("APIService", [uid, endpoint_url or family])
            self.add_node(
                node_id_value=api_id,
                node_type="APIService",
                label=f"{family} service for {self.nodes[resource_id]['label']}",
                properties={
                    "family": family,
                    "endpoint_url": endpoint_url,
                    "documentation_url": safe_url(item.get("documentation_url")),
                    "authentication": clean(item.get("authentication") or item.get("auth")),
                    "declared": bool(item.get("declared")),
                    "verified": bool(item.get("verified")),
                    "http_status": item.get("http_status"),
                    "response_media_types": _dedupe_strings(_list(item.get("response_media_types"))),
                },
            )
            evidence = _best_context(
                item,
                uid=uid,
                card_sha256=card_sha256,
                meta=meta,
                fallback_url=safe_url(item.get("documentation_url")) or endpoint_url or homepage,
                fallback_tier=fallback.tier,
                reviewed_at=reviewed_at,
                verified=bool(item.get("verified")),
                method="interoperability:api_endpoint",
            )
            self.add_relation_claim(
                subject=resource_id,
                predicate="EXPOSES_API",
                object_id=api_id,
                evidence=evidence,
                claim_predicate="api_service",
            )
        for family in api.get("families") or []:
            family = clean(family)
            if not family or family in endpoint_families:
                continue
            api_id = node_id("APIService", [uid, family])
            self.add_node(
                node_id_value=api_id,
                node_type="APIService",
                label=f"{family} service for {self.nodes[resource_id]['label']}",
                properties={"family": family, "endpoint_url": "", "documentation_url": ""},
            )
            self.add_relation_claim(
                subject=resource_id,
                predicate="EXPOSES_API",
                object_id=api_id,
                evidence=fallback,
                claim_predicate="api_service",
                limitations=["An API family is reported, but no endpoint is documented in the current catalog."]
            )
        for item in docs:
            if not isinstance(item, dict) or not safe_url(item.get("url")):
                continue
            api_id = node_id("APIService", [uid, "documentation", item.get("url")])
            self.add_node(
                node_id_value=api_id,
                node_type="APIService",
                label=clean(item.get("label")) or f"API documentation for {self.nodes[resource_id]['label']}",
                properties={"documentation_url": safe_url(item.get("url")), "family": clean(item.get("family"))},
            )
            evidence = _best_context(
                item,
                uid=uid,
                card_sha256=card_sha256,
                meta=meta,
                fallback_url=safe_url(item.get("url")),
                fallback_tier=fallback.tier,
                reviewed_at=reviewed_at,
                method="interoperability:api_documentation",
            )
            self.add_relation_claim(
                subject=resource_id,
                predicate="EXPOSES_API",
                object_id=api_id,
                evidence=evidence,
                claim_predicate="api_service",
                limitations=["Documentation evidence does not establish endpoint reachability or protocol conformance."]
            )
        programmatic = "not_established"
        family_text = " ".join(clean(item).casefold() for item in api.get("families") or [])
        access_class = clean(block.get("access_class")).casefold()
        if "bulk" in family_text:
            programmatic = "bulk_download"
        elif endpoints or docs or api.get("families"):
            programmatic = "authenticated_api" if access_class in {"controlled", "gated", "authenticated"} else "public_api"
        elif clean(block.get("api_evidence_state")) == "absent":
            programmatic = "no_public_api_reported"
        self.add_claim(
            subject=resource_id,
            predicate="programmatic_access",
            value=self.vocabularies.validate("programmatic_access", programmatic),
            evidence=fallback,
            resolution_status="not_established" if programmatic == "not_established" else "selected",
        )
        for item in block.get("standards") or []:
            if not isinstance(item, dict) or not clean(item.get("name")):
                continue
            name = clean(item.get("name"))
            standard_id = node_id("Standard", item.get("url") or name)
            self.add_node(
                node_id_value=standard_id,
                node_type="Standard",
                label=name,
                properties={
                    "category": clean(item.get("category")),
                    "url": safe_url(item.get("url")),
                    "version": clean(item.get("version")),
                    "implementation_role": clean(item.get("implementation_role") or item.get("role")),
                },
            )
            evidence = _best_context(
                item,
                uid=uid,
                card_sha256=card_sha256,
                meta=meta,
                fallback_url=safe_url(item.get("evidence_url")) or safe_url(item.get("url")) or homepage,
                fallback_tier=fallback.tier,
                reviewed_at=reviewed_at,
                verified=bool(item.get("verified")),
                method="interoperability:standard",
            )
            self.add_relation_claim(
                subject=resource_id,
                predicate="IMPLEMENTS_STANDARD",
                object_id=standard_id,
                evidence=evidence,
                claim_predicate="standard",
                limitations=[] if item.get("verified") else ["A named standard is reported; implementation conformance was not independently tested unless stated otherwise."],
            )
        for item in block.get("pid_systems") or []:
            if not isinstance(item, dict) or not clean(item.get("name")):
                continue
            name = clean(item.get("name"))
            pid_id = node_id("IdentifierScheme", item.get("url") or name)
            self.add_node(
                node_id_value=pid_id,
                node_type="IdentifierScheme",
                label=name,
                properties={"url": safe_url(item.get("url")), "scope": clean(item.get("scope"))},
            )
            evidence = _best_context(
                item,
                uid=uid,
                card_sha256=card_sha256,
                meta=meta,
                fallback_url=safe_url(item.get("url")) or homepage,
                fallback_tier=fallback.tier,
                reviewed_at=reviewed_at,
                method="interoperability:pid_system",
            )
            self.add_relation_claim(
                subject=resource_id,
                predicate="SUPPORTS_IDENTIFIER_SCHEME",
                object_id=pid_id,
                evidence=evidence,
                claim_predicate="pid_system",
            )
        for item in block.get("guid_systems") or []:
            if not isinstance(item, dict) or not clean(item.get("name")):
                continue
            name = clean(item.get("name"))
            guid_id = node_id("GUIDSystem", item.get("documentation_url") or name)
            self.add_node(
                node_id_value=guid_id,
                node_type="GUIDSystem",
                label=name,
                properties={
                    "scope": clean(item.get("scope")),
                    "format": clean(item.get("format")),
                    "documentation_url": safe_url(item.get("documentation_url")),
                    "tool_url": safe_url(item.get("tool_url")),
                    "privacy_characteristics": clean(item.get("privacy_characteristics")),
                    "operator": clean(item.get("operator")),
                    "generation_method": clean(item.get("generation_method")),
                    "cross_repository_linkage": item.get("cross_repository_linkage"),
                },
            )
            evidence = _best_context(
                item,
                uid=uid,
                card_sha256=card_sha256,
                meta=meta,
                fallback_url=safe_url(item.get("documentation_url")) or safe_url(item.get("tool_url")) or homepage,
                fallback_tier=clean(item.get("tier")) or fallback.tier,
                reviewed_at=reviewed_at,
                method="interoperability:guid_system",
            )
            self.add_relation_claim(
                subject=resource_id,
                predicate="SUPPORTS_GUID_SYSTEM",
                object_id=guid_id,
                evidence=evidence,
                claim_predicate="guid_system",
            )

    def _compile_status(
        self,
        resource_id: str,
        uid: str,
        raw_block: Any,
        identity: dict[str, Any],
        card_sha256: str,
        reviewed_at: str,
        homepage: str,
    ) -> str:
        block = normalize_repository_status(raw_block)
        meta = _meta(block)
        operational = block.get("operational") or {}
        identity_status, identity_tier, identity_asof, identity_source = _identity_scalar(identity.get("status"))
        operational_value = clean(operational.get("status"))
        if operational_value == "unknown" and clean(identity_status):
            operational_value = clean(identity_status)
        operational_context = _best_context(
            operational if isinstance(operational, dict) else None,
            uid=uid,
            card_sha256=card_sha256,
            meta=meta,
            fallback_url=safe_url(operational.get("source_url")) or homepage,
            fallback_tier=clean(operational.get("tier")) or identity_tier or "attestation",
            reviewed_at=reviewed_at,
            method="repository_status:operational",
        )
        if not operational_context.source_date and identity_asof:
            operational_context = EvidenceContext(
                source=operational_context.source or identity_source or f"reviewed_card:{uid}",
                source_url=operational_context.source_url,
                source_date=identity_asof,
                tier=operational_context.tier,
                method=operational_context.method,
                source_sha256=operational_context.source_sha256,
                reviewed_at=operational_context.reviewed_at,
                verified=operational_context.verified,
            )
        self.add_claim(
            subject=resource_id,
            predicate="operational_status",
            value=map_operational_status(operational_value, self.vocabularies),
            evidence=operational_context,
            resolution_status="not_established" if map_operational_status(operational_value, self.vocabularies) == "not_established" else "selected",
        )
        submissions = block.get("submissions") or {}
        submission_context = _best_context(
            submissions if isinstance(submissions, dict) else None,
            uid=uid,
            card_sha256=card_sha256,
            meta=meta,
            fallback_url=safe_url(submissions.get("policy_url")) or safe_url(submissions.get("source_url")) or homepage,
            fallback_tier=clean(submissions.get("tier")) or "attestation",
            reviewed_at=reviewed_at,
            method="repository_status:submissions",
        )
        submission_term = map_submission_status(submissions.get("status"), self.vocabularies)
        self.add_claim(
            subject=resource_id,
            predicate="submission_model",
            value=submission_term,
            evidence=submission_context,
            resolution_status="not_established" if submission_term == "not_established" else "selected",
            limitations=["Broad investigator intake is distinct from access openness and program-specific contribution pathways."],
        )
        access = block.get("access") or {}
        access_context = _best_context(
            access if isinstance(access, dict) else None,
            uid=uid,
            card_sha256=card_sha256,
            meta=meta,
            fallback_url=safe_url(access.get("access_url")) or safe_url(access.get("source_url")) or homepage,
            fallback_tier="attestation",
            reviewed_at=reviewed_at,
            method="repository_status:access",
        )
        access_term = map_access_status(access.get("status"), self.vocabularies)
        self.add_claim(
            subject=resource_id,
            predicate="access_pathway",
            value=access_term,
            evidence=access_context,
            resolution_status="not_established" if access_term == "not_established" else "selected",
        )
        support = block.get("support") or {}
        support_context = _best_context(
            None,
            uid=uid,
            card_sha256=card_sha256,
            meta=meta,
            fallback_url=homepage,
            fallback_tier="attestation",
            reviewed_at=reviewed_at,
            method="repository_status:support",
        )
        if support.get("current_nih_funding_support") is not None:
            self.add_claim(
                subject=resource_id,
                predicate="current_nih_funding_support",
                value=bool(support.get("current_nih_funding_support")),
                evidence=support_context,
            )
        if support.get("sustained_support") is not None:
            self.add_claim(
                subject=resource_id,
                predicate="sustained_support",
                value=bool(support.get("sustained_support")),
                evidence=support_context,
                limitations=["Evidence of sustained NIH support is not a guarantee of continued funding or preservation."],
            )
        return access_term

    def _compile_human_data_context(
        self,
        resource_id: str,
        organism_scopes: list[str],
        access_term: str,
        card_context: EvidenceContext,
    ) -> None:
        term_id = infer_human_data_sensitivity(organism_scopes, access_term, self.vocabularies)
        evidence = EvidenceContext(
            source=card_context.source,
            source_url=card_context.source_url,
            source_date=card_context.source_date,
            tier=card_context.tier,
            method="deterministic_human_data_context",
            source_sha256=card_context.source_sha256,
            reviewed_at=card_context.reviewed_at,
        )
        self.add_claim(
            subject=resource_id,
            predicate="human_data_sensitivity",
            value=term_id,
            evidence=evidence,
            resolution_status="not_established" if term_id == "not_established" else "selected",
            limitations=[
                "This public facet combines resource-level organism terms and access pathway. It does not independently classify consent, de-identification, privacy, or legal sensitivity for individual data assets."
            ] if term_id != "not_applicable" else [
                "No human-data context was established from the current source-bound organism and model-system evidence."
            ],
        )

    def _compile_policy(
        self,
        resource_id: str,
        uid: str,
        raw_block: Any,
        card_sha256: str,
        reviewed_at: str,
    ) -> None:
        block = normalize_policy_context(raw_block)
        meta = _meta(block)
        cadr = block.get("nih_cadr") or {}
        cadr_context = _best_context(
            cadr if isinstance(cadr, dict) else None,
            uid=uid,
            card_sha256=card_sha256,
            meta=meta,
            fallback_url=safe_url(cadr.get("source_url")) or safe_url(cadr.get("url")),
            fallback_tier="policy",
            reviewed_at=reviewed_at,
            method="policy_context:nih_cadr",
        )
        status = clean(cadr.get("status")) or "not_evaluated"
        self.add_claim(
            subject=resource_id,
            predicate="nih_cadr_status",
            value=status,
            evidence=cadr_context,
            resolution_status="selected" if status == "listed" else "not_established" if status in {"not_evaluated", "ambiguous"} else "selected",
            limitations=["NIH CADR listing is a policy designation and does not by itself establish current user-facing availability."],
        )
        if status == "listed":
            designation_id = node_id("PolicyDesignation", "NIH Controlled-Access Data Repository")
            self.add_node(
                node_id_value=designation_id,
                node_type="PolicyDesignation",
                label="NIH Controlled-Access Data Repository",
                properties={"url": safe_url(cadr.get("source_url")) or safe_url(cadr.get("url"))},
            )
            self.add_relation_claim(
                subject=resource_id,
                predicate="HAS_POLICY_DESIGNATION",
                object_id=designation_id,
                evidence=cadr_context,
                claim_predicate="policy_designation",
            )
        if cadr.get("nih_designated_genomic_data_repository") is not None:
            self.add_claim(
                subject=resource_id,
                predicate="nih_designated_genomic_data_repository",
                value=bool(cadr.get("nih_designated_genomic_data_repository")),
                evidence=cadr_context,
            )
        access_system = clean(cadr.get("access_system"))
        if access_system:
            access_id = node_id("AccessSystem", access_system)
            self.add_node(
                node_id_value=access_id,
                node_type="AccessSystem",
                label=access_system,
                properties={"source_url": safe_url(cadr.get("source_url"))},
            )
            self.add_relation_claim(
                subject=resource_id,
                predicate="HAS_ACCESS_SYSTEM",
                object_id=access_id,
                evidence=cadr_context,
                claim_predicate="access_system",
            )
        eds = block.get("dbgap_external_data_source") or {}
        eds_context = _best_context(
            eds if isinstance(eds, dict) else None,
            uid=uid,
            card_sha256=card_sha256,
            meta=meta,
            fallback_url=safe_url(eds.get("source_url")),
            fallback_tier="policy",
            reviewed_at=reviewed_at,
            method="policy_context:dbgap_external_data_source",
        )
        self.add_claim(
            subject=resource_id,
            predicate="dbgap_external_data_source_status",
            value=clean(eds.get("status")) or "not_established",
            evidence=eds_context,
            resolution_status="not_established" if clean(eds.get("status")) in {"", "not_established"} else "selected",
            limitations=[clean(eds.get("note"))] if clean(eds.get("note")) else [],
        )
        studies = block.get("dbgap_public_studies") or {}
        if int(studies.get("study_count") or 0) > 0:
            study_context = _best_context(
                studies if isinstance(studies, dict) else None,
                uid=uid,
                card_sha256=card_sha256,
                meta=meta,
                fallback_tier="policy",
                reviewed_at=reviewed_at,
                method="policy_context:dbgap_public_studies",
            )
            self.add_claim(
                subject=resource_id,
                predicate="dbgap_public_study_count",
                value=int(studies.get("study_count") or 0),
                evidence=study_context,
                limitations=[clean(studies.get("limitation"))] if clean(studies.get("limitation")) else [],
            )

    def _compile_publishers(
        self,
        resource_id: str,
        uid: str,
        raw_block: Any,
        card_sha256: str,
        reviewed_at: str,
    ) -> None:
        block = normalize_publisher_context(raw_block)
        meta = _meta(block)
        predicate_map = {
            "mandatory": "REQUIRED_BY_PUBLISHER_POLICY",
            "recommended": "RECOMMENDED_BY_PUBLISHER_POLICY",
            "preferred": "PREFERRED_BY_PUBLISHER_POLICY",
            "acceptable_fallback": "ACCEPTABLE_FALLBACK_FOR_PUBLISHER_POLICY",
            "example": "MENTIONED_BY_PUBLISHER_POLICY",
            "directory_referral": "MENTIONED_BY_PUBLISHER_POLICY",
        }
        for item in block.get("mentions") or []:
            if not isinstance(item, dict):
                continue
            publisher = clean(item.get("publisher_family"))
            policy_url = safe_url(item.get("policy_url"))
            if not publisher or not policy_url:
                continue
            policy_id = node_id("PublisherPolicy", [publisher, item.get("journal_title"), policy_url])
            self.add_node(
                node_id_value=policy_id,
                node_type="PublisherPolicy",
                label=clean(item.get("policy_title")) or f"{publisher} repository policy",
                properties={
                    "publisher_family": publisher,
                    "journal_title": clean(item.get("journal_title")),
                    "policy_url": policy_url,
                    "policy_level": clean(item.get("policy_level")),
                    "mention_type": clean(item.get("mention_type")),
                    "data_domain": clean(item.get("data_domain")),
                    "access_model": clean(item.get("access_model")),
                    "date_checked": clean(item.get("date_checked")),
                },
            )
            evidence = _best_context(
                item,
                uid=uid,
                card_sha256=card_sha256,
                meta=meta,
                fallback_url=policy_url,
                fallback_tier="policy",
                reviewed_at=reviewed_at,
                method="publisher_policy",
            )
            mention_type = clean(item.get("mention_type"))
            self.add_relation_claim(
                subject=resource_id,
                predicate=predicate_map.get(mention_type, "MENTIONED_BY_PUBLISHER_POLICY"),
                object_id=policy_id,
                evidence=evidence,
                claim_predicate="publisher_policy",
                properties={"mention_type": mention_type},
                limitations=[clean(block.get("limitation"))] if clean(block.get("limitation")) else [],
            )

    def _finalize_scope_claims(
        self,
        resource_id: str,
        uid: str,
        blocks: dict[str, Any],
        card_context: EvidenceContext,
    ) -> None:
        claims = [self.claims[identifier] for identifier in self._claim_ids_by_subject[resource_id]]
        by_predicate: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for claim in claims:
            by_predicate[claim["predicate"]].append(claim)
        relationships: list[str] = []
        if any(claim.get("value") is True for claim in by_predicate.get("current_nih_funding_support", [])):
            relationships.append("currently_nih_funded")
        if any(claim.get("value") is True for claim in by_predicate.get("sustained_support", [])):
            relationships.append("sustained_historical_nih_support")
        if by_predicate.get("nih_ico_association"):
            relationships.append("ico_associated")
        if not relationships:
            relationships.append("external_relevant")
        for term_id in relationships:
            self.add_claim(
                subject=resource_id,
                predicate="nih_relationship",
                value=self.vocabularies.validate("nih_relationship", term_id),
                evidence=card_context,
                limitations=["Inclusion in the tracker is not NIH designation, endorsement, or a determination of policy compliance."],
            )

    def sorted_nodes(self) -> list[dict[str, Any]]:
        for node_id_value, claim_ids in self._claim_ids_by_subject.items():
            if node_id_value in self.nodes:
                self.nodes[node_id_value]["source_claim_ids"] = sorted(claim_ids)
        return [copy.deepcopy(self.nodes[key]) for key in sorted(self.nodes)]

    def sorted_edges(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(self.edges[key]) for key in sorted(self.edges)]

    def sorted_claims(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(self.claims[key]) for key in sorted(self.claims)]

    def input_fingerprint(self) -> str:
        return stable_hash(sorted(self.card_sha256.items()), length=64)
