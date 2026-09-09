from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .config import AgentProfile, expand_command
from .errors import ValidationError
from .feedback import pending_feedback
from .runs import verify_run_snapshot
from .state import load_state, save_state
from .util import append_jsonl, atomic_write_json, read_json, sha256_bytes, utc_now

RESULT_STATUSES = {"submitted", "checkpoint", "failed"}
BASE_ENV_KEYS = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "NO_COLOR",
    "NUMBER_OF_PROCESSORS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROGRAMDATA",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}


@dataclass(frozen=True)
class AgentResult:
    status: str
    blend_path: str | None
    phase: str
    reported_iterations: int | None
    feedback_requested: bool
    notes: str

    @classmethod
    def load(cls, path: Path) -> "AgentResult":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"Agent did not write valid result JSON at {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValidationError("Agent result must be an object")
        fields = {"status", "blend_path", "phase", "reported_iterations", "feedback_requested", "notes"}
        unknown = set(data) - fields
        if unknown:
            raise ValidationError(f"Unknown agent result fields: {', '.join(sorted(unknown))}")
        missing = fields - set(data)
        if missing:
            raise ValidationError(f"Missing agent result fields: {', '.join(sorted(missing))}")
        status = data.get("status")
        if status not in RESULT_STATUSES:
            raise ValidationError(f"Agent result status must be one of {sorted(RESULT_STATUSES)}")
        blend_path = data.get("blend_path")
        if status in {"submitted", "checkpoint"} and (not isinstance(blend_path, str) or not blend_path):
            raise ValidationError(f"Agent status {status} requires blend_path")
        if blend_path is not None and not isinstance(blend_path, str):
            raise ValidationError("blend_path must be a string or null")
        phase = data.get("phase", "unknown")
        if not isinstance(phase, str) or not phase or len(phase) > 128:
            raise ValidationError("Agent result phase must be 1-128 characters")
        iterations = data.get("reported_iterations")
        if iterations is not None and (isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 0):
            raise ValidationError("reported_iterations must be a non-negative integer or null")
        feedback_requested = data.get("feedback_requested", False)
        if not isinstance(feedback_requested, bool):
            raise ValidationError("feedback_requested must be boolean")
        notes = data.get("notes", "")
        if not isinstance(notes, str) or len(notes) > 4000:
            raise ValidationError("notes must be a string of at most 4000 characters")
        return cls(status, blend_path, phase, iterations, feedback_requested, notes)


def build_agent_prompt(run_dir: Path) -> str:
    state = load_state(run_dir)
    prompt = (run_dir / "workspace" / "task" / "prompt.md").read_text(encoding="utf-8")
    instructions_dir = run_dir / "workspace" / "task" / "instructions"
    instructions = []
    if instructions_dir.exists():
        for path in sorted(instructions_dir.rglob("*")):
            if path.is_file():
                instructions.append(f"\n## Instruction: {path.relative_to(instructions_dir).as_posix()}\n{path.read_text(encoding='utf-8')}")
    latest = state.get("latest_checkpoint")
    pending = pending_feedback(run_dir)
    context = {
        "run_id": state["run_id"],
        "task_id": state["task_id"],
        "workspace": str(run_dir / "workspace"),
        "task_snapshot": str(run_dir / "workspace" / "task"),
        "input_manifest": str(run_dir / "workspace" / "task" / "inputs" / "manifest.toml"),
        "evidence_registry": str(run_dir / "evidence" / "registry.jsonl"),
        "latest_checkpoint": str(run_dir / "checkpoints" / latest / "model.blend") if latest else None,
        "pending_feedback": pending,
        "result_file": str(run_dir / "workspace" / "agent_result.json"),
        "checkpoint_command": "modelbench-agent checkpoint --source <workspace .blend> --phase <label>",
        "evidence_command": "modelbench-agent evidence --file <workspace file> [metadata]",
    }
    return (
        "You are the modeling agent for a ModelingBench run. Work only inside the supplied workspace. "
        "Preserve +Z up, -Y front, task bounds, anchors, and the required MODEL collection. "
        "Use agent-facing commands for auditable evidence and checkpoints. Write exactly one structured result to the result path.\n\n"
        f"# Run context\n```json\n{json.dumps(context, indent=2)}\n```\n\n"
        f"# Task\n{prompt}\n" + "".join(instructions)
    )


def _capture(stream: TextIO, raw_path: Path, events_path: Path, stream_name: str, event_lock: threading.Lock) -> None:
    with raw_path.open("w", encoding="utf-8", newline="\n") as raw:
        for line in iter(stream.readline, ""):
            raw.write(line)
            raw.flush()
            stripped = line.strip()
            try:
                payload = json.loads(stripped)
                if not isinstance(payload, dict):
                    payload = {"value": payload}
            except json.JSONDecodeError:
                payload = {"text": stripped}
            with event_lock:
                append_jsonl(events_path, {"at": utc_now(), "stream": stream_name, "event": payload})


def _agent_environment(
    root: Path,
    run_dir: Path,
    result_path: Path,
    schema: Path,
    profile: AgentProfile,
    token: str,
) -> dict[str, str]:
    allowed = BASE_ENV_KEYS | {key.upper() for key in profile.data.get("pass_env", [])}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update({
        "MODELBENCH_ROOT": str(root),
        "MODELBENCH_RUN_DIR": str(run_dir),
        "MODELBENCH_WORKSPACE": str(run_dir / "workspace"),
        "MODELBENCH_RESULT_FILE": str(result_path),
        "MODELBENCH_RESULT_SCHEMA": str(schema),
        "MODELBENCH_AGENT_API": "1",
        "MODELBENCH_AGENT_TOKEN": token,
        "PYTHONPATH": str(root),
    })
    return env


def run_adapter(root: Path, run_dir: Path, profile: AgentProfile) -> AgentResult:
    verify_run_snapshot(run_dir)
    state = load_state(run_dir)
    turn = int(state.get("observed_turns", 0)) + 1
    if turn > profile.limit("max_turns", 8):
        raise ValidationError(f"Agent turn limit exceeded ({profile.limit('max_turns', 8)})")
    result_path = run_dir / "workspace" / "agent_result.json"
    result_path.unlink(missing_ok=True)
    schema = run_dir / "snapshot" / "schemas" / "agent_result.schema.json"
    values = {
        "python": os.sys.executable,
        "root": str(root),
        "workspace": str(run_dir / "workspace"),
        "task_snapshot": str(run_dir / "snapshot" / "task"),
        "input_manifest": str(run_dir / "snapshot" / "task" / "inputs" / "manifest.toml"),
        "evidence_registry": str(run_dir / "evidence" / "registry.jsonl"),
        "latest_checkpoint": str(run_dir / "checkpoints" / state["latest_checkpoint"] / "model.blend") if state.get("latest_checkpoint") else "",
        "feedback": str(run_dir / "feedback" / "events.jsonl"),
        "result_schema": str(schema),
        "result_file": str(result_path),
        "run_dir": str(run_dir),
    }
    command = expand_command(profile.command, values)
    prompt = build_agent_prompt(run_dir)
    token = secrets.token_urlsafe(32)
    capability_path = run_dir / ".agent-api.json"
    atomic_write_json(capability_path, {
        "version": 1,
        "run_id": state["run_id"],
        "turn": turn,
        "token_sha256": sha256_bytes(token.encode("utf-8")),
        "created_at": utc_now(),
    })
    env = _agent_environment(root, run_dir, result_path, schema, profile, token)
    invocation = {"turn": turn, "command": command, "cwd": str(run_dir / "workspace"), "started_at": utc_now()}
    atomic_write_json(run_dir / "logs" / f"agent_turn_{turn:04d}.invocation.json", invocation)
    try:
        process = subprocess.Popen(
            command,
            cwd=run_dir / "workspace",
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
    except BaseException:
        capability_path.unlink(missing_ok=True)
        raise
    assert process.stdin and process.stdout and process.stderr
    events = run_dir / "logs" / "agent_events.jsonl"
    event_lock = threading.Lock()
    out_thread = threading.Thread(target=_capture, args=(process.stdout, run_dir / "logs" / f"agent_turn_{turn:04d}.stdout.log", events, "stdout", event_lock))
    err_thread = threading.Thread(target=_capture, args=(process.stderr, run_dir / "logs" / f"agent_turn_{turn:04d}.stderr.log", events, "stderr", event_lock))
    out_thread.start()
    err_thread.start()

    def feed_prompt() -> None:
        try:
            process.stdin.write(prompt)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            process.stdin.close()

    input_thread = threading.Thread(target=feed_prompt)
    input_thread.start()
    try:
        code = process.wait(timeout=profile.limit("wall_clock_seconds", 14400))
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    finally:
        input_thread.join(timeout=10)
        out_thread.join(timeout=10)
        err_thread.join(timeout=10)
        process.stdout.close()
        process.stderr.close()
        capability_path.unlink(missing_ok=True)
    state = load_state(run_dir)
    state["observed_turns"] = turn
    save_state(run_dir, state)
    if code != 0:
        raise ValidationError(f"Agent command exited with code {code}; see run logs")
    result = AgentResult.load(result_path)
    append_jsonl(events, {"at": utc_now(), "stream": "harness", "event": {"type": "agent_result", **result.__dict__}})
    return result
