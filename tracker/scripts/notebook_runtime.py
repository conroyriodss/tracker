"""Shared runtime support for the NIH Tracker Colab notebooks.

The module keeps notebook setup, environment selection, secret loading,
subprocess execution, smoke testing, and diagnostic logging consistent across
the pipeline, review, and maintenance notebooks. It intentionally avoids
importing :mod:`common` until the requested environment has been configured.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib
import json
import os
import platform
import shlex
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


PRODUCTION_ONLY = str(os.environ.get("NIH_TRACKER_PRODUCTION_ONLY", "")).strip().lower() in {
    "1", "true", "yes", "y", "on"
}
DEFAULT_ROOTS = (
    {"prod": "/content/drive/MyDrive/NIH_Tracker"}
    if PRODUCTION_ONLY
    else {
        "dev": "/content/drive/MyDrive/NIH_Tracker_dev",
        "staging": "/content/drive/MyDrive/NIH_Tracker_staging",
        "prod": "/content/drive/MyDrive/NIH_Tracker",
    }
)
VALID_ENVS = tuple(DEFAULT_ROOTS)
BASE_API_SECRETS = (
    "FS_U",
    "FS_PWD",
    "SCICRUNCH_API_KEY",
    "RRID",
    "FAIRSHARING_API_TOKEN",
    "FAIRSHARING_API_KEY",
)
GITHUB_SECRETS = (
    "GITHUB_TOKEN",
    "GITHUB_REPOSITORY",
    "GITHUB_BRANCH",
    "GITHUB_PUBLISH_DIR",
    "GITHUB_COMMIT_NAME",
    "GITHUB_COMMIT_EMAIL",
)
SCRIPT_SECRETS = {
    "step_build_rrids.py": ("SCICRUNCH_API_KEY", "RRID"),
    "step_build_fairsharing.py": (
        "FS_U",
        "FS_PWD",
        "FAIRSHARING_API_TOKEN",
        "FAIRSHARING_API_KEY",
    ),
    "step_build_resource_profiles.py": (
        "FS_U",
        "FS_PWD",
        "FAIRSHARING_API_TOKEN",
        "FAIRSHARING_API_KEY",
    ),
    "verify_external_ids.py": BASE_API_SECRETS,
    "step_publish_github.py": GITHUB_SECRETS,
}
ALL_MANAGED_SECRETS = tuple(
    dict.fromkeys([*BASE_API_SECRETS, *GITHUB_SECRETS, "NIH_TRACKER_REVIEWER"])
)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _safe_slug(value: str) -> str:
    cleaned = "".join(character if character.isalnum() else "_" for character in value)
    return cleaned.strip("_") or "operation"


def _purge_tracker_modules() -> None:
    for name in list(sys.modules):
        if (
            name == "common"
            or name == "identity_hygiene"
            or name == "interoperability_schema"
            or name == "resource_profile_schema"
            or name == "policy_context_schema"
            or name == "repository_status_schema"
            or name == "publisher_context_schema"
            or name == "review_widgets"
            or name == "notebook_runtime"
            or name == "verify_external_ids"
            or name.startswith("step_")
        ):
            # Do not remove the currently executing module. This branch is used
            # only before notebook_runtime is imported in normal notebook use.
            if name != __name__:
                sys.modules.pop(name, None)


def mount_google_drive() -> None:
    """Mount Google Drive when running in Colab; no-op elsewhere."""
    try:
        from google.colab import drive  # type: ignore

        if not Path("/content/drive/MyDrive").exists():
            drive.mount("/content/drive")
    except Exception:
        return


def configure_environment(
    env: str,
    *,
    enable_prod_writes: bool = False,
    roots: Mapping[str, str] | None = None,
    shared_cache_root: str = "/content/drive/MyDrive/NIH_Tracker_shared/cache",
    allow_nonprod_credentials: bool = True,
    mount_drive: bool = True,
) -> tuple[Path, Path]:
    """Configure environment variables before importing tracker modules.

    v19.0.2 notebooks set production-only mode before importing this module.
    The compatibility branch for dev/staging remains available only when that
    mode is not enabled, primarily for isolated tests and legacy command lines.
    """
    normalized = str(env).strip().lower()
    if normalized not in VALID_ENVS:
        raise ValueError(f"env must be one of {VALID_ENVS}; got {env!r}")
    resolved_roots = {
        name: os.environ.get(f"NIH_TRACKER_ROOT_{name.upper()}", default)
        for name, default in DEFAULT_ROOTS.items()
    }
    # An explicit mapping supplied by the notebook/test caller is authoritative;
    # stale process environment variables must not redirect the selected tier.
    if roots:
        resolved_roots.update({key: str(value) for key, value in roots.items()})
    canonical = {
        name: str(Path(path).expanduser().resolve())
        for name, path in resolved_roots.items()
    }
    if len(set(canonical.values())) != len(canonical):
        raise RuntimeError(f"Environment roots must be distinct: {canonical}")

    if mount_drive:
        mount_google_drive()

    os.environ.pop("NIH_TRACKER", None)
    os.environ.pop("NIH_TRACKER_ALLOW_PROJECT_OVERRIDE", None)
    os.environ["NIH_TRACKER_ENV"] = normalized
    for name, root in canonical.items():
        os.environ[f"NIH_TRACKER_ROOT_{name.upper()}"] = root
    os.environ["NIH_TRACKER_SHARED_CACHE_ROOT"] = str(
        Path(shared_cache_root).expanduser().resolve()
    )
    os.environ["NIH_TRACKER_RE3DATA_CACHE_READ_ONLY"] = (
        "0" if normalized == "prod" else "1"
    )
    if allow_nonprod_credentials:
        os.environ["NIH_TRACKER_ALLOW_NONPROD_CREDENTIALS"] = "1"
    else:
        os.environ.pop("NIH_TRACKER_ALLOW_NONPROD_CREDENTIALS", None)

    if normalized == "prod" and enable_prod_writes:
        os.environ["NIH_TRACKER_PROD_WRITE_CONFIRMATION"] = "ALLOW_PROD_CARD_WRITES"
    else:
        os.environ.pop("NIH_TRACKER_PROD_WRITE_CONFIRMATION", None)

    project = Path(canonical[normalized])
    scripts = Path(
        os.environ.get("NIH_TRACKER_SCRIPTS_ROOT", str(project / "scripts"))
    ).expanduser().resolve()
    return project, scripts


@dataclass(frozen=True)
class RunResult:
    label: str
    script: str
    run_id: str
    command: list[str]
    returncode: int
    gate_passed: bool | None
    ok: bool
    started_at: str
    finished_at: str
    duration_seconds: float
    log_path: str
    result_path: str
    ledger_rows: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _Tee:
    def __init__(self, *streams: Any) -> None:
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            stream.write(text)
            stream.flush()
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


class NotebookRuntime:
    """Environment-bound notebook execution and diagnostic controller."""

    def __init__(
        self,
        *,
        notebook_name: str,
        env: str,
        project: Path,
        scripts: Path,
        enable_prod_writes: bool,
        secret_provider: Callable[[str], str | None] | None = None,
    ) -> None:
        self.notebook_name = _safe_slug(notebook_name)
        self.env = env
        self.project = project.resolve()
        self.scripts = scripts.resolve()
        self.enable_prod_writes = bool(enable_prod_writes)
        self.secret_provider = secret_provider
        self.session_id = (
            f"{dt.datetime.now().strftime('%Y%m%dT%H%M%S%f')}-"
            f"{uuid.uuid4().hex[:8]}"
        )
        self.debug_dir = (
            self.project / "runs" / "notebook_debug" / self.notebook_name / self.session_id
        )
        self.debug_dir.mkdir(parents=True, exist_ok=False)
        self._operation_counter = 0
        self._run_results: list[RunResult] = []
        self.common = importlib.import_module("common")
        self._write_json(
            self.debug_dir / "session.json",
            {
                "schema": "nih-tracker-notebook-session/v1",
                "notebook": self.notebook_name,
                "session_id": self.session_id,
                "started_at": _utc_now(),
                "environment": self.env,
                "project": str(self.project),
                "scripts": str(self.scripts),
                "production_writes_enabled": self.enable_prod_writes,
            },
        )

    @classmethod
    def create(
        cls,
        *,
        notebook_name: str,
        env: str,
        enable_prod_writes: bool = False,
        roots: Mapping[str, str] | None = None,
        shared_cache_root: str = "/content/drive/MyDrive/NIH_Tracker_shared/cache",
        mount_drive: bool = True,
        secret_provider: Callable[[str], str | None] | None = None,
    ) -> "NotebookRuntime":
        project, scripts = configure_environment(
            env,
            enable_prod_writes=enable_prod_writes,
            roots=roots,
            shared_cache_root=shared_cache_root,
            allow_nonprod_credentials=True,
            mount_drive=mount_drive,
        )
        if not scripts.is_dir():
            raise FileNotFoundError(
                f"Scripts directory not found: {scripts}. Run the maintenance "
                "notebook's production release installer first."
            )
        scripts_text = str(scripts)
        # Remove all configured tier script paths, then put the selected tier at
        # index zero. A stale sys.path entry must not import another environment.
        configured_script_paths = {
            str(Path(os.environ[f"NIH_TRACKER_ROOT_{name.upper()}"]).resolve() / "scripts")
            for name in VALID_ENVS
        }
        sys.path[:] = [
            entry
            for entry in sys.path
            if entry != scripts_text and entry not in configured_script_paths
        ]
        sys.path.insert(0, scripts_text)
        for name in list(sys.modules):
            if (
                name == "common"
                or name == "review_widgets"
                or name == "verify_external_ids"
                or name.startswith("step_")
            ):
                sys.modules.pop(name, None)
        return cls(
            notebook_name=notebook_name,
            env=str(env).strip().lower(),
            project=project,
            scripts=scripts,
            enable_prod_writes=enable_prod_writes,
            secret_provider=secret_provider,
        )

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.tmp.{uuid.uuid4().hex}"
        temporary.write_text(
            json.dumps(payload, indent=2, default=str, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def _provider(self, name: str) -> str | None:
        if self.secret_provider is not None:
            try:
                value = self.secret_provider(name)
            except Exception:
                value = None
            return str(value) if value not in (None, "") else None
        try:
            from google.colab import userdata  # type: ignore

            try:
                value = userdata.get(name)
            except Exception:
                value = None
            return str(value) if value not in (None, "") else None
        except Exception:
            return None

    def load_secrets(
        self,
        names: Iterable[str],
        *,
        required: Iterable[str] = (),
        quiet: bool = False,
    ) -> dict[str, bool]:
        """Load environment-scoped then generic Colab secrets.

        For ``FS_U`` in dev, ``FS_U_DEV`` is checked before ``FS_U``. Values are
        placed only in process memory and are never printed or serialized.
        """
        status: dict[str, bool] = {}
        for raw_name in names:
            name = str(raw_name).strip()
            if not name:
                continue
            scoped = f"{name}_{self.env.upper()}"
            source_marker = f"NIH_TRACKER_SECRET_SOURCE_{name}"
            scoped_value = self._provider(scoped)
            generic_value = self._provider(name)
            if scoped_value:
                value = scoped_value
                os.environ[source_marker] = scoped
            elif generic_value:
                value = generic_value
                os.environ[source_marker] = name
            else:
                prior_source = os.environ.get(source_marker, "")
                if prior_source and prior_source not in {name, scoped}:
                    os.environ.pop(name, None)
                    os.environ.pop(source_marker, None)
                value = os.environ.get(name)
            if value:
                os.environ[name] = str(value)
                status[name] = True
            else:
                status[name] = False
        missing = sorted(name for name in required if not status.get(name, bool(os.getenv(name))))
        if not quiet:
            print("Secret availability (values are never displayed):")
            for name in sorted(status):
                print(f"  {'AVAILABLE' if status[name] else 'MISSING'}  {name}")
        if missing:
            raise RuntimeError(
                "Required Colab secret(s) are unavailable: " + ", ".join(missing)
            )
        return status

    def load_secrets_for_script(
        self,
        script: str | Path,
        *,
        required: Iterable[str] = (),
        quiet: bool = True,
    ) -> dict[str, bool]:
        names = SCRIPT_SECRETS.get(Path(script).name, ())
        combined = tuple(dict.fromkeys([*names, *required]))
        return self.load_secrets(combined, required=required, quiet=quiet)

    def load_pipeline_secrets(self) -> dict[str, bool]:
        return self.load_secrets(BASE_API_SECRETS, quiet=False)

    def clear_pipeline_secrets(self) -> dict[str, bool]:
        """Remove API credentials previously loaded into this notebook process.

        The offline/cached pipeline path calls this before subprocess execution so
        a prior live run in the same Colab runtime cannot leak credentials into a
        nominally offline run. GitHub and reviewer secrets are unaffected.
        """
        status: dict[str, bool] = {}
        for name in BASE_API_SECRETS:
            status[name] = bool(os.environ.pop(name, None))
            os.environ.pop(f"NIH_TRACKER_SECRET_SOURCE_{name}", None)
        return status

    def _git_version(self) -> str:
        try:
            completed = subprocess.run(
                ["git", "--version"],
                check=False,
                text=True,
                capture_output=True,
                timeout=10,
            )
            return (completed.stdout or completed.stderr).strip()
        except Exception as exc:
            return f"unavailable: {exc}"

    def diagnostics(self) -> dict[str, Any]:
        input_dir = Path(self.common.INP)
        card_dir = Path(self.common.CARDS)
        ledger_path = Path(self.common.LEDGER)
        issue_path = Path(self.common.ISSUES_LOG)
        card_scan_error = ""
        try:
            card_count: int | None = len(self.common.all_cards())
        except Exception as exc:
            # Diagnostics must remain available when one malformed card is the
            # failure being investigated. The pipeline's own gates still fail
            # on the malformed card; this record captures the diagnostic gap.
            card_count = None
            card_scan_error = f"{type(exc).__name__}: {exc}"
        diagnostics = {
            "schema": "nih-tracker-notebook-diagnostics/v1",
            "captured_at": _utc_now(),
            "notebook": self.notebook_name,
            "session_id": self.session_id,
            "environment": self.env,
            "project": str(self.project),
            "scripts": str(self.scripts),
            "python": sys.version.replace("\n", " "),
            "platform": platform.platform(),
            "git": self._git_version(),
            "production_writes_enabled": self.enable_prod_writes,
            "credentials_allowed_by_common": bool(self.common.CREDENTIALS_ALLOWED),
            "re3data_cache": str(self.common.RE3DATA_CATALOG_CACHE),
            "re3data_cache_read_only": bool(self.common.RE3DATA_CACHE_READ_ONLY),
            "resource_profile_cache_read_only": bool(
                self.common.RESOURCE_PROFILE_CACHE_READ_ONLY
            ),
            "cards": card_count,
            "card_scan_error": card_scan_error,
            "inputs": len(list(input_dir.iterdir())) if input_dir.is_dir() else 0,
            "python_scripts": len(list(self.scripts.glob("*.py"))),
            "card_lock": self.common.card_lock_status(),
            "ledger_exists": ledger_path.is_file(),
            "issues_exists": issue_path.is_file(),
            "debug_dir": str(self.debug_dir),
            "configured_roots": self.common.configured_project_roots(),
        }
        self._write_json(self.debug_dir / "diagnostics.json", diagnostics)
        return diagnostics

    def show_diagnostics(self) -> dict[str, Any]:
        diagnostics = self.diagnostics()
        print(json.dumps(diagnostics, indent=2, default=str))
        return diagnostics

    def _ledger_rows_for_run(self, run_id: str) -> list[dict[str, Any]]:
        path = Path(self.common.LEDGER)
        if not path.is_file() or path.stat().st_size == 0:
            return []
        try:
            import pandas as pd

            frame = pd.read_csv(path, dtype=str).fillna("")
            if "run_id" not in frame.columns:
                return []
            return frame.loc[frame["run_id"] == run_id].to_dict("records")
        except Exception:
            return []

    @contextlib.contextmanager
    def logged_operation(self, label: str):
        """Tee Python-level output to a stable notebook debug log."""
        self._operation_counter += 1
        prefix = f"{self._operation_counter:02d}_{_safe_slug(label)}"
        log_path = self.debug_dir / f"{prefix}.log"
        started = _utc_now()
        with log_path.open("w", encoding="utf-8") as handle:
            tee_out = _Tee(sys.stdout, handle)
            tee_err = _Tee(sys.stderr, handle)
            with contextlib.redirect_stdout(tee_out), contextlib.redirect_stderr(tee_err):
                print(f"operation={label} | started_at={started} | env={self.env}")
                try:
                    yield log_path
                except Exception:
                    traceback.print_exc()
                    raise
                finally:
                    print(f"operation={label} | finished_at={_utc_now()} | log={log_path}")

    def run_script(
        self,
        script: str | Path,
        arguments: Sequence[str] = (),
        *,
        label: str | None = None,
        writes_cards: bool = False,
        required_secrets: Iterable[str] = (),
        strict_gate: bool = True,
        load_script_secrets: bool = True,
    ) -> RunResult:
        script_path = Path(script)
        if not script_path.is_absolute():
            script_path = self.scripts / script_path
        script_path = script_path.resolve()
        if not script_path.is_file():
            raise FileNotFoundError(script_path)
        operation = label or script_path.stem
        required_secret_names = tuple(
            dict.fromkeys(str(name).strip() for name in required_secrets if str(name).strip())
        )
        needed_secrets = tuple(
            dict.fromkeys(
                [*SCRIPT_SECRETS.get(script_path.name, ()), *required_secret_names]
            )
        ) if load_script_secrets else required_secret_names
        if needed_secrets:
            self.load_secrets(
                needed_secrets,
                required=required_secret_names,
                quiet=True,
            )
        if writes_cards and self.common.card_lock_status():
            raise RuntimeError(
                "cards/.lock is already held; inspect it in the maintenance notebook."
            )
        if writes_cards and self.env == "prod" and not self.enable_prod_writes:
            raise RuntimeError(
                "Production card writes are disabled. Re-run setup with the production "
                "write acknowledgement checked."
            )

        run_id = (
            f"{_safe_slug(operation)}-{dt.datetime.now().strftime('%Y%m%dT%H%M%S%f')}-"
            f"{uuid.uuid4().hex[:8]}"
        )
        command = [sys.executable, str(script_path), *[str(value) for value in arguments]]
        child_env = os.environ.copy()
        # Give each subprocess only the managed secrets declared for that script.
        # Parent-notebook availability checks never imply broad child-process access.
        for secret_name in ALL_MANAGED_SECRETS:
            if secret_name not in needed_secrets:
                child_env.pop(secret_name, None)
            child_env.pop(f"NIH_TRACKER_SECRET_SOURCE_{secret_name}", None)
        child_env["NIH_TRACKER_RUN_ID"] = run_id
        child_env["NIH_TRACKER_NOTEBOOK_DEBUG_DIR"] = str(self.debug_dir)
        child_env["PYTHONUNBUFFERED"] = "1"
        if writes_cards and self.env == "prod":
            child_env["NIH_TRACKER_PROD_WRITE_CONFIRMATION"] = (
                self.common.PROD_WRITE_CONFIRMATION_TOKEN
            )
        elif self.env != "prod":
            child_env.pop("NIH_TRACKER_PROD_WRITE_CONFIRMATION", None)

        self._operation_counter += 1
        prefix = f"{self._operation_counter:02d}_{_safe_slug(operation)}"
        log_path = self.debug_dir / f"{prefix}.log"
        result_path = self.debug_dir / f"{prefix}.result.json"
        started_at = _utc_now()
        started_monotonic = time.monotonic()
        print("\n" + "=" * 100)
        print(f"{operation} | env={self.env} | run_id={run_id}")
        print("$ " + shlex.join(command))
        print(f"debug_log={log_path}")
        print("=" * 100)

        returncode = 1
        try:
            with log_path.open("w", encoding="utf-8") as handle:
                handle.write(
                    f"operation={operation}\nenvironment={self.env}\nrun_id={run_id}\n"
                    f"started_at={started_at}\ncommand={shlex.join(command)}\n\n"
                )
                process = subprocess.Popen(
                    command,
                    env=child_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                assert process.stdout is not None
                with process.stdout:
                    for line in process.stdout:
                        print(line, end="")
                        handle.write(line)
                        handle.flush()
                returncode = process.wait()
        except Exception:
            error_text = traceback.format_exc()
            print(error_text)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write("\n" + error_text)
            returncode = 1

        ledger_rows = self._ledger_rows_for_run(run_id)
        gate_values: list[bool] = []
        for row in ledger_rows:
            raw = row.get("gate_passed", "")
            if str(raw).strip():
                gate_values.append(_truthy(raw))
        gate_passed = all(gate_values) if gate_values else None
        ok = returncode == 0 and (gate_passed is not False or not strict_gate)
        finished_at = _utc_now()
        result = RunResult(
            label=operation,
            script=str(script_path),
            run_id=run_id,
            command=command,
            returncode=returncode,
            gate_passed=gate_passed,
            ok=ok,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=round(time.monotonic() - started_monotonic, 3),
            log_path=str(log_path),
            result_path=str(result_path),
            ledger_rows=len(ledger_rows),
        )
        self._write_json(result_path, result.to_dict())
        self._run_results.append(result)
        print(
            f"DEBUG RESULT | status={'PASS' if result.ok else 'FAIL'} | "
            f"returncode={result.returncode} | gate={result.gate_passed} | "
            f"ledger_rows={result.ledger_rows} | duration={result.duration_seconds}s"
        )
        print(f"DEBUG ARTIFACTS | log={log_path} | result={result_path}")
        return result

    def compile_scripts(self) -> RunResult:
        scripts = sorted(self.scripts.glob("*.py"))
        if not scripts:
            raise RuntimeError(f"No Python scripts found in {self.scripts}")
        checker = self.debug_dir / "compile_all.py"
        checker.write_text(
            "import py_compile, sys\n"
            "for path in sys.argv[1:]: py_compile.compile(path, doraise=True)\n",
            encoding="utf-8",
        )
        return self.run_script(
            checker,
            [str(path) for path in scripts],
            label="compile_all_scripts",
            strict_gate=False,
        )

    def smoke_suite(
        self,
        *,
        required_inputs: Iterable[str],
        required_project_files: Iterable[str] = (),
        external: bool = True,
    ) -> dict[str, Any]:
        """Run the mandatory pre-pipeline smoke suite and persist its report."""
        rows: list[dict[str, Any]] = []
        compile_result = self.compile_scripts()
        rows.append(
            {
                "check": "compile_all_scripts",
                "passed": compile_result.ok,
                "detail": compile_result.log_path,
            }
        )
        for name in required_inputs:
            path = Path(self.common.INP) / name
            rows.append(
                {
                    "check": f"input:{name}",
                    "passed": path.is_file(),
                    "detail": str(path),
                }
            )
        for name in required_project_files:
            path = self.project / name
            rows.append(
                {
                    "check": f"project_file:{name}",
                    "passed": path.is_file(),
                    "detail": str(path),
                }
            )
        rows.append(
            {
                "check": "card_lock_available",
                "passed": self.common.card_lock_status() is None,
                "detail": str(self.common.card_lock_status() or "not held"),
            }
        )
        try:
            cards = self.common.all_cards()
            rows.append(
                {
                    "check": "card_yaml_parse",
                    "passed": True,
                    "detail": f"{len(cards)} top-level card(s) parsed",
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "check": "card_yaml_parse",
                    "passed": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
        if external:
            smoke = self.run_script(
                "verify_external_ids.py",
                ["--smoke-test-only", "--strict-errors", "--verbose"],
                label="external_service_smoke",
                strict_gate=False,
                load_script_secrets=True,
            )
            rows.append(
                {
                    "check": "external_service_smoke",
                    "passed": smoke.ok,
                    "detail": smoke.log_path,
                }
            )
        else:
            rows.append(
                {
                    "check": "external_service_smoke",
                    "passed": True,
                    "detail": "skipped by explicit offline/cached-only pipeline selection",
                }
            )
        passed = all(bool(row["passed"]) for row in rows)
        report = {
            "schema": "nih-tracker-smoke-suite/v1",
            "captured_at": _utc_now(),
            "environment": self.env,
            "passed": passed,
            "checks": rows,
        }
        report_path = self.debug_dir / "smoke_report.json"
        self._write_json(report_path, report)
        print("\nSMOKE SUITE")
        for row in rows:
            print(f"  {'PASS' if row['passed'] else 'FAIL'}  {row['check']}: {row['detail']}")
        print(f"Overall: {'PASS' if passed else 'FAIL'} | report={report_path}")
        return report

    def recent_ledger(self, limit: int = 20) -> list[dict[str, Any]]:
        path = Path(self.common.LEDGER)
        if not path.is_file() or path.stat().st_size == 0:
            return []
        try:
            import pandas as pd

            frame = pd.read_csv(path, dtype=str).fillna("")
            return frame.tail(max(1, int(limit))).iloc[::-1].to_dict("records")
        except Exception:
            return []

    def open_issues(self, limit: int = 250) -> list[dict[str, Any]]:
        path = Path(self.common.ISSUES_LOG)
        if not path.is_file() or path.stat().st_size == 0:
            return []
        try:
            import pandas as pd

            frame = pd.read_csv(path, dtype=str).fillna("")
            if "resolved" in frame.columns:
                resolved = frame["resolved"].map(_truthy)
                frame = frame.loc[~resolved]
            ranks = {"blocking": 3, "needs_attention": 2, "advisory": 1}
            if "severity" in frame.columns:
                frame["_rank"] = frame["severity"].map(ranks).fillna(0)
            else:
                frame["_rank"] = 0
            if "times_seen" in frame.columns:
                frame["_times"] = frame["times_seen"].apply(
                    lambda value: int(float(value)) if str(value).strip() else 0
                )
            else:
                frame["_times"] = 0
            frame = frame.sort_values(["_rank", "_times"], ascending=False)
            return frame.drop(columns=["_rank", "_times"]).head(limit).to_dict("records")
        except Exception:
            return []

    def run_results(self) -> list[dict[str, Any]]:
        return [result.to_dict() for result in self._run_results]

    def write_session_summary(self) -> Path:
        summary_path = self.debug_dir / "session_summary.json"
        self._write_json(
            summary_path,
            {
                "schema": "nih-tracker-notebook-session-summary/v1",
                "notebook": self.notebook_name,
                "session_id": self.session_id,
                "environment": self.env,
                "captured_at": _utc_now(),
                "runs": self.run_results(),
                "recent_ledger": self.recent_ledger(20),
                "open_issues": self.open_issues(250),
            },
        )
        print(f"Session summary: {summary_path}")
        return summary_path
