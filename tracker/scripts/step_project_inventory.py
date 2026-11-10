"""Inventory the live project and explicitly pin review sources. Offline only."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import os
from pathlib import Path

from run_provenance import PIN_NAME, PIN_SCHEMA, digest, retention_report, protection_reason


def collect_inventory(project: Path, destination: Path) -> dict:
    from common import atomic_write_json, atomic_write_text
    from review_workflow import compute_review_status
    project = project.resolve()
    destination = destination.resolve()
    retention = retention_report(project)
    records = []
    duplicates = {}
    errors = list(retention["errors"])
    for root, dirs, files in os.walk(project, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in {".git", "__pycache__", ".ipynb_checkpoints"}
                         and not (Path(root) / d).is_symlink()
                         and (Path(root) / d).resolve() != destination)
        for name in sorted(files):
            path = Path(root) / name
            rel = path.relative_to(project).as_posix()
            if path.is_symlink():
                records.append({"path": rel, "status": "symlink_not_followed"})
                continue
            # Credential files are never read or fingerprinted by this inventory.
            import re
            if name.startswith(".env") or re.search(r"(?:^|[._-])(secret|credential|token|password|private[_-]?key)s?(?:[._-]|$)", name, re.I):
                records.append({"path": rel, "status": "sensitive_name_excluded"})
                continue
            if name.endswith(".lock"):
                continue
            try:
                before = path.stat()
                sha = digest(path)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise RuntimeError("changed during inventory; rerun with writers stopped")
                records.append({"path": rel, "status": "hashed", "size_bytes": after.st_size,
                                "mtime_ns": after.st_mtime_ns, "sha256": sha})
                if rel.startswith("runs/"):
                    duplicates.setdefault(sha, []).append(rel)
            except (OSError, RuntimeError) as exc:
                errors.append({"path": rel, "error": str(exc)})
    try:
        review = compute_review_status()
    except Exception as exc:
        review = {"state": "UNRESOLVED", "error": str(exc), "queues": []}
        errors.append({"path": "review_sources", "error": str(exc)})
    report = {"schema": "nih-tracker-project-inventory/v1", "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "project": str(project), "files": records, "retention": retention, "review": review,
              "duplicate_run_files": [{"sha256": h, "paths": p} for h, p in sorted(duplicates.items()) if len(p) > 1],
              "errors": errors, "complete": not errors,
              "limitations": ["Inventory describes observed bytes, not a consistent filesystem snapshot unless all writers are stopped.",
                              "Identical file bytes do not establish that run directories or provenance are interchangeable.",
                              "Missing evidence does not establish absent capability."]}
    destination.mkdir(parents=True, exist_ok=False)
    atomic_write_json(destination / "inventory.json", report)
    with (destination / "files.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["path", "status", "size_bytes", "mtime_ns", "sha256"])
        writer.writeheader(); writer.writerows(records)
    atomic_write_text(destination / "index.html", render_report(report))
    atomic_write_text(destination / "SHA256SUMS", "".join(f"{digest(destination / n)}  {n}\n" for n in ("inventory.json", "files.csv", "index.html")))
    return report


def render_report(report: dict) -> str:
    esc = lambda x: html.escape(str(x), quote=True)
    review = report["review"]
    queues = "".join("<tr>" + "".join(f"<td>{esc(r.get(k, ''))}</td>" for k in
                     ("label", "pending_rows", "decided_rows", "run", "selection", "source_sha256")) + "</tr>"
                     for r in review.get("queues", []))
    backlogs = "".join(f"<li>{esc(r['domain'])}: <strong>{esc(r['rows'])}</strong> backlog rows</li>"
                       for r in review.get("non_gating_evidence_backlogs", []))
    protected = "".join(f"<tr><td>{esc(r['path'])}</td><td>{esc('; '.join(r['reasons']))}</td></tr>"
                        for r in report["retention"]["protected"])
    problems = "".join(f"<li>{esc(r['path'])}: {esc(r['error'])}</li>" for r in report["errors"])
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NIH Tracker — project inventory</title><style>
:root{{color-scheme:dark}}body{{margin:0;background:#0d1424;color:#e5edf8;font:16px/1.55 system-ui,sans-serif}}main{{max-width:1320px;margin:auto;padding:40px 28px}}h1{{font-size:34px;letter-spacing:-.03em}}h2{{margin-top:36px}}.muted{{color:#a8b9d3}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:16px}}.card{{background:#18243a;padding:20px;border-radius:12px;border:1px solid #2c3c58}}.number{{font-size:30px;font-weight:700;color:#8bd6cb}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{padding:12px;text-align:left;border-bottom:1px solid #30415d;overflow-wrap:anywhere}}th{{color:#8bd6cb}}.scroll{{overflow:auto}}a{{color:#9cc9ff}}code{{overflow-wrap:anywhere}}.note{{border-left:3px solid #e4ba72;padding:12px 18px;background:#1a2539}}footer{{margin-top:36px;color:#a8b9d3}}details{{margin:22px 0}}</style>
<main><p class="muted">NIH TRACKER · OPERATIONS</p><h1>Project inventory & review provenance</h1>
<p class="muted">Captured {esc(report['captured_at'])}<br><code>{esc(report['project'])}</code></p>
<div class="cards"><div class="card">Review state<div class="number">{esc(review['state'])}</div></div>
<div class="card">Candidate rows pending<div class="number">{esc(review.get('queue_pending_rows', 'Unresolved'))}</div></div>
<div class="card">Protected paths<div class="number">{len(report['retention']['protected'])}</div></div>
<div class="card">Inventory status<div class="number">{'Complete' if report['complete'] else 'Incomplete'}</div></div></div>
<h2>Selected review sources</h2><p>Source SHA-256 identifies the observed queue bytes. A pinned source remains selected until explicitly replaced or unpinned.</p>
<div class="scroll"><table><thead><tr><th>Queue</th><th>Pending</th><th>Decided</th><th>Run</th><th>Selection</th><th>SHA-256</th></tr></thead><tbody>{queues}</tbody></table></div>
<h2>Evidence work remaining</h2><ul>{backlogs}</ul><p class="note">Backlog rows are not repository counts or measured capability deficits. This operational report does not calculate FAIRness scores.</p>
<h2>Retention</h2><p>Structured run artifacts, card transaction history and detected dependencies are retained. Duplicate bytes are diagnostic only. Other cleanup candidates require an exact sealed plan and a verified recovery archive.</p>
<details><summary>Inspect {len(report['retention']['protected'])} protected paths</summary><table><thead><tr><th>Path</th><th>Reason</th></tr></thead><tbody>{protected}</tbody></table></details>
<h2>Data quality and scope</h2><ul>{problems or '<li>No inventory errors recorded.</li>'}</ul>
<p>Stop other writers before capture. Unknown reference formats are not fully dependency-parsed; archiving provides an additional recovery safeguard.</p>
<footer><a href="inventory.json">Full inventory JSON</a> · <a href="files.csv">File hashes CSV</a> · <a href="SHA256SUMS">Report checksums</a><br>This report is for internal operations. Do not publish it with the public catalog.</footer></main></html>'''


def pin_sources(project: Path, *, replace: bool = False) -> dict:
    from common import FileLock, atomic_write_json, new_run_dir
    from review_workflow import current_queue_summary
    target = project / "inputs" / PIN_NAME
    with FileLock(project / ".review_sources.lock", purpose="pin review sources", timeout=0):
        if target.exists() and not replace:
            raise RuntimeError("Pins already exist; inspect or unpin explicitly before selecting new sources")
        rows = current_queue_summary()
        if not rows:
            raise RuntimeError("No review sources to pin")
        sources = {r["family"]: {"path": Path(r["source_file"]).relative_to(project).as_posix(),
                   "sha256": r["source_sha256"], "selection_basis": r["selection"]} for r in rows}
        value = {"schema": PIN_SCHEMA, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "sources": sources}
        audit = Path(new_run_dir("review_source_selection"))
        if target.exists():
            atomic_write_json(audit / "previous.json", json.loads(target.read_text()))
        atomic_write_json(audit / "selected.json", value)
        atomic_write_json(target, value)
        return value


def unpin_sources(project: Path) -> None:
    from common import FileLock, atomic_write_bytes, new_run_dir
    target = project / "inputs" / PIN_NAME
    with FileLock(project / ".review_sources.lock", purpose="unpin review sources", timeout=0):
        if target.exists():
            if target.is_symlink():
                raise ValueError("Pin manifest cannot be a symlink")
            archive = Path(new_run_dir("review_source_selection")) / "previous.json"
            atomic_write_bytes(archive, target.read_bytes())
            target.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inventory", "inspect-review", "pin-review", "unpin-review"])
    parser.add_argument("--out", type=Path)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    from common import PROJECT
    project = Path(PROJECT).resolve()
    if args.action == "inspect-review":
        from run_provenance import load_pins
        from review_workflow import current_queue_summary
        pins = load_pins(project)
        queues = current_queue_summary() if pins else []
        verified = {row["family"]: row for row in queues if row.get("selection") == "pinned"}
        if set(pins) - set(verified):
            raise RuntimeError("Some existing pin families could not be verified; inspect the manifest before changing it")
        print(json.dumps({"status": "pinned" if pins else "not_pinned",
                          "manifest": str(project / "inputs" / PIN_NAME),
                          "sources": list(verified.values()), "changed": False}, indent=2))
        return 0
    if args.action == "inventory":
        import uuid
        out = args.out or project / "runs" / "project_inventory" / (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
        report = collect_inventory(project, out)
        print(f"Report: {out / 'index.html'}\nFiles: {len(report['files'])}\nComplete: {report['complete']}")
        return 0 if report["complete"] else 1
    token = "PIN_REVIEW_SOURCES" if args.action == "pin-review" else "UNPIN_REVIEW_SOURCES"
    if args.confirm != token:
        raise RuntimeError(f"Exact confirmation required: {token}")
    if args.action == "pin-review":
        print(json.dumps(pin_sources(project), indent=2))
    else:
        unpin_sources(project)
        print("Review sources unpinned; previous selection preserved in runs/review_source_selection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
