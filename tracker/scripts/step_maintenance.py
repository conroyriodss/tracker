"""Inspect and maintain one NIH Tracker environment.

The command centralizes low-frequency operational work that should not appear in
the pipeline notebook: lock inspection/removal, bounded cleanup of old run and
debug folders, optional card-snapshot pruning, and archive-backed card reset.
Destructive operations are dry-run by default and require exact confirmations.
Cleanup uses a sealed plan created in a prior invocation; ``--apply`` never
silently recalculates what will be deleted.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any
from run_provenance import archive_candidates, protection_reason, retention_report

from common import (
    BUILD,
    CARDS,
    ENV,
    INP,
    LATEST,
    PROJECT,
    RUNS,
    SNAPSHOTS,
    PROD_WRITE_CONFIRMATION_TOKEN,
    FileLock,
    atomic_write_bytes,
    atomic_write_json,
    card_lock_status,
    card_transaction,
    ledger_append,
    new_run_dir,
    remove_card_lock,
    run_ts,
    sha256_file,
    snapshot_cards_zip,
)

CLEANUP_SCHEMA = "nih-tracker-maintenance-cleanup/v1"
CLEANUP_CONFIRMATION = "APPLY_MAINTENANCE_CLEANUP"
CLEANUP_PLAN_SEAL = "cleanup_plan.sha256"
_TEMP_RE = re.compile(r"(?:^\.tmp(?:[-.]|$)|\.tmp\.|\.old\.)")


def _directory_size(path: Path) -> int:
    total = 0
    if path.is_file() and not path.is_symlink():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    for root, directories, files in os.walk(path, followlinks=False):
        directories.sort()
        files.sort()
        for name in files:
            candidate = Path(root) / name
            if candidate.is_symlink():
                continue
            try:
                total += candidate.stat().st_size
            except OSError:
                pass
    return total


def _path_fingerprint(path: Path) -> str:
    """Hash a candidate's path, metadata, links, and regular-file contents."""
    path = path.resolve()
    if not path.exists():
        return ""
    digest = hashlib.sha256()

    def add_record(kind: str, relative: str, candidate: Path) -> None:
        stat_result = candidate.lstat()
        digest.update(
            f"{kind}\0{relative}\0{stat_result.st_mode}\0{stat_result.st_size}\0"
            f"{stat_result.st_mtime_ns}\0".encode("utf-8")
        )
        if candidate.is_symlink():
            digest.update(os.readlink(candidate).encode("utf-8", errors="surrogateescape"))
        elif candidate.is_file():
            file_hash = sha256_file(candidate) or ""
            digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")

    if path.is_symlink() or path.is_file():
        add_record("link" if path.is_symlink() else "file", path.name, path)
        return digest.hexdigest()

    add_record("directory", ".", path)
    for root, directories, files in os.walk(path, followlinks=False):
        directories.sort()
        files.sort()
        root_path = Path(root)
        for name in directories:
            candidate = root_path / name
            relative = candidate.relative_to(path).as_posix()
            add_record("link" if candidate.is_symlink() else "directory", relative, candidate)
        for name in files:
            candidate = root_path / name
            relative = candidate.relative_to(path).as_posix()
            add_record("link" if candidate.is_symlink() else "file", relative, candidate)
    return digest.hexdigest()


def _candidate_record(kind: str, path: Path) -> dict[str, Any]:
    path = path.resolve()
    stat_result = path.stat()
    return {
        "kind": kind,
        "path": str(path),
        "size_bytes": _directory_size(path),
        "modified_at": dt.datetime.fromtimestamp(
            stat_result.st_mtime, tz=dt.timezone.utc
        ).isoformat(timespec="seconds"),
        "fingerprint": _path_fingerprint(path),
    }


def status() -> dict[str, Any]:
    project = Path(PROJECT)
    report = {
        "schema": "nih-tracker-maintenance-status/v1",
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "environment": ENV,
        "project": str(project),
        "card_lock": card_lock_status(),
        "counts": {
            "cards": len(list(Path(CARDS).glob("*.yaml")))
            + len(list(Path(CARDS).glob("*.yml"))),
            "inputs": len(list(Path(INP).iterdir())) if Path(INP).is_dir() else 0,
            "run_steps": len([path for path in Path(RUNS).iterdir() if path.is_dir()])
            if Path(RUNS).is_dir()
            else 0,
            "card_snapshots": len(
                list((Path(SNAPSHOTS) / "cards").glob("*/manifest.json"))
            ),
        },
        "sizes_bytes": {
            "cards": _directory_size(Path(CARDS)),
            "inputs": _directory_size(Path(INP)),
            "runs": _directory_size(Path(RUNS)),
            "latest": _directory_size(Path(LATEST)),
            "build": _directory_size(Path(BUILD)),
            "snapshots": _directory_size(Path(SNAPSHOTS)),
        },
    }
    print(json.dumps(report, indent=2, default=str))
    return report


def _active_debug_directory() -> Path | None:
    value = os.getenv("NIH_TRACKER_NOTEBOOK_DEBUG_DIR", "").strip()
    if not value:
        return None
    try:
        path = Path(value).expanduser().resolve()
        path.relative_to((Path(RUNS) / "notebook_debug").resolve())
        return path
    except (OSError, ValueError):
        return None


def _run_directory_candidates(keep_runs: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    root = Path(RUNS)
    active_debug = _active_debug_directory()
    if not root.is_dir():
        return candidates

    def add_older_sessions(container: Path) -> None:
        sessions = sorted(
            (
                path
                for path in container.iterdir()
                if path.is_dir() and not path.is_symlink()
            ),
            key=lambda path: (path.stat().st_mtime_ns, path.as_posix()),
            reverse=True,
        )
        retained = 0
        for path in sessions:
            if active_debug is not None and path.resolve() == active_debug:
                continue
            if retained < keep_runs:
                retained += 1
                continue
            candidates.append(_candidate_record("run_directory", path))

    for step_dir in sorted(
        path for path in root.iterdir() if path.is_dir() and not path.is_symlink()
    ):
        if step_dir.name == "notebook_debug":
            for notebook_dir in sorted(
                path
                for path in step_dir.iterdir()
                if path.is_dir() and not path.is_symlink()
            ):
                add_older_sessions(notebook_dir)
        else:
            add_older_sessions(step_dir)
    return candidates


def _snapshot_candidates(keep_snapshots: int) -> list[dict[str, Any]]:
    root = Path(SNAPSHOTS) / "cards"
    if not root.is_dir():
        return []
    snapshots: list[Path] = []
    for directory in root.iterdir():
        manifest = directory / "manifest.json"
        if (
            not directory.is_dir()
            or directory.is_symlink()
            or not manifest.is_file()
            or manifest.is_symlink()
        ):
            continue
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("status") not in {"committed", "rolled_back"}:
            continue
        snapshots.append(directory)
    snapshots.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    return [
        _candidate_record("card_snapshot", path)
        for path in snapshots[max(0, keep_snapshots) :]
    ]


def _is_temporary_name(name: str) -> bool:
    return bool(_TEMP_RE.search(name))


def _temporary_candidates(older_than_hours: int) -> list[dict[str, Any]]:
    cutoff = dt.datetime.now().timestamp() - max(1, older_than_hours) * 3600
    candidates: list[dict[str, Any]] = []
    # latest/ is intentionally excluded in full; it is an operational pointer.
    roots = [Path(CARDS), Path(INP), Path(BUILD), Path(PROJECT) / "scripts"]
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            try:
                if path.is_symlink() or path.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            if not _is_temporary_name(path.name):
                continue
            if path.name == ".lock" or path.name.endswith(".lock"):
                continue
            candidates.append(_candidate_record("temporary_path", path))

    # Keep the shallowest directory candidate and remove nested duplicates.
    ordered = sorted(candidates, key=lambda row: len(Path(row["path"]).parts))
    selected: list[dict[str, Any]] = []
    selected_dirs: list[Path] = []
    for row in ordered:
        path = Path(row["path"])
        if any(parent == path or parent in path.parents for parent in selected_dirs):
            continue
        selected.append(row)
        if path.is_dir():
            selected_dirs.append(path)
    return sorted(selected, key=lambda row: len(Path(row["path"]).parts), reverse=True)


def _validate_candidate_scope(path: Path, kind: str) -> None:
    project = Path(PROJECT).resolve()
    path = path.expanduser().resolve()
    if path == project:
        raise ValueError("Cleanup candidate cannot be the project root")
    try:
        path.relative_to(project)
    except ValueError as exc:
        raise ValueError(f"Cleanup candidate is outside the project root: {path}") from exc
    if path.name == ".lock" or path.name.endswith(".lock"):
        raise ValueError(f"Lock files are protected: {path}")
    latest = Path(LATEST).resolve()
    if path == latest or latest in path.parents:
        raise ValueError(f"latest/ is protected: {path}")

    if kind == "run_directory":
        relative = path.relative_to(Path(RUNS).resolve())
        valid = (
            len(relative.parts) == 2 and relative.parts[0] != "notebook_debug"
        ) or (
            len(relative.parts) == 3 and relative.parts[0] == "notebook_debug"
        )
        if not valid:
            raise ValueError(f"Run cleanup candidate is not a session directory: {path}")
        active_debug = _active_debug_directory()
        if active_debug is not None and path == active_debug:
            raise ValueError(f"Active notebook debug directory is protected: {path}")
        return

    if kind == "card_snapshot":
        relative = path.relative_to((Path(SNAPSHOTS) / "cards").resolve())
        if len(relative.parts) != 1:
            raise ValueError(f"Snapshot cleanup candidate is not one transaction: {path}")
        if path.exists():
            manifest = path / "manifest.json"
            if not manifest.is_file():
                raise ValueError(f"Snapshot candidate has no manifest: {path}")
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            if payload.get("status") not in {"committed", "rolled_back"}:
                raise ValueError(f"Snapshot candidate status is not final: {path}")
        return

    if kind == "temporary_path":
        allowed_roots = [
            Path(CARDS).resolve(),
            Path(INP).resolve(),
            Path(BUILD).resolve(),
            (Path(PROJECT) / "scripts").resolve(),
        ]
        if not any(path != root and root in path.parents for root in allowed_roots):
            raise ValueError(f"Temporary cleanup candidate is outside an allowed root: {path}")
        if not _is_temporary_name(path.name):
            raise ValueError(f"Temporary cleanup candidate has a non-temporary name: {path}")
        return

    raise ValueError(f"Unknown cleanup candidate kind: {kind!r}")


def build_cleanup_plan(
    *,
    keep_runs: int,
    prune_card_snapshots: bool,
    keep_snapshots: int,
    remove_temporary: bool,
    temp_older_hours: int,
) -> tuple[Path, dict[str, Any]]:
    if keep_runs < 0 or keep_snapshots < 0:
        raise ValueError("Retention counts must be zero or greater")
    candidates = _run_directory_candidates(keep_runs)
    if prune_card_snapshots:
        candidates.extend(_snapshot_candidates(keep_snapshots))
    if remove_temporary:
        candidates.extend(_temporary_candidates(temp_older_hours))

    retention = retention_report(Path(PROJECT))
    if retention["errors"]:
        raise RuntimeError(f"Dependency scan incomplete; cleanup not planned: {retention['errors']}")
    protected_candidates = []
    eligible = []
    for row in candidates:
        reasons = protection_reason(Path(row["path"]), retention)
        if reasons:
            protected_candidates.append({**row, "retention_reasons": reasons})
        else:
            eligible.append(row)
    candidates = eligible

    seen: set[str] = set()
    for row in candidates:
        path = str(Path(row["path"]).resolve())
        if path in seen:
            raise RuntimeError(f"Cleanup planner produced a duplicate path: {path}")
        seen.add(path)
        _validate_candidate_scope(Path(path), str(row["kind"]))

    run_dir = Path(new_run_dir("maintenance"))
    plan = {
        "schema": CLEANUP_SCHEMA,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "environment": ENV,
        "project": str(Path(PROJECT).resolve()),
        "policy": {
            "keep_runs_per_step": keep_runs,
            "prune_card_snapshots": prune_card_snapshots,
            "keep_card_snapshots": keep_snapshots,
            "remove_temporary": remove_temporary,
            "temporary_older_than_hours": temp_older_hours,
            "latest_never_deleted": True,
            "apply_requires_existing_sealed_plan": True,
            "verified_archive_before_removal": True,
            "dependency_check_at_plan_and_apply": True,
            "card_transaction_history_retained": True,
        },
        "protected_candidates": protected_candidates,
        "protected_candidate_count": len(protected_candidates),
        "candidate_count": len(candidates),
        "candidate_bytes": sum(int(row["size_bytes"]) for row in candidates),
        "candidates": candidates,
    }
    plan_path = run_dir / "cleanup_plan.json"
    atomic_write_json(plan_path, plan)
    plan_sha256 = sha256_file(plan_path) or ""
    atomic_write_bytes(
        run_dir / CLEANUP_PLAN_SEAL,
        f"{plan_sha256}  {plan_path.name}\n".encode("utf-8"),
    )
    print(f"Cleanup plan: {plan_path}")
    print(f"Plan SHA-256: {plan_sha256}")
    print(f"Retained for provenance: {len(protected_candidates)} candidate(s)")
    print(
        f"Candidates: {plan['candidate_count']} | "
        f"bytes: {plan['candidate_bytes']:,} | latest/ preserved"
    )
    for row in candidates[:250]:
        print(f"  {row['kind']:<18} {row['size_bytes']:>10,}  {row['path']}")
    if len(candidates) > 250:
        print(f"  ... {len(candidates) - 250} additional candidate(s) in the plan JSON")
    return plan_path, plan


def load_cleanup_plan(plan_path: Path) -> dict[str, Any]:
    plan_path = plan_path.expanduser().resolve()
    if not plan_path.is_file():
        raise FileNotFoundError(plan_path)
    seal_path = plan_path.parent / CLEANUP_PLAN_SEAL
    if not seal_path.is_file():
        raise FileNotFoundError(f"Missing cleanup plan seal: {seal_path}")
    parts = seal_path.read_text(encoding="utf-8").strip().split()
    expected = parts[0] if parts else ""
    actual = sha256_file(plan_path) or ""
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or expected != actual:
        raise ValueError(
            f"Cleanup plan seal mismatch: expected={expected!r}, actual={actual!r}"
        )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema") != CLEANUP_SCHEMA:
        raise ValueError(f"Unsupported cleanup plan: {plan.get('schema')!r}")
    if plan.get("environment") != ENV:
        raise RuntimeError(
            f"Cleanup plan environment is {plan.get('environment')!r}; active environment is {ENV!r}"
        )
    if Path(str(plan.get("project", ""))).expanduser().resolve() != Path(PROJECT).resolve():
        raise RuntimeError("Cleanup plan belongs to a different project root")
    try:
        plan_path.relative_to((Path(RUNS) / "maintenance").resolve())
    except ValueError as exc:
        raise ValueError("Cleanup plan is outside the maintenance run directory") from exc

    policy = plan.get("policy")
    if not isinstance(policy, dict) or policy.get("latest_never_deleted") is not True:
        raise ValueError("Cleanup plan has no safe latest/ preservation policy")
    candidates = plan.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("Cleanup plan candidates must be a list")
    if int(plan.get("candidate_count", -1)) != len(candidates):
        raise ValueError("Cleanup plan candidate_count mismatch")

    seen: set[str] = set()
    planned_bytes = 0
    for index, row in enumerate(candidates):
        if not isinstance(row, dict):
            raise ValueError(f"Cleanup candidate {index} is not an object")
        kind = str(row.get("kind", ""))
        path = Path(str(row.get("path", ""))).expanduser().resolve()
        path_text = str(path)
        if path_text in seen:
            raise ValueError(f"Duplicate cleanup candidate: {path}")
        seen.add(path_text)
        _validate_candidate_scope(path, kind)
        fingerprint = str(row.get("fingerprint", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError(f"Cleanup candidate has invalid fingerprint: {path}")
        try:
            size_bytes = int(row.get("size_bytes", -1))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Cleanup candidate has invalid size: {path}") from exc
        if size_bytes < 0:
            raise ValueError(f"Cleanup candidate has invalid size: {path}")
        planned_bytes += size_bytes
    if int(plan.get("candidate_bytes", -1)) != planned_bytes:
        raise ValueError("Cleanup plan candidate_bytes mismatch")
    return plan


def apply_cleanup(plan_path: Path, *, confirm: str, reviewed_sha256: str = "") -> dict[str, Any]:
    if confirm != CLEANUP_CONFIRMATION:
        raise RuntimeError(f"Exact confirmation required: {CLEANUP_CONFIRMATION}")
    plan_path = plan_path.expanduser().resolve()
    plan = load_cleanup_plan(plan_path)
    if reviewed_sha256 and sha256_file(plan_path) != reviewed_sha256:
        raise RuntimeError("Cleanup plan changed since review; inspect it again. No files were deleted.")
    if card_lock_status():
        raise RuntimeError("cards/.lock is held; maintenance cleanup will not run concurrently")

    deleted: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    with FileLock(
        Path(PROJECT) / ".maintenance.lock",
        purpose="maintenance cleanup",
        timeout=0,
    ), FileLock(
        Path(CARDS) / ".lock",
        purpose="maintenance cleanup exclusion",
        timeout=0,
    ):
        retention = retention_report(Path(PROJECT))
        if retention["errors"]:
            raise RuntimeError("Dependency scan incomplete; no files were deleted")
        deletable: list[tuple[dict[str, Any], Path]] = []
        for row in plan["candidates"]:
            path = Path(row["path"]).expanduser().resolve()
            if not path.exists():
                continue
            try:
                _validate_candidate_scope(path, str(row["kind"]))
                if path == plan_path or path in plan_path.parents:
                    raise RuntimeError("active cleanup plan is protected")
                reasons = protection_reason(path, retention)
                if reasons:
                    raise RuntimeError("retained dependency: " + "; ".join(reasons))
                current_fingerprint = _path_fingerprint(path)
                if current_fingerprint != row["fingerprint"]:
                    raise RuntimeError("candidate changed after cleanup planning")
                deletable.append((row, path))
            except Exception as exc:
                failures.append({"path": str(path), "error": str(exc)})
        if failures:
            result = {
                "schema": plan["schema"],
                "status": "preflight_failed",
                "applied_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "deleted_count": 0,
                "deleted_bytes_planned": 0,
                "failure_count": len(failures),
                "failures": failures,
            }
            atomic_write_json(plan_path.parent / "cleanup_receipt.json", result)
            raise RuntimeError(
                f"Cleanup preflight failed for {len(failures)} candidate(s); no files were deleted"
            )

        archive = None
        if deletable:
            archive = archive_candidates(
                Path(PROJECT).resolve(), [row for row, _ in deletable],
                Path(SNAPSHOTS) / "maintenance" / f"{run_ts()}-{uuid.uuid4().hex[:12]}.zip",
            )
            # Check every source after archiving, before the first removal.
            for row, path in deletable:
                if _path_fingerprint(path) != row["fingerprint"]:
                    raise RuntimeError("Candidate changed during archiving; no files were deleted")
            atomic_write_json(plan_path.parent / "cleanup_archive.json", archive)

        for row, path in deletable:
            try:
                if _path_fingerprint(path) != row["fingerprint"]:
                    raise RuntimeError("Candidate changed immediately before removal")
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                deleted.append(row)
            except Exception as exc:
                failures.append({"path": str(path), "error": str(exc)})

    result = {
        "schema": plan["schema"],
        "status": "committed" if not failures else "partial_failure",
        "applied_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "deleted_count": len(deleted),
        "deleted_bytes_planned": sum(int(row["size_bytes"]) for row in deleted),
        "failure_count": len(failures),
        "failures": failures,
        "archive": archive,
    }
    atomic_write_json(plan_path.parent / "cleanup_receipt.json", result)
    print(json.dumps(result, indent=2))
    if failures:
        raise RuntimeError(f"Cleanup completed with {len(failures)} deletion failure(s)")
    return result


def reset_cards(*, confirm: str) -> dict[str, Any]:
    required = f"ARCHIVE_AND_RESET_{ENV.upper()}_CARDS"
    if confirm != required:
        raise RuntimeError(f"Exact confirmation required: {required}")
    if (
        ENV == "prod"
        and os.getenv("NIH_TRACKER_PROD_WRITE_CONFIRMATION")
        != PROD_WRITE_CONFIRMATION_TOKEN
    ):
        raise RuntimeError("Production reset requires the lower-level production write guard")
    with card_transaction("maintenance_archive_and_reset") as transaction:
        archive = snapshot_cards_zip(label=f"pre_reset_{ENV}")
        files = [
            path
            for path in Path(CARDS).rglob("*")
            if path.is_file()
            and path.name != ".lock"
            and ".tmp" not in path.parts
            and ".tmp." not in path.name
        ]
        for path in files:
            transaction.delete(path, operation="maintenance_reset_delete")
    result = {
        "environment": ENV,
        "archive": str(archive),
        "deleted_files": len(files),
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    ledger_append(
        "maintenance_archive_and_reset",
        params={"environment": ENV},
        inputs={"archive": str(archive)},
        outputs=result,
        gate_passed=True,
        run_dir="",
    )
    print(json.dumps(result, indent=2))
    return result


def _print_plan(plan_path: Path, plan: dict[str, Any]) -> None:
    print(f"Cleanup plan: {plan_path}")
    print(
        f"Candidates: {plan['candidate_count']} | "
        f"bytes: {int(plan['candidate_bytes']):,}"
    )
    for row in plan["candidates"][:250]:
        print(f"  {row['kind']:<18} {int(row['size_bytes']):>10,}  {row['path']}")
    if len(plan["candidates"]) > 250:
        print(f"  ... {len(plan['candidates']) - 250} additional candidate(s) in the plan JSON")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Inspect storage, locks, and retained artifacts.")

    cleanup_parser = subparsers.add_parser("cleanup", help="Plan or apply bounded cleanup.")
    cleanup_parser.add_argument("--keep-runs", type=int, default=5)
    cleanup_parser.add_argument("--prune-card-snapshots", action="store_true")
    cleanup_parser.add_argument("--keep-snapshots", type=int, default=20)
    cleanup_parser.add_argument("--remove-temporary", action="store_true")
    cleanup_parser.add_argument("--temp-older-hours", type=int, default=24)
    cleanup_parser.add_argument(
        "--plan", default="", help="Existing sealed cleanup_plan.json to inspect or apply."
    )
    cleanup_parser.add_argument("--apply", action="store_true")
    cleanup_parser.add_argument("--reviewed-sha256", default="", help="Optional SHA-256 captured during plan inspection.")
    cleanup_parser.add_argument("--confirm", default="")

    lock_parser = subparsers.add_parser(
        "remove-lock", help="Remove a confirmed abandoned card lock."
    )
    lock_parser.add_argument("--apply", action="store_true")
    lock_parser.add_argument("--confirm", default="")

    reset_parser = subparsers.add_parser(
        "reset-cards", help="Archive and reset cards transactionally."
    )
    reset_parser.add_argument("--apply", action="store_true")
    reset_parser.add_argument("--confirm", default="")

    args = parser.parse_args()
    if args.command == "status":
        status()
        return 0

    if args.command == "cleanup":
        if args.plan:
            plan_path = Path(args.plan).expanduser().resolve()
            plan = load_cleanup_plan(plan_path)
            _print_plan(plan_path, plan)
        else:
            if args.apply:
                raise RuntimeError(
                    "Cleanup --apply requires --plan from a prior dry run; no deletion set was recalculated."
                )
            plan_path, plan = build_cleanup_plan(
                keep_runs=args.keep_runs,
                prune_card_snapshots=args.prune_card_snapshots,
                keep_snapshots=args.keep_snapshots,
                remove_temporary=args.remove_temporary,
                temp_older_hours=args.temp_older_hours,
            )

        outputs: dict[str, Any] = {
            "plan": str(plan_path),
            "candidates": plan["candidate_count"],
            "candidate_bytes": plan["candidate_bytes"],
            "applied": False,
        }
        if args.apply:
            result = apply_cleanup(plan_path, confirm=args.confirm, reviewed_sha256=args.reviewed_sha256)
            outputs.update(result)
            outputs["applied"] = True
        else:
            print("DRY RUN: no files were deleted.")
            print(
                "Apply this exact sealed plan with --plan, --apply, and confirmation "
                f"{CLEANUP_CONFIRMATION}."
            )
        ledger_append(
            "maintenance_cleanup",
            params={**plan["policy"], "apply": args.apply},
            inputs={"plan": str(plan_path)},
            outputs=outputs,
            gate_passed=True,
            run_dir=str(plan_path.parent),
        )
        return 0

    if args.command == "remove-lock":
        lock = card_lock_status()
        print(json.dumps(lock or {"card_lock": "not held"}, indent=2, default=str))
        if not args.apply:
            print("DRY RUN: no lock was removed.")
            return 0
        removed = remove_card_lock(args.confirm)
        remaining_lock = card_lock_status()
        succeeded = remaining_lock is None
        outcome = "removed" if removed else "already_unlocked"
        if not succeeded:
            outcome = "lock_still_held"
        print(f"Lock maintenance: {outcome}; removed={removed}")
        ledger_append(
            "maintenance_remove_card_lock",
            params={"apply": True},
            inputs={"prior_lock": lock or {}},
            outputs={"removed": bool(removed), "outcome": outcome, "remaining_lock": remaining_lock},
            gate_passed=succeeded,
            run_dir="",
        )
        return 0 if succeeded else 1

    if args.command == "reset-cards":
        print(json.dumps(card_lock_status() or {"card_lock": "not held"}, indent=2, default=str))
        if not args.apply:
            print("DRY RUN: no cards were reset.")
            print(f"Apply with exact confirmation: ARCHIVE_AND_RESET_{ENV.upper()}_CARDS")
            return 0
        reset_cards(confirm=args.confirm)
        return 0
    raise RuntimeError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
