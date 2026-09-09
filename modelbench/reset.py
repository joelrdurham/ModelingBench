from __future__ import annotations

import shutil
from pathlib import Path

from .errors import ValidationError
from .locking import OperationLock
from .project import MARKER, MARKER_CONTENT, ensure_generated_root
from .util import is_link

CONFIRMATION = "DELETE-ALL-GENERATED"


def reset_all(root: Path, *, dry_run: bool, confirmation: str | None = None) -> list[str]:
    generated = ensure_generated_root(root)
    if is_link(root / "generated") or generated.resolve().parent != root.resolve():
        raise ValidationError("Unsafe generated root; refusing reset")
    marker = generated / MARKER
    if not marker.is_file() or is_link(marker):
        raise ValidationError("Generated-root safety marker missing or invalid")
    with OperationLock(generated, "reset"):
        targets = sorted(
            [p for p in generated.iterdir() if p.name not in {MARKER, ".modelbench-operation.lock"}],
            key=lambda p: p.name,
        )
        display = [str(path.resolve()) for path in targets]
        if dry_run:
            return display
        if confirmation != CONFIRMATION:
            raise ValidationError(f"Reset requires --confirm {CONFIRMATION}")
        for target in targets:
            resolved = target.resolve()
            try:
                resolved.relative_to(generated)
            except ValueError as exc:
                raise ValidationError(f"Reset target escaped generated root: {target}") from exc
            if is_link(target):
                raise ValidationError(f"Refusing reset because generated child is a link: {target}")
        for target in targets:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        marker.write_text(MARKER_CONTENT, encoding="utf-8")
        return display
