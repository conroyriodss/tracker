"""Compatibility command to plan or remove legacy dev and staging directories.

The v19.0.2 maintenance notebook no longer exposes this completed migration.
The command remains production-only and fail-closed for forensic or delayed upgrades.  Dry run creates a sealed plan
under the production run history.  Apply accepts only that exact plan and the
fixed confirmation token ``DELETE_DEV_STAGING_ENVIRONMENTS``.  The production
root and ``NIH_Tracker_shared`` cache are never deletion candidates.
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

from common import (
    ENV,
    PROJECT,
    FileLock,
    atomic_write_bytes,
    atomic_write_json,
    current_run_id,
    ledger_append,
    new_run_dir,
    sha256_file,
)

SCHEMA = "nih-tracker-remove-nonproduction/v1"
CONFIRM = "DELETE_DEV_STAGING_ENVIRONMENTS"
PLAN_NAME = "nonproduction_deletion_plan.json"
SEAL_NAME = "nonproduction_deletion_plan.sha256"
RECEIPT_NAME = "nonproduction_deletion_receipt.json"
LEGACY_NAMES = ("NIH_Tracker_dev", "NIH_Tracker_staging")


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_drive_root(value: str | None = None) -> Path:
    root = Path(value or "/content/drive/MyDrive").expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Drive root does not exist: {root}")
    return root


def _assert_target(path: Path, drive_root: Path) -> None:
    resolved_parent = path.parent.resolve()
    if resolved_parent != drive_root:
        raise RuntimeError(f"Deletion target is not an immediate child of {drive_root}: {path}")
    if path.name not in LEGACY_NAMES:
        raise RuntimeError(f"Unexpected deletion target name: {path.name!r}")
    production = (drive_root / "NIH_Tracker").resolve()
    shared = (drive_root / "NIH_Tracker_shared").resolve()
    resolved = path.resolve(strict=False)
    if resolved in {production, shared}:
        raise RuntimeError(f"Protected path cannot be deleted: {path}")
    if path.is_symlink():
        raise RuntimeError(f"Refusing symlink deletion target: {path}")


def _tree_state(path: Path, drive_root: Path) -> dict[str, Any]:
    _assert_target(path, drive_root)
    if not path.exists():
        return {
            "path": str(path),
            "name": path.name,
            "exists": False,
            "files": 0,
            "directories": 0,
            "bytes": 0,
            "tree_fingerprint": "",
            "tree_fingerprint_basis": "relative_path,size,mtime_ns; file contents not hashed",
            "locks": [],
            "symlinks": [],
        }
    if not path.is_dir():
        raise RuntimeError(f"Legacy environment target is not a directory: {path}")
    records: list[str] = []
    files = 0
    directories = 1
    total_bytes = 0
    locks: list[str] = []
    symlinks: list[str] = []
    for root_text, dir_names, file_names in os.walk(path, topdown=True, followlinks=False):
        root = Path(root_text)
        for name in list(dir_names):
            child = root / name
            relative = child.relative_to(path).as_posix()
            if child.is_symlink():
                symlinks.append(relative)
                dir_names.remove(name)
                continue
            stat = child.stat()
            directories += 1
            records.append(f"d\t{relative}\t{stat.st_mtime_ns}")
        for name in file_names:
            child = root / name
            relative = child.relative_to(path).as_posix()
            if child.is_symlink():
                symlinks.append(relative)
                continue
            stat = child.stat()
            files += 1
            total_bytes += stat.st_size
            records.append(f"f\t{relative}\t{stat.st_size}\t{stat.st_mtime_ns}")
            if name == ".lock" or name.endswith(".lock"):
                locks.append(relative)
    records.sort()
    fingerprint = _sha256_bytes(("\n".join(records) + "\n").encode("utf-8"))
    return {
        "path": str(path),
        "name": path.name,
        "exists": True,
        "files": files,
        "directories": directories,
        "bytes": total_bytes,
        "tree_fingerprint": fingerprint,
        "tree_fingerprint_basis": "relative_path,size,mtime_ns; file contents not hashed",
        "locks": sorted(locks),
        "symlinks": sorted(symlinks),
    }


def _seal(plan_path: Path) -> str:
    digest = sha256_file(plan_path) or ""
    atomic_write_bytes(plan_path.parent / SEAL_NAME, f"{digest}  {plan_path.name}\n".encode())
    return digest


def create_plan(*, drive_root: Path | None = None) -> Path:
    if ENV != "prod":
        raise RuntimeError("Legacy environment removal must run from production.")
    drive_root = _safe_drive_root(str(drive_root) if drive_root else None)
    production = Path(PROJECT).expanduser().resolve()
    if production != (drive_root / "NIH_Tracker").resolve():
        raise RuntimeError(
            f"Production root must be {drive_root / 'NIH_Tracker'}; got {production}"
        )
    targets = [_tree_state(drive_root / name, drive_root) for name in LEGACY_NAMES]
    existing_targets = [target for target in targets if target["exists"]]
    blockers = []
    for target in targets:
        blockers.extend(f"{target['name']}: active/unknown lock {item}" for item in target["locks"])
        blockers.extend(f"{target['name']}: symlink {item}" for item in target["symlinks"])
    run_dir = Path(new_run_dir("remove_nonproduction_environments"))
    plan = {
        "schema": SCHEMA,
        "created_at": _utc_now(),
        "created_run_id": current_run_id(),
        "production_root": str(production),
        "drive_root": str(drive_root),
        "protected_paths": [str(production), str((drive_root / "NIH_Tracker_shared").resolve())],
        "targets": targets,
        "existing_target_count": len(existing_targets),
        "requires_apply": bool(existing_targets),
        "blockers": blockers,
        "confirmation": CONFIRM,
    }
    plan_path = run_dir / PLAN_NAME
    atomic_write_json(plan_path, plan)
    digest = _seal(plan_path)
    print(f"Nonproduction deletion plan: {plan_path}")
    print(f"  plan_sha256={digest}")
    for target in targets:
        print(
            f"  {target['name']}: exists={target['exists']} | files={target['files']:,} | "
            f"directories={target['directories']:,} | bytes={target['bytes']:,}"
        )
    if not existing_targets:
        print("Legacy nonproduction environments are already absent; valid no-op.")
        print("No deletion apply step is required.")
    elif blockers:
        print("BLOCKERS:")
        for blocker in blockers:
            print(f"  - {blocker}")
    else:
        print(f"Apply only after review with exact confirmation: {CONFIRM}")
    ledger_append(
        "remove_nonproduction_environments_plan",
        params={"drive_root": str(drive_root)},
        # Never pass the My Drive root as a ledger input.  ledger_append hashes
        # inputs recursively, and Google Docs placeholders are not ordinary
        # readable files on the mounted filesystem.  The sealed plan is the
        # bounded, reproducible input artifact for this operation.
        inputs={"plan": str(plan_path)},
        outputs={
            "plan": str(plan_path),
            "plan_sha256": digest,
            "targets": len(targets),
            "existing_targets": len(existing_targets),
            "requires_apply": bool(existing_targets),
        },
        gate_passed=not blockers,
        run_dir=str(run_dir),
    )
    return plan_path


def _load_plan(plan_path: Path) -> dict[str, Any]:
    plan_path = plan_path.expanduser().resolve()
    seal_path = plan_path.parent / SEAL_NAME
    if not plan_path.is_file() or not seal_path.is_file():
        raise FileNotFoundError(f"Plan or seal missing: {plan_path}")
    parts = seal_path.read_text(encoding="utf-8").strip().split()
    expected = parts[0] if parts else ""
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RuntimeError(f"Invalid plan seal: {seal_path}")
    actual = sha256_file(plan_path) or ""
    if actual != expected:
        raise RuntimeError(f"Plan seal mismatch: expected {expected}, got {actual}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema") != SCHEMA:
        raise RuntimeError(f"Unsupported plan schema: {plan.get('schema')!r}")
    return plan


def apply_plan(plan_path: Path, *, confirm: str) -> dict[str, Any]:
    if confirm != CONFIRM:
        raise RuntimeError(f"Exact confirmation required: {CONFIRM}")
    if ENV != "prod":
        raise RuntimeError("Legacy environment removal must run from production.")
    plan_path = plan_path.expanduser().resolve()
    plan = _load_plan(plan_path)
    if plan.get("blockers"):
        raise RuntimeError("Plan contains blockers; create a new plan after resolving them.")
    drive_root = _safe_drive_root(str(plan.get("drive_root", "")))
    production = Path(PROJECT).expanduser().resolve()
    if production != Path(str(plan.get("production_root", ""))).resolve():
        raise RuntimeError("Production root changed after planning.")
    try:
        plan_path.relative_to(production / "runs" / "remove_nonproduction_environments")
    except ValueError as exc:
        raise RuntimeError("Deletion plan is outside the production run directory.") from exc

    current_states = []
    for prior in plan.get("targets", []):
        target = drive_root / str(prior.get("name", ""))
        current = _tree_state(target, drive_root)
        current_states.append(current)
        if bool(current["exists"]) != bool(prior.get("exists")):
            raise RuntimeError(f"Target existence changed after planning: {target}")
        if current["tree_fingerprint"] != str(prior.get("tree_fingerprint", "")):
            raise RuntimeError(f"Target changed after planning: {target}")
        if current["locks"] or current["symlinks"]:
            raise RuntimeError(f"Target now contains a lock or symlink: {target}")

    removed = []
    lock_path = production / ".remove_nonproduction_environments.lock"
    with FileLock(lock_path, purpose="remove legacy dev and staging roots", timeout=0):
        for current in current_states:
            target = Path(current["path"])
            if not current["exists"]:
                removed.append({**current, "status": "already_absent"})
                continue
            tombstone = drive_root / f".{target.name}.deleting.{uuid.uuid4().hex}"
            os.replace(target, tombstone)
            try:
                shutil.rmtree(tombstone)
            except Exception:
                if tombstone.exists() and not target.exists():
                    os.replace(tombstone, target)
                raise
            removed.append({**current, "status": "deleted"})

    receipt = {
        "schema": SCHEMA,
        "status": "committed",
        "applied_at": _utc_now(),
        "plan": str(plan_path),
        "removed": removed,
        "production_root_preserved": str(production),
        "shared_cache_preserved": str((drive_root / "NIH_Tracker_shared").resolve()),
    }
    receipt_path = plan_path.parent / RECEIPT_NAME
    atomic_write_json(receipt_path, receipt)
    print("Legacy nonproduction environments removed.")
    for row in removed:
        print(f"  {row['name']}: {row['status']}")
    print(f"Production preserved: {production}")
    print(f"Shared cache preserved: {receipt['shared_cache_preserved']}")
    ledger_append(
        "remove_nonproduction_environments_apply",
        params={"confirm": CONFIRM},
        inputs={"plan": str(plan_path), "plan_sha256": sha256_file(plan_path) or ""},
        outputs={"receipt": str(receipt_path), "deleted": sum(r['status'] == 'deleted' for r in removed)},
        gate_passed=True,
        run_dir=str(plan_path.parent),
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drive-root", default="")
    parser.add_argument("--plan", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.apply:
        if not args.plan:
            raise SystemExit("--apply requires --plan")
        apply_plan(Path(args.plan), confirm=args.confirm)
    else:
        create_plan(drive_root=Path(args.drive_root).expanduser() if args.drive_root else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
