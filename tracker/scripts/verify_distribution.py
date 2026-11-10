"""Verify a supplied distribution inventory without importing tracker modules."""
import hashlib
from pathlib import Path


def verify(root):
    count = 0
    for line in (root / "SHA256SUMS").read_text().splitlines():
        if not line.strip():
            continue
        expected, name = line.split(maxsplit=1)
        relative = Path(name.lstrip("*"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe distribution path")
        target = root / relative
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"Missing or linked distribution file: {relative}")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"Distribution hash mismatch: {relative}")
        count += 1
    if not count:
        raise ValueError("Empty inventory")
    return count


if __name__ == "__main__":
    print(f"Verified {verify(Path(__file__).resolve().parents[1])} distribution files")
