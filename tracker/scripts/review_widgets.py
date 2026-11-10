"""Colab ipywidgets workbench for NIH Tracker review queues.

The module keeps UI concerns separate from deterministic normalization and CSV
persistence so the latter can be unit-tested. Identifier candidate queues are
written to inputs/verify_adjudications.csv; card-level queues are translated to
inputs/adjudications.csv; and unresolved or reviewed-deferred crosswalk rows are
written to inputs/crosswalk_adjudications.csv. A receipted DEFER is nonblocking
but remains available for later reconsideration. No candidate is applied
automatically.

Typical Colab use::

    from review_widgets import discover_review_files, launch_review_workbench
    sources = discover_review_files()
    display(sources)
    workbench = launch_review_workbench(sources.iloc[0].source_file, reviewer="initials")

No card or source file is changed by this module. Apply structured decisions
separately with step_apply_verify_adjudications.py, step_promote_review.py, or
step_apply_crosswalk_adjudications.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import pandas as pd

from common import (
    ENV,
    FileLock,
    INP,
    PROJECT,
    RUNS,
    VERIFY_ADJUDICATIONS_CSV,
    atomic_write_csv_rows,
    all_cards,
)

VERIFY_DECISIONS = ("PENDING", "ACCEPT", "REJECT", "EDIT_ALIAS")
CARD_DECISIONS = ("PENDING", "ACCEPT", "REJECT", "EDIT_ALIAS")
CROSSWALK_DECISIONS = (
    "PENDING",
    "REPLACE_CROSSWALK_UID",
    "RECORD_ID_REMAP",
    "DEFER",
)
VERIFY_FIELDS = [
    "review_key",
    "source_file",
    "source_row",
    "source_run",
    "repo_uid",
    "card_name",
    "id_type",
    "existing_id",
    "existing_url",
    "candidate_id",
    "candidate_url",
    "candidate_name",
    "candidate_confidence",
    "candidate_source",
    "candidate_evidence_kind",
    "decision",
    "alias",
    "note",
    "reviewer",
    "reviewed_at",
    "applied_at",
    "applied_run_id",
    "apply_status",
]
CARD_FIELDS = [
    "review_key",
    "source_file",
    "source_row",
    "source_run",
    "review_decision",
    "repo_uid",
    "target",
    "verdict",
    "canonical_name",
    "abbreviation",
    "add_aliases",
    "drop_aliases",
    "move_to_org",
    "rrid",
    "relation_type",
    "relation_target",
    "note",
    "reviewer",
    "reviewed_at",
    "applied_at",
    "applied_run_id",
]
CROSSWALK_FIELDS = [
    "review_key",
    "source_file",
    "source_row",
    "source_run",
    "source_crosswalk_sha256",
    "source_registry_file",
    "source_registry_sha256",
    "source_id_remap_sha256",
    "source_row_fingerprint",
    "crosswalk_row",
    "csv_id",
    "probe_name",
    "registry_name",
    "original_repo_uid",
    "resolved_uid",
    "status",
    "candidate_uids",
    "candidate_names",
    "candidate_scores",
    "detail",
    "decision",
    "selected_repo_uid",
    "note",
    "reviewer",
    "reviewed_at",
    "applied_at",
    "applied_run_id",
    "apply_status",
]
CROSSWALK_ADJUDICATIONS_CSV = Path(INP) / "crosswalk_adjudications.csv"


class ReviewQueueError(ValueError):
    """Raised when a queue cannot be normalized without guessing."""


def _clean(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    value = str(value).strip()
    return "" if value.lower() in {"nan", "none", "null", "undefined"} else value


def _truthy(value: Any) -> bool:
    return _clean(value).lower() in {"1", "true", "yes", "y", "on"}


def _stable_key(*parts: Any) -> str:
    normalized = "|".join(_clean(value).lower() for value in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _source_run(path: Path) -> str:
    try:
        return path.parent.relative_to(Path(RUNS)).as_posix()
    except ValueError:
        return path.parent.name


def _read_csv(path: os.PathLike[str] | str) -> pd.DataFrame:
    source = Path(path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(source)
    try:
        frame = pd.read_csv(source, dtype=str).fillna("")
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    frame.columns = [str(column).strip() for column in frame.columns]
    return frame


def classify_review_source(path: os.PathLike[str] | str, frame: Optional[pd.DataFrame] = None) -> str:
    """Return ``verify`` for identifier candidates, otherwise ``card``.

    Classification is schema-based first and filename-based second. This avoids
    routing an arbitrary file to identifier application merely because it lives
    under a verify-named directory.
    """
    source = Path(path)
    frame = _read_csv(source) if frame is None else frame
    columns = set(frame.columns)
    if source.name == "crosswalk_integrity.csv" and {
        "status",
        "registry_name",
        "crosswalk_uid",
    }.issubset(columns):
        return "crosswalk"
    if {"candidate_id", "id_type"}.issubset(columns):
        return "verify"
    if "rrid" in columns and {"repo_uid", "card_name"}.issubset(columns):
        return "verify"
    if "doi" in columns and {"repo_uid", "card_name"}.issubset(columns):
        return "verify"
    if source.name == "verify_report.csv":
        return "verify"
    return "card"


def _verify_rows(frame: pd.DataFrame, source: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for source_row, raw in frame.iterrows():
        row = {str(key): _clean(value) for key, value in raw.to_dict().items()}
        if {"candidate_id", "id_type"}.issubset(frame.columns):
            candidate_id = row.get("candidate_id", "")
            candidate_url = row.get("candidate_url", "")
            # verify_report.csv may include below-threshold diagnostics. Review
            # only candidates that explicitly cleared the source threshold.
            if source.name == "verify_report.csv":
                if not candidate_id and not candidate_url:
                    continue
                if not _truthy(row.get("candidate_eligible")):
                    continue
            elif not candidate_id and not candidate_url:
                continue
            id_type = row.get("id_type", "").lower()
            candidate_name = row.get("candidate_name", "")
            confidence = row.get("candidate_confidence", "")
            candidate_source = row.get("candidate_source", "")
            evidence_kind = row.get("candidate_evidence_kind", "")
            existing_id = row.get("id_value", "")
            existing_url = row.get("url", "")
        elif row.get("rrid"):
            id_type = "rrid"
            candidate_id = row["rrid"]
            candidate_url = f"https://scicrunch.org/resolver/{candidate_id}"
            candidate_name = row.get("resolved_name") or row.get("matched_name", "")
            confidence = row.get("confidence", "")
            candidate_source = "build_rrids"
            evidence_kind = row.get("basis", "")
            existing_id = ""
            existing_url = ""
        elif row.get("doi"):
            id_type = "fairsharing_doi"
            candidate_id = row["doi"]
            candidate_url = f"https://fairsharing.org/{candidate_id}"
            candidate_name = row.get("resolved_title") or row.get("matched_name", "")
            confidence = row.get("confidence", "")
            candidate_source = "build_fairsharing"
            evidence_kind = row.get("basis", "")
            existing_id = ""
            existing_url = ""
        else:
            continue

        repo_uid = row.get("repo_uid", "")
        key = _stable_key("verify", repo_uid, id_type, candidate_id, candidate_url)
        rows.append(
            {
                "review_key": key,
                "source_file": str(source.resolve()),
                "source_row": str(source_row + 2),  # CSV line including header
                "source_run": _source_run(source),
                "repo_uid": repo_uid,
                "card_name": row.get("card_name", ""),
                "id_type": id_type,
                "existing_id": existing_id,
                "existing_url": existing_url,
                "candidate_id": candidate_id,
                "candidate_url": candidate_url,
                "candidate_name": candidate_name,
                "candidate_confidence": confidence,
                "candidate_source": candidate_source,
                "candidate_evidence_kind": evidence_kind,
                "decision": "PENDING",
                "alias": "",
                "note": row.get("notes", ""),
                "reviewer": "",
                "reviewed_at": "",
                "applied_at": "",
                "applied_run_id": "",
                "apply_status": "",
            }
        )
    return rows


def _first(row: Mapping[str, str], *keys: str) -> str:
    for key in keys:
        value = _clean(row.get(key, ""))
        if value:
            return value
    return ""


def _card_rows(frame: pd.DataFrame, source: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for source_row, raw in frame.iterrows():
        row = {str(key): _clean(value) for key, value in raw.to_dict().items()}
        repo_uid = _first(row, "repo_uid", "matched_card", "card_uid")
        candidate_label = _first(
            row,
            "registry_name",
            "candidate_name",
            "matched_name",
            "name",
            "card_name",
        )
        context = "; ".join(
            f"{key}={value}"
            for key, value in row.items()
            if value and key not in {"repo_uid", "matched_card", "card_uid"}
        )
        key = _stable_key(
            "card",
            source.name,
            repo_uid,
            candidate_label,
            row.get("how", ""),
            row.get("basis", ""),
        )
        rows.append(
            {
                "review_key": key,
                "source_file": str(source.resolve()),
                "source_row": str(source_row + 2),
                "source_run": _source_run(source),
                "review_decision": "PENDING",
                "repo_uid": repo_uid,
                "target": candidate_label,
                "verdict": "",
                "canonical_name": "",
                "abbreviation": "",
                "add_aliases": "",
                "drop_aliases": "",
                "move_to_org": "",
                "rrid": "",
                "relation_type": "",
                "relation_target": "",
                "note": context,
                "reviewer": "",
                "reviewed_at": "",
                "applied_at": "",
                "applied_run_id": "",
            }
        )
    return rows


def _crosswalk_rows(frame: pd.DataFrame, source: Path) -> list[dict[str, str]]:
    """Normalize unresolved and reviewed-deferred registry-linked rows."""
    required = {
        "source_row_fingerprint",
        "crosswalk_sha256",
        "crosswalk_row",
        "registry_file",
        "registry_sha256",
    }
    if not required.issubset(frame.columns):
        raise ReviewQueueError(
            "Crosswalk report lacks current source-binding fields; rerun the current "
            "crosswalk preflight before adjudicating it."
        )
    rows: list[dict[str, str]] = []
    reviewable = {"STALE", "DEFERRED", "CONFLICT", "INVALID_ROW"}
    for source_row, raw in frame.iterrows():
        row = {str(key): _clean(value) for key, value in raw.to_dict().items()}
        status = row.get("status", "").upper()
        if status not in reviewable:
            continue
        original_uid = row.get("crosswalk_uid", "")
        row_fingerprint = row.get("source_row_fingerprint", "") or _stable_key(
            "crosswalk-source",
            row.get("csv_id", ""),
            row.get("probe_name", ""),
            row.get("registry_name", ""),
            original_uid,
        )
        key = _stable_key("crosswalk", row_fingerprint)
        rows.append(
            {
                "review_key": key,
                "source_file": str(source.resolve()),
                "source_row": str(source_row + 2),
                "source_run": _source_run(source),
                "source_crosswalk_sha256": row.get("crosswalk_sha256", ""),
                "source_registry_file": row.get("registry_file", ""),
                "source_registry_sha256": row.get("registry_sha256", ""),
                "source_id_remap_sha256": row.get("id_remap_sha256", ""),
                "source_row_fingerprint": row_fingerprint,
                "crosswalk_row": row.get("crosswalk_row", ""),
                "csv_id": row.get("csv_id", ""),
                "probe_name": row.get("probe_name", ""),
                "registry_name": row.get("registry_name", ""),
                "original_repo_uid": original_uid,
                "resolved_uid": row.get("resolved_uid", ""),
                "status": status,
                "candidate_uids": row.get("candidate_uids", ""),
                "candidate_names": row.get("candidate_names", ""),
                "candidate_scores": row.get("candidate_scores", ""),
                "detail": row.get("detail", ""),
                "decision": "PENDING",
                "selected_repo_uid": "",
                "note": "",
                "reviewer": "",
                "reviewed_at": "",
                "applied_at": "",
                "applied_run_id": "",
                "apply_status": "",
            }
        )
    return rows


def normalize_review_file(path: os.PathLike[str] | str) -> tuple[str, pd.DataFrame]:
    """Normalize one emitted review file into a stable, writable queue."""
    source = Path(path).expanduser()
    frame = _read_csv(source)
    kind = classify_review_source(source, frame)
    if kind == "verify":
        rows, fields = _verify_rows(frame, source), VERIFY_FIELDS
    elif kind == "crosswalk":
        rows, fields = _crosswalk_rows(frame, source), CROSSWALK_FIELDS
    else:
        rows, fields = _card_rows(frame, source), CARD_FIELDS
    return kind, pd.DataFrame(rows, columns=fields)


def adjudication_path(kind: str) -> Path:
    if kind == "verify":
        return Path(VERIFY_ADJUDICATIONS_CSV)
    if kind == "card":
        return Path(INP) / "adjudications.csv"
    if kind == "crosswalk":
        return CROSSWALK_ADJUDICATIONS_CSV
    raise ValueError(f"Unknown review kind: {kind!r}")


def load_adjudications(kind: str, path: os.PathLike[str] | str | None = None) -> pd.DataFrame:
    destination = Path(path) if path else adjudication_path(kind)
    fields = VERIFY_FIELDS if kind == "verify" else CROSSWALK_FIELDS if kind == "crosswalk" else CARD_FIELDS
    if not destination.is_file() or destination.stat().st_size == 0:
        return pd.DataFrame(columns=fields)
    frame = _read_csv(destination)
    for field in fields:
        if field not in frame.columns:
            frame[field] = ""
    return frame[fields]


def overlay_saved_decisions(queue: pd.DataFrame, saved: pd.DataFrame) -> pd.DataFrame:
    """Overlay prior reviewer fields by stable review_key without duplicating rows."""
    if queue.empty or saved.empty or "review_key" not in saved.columns:
        return queue.copy()
    result = queue.copy()
    saved_by_key = {
        _clean(row.get("review_key")): row
        for row in saved.to_dict("records")
        if _clean(row.get("review_key"))
    }
    editable = {
        "decision",
        "review_decision",
        "repo_uid",
        "alias",
        "note",
        "reviewer",
        "reviewed_at",
        "verdict",
        "add_aliases",
        "applied_at",
        "applied_run_id",
        "apply_status",
        "selected_repo_uid",
    }
    for index, row in result.iterrows():
        prior = saved_by_key.get(_clean(row.get("review_key")))
        if not prior:
            continue
        for field in editable & set(result.columns):
            if field in prior:
                result.at[index, field] = _clean(prior.get(field))
    return result


def _map_card_decision(row: dict[str, str]) -> dict[str, str]:
    decision = _clean(row.get("review_decision") or row.get("decision")).upper()
    if decision not in CARD_DECISIONS:
        raise ReviewQueueError(f"Unknown card review decision: {decision!r}")
    row["review_decision"] = decision
    if decision == "ACCEPT":
        # Accepting a fuzzy/name candidate records that source label as an
        # adjudicated alias. That makes the next deterministic join exact.
        candidate_alias = _clean(row.get("target"))
        if candidate_alias and _clean(row.get("repo_uid")):
            row["add_aliases"] = candidate_alias
            row["verdict"] = "ACCEPT_ALIAS"
        else:
            row["verdict"] = "ACCEPT"
    elif decision == "REJECT":
        row["verdict"] = "REJECT_CANDIDATE"
    elif decision == "EDIT_ALIAS":
        alias = _clean(row.get("add_aliases") or row.get("alias"))
        if not alias:
            raise ReviewQueueError(
                f"EDIT_ALIAS requires an alias for review_key={row.get('review_key', '')}."
            )
        if not _clean(row.get("repo_uid")):
            raise ReviewQueueError(
                f"EDIT_ALIAS requires repo_uid for review_key={row.get('review_key', '')}."
            )
        row["add_aliases"] = alias
        row["verdict"] = "ACCEPT_ALIAS"
    else:
        row["verdict"] = ""
    return row


def prepare_decisions(
    kind: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    reviewer: str = "",
    reviewed_at: str | None = None,
) -> list[dict[str, str]]:
    """Validate and canonicalize UI state before it enters an adjudication file."""
    fields = VERIFY_FIELDS if kind == "verify" else CROSSWALK_FIELDS if kind == "crosswalk" else CARD_FIELDS
    timestamp = reviewed_at or dt.datetime.now().isoformat(timespec="seconds")
    prepared: list[dict[str, str]] = []
    for raw in rows:
        row = {field: _clean(raw.get(field, "")) for field in fields}
        if not row["review_key"]:
            raise ReviewQueueError("Every decision requires a stable review_key.")
        if kind == "verify":
            decision = _clean(row.get("decision")).upper()
            if decision not in VERIFY_DECISIONS:
                raise ReviewQueueError(f"Unknown verify decision: {decision!r}")
            if decision == "PENDING":
                continue
            if not row["repo_uid"] or not row["id_type"]:
                raise ReviewQueueError(
                    f"{decision} requires repo_uid and id_type for {row['review_key']}."
                )
            if decision in {"ACCEPT", "REJECT"} and not (
                row["candidate_id"] or row["candidate_url"]
            ):
                raise ReviewQueueError(
                    f"{decision} requires a candidate ID or URL for {row['review_key']}."
                )
            if decision == "EDIT_ALIAS" and not row["alias"]:
                raise ReviewQueueError(
                    f"EDIT_ALIAS requires alias text for {row['review_key']}."
                )
        elif kind == "card":
            decision = _clean(row.get("review_decision")).upper()
            if decision == "PENDING":
                continue
            row = _map_card_decision(row)
            if decision in {"ACCEPT", "EDIT_ALIAS"} and not row["repo_uid"]:
                raise ReviewQueueError(
                    f"{decision} requires repo_uid for {row['review_key']}."
                )
        elif kind == "crosswalk":
            decision = _clean(row.get("decision")).upper()
            if decision not in CROSSWALK_DECISIONS:
                raise ReviewQueueError(f"Unknown crosswalk decision: {decision!r}")
            if decision == "PENDING":
                continue
            if not row["source_row_fingerprint"]:
                raise ReviewQueueError(
                    f"Crosswalk decision requires source_row_fingerprint for {row['review_key']}."
                )
            if decision in {"REPLACE_CROSSWALK_UID", "RECORD_ID_REMAP"}:
                if not row["selected_repo_uid"]:
                    raise ReviewQueueError(
                        f"{decision} requires a selected active UID for {row['review_key']}."
                    )
                if not row["note"]:
                    raise ReviewQueueError(
                        f"{decision} requires an evidence note for {row['review_key']}."
                    )
            if decision == "RECORD_ID_REMAP" and not row["original_repo_uid"]:
                raise ReviewQueueError(
                    f"RECORD_ID_REMAP requires an original UID for {row['review_key']}."
                )
        else:
            raise ValueError(f"Unknown review kind: {kind!r}")
        row["reviewer"] = _clean(reviewer) or row.get("reviewer", "")
        row["reviewed_at"] = timestamp
        prepared.append(row)
    return prepared


def upsert_adjudications(
    kind: str,
    decisions: Iterable[Mapping[str, Any]],
    *,
    path: os.PathLike[str] | str | None = None,
) -> tuple[Path, int, int]:
    """Atomically upsert decisions by review_key; return path, inserted, updated."""
    destination = Path(path) if path else adjudication_path(kind)
    fields = VERIFY_FIELDS if kind == "verify" else CROSSWALK_FIELDS if kind == "crosswalk" else CARD_FIELDS
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(
        f"{destination}.lock",
        purpose=f"{kind} adjudication upsert",
        timeout=5,
    )
    with lock:
        # Re-read only after acquiring the adjudication lock. Atomic replace
        # prevents torn files; this read/merge/write lock also prevents lost rows.
        existing = load_adjudications(kind, destination)
        records = existing.to_dict("records")
        by_key = {
            _clean(row.get("review_key")): index
            for index, row in enumerate(records)
            if _clean(row.get("review_key"))
        }
        inserted = updated = 0
        for raw in decisions:
            row = {field: _clean(raw.get(field, "")) for field in fields}
            key = row["review_key"]
            if not key:
                raise ReviewQueueError("Cannot persist a decision without review_key.")
            if key in by_key:
                prior = records[by_key[key]]
                # Applying a replacement decision invalidates an old application
                # receipt; the apply step will issue a new receipt.
                if any(
                    _clean(prior.get(field)) != row.get(field, "")
                    for field in (
                        "decision",
                        "review_decision",
                        "repo_uid",
                        "alias",
                        "add_aliases",
                        "selected_repo_uid",
                    )
                    if field in fields
                ):
                    for field in ("applied_at", "applied_run_id", "apply_status"):
                        if field in row:
                            row[field] = ""
                records[by_key[key]] = row
                updated += 1
            else:
                by_key[key] = len(records)
                records.append(row)
                inserted += 1
        atomic_write_csv_rows(destination, records, fields)
    return destination, inserted, updated


def queue_decision_counts(kind: str, queue: pd.DataFrame) -> dict[str, int]:
    """Return stable pending/decided/applied counts for a normalized queue.

    ``reviewable_rows`` describes the source queue. ``pending_rows`` describes
    what the default exporter/workbench can act on after overlaying saved
    adjudications. This distinction prevents a reviewed DEFER queue from being
    presented as though it still contains ten new items.
    """
    total = int(len(queue))
    if queue.empty:
        return {
            "reviewable_rows": 0,
            "pending_rows": 0,
            "decided_rows": 0,
            "applied_rows": 0,
        }
    if kind == "card":
        decision = queue.get(
            "review_decision",
            pd.Series("", index=queue.index, dtype=str),
        ).astype(str).str.strip().str.upper()
        verdict = queue.get(
            "verdict",
            pd.Series("", index=queue.index, dtype=str),
        ).astype(str).str.strip()
        pending = decision.isin(["", "PENDING"]) & verdict.eq("")
    else:
        decision = queue.get(
            "decision",
            pd.Series("", index=queue.index, dtype=str),
        ).astype(str).str.strip().str.upper()
        pending = decision.isin(["", "PENDING"])
    applied = queue.get(
        "applied_at",
        pd.Series("", index=queue.index, dtype=str),
    ).astype(str).str.strip().ne("")
    if "apply_status" in queue.columns:
        applied = applied | queue["apply_status"].astype(str).str.strip().ne("")
    return {
        "reviewable_rows": total,
        "pending_rows": int(pending.sum()),
        "decided_rows": int((~pending).sum()),
        "applied_rows": int(applied.sum()),
    }


def review_queue_status(path: os.PathLike[str] | str) -> tuple[str, pd.DataFrame, dict[str, int]]:
    """Normalize one queue, overlay durable decisions, and return row counts."""
    kind, base = normalize_review_file(path)
    saved = load_adjudications(kind, adjudication_path(kind))
    queue = overlay_saved_decisions(base, saved)
    return kind, queue, queue_decision_counts(kind, queue)


def discover_review_files(
    runs_root: os.PathLike[str] | str = RUNS,
    *,
    include_project_verify_report: bool = True,
) -> pd.DataFrame:
    """Find emitted review queues and distinguish source rows from pending rows."""
    root = Path(runs_root)
    candidates: set[Path] = set()
    if root.is_dir():
        candidates.update(root.glob("*/*/*_review.csv"))
        candidates.update(root.glob("*/verify_report.csv"))
        candidates.update(root.glob("*/*/verify_report.csv"))
        candidates.update(root.glob("*/*/crosswalk_integrity.csv"))
    if include_project_verify_report:
        report = Path(PROJECT) / "verify_report.csv"
        if report.is_file():
            candidates.add(report)

    rows = []
    saved_cache: dict[str, pd.DataFrame] = {}
    for path in sorted(candidates, key=lambda value: (value.stat().st_mtime_ns, value.as_posix()), reverse=True):
        try:
            kind, base = normalize_review_file(path)
            if kind not in saved_cache:
                saved_cache[kind] = load_adjudications(kind, adjudication_path(kind))
            queue = overlay_saved_decisions(base, saved_cache[kind])
            counts = queue_decision_counts(kind, queue)
            rows.append(
                {
                    "source_file": str(path.resolve()),
                    "kind": kind,
                    **counts,
                    "modified_at": dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(
                        timespec="seconds"
                    ),
                    "run": _source_run(path),
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "source_file": str(path.resolve()),
                    "kind": "error",
                    "reviewable_rows": 0,
                    "pending_rows": 0,
                    "decided_rows": 0,
                    "applied_rows": 0,
                    "modified_at": dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(
                        timespec="seconds"
                    ),
                    "run": f"ERROR: {exc}",
                }
            )
    return pd.DataFrame(
        rows,
        columns=[
            "source_file",
            "kind",
            "reviewable_rows",
            "pending_rows",
            "decided_rows",
            "applied_rows",
            "modified_at",
            "run",
        ],
    )


@dataclass
class _RowWidgets:
    decision: Any
    repo_uid: Any
    alias: Any
    note: Any


class ReviewWorkbench:
    """Paginated ipywidgets controller for one normalized source file."""

    def __init__(
        self,
        source_file: os.PathLike[str] | str,
        *,
        reviewer: str = "",
        page_size: int = 8,
        include_decided: bool = True,
    ) -> None:
        try:
            import ipywidgets as widgets
            from IPython.display import display
        except ImportError as exc:  # pragma: no cover - Colab normally provides these
            raise RuntimeError("ipywidgets and IPython are required for the review UI.") from exc

        self.widgets = widgets
        self.display = display
        self.source_file = Path(source_file).expanduser().resolve()
        self.kind, base = normalize_review_file(self.source_file)
        self.destination = adjudication_path(self.kind)
        saved = load_adjudications(self.kind, self.destination)
        self.queue = overlay_saved_decisions(base, saved)
        decision_column = "review_decision" if self.kind == "card" else "decision"
        if not include_decided and not self.queue.empty:
            self.queue = self.queue.loc[
                self.queue[decision_column].astype(str).str.upper().eq("PENDING")
            ].reset_index(drop=True)
        self.reviewer = reviewer
        cards = all_cards()
        self.card_options = [("[select card]", "")] + [
            (
                f"{uid} — {_clean((card.get('identity') or {}).get('name')) or '[unnamed]'}",
                uid,
            )
            for uid, card in sorted(cards.items())
        ]
        self.page_size = max(1, int(page_size))
        self.page = 0
        self.row_widgets: dict[int, _RowWidgets] = {}
        self.status = widgets.HTML()
        self.body = widgets.VBox()
        self.prev_button = widgets.Button(description="Previous", icon="arrow-left")
        self.next_button = widgets.Button(description="Next", icon="arrow-right")
        self.save_button = widgets.Button(
            description="Save explicit decisions", icon="save", button_style="success"
        )
        self.page_label = widgets.HTML()
        self.prev_button.on_click(lambda _: self._move(-1))
        self.next_button.on_click(lambda _: self._move(1))
        self.save_button.on_click(self._save)
        title = widgets.HTML(
            f"<h3>NIH Tracker review workbench</h3>"
            f"<b>Environment:</b> {html.escape(ENV)} &nbsp; "
            f"<b>Queue:</b> {html.escape(str(self.source_file))}<br>"
            f"<b>Destination:</b> {html.escape(str(self.destination))} &nbsp; "
            f"<b>Rows:</b> {len(self.queue)}"
        )
        controls = widgets.HBox(
            [self.prev_button, self.page_label, self.next_button, self.save_button]
        )
        self.root = widgets.VBox([title, controls, self.status, self.body])
        self._render()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.queue) + self.page_size - 1) // self.page_size)

    def show(self):
        self.display(self.root)
        return self

    def _capture(self) -> None:
        decision_column = "review_decision" if self.kind == "card" else "decision"
        for index, controls in self.row_widgets.items():
            self.queue.at[index, decision_column] = controls.decision.value
            if self.kind == "crosswalk":
                self.queue.at[index, "selected_repo_uid"] = controls.repo_uid.value.strip()
            else:
                self.queue.at[index, "repo_uid"] = controls.repo_uid.value.strip()
                alias_column = "alias" if self.kind == "verify" else "add_aliases"
                self.queue.at[index, alias_column] = controls.alias.value.strip()
            self.queue.at[index, "note"] = controls.note.value.strip()

    def _move(self, delta: int) -> None:
        self._capture()
        self.page = min(max(0, self.page + delta), self.page_count - 1)
        self._render()

    def _row_heading(self, row: Mapping[str, Any], ordinal: int) -> str:
        if self.kind == "verify":
            candidate_url = _clean(row.get("candidate_url"))
            candidate = html.escape(_clean(row.get("candidate_id")) or candidate_url)
            if candidate_url.startswith(("http://", "https://")):
                candidate = (
                    f'<a href="{html.escape(candidate_url, quote=True)}" target="_blank">'
                    f"{candidate}</a>"
                )
            return (
                f"<b>{ordinal}. {html.escape(_clean(row.get('repo_uid')))}</b> — "
                f"{html.escape(_clean(row.get('card_name')))}<br>"
                f"{html.escape(_clean(row.get('id_type')))} candidate: {candidate}; "
                f"name={html.escape(_clean(row.get('candidate_name')))}; "
                f"confidence={html.escape(_clean(row.get('candidate_confidence')))}; "
                f"evidence={html.escape(_clean(row.get('candidate_evidence_kind')))}"
            )
        if self.kind == "crosswalk":
            return (
                f"<b>{ordinal}. row {html.escape(_clean(row.get('crosswalk_row')))} | "
                f"{html.escape(_clean(row.get('status')))}</b> — "
                f"{html.escape(_clean(row.get('registry_name')))}<br>"
                f"current target={html.escape(_clean(row.get('original_repo_uid')) or '[missing]')}; "
                f"candidates={html.escape(_clean(row.get('candidate_uids')))}; "
                f"scores={html.escape(_clean(row.get('candidate_scores')))}<br>"
                f"{html.escape(_clean(row.get('detail'))[:700])}"
            )
        return (
            f"<b>{ordinal}. {html.escape(_clean(row.get('repo_uid')) or '[UID required for accept/edit]')}</b> — "
            f"candidate={html.escape(_clean(row.get('target')))}<br>"
            f"{html.escape(_clean(row.get('note'))[:700])}"
        )

    def _render(self) -> None:
        self.row_widgets = {}
        start = self.page * self.page_size
        stop = min(len(self.queue), start + self.page_size)
        boxes = []
        decision_column = "review_decision" if self.kind == "card" else "decision"
        if self.kind == "verify":
            choices = VERIFY_DECISIONS
        elif self.kind == "crosswalk":
            choices = CROSSWALK_DECISIONS
        else:
            choices = CARD_DECISIONS
        for ordinal, index in enumerate(range(start, stop), start=start + 1):
            row = self.queue.loc[index].to_dict()
            decision = self.widgets.Dropdown(
                options=choices,
                value=_clean(row.get(decision_column)).upper()
                if _clean(row.get(decision_column)).upper() in choices
                else "PENDING",
                description="Decision",
                layout=self.widgets.Layout(width="300px"),
            )
            if self.kind == "crosswalk":
                current_uid = _clean(row.get("selected_repo_uid"))
                candidate_uids = [
                    value.strip()
                    for value in _clean(row.get("candidate_uids")).split(";")
                    if value.strip()
                ]
                card_by_uid = {value: label for label, value in self.card_options if value}
                ordered = []
                for uid in candidate_uids:
                    if uid in card_by_uid and uid not in ordered:
                        ordered.append(uid)
                ordered.extend(uid for uid in card_by_uid if uid not in ordered)
                options = [("[select active target]", "")] + [
                    (card_by_uid[uid], uid) for uid in ordered
                ]
                repo_uid = self.widgets.Dropdown(
                    options=options,
                    value=current_uid if current_uid in {value for _, value in options} else "",
                    description="Target UID",
                    layout=self.widgets.Layout(width="620px"),
                )
                alias = self.widgets.HTML(
                    value=(
                        "<i>REPLACE_CROSSWALK_UID edits only this crosswalk row; "
                        "RECORD_ID_REMAP asserts documented historical UID continuity; "
                        "DEFER excludes the row and becomes nonblocking only after the "
                        "decision is applied and receipted.</i>"
                    )
                )
            else:
                current_uid = _clean(row.get("repo_uid"))
                if self.kind == "verify":
                    uid_options = [(current_uid or "[missing UID]", current_uid)]
                    repo_uid = self.widgets.Dropdown(
                        options=uid_options,
                        value=current_uid,
                        description="repo_uid",
                        disabled=True,
                        layout=self.widgets.Layout(width="360px"),
                    )
                else:
                    uid_values = {value for _, value in self.card_options}
                    options = list(self.card_options)
                    if current_uid and current_uid not in uid_values:
                        options.insert(1, (f"{current_uid} — [not in current cards]", current_uid))
                    repo_uid = self.widgets.Dropdown(
                        options=options,
                        value=current_uid if current_uid in {value for _, value in options} else "",
                        description="repo_uid",
                        layout=self.widgets.Layout(width="520px"),
                    )
                alias_column = "alias" if self.kind == "verify" else "add_aliases"
                alias = self.widgets.Text(
                    value=_clean(row.get(alias_column)),
                    description="Alias",
                    placeholder="required for EDIT_ALIAS",
                    layout=self.widgets.Layout(width="520px"),
                )
            note = self.widgets.Textarea(
                value=_clean(row.get("note")),
                description="Note",
                rows=2,
                layout=self.widgets.Layout(width="780px"),
            )
            self.row_widgets[index] = _RowWidgets(decision, repo_uid, alias, note)
            boxes.append(
                self.widgets.VBox(
                    [
                        self.widgets.HTML(self._row_heading(row, ordinal)),
                        self.widgets.HBox([decision, repo_uid]),
                        alias,
                        note,
                    ],
                    layout=self.widgets.Layout(
                        border="1px solid #bbb", padding="8px", margin="4px 0"
                    ),
                )
            )
        self.body.children = tuple(boxes) if boxes else (
            self.widgets.HTML("<i>No reviewable rows in this source.</i>"),
        )
        self.page_label.value = f"&nbsp; Page {self.page + 1} of {self.page_count} &nbsp;"
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.page_count - 1

    def _save(self, _button=None) -> None:
        self._capture()
        try:
            decisions = prepare_decisions(
                self.kind,
                self.queue.to_dict("records"),
                reviewer=self.reviewer,
            )
            path, inserted, updated = upsert_adjudications(
                self.kind, decisions, path=self.destination
            )
            self.status.value = (
                f"<span style='color:green'><b>Saved {len(decisions)} explicit decision(s)</b> "
                f"({inserted} inserted, {updated} updated) to {html.escape(str(path))}. "
                "Cards were not modified.</span>"
            )
        except Exception as exc:
            self.status.value = (
                f"<span style='color:#b00020'><b>Not saved:</b> {html.escape(str(exc))}</span>"
            )


def launch_review_workbench(
    source_file: os.PathLike[str] | str,
    *,
    reviewer: str = "",
    page_size: int = 8,
    include_decided: bool = True,
) -> ReviewWorkbench:
    """Construct, display, and return a paginated review workbench."""
    return ReviewWorkbench(
        source_file,
        reviewer=reviewer,
        page_size=page_size,
        include_decided=include_decided,
    ).show()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List discoverable review files.")
    parser.add_argument("--source", help="Normalize one review file and print a preview.")
    args = parser.parse_args()
    if args.source:
        kind, frame = normalize_review_file(args.source)
        print(f"kind={kind}; reviewable_rows={len(frame)}; destination={adjudication_path(kind)}")
        print(frame.head(20).to_string(index=False))
    else:
        print(discover_review_files().to_string(index=False))


if __name__ == "__main__":
    main()
