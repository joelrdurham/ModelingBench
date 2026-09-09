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
    if is_link(raw_directory):
        raise ValidationError("Task directory may not be a link or junction")
    directory = raw_directory.resolve()
    tasks_root = (root / "tasks").resolve()
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
    if config.get("version") != 1:
        raise ValidationError("task.toml version must be 1")
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
        unknown = set(entry) - {"id", "role", "use", "path", "media_type", "title", "source_url", "view_classification", "known_dimension", "notes", "confidence"}
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
        "material": "neutral_gray",
    }
    for key, expected in required.items():
        if data.get(key) != expected:
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
