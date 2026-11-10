"""Inspect, validate, or finalize the entire NIH Tracker review workflow.

The command is the single operational entry point used by the simplified review
workbench. It skips empty decision families rather than launching successful
no-op subprocesses. Existing family-specific scripts remain available for
forensic or targeted use.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from common import RUNS, atomic_write_json, current_run_id, ledger_append, new_run_dir
from review_workflow import compute_review_status, concise_lines

CONFIRM_TOKEN = "FINALIZE_REVIEW_WORKFLOW"


def _run_id(label: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return f"{current_run_id()}-{label}-{stamp}"


def _ledger_gate(run_id: str) -> bool | None:
    from common import LEDGER

    path = Path(LEDGER)
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        frame = pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")
    except Exception:
        return None
    if "run_id" not in frame.columns:
        return None
    matches = frame.loc[frame["run_id"].astype(str).eq(run_id)]
    if matches.empty:
        return None
    value = str(matches.iloc[-1].get("gate_passed", "")).strip().lower()
    if value in {"true", "1", "yes"}:
        return True
    if value in {"false", "0", "no"}:
        return False
    return None


def _run_script(
    scripts_dir: Path,
    run_dir: Path,
    label: str,
    script: str,
    args: list[str],
) -> dict[str, Any]:
    run_id = _run_id(label)
    command = [sys.executable, str(scripts_dir / script), *args]
    env = os.environ.copy()
    env["NIH_TRACKER_RUN_ID"] = run_id
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        check=False,
    )
    log_path = run_dir / f"{label}.log"
    log_path.write_text(completed.stdout or "", encoding="utf-8")
    if completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    gate = _ledger_gate(run_id)
    ok = completed.returncode == 0 and gate is not False
    record = {
        "label": label,
        "script": script,
        "args": args,
        "run_id": run_id,
        "returncode": completed.returncode,
        "gate_passed": gate,
        "ok": ok,
        "log_path": str(log_path),
    }
    atomic_write_json(run_dir / f"{label}.result.json", record)
    return record


def _pending(status: dict[str, Any], family: str) -> int:
    return int(status["operational_adjudications"].get(family, 0) or 0)


def _validate_operational(
    scripts_dir: Path,
    run_dir: Path,
    status: dict[str, Any],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if _pending(status, "identifier"):
        results.append(
            _run_script(
                scripts_dir,
                run_dir,
                "validate_identifier_decisions",
                "step_apply_verify_adjudications.py",
                ["--adjudications", "verify_adjudications.csv", "--bulk-safe"],
            )
        )
    if _pending(status, "crosswalk"):
        results.append(
            _run_script(
                scripts_dir,
                run_dir,
                "validate_crosswalk_decisions",
                "step_apply_crosswalk_adjudications.py",
                ["--adjudications", "crosswalk_adjudications.csv"],
            )
        )
    if _pending(status, "card_name"):
        results.append(
            _run_script(
                scripts_dir,
                run_dir,
                "validate_card_name_decisions",
                "step_promote_review.py",
                ["--adjudications", "adjudications.csv"],
            )
        )
    return results


def _apply_operational(
    scripts_dir: Path,
    run_dir: Path,
    status: dict[str, Any],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if _pending(status, "identifier"):
        results.append(
            _run_script(
                scripts_dir,
                run_dir,
                "apply_identifier_decisions",
                "step_apply_verify_adjudications.py",
                ["--adjudications", "verify_adjudications.csv", "--bulk-safe", "--apply"],
            )
        )
    if _pending(status, "crosswalk"):
        results.append(
            _run_script(
                scripts_dir,
                run_dir,
                "apply_crosswalk_decisions",
                "step_apply_crosswalk_adjudications.py",
                ["--adjudications", "crosswalk_adjudications.csv", "--apply"],
            )
        )
    if _pending(status, "card_name"):
        results.append(
            _run_script(
                scripts_dir,
                run_dir,
                "apply_card_name_decisions",
                "step_promote_review.py",
                ["--adjudications", "adjudications.csv", "--apply"],
            )
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--action",
        choices=["status", "validate", "apply"],
        default="status",
    )
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    scripts_dir = Path(__file__).resolve().parent
    run_dir = Path(new_run_dir("review_workflow"))
    before = compute_review_status(include_evidence=True)
    atomic_write_json(run_dir / "status_before.json", before)
    print("\n".join(concise_lines(before)))

    results: list[dict[str, Any]] = []
    status = "complete" if before["ready_for_pipeline"] else "pending"
    message = before["next_action"]

    pending_human_approval = int(
        before["recommendations"]["review_items_awaiting_human_approval"]
    )
    if args.action in {"validate", "apply"} and pending_human_approval:
        status = "approval_required"
        message = (
            f"Finalization is disabled because {pending_human_approval} imported review "
            "item(s) still require named human approval."
        )
        final_status = compute_review_status(include_evidence=True)
        atomic_write_json(run_dir / "review_status.json", final_status)
        plan = {
            "schema": "nih-tracker-guided-review-run/v1",
            "action": args.action,
            "status": status,
            "message": message,
            "before": before,
            "after": final_status,
            "subprocesses": [],
        }
        atomic_write_json(run_dir / "review_workflow_result.json", plan)
        print(f"\n{message}")
        print("Open recommendation triage and save those decisions first.")
        print("No promotion or operational application was attempted.")
        print(f"Review workflow artifacts: {run_dir}")
        ledger_append(
            "review_workflow",
            params={"action": args.action},
            inputs={},
            outputs={
                "state_before": before["state"],
                "state_after": final_status["state"],
                "subprocess_count": 0,
                "result": str(run_dir / "review_workflow_result.json"),
            },
            gate_passed=False,
            run_dir=str(run_dir),
        )
        return 2

    if args.action == "status":
        pass
    elif args.action == "validate":
        pending_ai = int(
            before["recommendations"]["approved_recommendations_pending_promotion"]
        )
        if pending_ai:
            results.append(
                _run_script(
                    scripts_dir,
                    run_dir,
                    "validate_ai_promotion",
                    "step_promote_ai_review.py",
                    ["--bulk"],
                )
            )
        results.extend(_validate_operational(scripts_dir, run_dir, before))
        if not results:
            status = "valid_no_op"
            message = "No approved recommendation or operational adjudication requires validation."
        elif all(result["ok"] for result in results):
            status = "validation_passed"
            message = (
                "All currently materialized review decisions passed validation. "
                "Any AI approvals will be promoted and revalidated during Apply."
            )
        else:
            status = "validation_failed"
            message = "At least one review family failed validation; no source or card write was attempted."
    else:
        if args.confirm != CONFIRM_TOKEN:
            raise SystemExit(
                f"Apply requires --confirm {CONFIRM_TOKEN}. No review file or card was changed."
            )
        pending_ai = int(
            before["recommendations"]["approved_recommendations_pending_promotion"]
        )
        if pending_ai:
            validation = _run_script(
                scripts_dir,
                run_dir,
                "validate_ai_promotion",
                "step_promote_ai_review.py",
                ["--bulk"],
            )
            results.append(validation)
            if validation["ok"]:
                results.append(
                    _run_script(
                        scripts_dir,
                        run_dir,
                        "promote_ai_recommendations",
                        "step_promote_ai_review.py",
                        ["--bulk", "--apply", "--confirm", "PROMOTE_AI_RECOMMENDATIONS"],
                    )
                )
        if all(result["ok"] for result in results):
            materialized = compute_review_status(include_evidence=False)
            validation_results = _validate_operational(scripts_dir, run_dir, materialized)
            results.extend(validation_results)
            if all(result["ok"] for result in validation_results):
                results.extend(_apply_operational(scripts_dir, run_dir, materialized))

        after = compute_review_status(include_evidence=True)
        atomic_write_json(run_dir / "status_after.json", after)
        if all(result["ok"] for result in results):
            status = "applied" if results else "valid_no_op"
            message = (
                "All pending reviewed decisions were finalized."
                if results
                else "Review was already complete; no subprocess was launched."
            )
        else:
            status = "stopped"
            message = (
                "The workflow stopped at the first failed validation or application. "
                "Completed earlier stages remain receipted and are safe to retry."
            )

    final_status = compute_review_status(include_evidence=True)
    atomic_write_json(run_dir / "review_status.json", final_status)
    plan = {
        "schema": "nih-tracker-guided-review-run/v1",
        "action": args.action,
        "status": status,
        "message": message,
        "before": before,
        "after": final_status,
        "subprocesses": results,
    }
    atomic_write_json(run_dir / "review_workflow_result.json", plan)
    print(f"\n{message}")
    print(f"Review workflow artifacts: {run_dir}")
    print("\n".join(concise_lines(final_status)))

    gate = all(result["ok"] for result in results)
    ledger_append(
        "review_workflow",
        params={"action": args.action},
        inputs={},
        outputs={
            "state_before": before["state"],
            "state_after": final_status["state"],
            "subprocess_count": len(results),
            "result": str(run_dir / "review_workflow_result.json"),
        },
        gate_passed=gate,
        run_dir=str(run_dir),
    )
    return 0 if gate else 2


if __name__ == "__main__":
    raise SystemExit(main())
