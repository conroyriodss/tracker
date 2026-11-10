"""Canonical publisher-policy context for NIH Tracker repository cards."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

SCHEMA_VERSION = "1.0"
MENTION_TYPES = {
    "mandatory",
    "recommended",
    "preferred",
    "acceptable_fallback",
    "example",
    "directory_referral",
}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def empty_publisher_context() -> dict[str, Any]:
    return {
        "_meta": {
            "block_version": SCHEMA_VERSION,
            "as_of": "",
            "tier": "policy",
            "review_state": "auto",
            "sources": [],
        },
        "publisher_mentioned": False,
        "publisher_recommended": False,
        "publisher_mandated": False,
        "publisher_acceptable_fallback": False,
        "publisher_count": 0,
        "mentions": [],
        "limitation": (
            "Publisher policies vary by journal, article type, and data type. A mention does not "
            "supersede NIH, consent, legal, institutional, or repository intake requirements."
        ),
    }


def normalize_publisher_context(value: Any) -> dict[str, Any]:
    result = empty_publisher_context()
    if not isinstance(value, Mapping):
        return result
    meta = value.get("_meta") if isinstance(value.get("_meta"), Mapping) else {}
    result["_meta"].update(
        {
            "block_version": _clean(meta.get("block_version")) or SCHEMA_VERSION,
            "as_of": _clean(meta.get("as_of")),
            "tier": _clean(meta.get("tier")) or "policy",
            "review_state": _clean(meta.get("review_state")) or "auto",
            "sources": list(
                dict.fromkeys(_clean(item) for item in _items(meta.get("sources")) if _clean(item))
            ),
        }
    )
    mentions: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw in _items(value.get("mentions")):
        if not isinstance(raw, Mapping):
            continue
        mention_type = _clean(raw.get("mention_type"))
        if mention_type not in MENTION_TYPES:
            continue
        item = {
            "publisher_family": _clean(raw.get("publisher_family")),
            "journal_title": _clean(raw.get("journal_title")),
            "policy_title": _clean(raw.get("policy_title")),
            "policy_url": _clean(raw.get("policy_url")),
            "policy_level": _clean(raw.get("policy_level")),
            "mention_type": mention_type,
            "data_domain": _clean(raw.get("data_domain")),
            "access_model": _clean(raw.get("access_model")),
            "repository_name_as_listed": _clean(raw.get("repository_name_as_listed")),
            "source_location": _clean(raw.get("source_location")),
            "evidence_note": _clean(raw.get("evidence_note")),
            "date_checked": _clean(raw.get("date_checked")),
            "source_sha256": _clean(raw.get("source_sha256")),
            "source_snapshot": _clean(raw.get("source_snapshot")),
        }
        if not item["publisher_family"] or not item["policy_url"]:
            continue
        key = (
            item["publisher_family"].casefold(),
            item["journal_title"].casefold(),
            item["policy_url"],
            item["mention_type"],
        )
        if key in seen:
            continue
        seen.add(key)
        mentions.append(item)
    mentions.sort(
        key=lambda item: (
            item["publisher_family"].casefold(),
            item["journal_title"].casefold(),
            item["mention_type"],
            item["policy_url"],
        )
    )
    result["mentions"] = mentions
    mention_types = {item["mention_type"] for item in mentions}
    result["publisher_mentioned"] = bool(mentions)
    result["publisher_recommended"] = bool(
        mention_types & {"mandatory", "recommended", "preferred"}
    )
    result["publisher_mandated"] = "mandatory" in mention_types
    result["publisher_acceptable_fallback"] = "acceptable_fallback" in mention_types
    result["publisher_count"] = len(
        {item["publisher_family"].casefold() for item in mentions if item["publisher_family"]}
    )
    result["limitation"] = _clean(value.get("limitation")) or result["limitation"]
    return result


def publisher_context_summary(value: Any) -> dict[str, Any]:
    block = normalize_publisher_context(value)
    publishers = sorted(
        {item["publisher_family"] for item in block["mentions"] if item["publisher_family"]},
        key=str.casefold,
    )
    types = sorted({item["mention_type"] for item in block["mentions"]})
    return {
        "publisher_mentioned": int(block["publisher_mentioned"]),
        "publisher_recommended": int(block["publisher_recommended"]),
        "publisher_mandated": int(block["publisher_mandated"]),
        "publisher_acceptable_fallback": int(block["publisher_acceptable_fallback"]),
        "publisher_count": block["publisher_count"],
        "publisher_families": "; ".join(publishers),
        "publisher_mention_types": "; ".join(types),
    }


def copy_publisher_context(value: Any) -> dict[str, Any]:
    return deepcopy(normalize_publisher_context(value))
