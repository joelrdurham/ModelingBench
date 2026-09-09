from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import StateError
from .util import atomic_write_json, read_json, utc_now

TRANSITIONS: dict[str, set[str]] = {
    "created": {"preparing", "failed", "interrupted"},
    "preparing": {"researching", "modeling", "failed", "interrupted"},
    "researching": {"modeling", "checkpointing", "failed", "interrupted"},
    "modeling": {"researching", "checkpointing", "awaiting_feedback", "submitted", "failed", "interrupted"},
    "checkpointing": {"modeling", "researching", "awaiting_feedback", "failed", "interrupted"},
    "awaiting_feedback": {"modeling", "failed", "interrupted"},
    "submitted": {"validating", "failed", "interrupted"},
    "validating": {"final_rendering", "failed", "interrupted"},
    "final_rendering": {"completed", "failed", "interrupted"},
    "completed": {"promoted", "failed"},
    "promoted": set(),
    "failed": {"validating"},
    "interrupted": {"modeling", "validating"},
}


def initial_state(run_id: str, task_id: str) -> dict[str, Any]:
    now = utc_now()
    return {
        "version": 1,
        "run_id": run_id,
        "task_id": task_id,
        "state": "created",
        "created_at": now,
        "updated_at": now,
        "history": [{"state": "created", "at": now}],
        "observed_turns": 0,
        "observed_checkpoints": 0,
        "reported_iterations": None,
        "latest_checkpoint": None,
        "artifact": None,
        "failure": None,
    }


def load_state(run_dir: Path) -> dict[str, Any]:
    state = read_json(run_dir / "state.json")
    if not isinstance(state, dict):
        raise StateError(f"Missing or invalid state for {run_dir.name}")
    return state


def save_state(run_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    atomic_write_json(run_dir / "state.json", state)


def transition(run_dir: Path, target: str, *, detail: dict[str, Any] | None = None, force: bool = False) -> dict[str, Any]:
    state = load_state(run_dir)
    source = state["state"]
    if target == source:
        return state
    if target not in TRANSITIONS.get(source, set()) and not force:
        raise StateError(f"Invalid lifecycle transition {source} -> {target}")
    state["state"] = target
    event: dict[str, Any] = {"state": target, "at": utc_now()}
    if detail:
        event["detail"] = detail
    state.setdefault("history", []).append(event)
    save_state(run_dir, state)
    return state


def fail(run_dir: Path, stage: str, message: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    state = load_state(run_dir)
    state["failure"] = {"stage": stage, "message": message, "details": details or {}, "at": utc_now()}
    save_state(run_dir, state)
    return transition(run_dir, "failed", detail={"stage": stage, "message": message}, force=True)

