"""Publish the production NIH Tracker front end to a GitHub Pages directory.

Dry run is the default. Apply mode is production-only, requires the exact
``PUBLISH_PROD_TO_GITHUB`` confirmation, loads authentication from
``GITHUB_TOKEN``, and uses ``GIT_ASKPASS`` so the token never appears in a
command line or remote URL. The script updates only NIH Tracker-managed files
inside the repository's GitHub Pages /docs directory.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable

from common import (
    BUILD,
    ENV,
    PROJECT,
    RUNS,
    FileLock,
    atomic_write_bytes,
    atomic_write_json,
    current_run_id,
    ledger_append,
    new_run_dir,
    sha256_file,
)

CONFIRMATION_TOKEN = "PUBLISH_PROD_TO_GITHUB"
MANAGED_MANIFEST = ".nih_tracker_publish_manifest.json"
PLAN_SEAL_NAME = "publication_plan.sha256"
STATIC_MANAGED_FILES = (
    ".nojekyll",
    "publication_manifest.json",
    MANAGED_MANIFEST,
)
PUBLIC_BUILD_SUBDIRECTORIES = ("data", "schemas")
# A JSONL stream with zero records is represented by a zero-byte file.  That is
# a valid state for change history on the first catalog release or whenever no
# material public field changed.  Other public payloads remain non-empty by
# contract so a truncated catalog still fails closed.
EMPTY_PAYLOAD_ALLOWLIST = frozenset({"data/change_history.jsonl"})
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
KNOWN_SECRET_NAMES = (
    "GITHUB_TOKEN",
    "FS_U",
    "FS_PWD",
    "SCICRUNCH_API_KEY",
    "RRID",
    "FAIRSHARING_API_TOKEN",
    "FAIRSHARING_API_KEY",
)
SENSITIVE_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(rb"Authorization\s*:\s*(?:Basic|Bearer)\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _validate_repository(value: str, *, required: bool) -> str:
    repository = _clean(value)
    if not repository and not required:
        return ""
    if not REPOSITORY_RE.fullmatch(repository):
        raise ValueError(
            "GitHub repository must use owner/repository syntax and contain only "
            "letters, digits, dot, underscore, and hyphen."
        )
    return repository


def _validate_branch(value: str) -> str:
    branch = _clean(value) or "main"
    if (
        not BRANCH_RE.fullmatch(branch)
        or branch.startswith(("/", "-"))
        or branch.endswith(("/", "."))
        or ".." in branch
        or "//" in branch
        or "@{" in branch
    ):
        raise ValueError(f"Unsafe Git branch name: {branch!r}")
    return branch


def _validate_publish_dir(value: str) -> str:
    publish_dir = _clean(value) or "docs"
    # This publisher commits a directory on a branch; GitHub Pages branch sources
    # support only /(root) or /docs. The managed-file protocol intentionally avoids
    # owning repository root, so /docs is the sole supported public destination.
    if publish_dir != "docs":
        raise ValueError(
            "GITHUB_PUBLISH_DIR must be 'docs' for the NIH Tracker GitHub Pages "
            f"publisher; got {publish_dir!r}. Configure Pages for the selected branch /docs."
        )
    return publish_dir


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def scan_public_payload(data: bytes, *, source: Path) -> None:
    """Fail closed when a public payload contains a loaded secret or key marker."""
    for name in KNOWN_SECRET_NAMES:
        value = os.environ.get(name, "").encode("utf-8")
        if len(value) >= 8 and value in data:
            raise RuntimeError(
                f"Refusing publication: {source} contains the loaded value of {name}."
            )
    for pattern in SENSITIVE_PATTERNS:
        if pattern.search(data):
            raise RuntimeError(
                f"Refusing publication: {source} matches sensitive-token pattern {pattern.pattern!r}."
            )


def _publication_files(source: Path) -> list[tuple[str, Path]]:
    """Return the bounded build files included in one public release."""
    build_root = Path(BUILD).resolve()
    files: list[tuple[str, Path]] = [("index.html", source)]
    for directory_name in PUBLIC_BUILD_SUBDIRECTORIES:
        root = build_root / directory_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(build_root).as_posix()
            if any(part in {".git", "__pycache__", ".tmp"} for part in Path(relative).parts):
                continue
            files.append((relative, path.resolve()))
    return files


def build_publication_plan(
    *,
    source: Path,
    repository: str,
    branch: str,
    publish_dir: str,
    run_dir: Path,
) -> Path:
    if ENV != "prod":
        raise RuntimeError("GitHub publication planning is production-only.")
    repository = _validate_repository(repository, required=True)
    branch = _validate_branch(branch)
    publish_dir = _validate_publish_dir(publish_dir)
    run_dir = run_dir.expanduser().resolve()
    try:
        run_dir.relative_to((Path(RUNS) / "publish_github").resolve())
    except ValueError as exc:
        raise ValueError("Publication run directory must be under runs/publish_github") from exc
    run_dir.mkdir(parents=True, exist_ok=True)
    source = source.expanduser().resolve()
    build_root = Path(BUILD).resolve()
    try:
        source.relative_to(build_root)
    except ValueError as exc:
        raise ValueError(f"Publication source must be under {build_root}: {source}") from exc
    if not source.is_file():
        raise FileNotFoundError(source)
    source_data = source.read_bytes()
    if not source_data:
        raise ValueError(f"Publication source is empty: {source}")
    payload_dir = run_dir / "payload"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payload_files: list[dict[str, Any]] = []
    for relative, path in _publication_files(source):
        data = path.read_bytes()
        if not data and relative not in EMPTY_PAYLOAD_ALLOWLIST:
            raise ValueError(f"Public payload is empty: {path}")
        scan_public_payload(data, source=path)
        target = payload_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(target, data)
        digest = _sha256_bytes(data)
        if sha256_file(target) != digest:
            raise RuntimeError(f"Publication payload hash verification failed: {relative}")
        payload_files.append(
            {
                "path": relative,
                "source": str(path),
                "sha256": digest,
                "bytes": len(data),
            }
        )
    payload_files.sort(key=lambda item: item["path"])
    managed_files = [item["path"] for item in payload_files] + list(STATIC_MANAGED_FILES)
    plan = {
        "schema": "nih-tracker-github-publication/v2",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "created_run_id": current_run_id(),
        "environment": ENV,
        "project": str(Path(PROJECT).resolve()),
        "source": str(source),
        "source_sha256": _sha256_bytes(source_data),
        "source_bytes": len(source_data),
        "repository": repository,
        "branch": branch,
        "publish_dir": publish_dir,
        "managed_files": managed_files,
        "payload_root": str(payload_dir),
        "payload_index": str(payload_dir / "index.html"),
        "payload_sha256": _sha256_bytes(source_data),
        "payload_files": payload_files,
        "payload_file_count": len(payload_files),
        "payload_bytes": sum(item["bytes"] for item in payload_files),
        "empty_payload_files": [
            item["path"] for item in payload_files if int(item["bytes"]) == 0
        ],
    }
    plan_path = run_dir / "publication_plan.json"
    atomic_write_json(plan_path, plan)
    plan_sha256 = sha256_file(plan_path) or ""
    atomic_write_bytes(
        run_dir / PLAN_SEAL_NAME,
        f"{plan_sha256}  {plan_path.name}\n".encode("utf-8"),
    )
    print(f"Publication plan: {plan_path}")
    print(f"  plan_sha256={plan_sha256}")
    print(f"  source={source}")
    print(f"  source_sha256={plan['source_sha256']}")
    print(f"  source_bytes={plan['source_bytes']:,}")
    print(f"  payload_files={plan['payload_file_count']}")
    print(f"  payload_bytes={plan['payload_bytes']:,}")
    if plan["empty_payload_files"]:
        print(f"  valid_empty_payloads={', '.join(plan['empty_payload_files'])}")
    print(f"  repository={repository or '[not configured]'}")
    print(f"  branch={branch}")
    print(f"  publish_dir={publish_dir}")
    return plan_path


def load_publication_plan(plan_path: Path) -> dict[str, Any]:
    """Load and structurally validate one sealed production publication plan."""
    plan_path = plan_path.expanduser().resolve()
    if not plan_path.is_file():
        raise FileNotFoundError(plan_path)
    seal_path = plan_path.parent / PLAN_SEAL_NAME
    if not seal_path.is_file():
        raise FileNotFoundError(f"Missing publication plan seal: {seal_path}")
    parts = seal_path.read_text(encoding="utf-8").strip().split()
    expected = parts[0] if parts else ""
    actual = sha256_file(plan_path) or ""
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or expected != actual:
        raise ValueError(
            f"Publication plan seal mismatch: expected={expected!r}, actual={actual!r}"
        )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema") not in {"nih-tracker-github-publication/v1", "nih-tracker-github-publication/v2"}:
        raise ValueError(f"Unsupported publication plan: {plan.get('schema')!r}")
    if plan.get("environment") != "prod":
        raise ValueError("Publication plan environment must be prod")
    if Path(str(plan.get("project", ""))).expanduser().resolve() != Path(PROJECT).resolve():
        raise RuntimeError("Publication plan belongs to a different project root")
    try:
        plan_path.relative_to((Path(RUNS) / "publish_github").resolve())
    except ValueError as exc:
        raise ValueError("Publication plan is outside the production publish run directory") from exc
    _validate_repository(plan.get("repository", ""), required=True)
    _validate_branch(plan.get("branch", ""))
    _validate_publish_dir(plan.get("publish_dir", ""))
    source = Path(str(plan.get("source", ""))).expanduser().resolve()
    try:
        source.relative_to(Path(BUILD).resolve())
    except ValueError as exc:
        raise ValueError("Publication source escapes the production build directory") from exc
    source_hash = _clean(plan.get("source_sha256"))
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ValueError("Publication source SHA-256 is invalid")
    try:
        source_bytes = int(plan.get("source_bytes", -1))
    except (TypeError, ValueError) as exc:
        raise ValueError("Publication source byte count is invalid") from exc
    if source_bytes <= 0:
        raise ValueError("Publication source byte count must be positive")
    if plan.get("schema") == "nih-tracker-github-publication/v1":
        payload = Path(str(plan.get("payload_index", ""))).expanduser().resolve()
        if not payload.is_file():
            raise FileNotFoundError(payload)
        return plan
    payload_root = Path(str(plan.get("payload_root", ""))).expanduser().resolve()
    try:
        payload_root.relative_to((plan_path.parent / "payload").resolve())
    except ValueError as exc:
        raise ValueError("Publication payload root escapes the sealed plan payload directory") from exc
    records = plan.get("payload_files")
    if not isinstance(records, list) or not records:
        raise ValueError("Publication plan has no payload files")
    paths: list[str] = []
    empty_paths: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Publication payload file record must be an object")
        relative = str(record.get("path") or "")
        relative_path = Path(relative)
        if not relative or relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"Unsafe publication payload path: {relative!r}")
        payload = (payload_root / relative_path).resolve()
        try:
            payload.relative_to(payload_root)
        except ValueError as exc:
            raise ValueError(f"Publication payload path escapes root: {relative}") from exc
        if not payload.is_file():
            raise FileNotFoundError(payload)
        expected_hash = str(record.get("sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or sha256_file(payload) != expected_hash:
            raise ValueError(f"Publication payload hash mismatch: {relative}")
        recorded_bytes = int(record.get("bytes", -1))
        if payload.stat().st_size != recorded_bytes:
            raise ValueError(f"Publication payload byte count mismatch: {relative}")
        if recorded_bytes == 0:
            if relative not in EMPTY_PAYLOAD_ALLOWLIST:
                raise ValueError(f"Unexpected empty publication payload: {relative}")
            empty_paths.append(relative)
        paths.append(relative)
    expected_managed = paths + list(STATIC_MANAGED_FILES)
    if plan.get("managed_files") != expected_managed:
        raise ValueError("Publication plan managed-file policy is missing or unexpected")
    if "index.html" not in paths:
        raise ValueError("Publication payload must include index.html")
    if plan.get("empty_payload_files", []) != empty_paths:
        raise ValueError("Publication plan empty-payload inventory is missing or unexpected")
    return plan


def _run_git(
    arguments: Iterable[str],
    *,
    cwd: Path,
    env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = ["git", *[str(value) for value in arguments]]
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"git command failed ({' '.join(command)}):\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    return completed


def _askpass_script(directory: Path) -> Path:
    path = directory / "git-askpass.sh"
    path.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
        "  *Password*) printf '%s\\n' \"$NIH_TRACKER_GITHUB_TOKEN\" ;;\n"
        "  *) printf '%s\\n' '' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path


def _managed_publication_manifest(plan: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic public metadata tied to the sealed plan."""
    return {
        "schema": "nih-tracker-github-pages-payload/v2",
        "published_at": plan["created_at"],
        "run_id": plan["created_run_id"],
        "source_sha256": plan["source_sha256"],
        "source_bytes": plan["source_bytes"],
        "payload_file_count": int(plan.get("payload_file_count") or 1),
        "payload_bytes": int(plan.get("payload_bytes") or plan["source_bytes"]),
        "empty_payload_files": list(plan.get("empty_payload_files") or []),
        "managed_files": plan["managed_files"],
    }


def _safe_remove_managed(destination: Path) -> None:
    manifest_path = destination / MANAGED_MANIFEST
    if not manifest_path.is_file():
        return
    try:
        prior = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = prior.get("files") if isinstance(prior, dict) else []
    except Exception:
        files = []
    for relative in files or []:
        relative_path = Path(str(relative))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            continue
        target = (destination / relative_path).resolve()
        try:
            target.relative_to(destination.resolve())
        except ValueError:
            continue
        if target.is_file() or target.is_symlink():
            target.unlink(missing_ok=True)
    for directory in sorted((path for path in destination.rglob("*") if path.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def apply_publication(
    plan_path: Path,
    *,
    token: str,
    confirm: str,
    remote_url: str | None = None,
) -> dict[str, Any]:
    if ENV != "prod":
        raise RuntimeError("GitHub publication is production-only.")
    if confirm != CONFIRMATION_TOKEN:
        raise RuntimeError(f"Exact confirmation required: {CONFIRMATION_TOKEN}")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required for publication.")
    plan = load_publication_plan(plan_path)
    repository = _validate_repository(plan.get("repository", ""), required=True)
    branch = _validate_branch(plan.get("branch", ""))
    publish_dir = _validate_publish_dir(plan.get("publish_dir", ""))
    source = Path(plan["source"]).resolve()
    if sha256_file(source) != plan.get("source_sha256"):
        raise RuntimeError("Production build changed after publication planning.")
    if source.stat().st_size != int(plan.get("source_bytes", -1)):
        raise RuntimeError("Production build size changed after publication planning.")
    if plan.get("schema") == "nih-tracker-github-publication/v1":
        payload_records = [{"path": "index.html", "payload": str(Path(plan["payload_index"]).resolve())}]
    else:
        root = Path(plan["payload_root"]).resolve()
        payload_records = [
            {"path": record["path"], "payload": str((root / record["path"]).resolve())}
            for record in plan["payload_files"]
        ]
    for record in payload_records:
        payload_path = Path(record["payload"])
        scan_public_payload(payload_path.read_bytes(), source=payload_path)
    remote = remote_url or f"https://github.com/{repository}.git"
    with tempfile.TemporaryDirectory(prefix="nih-tracker-github-") as temporary:
        checkout = Path(temporary) / "checkout"
        checkout.mkdir()
        askpass = _askpass_script(Path(temporary))
        git_env = os.environ.copy()
        git_env.update({"GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0", "NIH_TRACKER_GITHUB_TOKEN": token})
        _run_git(["init", "--initial-branch", branch], cwd=checkout, env=git_env)
        _run_git(["remote", "add", "origin", remote], cwd=checkout, env=git_env)
        branch_probe = _run_git(["ls-remote", "--exit-code", "--heads", "origin", branch], cwd=checkout, env=git_env, check=False)
        if branch_probe.returncode == 0:
            _run_git(["fetch", "--depth", "1", "origin", branch], cwd=checkout, env=git_env)
            _run_git(["checkout", "-B", branch, "FETCH_HEAD"], cwd=checkout, env=git_env)
        elif branch_probe.returncode != 2:
            raise RuntimeError("Unable to inspect the remote branch before publication:\n" + branch_probe.stdout + "\n" + branch_probe.stderr)
        destination = checkout / publish_dir
        destination.mkdir(parents=True, exist_ok=True)
        _safe_remove_managed(destination)
        for record in payload_records:
            relative = Path(record["path"])
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(record["payload"], target)
        (destination / ".nojekyll").write_text("", encoding="utf-8")
        payload_manifest = _managed_publication_manifest(plan)
        (destination / "publication_manifest.json").write_text(json.dumps(payload_manifest, indent=2) + "\n", encoding="utf-8")
        (destination / MANAGED_MANIFEST).write_text(
            json.dumps({"schema": "nih-tracker-managed-files/v2", "files": plan["managed_files"]}, indent=2) + "\n",
            encoding="utf-8",
        )
        _run_git(["config", "user.name", os.getenv("GITHUB_COMMIT_NAME", "NIH Tracker Publisher")], cwd=checkout, env=git_env)
        _run_git(["config", "user.email", os.getenv("GITHUB_COMMIT_EMAIL", "nih-tracker-publisher@users.noreply.github.com")], cwd=checkout, env=git_env)
        _run_git(["add", "--", publish_dir], cwd=checkout, env=git_env)
        diff = _run_git(["diff", "--cached", "--quiet"], cwd=checkout, env=git_env, check=False)
        if diff.returncode == 0:
            commit = _run_git(["rev-parse", "HEAD"], cwd=checkout, env=git_env, check=False)
            return {"status": "no_changes", "repository": repository, "branch": branch, "publish_dir": publish_dir, "commit": commit.stdout.strip() if commit.returncode == 0 else "", "files": len(payload_records)}
        if diff.returncode != 1:
            raise RuntimeError(f"Unable to inspect staged Git changes: {diff.stderr}")
        message = f"Publish NIH Tracker {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')} ({plan['source_sha256'][:12]})"
        _run_git(["commit", "-m", message], cwd=checkout, env=git_env)
        commit_sha = _run_git(["rev-parse", "HEAD"], cwd=checkout, env=git_env).stdout.strip()
        _run_git(["push", "origin", f"HEAD:refs/heads/{branch}"], cwd=checkout, env=git_env)
        return {"status": "published", "repository": repository, "branch": branch, "publish_dir": publish_dir, "commit": commit_sha, "files": len(payload_records)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(Path(BUILD) / "index.html"))
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", ""))
    parser.add_argument("--branch", default=os.getenv("GITHUB_BRANCH", "main"))
    parser.add_argument("--publish-dir", default=os.getenv("GITHUB_PUBLISH_DIR", "docs"))
    parser.add_argument("--plan", default="", help="Existing sealed publication_plan.json to apply.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    if ENV != "prod":
        raise RuntimeError(
            f"GitHub publication is production-only; active environment is {ENV!r}."
        )
    if args.plan:
        plan_path = Path(args.plan).expanduser().resolve()
        plan = load_publication_plan(plan_path)
        if plan.get("environment") != "prod":
            raise RuntimeError("Publication plan was not created from production.")
        repository = _validate_repository(plan.get("repository", ""), required=True)
        branch = _validate_branch(plan.get("branch", ""))
        publish_dir = _validate_publish_dir(plan.get("publish_dir", ""))
        run_dir = plan_path.parent
        if not args.apply:
            print(json.dumps(plan, indent=2))
            print("Existing sealed plan loaded; no repository was changed.")
    else:
        repository = _validate_repository(args.repository, required=True)
        branch = _validate_branch(args.branch)
        publish_dir = _validate_publish_dir(args.publish_dir)
        run_dir = Path(new_run_dir("publish_github"))
        plan_path = build_publication_plan(
            source=Path(args.source),
            repository=repository,
            branch=branch,
            publish_dir=publish_dir,
            run_dir=run_dir,
        )
    outputs: dict[str, Any] = {
        "plan": str(plan_path),
        "source_sha256": (load_publication_plan(plan_path).get("source_sha256") or ""),
        "applied": False,
    }
    if args.apply:
        result = apply_publication(
            plan_path,
            token=os.getenv("GITHUB_TOKEN", ""),
            confirm=args.confirm,
        )
        outputs.update(result)
        outputs["applied"] = result.get("status") in {"published", "no_changes"}
        atomic_write_json(run_dir / "publication_receipt.json", result)
        print(f"GitHub publication status: {result['status']}")
        print(f"Commit: {result.get('commit') or '[none]'}")
    elif not args.plan:
        print("DRY RUN: no repository was cloned and no GitHub branch was changed.")
        print(f"Apply the sealed plan with exact confirmation: {CONFIRMATION_TOKEN}")

    ledger_append(
        "publish_github",
        params={
            "repository": repository,
            "branch": branch,
            "publish_dir": publish_dir,
            "apply": args.apply,
        },
        inputs={"source": str(load_publication_plan(plan_path)["source"])},
        outputs=outputs,
        gate_passed=True,
        run_dir=str(run_dir),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
