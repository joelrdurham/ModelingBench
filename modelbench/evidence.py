from __future__ import annotations

import mimetypes
import shutil
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .util import append_jsonl, read_jsonl, resolve_within, sha256_file, utc_now


def evidence_records(run_dir: Path) -> list[dict[str, Any]]:
    return read_jsonl(run_dir / "evidence" / "registry.jsonl")


def register_evidence(
    run_dir: Path,
    source: Path,
    *,
    origin: str,
    title: str | None = None,
    source_url: str | None = None,
    media_type: str | None = None,
    view_classification: str = "unknown",
    known_dimension: dict[str, Any] | None = None,
    notes: str | None = None,
    confidence: float | None = None,
    provided_input_id: str | None = None,
    byte_limit: int | None = None,
) -> dict[str, Any]:
    if origin not in {"provided", "agent_researched"}:
        raise ValidationError(f"Invalid evidence origin: {origin}")
    if view_classification not in {"orthographic", "perspective", "section", "plan", "unknown"}:
        raise ValidationError(f"Invalid view classification: {view_classification}")
    if confidence is not None and not 0 <= confidence <= 1:
        raise ValidationError("Evidence confidence must be between 0 and 1")
    if not source.is_file() or source.is_symlink():
        raise ValidationError(f"Evidence must be a regular non-linked file: {source}")
    existing = evidence_records(run_dir)
    if byte_limit is not None:
        used = sum(int(record.get("size", 0)) for record in existing if record.get("origin") == "agent_researched")
        if origin == "agent_researched" and used + source.stat().st_size > byte_limit:
            raise ValidationError(f"Research storage limit exceeded ({byte_limit} bytes)")
    ev_id = f"ev_{len(existing) + 1:04d}"
    suffix = source.suffix.lower()[:16]
    destination = run_dir / "evidence" / "files" / f"{ev_id}{suffix}"
    if destination.exists():
        raise ValidationError(f"Evidence destination already exists: {destination}")
    shutil.copy2(source, destination)
    record = {
        "schema_version": 1,
        "id": ev_id,
        "origin": origin,
        "provided_input_id": provided_input_id,
        "local_path": destination.relative_to(run_dir).as_posix(),
        "sha256": sha256_file(destination),
        "size": destination.stat().st_size,
        "original_url": source_url,
        "title": title or source.name,
        "source": source_url,
        "retrieved_at": utc_now(),
        "media_type": media_type or mimetypes.guess_type(source.name)[0] or "application/octet-stream",
        "view_classification": view_classification,
        "known_dimension": known_dimension,
        "notes": notes,
        "confidence": confidence,
    }
    append_jsonl(run_dir / "evidence" / "registry.jsonl", record)
    return record


def register_provided_inputs(run_dir: Path, inputs: tuple[Any, ...]) -> list[dict[str, Any]]:
    records = []
    for item in inputs:
        records.append(register_evidence(
            run_dir,
            item.path,
            origin="provided",
            title=item.title or item.id,
            source_url=item.source_url,
            media_type=item.media_type,
            view_classification=item.view_classification,
            known_dimension=item.known_dimension,
            provided_input_id=item.id,
            notes=item.notes or f"role={item.role}; use={item.use}",
            confidence=item.confidence if item.confidence is not None else 1.0,
        ))
    return records


def register_measurement(
    run_dir: Path,
    *,
    name: str,
    value: float,
    unit: str,
    provenance: str,
    evidence_ids: list[str],
    notes: str | None = None,
) -> dict[str, Any]:
    if provenance not in {"authoritative", "derived", "visual_estimate"}:
        raise ValidationError(f"Invalid measurement provenance: {provenance}")
    known = {record["id"] for record in evidence_records(run_dir)}
    missing = sorted(set(evidence_ids) - known)
    if missing:
        raise ValidationError(f"Unknown evidence IDs: {', '.join(missing)}")
    existing = read_jsonl(run_dir / "evidence" / "measurements.jsonl")
    record = {
        "schema_version": 1,
        "id": f"ms_{len(existing) + 1:04d}",
        "name": name,
        "value": float(value),
        "unit": unit,
        "provenance": provenance,
        "evidence_ids": evidence_ids,
        "notes": notes,
        "created_at": utc_now(),
    }
    append_jsonl(run_dir / "evidence" / "measurements.jsonl", record)
    return record
