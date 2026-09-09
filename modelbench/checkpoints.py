from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .blender import STANDARD_VIEWS, run_blender
from .config import load_toml
from .errors import StateError, ValidationError
from .evidence import evidence_records
from .feedback import consume_feedback, pending_feedback
from .runs import verify_run_snapshot
from .state import load_state, save_state, transition
from .util import atomic_write_json, copy_file_owned, read_json, resolve_within, utc_now


def _checkpoint_lock(run_dir: Path):
    from .locking import OperationLock

    return OperationLock(run_dir / "checkpoints", "agent-checkpoint", run_dir.name)


def create_checkpoint(
    root: Path,
    run_dir: Path,
    source: Path,
    phase: str,
    *,
    views: list[str] | None = None,
    diagnostic_cameras: list[dict[str, Any]] | None = None,
    observations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    verify_run_snapshot(run_dir)
    workspace = (run_dir / "workspace").resolve()
    source = resolve_within(workspace, source)
    if source.suffix.lower() != ".blend" or not source.is_file():
        raise ValidationError("Checkpoint source must be a .blend file inside the run workspace")
    if not phase.strip() or len(phase) > 128:
        raise ValidationError("Checkpoint phase must be 1-128 characters")
    unknown_views = sorted(set(views or STANDARD_VIEWS) - set(STANDARD_VIEWS))
    if unknown_views:
        raise ValidationError(f"Unknown evaluation views: {', '.join(unknown_views)}")
    with _checkpoint_lock(run_dir):
        state = load_state(run_dir)
        if state["state"] not in {"modeling", "researching", "checkpointing"}:
            raise StateError(f"Cannot checkpoint while run is {state['state']}")
        limit = int(read_json(run_dir / "run.json")["agent_profile"]["data"].get("limits", {}).get("max_checkpoints", 32))
        number = int(state.get("observed_checkpoints", 0)) + 1
        if number > limit:
            raise StateError(f"Agent checkpoint limit exceeded ({limit})")
        checkpoint_id = f"checkpoint_{number:04d}"
        checkpoint_dir = run_dir / "checkpoints" / checkpoint_id
        if checkpoint_dir.exists():
            raise StateError(f"Checkpoint already exists: {checkpoint_id}")
        staging_dir = run_dir / "checkpoints" / f".{checkpoint_id}-attempt-{os.getpid()}"
        if staging_dir.exists() or staging_dir.is_symlink():
            raise StateError(f"Unexpected checkpoint staging path: {staging_dir}")
        staging_dir.mkdir()
        source_state = state["state"]
        parent = state.get("latest_checkpoint")
        pending = pending_feedback(run_dir)
        transition(run_dir, "checkpointing", detail={"checkpoint": checkpoint_id, "phase": phase})
        try:
            owned = copy_file_owned(source, staging_dir / "model.blend")
            metadata = read_json(run_dir / "run.json")
            staging_preview = staging_dir / "preview"
            result = run_blender(
                root,
                run_dir,
                staging_dir / "model.blend",
                staging_preview,
                read_json(run_dir / "snapshot" / "task.json", metadata.get("task_config")),
                load_toml(run_dir / "snapshot" / "checkpoint_profile.toml"),
                mode="checkpoint",
                timeout=int(metadata["agent_profile"]["data"].get("limits", {}).get("blender_seconds", 3600)),
                views=views,
                diagnostic_cameras=diagnostic_cameras,
            )

            def relocate(value: Any) -> Any:
                if isinstance(value, str):
                    return value.replace(str(staging_dir), str(checkpoint_dir))
                if isinstance(value, list):
                    return [relocate(item) for item in value]
                if isinstance(value, dict):
                    return {key: relocate(item) for key, item in value.items()}
                return value

            result = relocate(result)
            owned["path"] = str(checkpoint_dir / "model.blend")
            record = {
                "schema_version": 1,
                "id": checkpoint_id,
                "phase": phase.strip(),
                "parent_checkpoint": parent,
                "created_at": utc_now(),
                "artifact": owned,
                "evidence_ids": [item["id"] for item in evidence_records(run_dir)],
                "feedback_ids": [item["id"] for item in pending],
                "evaluation_views": views or STANDARD_VIEWS,
                "diagnostic_cameras": diagnostic_cameras or [],
                "observations": observations or {},
                "preview_result": result,
            }
            atomic_write_json(staging_dir / "checkpoint.json", record)
            os.replace(staging_dir, checkpoint_dir)
        except BaseException:
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            if load_state(run_dir)["state"] == "checkpointing":
                transition(run_dir, source_state, detail={"checkpoint": checkpoint_id, "rolled_back": True})
            raise

        preview_dir = checkpoint_dir / "preview"
        consume_feedback(run_dir, checkpoint_id, pending)
        state = load_state(run_dir)
        state["observed_checkpoints"] = number
        state["latest_checkpoint"] = checkpoint_id
        save_state(run_dir, state)
        control = read_json(run_dir / "control.json", {})
        paused = bool(control.get("pause_requested"))
        transition(run_dir, "awaiting_feedback" if paused else "modeling", detail={"checkpoint": checkpoint_id})
        return {
            "checkpoint": checkpoint_id,
            "owned_model": str(checkpoint_dir / "model.blend"),
            "previews": [str(preview_dir / ("orthographic" if view in STANDARD_VIEWS[:6] else "turntable") / f"{view}.png") for view in (views or STANDARD_VIEWS)],
            "feedback_consumed": pending,
            "pause_requested": paused,
        }
