from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from .config import AgentProfile, RenderProfile, TaskDefinition
from .errors import StateError, ValidationError
from .project import git_identity
from .state import initial_state, load_state
from .util import atomic_write_json, file_inventory, is_link, read_json, sha256_file, utc_now

RUN_RE = re.compile(r"^run_(\d{6})$")


def run_dirs(generated: Path) -> list[Path]:
    return sorted(p for p in generated.glob("*/runs/run_*") if p.is_dir() and RUN_RE.match(p.name))


def resolve_run(generated: Path, identifier: str) -> Path:
    normalized = identifier.replace("\\", "/")
    if "/" in normalized:
        task_id, run_id = normalized.split("/", 1)
        candidate = (generated / task_id / "runs" / run_id).resolve()
        try:
            candidate.relative_to(generated.resolve())
        except ValueError as exc:
            raise ValidationError("Unsafe run identifier") from exc
        if candidate.is_dir() and RUN_RE.match(candidate.name):
            return candidate
        raise ValidationError(f"Run not found: {identifier}")
    matches = [p for p in run_dirs(generated) if p.name == identifier]
    if not matches:
        raise ValidationError(f"Run not found: {identifier}")
    if len(matches) > 1:
        options = ", ".join(f"{p.parents[1].name}/{p.name}" for p in matches)
        raise ValidationError(f"Ambiguous run ID {identifier}; use one of: {options}")
    return matches[0]


def create_run(
    root: Path,
    generated: Path,
    task: TaskDefinition,
    agent: AgentProfile,
    checkpoint_profile: RenderProfile,
    final_profile: RenderProfile,
) -> Path:
    schemas_root = root / "schemas"
    driver = root / "modelbench" / "blender_driver.py"
    if not schemas_root.is_dir() or is_link(schemas_root):
        raise ValidationError("schemas/ must be a real directory")
    schema_links = [path for path in schemas_root.rglob("*") if is_link(path)]
    if schema_links:
        raise ValidationError(f"Schema tree may not contain links or junctions: {schema_links[0]}")
    if not driver.is_file() or is_link(driver):
        raise ValidationError("Blender evaluator driver is missing or linked")
    schemas_inventory = file_inventory(schemas_root)
    driver_sha256 = sha256_file(driver)
    task_root = generated / task.id
    runs_root = task_root / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    numbers = [int(match.group(1)) for p in runs_root.iterdir() if p.is_dir() and (match := RUN_RE.match(p.name))]
    run_id = f"run_{(max(numbers, default=0) + 1):06d}"
    run_dir = runs_root / run_id
    run_dir.mkdir()
    for name in ("workspace", "evidence/files", "checkpoints", "feedback", "artifacts", "renders", "logs", "snapshot"):
        (run_dir / name).mkdir(parents=True, exist_ok=True)
    atomic_write_json(run_dir / "state.json", initial_state(run_id, task.id))
    atomic_write_json(run_dir / "control.json", {"pause_requested": False, "updated_at": utc_now()})
    metadata = {
        "version": 1,
        "run_id": run_id,
        "task_id": task.id,
        "created_at": utc_now(),
        "project_git": git_identity(root),
        "task": {"digest": task.digest, "files": list(task.inventory)},
        "task_config": task.config,
        "input_manifest": task.manifest,
        "schemas": {"files": schemas_inventory},
        "blender_driver": {"sha256": driver_sha256},
        "agent_profile": {"name": agent.name, "path": agent.path.relative_to(root).as_posix(), "sha256": agent.digest, "data": agent.data},
        "render_profiles": {
            "checkpoint": {"name": checkpoint_profile.name, "sha256": checkpoint_profile.digest, "data": checkpoint_profile.data},
            "final": {"name": final_profile.name, "sha256": final_profile.digest, "data": final_profile.data},
        },
    }
    atomic_write_json(run_dir / "run.json", metadata)
    atomic_write_json(run_dir / "snapshot" / "task.json", task.config)
    shutil.copytree(task.directory, run_dir / "snapshot" / "task", dirs_exist_ok=False)
    shutil.copy2(agent.path, run_dir / "snapshot" / "agent_profile.toml")
    shutil.copy2(checkpoint_profile.path, run_dir / "snapshot" / "checkpoint_profile.toml")
    shutil.copy2(final_profile.path, run_dir / "snapshot" / "final_profile.toml")
    shutil.copytree(schemas_root, run_dir / "snapshot" / "schemas", dirs_exist_ok=False)
    shutil.copy2(driver, run_dir / "snapshot" / "blender_driver.py")
    if file_inventory(run_dir / "snapshot" / "task") != list(task.inventory):
        raise StateError("Task snapshot does not match its source inventory")
    if file_inventory(run_dir / "snapshot" / "schemas") != schemas_inventory:
        raise StateError("Schema snapshot does not match its source inventory")
    copied_hashes = {
        "agent": sha256_file(run_dir / "snapshot" / "agent_profile.toml"),
        "checkpoint": sha256_file(run_dir / "snapshot" / "checkpoint_profile.toml"),
        "final": sha256_file(run_dir / "snapshot" / "final_profile.toml"),
        "driver": sha256_file(run_dir / "snapshot" / "blender_driver.py"),
    }
    expected_hashes = {
        "agent": agent.digest,
        "checkpoint": checkpoint_profile.digest,
        "final": final_profile.digest,
        "driver": driver_sha256,
    }
    if copied_hashes != expected_hashes:
        raise StateError("Run snapshot hash verification failed")
    shutil.copytree(task.directory, run_dir / "workspace" / "task", dirs_exist_ok=False)
    for task_input in task.inputs:
        if task_input.use == "editable_seed":
            destination = run_dir / "workspace" / "seeds" / task_input.id / task_input.path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(task_input.path, destination)
    measurement_driver = root / 'modelbench' / 'measure_driver.py'
    if measurement_driver.is_file():
        shutil.copy2(measurement_driver, run_dir / 'snapshot' / 'measure_driver.py')
        metadata['measurement_driver'] = {'sha256': sha256_file(measurement_driver)}
        atomic_write_json(run_dir / 'run.json', metadata)
    helper = root / 'modelbench/geometry_checks.py'
    if helper.is_file():
        shutil.copy2(helper, run_dir / 'snapshot/geometry_checks.py')
        metadata['geometry_checks'] = {'sha256': sha256_file(helper)}
    reference_driver = root / 'modelbench/reference_driver.py'
    if reference_driver.is_file():
        shutil.copy2(reference_driver, run_dir / 'snapshot/reference_driver.py')
        metadata['reference_driver'] = {'sha256': sha256_file(reference_driver)}
    if task.config.get('specification_mode') == 'discovered':
        metadata['workflow_version'] = 2
        from .reconstruction.core import _implementation_hash
        metadata['reconstruction_implementation_sha256'] = _implementation_hash()
        metadata['reference_evaluator_sha256'] = sha256_file(Path(__file__).with_name('reference.py'))
        metadata['runtime'] = {'python': __import__('sys').version, 'platform': __import__('platform').platform(),
            'context_policy': 'Owned brief, registered sources, explicit profile tools; no implicit asset context'}
    atomic_write_json(run_dir / 'run.json', metadata)
    return run_dir


def update_run_metadata(run_dir: Path, **values: Any) -> None:
    metadata = read_json(run_dir / "run.json", {})
    metadata.update(values)
    atomic_write_json(run_dir / "run.json", metadata)


def verify_run_snapshot(run_dir: Path) -> None:
    metadata = read_json(run_dir / "run.json")
    if not isinstance(metadata, dict):
        raise StateError("Run metadata is missing or invalid")
    snapshot = run_dir / "snapshot"
    if file_inventory(snapshot / "task") != metadata.get("task", {}).get("files"):
        raise StateError("Task snapshot integrity check failed")
    if file_inventory(snapshot / "schemas") != metadata.get("schemas", {}).get("files"):
        raise StateError("Schema snapshot integrity check failed")
    checks = {
        "agent profile": (
            snapshot / "agent_profile.toml",
            metadata.get("agent_profile", {}).get("sha256"),
        ),
        "checkpoint render profile": (
            snapshot / "checkpoint_profile.toml",
            metadata.get("render_profiles", {}).get("checkpoint", {}).get("sha256"),
        ),
        "final render profile": (
            snapshot / "final_profile.toml",
            metadata.get("render_profiles", {}).get("final", {}).get("sha256"),
        ),
        "Blender evaluator": (
            snapshot / "blender_driver.py",
            metadata.get("blender_driver", {}).get("sha256"),
        ),
    }
    if metadata.get('measurement_driver'):
        checks['measurement evaluator'] = (snapshot / 'measure_driver.py', metadata['measurement_driver']['sha256'])
    if metadata.get('geometry_checks'):
        checks['geometry checks'] = (snapshot / 'geometry_checks.py', metadata['geometry_checks']['sha256'])
    if metadata.get('reference_driver'):
        checks['reference driver'] = (snapshot / 'reference_driver.py', metadata['reference_driver']['sha256'])
    for label, (path, expected) in checks.items():
        if not path.is_file() or is_link(path) or not isinstance(expected, str) or sha256_file(path) != expected:
            raise StateError(f"{label.capitalize()} snapshot integrity check failed")
    if read_json(snapshot / "task.json") != metadata.get("task_config"):
        raise StateError("Task configuration snapshot integrity check failed")


def next_artifact_path(run_dir: Path) -> Path:
    current = read_json(run_dir.parents[1] / "current.json", {})
    historical = current.get("artifact", {}).get("historical_path", "")
    match = re.search(r"model_v(\d{6})\.blend$", historical)
    version = int(match.group(1)) + 1 if match else 1
    existing = [int(p.stem.split('v')[-1]) for p in (run_dir / 'artifacts').glob('model_v*.blend')]
    version = max(version, max(existing, default=0) + 1)
    return run_dir / "artifacts" / f"model_v{version:06d}.blend"


def promote(generated, run_dir, artifact, *, expected_artifact_sha256, budget_deadline: float | None = None):
    from .review import lock
    from .budgets import require_remaining
    if budget_deadline is not None: require_remaining(budget_deadline)
    with lock(run_dir):
        return _promote_locked(generated, run_dir, artifact, expected_artifact_sha256=expected_artifact_sha256, budget_deadline=budget_deadline)


def _promote_locked(
    generated: Path,
    run_dir: Path,
    artifact: Path,
    *,
    expected_artifact_sha256: str,
    budget_deadline: float | None = None,
) -> dict[str, Any]:
    from .review import assert_publishable
    from .budgets import require_remaining
    assert_publishable(run_dir, expected_artifact_sha256)
    from .review import current as current_revision
    from .automation import validate_render_set, _canonical_render_result
    revision = current_revision(run_dir)
    registered_final = run_dir / 'revisions' / revision['id'] / 'final'
    expected_renders = validate_render_set(run_dir, registered_final, read_json(registered_final / 'blender_result.json'), 'final', expected_artifact_sha256)
    mirror = run_dir / 'renders/final'
    mirror_result = _canonical_render_result(run_dir, mirror, read_json(mirror / 'blender_result.json'), 'final')
    if validate_render_set(run_dir, mirror, mirror_result, 'final', expected_artifact_sha256) != expected_renders:
        raise StateError('Publication renders differ from registered final evidence')
    task_id = load_state(run_dir)["task_id"]
    task_root = generated / task_id
    current = task_root / "current"
    if artifact.is_symlink() or sha256_file(artifact) != expected_artifact_sha256:
        raise StateError("Owned artifact hash changed before publication")
    if current.is_symlink():
        raise StateError("Published current path may not be a symbolic link")
    existing = read_json(current / "publication.json") if current.is_dir() else None
    if (
        isinstance(existing, dict)
        and existing.get("run_id") == run_dir.name
        and existing.get("artifact", {}).get("sha256") == expected_artifact_sha256
    ):
        published_model = current / "model.blend"
        if not published_model.is_file() or sha256_file(published_model) != expected_artifact_sha256:
            raise StateError("Existing publication model does not match its evaluation")
        intact_renders = all((current / 'renders' / relative).is_file() and sha256_file(current / 'renders' / relative) == digest for relative, digest in expected_renders.items())
        if intact_renders:
            atomic_write_json(task_root / "current.json", existing)
            for stale in task_root.glob(".current-old-*"):
                if stale.is_dir() and not stale.is_symlink():
                    shutil.rmtree(stale)
            return existing

    nonce = uuid.uuid4().hex
    staging = task_root / f".current-{run_dir.name}-{nonce}"
    staging.mkdir(parents=True)
    old = task_root / f".current-old-{nonce}"
    previous_pointer = read_json(task_root / "current.json")
    try:
        published_model = staging / "model.blend"
        shutil.copy2(artifact, published_model)
        if sha256_file(published_model) != expected_artifact_sha256:
            raise StateError("Published model copy does not match the evaluated artifact")
        renders = run_dir / "renders" / "final"
        if not renders.is_dir() or renders.is_symlink():
            raise StateError("Final renders are missing or linked; refusing promotion")
        shutil.copytree(renders, staging / "renders")
        render_inventory = file_inventory(
            staging / "renders",
            exclude={"invocation.json", "blender_result.json"},
        )
        from .review import package
        package(run_dir, staging / 'review')
        publication = {
            "version": 1,
            "task_id": task_id,
            "run_id": run_dir.name,
            "artifact": {
                "historical_path": artifact.relative_to(generated).as_posix(),
                "sha256": expected_artifact_sha256,
                "size": published_model.stat().st_size,
            },
            "renders": render_inventory,
            "promoted_at": utc_now(),
        }
        atomic_write_json(staging / "publication.json", publication)
        atomic_write_json(staging / 'review/publication.json', publication)
        atomic_write_json(staging / 'review/manifest.json', {'version': 1, 'artifact_sha256': expected_artifact_sha256, 'files': file_inventory(staging / 'review', exclude={'manifest.json'})})
        if budget_deadline is not None: require_remaining(budget_deadline)
        if current.exists():
            os.replace(current, old)
        try:
            if budget_deadline is not None: require_remaining(budget_deadline)
            os.replace(staging, current)
            atomic_write_json(task_root / "current.json", publication)
        except BaseException:
            if current.exists() and not current.is_symlink():
                shutil.rmtree(current)
            if old.exists():
                os.replace(old, current)
            if previous_pointer is None:
                (task_root / "current.json").unlink(missing_ok=True)
            else:
                atomic_write_json(task_root / "current.json", previous_pointer)
            raise
    finally:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
    if old.exists():
        try:
            shutil.rmtree(old)
        except OSError:
            pass
    return publication


def status_records(generated: Path, identifier: str | None = None) -> list[dict[str, Any]]:
    paths = [resolve_run(generated, identifier)] if identifier else run_dirs(generated)
    records = []
    for path in paths:
        state = load_state(path)
        model_settings = __import__('modelbench.models', fromlist=['settings']).settings(path)
        invocation_activity = [
            item.get("heartbeat_at") or item.get("finished_at") or item.get("requested_at")
            for item in model_settings.get("invocations", [])
            if isinstance(item, dict)
        ]
        activity_at = max([value for value in [state.get("updated_at"), *invocation_activity] if isinstance(value, str)], default=None)
        records.append({
            "run": f"{state['task_id']}/{state['run_id']}",
            "state": state["state"],
            "turns": state.get("observed_turns", 0),
            "checkpoints": state.get("observed_checkpoints", 0),
            "updated_at": state.get("updated_at"),
            "activity_at": activity_at,
            "artifact": state.get("artifact"),
            "failure": state.get("failure"),
            "models": model_settings,
            "review": read_json(path / 'review.json', {'provenance': 'Not recorded'}),
        })
    return records
