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
from .budgets import BudgetExhausted, require_remaining
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
    # Harness-owned provenance; never accepted from agent-authored result JSON.
    invocation: dict[str, Any] | None = None

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
    from .discovery import enabled as discovery_enabled, active_spec, active_record, PROMPT
    discovered = discovery_enabled(run_dir)
    if discovered:
        prompt += '\n# Audited discovered specification\n' + json.dumps(active_spec(run_dir), indent=2)
        prompt += '\n' + PROMPT + '\nThe discovery stage is complete. Model against this approved specification. Request an amendment with modelbench-agent propose-spec --file <spec.json> --reason <reason>; only an independent audit can approve changes. Do not wait for audit before returning a checkpoint/submission.\n'
    latest = state.get("latest_checkpoint")
    pending = pending_feedback(run_dir)
    metadata = read_json(run_dir / "run.json", {})
    blender_executable = (
        metadata.get("render_profiles", {}).get("final", {}).get("data", {}).get("blender_executable")
        if isinstance(metadata, dict)
        else None
    )
    render_profile = (
        metadata.get("render_profiles", {}).get("final", {}).get("data", {})
        if isinstance(metadata, dict)
        else {}
    )
    base_color = render_profile.get("base_color", [0.18, 0.18, 0.18, 1.0])
    roughness_default = render_profile.get("roughness_default", 0.36)
    roughness_min = render_profile.get("roughness_min", 0.2)
    roughness_max = render_profile.get("roughness_max", 0.8)
    glass_roughness = render_profile.get("glass_roughness", 0.1)
    material_instruction = (
        "# Benchmark render material\n"
        f"Use neutral gray Base Color RGB ({base_color[0]:g}, {base_color[1]:g}, {base_color[2]:g}) "
        f"for all rendered surfaces. Use Roughness {roughness_default:g} by default and keep most surfaces "
        f"at that value. Roughness may vary from {roughness_min:g} to {roughness_max:g} only when needed "
        f"to communicate material identity; rubber may sit near the upper end. Glass may use Roughness "
        f"{glass_roughness:g}. For an intentional Roughness 0.5, set the material custom property "
        "modelbench_roughness_override to true so it is distinguishable from Blender's untouched default. "
        "Do not vary Base Color to distinguish materials. Set these values in the saved model active surface shader; "
        "review-render overrides are not evidence of source-material compliance. Follow any stricter task material policy.\n\n"
    )
    review_state = read_json(run_dir / 'review.json', {})
    acceptance = read_json(run_dir / 'acceptance.json', {})
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
        "blender_executable": blender_executable,
        "checkpoint_command": f'"{os.sys.executable}" -m modelbench.agent_cli checkpoint --source <workspace .blend> --phase <label>',
        "evidence_command": f'"{os.sys.executable}" -m modelbench.agent_cli evidence --file <workspace file> [metadata]',
        "design_manifest": acceptance.get('design_manifest'),
        "specification_sha256": active_record(run_dir)["specification"]["sha256"] if discovered else None,
        "design_exploration": review_state.get('exploration'),
    }
    return (
        "You are the modeling agent for a ModelingBench run. Work only inside the supplied workspace. "
        "Preserve +Z up, -Y front, task bounds, anchors, and the required MODEL collection. "
        "Use agent-facing commands for auditable evidence and checkpoints. Write exactly one structured result to the result path.\n\n"
        f"{material_instruction}"
        f"# Run context\n```json\n{json.dumps(context, indent=2)}\n```\n\n"
        f"# Task\n{prompt}\n" + "".join(instructions) +
        ('\n\n# Assembly-aware exploration\nThe manifest is a functional obligation, not a prescribed shape. Carry every stable requirement ID into plans, evidence, and revisions. '
         'When exploration is enabled, make the requested concept structurally distinct from prior concepts during divergence; refine only the selected concept during refinement. '
         'Object names and assertions are traceability aids, never proof. Inspect actual solids, transforms, axes, fits, engagement, retention, patterns, sections, and swept volumes. '
         'Record unavailable measurements as unknown. Target explicit failed/weak records and retain before/after evidence.')
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
    metadata = read_json(run_dir / "run.json", {})
    blender_executable = (
        metadata.get("render_profiles", {}).get("final", {}).get("data", {}).get("blender_executable")
        if isinstance(metadata, dict)
        else None
    )
    if isinstance(blender_executable, str) and blender_executable:
        env["MODELBENCH_BLENDER"] = blender_executable
    return env


def terminate_process_tree(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=30, check=False)
    else:
        import signal
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def cli_version(profile):
    try:
        index = profile.command.index('exec')
        result = subprocess.run(profile.command[:index] + ['--version'], capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else 'Not recorded'
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return 'Not recorded'


def run_adapter(root: Path, run_dir: Path, profile: AgentProfile, *, budget_deadline: float | None = None) -> AgentResult:
    verify_run_snapshot(run_dir)
    if budget_deadline is not None:
        require_remaining(budget_deadline)
    state = load_state(run_dir)
    turn = int(state.get("observed_turns", 0)) + 1
    if "max_turns" in profile.data.get("limits", {}) and turn > profile.limit("max_turns", 8):
        raise ValidationError(f"Agent turn limit exceeded ({profile.limit('max_turns', 8)})")
    from .models import begin_invocation, finish_invocation, heartbeat_invocation
    frozen = begin_invocation(run_dir, 'builder', cli_version=cli_version(profile)) if (run_dir / 'model-settings.json').exists() else None
    state['observed_turns'] = turn
    save_state(run_dir, state)
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
    if frozen:
        values.update(model=frozen['requested_model'], effort=frozen['requested_effort'])
    command = expand_command(profile.command, values)
    prompt = build_agent_prompt(run_dir)
    if (run_dir / 'review.json').exists():
        from .review import load
        review_state = load(run_dir)
        tasks = [t for t in review_state['tasks'] if t['status'] != 'verifier_resolved']
        prompt += '\n# Corrections and independent evidence\n' + json.dumps({'tasks': tasks, 'revisions': [{k:r[k] for k in ('id','parent','artifact','sha256')} for r in review_state['revisions']], 'evidence_directory': str(run_dir / 'revisions'), 'seed': str(run_dir / 'workspace/seed.blend')}, indent=2)
        prompt += ('\nFor EVERY correction, use modelbench-agent respond --task <id> --message <specific change and evidence>. '
                   'Responding never resolves a task. Use modelbench-agent self-check --source <workspace .blend> for the independent checks. '
                   'Preserve matching diagnostic views. Correct the whole asset and submit; no human approval is required. '
                   'After checkpoint creation, continue to a submitted result unless a pause was requested. '
                   'The independent harness verifier starts only AFTER you return status submitted. '
                   'Do not wait for a harness verifier result before submission. Use your own visual inspection and self-checks; '
                   'do not launch a separate verifier or wait for private verifier sign-off. The orchestrator owns that role and its configured model/provenance. '
                   'Your self-check is advisory; publication remains gated by the orchestrator.')
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
    invocation = {"role": "builder", "settings": frozen or "Not recorded", "turn": turn, "command": command, "cwd": str(run_dir / "workspace"), "started_at": utc_now()}
    atomic_write_json(run_dir / "logs" / f"agent_turn_{turn:04d}.invocation.json", invocation)
    try:
        if budget_deadline is not None:
            require_remaining(budget_deadline)
        process = subprocess.Popen(
            command,
            cwd=run_dir / "workspace",
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
    except BaseException:
        capability_path.unlink(missing_ok=True)
        if frozen:
            finish_invocation(run_dir, 'builder', frozen['id'], status='failed', error='Process creation failed')
        raise
    assert process.stdin and process.stdout and process.stderr
    events = run_dir / "logs" / "agent_events.jsonl"
    event_lock = threading.Lock()
    out_thread = threading.Thread(target=_capture, args=(process.stdout, run_dir / "logs" / f"agent_turn_{turn:04d}.stdout.log", events, "stdout", event_lock))
    err_thread = threading.Thread(target=_capture, args=(process.stderr, run_dir / "logs" / f"agent_turn_{turn:04d}.stderr.log", events, "stderr", event_lock))
    out_thread.start()
    err_thread.start()
    last_event_size = events.stat().st_size if events.exists() else 0
    last_output_at = invocation["started_at"]
    if frozen:
        heartbeat_invocation(run_dir, 'builder', frozen['id'], pid=process.pid, last_output_at=last_output_at)

    def feed_prompt() -> None:
        try:
            process.stdin.write(prompt)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try: process.stdin.close()
            except OSError: pass

    input_thread = threading.Thread(target=feed_prompt)
    input_thread.start()
    try:
        import time
        deadline = time.monotonic() + profile.limit('wall_clock_seconds', 14400)
        if budget_deadline is not None:
            deadline = min(deadline, budget_deadline)
        while True:
            if read_json(run_dir / 'control.json', {}).get('cancel_requested'):
                raise KeyboardInterrupt('Run cancelled')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if budget_deadline is not None and deadline == budget_deadline:
                    raise BudgetExhausted('User budget exhausted')
                raise subprocess.TimeoutExpired(command, profile.limit('wall_clock_seconds', 14400))
            try:
                code = process.wait(timeout=min(1.0, remaining))
                break
            except subprocess.TimeoutExpired:
                if frozen:
                    event_size = events.stat().st_size if events.exists() else 0
                    if event_size != last_event_size:
                        last_event_size = event_size
                        last_output_at = utc_now()
                    heartbeat_invocation(run_dir, 'builder', frozen['id'], pid=process.pid, last_output_at=last_output_at)
    except (subprocess.TimeoutExpired, KeyboardInterrupt, BudgetExhausted) as exc:
        terminate_process_tree(process)
        raise
    finally:
        input_thread.join(timeout=10)
        out_thread.join(timeout=10)
        err_thread.join(timeout=10)
        process.stdout.close()
        process.stderr.close()
        capability_path.unlink(missing_ok=True)
        if frozen:
            usage = None
            from .util import read_jsonl
            for record in read_jsonl(events):
                payload = record.get('event', {})
                if 'usage' in payload:
                    usage = payload['usage']
            finish_invocation(run_dir, 'builder', frozen['id'], usage=usage,
                              status='finished' if process.returncode == 0 else 'failed')
    state = load_state(run_dir)
    state["observed_turns"] = turn
    save_state(run_dir, state)
    if code != 0:
        raise ValidationError(f"Agent command exited with code {code}; see run logs")
    result = AgentResult.load(result_path)
    append_jsonl(events, {"at": utc_now(), "stream": "harness", "event": {"type": "agent_result", **result.__dict__}})
    return AgentResult(result.status, result.blend_path, result.phase, result.reported_iterations, result.feedback_requested, result.notes, frozen)
