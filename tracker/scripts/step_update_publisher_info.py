"""Refresh publisher-policy source snapshots and detect semantic drift.

This command refreshes the *source pages* used by the pinned publisher mention
inventory.  It never scrapes a changed page into new repository assertions.
Instead, it stores content-addressed snapshots, checks expected anchor terms,
and marks changed or structurally unexpected pages for human review.  Dry run is
the default; ``--apply`` updates only ``reference/publisher_policy_sources.csv``
and its metadata sidecar.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import pandas as pd

from common import (
    ENV,
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

REFERENCE = Path(PROJECT) / "reference"
DEFAULT_SOURCES = REFERENCE / "publisher_policy_sources.csv"
SNAPSHOT_ROOT = REFERENCE / "publisher_source_snapshots"
LOCK_PATH = REFERENCE / ".publisher_update.lock"
REQUIRED_COLUMNS = [
    "publisher_family", "policy_title", "policy_url", "expected_terms", "date_checked",
    "content_sha256", "snapshot_path", "http_status", "final_url", "refresh_status",
]


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _fetch(url: str) -> tuple[bytes, int, str, str]:
    if not url.startswith("https://"):
        raise ValueError(f"Publisher policy URL must use HTTPS: {url}")
    request = Request(url, headers={"User-Agent": "NIH-Tracker/17.1", "Accept": "text/html,application/pdf,*/*"})
    with urlopen(request, timeout=90) as response:
        data = response.read()
        status = int(getattr(response, "status", 200))
        final_url = response.geturl()
        content_type = str(response.headers.get("Content-Type") or "")
    return data, status, final_url, content_type


def _plain_text(data: bytes, content_type: str) -> str:
    if "pdf" in content_type.casefold() or data.startswith(b"%PDF"):
        return ""
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split()).casefold()


def _snapshot_path(digest: str, extension: str) -> Path:
    return SNAPSHOT_ROOT / digest[:2] / f"{digest}.{extension}.gz"


def _production_guard(apply: bool) -> None:
    if apply and ENV == "prod" and os.environ.get("NIH_TRACKER_PROD_WRITE_CONFIRMATION") != PROD_WRITE_CONFIRMATION_TOKEN:
        raise RuntimeError(
            "Production reference writes require "
            f"NIH_TRACKER_PROD_WRITE_CONFIRMATION={PROD_WRITE_CONFIRMATION_TOKEN!r}."
        )


def run(
    *,
    sources_path: Path,
    apply: bool,
    continue_on_error: bool,
    max_fetch_errors: int = 2,
) -> tuple[bool, dict[str, Any], Path]:
    _production_guard(apply)
    run_dir = Path(new_run_dir("update_publisher_info"))
    source_sha256_before = sha256_file(sources_path)
    frame = pd.read_csv(sources_path, dtype=str, keep_default_na=False).fillna("")
    frame.columns = [str(column).strip() for column in frame.columns]
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"Publisher source inventory lacks columns: {missing}")
    output_rows: list[dict[str, str]] = []
    report_rows: list[dict[str, Any]] = []
    staged_snapshots: dict[Path, bytes] = {}
    fetch_errors = 0
    successful_fetches = 0
    review_required = 0
    changed = 0
    failed_urls: list[str] = []
    failed_rows: list[int] = []
    for row_number, row in frame.iterrows():
        original_record = {column: _clean(row.get(column)) for column in REQUIRED_COLUMNS}
        record = dict(original_record)
        old_sha = record["content_sha256"]
        url = record["policy_url"]
        try:
            data, status, final_url, content_type = _fetch(url)
            successful_fetches += 1
            digest = hashlib.sha256(data).hexdigest()
            extension = "pdf" if "pdf" in content_type.casefold() or data.startswith(b"%PDF") else "html"
            snapshot = _snapshot_path(digest, extension)
            terms = [_clean(value) for value in record["expected_terms"].split(";") if _clean(value)]
            text = _plain_text(data, content_type)
            missing_terms = [term for term in terms if text and term.casefold() not in text]
            is_changed = bool(old_sha and old_sha != digest)
            status_label = "review_required" if missing_terms or is_changed else "current"
            if not old_sha:
                status_label = "initialized" if not missing_terms else "review_required"
            if is_changed:
                changed += 1
            if status_label == "review_required":
                review_required += 1
            record.update(
                {
                    "date_checked": dt.date.today().isoformat(),
                    "content_sha256": digest,
                    "snapshot_path": str(snapshot.relative_to(Path(PROJECT))),
                    "http_status": str(status),
                    "final_url": final_url,
                    "refresh_status": status_label,
                }
            )
            report_rows.append(
                {
                    "source_row": int(row_number) + 2,
                    "publisher_family": record["publisher_family"],
                    "policy_url": url,
                    "http_status": status,
                    "old_sha256": old_sha,
                    "new_sha256": digest,
                    "changed": str(is_changed).lower(),
                    "missing_expected_terms": "; ".join(missing_terms),
                    "refresh_status": status_label,
                    "snapshot_path": str(snapshot),
                    "row_action": "update_from_successful_fetch",
                    "error": "",
                }
            )
            if not snapshot.is_file():
                staged_snapshots[snapshot] = gzip.compress(data, mtime=0)
        except Exception as exc:
            fetch_errors += 1
            failed_urls.append(url)
            failed_rows.append(int(row_number) + 2)
            # Preserve the last successful, source-bound row exactly. The drift
            # report records the failed attempt; a transient fetch failure must
            # not erase or relabel the prior evidence snapshot.
            record = original_record
            report_rows.append(
                {
                    "source_row": int(row_number) + 2,
                    "publisher_family": record["publisher_family"],
                    "policy_url": url,
                    "http_status": "",
                    "old_sha256": old_sha,
                    "new_sha256": "",
                    "changed": "",
                    "missing_expected_terms": "",
                    "refresh_status": "fetch_error_retained",
                    "snapshot_path": record.get("snapshot_path", ""),
                    "row_action": "retain_prior_row_unchanged",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            output_rows.append(record)
            if not continue_on_error:
                break
            continue
        output_rows.append(record)

    report = pd.DataFrame(report_rows)
    report.to_csv(run_dir / "publisher_source_drift_report.csv", index=False)
    proposed = pd.DataFrame(output_rows, columns=REQUIRED_COLUMNS)
    proposed.to_csv(run_dir / "publisher_policy_sources.proposed.csv", index=False)

    bounded_partial_allowed = (
        continue_on_error
        and fetch_errors <= max(0, int(max_fetch_errors))
        and successful_fetches > 0
    )
    fetch_policy_ok = fetch_errors == 0 or bounded_partial_allowed
    fetch_policy_detail = (
        "no fetch errors"
        if fetch_errors == 0
        else (
            f"{fetch_errors} error(s) retained unchanged; "
            f"{successful_fetches} successful fetch(es); limit={max_fetch_errors}"
            if bounded_partial_allowed
            else f"{fetch_errors} error(s); successful={successful_fetches}; limit={max_fetch_errors}"
        )
    )
    checks = {
        "publisher sources loaded": (len(frame) > 0, f"{len(frame)} rows"),
        "all rows processed": (len(output_rows) == len(frame), f"{len(output_rows)}/{len(frame)}"),
        "successful source fetches": (successful_fetches > 0, f"{successful_fetches} successful"),
        "fetch-error policy": (fetch_policy_ok, fetch_policy_detail),
        "source URLs unique": (frame["policy_url"].astype(str).is_unique, "unique"),
    }
    ok = gate(checks)
    partial_success = bool(ok and fetch_errors)
    applied = False
    if apply and ok:
        with FileLock(LOCK_PATH, purpose="update_publisher_info"):
            for snapshot, compressed_payload in sorted(staged_snapshots.items(), key=lambda item: str(item[0])):
                if not snapshot.is_file():
                    snapshot.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(snapshot, compressed_payload)
            csv_text = proposed.to_csv(index=False, lineterminator="\n")
            atomic_write_text(sources_path, csv_text)
            atomic_write_json(
                sources_path.with_suffix(sources_path.suffix + ".metadata.json"),
                {
                    "source_file": sources_path.name,
                    "source_sha256": sha256_file(sources_path),
                    "rows": len(proposed),
                    "date_checked": dt.date.today().isoformat(),
                    "successful_fetches": successful_fetches,
                    "changed_pages": changed,
                    "review_required_pages": review_required,
                    "fetch_errors": fetch_errors,
                    "failed_source_rows": failed_rows,
                    "failed_urls": failed_urls,
                    "partial_success": partial_success,
                    "failed_rows_retained_unchanged": bool(fetch_errors),
                },
            )
        applied = True
        if partial_success:
            print("\nPARTIAL PUBLISHER REFRESH APPLIED")
            print(
                f"  Updated {successful_fetches} successfully fetched source row(s); "
                f"retained {fetch_errors} failed row(s) unchanged."
            )
            for row in report_rows:
                if row.get("refresh_status") == "fetch_error_retained":
                    print(f"  - row {row['source_row']}: {row['policy_url']} | {row['error']}")
            print("  Inspect publisher_source_drift_report.csv and retry failed sources later.")
        else:
            print(f"\nUpdated publisher source inventory: {sources_path}")
    elif apply:
        print("\nGate failed; publisher reference files and source snapshots were not changed.")
    else:
        print("\nDRY RUN - inspect publisher_source_drift_report.csv before --apply.")
        if partial_success:
            print(
                f"  A bounded partial apply would update {successful_fetches} source row(s) "
                f"and retain {fetch_errors} failed row(s) unchanged."
            )
    coverage = {
        "source_pages": len(frame),
        "successful_fetches": successful_fetches,
        "changed_pages": changed,
        "review_required_pages": review_required,
        "fetch_errors": fetch_errors,
        "failed_source_rows": failed_rows,
        "failed_urls": failed_urls,
        "partial_success": partial_success,
        "max_fetch_errors": max_fetch_errors,
        "applied": int(applied),
        "source_sha256_before": source_sha256_before,
        "source_sha256_after": sha256_file(sources_path),
    }
    atomic_write_json(run_dir / "coverage.json", coverage)
    ledger_append(
        "update_publisher_info",
        params={
            "sources": sources_path.name,
            "apply": apply,
            "continue_on_error": continue_on_error,
            "max_fetch_errors": max_fetch_errors,
        },
        inputs={"sources": coverage["source_sha256_before"]},
        outputs={**coverage, "report": str(run_dir / "publisher_source_drift_report.csv")},
        gate_passed=ok,
        run_dir=str(run_dir),
    )
    print(f"Publisher-source update run: {run_dir}")
    return ok, coverage, run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", default=str(DEFAULT_SOURCES))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--max-fetch-errors", type=int, default=2)
    args = parser.parse_args()
    ok, _, _ = run(
        sources_path=Path(args.sources).expanduser(),
        apply=args.apply,
        continue_on_error=args.continue_on_error,
        max_fetch_errors=max(0, args.max_fetch_errors),
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
