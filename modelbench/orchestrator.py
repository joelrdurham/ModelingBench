from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .adapter import AgentResult, run_adapter
from .blender import STANDARD_VIEWS, run_blender
from .checkpoints import create_checkpoint
from .config import AgentProfile, load_agent_profile, load_render_profile, load_task, load_toml
from .errors import StateError, ValidationError
from .evidence import register_provided_inputs
from .locking import OperationLock
from .project import ensure_generated_root
from .runs import create_run, next_artifact_path, promote, resolve_run, verify_run_snapshot
from .state import fail, load_state, save_state, transition
from .util import atomic_write_json, copy_file_owned, read_json, resolve_within, sha256_file, utc_now

CHECKPOINT_PROFILE = "checkpoint_cycles_aces_v1"
FINAL_PROFILE = "final_cycles_aces_v1"


def prepare_new_run(root: Path, task_id: str, agent_name: str) -> tuple[Path, AgentProfile]:
    generated = ensure_generated_root(root)
    task = load_task(root, task_id)
    agent = load_agent_profile(root, agent_name)
    checkpoint_profile = load_render_profile(root, CHECKPOINT_PROFILE)
    final_profile = load_render_profile(root, FINAL_PROFILE)
    run_dir = create_run(root, generated, task, agent, checkpoint_profile, final_profile)
    transition(run_dir, "preparing")
    register_provided_inputs(run_dir, task.inputs)
    transition(run_dir, "researching" if task.research_enabled else "modeling")
    if task.research_enabled:
        transition(run_dir, "modeling")
    return run_dir, agent


def _owned_submission(run_dir: Path, result: AgentResult) -> Path:
    if not result.blend_path:
        raise ValidationError("Submitted result is missing blend_path")
    source = resolve_within(run_dir / "workspace", result.blend_path)
    if source.suffix.lower() != ".blend" or not source.is_file():
        raise ValidationError("Submitted artifact must be a .blend file inside the workspace")
    destination = next_artifact_path(run_dir)
    copy_file_owned(source, destination)
    state = load_state(run_dir)
    state["artifact"] = {
        "path": destination.relative_to(run_dir).as_posix(),
        "sha256": sha256_file(destination),
        "size": destination.stat().st_size,
        "owned_at": utc_now(),
    }
    state["reported_iterations"] = result.reported_iterations
    save_state(run_dir, state)
    return destination


def _verify_owned_artifact(run_dir: Path, artifact: Path) -> str:
    state = load_state(run_dir)
    info = state.get("artifact")
    if not isinstance(info, dict):
        raise StateError("Run has no owned artifact record")
    recorded = resolve_within(run_dir, info.get("path", ""))
    if artifact.resolve() != recorded or artifact.is_symlink() or not artifact.is_file():
        raise StateError("Evaluation artifact does not match the owned artifact")
    expected = info.get("sha256")
    if not isinstance(expected, str) or sha256_file(artifact) != expected:
        raise StateError("Owned artifact hash changed; refusing evaluation")
    return expected


def _validate_render_result(output_dir: Path, result: dict[str, Any], artifact_sha256: str) -> None:
    expected = {
        f"{'orthographic' if view in STANDARD_VIEWS[:6] else 'turntable'}/{view}.png"
        for view in STANDARD_VIEWS
    }
    if result.get("ok") is not True or result.get("source_sha256") != artifact_sha256:
        raise StateError("Render result is not bound to the owned artifact")
    records = result.get("renders")
    if not isinstance(records, list):
        raise StateError("Render result is missing its standardized render records")
    recorded: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise StateError("Render result contains an invalid render record")
        path = Path(record["path"]).resolve()
        try:
            relative = path.relative_to(output_dir.resolve()).as_posix()
        except ValueError as exc:
            raise StateError("Render result references a file outside its output directory") from exc
        if relative in recorded:
            raise StateError(f"Duplicate render record: {relative}")
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            raise StateError(f"Render output is missing, linked, or empty: {relative}")
        if record.get("size") != path.stat().st_size or record.get("sha256") != sha256_file(path):
            raise StateError(f"Render output does not match its recorded hash: {relative}")
        recorded.add(relative)
    actual = {
        path.relative_to(output_dir).as_posix()
        for path in output_dir.rglob("*.png")
        if path.is_file()
    }
    if recorded != expected or actual != expected:
        raise StateError("Final evaluation must contain exactly the 14 standardized PNG views")


def _record_interrupt(run_dir: Path) -> None:
    state = load_state(run_dir)
    evaluation = state["state"] in {"validating", "final_rendering", "completed"} or (
        state["state"] == "submitted" and bool(state.get("artifact"))
    )
    stage = "evaluation" if evaluation else "agent"
    fail(run_dir, stage, "Interrupted by user")
    transition(run_dir, "interrupted", force=True)


def evaluate_and_promote(root: Path, generated: Path, run_dir: Path, artifact: Path) -> dict[str, Any]:
    verify_run_snapshot(run_dir)
    metadata = read_json(run_dir / "run.json")
    final_profile = load_toml(run_dir / "snapshot" / "final_profile.toml")
    task_config = read_json(run_dir / "snapshot" / "task.json")
    artifact_sha256 = _verify_owned_artifact(run_dir, artifact)
    attempt_number = len(list((run_dir / "renders").glob(".final-attempt-*"))) + 1
    attempt_dir = run_dir / "renders" / f".final-attempt-{attempt_number:04d}"
    final_dir = run_dir / "renders" / "final"
    transition(run_dir, "validating")
    try:
        transition(run_dir, "final_rendering")
        if final_dir.exists():
            result = read_json(final_dir / "blender_result.json")
            if not isinstance(result, dict):
                raise StateError("Existing final evaluation result is missing or invalid")
            _validate_render_result(final_dir, result, artifact_sha256)
        else:
            result = run_blender(
                root,
                run_dir,
                artifact,
                attempt_dir,
                task_config,
                final_profile,
                mode="final",
                timeout=int(metadata["agent_profile"]["data"].get("limits", {}).get("blender_seconds", 3600)),
            )
            _verify_owned_artifact(run_dir, artifact)
            _validate_render_result(attempt_dir, result, artifact_sha256)
            os.replace(attempt_dir, final_dir)

            def relocate(value: Any) -> Any:
                if isinstance(value, str):
                    return value.replace(str(attempt_dir), str(final_dir))
                if isinstance(value, list):
                    return [relocate(item) for item in value]
                if isinstance(value, dict):
                    return {key: relocate(item) for key, item in value.items()}
                return value

            result = relocate(result)
            atomic_write_json(final_dir / "blender_result.json", result)
        _verify_owned_artifact(run_dir, artifact)
        transition(run_dir, "completed")
        publication = promote(
            generated,
            run_dir,
            artifact,
            expected_artifact_sha256=artifact_sha256,
        )
        transition(run_dir, "promoted")
        state = load_state(run_dir)
        state["evaluation"] = result
        state["publication"] = publication
        state["failure"] = None
        save_state(run_dir, state)
        return publication
    except Exception as exc:
        fail(run_dir, "evaluation", str(exc), details={"attempt_directory": str(attempt_dir)})
        raise


def handle_result(root: Path, generated: Path, run_dir: Path, result: AgentResult) -> dict[str, Any]:
    state = load_state(run_dir)
    if state["state"] == "failed":
        raise StateError("Run failed during the agent turn; inspect status and logs")
    state["reported_iterations"] = result.reported_iterations
    save_state(run_dir, state)
    run_name = f"{state['task_id']}/{state['run_id']}"
    if state["state"] == "awaiting_feedback" and result.status in {"checkpoint", "submitted"}:
        source = resolve_within(run_dir / "workspace", result.blend_path or "")
        latest = state.get("latest_checkpoint")
        checkpoint = run_dir / "checkpoints" / str(latest) / "model.blend"
        if not latest or not source.is_file() or not checkpoint.is_file() or sha256_file(source) != sha256_file(checkpoint):
            raise StateError("Agent result does not match the owned checkpoint at the pause boundary")
        return {"run": run_name, "state": "awaiting_feedback"}
    if result.status == "failed":
        fail(run_dir, "agent", result.notes or "Agent reported failure")
        return {"run": run_name, "state": "failed"}
    if result.status == "checkpoint":
        source = resolve_within(run_dir / "workspace", result.blend_path or "")
        latest = state.get("latest_checkpoint")
        already_owned = bool(
            latest
            and (run_dir / "checkpoints" / latest / "model.blend").is_file()
            and sha256_file(source) == sha256_file(run_dir / "checkpoints" / latest / "model.blend")
        )
        if not already_owned:
            if load_state(run_dir)["state"] == "awaiting_feedback":
                raise StateError("Agent returned a checkpoint different from the checkpoint at the pause boundary")
            create_checkpoint(root, run_dir, source, result.phase)
        transition(run_dir, "awaiting_feedback", detail={"phase": result.phase, "feedback_requested": result.feedback_requested})
        return {"run": run_name, "state": "awaiting_feedback"}
    transition(run_dir, "submitted", detail={"phase": result.phase})
    artifact = _owned_submission(run_dir, result)
    publication = evaluate_and_promote(root, generated, run_dir, artifact)
    return {"run": run_name, "state": "promoted", "publication": publication}


def run_new(root: Path, task_id: str, agent_name: str) -> dict[str, Any]:
    generated = ensure_generated_root(root)
    with OperationLock(generated, "run", task_id):
        run_dir: Path | None = None
        try:
            run_dir, agent = prepare_new_run(root, task_id, agent_name)
            return handle_result(root, generated, run_dir, run_adapter(root, run_dir, agent))
        except KeyboardInterrupt:
            if run_dir:
                _record_interrupt(run_dir)
            raise
        except Exception as exc:
            if run_dir and load_state(run_dir)["state"] not in {"failed", "promoted"}:
                fail(run_dir, "orchestration", str(exc))
            raise


def continue_run(root: Path, identifier: str) -> dict[str, Any]:
    generated = ensure_generated_root(root)
    run_dir = resolve_run(generated, identifier)
    with OperationLock(generated, "continue", f"{run_dir.parents[1].name}/{run_dir.name}"):
        state = load_state(run_dir)
        failure = state.get("failure") or {}
        latest = state.get("latest_checkpoint")
        recoverable_checkpoint_failure = bool(
            state["state"] == "failed"
            and failure.get("stage") == "orchestration"
            and latest
            and not state.get("artifact")
            and (run_dir / "checkpoints" / str(latest) / "model.blend").is_file()
        )
        if state["state"] not in {"awaiting_feedback", "interrupted"} and not recoverable_checkpoint_failure:
            raise StateError(f"Run must be awaiting_feedback or interrupted, not {state['state']}")
        if recoverable_checkpoint_failure:
            state["failure"] = None
            save_state(run_dir, state)
            transition(run_dir, "awaiting_feedback", detail={"recovered_checkpoint": latest}, force=True)
            state = load_state(run_dir)
        if state["state"] == "interrupted" and (state.get("failure") or {}).get("stage") == "evaluation":
            raise StateError("Evaluation-interrupted runs must use resume, not continue")
        control = read_json(run_dir / "control.json", {})
        control.update({"pause_requested": False, "updated_at": utc_now()})
        atomic_write_json(run_dir / "control.json", control)
        metadata = read_json(run_dir / "run.json")
        name = metadata["agent_profile"]["name"]
        live_profile = load_agent_profile(root, name)
        agent = live_profile
        if live_profile.digest != metadata["agent_profile"]["sha256"]:
            snapshot_profile = run_dir / "snapshot" / "agent_profile.toml"
            agent = AgentProfile(name, snapshot_profile, load_toml(snapshot_profile), metadata["agent_profile"]["sha256"])
        transition(run_dir, "modeling")
        try:
            return handle_result(root, generated, run_dir, run_adapter(root, run_dir, agent))
        except KeyboardInterrupt:
            _record_interrupt(run_dir)
            raise
        except Exception as exc:
            if load_state(run_dir)["state"] not in {"failed", "promoted"}:
                fail(run_dir, "orchestration", str(exc))
            raise


def resume_evaluation(root: Path, identifier: str) -> dict[str, Any]:
    generated = ensure_generated_root(root)
    run_dir = resolve_run(generated, identifier)
    with OperationLock(generated, "resume", f"{run_dir.parents[1].name}/{run_dir.name}"):
        state = load_state(run_dir)
        if state["state"] not in {"failed", "interrupted"}:
            raise StateError(f"Only failed or interrupted evaluation can resume, not {state['state']}")
        failure = state.get("failure") or {}
        if failure.get("stage") not in {"evaluation", "validating", "final_rendering"}:
            raise StateError("Run did not fail during evaluation; refusing to rerun modeling")
        artifact_info = state.get("artifact")
        if not artifact_info:
            raise StateError("No owned artifact is available for evaluation resume")
        artifact = resolve_within(run_dir, artifact_info["path"])
        if sha256_file(artifact) != artifact_info["sha256"]:
            raise StateError("Owned artifact hash changed; refusing resume")
        try:
            publication = evaluate_and_promote(root, generated, run_dir, artifact)
        except KeyboardInterrupt:
            _record_interrupt(run_dir)
            raise
        return {"run": f"{state['task_id']}/{state['run_id']}", "state": "promoted", "publication": publication}
