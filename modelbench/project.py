from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .util import is_link

MARKER = ".modelbench-generated-root"
MARKER_CONTENT = "ModelingBench generated data root. Safe reset boundary.\n"


def find_project_root(start: Path | None = None) -> Path:
    override = os.environ.get("MODELBENCH_ROOT")
    if override:
        candidate = Path(override).resolve()
        if (candidate / "pyproject.toml").is_file():
            return candidate
        raise ValidationError(f"MODELBENCH_ROOT is not a ModelingBench project: {candidate}")
    cursor = (start or Path.cwd()).resolve()
    for candidate in (cursor, *cursor.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "modelbench").is_dir():
            return candidate
    raise ValidationError("No ModelingBench project root found")


def ensure_generated_root(root: Path) -> Path:
    raw_generated = root / "generated"
    if is_link(raw_generated):
        raise ValidationError("generated/ must not be a link or junction")
    generated = raw_generated.resolve()
    if generated.parent != root.resolve():
        raise ValidationError("Unsafe generated directory resolution")
    created = not generated.exists()
    if not created and not generated.is_dir():
        raise ValidationError("generated/ must be a real directory")
    if created:
        generated.mkdir(parents=True)
    marker = generated / MARKER
    if not marker.exists():
        if not created and any(generated.iterdir()):
            raise ValidationError("Refusing to adopt a nonempty unmarked generated/ directory")
        marker.write_text(MARKER_CONTENT, encoding="utf-8")
    if not marker.is_file() or is_link(marker):
        raise ValidationError("Invalid generated-root safety marker")
    if marker.read_text(encoding="utf-8") != MARKER_CONTENT:
        raise ValidationError("Generated-root safety marker has unexpected content")
    return generated


def git_identity(root: Path) -> dict[str, Any]:
    def call(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=root, text=True, capture_output=True, check=False)

    safe = f"safe.directory={root.resolve().as_posix()}"
    top = call(["git", "-c", safe, "rev-parse", "--show-toplevel"])
    if top.returncode != 0:
        return {"revision": None, "dirty": None, "status": "not_a_repository"}
    head = call(["git", "-c", safe, "rev-parse", "HEAD"])
    status = call(["git", "-c", safe, "status", "--porcelain=v1", "--untracked-files=all"])
    return {
        "revision": head.stdout.strip() if head.returncode == 0 else None,
        "dirty": bool(status.stdout.strip()),
        "status": "ok" if head.returncode == 0 else "unborn",
        "worktree": top.stdout.strip(),
    }
