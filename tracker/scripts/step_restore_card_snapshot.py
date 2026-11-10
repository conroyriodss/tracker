"""Restore every file touched by one committed card transaction.

The restore is optimistic: each current file must still match the transaction's
recorded after-image, so a later legitimate write is never silently overwritten.
The restore itself is a new lock-protected transaction and therefore has its own
before-images and can be reversed in turn.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from common import (
    ENV,
    PROJECT,
    SNAPSHOTS,
    atomic_write_json,
    card_transaction,
    current_run_id,
    ledger_append,
    new_run_dir,
    sha256_file,
)


def _manifest_path(value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    under_snapshots = Path(SNAPSHOTS) / "cards" / value / "manifest.json"
    if under_snapshots.is_file():
        return under_snapshots.resolve()
    raise FileNotFoundError(
        f"No manifest found at {candidate} or for transaction id {value!r} under {Path(SNAPSHOTS) / 'cards'}"
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    required = {"transaction_id", "status", "project_root", "cards_dir", "touched"}
    missing = sorted(required - set(manifest))
    if missing:
        raise ValueError(f"Transaction manifest lacks required fields: {missing}")
    if manifest["status"] != "committed":
        raise ValueError(
            f"Only committed transactions need restoration; status={manifest['status']!r}"
        )
    project_root = Path(manifest["project_root"]).expanduser().resolve()
    if project_root != Path(PROJECT).expanduser().resolve():
        raise RuntimeError(
            f"Manifest project is {project_root}, but active {ENV} project is {Path(PROJECT).resolve()}. "
            "Select the manifest's environment before restoring."
        )
    return manifest


def _preflight(manifest: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    operations: list[dict[str, Any]] = []
    project_root = Path(manifest["project_root"]).expanduser().resolve()
    for record in manifest.get("touched", []):
        path = Path(str(record.get("path", ""))).expanduser().resolve()
        try:
            path.relative_to(project_root)
        except ValueError:
            errors.append(f"path outside project root: {path}")
            continue
        existed_after = bool(record.get("exists_after"))
        current_exists = path.is_file()
        current_hash = sha256_file(path) or ""
        expected_after_hash = str(record.get("after_sha256", "") or "")
        if current_exists != existed_after or current_hash != expected_after_hash:
            errors.append(
                f"{record.get('relative_path', path.name)} changed after the transaction "
                f"(expected exists={existed_after}, sha256={expected_after_hash or '[absent]'}; "
                f"current exists={current_exists}, sha256={current_hash or '[absent]'})"
            )
            continue
        existed_before = bool(record.get("existed_before"))
        backup_path = Path(str(record.get("backup_path", ""))).expanduser()
        before_hash = str(record.get("before_sha256", "") or "")
        if existed_before:
            if not backup_path.is_file():
                errors.append(f"missing before-image for {record.get('relative_path')}: {backup_path}")
                continue
            if sha256_file(backup_path) != before_hash:
                errors.append(f"before-image hash mismatch for {record.get('relative_path')}")
                continue
            action = "restore_before_image"
        else:
            action = "delete_transaction_created_file"
        operations.append(
            {
                "path": path,
                "relative_path": str(record.get("relative_path", "")),
                "action": action,
                "backup_path": backup_path,
            }
        )
    return errors, operations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        required=True,
        help="Path to manifest.json or a transaction ID under snapshots/cards/.",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    manifest_path = _manifest_path(args.manifest)
    manifest = _load_manifest(manifest_path)
    transaction_id = str(manifest["transaction_id"])
    token = f"RESTORE_{transaction_id}"
    errors, operations = _preflight(manifest)

    print(f"Transaction: {transaction_id}")
    print(f"Step: {manifest.get('step')} | completed: {manifest.get('completed_at')}")
    print(f"Touched files: {len(operations)} | preflight errors: {len(errors)}")
    for operation in operations:
        print(f"  {operation['action']:<31} {operation['relative_path']}")
    if errors:
        for error in errors:
            print(f"  ERROR: {error}")

    if not args.apply:
        print(f"\nDRY RUN. To restore, rerun with --apply --confirm {token!r}")
        return 0 if not errors else 1
    if args.confirm != token:
        raise RuntimeError(f"Exact confirmation required: {token}")
    if errors:
        raise RuntimeError("Restore preflight failed; no files were changed.")

    run_dir = Path(new_run_dir("restore_card_snapshot"))
    receipt = {
        "restored_transaction_id": transaction_id,
        "source_manifest": str(manifest_path),
        "restored_at": dt.datetime.now().isoformat(timespec="seconds"),
        "restored_by_run_id": current_run_id(),
        "file_count": len(operations),
        "operations": [
            {"relative_path": op["relative_path"], "action": op["action"]}
            for op in operations
        ],
    }
    with card_transaction(
        f"restore_{transaction_id}",
        project_root=manifest["project_root"],
        cards_dir=manifest["cards_dir"],
    ) as transaction:
        # Recheck after acquiring the same card lock used by writers.
        locked_errors, locked_operations = _preflight(manifest)
        if locked_errors:
            raise RuntimeError(
                "Files changed before the locked restore:\n - " + "\n - ".join(locked_errors)
            )
        for operation in reversed(locked_operations):
            if operation["action"] == "restore_before_image":
                transaction.write_bytes(
                    operation["path"],
                    operation["backup_path"].read_bytes(),
                    operation="restore_before_image",
                )
            elif operation["path"].is_file():
                transaction.delete(
                    operation["path"],
                    operation="remove_transaction_created_file",
                )
        transaction.write_text(
            run_dir / "restore_receipt.json",
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            operation="restore_receipt",
        )

    ledger_append(
        "restore_card_snapshot",
        params={"manifest": str(manifest_path), "apply": True},
        inputs={"manifest": str(manifest_path)},
        outputs={"restored_transaction_id": transaction_id, "files": len(operations)},
        gate_passed=True,
        run_dir=str(run_dir),
    )
    print(f"✓ restored {len(operations)} touched file(s); receipt → {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
