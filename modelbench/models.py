from __future__ import annotations

"""Auditable, per-run model selection and invocation provenance."""

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import StateError, ValidationError
from .util import atomic_write_json, read_json, utc_now

DEFAULT_MODEL = "gpt-6-astra"
DEFAULT_EFFORT = "high"
ROLES = {"builder", "verifier"}
_THREAD_LOCK = threading.RLock()
_LOCAL = threading.local()


def _paths(run_dir: Path) -> tuple[Path, Path, Path]:
    return (run_dir / "model-settings.initial.json", run_dir / "model-settings.json", run_dir / ".model-settings.lock")


@contextmanager
def mutation_lock(run_dir: Path) -> Iterator[None]:
    """Take a re-entrant, process-safe advisory lock for model ledger mutations."""
    run_dir = Path(run_dir)
    _, _, lock_path = _paths(run_dir)
    key = str(lock_path.resolve())
    with _THREAD_LOCK:
        held = getattr(_LOCAL, "model_locks", {})
        entry = held.get(key)
        if entry is None:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+b")
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            held[key] = [handle, 0]
            _LOCAL.model_locks = held
            entry = held[key]
        entry[1] += 1
        try:
            yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                handle = entry[0]
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                del held[key]


def _allowed(profile: dict[str, Any]) -> dict[str, list[str]]:
    allowed = profile.get("allowed_models", profile.get("allowed", profile.get("models")))
    if not isinstance(allowed, dict) or not allowed:
        raise ValidationError("Model profile requires a nonempty allowed model-to-efforts mapping")
    normalized: dict[str, list[str]] = {}
    for model, efforts in allowed.items():
        if not isinstance(model, str) or not model or not isinstance(efforts, list) or not efforts:
            raise ValidationError("Allowed models must map nonempty model names to nonempty effort lists")
        if not all(isinstance(effort, str) and effort for effort in efforts):
            raise ValidationError("Allowed model efforts must be nonempty strings")
        normalized[model] = list(efforts)
    return normalized


def _selection(profile: dict[str, Any], selection: dict[str, Any] | None = None) -> dict[str, str]:
    allowed = _allowed(profile)
    values = {"model": profile.get("model", DEFAULT_MODEL), "effort": profile.get("effort", DEFAULT_EFFORT)}
    if selection is not None:
        values.update(selection)
    model, effort = values.get("model"), values.get("effort")
    if not isinstance(model, str) or not model or not isinstance(effort, str) or not effort:
        raise ValidationError("A model selection requires explicit model and effort")
    if effort not in allowed.get(model, []):
        raise ValidationError(f"Model selection {model!r}/{effort!r} is not allowed by the project profile")
    return {"model": model, "effort": effort}


def _override(overrides: dict[str, Any], role: str) -> dict[str, Any] | None:
    value = overrides.get(role, overrides.get("both"))
    if value is None:
        model, effort = overrides.get(f"{role}_model"), overrides.get(f"{role}_effort")
        if model is not None or effort is not None:
            value = {key: candidate for key, candidate in {"model": model, "effort": effort}.items() if candidate is not None}
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError(f"Override for {role} must contain model and effort")
    return value


def _load_ledger(run_dir: Path) -> dict[str, Any] | None:
    value = read_json(_paths(run_dir)[1])
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValidationError("Invalid model settings ledger")
    return value


def initialize(run_dir: Path, builder_profile: dict[str, Any], verifier_profile: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Create a run's immutable initial settings and mutable provenance ledger."""
    run_dir = Path(run_dir)
    if not isinstance(overrides, dict):
        raise ValidationError("Model overrides must be a mapping")
    profiles = {"builder": builder_profile, "verifier": verifier_profile}
    selected = {role: _selection(profile, _override(overrides, role)) for role, profile in profiles.items()}
    allowed = {role: _allowed(profile) for role, profile in profiles.items()}
    with mutation_lock(run_dir):
        existing = _load_ledger(run_dir)
        if existing is not None:
            return existing
        initial_path, ledger_path, _ = _paths(run_dir)
        if initial_path.exists():
            raise StateError("Initial model settings already exist without a mutable ledger")
        now = utc_now()
        initial = {
            "version": 1, "created_at": now, "configuration_revision": 1,
            "profiles": {role: {"allowed_models": allowed[role], **selected[role]} for role in ROLES},
        }
        atomic_write_json(initial_path, initial)
        ledger = {**initial, "events": [{"kind": "initialized", "at": now, "configuration_revision": 1}], "invocations": []}
        atomic_write_json(ledger_path, ledger)
        return ledger


def _assert_mutable(run_dir: Path) -> None:
    state = read_json(Path(run_dir) / "state.json", {})
    if isinstance(state, dict) and state.get("state") in {"completed", "promoted"}:
        raise StateError("Model settings are immutable once a run is completed or promoted")


def _roles(role: str) -> tuple[str, ...]:
    if role == "both":
        return ("builder", "verifier")
    if role in ROLES:
        return (role,)
    raise ValidationError("role must be builder, verifier, or both")


def change(run_dir: Path, role: str, model: str, effort: str) -> dict[str, Any]:
    """Schedule an allowed model change for subsequent invocations only."""
    selected_roles = _roles(role)
    with mutation_lock(Path(run_dir)):
        _assert_mutable(Path(run_dir))
        ledger = _load_ledger(Path(run_dir))
        if ledger is None:
            raise StateError("Model settings have not been initialized")
        for item in selected_roles:
            _selection(ledger["profiles"][item], {"model": model, "effort": effort})
        revision = int(ledger["configuration_revision"]) + 1
        for item in selected_roles:
            ledger["profiles"][item].update({"model": model, "effort": effort})
        ledger["configuration_revision"] = revision
        ledger.setdefault("events", []).append({"kind": "changed", "at": utc_now(), "role": role, "model": model, "effort": effort, "configuration_revision": revision})
        atomic_write_json(_paths(Path(run_dir))[1], ledger)
        return ledger


def settings(run_dir: Path) -> dict[str, Any]:
    """Return the current ledger; legacy runs explicitly report unknown runtime data."""
    value = _load_ledger(Path(run_dir))
    return value if value is not None else {"version": 0, "legacy": True, "runtime_confirmed": "Not recorded", "usage": "Not recorded"}


def begin_invocation(run_dir: Path, role: str, cli_version: str | None = None) -> dict[str, Any]:
    """Freeze one role's requested configuration before starting an invocation."""
    selected_role, = _roles(role)
    with mutation_lock(Path(run_dir)):
        ledger = _load_ledger(Path(run_dir))
        if ledger is None:
            raise StateError("Model settings have not been initialized")
        profile = ledger["profiles"][selected_role]
        now = utc_now()
        invocation = {
            "id": len(ledger.setdefault("invocations", [])) + 1, "role": selected_role,
            "requested_model": profile["model"], "requested_effort": profile["effort"],
            "configuration_revision": ledger["configuration_revision"], "requested_at": now,
            "cli_version": cli_version or "Not recorded", "runtime_confirmed": "Not recorded",
            "usage": "Not recorded",
        }
        ledger["invocations"].append(invocation)
        atomic_write_json(_paths(Path(run_dir))[1], ledger)
        return invocation.copy()


def heartbeat_invocation(run_dir: Path, role: str, invocation_id: int, *, pid: int, last_output_at: str | None = None) -> dict[str, Any]:
    """Record that a live child is still supervised without mutating lifecycle state."""
    selected_role, = _roles(role)
    with mutation_lock(Path(run_dir)):
        ledger = _load_ledger(Path(run_dir))
        if ledger is None:
            raise StateError("Model settings have not been initialized")
        invocation = next((item for item in ledger.get("invocations", []) if item.get("id") == invocation_id and item.get("role") == selected_role), None)
        if invocation is None:
            raise ValidationError("Unknown invocation for role")
        if invocation.get("finished_at") is not None:
            raise StateError("Invocation is already finished")
        invocation["pid"] = int(pid)
        invocation["heartbeat_at"] = utc_now()
        if last_output_at is not None:
            invocation["last_output_at"] = last_output_at
        atomic_write_json(_paths(Path(run_dir))[1], ledger)
        return invocation.copy()


def finish_invocation(
    run_dir: Path, role: str, invocation_id: int, runtime_model: str | None = None,
    runtime_effort: str | None = None, usage: dict[str, Any] | None = None, *,
    status: str | None = None, error: str | None = None, cli_version: str | None = None,
) -> dict[str, Any]:
    """Record a runtime report; it cannot independently confirm the selected runtime."""
    selected_role, = _roles(role)
    if (runtime_model is None) != (runtime_effort is None):
        raise ValidationError("Runtime model and effort must be supplied together")
    with mutation_lock(Path(run_dir)):
        ledger = _load_ledger(Path(run_dir))
        if ledger is None:
            raise StateError("Model settings have not been initialized")
        invocation = next((item for item in ledger.get("invocations", []) if item.get("id") == invocation_id and item.get("role") == selected_role), None)
        if invocation is None:
            raise ValidationError("Unknown invocation for role")
        if invocation.get("finished_at") is not None:
            raise StateError("Invocation is already finished")
        if runtime_model is not None and (invocation["requested_model"] != runtime_model or invocation["requested_effort"] != runtime_effort):
            raise ValidationError("Runtime model or effort does not match the frozen invocation request")
        invocation["finished_at"] = utc_now()
        invocation["status"] = status or "completed"
        if runtime_model is not None:
            invocation["reported_model"] = runtime_model
            invocation["reported_effort"] = runtime_effort
            invocation["reported_at"] = invocation["finished_at"]
        if usage is not None:
            invocation["usage"] = usage
        if error is not None:
            invocation["error"] = error
        if cli_version is not None:
            invocation["cli_version"] = cli_version
        atomic_write_json(_paths(Path(run_dir))[1], ledger)
        return invocation.copy()


def confirm_invocation(
    run_dir: Path, role: str, invocation_id: int, observed_model: str, observed_effort: str,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Record a harness-observed runtime confirmation after matching the frozen request."""
    selected_role, = _roles(role)
    with mutation_lock(Path(run_dir)):
        ledger = _load_ledger(Path(run_dir))
        if ledger is None:
            raise StateError("Model settings have not been initialized")
        invocation = next((item for item in ledger.get("invocations", []) if item.get("id") == invocation_id and item.get("role") == selected_role), None)
        if invocation is None:
            raise ValidationError("Unknown invocation for role")
        if invocation["requested_model"] != observed_model or invocation["requested_effort"] != observed_effort:
            raise ValidationError("Observed runtime model or effort does not match the frozen invocation request")
        if invocation["runtime_confirmed"] != "Not recorded":
            raise StateError("Invocation runtime is already confirmed")
        invocation["runtime_confirmed"] = observed_at or utc_now()
        invocation["observed_model"] = observed_model
        invocation["observed_effort"] = observed_effort
        atomic_write_json(_paths(Path(run_dir))[1], ledger)
        return invocation.copy()
