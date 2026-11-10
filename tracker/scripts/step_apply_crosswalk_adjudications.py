"""Validate and apply structured crosswalk-reference adjudications.

This command consumes ``inputs/crosswalk_adjudications.csv`` produced by the
review workbench. It supports three explicit decisions:

``REPLACE_CROSSWALK_UID``
    Correct only the selected row in ``inputs/crosswalk.csv``.
``RECORD_ID_REMAP``
    Record an evidence-backed historical old-UID -> active-UID relationship in
    ``id_remap.csv`` without rewriting the crosswalk row.
``DEFER``
    Record a reviewed non-match disposition. Once receipted, the row remains
    excluded from the current registry join but no longer blocks the pipeline.

Dry run is the default. Apply is transaction-protected and row-fingerprint bound.
Unrelated changes elsewhere in ``crosswalk.csv`` do not invalidate an unchanged
reviewed row; material row, registry, card-set, remap, or adjudication drift still
fails closed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from common import (
    ID_REMAP_CSV,
    INP,
    all_cards,
    append_id_remap,
    card_transaction,
    current_run_id,
    gate,
    ledger_append,
    new_run_dir,
    sha256_file,
)
from step_build_links import (
    crosswalk_row_fingerprint,
    load_crosswalk_targets,
    load_uid_remap,
)

ALLOWED_DECISIONS = {
    "REPLACE_CROSSWALK_UID",
    "RECORD_ID_REMAP",
    "DEFER",
}
BLOCKING_STATUSES = {"STALE", "CONFLICT", "INVALID_ROW"}
REQUIRED_COLUMNS = {
    "review_key",
    "source_crosswalk_sha256",
    "source_registry_file",
    "source_registry_sha256",
    "source_row_fingerprint",
    "crosswalk_row",
    "csv_id",
    "probe_name",
    "registry_name",
    "original_repo_uid",
    "status",
    "decision",
    "selected_repo_uid",
    "note",
    "reviewer",
}
RECEIPT_COLUMNS = ("applied_at", "applied_run_id", "apply_status")


def _clean(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null", "undefined"} else text


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        frame = pd.read_csv(path, dtype=str).fillna("")
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    frame.columns = [str(column).strip() for column in frame.columns]
    return frame


def _crosswalk_fingerprints(frame: pd.DataFrame) -> list[str]:
    return [
        crosswalk_row_fingerprint(
            csv_id=row.get("csv_id", ""),
            probe_name=row.get("probe_name", ""),
            registry_name=row.get("registry_name", ""),
            repo_uid=row.get("repo_uid", ""),
        )
        for row in frame.to_dict("records")
    ]


def _explicit_rows(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    decisions = frame["decision"].map(lambda value: _clean(value).upper())
    applied = frame.get("apply_status", pd.Series("", index=frame.index)).map(_clean)
    return frame.loc[
        decisions.isin(ALLOWED_DECISIONS) & applied.eq("")
    ].copy()


def _plan(
    adjudications: pd.DataFrame,
    crosswalk: pd.DataFrame,
    cards: Mapping[str, Any],
    *,
    crosswalk_sha256: str,
    registry_file: str,
    registry_names: list[str],
    registry_sha256: str,
    id_remap_sha256: str,
) -> tuple[pd.DataFrame, list[str]]:
    """Build a row-bound plan and reject only material source drift.

    The whole crosswalk file may legitimately change when a different reviewed row is
    corrected. A pending decision remains valid when its exact source-row fingerprint
    still resolves uniquely and the registry snapshot is unchanged. This preserves
    optimistic concurrency without forcing reviewers to repeat unaffected decisions.
    """
    errors: list[str] = []
    explicit = _explicit_rows(adjudications)
    fingerprints = _crosswalk_fingerprints(crosswalk)
    by_fingerprint: dict[str, list[int]] = {}
    for index, fingerprint in enumerate(fingerprints):
        by_fingerprint.setdefault(fingerprint, []).append(index)

    current_mapping, current_report = load_crosswalk_targets(
        cards,
        uid_remap=load_uid_remap(),
        crosswalk_path=Path(INP) / "crosswalk.csv",
        registry_names=registry_names,
        registry_file=registry_file,
        registry_sha256=registry_sha256,
    )
    del current_mapping
    report_by_fingerprint = {
        _clean(row.get("source_row_fingerprint")): row
        for row in current_report.to_dict("records")
        if _clean(row.get("source_row_fingerprint"))
    }

    planned: list[dict[str, str]] = []
    seen_review_keys: set[str] = set()
    seen_source_rows: dict[str, str] = {}
    remap_targets: dict[str, str] = {}

    for record in explicit.to_dict("records"):
        row = {str(key): _clean(value) for key, value in record.items()}
        decision = row.get("decision", "").upper()
        review_key = row.get("review_key", "")
        fingerprint = row.get("source_row_fingerprint", "")
        selected_uid = row.get("selected_repo_uid", "")
        original_uid = row.get("original_repo_uid", "")
        source_crosswalk_row = row.get("crosswalk_row", "")
        row_errors: list[str] = []

        if not review_key:
            row_errors.append("missing review_key")
        elif review_key in seen_review_keys:
            row_errors.append("duplicate review_key")
        seen_review_keys.add(review_key)

        prior_decision = seen_source_rows.get(fingerprint)
        if prior_decision and prior_decision != decision:
            row_errors.append(
                f"same source row has conflicting decisions {prior_decision} and {decision}"
            )
        seen_source_rows[fingerprint] = decision

        source_crosswalk_hash = row.get("source_crosswalk_sha256", "")
        if not source_crosswalk_hash:
            row_errors.append("missing source_crosswalk_sha256")
        crosswalk_snapshot_changed = bool(
            source_crosswalk_hash and source_crosswalk_hash != crosswalk_sha256
        )

        expected_registry_hash = row.get("source_registry_sha256", "")
        if not expected_registry_hash or expected_registry_hash != registry_sha256:
            row_errors.append(
                "registry snapshot differs from the preflight snapshot; rerun preflight and review"
            )
        expected_registry_file = Path(row.get("source_registry_file", "")).name
        if not expected_registry_file or expected_registry_file != registry_file:
            row_errors.append(
                "registry file differs from the preflight snapshot; rerun preflight and review"
            )

        source_id_remap_hash = row.get("source_id_remap_sha256", "")
        id_remap_snapshot_changed = bool(
            source_id_remap_hash and source_id_remap_hash != id_remap_sha256
        )

        matches = by_fingerprint.get(fingerprint, [])
        current_crosswalk_row = ""
        current_status = ""
        if len(matches) != 1:
            row_errors.append(
                f"source row fingerprint resolves to {len(matches)} current crosswalk rows"
            )
            source_index = -1
        else:
            source_index = matches[0]
            current_crosswalk_row = str(source_index + 2)
            current_report_row = report_by_fingerprint.get(fingerprint, {})
            current_status = _clean(current_report_row.get("status")).upper()
            if current_status not in BLOCKING_STATUSES:
                row_errors.append(
                    f"source row is no longer pending adjudication "
                    f"(current status={current_status or 'missing'})"
                )

        if decision in {"REPLACE_CROSSWALK_UID", "RECORD_ID_REMAP"}:
            if selected_uid not in cards:
                row_errors.append(f"selected target is not an active card: {selected_uid!r}")
            if not row.get("note"):
                row_errors.append("an evidence note is required")
        if decision == "REPLACE_CROSSWALK_UID" and not row.get("registry_name"):
            row_errors.append("REPLACE_CROSSWALK_UID requires registry_name")
        if decision == "RECORD_ID_REMAP":
            if not original_uid:
                row_errors.append("RECORD_ID_REMAP requires original_repo_uid")
            elif original_uid in cards:
                row_errors.append(
                    "RECORD_ID_REMAP cannot retire a UID that is still an active card"
                )
            elif original_uid == selected_uid:
                row_errors.append("old and new UID cannot be identical")
            prior_target = remap_targets.get(original_uid)
            if prior_target and prior_target != selected_uid:
                row_errors.append(
                    f"conflicting remap targets for {original_uid}: {prior_target}, {selected_uid}"
                )
            remap_targets[original_uid] = selected_uid
        if decision == "DEFER" and current_status and current_status != "STALE":
            row_errors.append(
                "DEFER is permitted only for a structurally valid STALE target; "
                "CONFLICT and INVALID_ROW findings must be corrected"
            )

        if row_errors:
            errors.extend(f"{review_key or '[missing key]'}: {error}" for error in row_errors)

        planned.append(
            {
                "review_key": review_key,
                "source_crosswalk_row": source_crosswalk_row,
                "current_crosswalk_row": current_crosswalk_row,
                "row_relocated": str(
                    bool(
                        source_crosswalk_row
                        and current_crosswalk_row
                        and source_crosswalk_row != current_crosswalk_row
                    )
                ).lower(),
                "csv_id": row.get("csv_id", ""),
                "registry_name": row.get("registry_name", ""),
                "original_repo_uid": original_uid,
                "decision": decision,
                "selected_repo_uid": selected_uid,
                "reviewer": row.get("reviewer", ""),
                "note": row.get("note", ""),
                "source_index": str(source_index),
                "source_row_fingerprint": fingerprint,
                "source_crosswalk_sha256": source_crosswalk_hash,
                "current_crosswalk_sha256": crosswalk_sha256,
                "crosswalk_snapshot_rebased": str(crosswalk_snapshot_changed).lower(),
                "source_id_remap_sha256": source_id_remap_hash,
                "current_id_remap_sha256": id_remap_sha256,
                "id_remap_snapshot_changed": str(id_remap_snapshot_changed).lower(),
                "current_status": current_status,
                "validation": "FAIL" if row_errors else "PASS",
                "validation_detail": "; ".join(row_errors),
            }
        )

    return pd.DataFrame(planned), errors


def _update_adjudication_receipts(
    frame: pd.DataFrame,
    statuses: Mapping[str, str],
    *,
    applied_at: str,
    run_id: str,
) -> pd.DataFrame:
    result = frame.copy()
    for column in RECEIPT_COLUMNS:
        if column not in result.columns:
            result[column] = ""
    for index, row in result.iterrows():
        review_key = _clean(row.get("review_key"))
        if review_key not in statuses:
            continue
        result.at[index, "applied_at"] = applied_at
        result.at[index, "applied_run_id"] = run_id
        result.at[index, "apply_status"] = statuses[review_key]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--adjudications",
        default="crosswalk_adjudications.csv",
        help="CSV under inputs/ unless an absolute path is supplied",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    adjudications_path = Path(args.adjudications)
    if not adjudications_path.is_absolute():
        adjudications_path = Path(INP) / adjudications_path
    crosswalk_path = Path(INP) / "crosswalk.csv"

    adjudications = _read_csv(adjudications_path)
    missing_columns = sorted(REQUIRED_COLUMNS.difference(adjudications.columns))
    explicit_for_source = _explicit_rows(adjudications) if not missing_columns else pd.DataFrame()
    registry_files = sorted(
        {
            _clean(value)
            for value in explicit_for_source.get(
                "source_registry_file", pd.Series(dtype=str)
            ).tolist()
            if _clean(value)
        }
    )
    registry_path = None
    registry_source_error = ""
    if len(registry_files) > 1:
        registry_source_error = f"multiple registry snapshots in one apply batch: {registry_files}"
    elif len(registry_files) == 1:
        candidate_name = registry_files[0]
        if Path(candidate_name).name != candidate_name:
            registry_source_error = f"unsafe source_registry_file: {candidate_name!r}"
        else:
            registry_path = Path(INP) / candidate_name
    elif len(explicit_for_source):
        registry_source_error = "explicit decisions do not identify a registry snapshot"

    crosswalk = _read_csv(crosswalk_path)
    crosswalk_required = {"registry_name", "repo_uid"}
    missing_crosswalk_columns = sorted(crosswalk_required.difference(crosswalk.columns))
    cards = all_cards()
    crosswalk_hash = sha256_file(crosswalk_path) or ""
    id_remap_hash = sha256_file(ID_REMAP_CSV) or ""
    adjudications_hash = sha256_file(adjudications_path) or ""
    registry = pd.DataFrame()
    registry_hash = ""
    registry_names: list[str] = []
    if registry_path is not None:
        if not registry_path.is_file():
            registry_source_error = f"registry snapshot not found: {registry_path}"
        else:
            registry = _read_csv(registry_path)
            if "Repository_Name" not in registry.columns:
                registry_source_error = (
                    f"registry snapshot lacks Repository_Name: {list(registry.columns)}"
                )
            else:
                registry_names = registry["Repository_Name"].tolist()
                registry_hash = sha256_file(registry_path) or ""

    plan = pd.DataFrame()
    validation_errors: list[str] = []
    if not missing_columns and not missing_crosswalk_columns and not registry_source_error:
        plan, validation_errors = _plan(
            adjudications,
            crosswalk,
            cards,
            crosswalk_sha256=crosswalk_hash,
            registry_file=registry_path.name if registry_path is not None else "",
            registry_names=registry_names,
            registry_sha256=registry_hash,
            id_remap_sha256=id_remap_hash,
        )
    elif registry_source_error:
        validation_errors.append(registry_source_error)

    explicit_count = len(plan)
    checks = {
        "adjudication schema": (
            not missing_columns,
            "ok" if not missing_columns else f"missing={missing_columns}",
        ),
        "crosswalk schema": (
            not missing_crosswalk_columns,
            "ok" if not missing_crosswalk_columns else f"missing={missing_crosswalk_columns}",
        ),
        "registry snapshot": (
            not registry_source_error,
            str(registry_path) if registry_path is not None and not registry_source_error else (registry_source_error or "no explicit decisions"),
        ),
        "active cards loaded": (len(cards) > 0, f"{len(cards)} cards"),
        "explicit decisions parsed": (True, f"{explicit_count} pending application"),
        "source and decision validation": (
            not validation_errors,
            "all pass" if not validation_errors else f"{len(validation_errors)} error(s)",
        ),
    }
    ok = gate(checks)
    run_dir = new_run_dir("apply_crosswalk_adjudications")
    plan_path = Path(run_dir) / "crosswalk_adjudication_plan.csv"
    plan.to_csv(plan_path, index=False)

    if validation_errors:
        print("\n  BLOCKING VALIDATION ERRORS:")
        for error in validation_errors:
            print(f"   - {error}")
    if explicit_count:
        print("\n  DECISION PLAN:")
        print(
            plan[
                [
                    "source_crosswalk_row",
                    "current_crosswalk_row",
                    "registry_name",
                    "original_repo_uid",
                    "decision",
                    "selected_repo_uid",
                    "crosswalk_snapshot_rebased",
                    "validation",
                ]
            ].to_string(index=False)
        )
        source_hash_changed = plan["crosswalk_snapshot_rebased"] == "true"
        row_relocated = plan["row_relocated"] == "true"
        validated = plan["validation"] == "PASS"
        validated_rebased = int((validated & source_hash_changed).sum())
        validated_relocated = int((validated & row_relocated).sum())
        changed_snapshot_rows = int(source_hash_changed.sum())
        if validated_rebased or validated_relocated:
            print(
                "\n  ROW-BOUND REBASE: "
                f"{validated_rebased} validated decision(s) came from an older "
                "whole-file crosswalk hash; "
                f"{validated_relocated} validated row(s) moved. Their exact row "
                "fingerprints still match, so unrelated source changes do not "
                "invalidate them."
            )
        elif changed_snapshot_rows:
            print(
                "\n  ROW-BOUND REBASE NOT ESTABLISHED: the whole-file crosswalk "
                "hash changed, and the row-level validation results above must pass "
                "before any decision can be applied."
            )
    else:
        print("\n  No unapplied explicit crosswalk decisions were found; valid no-op.")

    applied = 0
    deferred = 0
    if args.apply and ok and explicit_count:
        with card_transaction(
            "apply_crosswalk_adjudications",
            auto_commit=False,
        ) as transaction:
            transaction.acquire_auxiliary_lock(
                f"{crosswalk_path}.lock",
                purpose="crosswalk adjudication apply",
                timeout=5,
            )
            transaction.acquire_auxiliary_lock(
                f"{adjudications_path}.lock",
                purpose="crosswalk adjudication receipt",
                timeout=5,
            )
            transaction.acquire_auxiliary_lock(
                f"{ID_REMAP_CSV}.lock",
                purpose="crosswalk adjudication id_remap snapshot",
                timeout=5,
            )
            if registry_path is not None:
                transaction.acquire_auxiliary_lock(
                    f"{registry_path}.lock",
                    purpose="crosswalk adjudication registry snapshot",
                    timeout=5,
                )
            if (sha256_file(crosswalk_path) or "") != crosswalk_hash:
                raise RuntimeError("crosswalk.csv changed after planning; rerun dry run")
            if (sha256_file(adjudications_path) or "") != adjudications_hash:
                raise RuntimeError(
                    "crosswalk_adjudications.csv changed after planning; rerun dry run"
                )
            if (sha256_file(ID_REMAP_CSV) or "") != id_remap_hash:
                raise RuntimeError("id_remap.csv changed after planning; rerun dry run")
            if registry_path is not None and (sha256_file(registry_path) or "") != registry_hash:
                raise RuntimeError("registry snapshot changed after planning; rerun dry run")

            locked_crosswalk = _read_csv(crosswalk_path)
            locked_adjudications = _read_csv(adjudications_path)
            locked_fingerprints = _crosswalk_fingerprints(locked_crosswalk)
            statuses: dict[str, str] = {}
            changed_crosswalk = False

            for row in plan.to_dict("records"):
                review_key = _clean(row.get("review_key"))
                decision = _clean(row.get("decision")).upper()
                fingerprint = _clean(row.get("source_row_fingerprint"))
                matches = [
                    index
                    for index, value in enumerate(locked_fingerprints)
                    if value == fingerprint
                ]
                if len(matches) != 1:
                    raise RuntimeError(
                        f"crosswalk source drift under lock for {review_key}: {len(matches)} matches"
                    )
                source_index = matches[0]
                selected_uid = _clean(row.get("selected_repo_uid"))
                original_uid = _clean(row.get("original_repo_uid"))

                if decision == "REPLACE_CROSSWALK_UID":
                    locked_crosswalk.at[source_index, "repo_uid"] = selected_uid
                    changed_crosswalk = True
                    statuses[review_key] = "applied:crosswalk_uid_replaced"
                    applied += 1
                elif decision == "RECORD_ID_REMAP":
                    append_id_remap(
                        original_uid,
                        selected_uid,
                        match_type="crosswalk_adjudicated",
                    )
                    statuses[review_key] = "applied:id_remap_recorded"
                    applied += 1
                elif decision == "DEFER":
                    statuses[review_key] = "deferred:reviewed_nonblocking"
                    deferred += 1
                else:  # protected by planning, retained as a fail-closed assertion
                    raise RuntimeError(f"unsupported decision under lock: {decision}")

            if changed_crosswalk:
                transaction.write_text(
                    crosswalk_path,
                    locked_crosswalk.to_csv(index=False),
                    operation="apply_crosswalk_uid_replacements",
                )

            applied_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            receipted = _update_adjudication_receipts(
                locked_adjudications,
                statuses,
                applied_at=applied_at,
                run_id=current_run_id(),
            )
            transaction.write_text(
                adjudications_path,
                receipted.to_csv(index=False),
                operation="write_crosswalk_adjudication_receipts",
            )

            # Reclassify the exact source rows after all coupled changes. Every
            # non-deferred decision must now resolve as ACTIVE or REMAPPED.
            _, after_report = load_crosswalk_targets(
                cards,
                uid_remap=load_uid_remap(),
                crosswalk_path=crosswalk_path,
                registry_names=registry_names,
                registry_file=registry_path.name if registry_path is not None else "",
                registry_sha256=registry_hash,
            )
            after_by_line = {
                _clean(row.get("crosswalk_row")): _clean(row.get("status")).upper()
                for row in after_report.to_dict("records")
            }
            unresolved_after: list[str] = []
            for row in plan.to_dict("records"):
                if _clean(row.get("decision")).upper() == "DEFER":
                    continue
                line = _clean(row.get("current_crosswalk_row"))
                status = after_by_line.get(line, "")
                if status not in {"ACTIVE", "REMAPPED"}:
                    unresolved_after.append(
                        f"row {line} remains {status or 'missing'} after adjudication"
                    )
            if unresolved_after:
                transaction.rollback(error="; ".join(unresolved_after))
                raise RuntimeError(
                    "Post-apply crosswalk validation failed: " + "; ".join(unresolved_after)
                )
            transaction.commit()
        print(
            f"\n  Applied {applied} source correction/remap decision(s); "
            f"recorded {deferred} reviewed nonblocking deferral(s)."
        )
    elif args.apply and not ok:
        print("\n  Apply refused because the sanity gate failed.")
    elif args.apply and not explicit_count:
        print("\n  Apply requested, but there were no unapplied explicit decisions.")
    else:
        print("\n  DRY RUN - no crosswalk or remap file was changed.")

    ledger_append(
        "apply_crosswalk_adjudications",
        params={"adjudications": str(adjudications_path), "apply": args.apply},
        inputs={
            "adjudications": adjudications_path,
            "crosswalk": crosswalk_path,
            **({"registry": registry_path} if registry_path is not None else {}),
            **({"id_remap": ID_REMAP_CSV} if Path(ID_REMAP_CSV).is_file() else {}),
        },
        outputs={
            "explicit": explicit_count,
            "applied": applied,
            "deferred": deferred,
            "validation_errors": len(validation_errors),
            "plan": str(plan_path),
        },
        gate_passed=ok,
        run_dir=run_dir,
    )
    print(f"\nCrosswalk adjudication run: {run_dir}")
    if not ok:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
