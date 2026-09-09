from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import append_jsonl, read_jsonl, utc_now


def feedback_events(run_dir: Path) -> list[dict[str, Any]]:
    return read_jsonl(run_dir / "feedback" / "events.jsonl")


def add_feedback(run_dir: Path, message: str) -> dict[str, Any]:
    if not message.strip():
        raise ValueError("Feedback message may not be empty")
    events = feedback_events(run_dir)
    submitted = [e for e in events if e.get("type") == "feedback_submitted"]
    record = {
        "schema_version": 1,
        "type": "feedback_submitted",
        "id": f"fb_{len(submitted) + 1:04d}",
        "message": message.strip(),
        "created_at": utc_now(),
    }
    append_jsonl(run_dir / "feedback" / "events.jsonl", record)
    return record


def pending_feedback(run_dir: Path) -> list[dict[str, Any]]:
    events = feedback_events(run_dir)
    consumed = {item for event in events if event.get("type") == "feedback_consumed" for item in event.get("feedback_ids", [])}
    return [event for event in events if event.get("type") == "feedback_submitted" and event.get("id") not in consumed]


def consume_feedback(run_dir: Path, checkpoint_id: str, pending: list[dict[str, Any]]) -> None:
    if not pending:
        return
    append_jsonl(run_dir / "feedback" / "events.jsonl", {
        "schema_version": 1,
        "type": "feedback_consumed",
        "checkpoint_id": checkpoint_id,
        "feedback_ids": [event["id"] for event in pending],
        "created_at": utc_now(),
    })

