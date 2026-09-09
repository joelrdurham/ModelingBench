"""Creation of isolated, sparse task briefs supplied at run time."""
from __future__ import annotations

import mimetypes
import secrets
import shutil
from pathlib import Path
from typing import Iterable

from .errors import ValidationError
from .project import ensure_generated_root
from .util import is_link, sha256_file, utc_now

MAX_REFERENCE_BYTES = 16 * 1024 * 1024
MAX_REFERENCE_TOTAL = 64 * 1024 * 1024
MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".pdf": "application/pdf", ".txt": "text/plain",
}


def create_brief_task(
    root: Path, brief: str, notes: str = "", references: Iterable[Path] = (),
    research_mode: str = "live",
) -> str:
    """Freeze a user brief and copied references beneath ``generated/briefs``.

    The generated task deliberately contains no guessed measurements or geometry.
    It is a v2 specification whose requirements are discovered during the run.
    """
    if not isinstance(brief, str) or not brief.strip() or len(brief) > 16_000:
        raise ValidationError("brief must contain 1-16000 characters")
    if not isinstance(notes, str) or len(notes) > 16_000:
        raise ValidationError("notes must contain at most 16000 characters")
    if research_mode not in {"live", "offline"}:
        raise ValidationError("research_mode must be live or offline")
    generated = ensure_generated_root(Path(root).resolve())
    for parent in (generated / "briefs", generated / "briefs" / "tasks"):
        if is_link(parent): raise ValidationError("brief task parents may not be links or junctions")
    task_id = "brief_" + secrets.token_hex(12)
    directory = generated / "briefs" / "tasks" / task_id
    inputs = directory / "inputs"
    inputs.mkdir(parents=True, exist_ok=False)
    records: list[dict[str, str | int]] = []
    total = 0
    for number, raw_source in enumerate(references, 1):
        source = Path(raw_source)
        if not source.is_file() or is_link(source):
            raise ValidationError("brief references must be regular non-linked files")
        suffix = source.suffix.lower()
        media_type = MEDIA_TYPES.get(suffix)
        if media_type is None:
            raise ValidationError(f"unsupported brief reference type: {suffix or 'no extension'}")
        size = source.stat().st_size
        if size > MAX_REFERENCE_BYTES or total + size > MAX_REFERENCE_TOTAL:
            raise ValidationError("brief references exceed the upload size limit")
        # Do not retain a user filename in the generated task tree.
        opaque = f"ref_{number:03d}_{secrets.token_hex(8)}{suffix}"
        destination = inputs / opaque
        shutil.copyfile(source, destination)
        if is_link(destination) or destination.stat().st_size != size:
            raise ValidationError("could not safely own brief reference")
        total += size
        role = "reference_image" if media_type.startswith("image/") else "dimension_document" if suffix == ".pdf" else "other"
        records.append({"id": f"reference_{number:03d}", "role": role, "use": "reference", "path": opaque,
                        "media_type": media_type, "size": size, "sha256": sha256_file(destination)})
    (directory / "prompt.md").write_text(brief.strip() + ("\n\nNotes:\n" + notes.strip() if notes.strip() else "") + "\n", encoding="utf-8")
    task_toml = "\n".join((
        "version = 2", f"id = {task_id!r}", 'title = "User brief"', 'specification_mode = "discovered"',
        "", "[research]", "enabled = true", f"mode = {research_mode!r}", "",
    ))
    (directory / "task.toml").write_text(task_toml, encoding="utf-8")
    manifest_lines = ["version = 1", f"created_at = {utc_now()!r}"]
    for record in records:
        manifest_lines.extend(["", "[[inputs]]"] + [f"{key} = {value!r}" for key, value in record.items()])
    (inputs / "manifest.toml").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    return task_id
