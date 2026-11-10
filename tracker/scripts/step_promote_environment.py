"""Create and apply an explicit, immutable card promotion plan.

A plan copies changed source cards into a hash-pinned payload under the source
run directory. Applying that plan verifies source, payload, and target hashes,
requires an exact confirmation token, preserves target-only cards, and commits
all target writes through one card transaction. No environment is selected by
pointing NIH_TRACKER at another folder.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import yaml

from common import (
    ENV,
    PROJECT,
    FileLock,
    RUNS,
    VALID_ENVS,
    PROD_WRITE_CONFIRMATION_TOKEN,
    atomic_write_bytes,
    atomic_write_csv_rows,
    atomic_write_json,
    card_transaction,
    current_run_id,
    ledger_append,
    project_for_env,
    run_ts,
    sha256_file,
)

PLAN_SCHEMA = "nih-tracker-environment-promotion/v1"
PLAN_SEAL_NAME = "promotion_plan.sha256"
ENV_RANK = {"dev": 0, "staging": 1, "prod": 2}
_UID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _root(env: str) -> Path:
    return Path(project_for_env(env)).expanduser().resolve()


def _card_files(cards_dir: Path) -> dict[str, Path]:
    if not cards_dir.is_dir():
        return {}
    files = sorted(set(cards_dir.glob("*.yaml")) | set(cards_dir.glob("*.yml")))
    result: dict[str, Path] = {}
    for path in files:
        uid = path.stem
        if uid in result:
            raise ValueError(f"Duplicate YAML/YML card stem in {cards_dir}: {uid}")
        result[uid] = path
    return result


def _validate_card_bytes(data: bytes, uid: str, *, origin: str) -> dict[str, Any]:
    if not _UID_RE.fullmatch(uid):
        raise ValueError(f"Unsafe card UID {uid!r} in {origin}")
    try:
        card = yaml.safe_load(data.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid YAML for {uid} in {origin}: {exc}") from exc
    if not isinstance(card, dict):
        raise ValueError(f"Card {uid} in {origin} is not a YAML mapping")
    card_uid = _clean(card.get("repo_uid"))
    if card_uid != uid:
        raise ValueError(
            f"Card identity mismatch in {origin}: filename={uid}, repo_uid={card_uid!r}"
        )
    return card


def _next_plan_dir(source_root: Path) -> Path:
    base = source_root / "runs" / "promote_environment" / run_ts()
    result = base
    suffix = 1
    while result.exists():
        result = Path(f"{base}_{suffix:02d}")
        suffix += 1
    result.mkdir(parents=True, exist_ok=False)
    return result


def _parse_uids(value: str) -> set[str] | None:
    values = {_clean(item) for item in value.split(",") if _clean(item)}
    for uid in values:
        if not _UID_RE.fullmatch(uid):
            raise ValueError(f"Unsafe UID in --uids: {uid!r}")
    return values or None


def create_plan(from_env: str, to_env: str, *, selected_uids: set[str] | None = None) -> Path:
    if ENV != from_env:
        raise RuntimeError(
            f"The active notebook/process environment is {ENV!r}; create promotion plans "
            f"from the selected source environment {from_env!r}."
        )
    if ENV_RANK[from_env] >= ENV_RANK[to_env]:
        raise ValueError("Promotion must move upward: dev -> staging/prod or staging -> prod.")

    source_root, target_root = _root(from_env), _root(to_env)
    if source_root == target_root:
        raise ValueError("Source and target roots resolve to the same directory.")

    # Take a coherent source snapshot under the same exclusive lock used by all
    # card writers. The lock is released once immutable bytes/hashes are in
    # memory; apply later refuses any source drift from this snapshot.
    source_cards_dir = source_root / "cards"
    with FileLock(
        source_cards_dir / ".lock",
        purpose=f"promotion plan snapshot: {from_env} to {to_env}",
        timeout=0,
    ):
        source_cards = _card_files(source_cards_dir)
        if not source_cards:
            raise ValueError(f"No source cards found in {source_cards_dir}")
        if selected_uids is not None:
            absent = sorted(selected_uids - set(source_cards))
            if absent:
                raise ValueError(f"Selected source UIDs do not exist: {absent[:10]}")
            source_cards = {
                uid: card_path
                for uid, card_path in source_cards.items()
                if uid in selected_uids
            }
        source_snapshots = {}
        for uid, source_path in sorted(source_cards.items()):
            data = source_path.read_bytes()
            _validate_card_bytes(data, uid, origin=str(source_path))
            source_snapshots[uid] = {
                "path": source_path,
                "data": data,
                "sha256": hashlib.sha256(data).hexdigest(),
            }

    target_cards = _card_files(target_root / "cards")
    plan_dir = _next_plan_dir(source_root)
    payload_dir = plan_dir / "payload" / "cards"
    payload_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for uid, snapshot in sorted(source_snapshots.items()):
        source_path = snapshot["path"]
        data = snapshot["data"]
        source_hash = snapshot["sha256"]
        target_path = target_cards.get(uid)
        target_hash = sha256_file(target_path) if target_path else None
        action = "unchanged" if target_hash == source_hash else ("update" if target_path else "add")
        payload_rel = ""
        payload_hash = ""
        if action != "unchanged":
            payload_path = payload_dir / f"{uid}.yaml"
            atomic_write_bytes(payload_path, data)
            payload_rel = payload_path.relative_to(plan_dir).as_posix()
            payload_hash = sha256_file(payload_path) or ""
        entries.append(
            {
                "repo_uid": uid,
                "action": action,
                "source_relative_path": source_path.relative_to(source_root).as_posix(),
                "target_relative_path": f"cards/{uid}.yaml",
                "source_sha256": source_hash,
                "target_before_sha256": target_hash or "",
                "payload_relative_path": payload_rel,
                "payload_sha256": payload_hash,
            }
        )

    changed = [entry for entry in entries if entry["action"] != "unchanged"]
    plan = {
        "schema": PLAN_SCHEMA,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "created_run_id": current_run_id(),
        "from_env": from_env,
        "to_env": to_env,
        "source_root": str(source_root),
        "target_root": str(target_root),
        "policy": {
            "scope": "top-level cards only",
            "delete_target_only_cards": False,
            "require_unchanged_source_since_plan": True,
            "require_unchanged_target_since_plan": True,
        },
        "selected_uids": sorted(selected_uids) if selected_uids else [],
        "entry_count": len(entries),
        "changed_count": len(changed),
        "entries": entries,
    }
    plan_path = plan_dir / "promotion_plan.json"
    atomic_write_json(plan_path, plan)
    plan_sha256 = sha256_file(plan_path) or ""
    atomic_write_bytes(
        plan_dir / PLAN_SEAL_NAME,
        f"{plan_sha256}  {plan_path.name}\n".encode("utf-8"),
    )
    atomic_write_csv_rows(
        plan_dir / "promotion_plan.csv",
        entries,
        [
            "repo_uid",
            "action",
            "source_relative_path",
            "target_relative_path",
            "source_sha256",
            "target_before_sha256",
            "payload_relative_path",
            "payload_sha256",
        ],
    )
    print(f"Promotion plan created: {plan_path}")
    print(f"  plan_sha256={plan_sha256}")
    print(
        f"  source={from_env}:{source_root}\n  target={to_env}:{target_root}\n"
        f"  add={sum(e['action'] == 'add' for e in entries)} "
        f"update={sum(e['action'] == 'update' for e in entries)} "
        f"unchanged={sum(e['action'] == 'unchanged' for e in entries)}"
    )
    if changed:
        print(f"Apply only after review with confirmation token: PROMOTE_{from_env.upper()}_TO_{to_env.upper()}")
    else:
        print("No changed cards require promotion.")
    ledger_append(
        "promote_environment_plan",
        params={
            "from_env": from_env,
            "to_env": to_env,
            "selected_uids": sorted(selected_uids) if selected_uids else [],
        },
        inputs={"source_cards": str(source_root / "cards")},
        outputs={
            "plan": str(plan_path),
            "plan_sha256": plan_sha256,
            "entries": len(entries),
            "changed": len(changed),
        },
        gate_passed=True,
        run_dir=str(plan_dir),
    )
    return plan_path


def _load_plan(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    seal_path = path.parent / PLAN_SEAL_NAME
    if not seal_path.is_file():
        raise FileNotFoundError(f"Missing promotion plan seal: {seal_path}")
    seal_parts = seal_path.read_text(encoding="utf-8").strip().split()
    expected_sha256 = seal_parts[0] if seal_parts else ""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError(f"Invalid promotion plan seal: {seal_path}")
    actual_sha256 = sha256_file(path) or ""
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"Promotion plan seal mismatch: expected {expected_sha256}, got {actual_sha256}"
        )
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid promotion plan JSON: {exc}") from exc
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError(f"Unsupported promotion plan schema: {plan.get('schema')!r}")
    return plan


def _validate_apply_plan(plan_path: Path, plan: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    from_env, to_env = plan.get("from_env"), plan.get("to_env")
    if from_env not in VALID_ENVS or to_env not in VALID_ENVS:
        errors.append("plan contains invalid environment names")
        return errors, []
    if ENV != from_env:
        errors.append(f"active environment is {ENV}, but plan source is {from_env}")
    expected_source, expected_target = _root(from_env), _root(to_env)
    source_root = Path(str(plan.get("source_root", ""))).expanduser().resolve()
    target_root = Path(str(plan.get("target_root", ""))).expanduser().resolve()
    try:
        plan_path.resolve().relative_to(source_root / "runs" / "promote_environment")
    except ValueError:
        errors.append("plan path is outside the source promotion-run directory")
    if source_root != expected_source:
        errors.append(f"source root mapping changed: plan={source_root}, current={expected_source}")
    if target_root != expected_target:
        errors.append(f"target root mapping changed: plan={target_root}, current={expected_target}")
    if source_root == target_root:
        errors.append("source and target roots are identical")

    entries = plan.get("entries", [])
    if not isinstance(entries, list):
        errors.append("plan entries must be a list")
        return errors, []
    if int(plan.get("entry_count", -1)) != len(entries):
        errors.append("plan entry_count does not match entries")
    actual_changed_count = sum(
        isinstance(entry, dict) and _clean(entry.get("action")) != "unchanged"
        for entry in entries
    )
    if int(plan.get("changed_count", -1)) != actual_changed_count:
        errors.append("plan changed_count does not match entries")
    policy = plan.get("policy", {})
    if not isinstance(policy, dict) or policy.get("delete_target_only_cards") is not False:
        errors.append("plan deletion policy is missing or unsafe")

    changed: list[dict[str, Any]] = []
    seen: set[str] = set()
    plan_dir = plan_path.parent
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("plan contains a non-object entry")
            continue
        uid = _clean(entry.get("repo_uid"))
        action = _clean(entry.get("action"))
        if not _UID_RE.fullmatch(uid) or uid in seen:
            errors.append(f"unsafe or duplicate plan UID: {uid!r}")
            continue
        seen.add(uid)
        if action not in {"add", "update", "unchanged"}:
            errors.append(f"{uid}: invalid action {action!r}")
            continue
        source_path = source_root / str(entry.get("source_relative_path", ""))
        try:
            source_path.resolve().relative_to(source_root / "cards")
        except ValueError:
            errors.append(f"{uid}: source path escapes source cards directory")
            continue
        if sha256_file(source_path) != _clean(entry.get("source_sha256")):
            errors.append(f"{uid}: source card changed or vanished since plan creation")
        if action == "unchanged":
            continue

        target_path = target_root / str(entry.get("target_relative_path", ""))
        try:
            target_path.resolve().relative_to(target_root / "cards")
        except ValueError:
            errors.append(f"{uid}: target path escapes target cards directory")
            continue
        current_target_hash = sha256_file(target_path) or ""
        if current_target_hash != _clean(entry.get("target_before_sha256")):
            errors.append(f"{uid}: target card changed or appeared since plan creation")

        payload_path = plan_dir / str(entry.get("payload_relative_path", ""))
        try:
            payload_path.resolve().relative_to(plan_dir / "payload" / "cards")
        except ValueError:
            errors.append(f"{uid}: payload path escapes plan payload directory")
            continue
        if sha256_file(payload_path) != _clean(entry.get("payload_sha256")):
            errors.append(f"{uid}: promotion payload hash mismatch")
            continue
        try:
            _validate_card_bytes(payload_path.read_bytes(), uid, origin=str(payload_path))
        except Exception as exc:
            errors.append(str(exc))
            continue
        changed.append({**entry, "source_path": source_path, "target_path": target_path, "payload_path": payload_path})
    return errors, changed


def apply_plan(plan_path: Path, *, confirm: str) -> Path:
    plan = _load_plan(plan_path)
    from_env, to_env = plan["from_env"], plan["to_env"]
    token = f"PROMOTE_{from_env.upper()}_TO_{to_env.upper()}"
    if confirm != token:
        raise RuntimeError(f"Exact confirmation required: {token}")
    errors, changed = _validate_apply_plan(plan_path, plan)
    if errors:
        raise RuntimeError("Promotion preflight failed:\n - " + "\n - ".join(errors))
    if not changed:
        raise RuntimeError("Plan contains no changed cards to promote.")

    target_root = Path(plan["target_root"]).expanduser().resolve()
    target_cards = target_root / "cards"
    target_run_dir = target_root / "runs" / "promote_environment" / f"{run_ts()}_from_{from_env}"
    suffix = 1
    while target_run_dir.exists():
        target_run_dir = Path(f"{target_run_dir}_{suffix:02d}")
        suffix += 1
    receipt_path = target_run_dir / "promotion_receipt.json"
    receipt = {
        "schema": PLAN_SCHEMA,
        "status": "committed",
        "applied_at": dt.datetime.now().isoformat(timespec="seconds"),
        "applied_run_id": current_run_id(),
        "plan_path": str(plan_path.resolve()),
        "plan_sha256": sha256_file(plan_path) or "",
        "from_env": from_env,
        "to_env": to_env,
        "source_root": plan["source_root"],
        "target_root": plan["target_root"],
        "confirmation_token": token,
        "cards_written": [
            {
                "repo_uid": entry["repo_uid"],
                "action": entry["action"],
                "sha256": entry["payload_sha256"],
            }
            for entry in changed
        ],
        "target_only_cards_preserved": True,
        "deletions": 0,
    }

    prior_prod_confirmation = os.environ.get("NIH_TRACKER_PROD_WRITE_CONFIRMATION")
    if to_env == "prod":
        # The promotion token above is the user-facing confirmation. Set the
        # lower-level transaction guard only for this validated commit scope.
        os.environ["NIH_TRACKER_PROD_WRITE_CONFIRMATION"] = PROD_WRITE_CONFIRMATION_TOKEN
    try:
        with card_transaction(
            f"promote_{from_env}_to_{to_env}",
            project_root=target_root,
            cards_dir=target_cards,
        ) as transaction:
            # Re-run target checks after acquiring the exclusive target lock. Source
            # and payload checks are also repeated by the helper for fail-closed use.
            locked_plan = _load_plan(plan_path)
            if locked_plan != plan:
                raise RuntimeError("Promotion plan changed between preflight and locked commit.")
            locked_errors, locked_changed = _validate_apply_plan(plan_path, locked_plan)
            if locked_errors:
                raise RuntimeError(
                    "Promotion inputs changed before locked commit:\n - "
                    + "\n - ".join(locked_errors)
                )
            target_run_dir.mkdir(parents=True, exist_ok=False)
            for entry in locked_changed:
                transaction.write_bytes(
                    entry["target_path"],
                    Path(entry["payload_path"]).read_bytes(),
                    operation=f"environment_promotion:{entry['action']}",
                )
            transaction.write_text(
                receipt_path,
                json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                operation="environment_promotion_receipt",
            )
    finally:
        if prior_prod_confirmation is None:
            os.environ.pop("NIH_TRACKER_PROD_WRITE_CONFIRMATION", None)
        else:
            os.environ["NIH_TRACKER_PROD_WRITE_CONFIRMATION"] = prior_prod_confirmation

    pointer = {
        "status": "committed",
        "target_receipt": str(receipt_path),
        "applied_at": receipt["applied_at"],
        "cards_written": len(changed),
    }
    audit_warnings = []
    try:
        atomic_write_json(plan_path.parent / "promotion_apply_pointer.json", pointer)
    except Exception as exc:
        audit_warnings.append(f"source apply pointer was not written: {exc}")
    try:
        ledger_append(
            "promote_environment_apply",
            params={"from_env": from_env, "to_env": to_env, "plan": str(plan_path)},
            inputs={"plan": str(plan_path)},
            outputs={"target_receipt": str(receipt_path), "cards_written": len(changed)},
            gate_passed=True,
            run_dir=str(plan_path.parent),
        )
    except Exception as exc:
        audit_warnings.append(f"source ledger row was not written: {exc}")
    print(f"✓ promoted {len(changed)} card(s) from {from_env} to {to_env}")
    print(f"✓ immutable target receipt: {receipt_path}")
    for warning in audit_warnings:
        print(f"WARNING after successful target commit: {warning}")
    return receipt_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-env", choices=VALID_ENVS, default=None)
    parser.add_argument("--to-env", choices=VALID_ENVS, default=None)
    parser.add_argument(
        "--uids",
        default="",
        help="Optional comma-separated source UIDs; default plans all changed top-level cards.",
    )
    parser.add_argument("--plan", help="Existing promotion_plan.json to apply.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    if args.apply:
        if not args.plan:
            parser.error("--apply requires --plan; plan creation and apply are separate invocations")
        plan_path = Path(args.plan).expanduser().resolve()
        plan = _load_plan(plan_path)
        if args.from_env and args.from_env != plan.get("from_env"):
            parser.error("--from-env conflicts with the plan")
        if args.to_env and args.to_env != plan.get("to_env"):
            parser.error("--to-env conflicts with the plan")
        apply_plan(plan_path, confirm=args.confirm)
        return 0

    if args.plan:
        parser.error("--plan is used only with --apply")
    from_env = args.from_env or ENV
    to_env = args.to_env or "prod"
    create_plan(from_env, to_env, selected_uids=_parse_uids(args.uids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
