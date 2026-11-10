"""Meaningful, field-level history for derived public resource views."""
from __future__ import annotations

import copy
from typing import Any

from catalog_schema import stable_hash


TRACKED_PATHS = {
    "operational.id": "Operational status",
    "deposit.id": "Deposit pathway",
    "access.id": "Access pathway",
    "nih_relationship.categories": "NIH relationship",
    "policy.nih_cadr_status": "NIH CADR status",
    "policy.nih_designated_genomic_data_repository": "NIH-designated genomic repository status",
    "interoperability.api_status": "API evidence status",
    "interoperability.programmatic_access.id": "Programmatic access",
    "interoperability.persistent_identifiers.id": "Persistent identifier level",
    "resource_types": "Resource type",
    "best_fit.modalities": "Data modalities",
    "best_fit.organism_scopes": "Organism or model-system scope",
    "best_fit.human_data_sensitivity.id": "Human-data access context",
    "best_fit.subject_areas": "Subject areas",
}


def _get(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _canonical_value(value: Any) -> Any:
    if isinstance(value, list):
        normalized = []
        for item in value:
            if isinstance(item, dict):
                normalized.append(item.get("id") or item.get("label") or item)
            else:
                normalized.append(item)
        return sorted(normalized, key=lambda item: str(item))
    if isinstance(value, dict):
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    return value


def build_change_history(
    previous_resources: list[dict[str, Any]],
    current_resources: list[dict[str, Any]],
    *,
    effective_date: str,
    prior_catalog_id: str = "",
    current_catalog_id: str = "",
) -> list[dict[str, Any]]:
    previous = {str(item.get("repo_uid")): item for item in previous_resources}
    current = {str(item.get("repo_uid")): item for item in current_resources}
    changes: list[dict[str, Any]] = []
    for uid in sorted(set(previous) | set(current)):
        before = previous.get(uid)
        after = current.get(uid)
        if before is None and after is not None:
            changes.append(
                {
                    "id": f"change:{stable_hash([uid, 'resource_added', current_catalog_id])}",
                    "repo_uid": uid,
                    "effective_date": effective_date,
                    "change_type": "resource_added",
                    "field": "resource",
                    "label": "Resource added",
                    "before": None,
                    "after": after.get("name"),
                    "prior_catalog_id": prior_catalog_id,
                    "current_catalog_id": current_catalog_id,
                }
            )
            continue
        if after is None and before is not None:
            changes.append(
                {
                    "id": f"change:{stable_hash([uid, 'resource_removed', current_catalog_id])}",
                    "repo_uid": uid,
                    "effective_date": effective_date,
                    "change_type": "resource_removed",
                    "field": "resource",
                    "label": "Resource removed",
                    "before": before.get("name"),
                    "after": None,
                    "prior_catalog_id": prior_catalog_id,
                    "current_catalog_id": current_catalog_id,
                }
            )
            continue
        assert before is not None and after is not None
        for path, label in TRACKED_PATHS.items():
            old = _canonical_value(_get(before, path))
            new = _canonical_value(_get(after, path))
            if old == new:
                continue
            changes.append(
                {
                    "id": f"change:{stable_hash([uid, path, old, new, current_catalog_id])}",
                    "repo_uid": uid,
                    "effective_date": effective_date,
                    "change_type": "field_changed",
                    "field": path,
                    "label": label,
                    "before": copy.deepcopy(old),
                    "after": copy.deepcopy(new),
                    "prior_catalog_id": prior_catalog_id,
                    "current_catalog_id": current_catalog_id,
                }
            )
    return changes
