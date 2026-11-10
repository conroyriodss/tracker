"""Deterministic identity-name hygiene shared by enrichment and cleanup steps.

The functions in this module are pure: they do not read project files or write cards.
They define the normalized identity representation that ``step_build_names.py`` emits
and ``step_clean_aliases.py`` enforces. Keeping one implementation prevents the two
steps from alternately adding and removing the same aliases on every pipeline run.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from common import slug, strip_acronym


def is_acronym(value: Any) -> bool:
    """Return whether *value* is a plausible short acronym token."""
    text = str(value or "").strip()
    if not text or len(text) > 12:
        return False
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return False
    return (
        sum(1 for character in letters if character.isupper()) / len(letters) >= 0.6
        and len(text.split()) == 1
    )


def acronym_consistent(alias: Any, name: Any, full_name: Any) -> bool:
    """Return whether *alias* plausibly abbreviates ``name`` or ``full_name``.

    This intentionally requires an initials relationship. It accepts abbreviations
    such as ``NDA`` for ``NIMH Data Archive`` while rejecting arbitrary short labels.
    """
    acronym = re.sub(r"[^a-z0-9]", "", str(alias or "").lower())
    if not acronym:
        return False
    for source in (full_name, name):
        if not source:
            continue
        words = re.findall(r"[A-Za-z0-9]+", str(source))
        initials = "".join(word[0].lower() for word in words if word)
        if acronym == initials:
            return True
        iterator = iter(initials)
        if (
            all(character in iterator for character in acronym)
            and len(acronym) >= 2
            and abs(len(acronym) - len(initials)) <= 2
        ):
            return True
    return False


def _canonical_slugs(name: str, full_name: str, abbreviation: str) -> set[str]:
    values = (name, full_name, abbreviation)
    result = {slug(strip_acronym(value)[0]) for value in values if value}
    result.update(slug(value) for value in values if value)
    result.discard("")
    return result


def clean_identity(identity: Mapping[str, Any] | None):
    """Return ``(normalized_identity, dropped_aliases, promoted_abbreviation)``.

    Normalization removes aliases equivalent to canonical names, promotes a valid
    acronym only when the abbreviation field is empty, removes normalized duplicate
    aliases, prunes orphan provenance, and emits deterministic alias/provenance order.
    """
    ident = dict(identity or {})
    name = str(ident.get("name") or "").strip()
    full_name = str(ident.get("full_name") or "").strip()
    abbreviation = str(ident.get("abbreviation") or "").strip()
    aliases = list(ident.get("aliases") or [])
    provenance = dict(ident.get("name_provenance") or {})

    canonical_slugs = _canonical_slugs(name, full_name, abbreviation)
    kept: list[str] = []
    dropped: list[str] = []
    promoted: str | None = None
    seen: set[str] = set()

    for raw_alias in aliases:
        alias = str(raw_alias or "").strip()
        if not alias:
            continue
        stripped_slug = slug(strip_acronym(alias)[0])
        raw_slug = slug(alias)
        if stripped_slug in canonical_slugs or raw_slug in canonical_slugs:
            dropped.append(alias)
            provenance.pop(alias, None)
            continue
        if (
            not abbreviation
            and is_acronym(alias)
            and acronym_consistent(alias, name, full_name)
        ):
            abbreviation = alias
            promoted = alias
            provenance.pop(alias, None)
            canonical_slugs = _canonical_slugs(name, full_name, abbreviation)
            continue
        if raw_slug in seen:
            dropped.append(alias)
            provenance.pop(alias, None)
            continue
        seen.add(raw_slug)
        kept.append(alias)

    kept = sorted(kept, key=lambda value: (value.casefold(), value))
    kept_set = set(kept)

    normalized: dict[str, Any] = {"name": name}
    if abbreviation:
        normalized["abbreviation"] = abbreviation
    if full_name and full_name != name:
        normalized["full_name"] = full_name
    for key, value in ident.items():
        if key not in {
            "name",
            "abbreviation",
            "full_name",
            "aliases",
            "name_provenance",
        }:
            normalized[key] = value
    if kept:
        normalized["aliases"] = kept

    retained_provenance = {
        key: provenance[key]
        for key in sorted(provenance, key=lambda value: (value.casefold(), value))
        if key in kept_set
    }
    if retained_provenance:
        normalized["name_provenance"] = retained_provenance
    return normalized, dropped, promoted


def identity_hygiene_issues(identity: Mapping[str, Any] | None) -> list[str]:
    """Return structural/hygiene defects without mutating *identity*."""
    ident = dict(identity or {})
    issues: list[str] = []
    name = str(ident.get("name") or "").strip()
    if not name:
        issues.append("missing identity.name")

    aliases_value = ident.get("aliases", [])
    if aliases_value is None:
        aliases: list[Any] = []
    elif isinstance(aliases_value, list):
        aliases = aliases_value
    else:
        issues.append("identity.aliases is not a list")
        aliases = []

    if "aliases" in ident and not aliases:
        issues.append("empty identity.aliases should be omitted")

    abbreviation = str(ident.get("abbreviation") or "").strip()
    full_name = str(ident.get("full_name") or "").strip()
    canonical_slugs = _canonical_slugs(name, full_name, abbreviation)
    seen: set[str] = set()
    alias_strings: set[str] = set()
    for raw_alias in aliases:
        alias = str(raw_alias or "").strip()
        if not alias:
            issues.append("blank alias")
            continue
        alias_strings.add(alias)
        stripped_slug = slug(strip_acronym(alias)[0])
        raw_slug = slug(alias)
        if stripped_slug in canonical_slugs or raw_slug in canonical_slugs:
            issues.append(f"redundant alias: {alias}")
        if raw_slug in seen:
            issues.append(f"duplicate normalized alias: {alias}")
        seen.add(raw_slug)

    provenance = ident.get("name_provenance") or {}
    if not isinstance(provenance, Mapping):
        issues.append("identity.name_provenance is not a mapping")
    else:
        orphan_keys = sorted(set(map(str, provenance)).difference(alias_strings))
        for key in orphan_keys:
            issues.append(f"orphan alias provenance: {key}")
    return issues
