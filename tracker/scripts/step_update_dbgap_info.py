"""Refresh the pinned dbGaP study inventory from a CSV or the official FHIR API.

The updater preserves the 16-column NIH Tracker study-export contract.  In FHIR
mode it follows Bundle ``next`` links and maps ResearchStudy resources into the
same canonical columns.  Dry run is the default; ``--apply`` validates and
atomically replaces ``reference/dbGaP_Studies.csv`` with a metadata sidecar and
content-addressed response snapshot.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
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

FHIR_ENDPOINT = "https://dbgap-api.ncbi.nlm.nih.gov/fhir/x1/ResearchStudy?_count=1000"
REFERENCE = Path(PROJECT) / "reference"
DEFAULT_DESTINATION = REFERENCE / "dbGaP_Studies.csv"
LOCK_PATH = REFERENCE / ".dbgap_update.lock"
COLUMNS = [
    "accession", "name", "description", "Parent study", "Study Disease/Focus",
    "Study Design", "Study Markerset", "Study Molecular Data Type", "Study Content",
    "Ancestry (computed)", "NIH Institute", "Study Consent", "Release Date",
    "Embargo Release Date", "Related Terms", "Collections",
]


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or value.get("display") or value.get("value") or "").strip()
    return str(value or "").strip()


def _first(values: Any) -> str:
    if not isinstance(values, list):
        values = [values] if values else []
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _codes(values: Any) -> str:
    if not isinstance(values, list):
        values = [values] if values else []
    output: list[str] = []
    for item in values:
        if not isinstance(item, dict):
            text = _text(item)
            if text:
                output.append(text)
            continue
        text = _text(item)
        if not text:
            for coding in item.get("coding", []) or []:
                text = _text(coding)
                if text:
                    break
        if text:
            output.append(text)
    return "; ".join(dict.fromkeys(output))


def _extension_map(resource: dict[str, Any]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for extension in resource.get("extension", []) or []:
        if not isinstance(extension, dict):
            continue
        url = str(extension.get("url") or "").rstrip("/").rsplit("/", 1)[-1].casefold()
        value: Any = ""
        for key, candidate in extension.items():
            if key.startswith("value"):
                value = candidate
                break
        if url:
            result.setdefault(url, []).append(value)
    return result


def _identifier(resource: dict[str, Any]) -> str:
    identifiers = resource.get("identifier", []) or []
    for item in identifiers:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or "").strip()
        if value.lower().startswith("phs"):
            return value
    value = str(resource.get("id") or "").strip()
    return value


def _resource_row(resource: dict[str, Any]) -> dict[str, str]:
    ext = _extension_map(resource)
    title = str(resource.get("title") or resource.get("name") or "").strip()
    description = str(resource.get("description") or "").strip()
    conditions = _codes(resource.get("condition", []))
    keywords = [str(value).strip() for value in resource.get("keyword", []) or [] if str(value).strip()]

    def e(*keys: str) -> str:
        for key in keys:
            values = ext.get(key.casefold(), [])
            value = _first(values)
            if value:
                return value
        return ""

    period = resource.get("period") if isinstance(resource.get("period"), dict) else {}
    row = {
        "accession": _identifier(resource),
        "name": title,
        "description": description,
        "Parent study": e("parent-study", "parentstudy"),
        "Study Disease/Focus": conditions or e("study-disease-focus", "disease-focus"),
        "Study Design": e("study-design", "design"),
        "Study Markerset": e("study-markerset", "markerset"),
        "Study Molecular Data Type": e("study-molecular-data-type", "molecular-data-type"),
        "Study Content": e("study-content", "content"),
        "Ancestry (computed)": e("ancestry-computed", "ancestry"),
        "NIH Institute": e("nih-institute", "institute"),
        "Study Consent": e("study-consent", "consent"),
        "Release Date": e("release-date", "releasedate") or str(period.get("start") or ""),
        "Embargo Release Date": e("embargo-release-date", "embargoreleasedate"),
        "Related Terms": "; ".join(dict.fromkeys(keywords)),
        "Collections": e("collections", "collection"),
    }
    return {column: str(row.get(column, "") or "") for column in COLUMNS}


class FHIRPayloadError(RuntimeError):
    """A transport succeeded but the response could not be decoded as JSON."""

    def __init__(self, message: str, *, payload: bytes, details: dict[str, Any]):
        super().__init__(message)
        self.payload = payload
        self.details = details


def _decode_json_payload(
    data: bytes,
    *,
    declared_charset: str = "",
    content_encoding: str = "",
) -> tuple[dict[str, Any], str, bool]:
    """Decode a dbGaP FHIR JSON payload using its declared charset.

    The dbGaP service has returned ISO-8859-1 payloads in production. JSON is
    normally UTF-8, but forcing UTF-8 makes otherwise valid catalogue pages
    fail on Latin-1 characters. The declared HTTP charset is authoritative;
    bounded fallbacks cover missing or inaccurate headers.
    """
    parse_bytes = data
    decompressed = False
    if "gzip" in content_encoding.casefold() or data.startswith(b"\x1f\x8b"):
        parse_bytes = gzip.decompress(data)
        decompressed = True

    candidates: list[str] = []
    for candidate in (declared_charset, "utf-8-sig", "utf-8", "iso-8859-1"):
        normalized = str(candidate or "").strip().casefold()
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    failures: list[str] = []
    for encoding in candidates:
        try:
            text = parse_bytes.decode(encoding)
        except (LookupError, UnicodeDecodeError) as exc:
            failures.append(f"{encoding}: {type(exc).__name__}: {exc}")
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            failures.append(f"{encoding}: JSONDecodeError: {exc}")
            continue
        if not isinstance(parsed, dict):
            failures.append(f"{encoding}: top-level JSON value is {type(parsed).__name__}, not object")
            continue
        return parsed, encoding, decompressed

    raise ValueError("; ".join(failures) or "No usable response encoding")


def _fetch_json(url: str) -> tuple[dict[str, Any], str, bytes, dict[str, Any]]:
    request = Request(
        url,
        headers={
            "Accept": "application/fhir+json, application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "NIH-Tracker/17.1",
        },
    )
    with urlopen(request, timeout=120) as response:
        data = response.read()
        final_url = response.geturl()
        headers = response.headers
        content_type = str(headers.get("Content-Type") or "")
        content_encoding = str(headers.get("Content-Encoding") or "")
        try:
            declared_charset = str(headers.get_content_charset() or "")
        except AttributeError:
            declared_charset = ""
    details = {
        "content_type": content_type,
        "content_encoding": content_encoding,
        "declared_charset": declared_charset,
        "payload_bytes": len(data),
        "payload_sha256": hashlib.sha256(data).hexdigest(),
    }
    try:
        parsed, used_encoding, decompressed = _decode_json_payload(
            data,
            declared_charset=declared_charset,
            content_encoding=content_encoding,
        )
    except Exception as exc:
        raise FHIRPayloadError(
            f"Could not decode dbGaP FHIR response from {final_url}: {exc}",
            payload=data,
            details={**details, "requested_url": url, "final_url": final_url},
        ) from exc
    details.update({"used_encoding": used_encoding, "decompressed": decompressed})
    return parsed, final_url, data, details


def _fhir_export(endpoint: str) -> tuple[pd.DataFrame, list[dict[str, Any]], list[bytes]]:
    rows: list[dict[str, str]] = []
    pages: list[dict[str, Any]] = []
    payloads: list[bytes] = []
    next_url = endpoint
    seen: set[str] = set()
    while next_url:
        if next_url in seen:
            raise RuntimeError(f"FHIR pagination cycle detected: {next_url}")
        seen.add(next_url)
        bundle, final_url, payload, fetch_details = _fetch_json(next_url)
        payloads.append(payload)
        entries = bundle.get("entry", []) or []
        for entry in entries:
            resource = entry.get("resource") if isinstance(entry, dict) else None
            if isinstance(resource, dict) and resource.get("resourceType") == "ResearchStudy":
                rows.append(_resource_row(resource))
        pages.append(
            {
                "requested_url": next_url,
                "final_url": final_url,
                "entries": len(entries),
                **fetch_details,
            }
        )
        next_url = ""
        for link in bundle.get("link", []) or []:
            if isinstance(link, dict) and str(link.get("relation") or "").casefold() == "next":
                next_url = urljoin(final_url, str(link.get("url") or "").strip())
                break
    frame = pd.DataFrame(rows, columns=COLUMNS)
    return frame, pages, payloads


def _local_export(path: Path) -> tuple[pd.DataFrame, list[dict[str, Any]], list[bytes]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    data = path.read_bytes()
    frame = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False).fillna("")
    missing = [column for column in COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"dbGaP CSV lacks canonical columns: {missing}")
    return frame[COLUMNS].copy(), [{"source_file": str(path.resolve()), "entries": len(frame)}], [data]


def _production_guard(apply: bool) -> None:
    if apply and ENV == "prod" and os.environ.get("NIH_TRACKER_PROD_WRITE_CONFIRMATION") != PROD_WRITE_CONFIRMATION_TOKEN:
        raise RuntimeError(
            "Production reference writes require "
            f"NIH_TRACKER_PROD_WRITE_CONFIRMATION={PROD_WRITE_CONFIRMATION_TOKEN!r}."
        )


def run(*, mode: str, source_file: Path | None, endpoint: str, destination: Path, minimum_rows: int, maximum_drop_fraction: float, apply: bool) -> tuple[bool, dict[str, Any], Path]:
    _production_guard(apply)
    run_dir = Path(new_run_dir("update_dbgap_info"))
    source = endpoint if mode == "fhir" else str(source_file.resolve()) if source_file else ""
    try:
        if mode == "fhir":
            frame, pages, payloads = _fhir_export(endpoint)
        else:
            if source_file is None:
                raise ValueError("--source-file is required in csv mode")
            frame, pages, payloads = _local_export(source_file)
            source = str(source_file.resolve())
    except Exception as exc:
        failure_snapshot = ""
        failure_sha256 = ""
        details: dict[str, Any] = {}
        if isinstance(exc, FHIRPayloadError):
            failure_sha256 = hashlib.sha256(exc.payload).hexdigest()
            failure_path = run_dir / f"failed_source_response_{failure_sha256}.bin.gz"
            atomic_write_bytes(failure_path, gzip.compress(exc.payload, mtime=0))
            failure_snapshot = str(failure_path)
            details = exc.details
        failure = {
            "schema": "nih-tracker-dbgap-update-error/v1",
            "source": source,
            "mode": mode,
            "destination": str(destination),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "response_sha256": failure_sha256,
            "response_snapshot": failure_snapshot,
            "response_details": details,
            "apply_requested": apply,
            "applied": False,
            "gate_passed": False,
            "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        atomic_write_json(run_dir / "update_error.json", failure)
        atomic_write_text(run_dir / "traceback.txt", traceback.format_exc())
        ledger_append(
            "update_dbgap_info",
            params={"mode": mode, "source": source, "destination": str(destination), "apply": apply},
            inputs={"response": failure_sha256 or None},
            outputs={"applied": 0, "error_report": str(run_dir / "update_error.json")},
            gate_passed=False,
            run_dir=str(run_dir),
        )
        print("\nDBGaP UPDATE FAILED")
        print(f"  {type(exc).__name__}: {exc}")
        print(f"  Error report: {run_dir / 'update_error.json'}")
        if failure_snapshot:
            print(f"  Exact failed response snapshot: {failure_snapshot}")
        print(f"dbGaP update run: {run_dir}")
        return False, failure, run_dir
    frame = frame.fillna("").astype(str)
    old_rows = len(pd.read_csv(destination, dtype=str, keep_default_na=False)) if destination.is_file() else 0
    floor = max(minimum_rows, int(old_rows * (1.0 - maximum_drop_fraction))) if old_rows else minimum_rows
    accessions = frame["accession"].astype(str).str.strip()
    checks = {
        "canonical schema": (list(frame.columns) == COLUMNS, f"{len(frame.columns)} columns"),
        "row count above safety floor": (len(frame) >= floor, f"{len(frame)} rows; floor={floor}"),
        "accessions populated": ((accessions != "").all(), f"{int((accessions == '').sum())} blank"),
        "accessions unique": (accessions.is_unique, f"{accessions.nunique()} unique"),
        "study names populated": ((frame["name"].astype(str).str.strip() != "").all(), "all populated"),
    }
    ok = gate(checks)
    candidate = run_dir / "dbGaP_Studies.candidate.csv"
    csv_bytes = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    atomic_write_bytes(candidate, csv_bytes)
    combined = b"\n--FHIR-PAGE--\n".join(payloads)
    response_sha = hashlib.sha256(combined).hexdigest()
    snapshot = run_dir / f"source_response_{response_sha}.bin.gz"
    atomic_write_bytes(snapshot, gzip.compress(combined, mtime=0))
    release_dates = pd.to_datetime(frame["Release Date"], errors="coerce")
    report = {
        "source": source,
        "mode": mode,
        "candidate_rows": len(frame),
        "candidate_sha256": sha256_file(candidate),
        "previous_rows": old_rows,
        "safety_floor": floor,
        "latest_release_date": str(release_dates.max().date()) if release_dates.notna().any() else "",
        "page_count": len(pages),
        "pages": pages,
        "response_sha256": response_sha,
        "response_snapshot": str(snapshot),
        "destination": str(destination),
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "apply_requested": apply,
        "gate_passed": ok,
    }
    applied = False
    if apply and ok:
        with FileLock(LOCK_PATH, purpose="update_dbgap_info"):
            atomic_write_bytes(destination, csv_bytes)
            metadata = {**report, "destination_sha256": sha256_file(destination), "applied": True}
            atomic_write_json(destination.with_suffix(destination.suffix + ".metadata.json"), metadata)
        applied = True
        print(f"\nUpdated dbGaP study snapshot: {destination}")
    elif apply:
        print("\nGate failed; the dbGaP snapshot was not changed.")
    else:
        print("\nDRY RUN - inspect the candidate export and source snapshot before --apply.")
    report["applied"] = applied
    atomic_write_json(run_dir / "update_report.json", report)
    ledger_append(
        "update_dbgap_info",
        params={"mode": mode, "source": source, "destination": str(destination), "apply": apply},
        inputs={"candidate": report["candidate_sha256"], "response": response_sha},
        outputs={"rows": len(frame), "applied": int(applied), "report": str(run_dir / "update_report.json")},
        gate_passed=ok,
        run_dir=str(run_dir),
    )
    print(f"dbGaP update run: {run_dir}")
    return ok, report, run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["csv", "fhir"], default="fhir")
    parser.add_argument("--source-file", default="")
    parser.add_argument("--endpoint", default=FHIR_ENDPOINT)
    parser.add_argument("--destination", default=str(DEFAULT_DESTINATION))
    parser.add_argument("--minimum-rows", type=int, default=1000)
    parser.add_argument("--maximum-drop-fraction", type=float, default=0.20)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    ok, _, _ = run(
        mode=args.mode,
        source_file=Path(args.source_file).expanduser() if args.source_file else None,
        endpoint=args.endpoint,
        destination=Path(args.destination).expanduser(),
        minimum_rows=max(1, args.minimum_rows),
        maximum_drop_fraction=min(max(args.maximum_drop_fraction, 0.0), 0.95),
        apply=args.apply,
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
