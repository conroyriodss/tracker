"""Shared helpers for NIH Tracker step scripts.

The module centralizes environment-aware project resolution, transaction-safe
card mutation, run/ledger helpers, and a deduplicated issue registry.
"""
from __future__ import annotations

import csv
import datetime as dt
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import socket
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Optional

import pandas as pd
import yaml


# ---------------------------------------------------------------------------
# Environment and project layout
# ---------------------------------------------------------------------------
# v19.0.2 notebooks run in a single production environment.  The compatibility
# branch remains available for isolated tests and older command-line tooling,
# but production notebooks set NIH_TRACKER_PRODUCTION_ONLY=1 before importing
# this module.
PRODUCTION_ONLY = str(os.environ.get("NIH_TRACKER_PRODUCTION_ONLY", "")).strip().lower() in {
    "1", "true", "yes", "y", "on"
}
VALID_ENVS = ("prod",) if PRODUCTION_ONLY else ("dev", "staging", "prod")
DEFAULT_PROJECT_ROOTS = (
    {"prod": "/content/drive/MyDrive/NIH_Tracker"}
    if PRODUCTION_ONLY
    else {
        "dev": "/content/drive/MyDrive/NIH_Tracker_dev",
        "staging": "/content/drive/MyDrive/NIH_Tracker_staging",
        "prod": "/content/drive/MyDrive/NIH_Tracker",
    }
)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize_env(value: Optional[str]) -> str:
    env = (value or ("prod" if PRODUCTION_ONLY else "dev")).strip().lower()
    if env not in VALID_ENVS:
        raise RuntimeError(
            f"Invalid NIH_TRACKER_ENV={value!r}; expected one of {', '.join(VALID_ENVS)}."
        )
    return env


def configured_project_roots() -> dict[str, str]:
    """Return every environment root after applying explicit overrides."""
    return {
        env: str(
            Path(
                os.environ.get(f"NIH_TRACKER_ROOT_{env.upper()}")
                or DEFAULT_PROJECT_ROOTS[env]
            )
            .expanduser()
            .resolve()
        )
        for env in VALID_ENVS
    }


def _assert_distinct_project_roots() -> dict[str, str]:
    roots = configured_project_roots()
    by_path: dict[str, list[str]] = {}
    for env, root in roots.items():
        by_path.setdefault(root, []).append(env)
    collisions = {root: envs for root, envs in by_path.items() if len(envs) > 1}
    if collisions:
        detail = "; ".join(
            f"{root} is mapped by {','.join(envs)}"
            for root, envs in sorted(collisions.items())
        )
        raise RuntimeError(
            "NIH Tracker environment roots must be distinct; refusing an unsafe "
            f"configuration: {detail}"
        )
    return roots


def project_for_env(env: str) -> str:
    """Return the configured project root for an explicit environment.

    Per-environment overrides are supported through NIH_TRACKER_ROOT_DEV,
    NIH_TRACKER_ROOT_STAGING, and NIH_TRACKER_ROOT_PROD. This function ignores
    the legacy NIH_TRACKER variable so promotion code cannot accidentally map
    both source and target to the same ad-hoc path.
    """
    normalized = _normalize_env(env)
    return _assert_distinct_project_roots()[normalized]


def _infer_env_from_legacy_path(path: str, roots: Mapping[str, str]) -> str:
    resolved = str(Path(path).expanduser().resolve())
    for env, root in roots.items():
        if resolved == root:
            return env
    name = Path(path).expanduser().name.lower()
    if name == "nih_tracker":
        return "prod"
    if name.endswith("_staging"):
        return "staging"
    return "dev"


def _resolve_environment_and_project() -> tuple[str, str]:
    roots = _assert_distinct_project_roots()
    explicit_env = os.environ.get("NIH_TRACKER_ENV")
    legacy_project = os.environ.get("NIH_TRACKER")

    if explicit_env:
        env = _normalize_env(explicit_env)
        mapped = roots[env]
        if legacy_project:
            legacy = str(Path(legacy_project).expanduser().resolve())
            if legacy != mapped:
                if _truthy(os.environ.get("NIH_TRACKER_ALLOW_PROJECT_OVERRIDE")):
                    aliases = [name for name, root in roots.items() if root == legacy]
                    if aliases:
                        raise RuntimeError(
                            "NIH_TRACKER_ALLOW_PROJECT_OVERRIDE cannot alias another "
                            f"configured environment root ({','.join(aliases)}): {legacy}"
                        )
                    mapped = legacy
                else:
                    raise RuntimeError(
                        "NIH_TRACKER conflicts with NIH_TRACKER_ENV. Select the environment "
                        "with NIH_TRACKER_ENV and configure its root with "
                        f"NIH_TRACKER_ROOT_{env.upper()}, or explicitly set "
                        "NIH_TRACKER_ALLOW_PROJECT_OVERRIDE=1 for a controlled custom run."
                    )
        return env, mapped

    # Backward-compatible migration path for existing CLI invocations. New
    # notebooks set NIH_TRACKER_ENV explicitly and never depend on this branch.
    if legacy_project:
        root = str(Path(legacy_project).expanduser().resolve())
        return _infer_env_from_legacy_path(root, roots), root

    if PRODUCTION_ONLY:
        return "prod", roots["prod"]
    env = "dev"  # compatibility default outside production-only notebooks
    return env, roots[env]


ENV, PROJECT = _resolve_environment_and_project()
os.environ.setdefault("NIH_TRACKER_ENV", ENV)
# Preserve compatibility for child processes and older scripts after the safe
# resolution above has completed.
os.environ["NIH_TRACKER"] = PROJECT

INP = os.path.join(PROJECT, "inputs")
CARDS = os.path.join(PROJECT, "cards")
LATEST = os.path.join(PROJECT, "latest")
RUNS = os.path.join(PROJECT, "runs")
BUILD = os.path.join(PROJECT, "build")
SNAPSHOTS = os.path.join(PROJECT, "snapshots")
LEDGER = os.path.join(PROJECT, "run_ledger.csv")
ISSUES_LOG = os.path.join(PROJECT, "issues_log.csv")
ID_REMAP_CSV = os.path.join(PROJECT, "id_remap.csv")
VERIFY_ADJUDICATIONS_CSV = os.path.join(INP, "verify_adjudications.csv")

SHARED_CACHE_ROOT = str(
    Path(
        os.environ.get(
            "NIH_TRACKER_SHARED_CACHE_ROOT",
            "/content/drive/MyDrive/NIH_Tracker_shared/cache",
        )
    ).expanduser()
)
RE3DATA_CATALOG_CACHE = str(
    Path(
        os.environ.get(
            "NIH_TRACKER_RE3DATA_CACHE",
            os.path.join(SHARED_CACHE_ROOT, "re3data_catalog_cache.json"),
        )
    ).expanduser()
)
RE3DATA_CACHE_READ_ONLY = _truthy(
    os.environ.get("NIH_TRACKER_RE3DATA_CACHE_READ_ONLY", ENV != "prod")
)
RESOURCE_PROFILE_CACHE_READ_ONLY = _truthy(
    os.environ.get("NIH_TRACKER_RESOURCE_PROFILE_CACHE_READ_ONLY", ENV != "prod")
)
CREDENTIALS_ALLOWED = ENV == "prod" or _truthy(
    os.environ.get("NIH_TRACKER_ALLOW_NONPROD_CREDENTIALS")
)
PROD_WRITE_CONFIRMATION_TOKEN = "ALLOW_PROD_CARD_WRITES"

for directory in (INP, CARDS, LATEST, RUNS, BUILD, SNAPSHOTS):
    os.makedirs(directory, exist_ok=True)

NOW = dt.datetime.now()
_PROCESS_RUN_ID = f"pid-{os.getpid()}-{NOW.strftime('%Y%m%dT%H%M%S%f')}"


def environment_summary() -> dict[str, Any]:
    return {
        "env": ENV,
        "project": PROJECT,
        "credentials_allowed": CREDENTIALS_ALLOWED,
        "re3data_cache": RE3DATA_CATALOG_CACHE,
        "re3data_cache_read_only": RE3DATA_CACHE_READ_ONLY,
        "resource_profile_cache_read_only": RESOURCE_PROFILE_CACHE_READ_ONLY,
        "configured_roots": configured_project_roots(),
        "production_only": PRODUCTION_ONLY,
    }


# ---------------------------------------------------------------------------
# General atomic I/O and file locking
# ---------------------------------------------------------------------------
class LockError(RuntimeError):
    """Raised when an exclusive lock cannot be acquired safely."""


class FileLock:
    """A small O_EXCL-based lock file with auditable owner metadata.

    Locks fail closed. They are never broken automatically, even when old;
    stale-lock removal must be a deliberate maintenance action.
    """

    def __init__(
        self,
        path: os.PathLike[str] | str,
        *,
        purpose: str,
        timeout: float = 0.0,
        poll_interval: float = 0.25,
        environment: Optional[str] = None,
        project: os.PathLike[str] | str | None = None,
    ) -> None:
        self.path = Path(path)
        self.purpose = purpose
        self.timeout = max(0.0, float(timeout))
        self.poll_interval = max(0.05, float(poll_interval))
        self.acquired = False
        self.owner = {
            "lock_id": uuid.uuid4().hex,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "purpose": purpose,
            "env": environment or ENV,
            "project": str(Path(project).expanduser().resolve()) if project else PROJECT,
            "acquired_at": dt.datetime.now().isoformat(timespec="seconds"),
            "run_id": current_run_id(),
        }

    def _existing_metadata(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            try:
                return {"raw": self.path.read_text(encoding="utf-8")[:1000]}
            except Exception:
                return {"raw": "unreadable"}

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        payload = (json.dumps(self.owner, indent=2, sort_keys=True) + "\n").encode("utf-8")
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self.acquired = True
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    metadata = self._existing_metadata()
                    raise LockError(
                        f"Lock already held at {self.path}. Existing owner metadata: "
                        f"{json.dumps(metadata, sort_keys=True)}"
                    )
                time.sleep(self.poll_interval)

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            # Do not remove a lock that has been replaced by another owner.
            existing = self._existing_metadata()
            if existing.get("lock_id") == self.owner["lock_id"]:
                self.path.unlink(missing_ok=True)
            else:
                raise LockError(
                    f"Refusing to release lock {self.path}: owner metadata changed while held."
                )
        finally:
            self.acquired = False

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def current_run_id() -> str:
    return os.environ.get("NIH_TRACKER_RUN_ID") or _PROCESS_RUN_ID


def _fsync_directory(path: Path) -> None:
    """Best-effort directory fsync; unsupported filesystems are tolerated."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: os.PathLike[str] | str, data: bytes, *, mode: int = 0o644) -> None:
    """Write bytes to a same-directory temporary file and atomically replace."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = f".{destination.name}.tmp."
    fd, tmp_name = tempfile.mkstemp(prefix=prefix, dir=str(destination.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp_path, mode)
        except OSError:
            pass
        os.replace(tmp_path, destination)
        _fsync_directory(destination.parent)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(
    path: os.PathLike[str] | str,
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: os.PathLike[str] | str, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, default=str, ensure_ascii=False) + "\n")


def atomic_write_yaml(path: os.PathLike[str] | str, value: Any) -> None:
    text = yaml.safe_dump(value, sort_keys=False, allow_unicode=True, default_flow_style=False)
    atomic_write_text(path, text)


def _rows_to_csv_text(rows: Iterable[Mapping[str, Any]], fieldnames: list[str]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in fieldnames})
    return buffer.getvalue()


def atomic_write_csv_rows(
    path: os.PathLike[str] | str,
    rows: Iterable[Mapping[str, Any]],
    fieldnames: list[str],
) -> None:
    atomic_write_text(path, _rows_to_csv_text(rows, fieldnames))


def sha1(path: os.PathLike[str] | str | None, buf: int = 1 << 20) -> Optional[str]:
    """Return a short content hash for a file or deterministic directory tree."""
    if not path or not os.path.exists(path):
        return None
    source = Path(path)
    digest = hashlib.sha1()
    if source.is_dir():
        for child in sorted(value for value in source.rglob("*") if value.is_file()):
            rel = child.relative_to(source)
            if rel.name == ".lock" or any(part == ".tmp" for part in rel.parts):
                continue
            digest.update(rel.as_posix().encode("utf-8"))
            digest.update(b"\0")
            with child.open("rb") as handle:
                for chunk in iter(lambda: handle.read(buf), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    else:
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(buf), b""):
                digest.update(chunk)
    return digest.hexdigest()[:12]


def sha256_file(path: os.PathLike[str] | str | None, buf: int = 1 << 20) -> Optional[str]:
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(buf), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Transaction-safe card mutation and rollback
# ---------------------------------------------------------------------------
_CARD_UID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ACTIVE_CARD_TRANSACTIONS: dict[str, "CardTransaction"] = {}


def _validate_card_uid(uid: Any) -> str:
    value = str(uid or "").strip()
    if not _CARD_UID_RE.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"Unsafe card UID: {uid!r}")
    return value


def card_path(uid: Any, *, cards_dir: os.PathLike[str] | str | None = None) -> str:
    value = _validate_card_uid(uid)
    return os.path.join(str(cards_dir or CARDS), f"{value}.yaml")


class CardTransaction:
    """Exclusive, snapshot-backed transaction for card and coupled project files.

    Every first-touch is copied to snapshots/cards/<transaction>/before/. Writes
    use os.replace from a same-directory temporary file. If an exception occurs,
    or an explicit transaction is not committed, all touched paths are restored
    to their before-images in reverse order.
    """

    def __init__(
        self,
        step: str,
        *,
        project_root: os.PathLike[str] | str = PROJECT,
        cards_dir: os.PathLike[str] | str = CARDS,
        snapshot: bool = True,
        auto_commit: bool = True,
        lock_timeout: Optional[float] = None,
    ) -> None:
        self.step = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(step or "card_write"))
        self.project_root = Path(project_root).expanduser().resolve()
        self.cards_dir = Path(cards_dir).expanduser().resolve()
        root_matches = [
            env
            for env, root in _assert_distinct_project_roots().items()
            if self.project_root == Path(root).expanduser().resolve()
        ]
        if root_matches:
            self.environment = root_matches[0]
        elif self.project_root == Path(PROJECT).expanduser().resolve():
            self.environment = ENV
        else:
            self.environment = "custom"
        if not snapshot:
            raise ValueError(
                "Card transactions require snapshot=True so rollback and restore evidence "
                "cannot be disabled."
            )
        self.snapshot_enabled = True
        self.auto_commit = bool(auto_commit)
        self.lock_timeout = (
            float(os.environ.get("NIH_TRACKER_LOCK_TIMEOUT_SECONDS", "0"))
            if lock_timeout is None
            else float(lock_timeout)
        )
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")
        self.transaction_id = f"{stamp}_{self.step}_{uuid.uuid4().hex[:8]}"
        self.snapshot_dir = self.project_root / "snapshots" / "cards" / self.transaction_id
        self.before_dir = self.snapshot_dir / "before"
        self.manifest_path = self.snapshot_dir / "manifest.json"
        self.lock = FileLock(
            self.cards_dir / ".lock",
            purpose=f"card transaction: {self.step}",
            timeout=self.lock_timeout,
            environment=self.environment,
            project=self.project_root,
        )
        self.started_at = dt.datetime.now().isoformat(timespec="seconds")
        self.completed_at = ""
        self._touch_order: list[str] = []
        self._touches: dict[str, dict[str, Any]] = {}
        self._operations: list[dict[str, Any]] = []
        self._entered = False
        self._finished = False
        self._committed = False
        self._rolled_back = False
        self._rollback_error = ""
        self._auxiliary_locks: dict[str, FileLock] = {}
        self._auxiliary_lock_order: list[str] = []

    @property
    def key(self) -> str:
        return str(self.cards_dir)

    def acquire_auxiliary_lock(
        self,
        path: os.PathLike[str] | str,
        *,
        purpose: str,
        timeout: float = 5.0,
    ) -> FileLock:
        """Acquire and retain a coupled-file lock until transaction close.

        Retaining the lock prevents a successful external write from occurring
        after this transaction's before-image was taken but before a later
        rollback, which would otherwise erase that external update.
        """
        if not self._entered or self._finished:
            raise RuntimeError("CardTransaction is not active.")
        key = str(Path(path).expanduser().resolve())
        existing = self._auxiliary_locks.get(key)
        if existing is not None:
            return existing
        lock = FileLock(
            key,
            purpose=purpose,
            timeout=timeout,
            environment=self.environment,
            project=self.project_root,
        )
        lock.acquire()
        self._auxiliary_locks[key] = lock
        self._auxiliary_lock_order.append(key)
        return lock

    def _relative_to_project(self, path: Path) -> Path:
        resolved = path.expanduser().resolve()
        try:
            return resolved.relative_to(self.project_root)
        except ValueError as exc:
            raise ValueError(
                f"Transaction path {resolved} is outside project root {self.project_root}."
            ) from exc

    def _snapshot_path(self, path: Path, operation: str) -> None:
        resolved = path.expanduser().resolve()
        key = str(resolved)
        if key in self._touches:
            self._touches[key]["operations"].append(operation)
            return

        rel = self._relative_to_project(resolved)
        existed = resolved.is_file()
        before_sha = sha256_file(resolved)
        backup = self.before_dir / rel
        if self.snapshot_enabled and existed:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(resolved, backup)
        record = {
            "path": str(resolved),
            "relative_path": rel.as_posix(),
            "existed_before": existed,
            "before_sha256": before_sha or "",
            "backup_path": str(backup) if existed and self.snapshot_enabled else "",
            "operations": [operation],
        }
        self._touches[key] = record
        self._touch_order.append(key)

    def write_bytes(self, path: os.PathLike[str] | str, data: bytes, *, operation: str = "write") -> None:
        if not self._entered or self._finished:
            raise RuntimeError("CardTransaction is not active.")
        target = Path(path)
        self._snapshot_path(target, operation)
        atomic_write_bytes(target, data)
        self._operations.append({"operation": operation, "path": str(target.resolve())})

    def write_text(self, path: os.PathLike[str] | str, text: str, *, operation: str = "write") -> None:
        self.write_bytes(path, text.encode("utf-8"), operation=operation)

    def write_yaml(self, path: os.PathLike[str] | str, value: Any, *, operation: str = "write_yaml") -> None:
        text = yaml.safe_dump(
            value,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )
        self.write_text(path, text, operation=operation)

    def delete(self, path: os.PathLike[str] | str, *, missing_ok: bool = False, operation: str = "delete") -> None:
        if not self._entered or self._finished:
            raise RuntimeError("CardTransaction is not active.")
        target = Path(path)
        if not target.exists():
            if missing_ok:
                return
            raise FileNotFoundError(target)
        if not target.is_file():
            raise IsADirectoryError(target)
        self._snapshot_path(target, operation)
        target.unlink()
        _fsync_directory(target.parent)
        self._operations.append({"operation": operation, "path": str(target.resolve())})

    def move(
        self,
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
        *,
        overwrite: bool = False,
        operation: str = "move",
    ) -> None:
        if not self._entered or self._finished:
            raise RuntimeError("CardTransaction is not active.")
        src, dst = Path(source), Path(destination)
        if not src.is_file():
            raise FileNotFoundError(src)
        if dst.exists() and not overwrite:
            raise FileExistsError(f"Refusing to overwrite transaction destination: {dst}")
        self._snapshot_path(src, f"{operation}:source")
        self._snapshot_path(dst, f"{operation}:destination")
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        _fsync_directory(src.parent)
        if dst.parent != src.parent:
            _fsync_directory(dst.parent)
        self._operations.append(
            {"operation": operation, "source": str(src.resolve()), "destination": str(dst.resolve())}
        )

    def _manifest(self, status: str, error: str = "") -> dict[str, Any]:
        touched = []
        for key in self._touch_order:
            record = dict(self._touches[key])
            path = Path(record["path"])
            record.update(
                {
                    "exists_after": path.is_file(),
                    "after_sha256": sha256_file(path) or "",
                }
            )
            touched.append(record)
        return {
            "transaction_id": self.transaction_id,
            "status": status,
            "step": self.step,
            "env": self.environment,
            "project_root": str(self.project_root),
            "cards_dir": str(self.cards_dir),
            "run_id": current_run_id(),
            "started_at": self.started_at,
            "completed_at": self.completed_at or dt.datetime.now().isoformat(timespec="seconds"),
            "snapshot_enabled": self.snapshot_enabled,
            "rollback_performed": self._rolled_back,
            "rollback_error": self._rollback_error,
            "error": error,
            "operations": self._operations,
            "touched": touched,
        }

    def _write_manifest(self, status: str, error: str = "") -> None:
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.manifest_path, self._manifest(status, error))

    def commit(self) -> None:
        if self._finished:
            if self._committed:
                return
            raise RuntimeError("Cannot commit a finished, uncommitted transaction.")
        self.completed_at = dt.datetime.now().isoformat(timespec="seconds")
        # Persist the audit manifest before declaring the transaction finished.
        # If this write fails, the context manager can still roll back all files.
        self._write_manifest("committed")
        self._committed = True
        self._finished = True

    def rollback(self, *, error: str = "") -> None:
        if self._finished and self._rolled_back:
            return
        failures = []
        for key in reversed(self._touch_order):
            record = self._touches[key]
            path = Path(record["path"])
            try:
                if record["existed_before"]:
                    backup = Path(record["backup_path"])
                    if not backup.is_file():
                        raise FileNotFoundError(f"Missing rollback before-image: {backup}")
                    atomic_write_bytes(path, backup.read_bytes())
                else:
                    if path.is_file():
                        path.unlink()
                        _fsync_directory(path.parent)
            except Exception as exc:  # keep restoring remaining paths
                failures.append(f"{path}: {exc}")
        self.completed_at = dt.datetime.now().isoformat(timespec="seconds")
        self._rolled_back = True
        self._rollback_error = "; ".join(failures)
        self._finished = True
        self._write_manifest("rollback_failed" if failures else "rolled_back", error)
        if failures:
            raise RuntimeError(
                "Transaction rollback encountered errors: " + self._rollback_error
            )

    def begin(self) -> "CardTransaction":
        if self._entered:
            return self
        prod_roots = {Path(project_for_env("prod")).expanduser().resolve()}
        # A controlled explicit override may give the active prod environment a
        # nonstandard root. It is still production and must not bypass the guard.
        if self.environment == "prod":
            prod_roots.add(self.project_root)
        if (
            self.project_root in prod_roots
            and os.environ.get("NIH_TRACKER_PROD_WRITE_CONFIRMATION")
            != PROD_WRITE_CONFIRMATION_TOKEN
        ):
            raise RuntimeError(
                "Production card writes are locked. Set "
                f"NIH_TRACKER_PROD_WRITE_CONFIRMATION={PROD_WRITE_CONFIRMATION_TOKEN!r} "
                "only for a deliberate production write."
            )
        self.lock.acquire()
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._entered = True
        self._write_manifest("active")
        return self

    def close(self) -> None:
        failures: list[str] = []
        for key in reversed(self._auxiliary_lock_order):
            try:
                self._auxiliary_locks[key].release()
            except Exception as exc:
                failures.append(f"{key}: {exc}")
        self._auxiliary_locks.clear()
        self._auxiliary_lock_order.clear()
        try:
            self.lock.release()
        except Exception as exc:
            failures.append(f"{self.lock.path}: {exc}")
        finally:
            self._entered = False
        if failures:
            raise LockError("Failed to release transaction lock(s): " + "; ".join(failures))


@contextmanager
def card_transaction(
    step: str,
    *,
    project_root: os.PathLike[str] | str = PROJECT,
    cards_dir: os.PathLike[str] | str = CARDS,
    snapshot: bool = True,
    auto_commit: bool = True,
    lock_timeout: Optional[float] = None,
):
    """Enter a re-entrant card transaction for one card directory."""
    key = str(Path(cards_dir).expanduser().resolve())
    existing = _ACTIVE_CARD_TRANSACTIONS.get(key)
    if existing is not None:
        yield existing
        return

    tx = CardTransaction(
        step,
        project_root=project_root,
        cards_dir=cards_dir,
        snapshot=snapshot,
        auto_commit=auto_commit,
        lock_timeout=lock_timeout,
    )
    _ACTIVE_CARD_TRANSACTIONS[key] = tx
    try:
        tx.begin()
        try:
            yield tx
        except Exception as exc:
            if not tx._finished:
                tx.rollback(error=f"{type(exc).__name__}: {exc}")
            raise
        else:
            if not tx._finished:
                try:
                    if auto_commit:
                        tx.commit()
                    else:
                        tx.rollback(error="Transaction exited without explicit commit.")
                except Exception as exc:
                    if not tx._finished:
                        tx.rollback(error=f"commit failure: {type(exc).__name__}: {exc}")
                    raise
    finally:
        try:
            tx.close()
        finally:
            _ACTIVE_CARD_TRANSACTIONS.pop(key, None)


def active_card_transaction(
    cards_dir: os.PathLike[str] | str = CARDS,
) -> Optional[CardTransaction]:
    return _ACTIVE_CARD_TRANSACTIONS.get(str(Path(cards_dir).expanduser().resolve()))


def load_card(uid: Any, *, cards_dir: os.PathLike[str] | str | None = None):
    path = card_path(uid, cards_dir=cards_dir)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class ConcurrentCardModificationError(RuntimeError):
    """Raised when an apply plan was built from a card that has since changed."""


def load_card_for_update(
    uid: Any,
    expected: Any,
    *,
    cards_dir: os.PathLike[str] | str | None = None,
):
    """Reload and compare a card while its card-directory transaction is held.

    Whole-card writers must not commit a plan computed before they acquired the
    lock unless the current on-disk object still equals the object they read.
    This check prevents serialized writers from becoming lost-update writers.
    """
    directory = str(cards_dir or CARDS)
    if active_card_transaction(directory) is None:
        raise RuntimeError("load_card_for_update requires an active card transaction.")
    current = load_card(uid, cards_dir=directory)
    if current is None:
        raise ConcurrentCardModificationError(
            f"Card vanished after planning and before locked commit: {uid}"
        )
    if current != expected:
        raise ConcurrentCardModificationError(
            f"Card changed after planning; rerun the dry-run before applying: {uid}"
        )
    return current


def require_card_absent(
    uid: Any,
    *,
    cards_dir: os.PathLike[str] | str | None = None,
) -> None:
    """Fail if a destination card appeared after planning, under the active lock."""
    directory = str(cards_dir or CARDS)
    if active_card_transaction(directory) is None:
        raise RuntimeError("require_card_absent requires an active card transaction.")
    if load_card(uid, cards_dir=directory) is not None:
        raise ConcurrentCardModificationError(
            f"Destination card appeared after planning; refusing overwrite: {uid}"
        )


def save_card(uid: Any, card: Any, *, cards_dir: os.PathLike[str] | str | None = None) -> None:
    uid_value = _validate_card_uid(uid)
    if not isinstance(card, Mapping):
        raise TypeError(f"Card {uid_value} must be a mapping, got {type(card).__name__}.")
    embedded_uid = str(card.get("repo_uid", "") or "").strip()
    if embedded_uid and embedded_uid != uid_value:
        raise ValueError(
            f"Refusing to save card under {uid_value}: embedded repo_uid is {embedded_uid!r}."
        )
    directory = str(cards_dir or CARDS)
    destination = card_path(uid_value, cards_dir=directory)
    tx = active_card_transaction(directory)
    if tx is not None:
        tx.write_yaml(destination, card, operation="save_card")
        return
    project_root = PROJECT if Path(directory).resolve() == Path(CARDS).resolve() else str(Path(directory).resolve().parent)
    with card_transaction(
        "save_card_one_shot",
        project_root=project_root,
        cards_dir=directory,
    ) as one_shot:
        one_shot.write_yaml(destination, card, operation="save_card")


def delete_card(uid: Any, *, cards_dir: os.PathLike[str] | str | None = None) -> None:
    directory = str(cards_dir or CARDS)
    destination = card_path(uid, cards_dir=directory)
    tx = active_card_transaction(directory)
    if tx is not None:
        tx.delete(destination, operation="delete_card")
        return
    project_root = PROJECT if Path(directory).resolve() == Path(CARDS).resolve() else str(Path(directory).resolve().parent)
    with card_transaction(
        "delete_card_one_shot",
        project_root=project_root,
        cards_dir=directory,
    ) as one_shot:
        one_shot.delete(destination, operation="delete_card")


def archive_card(
    uid: Any,
    *,
    archive_subdir: str = "_merged",
    cards_dir: os.PathLike[str] | str | None = None,
) -> str:
    directory = Path(cards_dir or CARDS)
    if Path(archive_subdir).is_absolute() or ".." in Path(archive_subdir).parts:
        raise ValueError(f"Unsafe archive subdirectory: {archive_subdir!r}")
    source = Path(card_path(uid, cards_dir=directory))
    destination = directory / archive_subdir / source.name
    tx = active_card_transaction(directory)
    if tx is not None:
        tx.move(source, destination, overwrite=False, operation="archive_card")
    else:
        project_root = PROJECT if directory.resolve() == Path(CARDS).resolve() else str(directory.resolve().parent)
        with card_transaction(
            "archive_card_one_shot",
            project_root=project_root,
            cards_dir=directory,
        ) as one_shot:
            one_shot.move(source, destination, overwrite=False, operation="archive_card")
    return str(destination)


def all_cards(*, cards_dir: os.PathLike[str] | str | None = None) -> dict[str, Any]:
    """Load only top-level YAML cards; cards/_merged/ archives are excluded."""
    directory = str(cards_dir or CARDS)
    out: dict[str, Any] = {}
    if not os.path.isdir(directory):
        return out
    for filename in sorted(os.listdir(directory)):
        if filename.endswith((".yaml", ".yml")):
            uid = filename.rsplit(".", 1)[0]
            card = load_card(uid, cards_dir=directory)
            if card:
                out[uid] = card
    return out


def card_lock_status(cards_dir: os.PathLike[str] | str = CARDS) -> Optional[dict[str, Any]]:
    path = Path(cards_dir) / ".lock"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = {"raw": path.read_text(encoding="utf-8", errors="replace")[:1000]}
    payload["path"] = str(path)
    try:
        payload["age_seconds"] = max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        payload["age_seconds"] = None
    return payload


def remove_card_lock(
    confirmation: str,
    *,
    cards_dir: os.PathLike[str] | str = CARDS,
) -> bool:
    """Deliberately remove an abandoned lock after exact confirmation."""
    required = "REMOVE_ABANDONED_CARD_LOCK"
    if confirmation != required:
        raise ValueError(f"Lock removal requires confirmation {required!r}.")
    path = Path(cards_dir) / ".lock"
    if not path.exists():
        return False
    path.unlink()
    return True


def snapshot_cards_zip(
    *,
    label: str,
    cards_dir: os.PathLike[str] | str = CARDS,
    project_root: os.PathLike[str] | str = PROJECT,
) -> str:
    """Create a point-in-time ZIP excluding transaction internals (.lock/.tmp)."""
    import zipfile

    source = Path(cards_dir)
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")
    nonce = uuid.uuid4().hex[:8]
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label or "manual")
    destination = (
        Path(project_root)
        / "snapshots"
        / "archives"
        / f"cards_{stamp}_{nonce}_{safe_label}.zip"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.tmp.", dir=str(destination.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(source)
                if rel.parts[0] == ".tmp" or rel.name == ".lock":
                    continue
                archive.write(path, arcname=rel.as_posix())
        with tmp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp_path, destination)
        _fsync_directory(destination.parent)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return str(destination)


# ---------------------------------------------------------------------------
# Name/card utility functions retained from the original workflow
# ---------------------------------------------------------------------------
def run_ts() -> str:
    return dt.datetime.now().strftime("%Y%m%dT%H%M%S")


def slug(s):
    s = "" if pd.isna(s) else str(s).strip().lower()
    for token in ("repository", "database", "data portal", "archive", "the "):
        s = s.replace(token, "")
    return re.sub(r"[^a-z0-9]+", "", s)


def strip_acronym(s):
    """Return ('Foo Bar', 'FITBIR') for 'Foo Bar (FITBIR)', else (s, None)."""
    if not isinstance(s, str):
        return s, None
    match = re.search(r"\(([A-Za-z0-9\-]{2,15})\)\s*$", s.strip())
    if match:
        return re.sub(r"\s*\([A-Za-z0-9\-]{2,15}\)\s*$", "", s.strip()), match.group(1)
    return s.strip(), None


def card_name_set(card):
    """Return normalized name and acronym sets covering all card identity names."""
    ident = (card or {}).get("identity", {}) or {}
    names, acronyms = set(), set()
    for key in ("name", "full_name"):
        value = ident.get(key)
        if value:
            base, acronym = strip_acronym(value)
            names.add(slug(base))
            if acronym:
                acronyms.add(slug(acronym))
    if ident.get("abbreviation"):
        acronyms.add(slug(ident["abbreviation"]))
    for alias in ident.get("aliases", []) or []:
        if alias:
            base, acronym = strip_acronym(alias)
            names.add(slug(base))
            if acronym:
                acronyms.add(slug(acronym))
    names.discard("")
    acronyms.discard("")
    return names, acronyms


def name_matches(card, candidate_name, fuzzy=0.94):
    """Return (matched, basis) against any canonical or alias card name."""
    if not candidate_name:
        return False, "empty"
    names, acronyms = card_name_set(card)
    base, candidate_acronym = strip_acronym(candidate_name)
    base = slug(base)
    candidate_acronym = slug(candidate_acronym) if candidate_acronym else ""
    if base in names:
        return True, "exact_name"
    if candidate_acronym and candidate_acronym in acronyms:
        return True, "acronym"
    if base in acronyms or (candidate_acronym and candidate_acronym in names):
        return True, "acronym_cross"
    if difflib.get_close_matches(base, sorted(names), n=1, cutoff=fuzzy):
        return True, f"fuzzy_{fuzzy}"
    return False, "no_match"


def index_cards_by_name(cards):
    idx, collisions = {}, []
    for uid, card in cards.items():
        names, acronyms = card_name_set(card)
        for value in names | acronyms:
            if value in idx and idx[value] != uid:
                collisions.append((value, idx[value], uid))
            else:
                idx.setdefault(value, uid)
    return idx, collisions


def months_since(asof):
    if not asof:
        return 9999
    try:
        value = str(asof)
        if "Q" in value:
            year, quarter = value.split("-Q")
            date_value = dt.date(int(year), (int(quarter) - 1) * 3 + 1, 1)
        else:
            parts = value.split("-")
            date_value = dt.date(int(parts[0]), int(parts[1]) if len(parts) > 1 else 1, 1)
        return (NOW.year - date_value.year) * 12 + (NOW.month - date_value.month)
    except Exception:
        return 9999


def block_asof(card, block):
    if not card:
        return None
    if block == "identity":
        return (card.get("identity", {}).get("status", {}) or {}).get("as_of")
    return ((card.get("blocks", {}) or {}).get(block, {}).get("_meta", {}) or {}).get("as_of")


def block_state(card, block):
    if not card:
        return "auto"
    if block == "identity":
        return card.get("curation", {}).get("review_state", "auto")
    return ((card.get("blocks", {}) or {}).get(block, {}).get("_meta", {}) or {}).get("review_state", "auto")


def gate(checks):
    print("\n── SANITY GATE ──")
    passed_all = True
    for name, (passed, detail) in checks.items():
        print(f" [{'PASS' if passed else 'FAIL'}] {name:<34} {detail}")
        passed_all = passed_all and bool(passed)
    print(f" ⇒ {'ALL PASS' if passed_all else 'FAILED'}")
    return passed_all


# ---------------------------------------------------------------------------
# Deduplicated issue registry with escalation
# ---------------------------------------------------------------------------
ISSUE_FIELDS = [
    "issue_fingerprint",
    "repo_uid",
    "issue_type",
    "step",
    "base_severity",
    "severity",
    "summary",
    "detail",
    "first_seen",
    "last_seen",
    "times_seen",
    "first_seen_run",
    "last_seen_run",
    "resolved",
    "resolved_at",
    "resolved_note",
]
_SEVERITY_RANK = {"advisory": 1, "needs_attention": 2, "blocking": 3}


def _summary_template(summary: Any) -> str:
    value = re.sub(r"\d+", "{n}", str(summary or "").strip().lower())
    return re.sub(r"\s+", " ", value)


def issue_fingerprint(
    *,
    repo_uid: Any = "",
    issue_type: Any = "",
    step: Any = "",
    summary: Any = "",
) -> str:
    uid = str(repo_uid or "").strip()
    kind = str(issue_type or "").strip().lower()
    if not kind:
        kind = f"legacy:{str(step or '').strip().lower()}:{_summary_template(summary)}"
    key = f"{uid}|{kind}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _parse_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except Exception:
        return default


def _canonical_issue_row(row: Mapping[str, Any]) -> dict[str, Any]:
    now = dt.datetime.now().isoformat(timespec="seconds")
    summary = str(row.get("summary", "") or "")
    step = str(row.get("step", "") or "")
    repo_uid = str(row.get("repo_uid", "") or "")
    issue_type = str(row.get("issue_type", "") or "")
    logged_at = str(row.get("logged_at", "") or "")
    first_seen = str(row.get("first_seen", "") or logged_at or now)
    last_seen = str(row.get("last_seen", "") or logged_at or first_seen)
    base = str(row.get("base_severity", "") or row.get("severity", "advisory")).strip()
    if base == "needs_attention":
        base = "advisory"
    if base not in {"advisory", "blocking"}:
        base = "advisory"
    severity = str(row.get("severity", "") or base).strip()
    if severity not in _SEVERITY_RANK:
        severity = base
    fingerprint = str(row.get("issue_fingerprint", "") or "") or issue_fingerprint(
        repo_uid=repo_uid,
        issue_type=issue_type,
        step=step,
        summary=summary,
    )
    return {
        "issue_fingerprint": fingerprint,
        "repo_uid": repo_uid,
        "issue_type": issue_type or f"legacy:{step}:{_summary_template(summary)}",
        "step": step,
        "base_severity": base,
        "severity": severity,
        "summary": summary,
        "detail": str(row.get("detail", "") or ""),
        "first_seen": first_seen,
        "last_seen": last_seen,
        "times_seen": max(1, _parse_int(row.get("times_seen", 1), 1)),
        "first_seen_run": str(row.get("first_seen_run", "") or "legacy"),
        "last_seen_run": str(row.get("last_seen_run", "") or "legacy"),
        "resolved": _parse_bool(row.get("resolved", False)),
        "resolved_at": str(row.get("resolved_at", "") or ""),
        "resolved_note": str(row.get("resolved_note", "") or ""),
    }


def _strongest_severity(*values: str) -> str:
    return max(values, key=lambda value: _SEVERITY_RANK.get(value, 0))


def _load_issue_rows() -> list[dict[str, Any]]:
    if not os.path.exists(ISSUES_LOG) or os.path.getsize(ISSUES_LOG) == 0:
        return []
    try:
        frame = pd.read_csv(ISSUES_LOG, dtype=str).fillna("")
    except pd.errors.EmptyDataError:
        return []
    canonical = [_canonical_issue_row(row) for row in frame.to_dict("records")]

    # Collapse historical duplicates during migration. The most recent row owns
    # mutable status/description; counts and first/last sightings are retained.
    grouped: dict[str, dict[str, Any]] = {}
    for row in sorted(canonical, key=lambda item: item["last_seen"]):
        fingerprint = row["issue_fingerprint"]
        if fingerprint not in grouped:
            grouped[fingerprint] = dict(row)
            continue
        current = grouped[fingerprint]
        current["first_seen"] = min(current["first_seen"], row["first_seen"])
        current["last_seen"] = max(current["last_seen"], row["last_seen"])
        current["times_seen"] = _parse_int(current["times_seen"], 1) + _parse_int(row["times_seen"], 1)
        current["base_severity"] = _strongest_severity(
            current["base_severity"], row["base_severity"]
        )
        current["severity"] = _strongest_severity(current["severity"], row["severity"])
        for field in (
            "repo_uid",
            "issue_type",
            "step",
            "summary",
            "detail",
            "last_seen_run",
            "resolved",
            "resolved_at",
            "resolved_note",
        ):
            current[field] = row[field]
    return list(grouped.values())


def _write_issue_rows(rows: Iterable[Mapping[str, Any]]) -> None:
    ordered = sorted(
        rows,
        key=lambda row: (
            bool(row.get("resolved")),
            -_SEVERITY_RANK.get(str(row.get("severity", "")), 0),
            str(row.get("last_seen", "")),
            str(row.get("repo_uid", "")),
        ),
    )
    atomic_write_csv_rows(ISSUES_LOG, ordered, ISSUE_FIELDS)


def log_issue(
    step,
    severity,
    summary,
    detail="",
    resolved=False,
    *,
    repo_uid="",
    issue_type="",
    escalation_after: Optional[int] = None,
    run_id: Optional[str] = None,
):
    """Upsert one issue by stable fingerprint and escalate recurring advisories.

    The natural key is repo_uid + issue_type. For backward-compatible calls that
    omit issue_type, a normalized step/summary template is used. times_seen is
    incremented at most once per NIH_TRACKER_RUN_ID.
    """
    if severity not in {"blocking", "advisory", "needs_attention"}:
        raise ValueError("severity must be blocking, needs_attention, or advisory")
    base_severity = "advisory" if severity == "needs_attention" else severity
    threshold = (
        int(os.environ.get("NIH_TRACKER_ISSUE_ESCALATE_AFTER", "3"))
        if escalation_after is None
        else int(escalation_after)
    )
    threshold = max(1, threshold)
    seen_run = run_id or current_run_id()
    now = dt.datetime.now().isoformat(timespec="seconds")
    fingerprint = issue_fingerprint(
        repo_uid=repo_uid,
        issue_type=issue_type,
        step=step,
        summary=summary,
    )

    lock = FileLock(f"{ISSUES_LOG}.lock", purpose="issues_log upsert", timeout=5)
    with lock:
        rows = _load_issue_rows()
        existing = next(
            (row for row in rows if row["issue_fingerprint"] == fingerprint),
            None,
        )
        if existing is None and issue_type:
            # One-time bridge from old append-only aggregate rows whose summary
            # differed only by a changing count.
            template = _summary_template(summary)
            existing = next(
                (
                    row
                    for row in rows
                    if not row.get("repo_uid")
                    and row.get("step") == step
                    and _summary_template(row.get("summary")) == template
                ),
                None,
            )
            if existing is not None:
                existing["issue_fingerprint"] = fingerprint
                existing["issue_type"] = str(issue_type)
                existing["repo_uid"] = str(repo_uid or "")

        if existing is None:
            times_seen = 1
            effective = (
                "blocking"
                if base_severity == "blocking"
                else ("needs_attention" if times_seen >= threshold else "advisory")
            )
            existing = {
                "issue_fingerprint": fingerprint,
                "repo_uid": str(repo_uid or ""),
                "issue_type": str(issue_type or f"legacy:{step}:{_summary_template(summary)}"),
                "step": str(step),
                "base_severity": base_severity,
                "severity": effective,
                "summary": str(summary),
                "detail": str(detail or ""),
                "first_seen": now,
                "last_seen": now,
                "times_seen": times_seen,
                "first_seen_run": seen_run,
                "last_seen_run": seen_run,
                "resolved": bool(resolved),
                "resolved_at": now if resolved else "",
                "resolved_note": "" if not resolved else str(detail or ""),
            }
            rows.append(existing)
        else:
            recurrence = existing.get("last_seen_run") != seen_run
            if recurrence:
                existing["times_seen"] = _parse_int(existing.get("times_seen"), 1) + 1
            existing.update(
                {
                    "repo_uid": str(repo_uid or existing.get("repo_uid", "")),
                    "issue_type": str(issue_type or existing.get("issue_type", "")),
                    "step": str(step),
                    "base_severity": _strongest_severity(
                        str(existing.get("base_severity", "advisory")), base_severity
                    ),
                    "summary": str(summary),
                    "detail": str(detail or ""),
                    "last_seen": now,
                    "last_seen_run": seen_run,
                }
            )
            count = _parse_int(existing.get("times_seen"), 1)
            if existing["base_severity"] == "blocking":
                existing["severity"] = "blocking"
            else:
                existing["severity"] = "needs_attention" if count >= threshold else "advisory"
            if resolved:
                existing["resolved"] = True
                existing["resolved_at"] = now
                existing["resolved_note"] = str(detail or existing.get("resolved_note", ""))
            else:
                # A recurrence re-opens a previously resolved issue.
                existing["resolved"] = False
                existing["resolved_at"] = ""
                existing["resolved_note"] = ""

        _write_issue_rows(rows)

    print(
        f"  📋 issue upserted: [{existing['severity']}] {existing['summary']} "
        f"(seen {existing['times_seen']} run(s); key={existing['issue_fingerprint']})"
    )
    return dict(existing)


def resolve_issue(
    *,
    fingerprint: str = "",
    repo_uid: str = "",
    issue_type: str = "",
    note: str = "",
) -> bool:
    if not fingerprint:
        fingerprint = issue_fingerprint(repo_uid=repo_uid, issue_type=issue_type)
    lock = FileLock(f"{ISSUES_LOG}.lock", purpose="issues_log resolve", timeout=5)
    with lock:
        rows = _load_issue_rows()
        target = next((row for row in rows if row["issue_fingerprint"] == fingerprint), None)
        if target is None:
            return False
        now = dt.datetime.now().isoformat(timespec="seconds")
        target["resolved"] = True
        target["resolved_at"] = now
        target["resolved_note"] = str(note or "resolved by current run")
        _write_issue_rows(rows)
    return True


def resolve_issues_not_in(
    *,
    step: str,
    issue_type: str,
    active_repo_uids: Iterable[str],
    note: str = "No longer observed in the current run.",
) -> int:
    active = {str(value).strip() for value in active_repo_uids}
    lock = FileLock(f"{ISSUES_LOG}.lock", purpose="issues_log reconcile", timeout=5)
    with lock:
        rows = _load_issue_rows()
        now = dt.datetime.now().isoformat(timespec="seconds")
        resolved_count = 0
        for row in rows:
            if (
                row.get("step") == step
                and row.get("issue_type") == issue_type
                and not _parse_bool(row.get("resolved"))
                and str(row.get("repo_uid", "")) not in active
            ):
                row["resolved"] = True
                row["resolved_at"] = now
                row["resolved_note"] = note
                resolved_count += 1
        if resolved_count:
            _write_issue_rows(rows)
    return resolved_count


# ---------------------------------------------------------------------------
# Coupled audit files and run helpers
# ---------------------------------------------------------------------------
def append_id_remap(old_uid, new_uid, match_type, reconciled_at=None):
    """Append one auditable old-to-new mapping, atomically and conflict-safe.

    When called inside a card transaction, the id-remap lock is retained until
    commit or rollback so no external append can be lost during restoration.
    """
    old_uid = _validate_card_uid(old_uid)
    new_uid = _validate_card_uid(new_uid)
    match_type = str(match_type).strip()
    if not match_type:
        raise ValueError("match_type must be non-empty")
    if old_uid == new_uid:
        raise ValueError("id remap old_uid and new_uid must differ")

    fieldnames = ["old_uid", "new_uid", "reconciled_at", "match_type"]
    tx = active_card_transaction(CARDS)
    one_shot_lock: FileLock | None = None
    if tx is not None:
        tx.acquire_auxiliary_lock(
            f"{ID_REMAP_CSV}.lock",
            purpose="id_remap transaction",
            timeout=5,
        )
    else:
        one_shot_lock = FileLock(
            f"{ID_REMAP_CSV}.lock", purpose="id_remap append", timeout=5
        )
        one_shot_lock.acquire()

    try:
        rows: list[dict[str, Any]] = []
        if os.path.exists(ID_REMAP_CSV) and os.path.getsize(ID_REMAP_CSV) > 0:
            existing = pd.read_csv(ID_REMAP_CSV, dtype=str).fillna("")
            rows = existing.to_dict("records")
            prior = [row for row in rows if str(row.get("old_uid", "")).strip() == old_uid]
            if prior:
                targets = {str(row.get("new_uid", "")).strip() for row in prior}
                if targets == {new_uid}:
                    return False
                raise ValueError(
                    f"conflicting id remap for {old_uid}: existing {sorted(targets)}, new {new_uid}"
                )
        rows.append(
            {
                "old_uid": old_uid,
                "new_uid": new_uid,
                "reconciled_at": reconciled_at
                or dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "match_type": match_type,
            }
        )
        text = _rows_to_csv_text(rows, fieldnames)
        if tx is not None:
            tx.write_text(ID_REMAP_CSV, text, operation="append_id_remap")
        else:
            atomic_write_text(ID_REMAP_CSV, text)
        return True
    finally:
        if one_shot_lock is not None:
            one_shot_lock.release()


def sha256_path(path):
    """Full SHA-256 for a file or a sorted manifest of directory file hashes."""
    if not path or not Path(path).exists():
        return None
    source = Path(path)
    if source.is_file():
        return sha256_file(source)
    records = {}
    for child in sorted(source.rglob("*")):
        rel = child.relative_to(source)
        if child.is_file() and rel.name != ".lock" and ".tmp" not in rel.parts:
            records[rel.as_posix()] = sha256_file(child)
    return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def ledger_append(step, params, inputs, outputs, gate_passed, run_dir):
    from code_provenance import describe_code
    code_identity = describe_code(Path(__file__).resolve().parents[1])
    provenance_hash = hashlib.sha256(json.dumps(code_identity, sort_keys=True).encode()).hexdigest()
    provenance_path = Path(run_dir) / f"code_provenance_{provenance_hash[:16]}.json"
    if not provenance_path.exists():
        atomic_write_json(provenance_path, code_identity)
    row = {
        "ts": run_ts(),
        "env": ENV,
        "project": PROJECT,
        "run_id": current_run_id(),
        "step": step,
        "gate_passed": bool(gate_passed),
        "params": json.dumps(params, default=str),
        "inputs": json.dumps({key: sha1(value) for key, value in inputs.items()}, default=str),
        "input_sha256": json.dumps({key: sha256_path(value) for key, value in inputs.items()}, default=str),
        "source_tree_sha256": code_identity["source_tree_sha256"],
        "git_commit": code_identity["git_commit"] or "",
        "git_dirty": code_identity["git_dirty"],
        "code_provenance": str(provenance_path),
        "outputs": json.dumps(outputs, default=str),
        "run_dir": run_dir,
    }
    lock = FileLock(f"{LEDGER}.lock", purpose="run ledger append", timeout=5)
    with lock:
        rows: list[dict[str, Any]] = []
        fieldnames = list(row)
        if os.path.exists(LEDGER) and os.path.getsize(LEDGER) > 0:
            frame = pd.read_csv(LEDGER, dtype=str).fillna("")
            rows = frame.to_dict("records")
            fieldnames = list(dict.fromkeys([*frame.columns.tolist(), *row.keys()]))
        rows.append(row)
        atomic_write_csv_rows(LEDGER, rows, fieldnames)
    print(f" ledger += {step} @ {row['ts']} (env={ENV}; gate={'PASS' if gate_passed else 'FAIL'})")
    return row


def new_run_dir(step):
    base = Path(RUNS) / step / run_ts()
    directory = base
    suffix = 1
    while directory.exists():
        directory = Path(f"{base}_{suffix:02d}")
        suffix += 1
    directory.mkdir(parents=True, exist_ok=False)
    return str(directory)


def save_run(mode, frames, text_files, report, gate_ok, run_dir=None, latest_dir=None):
    """Write a run; on a passing gate, replace latest/<mode> from a temp copy."""
    run_dir = run_dir or new_run_dir(mode)
    latest = Path(latest_dir or os.path.join(LATEST, mode))
    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)
    report.update(
        {
            "mode": mode,
            "run_ts": run_path.name,
            "gate_passed": gate_ok,
            "env": ENV,
            "project": PROJECT,
        }
    )
    atomic_write_json(run_path / "run_report.json", report)
    if not gate_ok:
        print("\nGate failed → wrote run_report.json only; latest/ untouched.")
        return
    for name, frame in frames.items():
        frame.to_parquet(run_path / f"{name}.parquet", index=False)
        frame.to_csv(run_path / f"{name}.csv", index=False)
    for name, text in text_files.items():
        destination = run_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(destination, text)

    latest.parent.mkdir(parents=True, exist_ok=True)
    temp_latest = latest.parent / f".{latest.name}.tmp.{uuid.uuid4().hex[:8]}"
    backup_latest = latest.parent / f".{latest.name}.old.{uuid.uuid4().hex[:8]}"
    shutil.copytree(run_path, temp_latest)
    try:
        if latest.exists():
            os.replace(latest, backup_latest)
        os.replace(temp_latest, latest)
        if backup_latest.exists():
            shutil.rmtree(backup_latest)
    except Exception:
        if latest.exists() and backup_latest.exists():
            shutil.rmtree(latest, ignore_errors=True)
            os.replace(backup_latest, latest)
        shutil.rmtree(temp_latest, ignore_errors=True)
        raise
    print(f"\n✓ saved run → {run_dir}")
    print(f"✓ latest/{mode}/ atomically repopulated ({len(os.listdir(run_dir))} top-level entries)")


def prune_runs(mode=None, keep_last=5, dry_run=True):
    """Delete run history beyond keep_last per mode; never touch latest/."""
    if keep_last < 0:
        raise ValueError("keep_last must be >= 0")
    if not os.path.isdir(RUNS):
        print(f"\nNo run history found at {RUNS}.")
        return
    modes = [mode] if mode else [name for name in os.listdir(RUNS) if os.path.isdir(os.path.join(RUNS, name))]
    total, count = 0, 0
    for current_mode in modes:
        mode_dir = os.path.join(RUNS, current_mode)
        if not os.path.isdir(mode_dir):
            continue
        timestamps = sorted(name for name in os.listdir(mode_dir) if os.path.isdir(os.path.join(mode_dir, name)))
        to_delete = timestamps[:-keep_last] if keep_last else timestamps
        for timestamp in to_delete:
            path = os.path.join(mode_dir, timestamp)
            size = sum(os.path.getsize(os.path.join(root, name)) for root, _, files in os.walk(path) for name in files)
            total += size
            count += 1
            if dry_run:
                print(f"  [DRY] would delete  {current_mode}/{timestamp}   ({size / 1024:.0f} KB)")
            else:
                shutil.rmtree(path)
                print(f"  deleted  {current_mode}/{timestamp}   ({size / 1024:.0f} KB)")
    print(f"\n{'Would free' if dry_run else 'Freed'}: {total / 1024 / 1024:.1f} MB across {count} run(s), keeping last {keep_last} per mode.")
    if dry_run:
        print("Re-run with dry_run=False to actually delete.")
