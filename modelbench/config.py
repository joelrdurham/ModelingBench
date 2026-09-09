from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .util import canonical_hash, ensure_id, file_inventory, is_link, resolve_within, sha256_file

INPUT_ROLES = {
    "reference_image",
    "drawing",
    "dimension_document",
    "starter_blend",
    "greybox",
    "constraint",
    "other",
}
INPUT_USES = {"reference", "editable_seed", "guide", "constraint"}


def load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValidationError(f"Could not load TOML {path}: {exc}") from exc


def _vec3(value: Any, name: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3 or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in value):
        raise ValidationError(f"{name} must be an array of three finite numbers")
    result = tuple(float(x) for x in value)
    if any(not __import__("math").isfinite(x) for x in result):
        raise ValidationError(f"{name} must contain only finite numbers")
    return result  # type: ignore[return-value]


@dataclass(frozen=True)
class TaskInput:
    id: str
    role: str
    use: str
    relative_path: str
    path: Path
    sha256: str
    media_type: str | None = None
    title: str | None = None
    source_url: str | None = None
    view_classification: str = "unknown"
    known_dimension: dict[str, Any] | None = None
    notes: str | None = None
    confidence: float | None = None

    def record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "use": self.use,
            "path": self.relative_path,
            "sha256": self.sha256,
            "size": self.path.stat().st_size,
            "media_type": self.media_type,
            "title": self.title,
            "source_url": self.source_url,
            "view_classification": self.view_classification,
            "known_dimension": self.known_dimension,
            "notes": self.notes,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class TaskDefinition:
    id: str
    directory: Path
    config: dict[str, Any]
    manifest: dict[str, Any]
    inputs: tuple[TaskInput, ...]
    inventory: tuple[dict[str, Any], ...]
    digest: str

    @property
    def frame(self) -> dict[str, Any]:
        return self.config["frame"]

    @property
    def research_enabled(self) -> bool:
        research = self.config.get("research", {})
        return bool(research.get("enabled", False))


def load_task(root: Path, task_id: str) -> TaskDefinition:
    ensure_id(task_id, "task ID")
    raw_directory = root / "tasks" / task_id
    if not raw_directory.exists() and task_id.startswith("brief_"):
        raw_directory = root / "generated" / "briefs" / "tasks" / task_id
    if is_link(raw_directory):
        raise ValidationError("Task directory may not be a link or junction")
    directory = raw_directory.resolve()
    tasks_root = raw_directory.parent.resolve()
    try:
        directory.relative_to(tasks_root)
    except ValueError as exc:
        raise ValidationError("Unsafe task path") from exc
    task_file = directory / "task.toml"
    prompt_file = directory / "prompt.md"
    manifest_file = directory / "inputs" / "manifest.toml"
    for required in (task_file, prompt_file, manifest_file):
        if not required.is_file():
            raise ValidationError(f"Task {task_id} is missing {required.relative_to(directory)}")
        if is_link(required):
            raise ValidationError(f"Task files may not be symbolic links: {required}")
    linked = [path for path in directory.rglob("*") if is_link(path)]
    if linked:
        raise ValidationError(f"Task trees may not contain links or junctions: {linked[0]}")

    config = load_toml(task_file)
    if config.get("version") not in (1, 2):
        raise ValidationError("task.toml version must be 1 or 2")
    discovered = config.get('version') == 2 and config.get('specification_mode') == 'discovered'
    if config.get('version') == 2 and not discovered:
        raise ValidationError('v2 requires specification_mode=discovered')
    if discovered:
        from .discovery import default_frame
        config['frame'] = {**default_frame(), **config.get('frame', {})}
        config.setdefault('research', {'enabled': True, 'mode': 'live'})
    if config.get("id") != task_id:
        raise ValidationError(f"task.toml id must match directory name {task_id!r}")
    if not isinstance(config.get("title"), str) or not config["title"].strip():
        raise ValidationError("task.toml title is required")
    frame = config.get("frame")
    if not isinstance(frame, dict):
        raise ValidationError("task.toml requires [frame]")
    if isinstance(frame.get("unit_scale"), bool) or not isinstance(frame.get("unit_scale"), (int, float)) or frame["unit_scale"] <= 0:
        raise ValidationError("frame.unit_scale must be positive")
    if frame.get("up_axis") != "+Z" or frame.get("front_axis") != "-Y":
        raise ValidationError("v1 requires frame.up_axis='+Z' and frame.front_axis='-Y'")
    center = _vec3(frame.get("evaluation_center"), "frame.evaluation_center")
    lower = _vec3(frame.get("bounds_min"), "frame.bounds_min")
    upper = _vec3(frame.get("bounds_max"), "frame.bounds_max")
    if any(lo >= hi for lo, hi in zip(lower, upper)):
        raise ValidationError("Each frame.bounds_min component must be below bounds_max")
    if any(c < lo or c > hi for c, lo, hi in zip(center, lower, upper)):
        raise ValidationError("frame.evaluation_center must lie inside task bounds")
    ground = frame.get("ground_plane", False)
    if not isinstance(ground, bool):
        raise ValidationError("frame.ground_plane must be boolean")
    evaluation_frame = frame.get("evaluation_frame", 1)
    if isinstance(evaluation_frame, bool) or not isinstance(evaluation_frame, int) or evaluation_frame < 1:
        raise ValidationError("frame.evaluation_frame must be a positive integer")
    anchors = config.get("anchors", [])
    if not isinstance(anchors, list):
        raise ValidationError("[[anchors]] must be an array of tables")
    anchor_names: set[str] = set()
    for anchor in anchors:
        if not isinstance(anchor, dict) or not isinstance(anchor.get("name"), str):
            raise ValidationError("Every anchor needs a name")
        ensure_id(anchor["name"], "anchor name")
        if anchor["name"] in anchor_names:
            raise ValidationError(f"Duplicate anchor: {anchor['name']}")
        anchor_names.add(anchor["name"])
        _vec3(anchor.get("position"), f"anchor {anchor['name']} position")
        tolerance = anchor.get("tolerance")
        if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or tolerance < 0:
            raise ValidationError(f"anchor {anchor['name']} tolerance must be non-negative")
    dimensions = config.get("dimensions", [])
    if not isinstance(dimensions, list):
        raise ValidationError("[[dimensions]] must be an array of tables")
    dimension_names: set[str] = set()
    for dimension in dimensions:
        if not isinstance(dimension, dict):
            raise ValidationError("Every dimension must be a table")
        name = ensure_id(str(dimension.get("name", "")), "dimension name")
        if name in dimension_names:
            raise ValidationError(f"Duplicate dimension: {name}")
        dimension_names.add(name)
        for endpoint in ("from_anchor", "to_anchor"):
            if dimension.get(endpoint) not in anchor_names:
                raise ValidationError(f"Dimension {name} references unknown {endpoint}: {dimension.get(endpoint)!r}")
        value = dimension.get("value")
        tolerance = dimension.get("tolerance")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValidationError(f"Dimension {name} value must be positive")
        if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or tolerance < 0:
            raise ValidationError(f"Dimension {name} tolerance must be non-negative")
        if not isinstance(dimension.get("unit"), str) or not dimension["unit"]:
            raise ValidationError(f"Dimension {name} requires a unit")

    verification = config.get('verification', {})
    if not isinstance(verification, dict):
        raise ValidationError('verification must be a table')
    if 'frames' in verification:
        frames = verification['frames']
        if not isinstance(frames, list) or not frames or any(isinstance(f, bool) or not isinstance(f, int) or f < 1 for f in frames) or len(set(frames)) != len(frames):
            raise ValidationError('verification.frames must contain unique positive integer frames')
    materials = verification.get('materials')
    # Legacy v1 tasks may omit the later global material policy entirely.
    if materials is not None and (not isinstance(materials, dict) or materials.get('enabled') is not True):
        raise ValidationError('verification.materials must enable the global gray-shaded material policy when declared')
    if materials is not None and _vec3(materials.get('base_color_linear_rgb'), 'verification.materials.base_color_linear_rgb') != (0.18, 0.18, 0.18):
        raise ValidationError('verification.materials.base_color_linear_rgb must be [0.18, 0.18, 0.18]')
    for key, expected in (('base_color_tolerance', 0.001), ('default_roughness', 0.36), ('roughness_tolerance', 0.01)):
        if materials is not None and materials.get(key) != expected:
            raise ValidationError(f'verification.materials.{key} must be {expected}')
    ranges = materials.get('roughness_ranges') if materials is not None else None
    expected_ranges = {'default': [0.2, 0.8], 'rubber': [0.2, 0.8], 'glass': [0.1, 0.8]}
    if materials is not None and ranges != expected_ranges:
        raise ValidationError('verification.materials.roughness_ranges must define the global, rubber, and glass policy ranges')
    roles = verification.get('roles', {})
    if not isinstance(roles, dict) or any(not isinstance(v, str) or not v for v in roles.values()):
        raise ValidationError('verification.roles must map role names to geometry names')
    for check in verification.get('dimensions', []):
        if not isinstance(check, dict) or not check.get('id') or check.get('kind', 'distance') not in {'distance','angle_xz','panel_size','bounds','relative_transform','upper_branch','extent'}:
            raise ValidationError('Unknown or malformed verification dimension')
        if check.get('kind') == 'extent':
            axis, expected = check.get('axis'), check.get('expected')
            if isinstance(axis, bool) or axis not in (0, 1, 2):
                raise ValidationError('Extent axis must be 0, 1, or 2 in world coordinates')
            if isinstance(expected, bool) or not isinstance(expected, (int, float)) or not __import__('math').isfinite(expected) or expected <= 0:
                raise ValidationError('Extent expected value must be finite and positive')
        tolerance = check.get('tolerance', 0)
        if isinstance(tolerance, bool) or not isinstance(tolerance, (float, int)) or not __import__('math').isfinite(tolerance) or tolerance < 0:
            raise ValidationError('Verification tolerances must be finite and nonnegative')
        for key in ('from_role','to_role','role','relative_to_role','b_role','c_role','d_role'):
            if key in check and check[key] not in roles:
                raise ValidationError('Verification check references unknown role: ' + check[key])

    # Optional declarative mechanism manifest; tasks without one retain v1 behavior.
    assembly = config.get('assembly')
    if assembly is not None:
        if not isinstance(assembly, dict):
            raise ValidationError('assembly must be a table')
        components, interfaces, constraints = (assembly.get(key, []) for key in ('components', 'interfaces', 'constraints'))
        if not all(isinstance(items, list) for items in (components, interfaces, constraints)):
            raise ValidationError('assembly components, interfaces, and constraints must be arrays of tables')
        component_kinds: dict[str, str] = {}
        for component in components:
            if not isinstance(component, dict): raise ValidationError('Every assembly component must be a table')
            ident = ensure_id(str(component.get('id', '')), 'assembly component ID')
            if ident in component_kinds: raise ValidationError(f'Duplicate assembly component: {ident}')
            kind = component.get('kind')
            if kind not in {'link', 'environment'}: raise ValidationError(f'Assembly component {ident} kind must be link or environment')
            names = component.get('roles', [])
            if not isinstance(names, list) or not names or any(name not in roles for name in names): raise ValidationError(f'Assembly component {ident} must reference one or more verification roles')
            component_kinds[ident] = kind
        interface_ids: set[str] = set()
        for interface in interfaces:
            if not isinstance(interface, dict): raise ValidationError('Every assembly interface must be a table')
            ident = ensure_id(str(interface.get('id', '')), 'assembly interface ID')
            if ident in interface_ids: raise ValidationError(f'Duplicate assembly interface: {ident}')
            interface_ids.add(ident)
            if interface.get('role') not in roles: raise ValidationError(f'Assembly interface {ident} references an unknown role')
            owners = interface.get('components', [])
            if not isinstance(owners, list) or len(owners) != 2 or any(owner not in component_kinds for owner in owners): raise ValidationError(f'Assembly interface {ident} must join exactly two declared components')
        for constraint in constraints:
            if not isinstance(constraint, dict): raise ValidationError('Every assembly constraint must be a table')
            ensure_id(str(constraint.get('id', '')), 'assembly constraint ID')
            endpoints = constraint.get('interfaces', [])
            if constraint.get('kind') not in {'rigid', 'fixed'} or not isinstance(endpoints, list) or len(endpoints) != 2 or any(item not in interface_ids for item in endpoints): raise ValidationError('Assembly constraints must be rigid/fixed and reference exactly two interfaces')
            resolved_by = constraint.get('resolved_by')
            if resolved_by not in component_kinds: raise ValidationError('Assembly constraint resolved_by must be a declared component')
            if constraint['kind'] == 'fixed' and component_kinds[resolved_by] != 'environment': raise ValidationError('Fixed assembly constraints must be resolved by an environment component')
        ground = assembly.get('ground_resolution')
        if ground is not None:
            if not isinstance(ground, dict) or ground.get('environment_component') not in component_kinds or component_kinds.get(ground.get('environment_component')) != 'environment':
                raise ValidationError('assembly.ground_resolution requires a declared environment_component')
    features = config.get('features', {})
    if not isinstance(features, dict) or not isinstance(features.get('repeated', []), list): raise ValidationError('features.repeated must be an array when declared')
    for feature in features.get('repeated', []):
        if not isinstance(feature, dict) or feature.get('kind') != 'radial_instances': raise ValidationError('Repeated feature kind must be radial_instances')
        ensure_id(str(feature.get('id', '')), 'feature ID')
        if not isinstance(feature.get('patterns'), list) or not feature['patterns'] or not all(isinstance(x, str) and x for x in feature['patterns']): raise ValidationError('Repeated feature requires patterns')
        if feature.get('axis') not in (0, 1, 2) or not isinstance(feature.get('count'), int) or feature['count'] < 1: raise ValidationError('Repeated feature requires axis and positive count')
        _vec3(feature.get('center'), 'repeated feature center')
        for key in ('spacing_tolerance', 'depth', 'depth_tolerance'):
            if isinstance(feature.get(key), bool) or not isinstance(feature.get(key), (int, float)) or feature[key] < 0: raise ValidationError('Repeated feature '+key+' must be nonnegative')
    exploration = config.get('design_exploration')
    if exploration is not None:
        if not isinstance(exploration, dict) or exploration.get('enabled') is not True:
            raise ValidationError('design_exploration must be an enabled table when declared')
        for key, expected in (('concepts', 3), ('refinement_rounds', 4), ('cycles', 2)):
            value = exploration.get(key, expected)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValidationError('design_exploration limits must be positive integers')
    from .design import parse_design_manifest
    if not discovered:
        parse_design_manifest(config)
    manifest = load_toml(manifest_file)
    if manifest.get("version") != 1:
        raise ValidationError("inputs/manifest.toml version must be 1")
    entries = manifest.get("inputs", [])
    if not isinstance(entries, list):
        raise ValidationError("manifest [[inputs]] must be an array")
    inputs_root = manifest_file.parent.resolve()
    ids: set[str] = set()
    inputs: list[TaskInput] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValidationError("Each input must be a table")
        unknown = set(entry) - {"id", "role", "use", "path", "media_type", "title", "source_url", "view_classification", "known_dimension", "notes", "confidence", "sha256", "size"}
        if unknown:
            raise ValidationError(f"Unknown input fields: {', '.join(sorted(unknown))}")
        input_id = ensure_id(str(entry.get("id", "")), "input ID")
        if input_id in ids:
            raise ValidationError(f"Duplicate input ID: {input_id}")
        ids.add(input_id)
        role = entry.get("role")
        use = entry.get("use")
        if role not in INPUT_ROLES:
            raise ValidationError(f"Invalid input role for {input_id}: {role!r}")
        if use not in INPUT_USES:
            raise ValidationError(f"Invalid input use for {input_id}: {use!r}")
        if not isinstance(entry.get("path"), str) or not entry["path"]:
            raise ValidationError(f"Input {input_id} needs a relative path")
        raw_path = Path(entry["path"])
        if raw_path.is_absolute():
            raise ValidationError(f"Input {input_id} path must be relative to inputs/")
        path = resolve_within(inputs_root, raw_path)
        if not path.is_file() or is_link(path):
            raise ValidationError(f"Input {input_id} must be a regular non-linked file")
        if entry.get("sha256") is not None and entry["sha256"] != sha256_file(path):
            raise ValidationError("Input checksum does not match owned bytes")
        if entry.get("size") is not None and entry["size"] != path.stat().st_size:
            raise ValidationError("Input size does not match owned bytes")
        view = entry.get("view_classification", "unknown")
        if view not in {"orthographic", "perspective", "section", "plan", "unknown"}:
            raise ValidationError(f"Invalid view classification for {input_id}: {view!r}")
        confidence = entry.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            raise ValidationError(f"Input {input_id} confidence must be between 0 and 1")
        known_dimension = entry.get("known_dimension")
        if known_dimension is not None:
            if not isinstance(known_dimension, dict):
                raise ValidationError(f"Input {input_id} known_dimension must be a table")
            if not isinstance(known_dimension.get("name"), str) or not known_dimension["name"]:
                raise ValidationError(f"Input {input_id} known_dimension requires a name")
            known_value = known_dimension.get("value")
            if isinstance(known_value, bool) or not isinstance(known_value, (int, float)):
                raise ValidationError(f"Input {input_id} known_dimension value must be numeric")
            if not isinstance(known_dimension.get("unit"), str) or not known_dimension["unit"]:
                raise ValidationError(f"Input {input_id} known_dimension requires a unit")
        inputs.append(TaskInput(
            id=input_id,
            role=role,
            use=use,
            relative_path=raw_path.as_posix(),
            path=path,
            sha256=sha256_file(path),
            media_type=entry.get("media_type"),
            title=entry.get("title"),
            source_url=entry.get("source_url"),
            view_classification=view,
            known_dimension=known_dimension,
            notes=entry.get("notes"),
            confidence=float(confidence) if confidence is not None else None,
        ))

    inventory = tuple(file_inventory(directory))
    digest = canonical_hash({"task_id": task_id, "files": inventory})
    return TaskDefinition(task_id, directory, config, manifest, tuple(inputs), inventory, digest)


@dataclass(frozen=True)
class AgentProfile:
    name: str
    path: Path
    data: dict[str, Any]
    digest: str

    @property
    def command(self) -> list[str]:
        command = self.data.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise ValidationError(f"Agent profile {self.name} command must be a nonempty string array")
        return command

    def limit(self, key: str, default: int) -> int:
        value = self.data.get("limits", {}).get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValidationError(f"Agent limit {key} must be a positive integer")
        return value


def load_agent_profile(root: Path, name: str) -> AgentProfile:
    ensure_id(name, "agent profile")
    path = root / "agents" / f"{name}.toml"
    if is_link(path):
        raise ValidationError(f"Agent profile may not be a link or junction: {path}")
    data = load_toml(path)
    if data.get("version") != 1:
        raise ValidationError(f"Agent profile {name} version must be 1")
    if data.get("adapter") != "command":
        raise ValidationError("v1 supports only adapter='command'")
    profile = AgentProfile(name, path, data, sha256_file(path))
    profile.command
    pass_env = data.get("pass_env", [])
    if (
        not isinstance(pass_env, list)
        or not all(isinstance(item, str) and item and item.isidentifier() for item in pass_env)
        or len(set(pass_env)) != len(pass_env)
    ):
        raise ValidationError(f"Agent profile {name} pass_env must be a unique array of environment variable names")
    for key, default in (("max_turns", 8), ("max_checkpoints", 32), ("wall_clock_seconds", 14400), ("research_bytes", 1_000_000_000), ("blender_seconds", 3600)):
        profile.limit(key, default)
    return profile


@dataclass(frozen=True)
class RenderProfile:
    name: str
    path: Path
    data: dict[str, Any]
    digest: str


def load_render_profile(root: Path, name: str) -> RenderProfile:
    ensure_id(name, "render profile")
    path = root / "render_profiles" / f"{name}.toml"
    if is_link(path):
        raise ValidationError(f"Render profile may not be a link or junction: {path}")
    data = load_toml(path)
    if data.get("version") != 1:
        raise ValidationError(f"Render profile {name} version must be 1")
    required = {
        "blender_version": "5.1",
        "engine": "CYCLES",
        "device": "OPTIX",
        "gpu_name": "NVIDIA GeForce RTX 3090",
        "denoiser": "OPTIX",
        "view_transform": "ACES 2.0",
        "display_device": "sRGB",
        "output_format": "PNG",
        "color_depth": 8,
        "resolution_percentage": 100,
        "background": "neutral",
        "background_color": [0.12, 0.12, 0.12, 1.0],
        "background_strength": 1.0,
        "material": "neutral_gray",
        "base_color": [0.18, 0.18, 0.18, 1.0],
        "roughness_default": 0.36,
        "roughness_min": 0.2,
        "roughness_max": 0.8,
        "glass_roughness": 0.1,
    }
    for key, expected in required.items():
        if key in data and data.get(key) != expected:
            raise ValidationError(f"{name}.{key} must be {expected!r}, got {data.get(key)!r}")
    for key in ("samples", "resolution"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValidationError(f"{name}.{key} must be a positive integer")
    if data.get("id") != name:
        raise ValidationError(f"Render profile id must match filename {name!r}")
    return RenderProfile(name, path, data, sha256_file(path))


def expand_command(tokens: list[str], values: dict[str, str]) -> list[str]:
    expanded = []
    for token in tokens:
        try:
            expanded.append(token.format_map(values))
        except KeyError as exc:
            raise ValidationError(f"Unknown command placeholder {exc.args[0]!r}") from exc
    if expanded[0] == "{python}":
        expanded[0] = sys.executable
    return expanded
