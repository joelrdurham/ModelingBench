from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .errors import ValidationError

ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(os.path, "isjunction", lambda _: False)(path))


def canonical_hash(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return sha256_bytes(data)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValidationError(f"Invalid JSONL at {path}:{number}: {exc}") from exc
    return records


def ensure_id(value: str, label: str = "ID") -> str:
    if not ID_RE.fullmatch(value):
        raise ValidationError(f"{label} must match {ID_RE.pattern}: {value!r}")
    return value


def resolve_within(base: Path, candidate: Path | str, *, must_exist: bool = True) -> Path:
    base = base.resolve()
    raw = Path(candidate)
    resolved = (base / raw).resolve() if not raw.is_absolute() else raw.resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValidationError(f"Path escapes allowed root {base}: {candidate}") from exc
    if must_exist and not resolved.exists():
        raise ValidationError(f"Required path does not exist: {resolved}")
    return resolved


def file_inventory(root: Path, *, exclude: Iterable[str] = ()) -> list[dict[str, Any]]:
    excluded = set(exclude)
    items = []
    for path in sorted(root.rglob("*"), key=lambda p: p.as_posix()):
        if path.is_file() and not any(part in excluded for part in path.relative_to(root).parts):
            items.append({
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            })
    return items


def copy_file_owned(source: Path, destination: Path) -> dict[str, Any]:
    import shutil

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValidationError(f"Owned destination already exists: {destination}")
    shutil.copy2(source, destination)
    return {"path": str(destination), "size": destination.stat().st_size, "sha256": sha256_file(destination)}
