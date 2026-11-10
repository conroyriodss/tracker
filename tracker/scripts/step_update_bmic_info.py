"""Validate and refresh the BMIC repository snapshot used by NIH Tracker.

The updater accepts an explicit local CSV, an explicit HTTPS CSV URL, or an
auto-discovered Colab upload. When no upload is present, it validates the
existing project snapshot as a clearly reported no-op rather than failing on a
hard-coded path. Dry run is the default. ``--apply`` atomically replaces
``inputs/BMIC_download.csv`` only when the validated candidate differs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import traceback
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import pandas as pd

from common import (
    ENV,
    INP,
    PROJECT,
    PROD_WRITE_CONFIRMATION_TOKEN,
    FileLock,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    gate,
    ledger_append,
    new_run_dir,
    sha256_file,
)

DEFAULT_DESTINATION = Path(INP) / "BMIC_download.csv"
DEFAULT_LOCAL_SOURCE = Path("/content/BMIC_download.csv")
LOCK_PATH = Path(PROJECT) / "inputs" / ".bmic_update.lock"
NAME_COLUMNS = {"repository_name", "repository name", "name", "repository"}
OPEN_SUBMISSION_COLUMNS = {"open_data_submission", "open data submission"}


def _column_keys(frame: pd.DataFrame) -> set[str]:
    return {str(column).strip().casefold() for column in frame.columns}


def _uploaded_candidates(destination: Path) -> list[Path]:
    """Return plausible BMIC uploads, newest first, with the project file last."""
    candidates: list[Path] = []
    exact = DEFAULT_LOCAL_SOURCE
    if exact.is_file():
        candidates.append(exact)

    for parent in (Path("/content"), Path("/content/drive/MyDrive")):
        if not parent.is_dir():
            continue
        matches = [
            path
            for path in parent.iterdir()
            if path.is_file()
            and path.suffix.casefold() == ".csv"
            and "bmic" in path.name.casefold()
            and path.resolve() != destination.resolve()
        ]
        matches.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        candidates.extend(matches)

    if destination.is_file():
        candidates.append(destination)

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved not in seen:
            unique.append(resolved)
            seen.add(resolved)
    return unique


def _resolve_source(
    source_file: Path | None,
    source_url: str,
    destination: Path,
    *,
    require_source_file: bool,
) -> tuple[bytes, str, str, bool]:
    if source_url:
        if not source_url.startswith("https://"):
            raise ValueError("--source-url must use HTTPS")
        request = Request(source_url, headers={"User-Agent": "NIH-Tracker/17.1"})
        with urlopen(request, timeout=60) as response:
            data = response.read()
            final_url = response.geturl()
        return data, final_url, "https_url", False

    if source_file is not None and source_file.is_file():
        resolved = source_file.expanduser().resolve()
        return (
            resolved.read_bytes(),
            str(resolved),
            "explicit_source_file",
            resolved == destination.expanduser().resolve(),
        )

    if source_file is not None and require_source_file:
        raise FileNotFoundError(
            f"Required BMIC source file not found: {source_file}. Upload the CSV or choose auto-detect."
        )

    # Backward compatibility: v17 notebooks explicitly passed the conventional
    # /content path. If it is absent, treat that as auto-detect rather than a
    # fatal error. Other missing explicit paths remain errors unless the caller
    # deliberately omitted --require-source-file.
    if source_file is not None and source_file != DEFAULT_LOCAL_SOURCE:
        raise FileNotFoundError(f"BMIC source file not found: {source_file}")

    candidates = _uploaded_candidates(destination)
    if not candidates:
        raise FileNotFoundError(
            "No BMIC CSV was found. Upload a file whose name contains 'BMIC' to /content, "
            "or retain inputs/BMIC_download.csv in the selected environment."
        )
    selected = candidates[0]
    selection = "existing_project_snapshot" if selected == destination.resolve() else "auto_detected_upload"
    return selected.read_bytes(), str(selected), selection, selected == destination.resolve()


def _parse_csv(data: bytes) -> tuple[pd.DataFrame, str]:
    failures: list[str] = []
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "iso-8859-1"):
        try:
            text = data.decode(encoding)
            frame = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False).fillna("")
            return frame, encoding
        except (UnicodeDecodeError, pd.errors.ParserError) as exc:
            failures.append(f"{encoding}: {type(exc).__name__}: {exc}")
    raise ValueError("Could not decode/parse BMIC CSV: " + "; ".join(failures))


def _production_guard(apply: bool) -> None:
    if apply and ENV == "prod" and os.environ.get("NIH_TRACKER_PROD_WRITE_CONFIRMATION") != PROD_WRITE_CONFIRMATION_TOKEN:
        raise RuntimeError(
            "Production reference writes require "
            f"NIH_TRACKER_PROD_WRITE_CONFIRMATION={PROD_WRITE_CONFIRMATION_TOKEN!r}."
        )


def run(
    *,
    source_file: Path | None,
    source_url: str,
    destination: Path,
    minimum_rows: int,
    maximum_drop_fraction: float,
    apply: bool,
    require_source_file: bool = False,
) -> tuple[bool, dict[str, Any], Path]:
    _production_guard(apply)
    run_dir = Path(new_run_dir("update_bmic_info"))
    try:
        data, source, source_selection, source_is_destination = _resolve_source(
            source_file,
            source_url,
            destination,
            require_source_file=require_source_file,
        )
        frame, source_encoding = _parse_csv(data)
    except Exception as exc:
        failure = {
            "schema": "nih-tracker-bmic-update-error/v1",
            "source_file": str(source_file or ""),
            "source_url": source_url,
            "destination": str(destination),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "apply_requested": apply,
            "applied": False,
            "gate_passed": False,
            "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        atomic_write_json(run_dir / "update_error.json", failure)
        atomic_write_text(run_dir / "traceback.txt", traceback.format_exc())
        ledger_append(
            "update_bmic_info",
            params={
                "source_file": str(source_file or ""),
                "source_url": source_url,
                "destination": str(destination),
                "apply": apply,
            },
            inputs={},
            outputs={"applied": 0, "error_report": str(run_dir / "update_error.json")},
            gate_passed=False,
            run_dir=str(run_dir),
        )
        print("\nBMIC UPDATE FAILED")
        print(f"  {type(exc).__name__}: {exc}")
        print(f"  Error report: {run_dir / 'update_error.json'}")
        print(f"BMIC update run: {run_dir}")
        return False, failure, run_dir

    keys = _column_keys(frame)
    previous_sha256 = sha256_file(destination) if destination.is_file() else None
    old_rows = 0
    if destination.is_file() and destination.stat().st_size:
        old_rows = len(pd.read_csv(destination, dtype=str, keep_default_na=False))
    row_floor = max(minimum_rows, int(old_rows * (1.0 - maximum_drop_fraction))) if old_rows else minimum_rows
    checks = {
        "source CSV parsed": (len(frame.columns) > 0, f"{len(frame.columns)} columns; encoding={source_encoding}"),
        "repository-name column present": (bool(keys & NAME_COLUMNS), str(sorted(keys & NAME_COLUMNS))),
        "open-submission column present": (
            bool(keys & OPEN_SUBMISSION_COLUMNS),
            str(sorted(keys & OPEN_SUBMISSION_COLUMNS)),
        ),
        "row count above safety floor": (len(frame) >= row_floor, f"{len(frame)} rows; floor={row_floor}"),
        "non-empty rows": (len(frame) > 0, f"{len(frame)} rows"),
    }
    ok = gate(checks)

    # Normalize a newly supplied CSV to UTF-8 for downstream reproducibility.
    # When validating the destination itself, preserve exact bytes so the
    # operation is a true no-op rather than a formatting rewrite.
    candidate_bytes = data if source_is_destination else frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    source_copy = run_dir / "BMIC_download.candidate.csv"
    atomic_write_bytes(source_copy, candidate_bytes)
    candidate_sha256 = sha256_file(source_copy)
    changed = previous_sha256 != candidate_sha256
    report = {
        "source": source,
        "source_selection": source_selection,
        "source_encoding": source_encoding,
        "source_is_destination": source_is_destination,
        "candidate_sha256": candidate_sha256,
        "candidate_rows": len(frame),
        "candidate_columns": list(map(str, frame.columns)),
        "previous_sha256": previous_sha256,
        "previous_rows": old_rows,
        "safety_floor": row_floor,
        "destination": str(destination),
        "changed": changed,
        "no_op": bool(ok and not changed),
        "apply_requested": apply,
        "gate_passed": ok,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    print(f"\nSelected BMIC source: {source}")
    print(f"Source selection: {source_selection}")

    applied = False
    if apply and ok and changed:
        with FileLock(LOCK_PATH, purpose="update_bmic_info"):
            atomic_write_bytes(destination, candidate_bytes)
            metadata = {
                **report,
                "destination_sha256": sha256_file(destination),
                "applied": True,
            }
            atomic_write_json(destination.with_suffix(destination.suffix + ".metadata.json"), metadata)
        applied = True
        print(f"Updated BMIC snapshot: {destination}")
    elif apply and ok:
        print("Valid no-op: the selected BMIC source is byte-identical to the current project snapshot.")
    elif apply:
        print("Gate failed; the BMIC snapshot was not changed.")
    else:
        print("DRY RUN - inspect the candidate snapshot and update report before --apply.")
        if not changed:
            print("The selected source matches the current project snapshot; apply would be a no-op.")
    report["applied"] = applied
    atomic_write_json(run_dir / "update_report.json", report)
    ledger_append(
        "update_bmic_info",
        params={
            "source_file": str(source_file or ""),
            "source_url": source_url,
            "source_selection": source_selection,
            "destination": str(destination),
            "apply": apply,
        },
        inputs={"candidate": report["candidate_sha256"], "previous": previous_sha256},
        outputs={
            "rows": len(frame),
            "changed": int(changed),
            "no_op": int(report["no_op"]),
            "applied": int(applied),
            "report": str(run_dir / "update_report.json"),
        },
        gate_passed=ok,
        run_dir=str(run_dir),
    )
    print(f"BMIC update run: {run_dir}")
    return ok, report, run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-file",
        default="",
        help="CSV path. Omit for auto-detection; the existing project snapshot is the final fallback.",
    )
    parser.add_argument("--require-source-file", action="store_true")
    parser.add_argument("--source-url", default="")
    parser.add_argument("--destination", default=str(DEFAULT_DESTINATION))
    parser.add_argument("--minimum-rows", type=int, default=100)
    parser.add_argument("--maximum-drop-fraction", type=float, default=0.25)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    ok, _, _ = run(
        source_file=Path(args.source_file).expanduser() if args.source_file else None,
        source_url=args.source_url.strip(),
        destination=Path(args.destination).expanduser(),
        minimum_rows=max(1, args.minimum_rows),
        maximum_drop_fraction=min(max(args.maximum_drop_fraction, 0.0), 0.95),
        apply=args.apply,
        require_source_file=args.require_source_file,
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
