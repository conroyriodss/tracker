"""Record observed code identity without inventing a Git commit for ZIP installs."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import subprocess
from pathlib import Path

from run_provenance import digest


def describe_code(root: Path) -> dict:
    root = root.resolve()
    files = {}
    for directory in ("scripts", "schemas", "templates"):
        for path in sorted((root / directory).rglob("*")):
            if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
                files[path.relative_to(root).as_posix()] = digest(path)
    for pattern in ("requirements*.txt", "frontend.html", "build_notebooks.py", "extend*_notebooks.py", "nih_tracker_colab_v19_*.ipynb"):
        for path in sorted(root.glob(pattern)):
            if path.is_file() and not path.is_symlink():
                files[path.relative_to(root).as_posix()] = digest(path)
    tree = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    commit = None
    dirty = None
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        # Require this code file to be tracked, not merely inside an unrelated repo.
        tracked = subprocess.run(["git", "-C", str(root), "ls-files", "--error-unmatch", "scripts/code_provenance.py"], capture_output=True, text=True, timeout=5)
        if result.returncode == tracked.returncode == 0:
            commit = result.stdout.strip()
            state = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", "."], capture_output=True, text=True, timeout=5, check=True)
            dirty = bool(state.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    manifest = root / "RELEASE_MANIFEST.json"
    versions = {}
    for name in ("pandas", "PyYAML", "requests", "beautifulsoup4", "nbformat", "ipywidgets", "jsonschema"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {"schema": "nih-tracker-code-provenance/v1", "git_commit": commit, "git_dirty": dirty,
            "source_tree_sha256": tree, "file_count": len(files), "files": files,
            "release_manifest_sha256": digest(manifest) if manifest.is_file() else None,
            "installed_dependencies": versions,
            "identity_note": "git_commit is null for an unversioned ZIP installation; source_tree_sha256 identifies the observed source bytes"}
