"""Explicit review sources and conservative retention, without import-time I/O.

All functions take a project path so notebook module reuse cannot bind them to a
previous project. Pins are operational configuration, not human adjudications.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from pathlib import Path

PIN_SCHEMA = "nih-tracker-review-sources/v1"
PIN_NAME = "active_review_sources.json"
FAMILIES = {"identifier", "rrid", "fairsharing", "crosswalk", "card"}
TEXT_SUFFIXES = {".csv", ".json", ".jsonl", ".yaml", ".yml", ".txt", ".md", ".html"}
REFERENCE = re.compile(r"(?<![\w-])((?:runs/[^/\s\"'<>|,;)}\]]+/[^/\s\"'<>|,;)}\]]+)(?:/[^/\s\"'<>|,;)}\]]+)?)")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def source_order(path: Path) -> tuple[int, str]:
    """Keep historical mtime semantics, with a stable full-path tie breaker."""
    return path.stat().st_mtime_ns, path.as_posix()


def load_pins(project: Path) -> dict:
    path = project / "inputs" / PIN_NAME
    if not path.exists():
        return {}
    if path.is_symlink():
        raise ValueError("Review source pins cannot be a symlink")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != PIN_SCHEMA or not isinstance(value.get("sources"), dict):
        raise ValueError("Invalid review source manifest; inspect inputs/active_review_sources.json")
    if set(value["sources"]) - FAMILIES:
        raise ValueError("Unknown review family in source manifest")
    return value["sources"]


def pinned_source(project: Path, family: str, candidates: list[Path]) -> Path | None:
    record = load_pins(project).get(family)
    if record is None:
        return None
    rel = Path(record.get("path", ""))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "runs":
        raise ValueError(f"Unsafe pinned source for {family}")
    target = project / rel
    if any(p.is_symlink() for p in (target, *target.parents)):
        raise ValueError(f"Symlink in pinned source for {family}")
    target = target.resolve()
    target.relative_to(project.resolve())
    if target not in {p.resolve() for p in candidates}:
        raise ValueError(f"Pinned {family} source is missing or outside its queue family: {rel}")
    if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", ""))) or digest(target) != record["sha256"]:
        raise ValueError(f"Pinned {family} source changed: {rel}; review before replacing the pin")
    return target


def retention_report(project: Path) -> dict:
    """Protect review/evidence/audit runs, plus text-referenced run directories.

Unknown formats are not claimed to be fully dependency-parsed. Every removed
candidate must also have a byte-verified archive. Ledger references deliberately
retain historical runs; age is not evidence of dispensability.
"""
    project = project.resolve()
    protected: dict[str, set[str]] = {}
    errors: list[dict] = []
    def protect(path: Path, reason: str) -> None:
        protected.setdefault(str(path.resolve()), set()).add(reason)

    root = project / "runs"
    if root.is_dir():
        for step in sorted(root.iterdir()):
            if not step.is_dir() or step.is_symlink():
                continue
            if step.name == "notebook_debug":
                continue
            for run in sorted(step.iterdir()):
                if not run.is_dir() or run.is_symlink():
                    continue
                # Structured output or captured evidence is an audit record.
                if any(p.suffix.lower() in {".csv", ".json", ".jsonl", ".yaml", ".yml", ".zip", ".xml", ".html", ".pdf"}
                       for p in run.rglob("*") if p.is_file()):
                    protect(run, "structured evidence, review, plan, or audit artifact")
    snapshots = project / "snapshots" / "cards"
    if snapshots.is_dir():
        for path in snapshots.iterdir():
            if path.is_dir():
                protect(path, "card transaction history retained")

    scan_roots = [project / n for n in ("cards", "inputs", "blocks", "latest", "build", "reference")]
    scan_files = [project / "run_ledger.csv"]
    for base in scan_roots:
        if base.is_dir():
            scan_files.extend(p for p in base.rglob("*") if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES)
    for path in scan_files:
        if not path.exists():
            continue
        if path.name.startswith(".env") or re.search(r"(?:^|[._-])(secret|credential|token|password|private[_-]?key)s?(?:[._-]|$)", path.name, re.I):
            continue
        if path.is_symlink():
            errors.append({"path": str(path), "error": "symlink prevents bounded dependency scan"})
            continue
        try:
            # Read bounded chunks; overlap accommodates references split across chunks.
            with path.open(encoding="utf-8", errors="strict") as handle:
                tail = ""
                while chunk := handle.read(1024 * 1024):
                    text = (tail + chunk).replace("\\/", "/")
                    for match in REFERENCE.finditer(text):
                        parts = Path(match.group(1)).parts
                        count = 4 if len(parts) > 1 and parts[1] == "notebook_debug" else 3
                        target = project.joinpath(*parts[:count])
                        if ".." not in parts and target.is_dir():
                            protect(target, "referenced by " + path.relative_to(project).as_posix())
                    tail = text[-8192:]
        except (OSError, UnicodeError) as exc:
            errors.append({"path": str(path), "error": str(exc)})
    return {"protected": [{"path": p, "reasons": sorted(reasons)} for p, reasons in sorted(protected.items())],
            "errors": errors, "policy": "retain structured runs, transaction snapshots and known dependencies; archive other candidates before removal"}


def protection_reason(path: Path, report: dict) -> list[str]:
    path = path.resolve()
    reasons = set()
    for row in report["protected"]:
        target = Path(row["path"])
        if path == target or path in target.parents or target in path.parents:
            reasons.update(row["reasons"])
    return sorted(reasons)


def archive_candidates(project: Path, candidates: list[dict], destination: Path) -> dict:
    """Write a new archive, then re-read every member and verify SHA-256.

    No removals occur here. Symlinks and special files fail closed. The caller
    rechecks fingerprints after this function and before deleting anything.
    """
    import stat
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"schema": "nih-tracker-cleanup-archive/v1", "files": [], "directories": []}
    files: dict[str, Path] = {}
    directories = set()
    for row in candidates:
        base = Path(row["path"])
        paths = [base, *base.rglob("*")] if base.is_dir() else [base]
        for path in paths:
            rel = path.relative_to(project).as_posix()
            mode = path.lstat().st_mode
            if path.is_symlink() or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError(f"Cannot archive symlink or special file: {rel}")
            if path.is_dir():
                directories.add(rel)
            else:
                files[rel] = path
    manifest["directories"] = sorted(directories)
    for rel, path in sorted(files.items()):
        manifest["files"].append({"path": rel, "sha256": digest(path), "size_bytes": path.stat().st_size})
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for name in sorted(directories):
            z.writestr(name + "/", b"")
        for rel, path in sorted(files.items()):
            z.write(path, rel)
        z.writestr("CLEANUP_ARCHIVE_MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
    with zipfile.ZipFile(destination) as z:
        for row in manifest["files"]:
            h = hashlib.sha256()
            with z.open(row["path"]) as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    h.update(block)
            if h.hexdigest() != row["sha256"]:
                raise RuntimeError("Archive verification failed; no source may be removed")
    return {"path": str(destination), "sha256": digest(destination), "files": len(files), "verified": True}
