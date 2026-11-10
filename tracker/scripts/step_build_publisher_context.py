"""Build publisher-policy context blocks from a pinned structured inventory.

Publisher relationships are coded as mandatory, recommended, preferred,
acceptable fallback, example, or directory referral.  The public
``publisher_recommended`` flag is true only for mandatory/recommended/preferred
relationships; mere examples and directory referrals remain distinguishable.

Rows are attached only by an explicit active ``repo_uid``, exact card name or
alias, or a unique non-generic homepage host. Fuzzy matching is never applied.
Dry run is the default; ``--apply`` writes card blocks transactionally.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pandas as pd
import yaml

from common import (
    PROJECT,
    all_cards,
    card_transaction,
    gate,
    ledger_append,
    load_card_for_update,
    new_run_dir,
    save_card,
    sha256_file,
)
from publisher_context_schema import MENTION_TYPES, normalize_publisher_context, publisher_context_summary

DEFAULT_MENTIONS = str(Path(PROJECT) / "reference" / "publisher_repository_mentions.csv")
DEFAULT_SOURCES = str(Path(PROJECT) / "reference" / "publisher_policy_sources.csv")

REQUIRED_MENTION_COLUMNS = {
    "publisher_family",
    "journal_title",
    "policy_title",
    "policy_url",
    "policy_level",
    "repository_name",
    "repository_url",
    "mention_type",
    "data_domain",
    "access_model",
    "source_location",
    "evidence_note",
    "date_checked",
    "repo_uid",
    "match_terms",
}
REQUIRED_SOURCE_COLUMNS = {
    "publisher_family",
    "policy_title",
    "policy_url",
    "date_checked",
    "content_sha256",
    "snapshot_path",
    "refresh_status",
}
GENERIC_HOSTS = {
    "nih.gov",
    "ncbi.nlm.nih.gov",
    "nlm.nih.gov",
    "nature.com",
    "plos.org",
    "wiley.com",
    "springernature.com",
    "elsevier.com",
    "tandfonline.com",
    "ebi.ac.uk",
    "embl.org",
    "umich.edu",
    "harvard.edu",
}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _norm(value: Any) -> str:
    text = _clean(value).casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _host(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"https://{text}")
    return (parsed.hostname or "").casefold().removeprefix("www.")


def _generic_host(host: str) -> bool:
    return not host or host in GENERIC_HOSTS or any(host.endswith(f".{item}") for item in GENERIC_HOSTS)


def _url_identity(value: Any) -> tuple[str, str]:
    text = _clean(value)
    if not text:
        return "", ""
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    path = re.sub(r"/+", "/", unquote(parsed.path or "/")).rstrip("/") or "/"
    return host, path.casefold()


def _active_uid_remap(cards: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Resolve retired UIDs through the project remap ledger to active cards."""
    path = Path(PROJECT) / "id_remap.csv"
    if not path.is_file() or not path.stat().st_size:
        return {}
    frame = pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")
    if not {"old_uid", "new_uid"}.issubset(frame.columns):
        raise ValueError("id_remap.csv lacks old_uid/new_uid columns")
    direct: dict[str, str] = {}
    for row in frame.to_dict("records"):
        old_uid = str(row.get("old_uid") or "").strip()
        new_uid = str(row.get("new_uid") or "").strip()
        if not old_uid or not new_uid:
            continue
        if old_uid in direct and direct[old_uid] != new_uid:
            raise ValueError(f"Conflicting id_remap targets for {old_uid}: {direct[old_uid]}, {new_uid}")
        direct[old_uid] = new_uid
    resolved: dict[str, str] = {}
    for old_uid in direct:
        current = old_uid
        seen: set[str] = set()
        while current in direct:
            if current in seen:
                raise ValueError(f"Cycle in id_remap.csv involving {current}")
            seen.add(current)
            current = direct[current]
        if current in cards:
            resolved[old_uid] = current
    return resolved


def _card_names(card: dict[str, Any]) -> list[str]:
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    values = [identity.get("name"), identity.get("full_name"), identity.get("abbreviation")]
    aliases = identity.get("aliases") if isinstance(identity.get("aliases"), list) else []
    values.extend(aliases)
    return list(dict.fromkeys(_clean(value) for value in values if _clean(value)))


def _card_homepage(card: dict[str, Any]) -> str:
    value = (card.get("identity") or {}).get("homepage")
    if isinstance(value, list):
        value = value[0] if value else ""
    return _clean(value)


def _read(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")
    frame.columns = [str(column).strip() for column in frame.columns]
    return frame


def _source_index(sources: pd.DataFrame) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    for row in sources.to_dict("records"):
        url = _clean(row.get("policy_url"))
        if not url:
            continue
        if url in index:
            raise ValueError(f"Duplicate publisher policy source URL: {url}")
        index[url] = {key: _clean(value) for key, value in row.items()}
    return index


def _match_mentions(
    cards: dict[str, dict[str, Any]],
    mentions: pd.DataFrame,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    normalized_names: dict[str, set[str]] = {}
    host_to_uids: dict[str, set[str]] = {}
    url_to_uids: dict[tuple[str, str], set[str]] = {}
    uid_remap = _active_uid_remap(cards)
    for uid, card in cards.items():
        for name in _card_names(card):
            normalized_names.setdefault(_norm(name), set()).add(uid)
        homepage = _card_homepage(card)
        host = _host(homepage)
        if host:
            host_to_uids.setdefault(host, set()).add(uid)
        url_identity = _url_identity(homepage)
        if all(url_identity):
            url_to_uids.setdefault(url_identity, set()).add(uid)

    by_uid: dict[str, list[dict[str, Any]]] = {}
    report: list[dict[str, Any]] = []
    for row_number, row in mentions.iterrows():
        configured_uid = _clean(row.get("repo_uid"))
        names = [_clean(row.get("repository_name"))]
        names.extend(
            _clean(value)
            for value in re.split(r"\s*[;|]\s*", _clean(row.get("match_terms")))
            if _clean(value)
        )
        candidate_uids: set[str] = set()
        basis = ""
        resolved_configured_uid = ""
        stale_reference = False
        if configured_uid:
            if configured_uid in cards:
                candidate_uids = {configured_uid}
                resolved_configured_uid = configured_uid
                basis = "reference_uid"
            elif configured_uid in uid_remap:
                resolved_configured_uid = uid_remap[configured_uid]
                candidate_uids = {resolved_configured_uid}
                basis = "reference_uid_remapped"
            else:
                basis = "stale_reference_uid"
                stale_reference = True
        if not candidate_uids and not stale_reference:
            for name in names:
                candidate_uids.update(normalized_names.get(_norm(name), set()))
            if candidate_uids:
                basis = "exact_card_name_set"
        if not candidate_uids and not stale_reference:
            url_identity = _url_identity(row.get("repository_url"))
            exact_url_candidates = url_to_uids.get(url_identity, set()) if all(url_identity) else set()
            if exact_url_candidates:
                candidate_uids = set(exact_url_candidates)
                basis = "exact_homepage_url"
            else:
                host, path = url_identity
                # Host-only identity is allowed only for a root URL on a
                # non-generic host. Institutional hosts with repository-specific
                # paths (for example EBI) must match by name or exact URL.
                host_candidates = (
                    host_to_uids.get(host, set())
                    if path == "/" and not _generic_host(host)
                    else set()
                )
                if len(host_candidates) == 1:
                    candidate_uids = set(host_candidates)
                    basis = "unique_root_homepage_host"
                elif len(host_candidates) > 1:
                    candidate_uids = set(host_candidates)
                    basis = "shared_root_homepage_host"

        matched_uid = next(iter(candidate_uids)) if len(candidate_uids) == 1 else ""
        status = (
            "STALE_REFERENCE" if stale_reference
            else "MATCHED" if matched_uid
            else "AMBIGUOUS" if candidate_uids
            else "UNMATCHED"
        )
        record = {
            "publisher_family": _clean(row.get("publisher_family")),
            "journal_title": _clean(row.get("journal_title")),
            "policy_title": _clean(row.get("policy_title")),
            "policy_url": _clean(row.get("policy_url")),
            "policy_level": _clean(row.get("policy_level")),
            "mention_type": _clean(row.get("mention_type")),
            "data_domain": _clean(row.get("data_domain")),
            "access_model": _clean(row.get("access_model")),
            "repository_name_as_listed": _clean(row.get("repository_name")),
            "source_location": _clean(row.get("source_location")),
            "evidence_note": _clean(row.get("evidence_note")),
            "date_checked": _clean(row.get("date_checked")),
        }
        if matched_uid:
            by_uid.setdefault(matched_uid, []).append(record)
        report.append(
            {
                "source_row": int(row_number) + 2,
                "publisher_family": record["publisher_family"],
                "journal_title": record["journal_title"],
                "repository_name": record["repository_name_as_listed"],
                "mention_type": record["mention_type"],
                "configured_repo_uid": configured_uid,
                "resolved_configured_repo_uid": resolved_configured_uid,
                "matched_repo_uid": matched_uid,
                "status": status,
                "match_basis": basis,
                "candidate_repo_uids": "; ".join(sorted(candidate_uids)),
                "policy_url": record["policy_url"],
            }
        )
    return by_uid, report


def run(*, mentions_path: Path, sources_path: Path, apply: bool) -> tuple[bool, dict[str, Any], Path]:
    run_dir = Path(new_run_dir("build_publisher_context"))
    cards = all_cards()
    mentions = _read(mentions_path)
    sources = _read(sources_path)
    missing_mentions = REQUIRED_MENTION_COLUMNS - set(mentions.columns)
    missing_sources = REQUIRED_SOURCE_COLUMNS - set(sources.columns)
    if missing_mentions:
        raise ValueError(f"Publisher mention inventory lacks columns: {sorted(missing_mentions)}")
    if missing_sources:
        raise ValueError(f"Publisher source inventory lacks columns: {sorted(missing_sources)}")
    invalid_types = sorted(set(mentions["mention_type"].astype(str)) - MENTION_TYPES)
    if invalid_types:
        raise ValueError(f"Invalid publisher mention types: {invalid_types}")
    source_index = _source_index(sources)
    unknown_source_urls = sorted(
        {url for url in mentions["policy_url"].astype(str).map(_clean) if url and url not in source_index}
    )
    mentions_sha = sha256_file(mentions_path) or ""
    sources_sha = sha256_file(sources_path) or ""
    mentions_by_uid, match_report = _match_mentions(cards, mentions)

    blocks: dict[str, dict[str, Any]] = {}
    proposals: list[dict[str, Any]] = []
    changed: list[str] = []
    for uid, card in cards.items():
        card_mentions: list[dict[str, Any]] = []
        for item in mentions_by_uid.get(uid, []):
            source = source_index.get(item["policy_url"], {})
            card_mentions.append(
                {
                    **item,
                    "source_sha256": _clean(source.get("content_sha256")),
                    "source_snapshot": _clean(source.get("snapshot_path")),
                    "date_checked": _clean(source.get("date_checked")) or item["date_checked"],
                }
            )
        as_of = max(
            [item.get("date_checked", "") for item in card_mentions if item.get("date_checked")]
            or [dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")]
        )
        block = normalize_publisher_context(
            {
                "_meta": {
                    "block_version": "1.0",
                    "as_of": as_of,
                    "tier": "policy",
                    "review_state": "auto",
                    "sources": [
                        f"publisher_mentions:{mentions_sha}",
                        f"publisher_sources:{sources_sha}",
                    ],
                },
                "mentions": card_mentions,
            }
        )
        blocks[uid] = block
        current = normalize_publisher_context((card.get("blocks") or {}).get("publisher_context"))
        is_changed = current != block
        if is_changed:
            changed.append(uid)
        proposals.append(
            {
                "repo_uid": uid,
                "name": _clean((card.get("identity") or {}).get("name")),
                **publisher_context_summary(block),
                "mention_count": len(block["mentions"]),
                "changed": str(is_changed).lower(),
            }
        )

    report_frame = pd.DataFrame(match_report)
    proposal_frame = pd.DataFrame(proposals)
    report_frame.to_csv(run_dir / "publisher_repository_match_report.csv", index=False)
    proposal_frame.to_csv(run_dir / "publisher_context_proposal.csv", index=False)
    (run_dir / "publisher_context_blocks.yaml").write_text(
        yaml.safe_dump(blocks, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    matched_rows = int((report_frame.get("status", pd.Series(dtype=str)) == "MATCHED").sum())
    ambiguous_rows = int((report_frame.get("status", pd.Series(dtype=str)) == "AMBIGUOUS").sum())
    stale_reference_rows = int((report_frame.get("status", pd.Series(dtype=str)) == "STALE_REFERENCE").sum())
    checks = {
        "cards loaded": (len(cards) > 0, f"{len(cards)} cards"),
        "publisher mentions loaded": (len(mentions) > 0, f"{len(mentions)} rows"),
        "publisher sources loaded": (len(sources) > 0, f"{len(sources)} pages"),
        "mention types valid": (not invalid_types, ", ".join(invalid_types) or "valid"),
        "policy URLs source-bound": (
            not unknown_source_urls,
            ", ".join(unknown_source_urls[:5]) or "all present",
        ),
        "no ambiguous auto-assignments": (ambiguous_rows == 0, f"{ambiguous_rows} ambiguous"),
        "configured publisher UIDs active/remapped": (stale_reference_rows == 0, f"{stale_reference_rows} stale configured UID(s)"),
        "publisher blocks normalized": (len(blocks) == len(cards), f"{len(blocks)} blocks"),
    }
    ok = gate(checks)

    applied = 0
    if apply and ok and changed:
        with card_transaction("build_publisher_context"):
            locked = {uid: load_card_for_update(uid, cards[uid]) for uid in changed}
            for uid in changed:
                next_card = copy.deepcopy(locked[uid])
                next_card.setdefault("blocks", {})["publisher_context"] = blocks[uid]
                save_card(uid, next_card)
        applied = len(changed)
        print(f"\n  Applied {applied} publisher-context update(s) transactionally.")
    elif apply and not ok:
        print("\n  Gate failed; no card was changed.")
    elif apply:
        print("\n  Apply requested; valid no-op because publisher context is current.")
    else:
        print("\n  DRY RUN - inspect publisher match and proposal reports.")

    coverage = {
        "cards": len(cards),
        "mention_rows": len(mentions),
        "matched_mention_rows": matched_rows,
        "unmatched_mention_rows": int((report_frame.get("status", pd.Series(dtype=str)) == "UNMATCHED").sum()),
        "ambiguous_mention_rows": ambiguous_rows,
        "cards_mentioned": sum(1 for block in blocks.values() if block["publisher_mentioned"]),
        "cards_recommended": sum(1 for block in blocks.values() if block["publisher_recommended"]),
        "cards_mandated": sum(1 for block in blocks.values() if block["publisher_mandated"]),
        "cards_acceptable_fallback": sum(
            1 for block in blocks.values() if block["publisher_acceptable_fallback"]
        ),
        "changed": len(changed),
        "applied": applied,
    }
    pd.Series(coverage).to_json(run_dir / "coverage.json", indent=2)
    ledger_append(
        "build_publisher_context",
        params={"mentions": mentions_path.name, "sources": sources_path.name, "apply": apply},
        inputs={"publisher_mentions": mentions_sha, "publisher_sources": sources_sha},
        outputs=coverage,
        gate_passed=ok,
        run_dir=str(run_dir),
    )
    print("\n  PUBLISHER CONTEXT COVERAGE")
    for key, value in coverage.items():
        print(f"  {key:34s} {value}")
    print(f"\nPublisher-context run: {run_dir}")
    return ok, coverage, run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mentions", default=DEFAULT_MENTIONS)
    parser.add_argument("--sources", default=DEFAULT_SOURCES)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    ok, _, _ = run(
        mentions_path=Path(args.mentions).expanduser(),
        sources_path=Path(args.sources).expanduser(),
        apply=args.apply,
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
