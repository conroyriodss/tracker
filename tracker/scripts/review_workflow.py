"""State model and helpers for the guided NIH Tracker review workflow.

This module intentionally separates four states that were previously spread across
many notebook cells:

* candidate queues awaiting review;
* imported model recommendations awaiting named human approval;
* human-approved recommendations awaiting promotion; and
* operational adjudications awaiting application.

Evidence-gap inventories are reported separately because they are enrichment
backlogs, not decision queues and not release blockers.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from automated_review import load_approvals, load_recommendations
from common import INP, PROJECT, RUNS, atomic_write_json
from run_provenance import digest, pinned_source, source_order
from review_widgets import (
    adjudication_path,
    load_adjudications,
    normalize_review_file,
    overlay_saved_decisions,
    queue_decision_counts,
)

STATUS_SCHEMA = "nih-tracker-guided-review-status/v1"


def _clean(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null", "undefined"} else text


def _truthy(value: Any) -> bool:
    return _clean(value).lower() in {"1", "true", "yes", "y", "on"}


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.is_file() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")


def _pending_operational_rows(path: Path, decision_columns: Iterable[str]) -> int:
    frame = _read_csv(path)
    if frame.empty:
        return 0
    decision_mask = pd.Series(False, index=frame.index, dtype=bool)
    for column in decision_columns:
        if column in frame.columns:
            decision_mask |= frame[column].astype(str).str.strip().ne("")
    receipt_mask = pd.Series(False, index=frame.index, dtype=bool)
    for column in ("applied_at", "applied_run_id", "apply_status"):
        if column in frame.columns:
            receipt_mask |= frame[column].astype(str).str.strip().ne("")
    return int((decision_mask & ~receipt_mask).sum())


def _queue_family(row: Any) -> str:
    path = Path(str(row.source_file))
    if row.kind == "crosswalk":
        return "crosswalk"
    if row.kind == "card":
        return "card"
    if path.name == "rrid_review.csv":
        return "rrid"
    if path.name == "fs_review.csv":
        return "fairsharing"
    return "identifier"


def _source_priority(path_text: str) -> int:
    path = Path(path_text)
    text = path.as_posix()
    if "/build_links_preflight/" in text:
        return 0
    if "/build_links/" in text:
        return 1
    if path.parent.resolve() == Path(PROJECT).resolve():
        return 3
    return 0


def current_queue_summary() -> list[dict[str, Any]]:
    """Return at most one current queue per review family.

    Only the newest viable artifact in each family is normalized. This avoids
    re-reading every historical queue on the Google Drive mount whenever the
    dashboard refreshes.
    """
    root = Path(RUNS)
    specifications: list[tuple[str, str, list[Path]]] = [
        (
            "identifier",
            "Combined identifier candidates",
            list(root.glob("verify_ids/*/verify_report.csv")),
        ),
        ("rrid", "RRID candidates", list(root.glob("build_rrids/*/rrid_review.csv"))),
        (
            "fairsharing",
            "FAIRsharing candidates",
            list(root.glob("build_fairsharing/*/fs_review.csv")),
        ),
        (
            "crosswalk",
            "Crosswalk references",
            list(root.glob("build_links_preflight/*/crosswalk_integrity.csv"))
            + list(root.glob("build_links/*/crosswalk_integrity.csv")),
        ),
        (
            "card",
            "Card/name candidates",
            [
                path
                for path in root.glob("*/*/*_review.csv")
                if path.name not in {"rrid_review.csv", "fs_review.csv"}
            ],
        ),
    ]
    rows: list[dict[str, Any]] = []
    saved_cache: dict[str, pd.DataFrame] = {}
    for family, label, candidates in specifications:
        pin = pinned_source(Path(PROJECT), family, candidates)
        viable = [pin] if pin else sorted(
            {path.resolve() for path in candidates if path.is_file()},
            key=source_order,
            reverse=True,
        )
        errors = []
        for source in viable:
            try:
                before_hash = digest(source)
                kind, base = normalize_review_file(source)
                if kind not in saved_cache:
                    saved_cache[kind] = load_adjudications(
                        kind, adjudication_path(kind)
                    )
                queue = overlay_saved_decisions(base, saved_cache[kind])
                counts = queue_decision_counts(kind, queue)
                if digest(source) != before_hash:
                    raise RuntimeError("Source changed while reading queue")
                if pin:
                    pinned_source(Path(PROJECT), family, candidates)
                rows.append(
                    {
                        "family": family,
                        "label": label,
                        "source_file": str(source),
                        "source_sha256": before_hash,
                        "selection": "pinned" if pin else "mtime_ns_then_path",
                        "skipped_sources": errors.copy(),
                        "run": source.parent.relative_to(root).as_posix(),
                        **{key: int(value) for key, value in counts.items()},
                        "modified_at": dt.datetime.fromtimestamp(
                            source.stat().st_mtime, dt.timezone.utc
                        ).isoformat(timespec="seconds"),
                    }
                )
                break
            except Exception as exc:
                errors.append({"source_file": str(source), "error": str(exc)})
                if pin:
                    raise RuntimeError(f"Pinned {family} source cannot be read: {source}") from exc
        else:
            if viable:
                raise RuntimeError(f"No readable {family} queue; review state is unresolved: {errors}")
    return sorted(rows, key=lambda item: (item["pending_rows"] == 0, item["label"]))


def operationally_decided_review_keys() -> set[str]:
    """Return review items that already have a durable human adjudication.

    Imported model recommendations are advisory.  Once a reviewer has written a
    non-pending decision to an operational adjudication file, a later provider
    recommendation for the same stable review key must not reopen the item
    automatically.  The existing decision remains authoritative until a human
    explicitly edits or reopens it in the manual review workflow.
    """
    decided: set[str] = set()
    for kind in ("verify", "crosswalk", "card"):
        frame = load_adjudications(kind, adjudication_path(kind))
        if frame.empty or "review_key" not in frame.columns:
            continue
        if kind == "card":
            decision = frame.get(
                "review_decision", pd.Series("", index=frame.index, dtype=str)
            ).astype(str).str.strip().str.upper()
            verdict = frame.get(
                "verdict", pd.Series("", index=frame.index, dtype=str)
            ).astype(str).str.strip()
            mask = (~decision.isin(["", "PENDING"])) | verdict.ne("")
        else:
            decision = frame.get(
                "decision", pd.Series("", index=frame.index, dtype=str)
            ).astype(str).str.strip().str.upper()
            mask = ~decision.isin(["", "PENDING"])
        decided.update(
            key
            for key in frame.loc[mask, "review_key"].astype(str).str.strip()
            if key
        )
    return decided


def pending_recommendations_for_human_approval(
    recommendations: pd.DataFrame | None = None,
    approvals: pd.DataFrame | None = None,
    *,
    decided_review_keys: set[str] | None = None,
) -> pd.DataFrame:
    """Return recommendation rows for review items that require a new approval.

    A historical approval does not permanently suppress a review item.  When all
    approvals for an item are terminal ``superseded:``/``quarantined:`` receipts
    and a different recommendation is imported later, that item is reopened.
    This is the restart-safe path for a refreshed candidate queue.
    """
    recommendations = (
        load_recommendations() if recommendations is None else recommendations.copy()
    )
    approvals = load_approvals() if approvals is None else approvals.copy()
    if recommendations.empty:
        return recommendations
    recommendations = recommendations.fillna("")
    approvals = approvals.fillna("")
    decided_review_keys = (
        operationally_decided_review_keys()
        if decided_review_keys is None
        else {_clean(value) for value in decided_review_keys if _clean(value)}
    )
    pending_indices: list[int] = []
    for review_key, group in recommendations.groupby(
        recommendations["review_key"].astype(str).str.strip(), sort=False
    ):
        review_key = _clean(review_key)
        if not review_key:
            continue
        if review_key in decided_review_keys:
            # A current human decision already exists in an operational
            # adjudication file.  Preserve the later model output for audit,
            # but do not require another approval or block finalization.
            continue
        approved_rows = approvals.loc[
            approvals.get("review_key", pd.Series("", index=approvals.index))
            .astype(str)
            .str.strip()
            .eq(review_key)
            & approvals.get("approved", pd.Series("", index=approvals.index)).map(_truthy)
        ].copy() if not approvals.empty else pd.DataFrame()
        if approved_rows.empty:
            pending_indices.extend(group.index.tolist())
            continue
        statuses = approved_rows.get(
            "promotion_status", pd.Series("", index=approved_rows.index, dtype=str)
        ).astype(str).str.strip().str.lower()
        # An approval that is still awaiting promotion remains authoritative for
        # this item; do not ask the reviewer to approve it again.
        if statuses.eq("").any():
            continue
        approved_recommendation_keys = set(
            approved_rows.get(
                "recommendation_key", pd.Series("", index=approved_rows.index, dtype=str)
            ).astype(str).str.strip()
        ) - {""}
        current_recommendation_keys = set(group["recommendation_key"].astype(str).str.strip()) - {""}
        terminal_only = statuses.str.startswith(("quarantined:", "superseded:")).all()
        if not terminal_only:
            continue
        new_keys = current_recommendation_keys - approved_recommendation_keys
        if not new_keys:
            continue
        latest_approval = pd.to_datetime(
            approved_rows.get("approved_at", ""), errors="coerce", utc=True
        ).max()
        candidate_rows = group.loc[group["recommendation_key"].astype(str).isin(new_keys)]
        latest_import = pd.to_datetime(
            candidate_rows.get("imported_at", ""), errors="coerce", utc=True
        ).max()
        if pd.isna(latest_approval) or pd.isna(latest_import) or latest_import > latest_approval:
            pending_indices.extend(candidate_rows.index.tolist())
    if not pending_indices:
        return recommendations.iloc[0:0].copy()
    return recommendations.loc[sorted(set(pending_indices))].copy()


def _recommendation_counts() -> dict[str, int]:
    recommendations = load_recommendations()
    approvals = load_approvals()
    if recommendations.empty:
        return {
            "provider_recommendations": 0,
            "review_items": 0,
            "review_items_awaiting_human_approval": 0,
            "approved_recommendations_pending_promotion": 0,
            "quarantined_or_superseded_approvals": 0,
            "recommendation_items_covered_by_current_decision": 0,
        }

    review_items = set(recommendations["review_key"].astype(str).str.strip()) - {""}
    decided_keys = operationally_decided_review_keys()
    pending_without_operational_decisions = pending_recommendations_for_human_approval(
        recommendations, approvals, decided_review_keys=set()
    )
    pending_for_approval = pending_recommendations_for_human_approval(
        recommendations, approvals, decided_review_keys=decided_keys
    )
    pending_without_keys = set(
        pending_without_operational_decisions.get(
            "review_key", pd.Series(dtype=str)
        ).astype(str).str.strip()
    ) - {""}
    pending_with_keys = set(
        pending_for_approval.get("review_key", pd.Series(dtype=str))
        .astype(str)
        .str.strip()
    ) - {""}
    covered_by_current_decision = len(pending_without_keys - pending_with_keys)
    pending_promotion = 0
    terminal_attention = 0
    if not approvals.empty:
        approved = approvals["approved"].map(_truthy)
        promotion_status = approvals.get(
            "promotion_status", pd.Series("", index=approvals.index, dtype=str)
        ).astype(str).str.strip()
        pending_promotion = int((approved & promotion_status.eq("")).sum())
        terminal_attention = int(
            (
                approved
                & promotion_status.str.lower().str.startswith(
                    ("quarantined:", "superseded:")
                )
            ).sum()
        )
    return {
        "provider_recommendations": int(len(recommendations)),
        "review_items": int(len(review_items)),
        "review_items_awaiting_human_approval": int(
            pending_for_approval["review_key"].astype(str).nunique()
            if not pending_for_approval.empty
            else 0
        ),
        "approved_recommendations_pending_promotion": pending_promotion,
        "quarantined_or_superseded_approvals": terminal_attention,
        "recommendation_items_covered_by_current_decision": int(
            covered_by_current_decision
        ),
    }


def evidence_backlog_summary() -> list[dict[str, Any]]:
    """Return only aggregate counts for non-gating evidence backlogs."""
    specifications = [
        ("interoperability", "build_interoperability", "interoperability_gaps.csv"),
        ("resource_profile", "build_resource_profiles", "resource_profile_gaps.csv"),
    ]
    rows: list[dict[str, Any]] = []
    for domain, step, filename in specifications:
        candidates = sorted(
            (Path(RUNS) / step).glob(f"*/{filename}"),
            key=source_order,
            reverse=True,
        )
        if not candidates:
            rows.append({"domain": domain, "rows": 0, "source_file": ""})
            continue
        source = candidates[0]
        frame = _read_csv(source)
        if "status" in frame.columns:
            closed = {"resolved", "accepted", "not_applicable"}
            frame = frame.loc[~frame["status"].astype(str).str.lower().isin(closed)]
        rows.append(
            {
                "domain": domain,
                "rows": int(len(frame)),
                "source_file": str(source),
            }
        )
    return rows


def compute_review_status(*, include_evidence: bool = True) -> dict[str, Any]:
    queues = current_queue_summary()
    recommendation_counts = _recommendation_counts()
    operational = {
        "identifier": _pending_operational_rows(
            Path(INP) / "verify_adjudications.csv", ["decision"]
        ),
        "crosswalk": _pending_operational_rows(
            Path(INP) / "crosswalk_adjudications.csv", ["decision"]
        ),
        "card_name": _pending_operational_rows(
            Path(INP) / "adjudications.csv", ["verdict", "review_decision"]
        ),
    }
    queue_pending = int(sum(item["pending_rows"] for item in queues))
    queue_pending_manual_only = 0
    queue_pending_ai_exportable = queue_pending - queue_pending_manual_only
    operational_pending = int(sum(operational.values()))
    pending_approval = recommendation_counts[
        "review_items_awaiting_human_approval"
    ]
    pending_promotion = recommendation_counts[
        "approved_recommendations_pending_promotion"
    ]

    if pending_approval:
        state = "APPROVAL_REQUIRED"
        next_action = "Open imported recommendations and record human approvals."
    elif pending_promotion or operational_pending:
        state = "READY_TO_FINALIZE"
        next_action = "Validate or apply all reviewed decisions."
    elif queue_pending:
        state = "REVIEW_REQUIRED"
        next_action = (
            f"Next: review {queue_pending_ai_exportable} candidate row(s), or create a Gemini queue plan "
            "in the GCP panel. Card/name, identifier and crosswalk rows support assisted review; "
            "every operational decision still requires human approval."
        )
    else:
        state = "COMPLETE"
        next_action = "Review is complete. Continue with the main pipeline."

    status = {
        "schema": STATUS_SCHEMA,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "project": str(PROJECT),
        "state": state,
        "next_action": next_action,
        "queues": queues,
        "queue_pending_rows": queue_pending,
        "queue_pending_manual_only": queue_pending_manual_only,
        "queue_pending_ai_exportable": queue_pending_ai_exportable,
        "recommendations": recommendation_counts,
        "operational_adjudications": operational,
        "operational_pending_rows": operational_pending,
        "ready_for_pipeline": bool(state == "COMPLETE"),
    }
    if include_evidence:
        status["non_gating_evidence_backlogs"] = evidence_backlog_summary()
    return status


def write_status(path: Path, *, include_evidence: bool = True) -> dict[str, Any]:
    status = compute_review_status(include_evidence=include_evidence)
    atomic_write_json(path, status)
    return status


def concise_lines(status: dict[str, Any]) -> list[str]:
    recommendations = status["recommendations"]
    operational = status["operational_adjudications"]
    lines = [
        f"Review state: {status['state']}",
        f"Next action: {status['next_action']}",
        f"Pending candidate rows: {status['queue_pending_rows']}",
        (
            "Imported review items awaiting human approval: "
            f"{recommendations['review_items_awaiting_human_approval']}"
        ),
        (
            "Imported recommendations already covered by current decisions: "
            f"{recommendations.get('recommendation_items_covered_by_current_decision', 0)}"
        ),
        (
            "Approved recommendations awaiting promotion: "
            f"{recommendations['approved_recommendations_pending_promotion']}"
        ),
        (
            "Operational adjudications awaiting application: "
            f"identifier={operational['identifier']}; "
            f"crosswalk={operational['crosswalk']}; "
            f"card/name={operational['card_name']}"
        ),
    ]
    return lines
