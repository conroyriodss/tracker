"""
step_build_links.py
Resolve external-registry identifiers from final_ultimate_enhanced_repos.csv and
write them into each card's `links` (tier=registry). Join order:
  1) crosswalk.csv (confirmed name -> repo_uid), translated through id_remap.csv
  2) exact match across each active card's complete identity name set
  3) acronym-stripped fuzzy match for remaining gaps, with review output

Also derives a `pid_culture` block (DOI vs accession - the ECO two-cultures split).

Run:  python step_build_links.py --registry final_ultimate_enhanced_repos.csv
Params:
  --registry FILE   harvested registry CSV (default: final_ultimate_enhanced_repos.csv)
  --apply           write links into cards (default: DRY RUN -> proposal only)
  --min-conf 0.94   fuzzy cutoff for gap matching

Honest-blank rule: FAIRsharing DOI is written when present; internal bsg-/biodbcore-
id remains absent when not independently confirmable. A wrong id is worse than a blank.

Safety rule: a crosswalk target that is no longer an active top-level card must either
resolve through id_remap.csv, have a current reviewed DEFER receipt, or fail the sanity
gate. A reviewed deferral is excluded from the join and retained as an advisory issue;
it never authorizes a guessed card match. Dry-run and apply therefore operate on the same
validated active UID set.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from common import (
    ID_REMAP_CSV,
    INP,
    NOW,
    all_cards,
    card_transaction,
    card_name_set,
    gate,
    index_cards_by_name,
    ledger_append,
    load_card_for_update,
    log_issue,
    new_run_dir,
    resolve_issues_not_in,
    save_card,
    sha256_file,
    slug,
    strip_acronym,
)

YM = NOW.strftime("%Y-%m")

# registry column -> (id_type, url template). {} is filled with the id value.
LINK_SPECS = {
    "FS_DOI": ("fairsharing_doi", "https://fairsharing.org/{}"),
    "RE3_Name": ("re3data", None),
    "SC_Identifier": ("rrid", "https://scicrunch.org/resolver/{}"),
}

CROSSWALK_PREFLIGHT_COLUMNS = [
    "crosswalk_row",
    "csv_id",
    "probe_name",
    "registry_name",
    "crosswalk_uid",
    "resolved_uid",
    "status",
    "scope_reason",
    "active_card_name",
    "candidate_uids",
    "candidate_names",
    "candidate_scores",
    "source_row_fingerprint",
    "crosswalk_sha256",
    "registry_file",
    "registry_sha256",
    "id_remap_sha256",
    "adjudication_review_key",
    "adjudication_decision",
    "adjudication_apply_status",
    "adjudication_reviewer",
    "adjudication_reviewed_at",
    "adjudication_note",
    "adjudication_source_crosswalk_sha256",
    "adjudication_rebased",
    "detail",
]

REVIEW_COLUMNS = [
    "registry_name",
    "matched_card",
    "how",
    "crosswalk_uid",
    "resolved_uid",
    "detail",
]

CROSSWALK_ADJUDICATIONS_CSV = Path(INP) / "crosswalk_adjudications.csv"
DEFERRED_RECEIPT_PREFIX = "deferred:"
DEFERRED_ISSUE_TYPE = "crosswalk_target_deferred"


def _clean(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return (
        None
        if text.lower() in {"", "undefined", "null", "n/a", "na", "nan", "none", "-"}
        else text
    )


def _re3(row: Mapping[str, Any]):
    """Return (id_or_None, url_or_None, present_bool).

    RE3_* columns often hold re3data's repository NAME, not the r3dNNN id, and some
    rows prove a re3data record only through enrichment fields.
    """
    from urllib.parse import quote

    for column in ("RE3_URL", "RE3_Name", "RE3_Additional_Name"):
        value = _clean(row.get(column))
        if value:
            match = re.search(r"(r3d\d{9})", value)
            if match:
                identifier = match.group(1)
                return identifier, f"https://www.re3data.org/repository/{identifier}", True
    url = _clean(row.get("RE3_URL"))
    if url and "re3data.org" in url:
        return None, url, True
    name = _clean(row.get("RE3_Name"))
    if name:
        return None, f"https://www.re3data.org/search?query={quote(name)}", True
    if any(
        _clean(row.get(column))
        for column in (
            "re3data_subjects",
            "re3data_content_types",
            "re3data_keywords",
            "RE3_Institution",
        )
    ):
        repository_name = _clean(row.get("Repository_Name")) or ""
        return None, f"https://www.re3data.org/search?query={quote(repository_name)}", True
    return None, None, False


def _fairsharing_doi(row: Mapping[str, Any]) -> str | None:
    value = _clean(row.get("FS_DOI"))
    if not value:
        return None
    match = re.search(r"(10\.25504/FAIRsharing\.\w+)", value)
    return match.group(1) if match else (value if value.startswith("10.25504") else None)


def _rrid(row: Mapping[str, Any]) -> str | None:
    value = _clean(row.get("SC_Identifier"))
    if not value:
        return None
    match = re.search(r"(SCR_\d+)", value)
    return f"RRID:{match.group(1)}" if match else None


def _pid_culture(row: Mapping[str, Any]) -> str | None:
    """Return the DOI/accession bucket from final_ultimate PID columns."""
    has_doi = _clean(row.get("repository_has_doi"))
    pid_types = (_clean(row.get("persistent_id_types")) or "").lower()
    dataset_doi_count = pd.to_numeric(row.get("doi_count_datasets"), errors="coerce")
    doi = (
        (has_doi and has_doi.lower() in {"true", "yes"})
        or "doi" in pid_types
        or (pd.notna(dataset_doi_count) and dataset_doi_count > 0)
    )
    accession = "accession" in pid_types or "internal" in pid_types
    if doi and accession:
        return "mixed"
    if doi:
        return "doi"
    if accession:
        return "accession"
    return None


def load_uid_remap(path: os.PathLike[str] | str = ID_REMAP_CSV) -> dict[str, str]:
    """Load and transitively resolve the persistent old-UID -> current-UID map.

    Conflicting targets and cycles are integrity failures; they are never resolved by
    row order. Missing/empty files are valid and produce an empty map.
    """
    remap_path = Path(path)
    if not remap_path.is_file() or remap_path.stat().st_size == 0:
        return {}
    try:
        frame = pd.read_csv(remap_path, dtype=str).fillna("")
    except pd.errors.EmptyDataError:
        return {}
    required = {"old_uid", "new_uid"}
    if not required.issubset(frame.columns):
        raise ValueError(
            f"{remap_path} must contain columns {sorted(required)}; found {list(frame.columns)}"
        )

    direct: dict[str, str] = {}
    for row in frame.itertuples(index=False):
        old_uid = _clean(getattr(row, "old_uid", ""))
        new_uid = _clean(getattr(row, "new_uid", ""))
        if not old_uid or not new_uid:
            continue
        prior = direct.get(old_uid)
        if prior is not None and prior != new_uid:
            raise ValueError(
                f"Conflicting id_remap targets for {old_uid}: {prior!r} and {new_uid!r}"
            )
        direct[old_uid] = new_uid

    resolved: dict[str, str] = {}
    for old_uid in sorted(direct):
        current = old_uid
        seen: list[str] = []
        while current in direct:
            if current in seen:
                cycle = " -> ".join([*seen, current])
                raise ValueError(f"Cycle in id_remap.csv: {cycle}")
            seen.append(current)
            current = direct[current]
        resolved[old_uid] = current
    return resolved


def crosswalk_row_fingerprint(
    *,
    csv_id: Any = "",
    probe_name: Any = "",
    registry_name: Any = "",
    repo_uid: Any = "",
) -> str:
    """Return a stable fingerprint for one logical crosswalk source row."""
    payload = "|".join(
        str(_clean(value) or "").strip()
        for value in (csv_id, probe_name, registry_name, repo_uid)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _optional_csv(path: os.PathLike[str] | str) -> pd.DataFrame:
    """Read an optional CSV without treating an absent/empty file as an error."""
    source = Path(path)
    if not source.is_file() or source.stat().st_size == 0:
        return pd.DataFrame()
    try:
        frame = pd.read_csv(source, dtype=str).fillna("")
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    frame.columns = [str(column).strip() for column in frame.columns]
    return frame


def load_reviewed_crosswalk_deferrals(
    *,
    adjudications_path: os.PathLike[str] | str = CROSSWALK_ADJUDICATIONS_CSV,
    registry_file: str,
    registry_sha256: str,
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Return current, receipted DEFER decisions keyed by source-row fingerprint.

    Deferrals are intentionally row-bound rather than whole-file-bound. An unrelated
    crosswalk correction may change ``crosswalk.csv``'s SHA-256 without changing the
    reviewed row. A deferral therefore remains valid when the exact row fingerprint and
    registry snapshot still match. Duplicate/conflicting records are rejected rather
    than resolved by row order.

    Returns ``(eligible, rejected_reason_by_fingerprint)``. Rejected records do not
    change crosswalk status; the underlying row remains blocking.
    """
    frame = _optional_csv(adjudications_path)
    if frame.empty:
        return {}, {}
    required = {
        "review_key",
        "source_row_fingerprint",
        "source_registry_file",
        "source_registry_sha256",
        "decision",
        "apply_status",
    }
    if not required.issubset(frame.columns):
        return {}, {
            "__schema__": (
                "crosswalk_adjudications.csv lacks required reviewed-deferral fields: "
                f"{sorted(required.difference(frame.columns))}"
            )
        }

    expected_registry_file = Path(str(registry_file or "")).name
    rows_by_fingerprint: dict[str, list[dict[str, str]]] = {}
    for raw in frame.to_dict("records"):
        row = {str(key): str(value or "").strip() for key, value in raw.items()}
        fingerprint = row.get("source_row_fingerprint", "")
        if not fingerprint:
            continue
        if row.get("decision", "").upper() != "DEFER":
            continue
        if not row.get("apply_status", "").lower().startswith(DEFERRED_RECEIPT_PREFIX):
            continue
        rows_by_fingerprint.setdefault(fingerprint, []).append(row)

    eligible: dict[str, dict[str, str]] = {}
    rejected: dict[str, str] = {}
    for fingerprint, rows in rows_by_fingerprint.items():
        if len(rows) != 1:
            rejected[fingerprint] = (
                f"{len(rows)} receipted DEFER records share this source-row fingerprint"
            )
            continue
        row = rows[0]
        source_registry_file = Path(row.get("source_registry_file", "")).name
        source_registry_hash = row.get("source_registry_sha256", "")
        if not expected_registry_file or source_registry_file != expected_registry_file:
            rejected[fingerprint] = (
                "reviewed deferral belongs to a different registry file "
                f"({source_registry_file or '[missing]'})"
            )
            continue
        if not registry_sha256 or source_registry_hash != registry_sha256:
            rejected[fingerprint] = (
                "reviewed deferral belongs to a different registry snapshot"
            )
            continue
        eligible[fingerprint] = row
    return eligible, rejected


def _candidate_cards(
    registry_name: str,
    cards: Mapping[str, Any],
    *,
    limit: int = 5,
) -> list[tuple[str, str, float]]:
    """Return nearest active-card name candidates for diagnostics only.

    Candidate scores are never used to repair a crosswalk automatically. They are
    included only to make a stale-target review actionable.
    """
    target = slug(strip_acronym(registry_name)[0])
    if not target:
        return []
    ranked: list[tuple[str, str, float]] = []
    for uid, card in cards.items():
        identity = (card or {}).get("identity", {}) or {}
        canonical = str(identity.get("name") or "")
        names, acronyms = card_name_set(card)
        candidates = names | acronyms
        score = max(
            (difflib.SequenceMatcher(None, target, candidate).ratio() for candidate in candidates),
            default=0.0,
        )
        ranked.append((str(uid), canonical, score))
    ranked.sort(key=lambda row: (-row[2], row[0]))
    return ranked[:limit]


def load_crosswalk_targets(
    cards: Mapping[str, Any],
    *,
    uid_remap: Mapping[str, str] | None = None,
    crosswalk_path: os.PathLike[str] | str | None = None,
    adjudications_path: os.PathLike[str] | str | None = None,
    id_remap_path: os.PathLike[str] | str = ID_REMAP_CSV,
    registry_names: Iterable[Any] | None = None,
    registry_file: str = "",
    registry_sha256: str = "",
) -> tuple[dict[str, dict[str, str]], pd.DataFrame]:
    """Load crosswalk rows and classify build-links-relevant targets.

    ``crosswalk.csv`` is a sparse bridge across multiple source lists. Rows without
    ``registry_name`` or whose name is absent from the selected registry snapshot
    are reported as ``NOT_APPLICABLE`` rather than malformed. Only rows that can
    participate in the current registry join can block this preflight.
    """
    path = Path(crosswalk_path or (Path(INP) / "crosswalk.csv"))
    if not path.is_file():
        raise FileNotFoundError(path)
    frame = pd.read_csv(path, dtype=str).fillna("")
    frame.columns = [column.strip() for column in frame.columns]
    required = {"registry_name", "repo_uid"}
    if not required.issubset(frame.columns):
        raise ValueError(
            f"{path} must contain columns {sorted(required)}; found {list(frame.columns)}"
        )

    crosswalk_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    id_remap_sha256 = sha256_file(id_remap_path) or ""
    deferrals: dict[str, dict[str, str]] = {}
    rejected_deferrals: dict[str, str] = {}
    if registry_file and registry_sha256:
        deferrals, rejected_deferrals = load_reviewed_crosswalk_deferrals(
            adjudications_path=adjudications_path or CROSSWALK_ADJUDICATIONS_CSV,
            registry_file=registry_file,
            registry_sha256=registry_sha256,
        )
    remap = dict(uid_remap or {})
    active_uids = set(cards)
    registry_keys = None
    if registry_names is not None:
        registry_keys = {
            slug(strip_acronym(str(name))[0])
            for name in registry_names
            if _clean(name)
        }
    source_fingerprints = [
        crosswalk_row_fingerprint(
            csv_id=row.get("csv_id", ""),
            probe_name=row.get("probe_name", ""),
            registry_name=row.get("registry_name", ""),
            repo_uid=row.get("repo_uid", ""),
        )
        for row in frame.to_dict("records")
    ]
    fingerprint_counts: dict[str, int] = {}
    for fingerprint in source_fingerprints:
        fingerprint_counts[fingerprint] = fingerprint_counts.get(fingerprint, 0) + 1

    grouped_targets: dict[str, set[str]] = {}
    for _, row in frame.iterrows():
        name = _clean(row.get("registry_name"))
        uid = _clean(row.get("repo_uid"))
        key = slug(strip_acronym(name)[0]) if name else ""
        if name and uid and (registry_keys is None or key in registry_keys):
            grouped_targets.setdefault(key, set()).add(remap.get(uid, uid))
    conflict_keys = {key for key, values in grouped_targets.items() if len(values) > 1}

    mapping: dict[str, dict[str, str]] = {}
    rows: list[dict[str, Any]] = []
    for source_index, row in frame.iterrows():
        csv_id = _clean(row.get("csv_id")) or ""
        probe_name = _clean(row.get("probe_name")) or ""
        name = _clean(row.get("registry_name")) or ""
        uid = _clean(row.get("repo_uid")) or ""
        key = slug(strip_acronym(name)[0]) if name else ""
        resolved_uid = remap.get(uid, uid) if uid else ""
        fingerprint = crosswalk_row_fingerprint(
            csv_id=csv_id,
            probe_name=probe_name,
            registry_name=name,
            repo_uid=uid,
        )
        status = ""
        scope_reason = ""
        detail = ""
        active_name = ""
        adjudication = deferrals.get(fingerprint, {})
        adjudication_rebased = ""

        if not name:
            status = "NOT_APPLICABLE"
            scope_reason = "missing_registry_name"
            detail = (
                "registry_name is blank, so this sparse crosswalk row is not used by "
                "step_build_links.py. It remains available to workflows that use probe_name."
            )
        elif registry_keys is not None and key not in registry_keys:
            status = "NOT_APPLICABLE"
            scope_reason = "not_in_current_registry"
            detail = (
                "registry_name is not present in the selected registry snapshot, so this "
                "historical crosswalk row cannot participate in the current build_links join."
            )
        elif fingerprint_counts.get(fingerprint, 0) != 1:
            scope_reason = "current_registry"
            status = "CONFLICT"
            detail = (
                "The logical crosswalk source row is duplicated, so its review "
                "fingerprint is not unique. Remove or reconcile the duplicate rows."
            )
        elif not uid:
            scope_reason = "current_registry"
            status = "INVALID_ROW"
            detail = "repo_uid is required when registry_name is populated."
        elif key in conflict_keys:
            scope_reason = "current_registry"
            status = "CONFLICT"
            detail = "The same normalized registry name maps to multiple UIDs."
        elif resolved_uid in active_uids:
            scope_reason = "current_registry"
            status = "REMAPPED" if resolved_uid != uid else "ACTIVE"
            active_name = str(
                ((cards.get(resolved_uid) or {}).get("identity", {}) or {}).get("name") or ""
            )
            detail = (
                "Retired UID translated through id_remap.csv."
                if status == "REMAPPED"
                else "Crosswalk target is an active top-level card."
            )
            mapping[key] = {
                "crosswalk_uid": uid,
                "resolved_uid": resolved_uid,
                "status": status,
            }
        else:
            scope_reason = "current_registry"
            status = "STALE"
            detail = (
                "Crosswalk target is not an active top-level card and has no valid "
                "terminal mapping in id_remap.csv."
            )
            if adjudication:
                status = "DEFERRED"
                scope_reason = "current_registry_reviewed_defer"
                prior_crosswalk_hash = adjudication.get(
                    "source_crosswalk_sha256", ""
                )
                adjudication_rebased = (
                    "true"
                    if prior_crosswalk_hash
                    and prior_crosswalk_hash != crosswalk_sha256
                    else "false"
                )
                reviewer = adjudication.get("reviewer", "") or "[unspecified reviewer]"
                reviewed_at = adjudication.get("reviewed_at", "") or "[unspecified time]"
                detail = (
                    "Crosswalk target remains inactive and unmapped, but a receipted "
                    f"DEFER decision by {reviewer} at {reviewed_at} excludes this row "
                    "from the current registry join without asserting an identity match."
                )
            elif fingerprint in rejected_deferrals:
                detail += " A prior DEFER receipt was not accepted: " + rejected_deferrals[
                    fingerprint
                ]

        candidates = (
            _candidate_cards(name, cards)
            if status in {"STALE", "DEFERRED", "CONFLICT", "INVALID_ROW"}
            else []
        )
        rows.append(
            {
                "crosswalk_row": str(int(source_index) + 2),
                "csv_id": csv_id,
                "probe_name": probe_name,
                "registry_name": name,
                "crosswalk_uid": uid,
                "resolved_uid": resolved_uid if resolved_uid != uid else "",
                "status": status,
                "scope_reason": scope_reason,
                "active_card_name": active_name,
                "candidate_uids": "; ".join(candidate[0] for candidate in candidates),
                "candidate_names": "; ".join(candidate[1] for candidate in candidates),
                "candidate_scores": "; ".join(f"{candidate[2]:.4f}" for candidate in candidates),
                "source_row_fingerprint": fingerprint,
                "crosswalk_sha256": crosswalk_sha256,
                "registry_file": registry_file,
                "registry_sha256": registry_sha256,
                "id_remap_sha256": id_remap_sha256,
                "adjudication_review_key": adjudication.get("review_key", ""),
                "adjudication_decision": adjudication.get("decision", ""),
                "adjudication_apply_status": adjudication.get("apply_status", ""),
                "adjudication_reviewer": adjudication.get("reviewer", ""),
                "adjudication_reviewed_at": adjudication.get("reviewed_at", ""),
                "adjudication_note": adjudication.get("note", ""),
                "adjudication_source_crosswalk_sha256": adjudication.get(
                    "source_crosswalk_sha256", ""
                ),
                "adjudication_rebased": adjudication_rebased,
                "detail": detail,
            }
        )
    report = pd.DataFrame(rows, columns=CROSSWALK_PREFLIGHT_COLUMNS)
    return mapping, report


def run_crosswalk_preflight(
    cards: Mapping[str, Any],
    uid_remap: Mapping[str, str],
    *,
    registry_path: os.PathLike[str] | str | None = None,
) -> bool:
    """Write and ledger a fail-closed, scope-aware crosswalk integrity report."""
    selected_registry_path = Path(registry_path) if registry_path else None
    registry_names = None
    registry_rows = None
    registry_hash = ""
    registry_file = ""
    if selected_registry_path is not None:
        if not selected_registry_path.is_file():
            raise FileNotFoundError(selected_registry_path)
        registry = pd.read_csv(selected_registry_path, dtype=str).fillna("")
        registry.columns = [column.strip() for column in registry.columns]
        if "Repository_Name" not in registry.columns:
            raise ValueError(
                f"{selected_registry_path} must contain Repository_Name; "
                f"found {list(registry.columns)}"
            )
        registry_names = registry["Repository_Name"].tolist()
        registry_rows = len(registry)
        registry_hash = hashlib.sha256(selected_registry_path.read_bytes()).hexdigest()
        registry_file = selected_registry_path.name
    mapping, report = load_crosswalk_targets(
        cards,
        uid_remap=uid_remap,
        registry_names=registry_names,
        registry_file=registry_file,
        registry_sha256=registry_hash,
    )
    counts = report["status"].value_counts().to_dict() if len(report) else {}
    stale = int(counts.get("STALE", 0))
    deferred = int(counts.get("DEFERRED", 0))
    conflicts = int(counts.get("CONFLICT", 0))
    invalid = int(counts.get("INVALID_ROW", 0))
    not_applicable = int(counts.get("NOT_APPLICABLE", 0))
    in_scope = len(report) - not_applicable
    missing_registry_name = int(
        (
            (report["status"] == "NOT_APPLICABLE")
            & (report["scope_reason"] == "missing_registry_name")
        ).sum()
    ) if len(report) else 0
    absent_from_registry = int(
        (
            (report["status"] == "NOT_APPLICABLE")
            & (report["scope_reason"] == "not_in_current_registry")
        ).sum()
    ) if len(report) else 0
    run_dir = new_run_dir("build_links_preflight")
    report.to_csv(os.path.join(run_dir, "crosswalk_integrity.csv"), index=False)
    checks = {
        "active cards loaded": (len(cards) > 0, f"{len(cards)} top-level cards"),
        "registry snapshot loaded": (
            registry_rows is None or registry_rows > 0,
            "not supplied" if registry_rows is None else f"{registry_rows} rows",
        ),
        "crosswalk rows loaded": (len(report) > 0, f"{len(report)} rows"),
        "build_links-scoped rows": (True, f"{in_scope} registry-linked rows"),
        "out-of-scope rows ignored": (
            True,
            f"{not_applicable} row(s) outside the current registry join; non-blocking",
        ),
        "scoped rows structurally valid": (invalid == 0, f"{invalid} invalid row(s)"),
        "crosswalk names unambiguous": (conflicts == 0, f"{conflicts} conflict(s)"),
        "crosswalk targets active/remapped/deferred": (
            stale == 0,
            (
                f"0 unreviewed stale targets; {deferred} reviewed deferral(s) withheld"
                if stale == 0
                else f"{stale} unreviewed stale target(s); inspect crosswalk_integrity.csv"
            ),
        ),
    }
    ok = gate(checks)
    blocking_statuses = {"STALE", "CONFLICT", "INVALID_ROW"}
    blocking = report.loc[report["status"].isin(blocking_statuses)]
    print(
        "\n  CROSSWALK SCOPE SUMMARY: "
        f"total={len(report)}, in_scope={in_scope}, not_applicable={not_applicable}, "
        f"active={int(counts.get('ACTIVE', 0))}, remapped={int(counts.get('REMAPPED', 0))}, "
        f"deferred={deferred}, blocking={len(blocking)}"
    )
    if len(blocking):
        print("\n  BLOCKING REGISTRY-LINKED CROSSWALK ROWS:")
        columns = [
            "crosswalk_row",
            "csv_id",
            "registry_name",
            "crosswalk_uid",
            "resolved_uid",
            "status",
            "candidate_uids",
            "candidate_names",
            "candidate_scores",
        ]
        print(blocking[columns].to_string(index=False))
        print(
            "  Candidate columns are diagnostic only. Use the review workbench to record "
            "an evidence-based source correction, historical UID remap, or reviewed deferral."
        )
    deferred_rows = report.loc[report["status"] == "DEFERRED"]
    if len(deferred_rows):
        print("\n  REVIEWED DEFERRALS - excluded from this join; non-blocking:")
        columns = [
            "crosswalk_row",
            "csv_id",
            "registry_name",
            "crosswalk_uid",
            "adjudication_reviewer",
            "adjudication_reviewed_at",
            "adjudication_rebased",
        ]
        print(deferred_rows[columns].to_string(index=False))

    active_deferred_issue_uids: set[str] = set()
    for row in deferred_rows.to_dict("records"):
        uid = str(row.get("crosswalk_uid") or "").strip()
        if not uid:
            continue
        active_deferred_issue_uids.add(uid)
        log_issue(
            "build_links_preflight",
            "advisory",
            f"Reviewed crosswalk target remains deferred: {uid}",
            detail=(
                f"registry_name={row.get('registry_name', '')!r}; "
                f"reviewer={row.get('adjudication_reviewer', '')!r}; "
                f"reviewed_at={row.get('adjudication_reviewed_at', '')!r}; "
                "the row is excluded from the current link join and does not authorize "
                "a fallback identity match"
            ),
            repo_uid=uid,
            issue_type=DEFERRED_ISSUE_TYPE,
        )
    resolve_issues_not_in(
        step="build_links_preflight",
        issue_type=DEFERRED_ISSUE_TYPE,
        active_repo_uids=active_deferred_issue_uids,
        note="The crosswalk row is no longer a current reviewed deferral.",
    )
    ledger_append(
        "build_links_preflight",
        params={"check_crosswalk_only": True, "scope": "selected registry-name join"},
        inputs={
            "crosswalk": os.path.join(INP, "crosswalk.csv"),
            **({"registry": selected_registry_path} if selected_registry_path else {}),
            **({"id_remap": ID_REMAP_CSV} if Path(ID_REMAP_CSV).is_file() else {}),
            **(
                {"crosswalk_adjudications": CROSSWALK_ADJUDICATIONS_CSV}
                if CROSSWALK_ADJUDICATIONS_CSV.is_file()
                else {}
            ),
        },
        outputs={
            "rows": len(report),
            "in_scope": in_scope,
            "not_applicable": not_applicable,
            "not_applicable_blank_registry_name": missing_registry_name,
            "not_applicable_not_in_registry_snapshot": absent_from_registry,
            "active": int(counts.get("ACTIVE", 0)),
            "remapped": int(counts.get("REMAPPED", 0)),
            "deferred": deferred,
            "stale": stale,
            "conflicts": conflicts,
            "invalid": invalid,
            "usable_mappings": len(mapping),
        },
        gate_passed=ok,
        run_dir=run_dir,
    )
    print(f"\nCrosswalk preflight report: {run_dir}")
    return ok


def build_join(
    registry: pd.DataFrame,
    cards: Mapping[str, Any],
    *,
    min_conf: float = 0.94,
    uid_remap: Mapping[str, str] | None = None,
    registry_file: str = "",
    registry_sha256: str = "",
    adjudications_path: os.PathLike[str] | str | None = None,
):
    """Return ``{registry_row_index: active_repo_uid}`` and review diagnostics.

    Crosswalk targets are authoritative only while they resolve to an active card. A
    stale target is translated through ``id_remap.csv``. If it still does not exist,
    the row is withheld and marked ``STALE_CROSSWALK_UID`` instead of leaking a missing
    UID into the apply plan.
    """
    if not 0.0 <= float(min_conf) <= 1.0:
        raise ValueError(f"min_conf must be between 0 and 1; got {min_conf!r}")
    uid_remap = dict(uid_remap or {})
    active_uids = set(cards)

    crosswalk_targets, crosswalk_report = load_crosswalk_targets(
        cards,
        uid_remap=uid_remap,
        registry_names=registry["Repository_Name"].tolist(),
        registry_file=registry_file,
        registry_sha256=registry_sha256,
        adjudications_path=adjudications_path,
    )

    name_index, collisions = index_cards_by_name(cards)
    if collisions:
        print(
            f"  WARNING: {len(collisions)} name-set collision(s); first-registered wins. "
            f"Examples: {collisions[:2]}"
        )

    working = registry.copy()
    working["_slug"] = working.Repository_Name.map(
        lambda value: slug(strip_acronym(value)[0])
    )

    uid_of: dict[Any, str] = {}
    review_rows: list[dict[str, Any]] = []
    all_slugs = list(name_index.keys())
    fuzzy_label = f"fuzzy_{float(min_conf):.3f}_nameset"

    for index, row in working.iterrows():
        registry_name = row["Repository_Name"]
        registry_slug = row["_slug"]
        crosswalk_target = crosswalk_targets.get(registry_slug)
        if crosswalk_target:
            crosswalk_uid = crosswalk_target["crosswalk_uid"]
            resolved_uid = crosswalk_target["resolved_uid"]
            uid_of[index] = resolved_uid
            if crosswalk_target["status"] == "REMAPPED":
                review_rows.append(
                    {
                        "registry_name": registry_name,
                        "matched_card": resolved_uid,
                        "how": "crosswalk_uid_remapped",
                        "crosswalk_uid": crosswalk_uid,
                        "resolved_uid": resolved_uid,
                        "detail": "Persistent id_remap.csv translated a retired UID.",
                    }
                )
            continue

        # A crosswalk row may exist but be intentionally absent from the usable mapping
        # because its UID is stale or has a reviewed deferral. Preserve the row-level
        # diagnostic and do not fall through to exact/fuzzy matching: DEFER means
        # "withhold this registry row", not "guess a different card".
        stale_rows = crosswalk_report.loc[
            (crosswalk_report["registry_name"].map(
                lambda value: slug(strip_acronym(value)[0])
            ) == registry_slug)
            & crosswalk_report["status"].isin(
                ["STALE", "DEFERRED", "CONFLICT", "INVALID_ROW"]
            )
        ]
        if len(stale_rows):
            stale_row = stale_rows.iloc[0]
            deferred = stale_row.get("status") == "DEFERRED"
            review_rows.append(
                {
                    "registry_name": registry_name,
                    "matched_card": None,
                    "how": (
                        "DEFERRED_CROSSWALK_UID"
                        if deferred
                        else "STALE_CROSSWALK_UID"
                    ),
                    "crosswalk_uid": stale_row.get("crosswalk_uid") or None,
                    "resolved_uid": stale_row.get("resolved_uid") or None,
                    "detail": (
                        stale_row.get("detail")
                        or (
                            "Reviewed deferral; excluded from the current join."
                            if deferred
                            else "Invalid crosswalk target."
                        )
                    ),
                }
            )
            continue

        if registry_slug in name_index:
            uid_of[index] = name_index[registry_slug]
            continue

        candidates = difflib.get_close_matches(
            registry_slug,
            all_slugs,
            n=1,
            cutoff=float(min_conf),
        )
        if candidates:
            matched_uid = name_index[candidates[0]]
            uid_of[index] = matched_uid
            review_rows.append(
                {
                    "registry_name": registry_name,
                    "matched_card": matched_uid,
                    "how": fuzzy_label,
                    "crosswalk_uid": None,
                    "resolved_uid": matched_uid,
                    "detail": "Fuzzy match against the active card identity name set.",
                }
            )
        else:
            review_rows.append(
                {
                    "registry_name": registry_name,
                    "matched_card": None,
                    "how": "UNRESOLVED",
                    "crosswalk_uid": None,
                    "resolved_uid": None,
                    "detail": "No crosswalk, exact identity-name, or threshold fuzzy match.",
                }
            )

    return uid_of, pd.DataFrame(review_rows, columns=REVIEW_COLUMNS)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default="final_ultimate_enhanced_repos.csv")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--min-conf", type=float, default=0.94)
    parser.add_argument(
        "--check-crosswalk-only",
        action="store_true",
        help="validate crosswalk UID referential integrity and exit before link planning",
    )
    args = parser.parse_args()

    if not 0.0 <= args.min_conf <= 1.0:
        parser.error("--min-conf must be between 0 and 1")

    cards = all_cards()
    uid_remap = load_uid_remap()
    registry_path = os.path.join(INP, args.registry)
    if args.check_crosswalk_only:
        if not run_crosswalk_preflight(
            cards,
            uid_remap,
            registry_path=registry_path,
        ):
            raise SystemExit(2)
        return

    registry = pd.read_csv(registry_path)
    registry.columns = [column.strip() for column in registry.columns]
    registry_sha256 = hashlib.sha256(Path(registry_path).read_bytes()).hexdigest()
    _, crosswalk_integrity = load_crosswalk_targets(
        cards,
        uid_remap=uid_remap,
        registry_names=registry["Repository_Name"].tolist(),
        registry_file=Path(registry_path).name,
        registry_sha256=registry_sha256,
    )
    uid_of, review = build_join(
        registry,
        cards,
        min_conf=args.min_conf,
        uid_remap=uid_remap,
        registry_file=Path(registry_path).name,
        registry_sha256=registry_sha256,
    )

    proposed: dict[str, dict[str, Any]] = {}
    for index, row in registry.iterrows():
        uid = uid_of.get(index)
        if not uid:
            continue
        links: list[dict[str, Any]] = []
        re3_id, re3_url, re3_present = _re3(row)
        if re3_present:
            links.append(
                {
                    "id_type": "re3data",
                    "id_value": re3_id,
                    "url": re3_url,
                    "tier": "registry",
                    "as_of": YM,
                }
            )
        fairsharing = _fairsharing_doi(row)
        if fairsharing:
            links.append(
                {
                    "id_type": "fairsharing_doi",
                    "id_value": fairsharing,
                    "url": f"https://fairsharing.org/{fairsharing}",
                    "tier": "registry",
                    "as_of": YM,
                }
            )
        links.append(
            {
                "id_type": "fairsharing_internal",
                "id_value": None,
                "url": None,
                "tier": None,
            }
        )
        rrid = _rrid(row)
        if rrid:
            links.append(
                {
                    "id_type": "rrid",
                    "id_value": rrid,
                    "url": f"https://scicrunch.org/resolver/{rrid}",
                    "tier": "registry",
                    "as_of": YM,
                }
            )
        proposed[uid] = {"links": links, "pid_culture": _pid_culture(row)}

    proposed_uids = set(proposed)
    missing_proposed_uids = sorted(proposed_uids.difference(cards))
    n_re3 = sum(
        any(link["id_type"] == "re3data" for link in proposal["links"])
        for proposal in proposed.values()
    )
    n_fs = sum(
        any(link["id_type"] == "fairsharing_doi" for link in proposal["links"])
        for proposal in proposed.values()
    )
    n_rrid = sum(
        any(link["id_type"] == "rrid" for link in proposal["links"])
        for proposal in proposed.values()
    )
    n_pid = sum(1 for proposal in proposed.values() if proposal["pid_culture"])
    n_unresolved = int((review["how"] == "UNRESOLVED").sum()) if len(review) else 0
    blocking_crosswalk_statuses = {"STALE", "CONFLICT", "INVALID_ROW"}
    n_blocking_crosswalk = (
        int(crosswalk_integrity["status"].isin(blocking_crosswalk_statuses).sum())
        if len(crosswalk_integrity)
        else 0
    )
    n_deferred_crosswalk = (
        int((crosswalk_integrity["status"] == "DEFERRED").sum())
        if len(crosswalk_integrity)
        else 0
    )
    n_remapped_crosswalk = (
        int((crosswalk_integrity["status"] == "REMAPPED").sum())
        if len(crosswalk_integrity)
        else 0
    )

    checks = {
        "registry loaded": (len(registry) > 0, f"{len(registry)} rows"),
        "cards matched > 0": (len(proposed) > 0, f"{len(proposed)} active cards will get links"),
        "all proposed UIDs active": (
            not missing_proposed_uids,
            "all active" if not missing_proposed_uids else f"missing={missing_proposed_uids}",
        ),
        "crosswalk UIDs active/remapped/deferred": (
            n_blocking_crosswalk == 0,
            (
                f"{n_remapped_crosswalk} remapped; "
                f"{n_deferred_crosswalk} reviewed deferral(s) withheld"
                if n_blocking_crosswalk == 0
                else (
                    f"{n_blocking_crosswalk} unreviewed or structural target(s) withheld; "
                    "inspect links_review.csv"
                )
            ),
        ),
        "fairsharing subset sanity": (n_fs <= len(proposed), f"{n_fs} FS DOIs"),
        "unresolved rows withheld": (True, f"{n_unresolved} unresolved held for review"),
    }
    ok = gate(checks)

    run_dir = new_run_dir("build_links")
    review.to_csv(os.path.join(run_dir, "links_review.csv"), index=False)
    crosswalk_integrity.to_csv(
        os.path.join(run_dir, "crosswalk_integrity.csv"),
        index=False,
    )
    pd.DataFrame(
        [
            {
                "repo_uid": uid,
                "re3data": any(link["id_type"] == "re3data" for link in proposal["links"]),
                "fairsharing": any(
                    link["id_type"] == "fairsharing_doi" for link in proposal["links"]
                ),
                "rrid": any(link["id_type"] == "rrid" for link in proposal["links"]),
                "pid_culture": proposal["pid_culture"],
            }
            for uid, proposal in sorted(proposed.items())
        ]
    ).to_csv(os.path.join(run_dir, "links_proposal.csv"), index=False)

    if n_blocking_crosswalk:
        print("\n  BLOCKING CROSSWALK TARGETS - no link updates will be applied:")
        blocking = crosswalk_integrity.loc[
            crosswalk_integrity["status"].isin(blocking_crosswalk_statuses),
            [
                "csv_id",
                "registry_name",
                "crosswalk_uid",
                "resolved_uid",
                "status",
                "candidate_uids",
                "candidate_names",
                "candidate_scores",
                "detail",
            ],
        ]
        print(blocking.to_string(index=False))
        print(
            "  Repair inputs/crosswalk.csv, add an independently audited old_uid -> "
            "current_uid mapping to id_remap.csv, or apply a reviewed DEFER for a "
            "structurally valid stale row; then rerun the preflight."
        )

    if n_deferred_crosswalk:
        print("\n  REVIEWED DEFERRED CROSSWALK TARGETS - withheld; non-blocking:")
        deferred_rows = crosswalk_integrity.loc[
            crosswalk_integrity["status"] == "DEFERRED",
            [
                "csv_id",
                "registry_name",
                "crosswalk_uid",
                "adjudication_reviewer",
                "adjudication_reviewed_at",
                "adjudication_rebased",
            ],
        ]
        print(deferred_rows.to_string(index=False))

    applied = 0
    if args.apply and ok:
        with card_transaction("build_links"):
            locked_cards = {
                uid: load_card_for_update(uid, cards[uid]) for uid in sorted(proposed)
            }
            for uid in sorted(proposed):
                proposal = proposed[uid]
                card = locked_cards[uid]
                keep = [
                    link
                    for link in card.get("links", [])
                    if link.get("id_type")
                    not in {"re3data", "fairsharing_doi", "fairsharing_internal", "rrid"}
                ]
                card["links"] = keep + [
                    link
                    for link in proposal["links"]
                    if link.get("url") or link.get("id_value")
                ]
                if proposal["pid_culture"]:
                    card.setdefault("blocks", {})["pid_culture"] = {
                        "_meta": {
                            "block_version": "1.0",
                            "source": "registry:final_ultimate",
                            "tier": "registry",
                            "as_of": YM,
                            "review_state": "auto",
                        },
                        "pid_culture": proposal["pid_culture"],
                    }
                save_card(uid, card)
                applied += 1
        print(f"\n  transactionally applied links to {applied} cards")
    elif args.apply and not ok:
        print("\n  GATE FAILED - no card changes were written.")
    else:
        print("\n  DRY RUN - review links_proposal.csv and links_review.csv, then --apply")

    print(
        f"\n  re3data ids       : {n_re3}"
        f"\n  FAIRsharing       : {n_fs}"
        f"\n  RRIDs             : {n_rrid}"
        f"\n  pid_culture       : {n_pid}"
        f"\n  unresolved        : {n_unresolved}"
        f"\n  crosswalk remapped: {n_remapped_crosswalk}"
        f"\n  crosswalk deferred: {n_deferred_crosswalk}"
        f"\n  crosswalk blocking: {n_blocking_crosswalk}"
    )

    input_paths = {
        "registry": registry_path,
        "crosswalk": os.path.join(INP, "crosswalk.csv"),
    }
    if Path(ID_REMAP_CSV).is_file():
        input_paths["id_remap"] = ID_REMAP_CSV
    if CROSSWALK_ADJUDICATIONS_CSV.is_file():
        input_paths["crosswalk_adjudications"] = CROSSWALK_ADJUDICATIONS_CSV
    ledger_append(
        "build_links",
        params={
            "registry": args.registry,
            "apply": args.apply,
            "min_conf": args.min_conf,
        },
        inputs=input_paths,
        outputs={
            "matched": len(proposed),
            "re3data": n_re3,
            "fairsharing": n_fs,
            "rrid": n_rrid,
            "pid_culture": n_pid,
            "unresolved": n_unresolved,
            "crosswalk_remapped": n_remapped_crosswalk,
            "crosswalk_deferred": n_deferred_crosswalk,
            "crosswalk_blocking": n_blocking_crosswalk,
            "applied": applied,
        },
        gate_passed=ok,
        run_dir=run_dir,
    )
    print(f"\nproposal -> {run_dir}")


if __name__ == "__main__":
    main()
