"""Feedback about ModelBench itself, kept outside artifact review records."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .util import append_jsonl, read_jsonl, utc_now


def feedback_path(generated_root: Path) -> Path:
    return Path(generated_root) / "system-feedback" / "events.jsonl"


def add_system_feedback(generated_root: Path, run: str, message: str) -> dict[str, Any]:
    if not message.strip():
        raise ValueError("Feedback message may not be empty")
    record = {
        "schema_version": 1,
        "type": "system_feedback_submitted",
        "id": "sfb_" + uuid.uuid4().hex,
        "run_id": run,
        "message": message.strip(),
        "timestamp": utc_now(),
    }
    append_jsonl(feedback_path(generated_root), record)
    return record


def system_feedback_events(generated_root: Path) -> list[dict[str, Any]]:
    return read_jsonl(feedback_path(generated_root))
