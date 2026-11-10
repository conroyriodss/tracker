"""Claim-level evidence helpers for the v19 derived catalog."""
from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from catalog_schema import claim_id, node_id


HIGH_STAKES_PREDICATES = {
    "operational_status",
    "submission_model",
    "access_pathway",
    "nih_cadr_status",
    "nih_designated_genomic_data_repository",
    "current_nih_funding_support",
}
MODERATE_CHANGE_PREDICATES = {
    "api_service",
    "programmatic_access",
    "standard",
    "pid_system",
    "guid_system",
    "resource_type",
    "data_asset_type",
    "subject_area",
    "taxonomy",
    "model_system",
    "organism_scope",
    "human_data_sensitivity",
    "data_format",
    "organization",
    "publisher_policy",
    "contact",
    "sustained_support",
}


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def safe_url(value: Any) -> str:
    text = clean(value)
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def parse_date(value: Any) -> dt.date | None:
    text = clean(value)
    if not text:
        return None
    match = re.fullmatch(r"(\d{4})", text)
    if match:
        return dt.date(int(match.group(1)), 1, 1)
    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return dt.date(year, month, 1)
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    try:
        normalized = text.replace("Z", "+00:00")
        return dt.datetime.fromisoformat(normalized).date()
    except ValueError:
        return None


def add_months(value: dt.date, months: int) -> dt.date:
    total = value.year * 12 + (value.month - 1) + months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def review_interval_months(predicate: str) -> int:
    if predicate in HIGH_STAKES_PREDICATES:
        return 6
    if predicate in MODERATE_CHANGE_PREDICATES:
        return 12
    return 24


def review_due_for(predicate: str, source_date: Any) -> str:
    parsed = parse_date(source_date)
    if parsed is None:
        return ""
    return add_months(parsed, review_interval_months(predicate)).isoformat()


def evidence_state_for(*, tier: Any = "", source: Any = "", verified: bool = False) -> str:
    tier_key = clean(tier).casefold()
    source_key = clean(source).casefold()
    if verified or tier_key == "verified":
        return "verified"
    if tier_key in {"policy", "curated", "attestation"}:
        return "officially_reported"
    if tier_key == "registry":
        return "registry_reported"
    if tier_key == "declared":
        if any(token in source_key for token in ("website", "official", "nih", "bmic", "operator")):
            return "officially_reported"
        return "registry_reported"
    if any(token in source_key for token in ("re3data", "fairsharing", "rrid", "scicrunch")):
        return "registry_reported"
    if any(token in source_key for token in ("website", "official", "nih", "bmic", "policy")):
        return "officially_reported"
    return "not_established"


@dataclass(frozen=True)
class EvidenceContext:
    source: str
    source_url: str = ""
    source_date: str = ""
    tier: str = ""
    method: str = ""
    source_sha256: str = ""
    reviewed_at: str = ""
    limitations: tuple[str, ...] = field(default_factory=tuple)
    verified: bool = False

    @property
    def source_id(self) -> str:
        natural = self.source_url or self.source or self.source_sha256 or "unknown"
        return node_id("EvidenceSource", natural)

    @property
    def evidence_state(self) -> str:
        return evidence_state_for(tier=self.tier, source=self.source, verified=self.verified)

    def source_node(self) -> dict[str, Any]:
        label = self.source or self.source_url or "Evidence source not established"
        return {
            "id": self.source_id,
            "type": "EvidenceSource",
            "label": label,
            "properties": {
                "url": safe_url(self.source_url),
                "source_date": self.source_date,
                "method": self.method,
                "source_sha256": self.source_sha256,
                "tier": self.tier,
            },
            "source_claim_ids": [],
        }


def claim_record(
    *,
    subject: str,
    predicate: str,
    evidence: EvidenceContext,
    object_id: str = "",
    value: Any = None,
    resolution_status: str = "selected",
    limitations: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    if not object_id and value is None:
        raise ValueError("claim_record requires object_id or value")
    combined_limitations = sorted(
        {
            clean(item)
            for item in [*(evidence.limitations or ()), *((limitations or []))]
            if clean(item)
        }
    )
    record: dict[str, Any] = {
        "id": claim_id(
            subject,
            predicate,
            object_id=object_id,
            value=value,
            source_id=evidence.source_id,
        ),
        "subject": subject,
        "predicate": predicate,
        "evidence_state": evidence.evidence_state,
        "source_id": evidence.source_id,
        "source_url": safe_url(evidence.source_url),
        "source_date": evidence.source_date,
        "observed_at": evidence.source_date,
        "reviewed_at": evidence.reviewed_at,
        "review_due": review_due_for(predicate, evidence.source_date or evidence.reviewed_at),
        "method": evidence.method,
        "source_sha256": evidence.source_sha256,
        "resolution_status": resolution_status,
        "limitations": combined_limitations,
    }
    if object_id:
        record["object_id"] = object_id
    else:
        record["value"] = value
    return record


def context_from_item(
    item: dict[str, Any] | None,
    *,
    fallback_source: str,
    fallback_url: str = "",
    fallback_date: str = "",
    fallback_tier: str = "",
    fallback_sha256: str = "",
    reviewed_at: str = "",
    method: str = "card_to_catalog",
    verified: bool = False,
) -> EvidenceContext:
    item = item if isinstance(item, dict) else {}
    source = clean(item.get("source") or fallback_source)
    source_url = safe_url(
        item.get("source_url")
        or item.get("documentation_url")
        or item.get("url")
        or fallback_url
    )
    source_date = clean(item.get("as_of") or item.get("source_as_of") or fallback_date)
    tier = clean(item.get("tier") or fallback_tier)
    source_sha256 = clean(
        item.get("source_sha256")
        or item.get("content_sha256")
        or fallback_sha256
    )
    item_method = clean(item.get("extraction_method") or item.get("match_basis") or method)
    return EvidenceContext(
        source=source,
        source_url=source_url,
        source_date=source_date,
        tier=tier,
        method=item_method,
        source_sha256=source_sha256,
        reviewed_at=reviewed_at,
        verified=verified or bool(item.get("verified")),
    )
