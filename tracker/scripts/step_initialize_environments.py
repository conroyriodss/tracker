"""Install or refresh NIH Tracker release code.

In v19.0.2 production notebooks set ``NIH_TRACKER_PRODUCTION_ONLY=1``. In that
mode this command inspects and updates only ``/content/drive/MyDrive/NIH_Tracker``
and never scans, stages, or recreates dev/staging project data. The legacy
multi-environment branch remains available only for backward-compatible tests
and older command lines.

Dry run is the default. ``missing-only`` never overwrites an existing file.
``refresh-code`` may replace release-managed scripts, notebooks, references,
templates, and documentation, while preserving cards, inputs, runs, snapshots,
builds, locks, caches, and credentials.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from common import (
    ENV,
    PRODUCTION_ONLY,
    PROD_WRITE_CONFIRMATION_TOKEN,
    VALID_ENVS,
    FileLock,
    atomic_write_bytes,
    atomic_write_json,
    card_transaction,
    configured_project_roots,
    current_run_id,
    ledger_append,
    project_for_env,
    run_ts,
    sha256_file,
)

CONFIRMATION_PREFIX = "INITIALIZE_DEV_STAGING_PROD_FROM_"
PRODUCTION_CONFIRMATION = "INSTALL_PRODUCTION_RELEASE"
PLAN_SCHEMA = "nih-tracker-environment-initialization/v1"
PLAN_SEAL_NAME = "environment_initialization_plan.sha256"
DATA_DIRECTORIES = ("cards", "inputs", "blocks")
LAYOUT_DIRECTORIES = ("cards", "inputs", "blocks", "scripts", "templates", "reference", "runs", "latest", "build", "snapshots")
RELEASE_FILE_PATTERNS = (
    "nih_tracker_colab_v19_*.ipynb",
    "frontend.html",
    "build_notebooks.py",
    "README.md",
    "RELEASE_NOTES.md",
    "REVISION_SUMMARY.md",
    "HOTFIX_NOTES.md",
    "MIGRATION_AND_OPERATIONS.md",
    "INTEROPERABILITY_EVIDENCE_MODEL.md",
    "RESOURCE_PROFILE_EVIDENCE_MODEL.md",
    "POLICY_CONTEXT_EVIDENCE_MODEL.md",
    "REPOSITORY_STATUS_EVIDENCE_MODEL.md",
    "PUBLISHER_POLICY_EVIDENCE_MODEL.md",
    "RELEASE_MANIFEST.json",
    "VALIDATION_REPORT.txt",
    "nih_tracker_v17_1_to_v18.diff",
    "nih_tracker_v18_1_1_to_v18_2.diff",
    "nih_tracker_v18_2_to_v18_3.diff",
    "nih_tracker_v18_4_to_v18_5.diff",
    "nih_tracker_v18_5_to_v19.diff",
    "nih_tracker_v19_to_v19_0_1.diff",
    "nih_tracker_v19_0_1_to_v19_0_2.diff",
    "PRODUCTION_ONLY_OPERATIONS.md",
    "AUTOMATED_REVIEW_EXCHANGE.md",
    "KNOWLEDGE_GRAPH_MODEL.md",
    "PUBLIC_CATALOG_METHODS.md",
    "GCP_INTEGRATION_AND_ASSESSMENT.md",
    "GCP_SEMIAUTOMATED_REVIEW.md",
    "ASSESSMENT_CRITERIA.md",
    "requirements-gcp.txt",
    "extend_gcp_notebooks.py",
    "extend_operations_notebooks.py",
    "OPERATIONS_AND_VERSIONING.md",
    "UPSTREAM_SOURCE.json",
    ".gitignore",
    ".gitattributes",
    "SHA256SUMS",
    "requirements.txt",
    "pyproject.toml",
)
RELEASE_DIRECTORIES = (
    ("scripts", "release_code"),
    ("tests", "release_test"),
    ("templates", "release_template"),
    ("reference", "release_reference"),
    ("schemas", "release_schema"),
)
SOURCE_TOP_LEVEL_FILES: tuple[str, ...] = ()
DENIED_PARTS = {"__pycache__", ".ipynb_checkpoints", ".git", ".tmp"}
DENIED_FILE_RE = re.compile(
    r"(?:^|[._-])(secret|secrets|credential|credentials|token|password|passwd|api[_-]?key|apikey|private[_-]?key)(?:[._-]|$)",
    re.I,
)


def release_overwrite_allowed(category: str, relative: Path) -> bool:
    if category == "release_reference":
        # Operational source snapshots and curated mappings can be newer than
        # the distribution. Only versioned code-owned instruments are refreshed.
        return relative.as_posix() in {
            "reference/catalog_vocabularies_v1.json",
            "reference/capability_assessment_rubric_v1.json",
            "reference/README.md",
        }
    return category.startswith("release_")


@dataclass(frozen=True)
class SourceFile:
    category: str
    relative_path: str
    source_path: str
    sha256: str
    size: int
    overwrite_allowed: bool
    data: bytes = field(repr=False, compare=False)


def _safe_file(path: Path, relative: Path) -> bool:
    if any(part in DENIED_PARTS for part in relative.parts):
        return False
    name = path.name
    if name == ".lock" or name.endswith(".lock") or ".tmp." in name or ".old." in name:
        return False
    if name == ".env" or name.startswith(".env.") or DENIED_FILE_RE.search(name):
        return False
    return path.is_file() and not path.is_symlink()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _release_root(value: str | None) -> Path:
    if value:
        root = Path(value).expanduser().resolve()
    else:
        root = Path(__file__).resolve().parents[1]
    if not (root / "scripts" / Path(__file__).name).is_file():
        raise FileNotFoundError(
            f"Release root does not contain scripts/{Path(__file__).name}: {root}"
        )
    return root


def collect_release_files(release_root: Path) -> list[SourceFile]:
    files: list[SourceFile] = []
    for directory_name, category in RELEASE_DIRECTORIES:
        directory_root = release_root / directory_name
        if not directory_root.is_dir():
            continue
        for path in sorted(directory_root.rglob("*")):
            if not path.is_file():
                continue
            relative_inside = path.relative_to(directory_root)
            if not _safe_file(path, relative_inside):
                continue
            data = path.read_bytes()
            files.append(
                SourceFile(
                    category=category,
                    relative_path=(Path(directory_name) / relative_inside).as_posix(),
                    source_path=str(path),
                    sha256=_sha256(data),
                    size=len(data),
                    overwrite_allowed=release_overwrite_allowed(category, Path(directory_name) / relative_inside),
                    data=data,
                )
            )
    seen: set[Path] = set()
    patterns = list(RELEASE_FILE_PATTERNS)
    # Legacy multi-environment installations retain the stable v18 notebook
    # aliases used by older operational tests. Production-only v19 installs
    # publish only the v19 notebook names.
    if not PRODUCTION_ONLY:
        patterns.append("nih_tracker_colab_v18_*.ipynb")
    for pattern in patterns:
        for path in sorted(release_root.glob(pattern)):
            if path in seen or not _safe_file(path, Path(path.name)):
                continue
            seen.add(path)
            data = path.read_bytes()
            files.append(
                SourceFile(
                    category="release_artifact",
                    relative_path=path.name,
                    source_path=str(path),
                    sha256=_sha256(data),
                    size=len(data),
                    overwrite_allowed=True,
                    data=data,
                )
            )
    return files


def collect_project_files(source_root: Path) -> list[SourceFile]:
    files: list[SourceFile] = []
    for directory_name in DATA_DIRECTORIES:
        root = source_root / directory_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source_root)
            if not _safe_file(path, relative):
                continue
            data = path.read_bytes()
            files.append(
                SourceFile(
                    category=f"project_{directory_name}",
                    relative_path=relative.as_posix(),
                    source_path=str(path),
                    sha256=_sha256(data),
                    size=len(data),
                    overwrite_allowed=False,
                    data=data,
                )
            )
    for name in SOURCE_TOP_LEVEL_FILES:
        path = source_root / name
        if _safe_file(path, Path(name)):
            data = path.read_bytes()
            files.append(
                SourceFile(
                    category="project_static",
                    relative_path=name,
                    source_path=str(path),
                    sha256=_sha256(data),
                    size=len(data),
                    overwrite_allowed=False,
                    data=data,
                )
            )
    return files


def _action_for(target: Path, source: SourceFile, mode: str) -> str:
    if not target.exists():
        return "add"
    current_hash = sha256_file(target) if target.is_file() else None
    if current_hash == source.sha256:
        return "unchanged"
    if mode == "refresh-code" and source.overwrite_allowed:
        return "update"
    return "preserve_existing"


def build_plan(
    *,
    source_env: str,
    release_root: Path,
    mode: str,
    run_dir: Path,
) -> Path:
    if source_env not in VALID_ENVS:
        raise ValueError(f"Invalid source environment: {source_env!r}")
    if ENV != source_env:
        raise RuntimeError(
            f"Active environment is {ENV!r}; initialization source is {source_env!r}"
        )
    if mode not in {"missing-only", "refresh-code"}:
        raise ValueError(f"Invalid initialization mode: {mode!r}")
    release_root = release_root.expanduser().resolve()
    run_dir = run_dir.expanduser().resolve()
    roots = {name: Path(project_for_env(name)).resolve() for name in VALID_ENVS}
    source_root = roots[source_env]
    if not source_root.is_dir():
        raise FileNotFoundError(f"Source environment root does not exist: {source_root}")
    run_step = "install_release" if PRODUCTION_ONLY else "initialize_environments"
    try:
        run_dir.relative_to((source_root / "runs" / run_step).resolve())
    except ValueError as exc:
        raise ValueError(
            f"Release run directory must be under {source_root / 'runs' / run_step}"
        ) from exc
    run_dir.mkdir(parents=True, exist_ok=True)

    release_files = collect_release_files(release_root)
    if PRODUCTION_ONLY:
        # A routine production release refresh must not walk, hash, or stage
        # hundreds of card/input files. Those are operational state and are
        # deliberately outside the release payload.
        project_files = []
    else:
        source_cards = source_root / "cards"
        # Compatibility branch for legacy environment seeding.
        if source_cards.is_dir():
            with FileLock(
                source_cards / ".lock",
                purpose=f"environment seed snapshot from {source_env}",
                timeout=0,
            ):
                project_files = collect_project_files(source_root)
        else:
            project_files = collect_project_files(source_root)
    source_files = [*release_files, *project_files]
    if not release_files:
        raise RuntimeError(f"No release code found under {release_root / 'scripts'}")
    relative_paths = [source.relative_path for source in source_files]
    duplicates = sorted({path for path in relative_paths if relative_paths.count(path) > 1})
    if duplicates:
        raise RuntimeError(f"Duplicate initialization source paths: {duplicates[:20]}")

    payload_root = run_dir / "payload"
    payload_records: dict[str, dict[str, str]] = {}
    for source in source_files:
        payload_path = payload_root / source.relative_path
        atomic_write_bytes(payload_path, source.data)
        payload_hash = sha256_file(payload_path) or ""
        if payload_hash != source.sha256:
            raise RuntimeError(f"Initialization payload hash mismatch: {payload_path}")
        payload_records[source.relative_path] = {
            "payload_relative_path": payload_path.relative_to(run_dir).as_posix(),
            "payload_sha256": payload_hash,
        }

    entries: list[dict[str, Any]] = []
    for target_env, target_root in roots.items():
        for source in source_files:
            target = target_root / source.relative_path
            action = (
                "source_environment"
                if target_env == source_env and source.category.startswith("project_")
                else _action_for(target, source, mode)
            )
            entries.append(
                {
                    "target_env": target_env,
                    "target_root": str(target_root),
                    "category": source.category,
                    "relative_path": source.relative_path,
                    "source_path": source.source_path,
                    "source_sha256": source.sha256,
                    "source_size": source.size,
                    "overwrite_allowed": source.overwrite_allowed,
                    **payload_records[source.relative_path],
                    "target_before_sha256": sha256_file(target) or "",
                    "action": action,
                }
            )
    plan = {
        "schema": PLAN_SCHEMA,
        "production_only": PRODUCTION_ONLY,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "created_run_id": current_run_id(),
        "active_env": ENV,
        "source_env": source_env,
        "source_root": str(source_root),
        "release_root": str(release_root),
        "mode": mode,
        "configured_roots": {name: str(path) for name, path in roots.items()},
        "entry_count": len(entries),
        "entries": entries,
        "excluded": [
            "runs/", "latest/", "build/", "snapshots/", "run_ledger.csv",
            "issues_log.csv", "id_remap.csv", "locks", "temporary files",
            "caches", "credentials and secret-like filenames",
        ],
    }
    plan_path = run_dir / "environment_initialization_plan.json"
    atomic_write_json(plan_path, plan)
    plan_sha256 = sha256_file(plan_path) or ""
    atomic_write_bytes(
        run_dir / PLAN_SEAL_NAME,
        f"{plan_sha256}  {plan_path.name}\n".encode("utf-8"),
    )
    print(f"{'Production release plan' if PRODUCTION_ONLY else 'Initialization plan'}: {plan_path}")
    print(f"  plan_sha256={plan_sha256}")
    counts: dict[str, int] = {}
    for entry in entries:
        key = f"{entry['target_env']}:{entry['action']}"
        counts[key] = counts.get(key, 0) + 1
    for key in sorted(counts):
        print(f"  {key:<32} {counts[key]}")
    print(f"  source files: {len(source_files)}")
    return plan_path


def _safe_relative_path(value: Any, *, label: str) -> Path:
    text = str(value or "").strip()
    path = Path(text)
    if not text or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe {label}: {text!r}")
    return path


def _validate_plan(plan_path: Path) -> dict[str, Any]:
    """Validate the sealed plan, path scope, environment mapping, and entry policy."""
    plan_path = plan_path.expanduser().resolve()
    if not plan_path.is_file():
        raise FileNotFoundError(plan_path)
    seal_path = plan_path.parent / PLAN_SEAL_NAME
    if not seal_path.is_file():
        raise FileNotFoundError(f"Missing initialization plan seal: {seal_path}")
    parts = seal_path.read_text(encoding="utf-8").strip().split()
    expected = parts[0] if parts else ""
    actual = sha256_file(plan_path) or ""
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or expected != actual:
        raise ValueError(
            f"Initialization plan seal mismatch: expected={expected!r}, actual={actual!r}"
        )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError(f"Unsupported initialization plan: {plan.get('schema')!r}")

    source_env = str(plan.get("source_env", ""))
    if source_env not in VALID_ENVS:
        raise ValueError(f"Invalid initialization source environment: {source_env!r}")
    if plan.get("active_env") != source_env or ENV != source_env:
        raise RuntimeError(
            f"Initialization plan source is {source_env!r}, but active environment is {ENV!r}"
        )
    if plan.get("mode") not in {"missing-only", "refresh-code"}:
        raise ValueError(f"Invalid initialization mode: {plan.get('mode')!r}")

    current_roots = {name: str(Path(project_for_env(name)).resolve()) for name in VALID_ENVS}
    if plan.get("configured_roots") != current_roots:
        raise RuntimeError("Environment root mapping changed after plan creation")
    source_root = Path(current_roots[source_env]).resolve()
    if Path(str(plan.get("source_root", ""))).expanduser().resolve() != source_root:
        raise RuntimeError("Initialization source root changed after plan creation")
    plan_production_only = bool(plan.get("production_only"))
    if plan_production_only != PRODUCTION_ONLY:
        raise RuntimeError(
            "Production-only mode changed after plan creation; create a new release plan."
        )
    run_step = "install_release" if plan_production_only else "initialize_environments"
    try:
        plan_path.relative_to((source_root / "runs" / run_step).resolve())
    except ValueError as exc:
        raise ValueError(
            f"Initialization plan is outside the source {run_step} run directory"
        ) from exc

    release_root = Path(str(plan.get("release_root", ""))).expanduser().resolve()
    if not (release_root / "scripts" / Path(__file__).name).is_file():
        raise FileNotFoundError(
            f"Initialization release root no longer contains scripts/{Path(__file__).name}: "
            f"{release_root}"
        )

    entries = plan.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("Initialization plan entries must be a list")
    if int(plan.get("entry_count", -1)) != len(entries):
        raise ValueError("Initialization plan entry_count mismatch")

    allowed_categories = {
        "release_code",
        "release_test",
        "release_template",
        "release_reference",
        "release_schema",
        "release_artifact",
        "project_cards",
        "project_inputs",
        "project_blocks",
        "project_static",
    }
    allowed_actions = {"add", "update", "unchanged", "preserve_existing", "source_environment"}
    seen_targets: set[tuple[str, str]] = set()
    coverage: dict[str, set[str]] = {}
    source_identity: dict[str, tuple[str, str, str, int, bool]] = {}

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"Initialization entry {index} is not an object")
        target_env = str(entry.get("target_env", ""))
        if target_env not in VALID_ENVS:
            raise ValueError(f"Entry {index} has invalid target environment: {target_env!r}")
        expected_target_root = Path(current_roots[target_env]).resolve()
        if Path(str(entry.get("target_root", ""))).expanduser().resolve() != expected_target_root:
            raise RuntimeError(f"Entry {index} target root does not match {target_env}")

        relative = _safe_relative_path(entry.get("relative_path"), label="initialization relative path")
        relative_text = relative.as_posix()
        key = (target_env, relative_text)
        if key in seen_targets:
            raise ValueError(f"Duplicate initialization target: {target_env}:{relative_text}")
        seen_targets.add(key)
        coverage.setdefault(relative_text, set()).add(target_env)

        category = str(entry.get("category", ""))
        action = str(entry.get("action", ""))
        if category not in allowed_categories:
            raise ValueError(f"Entry {index} has invalid category: {category!r}")
        if action not in allowed_actions:
            raise ValueError(f"Entry {index} has invalid action: {action!r}")
        if not isinstance(entry.get("overwrite_allowed"), bool):
            raise ValueError(f"Entry {index} overwrite policy must be boolean")
        overwrite_allowed = entry["overwrite_allowed"]
        if release_overwrite_allowed(category, relative) != overwrite_allowed:
            raise ValueError(f"Entry {index} has inconsistent overwrite policy")
        if category == "release_code" and (not relative.parts or relative.parts[0] != "scripts"):
            raise ValueError(f"Release-code entry is outside scripts/: {relative_text}")
        if category == "release_test" and (not relative.parts or relative.parts[0] != "tests"):
            raise ValueError(f"Release-test entry is outside tests/: {relative_text}")
        if category == "release_template" and (not relative.parts or relative.parts[0] != "templates"):
            raise ValueError(f"Release-template entry is outside templates/: {relative_text}")
        if category == "release_reference" and (not relative.parts or relative.parts[0] != "reference"):
            raise ValueError(f"Release-reference entry is outside reference/: {relative_text}")
        if category == "release_schema" and (not relative.parts or relative.parts[0] != "schemas"):
            raise ValueError(f"Release-schema entry is outside schemas/: {relative_text}")
        if category == "project_cards" and (not relative.parts or relative.parts[0] != "cards"):
            raise ValueError(f"Project-card entry is outside cards/: {relative_text}")
        if relative.parts and relative.parts[0] == "cards" and category != "project_cards":
            raise ValueError(f"Only project_cards may target cards/: {relative_text}")
        if action == "source_environment" and not (
            target_env == source_env and category.startswith("project_")
        ):
            raise ValueError(f"Invalid source_environment action for {target_env}:{relative_text}")
        if action == "update" and not (
            plan["mode"] == "refresh-code" and overwrite_allowed
        ):
            raise ValueError(f"Unsafe update action for {target_env}:{relative_text}")

        source_path = Path(str(entry.get("source_path", ""))).expanduser().resolve()
        expected_source_base = release_root if category.startswith("release_") else source_root
        try:
            source_path.relative_to(expected_source_base)
        except ValueError as exc:
            raise ValueError(
                f"Entry source escapes its allowed root: {target_env}:{relative_text}"
            ) from exc

        payload_relative = _safe_relative_path(
            entry.get("payload_relative_path"), label="initialization payload path"
        )
        payload_path = (plan_path.parent / payload_relative).resolve()
        expected_payload = (plan_path.parent / "payload" / relative).resolve()
        if payload_path != expected_payload:
            raise ValueError(f"Unexpected payload mapping for {target_env}:{relative_text}")

        source_hash = str(entry.get("source_sha256", ""))
        payload_hash = str(entry.get("payload_sha256", ""))
        target_before_hash = str(entry.get("target_before_sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise ValueError(f"Invalid source hash for {target_env}:{relative_text}")
        if payload_hash != source_hash:
            raise ValueError(f"Payload/source hash mismatch for {target_env}:{relative_text}")
        if target_before_hash and not re.fullmatch(r"[0-9a-f]{64}", target_before_hash):
            raise ValueError(f"Invalid target-before hash for {target_env}:{relative_text}")
        try:
            source_size = int(entry.get("source_size", -1))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid source size for {target_env}:{relative_text}") from exc
        if source_size < 0:
            raise ValueError(f"Invalid source size for {target_env}:{relative_text}")
        if action == "add" and target_before_hash:
            raise ValueError(f"Add entry already has target content: {target_env}:{relative_text}")
        if action == "update" and not target_before_hash:
            raise ValueError(f"Update entry has no target before-image: {target_env}:{relative_text}")

        identity = (
            category,
            str(source_path),
            source_hash,
            source_size,
            overwrite_allowed,
        )
        prior = source_identity.setdefault(relative_text, identity)
        if prior != identity:
            raise ValueError(f"Source identity differs across targets for {relative_text}")

    required_coverage = set(VALID_ENVS)
    incomplete = sorted(path for path, targets in coverage.items() if targets != required_coverage)
    if incomplete:
        raise ValueError(f"Initialization source paths lack all target environments: {incomplete[:20]}")
    return plan


def apply_plan(plan_path: Path, *, confirm: str) -> dict[str, Any]:
    plan = _validate_plan(plan_path)
    source_env = str(plan["source_env"])
    required = (
        PRODUCTION_CONFIRMATION
        if bool(plan.get("production_only"))
        else CONFIRMATION_PREFIX + source_env.upper()
    )
    if confirm != required:
        raise RuntimeError(f"Exact confirmation required: {required}")
    mode = str(plan["mode"])
    entries = [entry for entry in plan["entries"] if entry["action"] in {"add", "update"}]
    results: list[dict[str, Any]] = []

    by_env: dict[str, list[dict[str, Any]]] = {name: [] for name in VALID_ENVS}
    for entry in entries:
        by_env[str(entry["target_env"])].append(entry)

    for target_env in VALID_ENVS:
        target_root = Path(plan["configured_roots"][target_env]).resolve()
        target_root.mkdir(parents=True, exist_ok=True)
        for directory in LAYOUT_DIRECTORIES:
            (target_root / directory).mkdir(parents=True, exist_ok=True)
        env_entries = by_env[target_env]
        if not env_entries:
            continue
        root_lock = FileLock(
            target_root / ".environment_init.lock",
            purpose=f"initialize {target_env} from {source_env}",
            timeout=0,
            environment=target_env,
            project=target_root,
        )
        prior_prod = os.environ.get("NIH_TRACKER_PROD_WRITE_CONFIRMATION")
        if target_env == "prod":
            os.environ["NIH_TRACKER_PROD_WRITE_CONFIRMATION"] = PROD_WRITE_CONFIRMATION_TOKEN
        try:
            with root_lock:
                non_card_entries = [
                    entry for entry in env_entries if not entry["relative_path"].startswith("cards/")
                ]
                card_entries = [
                    entry for entry in env_entries if entry["relative_path"].startswith("cards/")
                ]
                # Validate every source, immutable payload, and target before the
                # first write. This turns a plan into an optimistic transaction.
                for entry in env_entries:
                    source = Path(entry["source_path"])
                    target = target_root / entry["relative_path"]
                    payload = plan_path.parent / entry["payload_relative_path"]
                    if sha256_file(source) != entry["source_sha256"]:
                        raise RuntimeError(f"Source changed after planning: {source}")
                    if sha256_file(payload) != entry["payload_sha256"]:
                        raise RuntimeError(f"Initialization payload changed: {payload}")
                    current = sha256_file(target) or ""
                    if current != entry["target_before_sha256"]:
                        raise RuntimeError(f"Target changed after planning: {target}")
                    if target.exists() and not target.is_file():
                        raise RuntimeError(f"Initialization target is not a regular file: {target}")

                before_images: list[tuple[Path, bytes | None]] = []
                target_results = [{**entry, "result": "written"} for entry in env_entries]
                manifest_dir = target_root / "bootstrap" / (
                    f"{run_ts()}_from_{source_env}_{uuid.uuid4().hex[:8]}"
                )
                receipt_path = manifest_dir / "environment_initialization_receipt.json"
                receipt = {
                    "schema": plan["schema"],
                    "applied_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "source_env": source_env,
                    "target_env": target_env,
                    "mode": mode,
                    "plan": str(plan_path.resolve()),
                    "files_written": len(env_entries),
                    "results": target_results,
                }
                try:
                    for entry in non_card_entries:
                        target = target_root / entry["relative_path"]
                        before_images.append(
                            (target, target.read_bytes() if target.is_file() else None)
                        )
                        payload = plan_path.parent / entry["payload_relative_path"]
                        atomic_write_bytes(target, payload.read_bytes())
                        if sha256_file(target) != entry["source_sha256"]:
                            raise RuntimeError(f"Copied file hash mismatch: {target}")

                    if card_entries:
                        with card_transaction(
                            f"initialize_{target_env}_from_{source_env}",
                            project_root=target_root,
                            cards_dir=target_root / "cards",
                        ) as transaction:
                            for entry in card_entries:
                                target = target_root / entry["relative_path"]
                                payload = plan_path.parent / entry["payload_relative_path"]
                                transaction.write_bytes(
                                    target,
                                    payload.read_bytes(),
                                    operation=f"environment_initialization:{entry['action']}",
                                )
                            transaction.write_text(
                                receipt_path,
                                json.dumps(receipt, indent=2, default=str) + "\n",
                                operation="environment_initialization_receipt",
                            )
                    else:
                        atomic_write_json(receipt_path, receipt)
                except Exception:
                    rollback_failures: list[str] = []
                    for target, before in reversed(before_images):
                        try:
                            if before is None:
                                target.unlink(missing_ok=True)
                            else:
                                atomic_write_bytes(target, before)
                        except Exception as rollback_exc:
                            rollback_failures.append(f"{target}: {rollback_exc}")
                    if receipt_path.exists():
                        receipt_path.unlink(missing_ok=True)
                    try:
                        if manifest_dir.exists() and not any(manifest_dir.iterdir()):
                            manifest_dir.rmdir()
                    except OSError:
                        pass
                    if rollback_failures:
                        raise RuntimeError(
                            "Environment initialization failed and non-card rollback was incomplete: "
                            + "; ".join(rollback_failures)
                        )
                    raise
                results.extend(target_results)
        finally:
            if prior_prod is None:
                os.environ.pop("NIH_TRACKER_PROD_WRITE_CONFIRMATION", None)
            else:
                os.environ["NIH_TRACKER_PROD_WRITE_CONFIRMATION"] = prior_prod

    summary = {
        "schema": plan["schema"],
        "status": "committed",
        "source_env": source_env,
        "mode": mode,
        "files_written": len(results),
        "by_environment": {
            name: sum(row["target_env"] == name for row in results) for name in VALID_ENVS
        },
    }
    atomic_write_json(plan_path.parent / "environment_initialization_receipt.json", summary)
    if bool(plan.get("production_only")):
        print(f"Production release installed/refreshed: {summary['by_environment'].get('prod', 0)} file(s)")
    else:
        print(f"Initialized environments: {summary['by_environment']}")
    print(f"Files written: {summary['files_written']}")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-env", choices=VALID_ENVS, default="prod")
    parser.add_argument("--mode", choices=("missing-only", "refresh-code"), default="missing-only")
    parser.add_argument("--release-root", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    if ENV != args.source_env:
        raise RuntimeError(
            f"Active environment is {ENV!r}; run initialization with NIH_TRACKER_ENV={args.source_env!r}."
        )
    source_root = Path(project_for_env(args.source_env)).resolve()
    run_root = source_root / "runs" / ("install_release" if PRODUCTION_ONLY else "initialize_environments")
    run_dir = run_root / f"{run_ts()}_{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    release_root = _release_root(args.release_root or None)
    plan_path = build_plan(
        source_env=args.source_env,
        release_root=release_root,
        mode=args.mode,
        run_dir=run_dir,
    )
    outputs: dict[str, Any] = {"plan": str(plan_path), "applied": False}
    if args.apply:
        summary = apply_plan(plan_path, confirm=args.confirm)
        outputs.update(summary)
        outputs["applied"] = True
    else:
        token = PRODUCTION_CONFIRMATION if PRODUCTION_ONLY else CONFIRMATION_PREFIX + args.source_env.upper()
        print("DRY RUN: no release files were changed.")
        print(f"Apply with exact confirmation: {token}")
    ledger_append(
        "install_release" if PRODUCTION_ONLY else "initialize_environments",
        params={
            "source_env": args.source_env,
            "mode": args.mode,
            "release_root": str(release_root),
            "apply": args.apply,
        },
        inputs={"source_root": str(source_root)},
        outputs=outputs,
        gate_passed=True,
        run_dir=str(run_dir),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
