"""Build deterministic repository identity name sets from local source exports.

The emitted identity is normalized through ``identity_hygiene.clean_identity`` before
comparison or write. This makes the step idempotent and prevents the following cleanup
step from removing aliases that this step would otherwise reintroduce on every run.

A valid no-op is success. Repeated dry-run/apply executions may therefore report zero
changed cards while still passing all invariants.

Run (dry):  python step_build_names.py
Apply:      python step_build_names.py --apply
"""
from __future__ import annotations

import argparse
import copy
import os
import re
from typing import Any

import pandas as pd

from common import (
    INP,
    NOW,
    all_cards,
    card_transaction,
    gate,
    ledger_append,
    load_card_for_update,
    new_run_dir,
    save_card,
    slug,
    strip_acronym,
)
from identity_hygiene import clean_identity, identity_hygiene_issues, is_acronym

YM = NOW.strftime("%Y-%m")
SAMPLE_COLUMNS = ["repo_uid", "name", "abbreviation", "aliases"]


def _clean(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return (
        None
        if text.lower() in {"", "undefined", "null", "n/a", "na", "nan", "none", "-"}
        else text
    )


def _split_list(value: Any) -> list[str]:
    text = _clean(value)
    return [part.strip() for part in re.split(r"[;,|]", text) if part.strip()] if text else []


def build_local_index(tools_path: str, registry_path: str) -> dict[str, set[str]]:
    """Return normalized-name -> alternate-name set from local exports."""
    index: dict[str, set[str]] = {}

    def add(key_names: list[str], alternate_names: set[str]) -> None:
        alternates = {value for value in alternate_names if value}
        for key_name in key_names:
            key = slug(strip_acronym(key_name)[0]) if key_name else ""
            if key:
                index.setdefault(key, set()).update(alternates)

    if os.path.exists(tools_path):
        tools = pd.read_csv(tools_path)
        tools.columns = [column.strip() for column in tools.columns]
        for _, row in tools.iterrows():
            name = _clean(row.get("Resource Name"))
            abbreviations = _split_list(row.get("Abbreviations"))
            synonyms = _split_list(row.get("Synonyms"))
            names = [value for value in [name, *abbreviations, *synonyms] if value]
            add(names, set(names))

    if os.path.exists(registry_path):
        registry = pd.read_csv(registry_path)
        registry.columns = [column.strip() for column in registry.columns]
        for _, row in registry.iterrows():
            names = [
                _clean(row.get(column))
                for column in ("Repository_Name", "FS_Name", "RE3_Name", "SC_Name")
            ]
            synonyms = _split_list(row.get("SC_Synonyms"))
            all_names = [value for value in names if value] + synonyms
            add(all_names, set(all_names))

    return index


def enrich_identity(card: dict[str, Any], local_index: dict[str, set[str]], bmic_name=None):
    """Return the normalized source-derived identity for one card."""
    identity = card.get("identity", {}) or {}
    name = _clean(identity.get("name")) or ""
    base, parenthetical_acronym = strip_acronym(name)

    abbreviation = _clean(identity.get("abbreviation"))
    full_name = _clean(identity.get("full_name"))
    aliases = set(identity.get("aliases") or [])
    provenance = dict(identity.get("name_provenance") or {})

    if parenthetical_acronym and not abbreviation:
        abbreviation = parenthetical_acronym
    if parenthetical_acronym and base and not full_name:
        full_name = base
    if is_acronym(name) and not abbreviation:
        abbreviation = name

    if bmic_name and slug(strip_acronym(bmic_name)[0]) != slug(base):
        if bmic_name not in aliases and bmic_name != name:
            aliases.add(bmic_name)
            provenance[bmic_name] = {"source": "BMIC-list", "as_of": YM}

    keys = {slug(base)}
    if abbreviation:
        keys.add(slug(abbreviation))
    for key in sorted(value for value in keys if value):
        for alternate in sorted(local_index.get(key, set()), key=lambda value: (value.casefold(), value)):
            if not alternate or alternate == name or alternate == base:
                continue
            if alternate not in aliases:
                aliases.add(alternate)
                provenance.setdefault(
                    alternate,
                    {"source": "registry_export", "as_of": YM},
                )

    aliases.discard(name)
    if full_name:
        aliases.discard(full_name)
    if abbreviation:
        aliases.discard(abbreviation)

    proposed: dict[str, Any] = {"name": name}
    if abbreviation:
        proposed["abbreviation"] = abbreviation
    if full_name and full_name != name:
        proposed["full_name"] = full_name
    for key, value in identity.items():
        if key not in {
            "name",
            "abbreviation",
            "full_name",
            "aliases",
            "name_provenance",
        }:
            proposed[key] = value
    if aliases:
        proposed["aliases"] = sorted(aliases, key=lambda value: (value.casefold(), value))
    if provenance:
        proposed["name_provenance"] = provenance

    normalized, _, _ = clean_identity(proposed)
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tools", default="Tools467results.csv")
    parser.add_argument("--registry", default="final_ultimate_enhanced_repos.csv")
    parser.add_argument("--bmic", default="BMIC_download.csv")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    tools_path = os.path.join(INP, args.tools)
    registry_path = os.path.join(INP, args.registry)
    local_index = build_local_index(tools_path, registry_path)

    bmic_by_slug: dict[str, str] = {}
    bmic_path = os.path.join(INP, args.bmic)
    if os.path.exists(bmic_path):
        bmic = pd.read_csv(bmic_path)
        bmic.columns = [column.strip() for column in bmic.columns]
        name_column = "Repository_Name" if "Repository_Name" in bmic.columns else bmic.columns[2]
        for _, row in bmic.iterrows():
            name = _clean(row.get(name_column))
            if name:
                bmic_by_slug[slug(strip_acronym(name)[0])] = name

    cards = all_cards()
    expected_cards = copy.deepcopy(cards)
    changed = 0
    aliases_added = 0
    aliases_removed = 0
    changed_with_abbreviation = 0
    sample: list[dict[str, str]] = []
    changed_cards = {}

    for uid, card in cards.items():
        base = strip_acronym(card.get("identity", {}).get("name", ""))[0]
        bmic_name = bmic_by_slug.get(slug(base))
        before = card.get("identity", {}) or {}
        after = enrich_identity(card, local_index, bmic_name)
        if after == before:
            continue

        before_aliases = set(before.get("aliases") or [])
        after_aliases = set(after.get("aliases") or [])
        aliases_added += len(after_aliases.difference(before_aliases))
        aliases_removed += len(before_aliases.difference(after_aliases))
        changed_with_abbreviation += int(bool(after.get("abbreviation")))
        changed += 1
        card["identity"] = after
        changed_cards[uid] = card
        if len(sample) < 12:
            sample.append(
                {
                    "repo_uid": uid,
                    "name": str(after.get("name") or ""),
                    "abbreviation": str(after.get("abbreviation") or ""),
                    "aliases": "; ".join(after.get("aliases", [])),
                }
            )

    hygiene_failures = {
        uid: identity_hygiene_issues(card.get("identity", {}) or {})
        for uid, card in cards.items()
    }
    hygiene_failures = {
        uid: issues for uid, issues in hygiene_failures.items() if issues
    }
    canonical_names_present = all(
        card.get("identity", {}).get("name") for card in cards.values()
    )
    outcome = (
        f"{changed} card(s) differ from normalized source-derived identity"
        if changed
        else "no changes required; source-derived identity state is current"
    )
    checks = {
        "cards considered": (len(cards) > 0, f"{len(cards)} cards"),
        "canonical name kept": (canonical_names_present, "name preserved"),
        "identity hygiene valid": (
            not hygiene_failures,
            "normalized output is valid"
            if not hygiene_failures
            else f"invalid cards: {list(hygiene_failures)[:5]}",
        ),
        "enrichment outcome": (True, outcome),
    }
    ok = gate(checks)

    applied = 0
    if args.apply and ok and changed_cards:
        with card_transaction("build_names"):
            for uid in sorted(changed_cards):
                load_card_for_update(uid, expected_cards[uid])
            for uid in sorted(changed_cards):
                save_card(uid, changed_cards[uid])
                applied += 1
        print(f"\n  Transactionally applied {applied} identity update(s).")
    elif args.apply and ok:
        print("\n  No card writes required; identity names are already current and normalized.")
    elif args.apply:
        print("\n  Gate failed; no card changes were written.")

    run_dir = new_run_dir("build_names")
    pd.DataFrame(sample, columns=SAMPLE_COLUMNS).to_csv(
        os.path.join(run_dir, "names_sample.csv"), index=False
    )

    print(f"\n  cards changed              : {changed}/{len(cards)}")
    print(f"  changed cards with abbr.   : {changed_with_abbreviation}")
    print(f"  aliases added (net exact)  : {aliases_added}")
    print(f"  aliases removed by hygiene : {aliases_removed}")
    if sample:
        print("\n  sample:")
        for row in sample:
            aliases = row["aliases"].split("; ") if row["aliases"] else []
            print(
                f"    {row['repo_uid']:<6} {row['name'][:34]:<34} "
                f"abbr={row['abbreviation'] or '-':<10} aliases={aliases[:2]}"
            )
    else:
        print("\n  sample: no changes")
    if not args.apply:
        print("\n  DRY RUN - inspect names_sample.csv; zero changes is a valid result.")

    ledger_append(
        "build_names",
        params={
            "tools": args.tools,
            "registry": args.registry,
            "bmic": args.bmic,
            "apply": args.apply,
        },
        inputs={"tools": tools_path, "registry": registry_path, "bmic": bmic_path},
        outputs={
            "cards": len(cards),
            "changed": changed,
            "applied": applied,
            "changed_with_abbreviation": changed_with_abbreviation,
            "aliases_added": aliases_added,
            "aliases_removed_by_hygiene": aliases_removed,
            "no_op": changed == 0,
            "hygiene_failures": len(hygiene_failures),
        },
        gate_passed=ok,
        run_dir=run_dir,
    )
    print(f"\nCompleted: {run_dir}")


if __name__ == "__main__":
    main()
