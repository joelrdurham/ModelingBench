"""Blender-side clean-scene validation and standardized rendering.

This file is executed by Blender, not imported by the normal Python process.
It intentionally uses only the Blender Python API and the standard library.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
import traceback
from pathlib import Path

import bpy
from mathutils import Matrix, Vector


def _args() -> tuple[Path, dict]:
    if "--" not in sys.argv:
        raise RuntimeError("Missing ModelingBench invocation argument")
    path = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
    return path, json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite(values) -> bool:
    return all(math.isfinite(float(value)) for value in values)


def _inspect_source(source: Path) -> None:
    if source.suffix.lower() != ".blend" or not source.is_file():
        raise RuntimeError("Submission is not a readable .blend file")
    bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False)
    collection = bpy.data.collections.get("MODEL")
    if collection is None:
        raise RuntimeError("Submission must contain a collection named MODEL")
    if bpy.data.libraries:
        names = ", ".join(library.filepath for library in bpy.data.libraries)
        raise RuntimeError(f"External linked libraries are unsupported: {names}")
    external = []
    for group_name in ("images", "movieclips", "sounds", "fonts", "cache_files"):
        for datablock in getattr(bpy.data, group_name, []):
            filepath = getattr(datablock, "filepath", "")
            packed = getattr(datablock, "packed_file", None)
            if filepath and not packed and not str(filepath).startswith("<builtin>"):
                external.append(f"{group_name}:{datablock.name}:{filepath}")
    if external:
        raise RuntimeError("Unsupported external dependencies: " + ", ".join(external))


def _append_model(source: Path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with bpy.data.libraries.load(str(source), link=False) as (available, requested):
        if "MODEL" not in available.collections:
            raise RuntimeError("MODEL collection disappeared during clean append")
        requested.collections = ["MODEL"]
    collection = bpy.data.collections.get("MODEL")
    if collection is None:
        raise RuntimeError("Could not append MODEL into the clean evaluation scene")
    bpy.context.scene.collection.children.link(collection)
    return collection


def _collection_objects(collection):
    seen = set()
    result = []
    for obj in collection.all_objects:
        if obj.as_pointer() not in seen:
            seen.add(obj.as_pointer())
            result.append(obj)
    return result


def _geometry_inventory(collection, task: dict) -> tuple[dict, list[float], list[float]]:
    objects = _collection_objects(collection)
    if not objects:
        raise RuntimeError("MODEL is empty")
    for obj in objects:
        if not _finite(value for row in obj.matrix_world for value in row):
            raise RuntimeError(f"Object {obj.name!r} has a non-finite transform")
    depsgraph = bpy.context.evaluated_depsgraph_get()
    lower = [math.inf, math.inf, math.inf]
    upper = [-math.inf, -math.inf, -math.inf]
    polygons = 0
    evaluated_objects = 0
    per_object = []
    for instance in depsgraph.object_instances:
        original = instance.object.original
        if original not in objects and not any(parent in objects for parent in (instance.parent, getattr(instance.parent, "original", None)) if parent):
            continue
        evaluated = instance.object
        mesh = None
        try:
            mesh = evaluated.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
        except RuntimeError:
            mesh = None
        if mesh is None or not mesh.vertices:
            if mesh is not None:
                evaluated.to_mesh_clear()
            continue
        matrix = instance.matrix_world.copy()
        count = len(mesh.polygons)
        polygons += count
        evaluated_objects += 1
        for vertex in mesh.vertices:
            point = matrix @ vertex.co
            if not _finite(point):
                raise RuntimeError(f"Object {original.name!r} produced non-finite evaluated geometry")
            for axis in range(3):
                lower[axis] = min(lower[axis], point[axis])
                upper[axis] = max(upper[axis], point[axis])
        per_object.append({"name": original.name, "type": original.type, "evaluated_polygons": count})
        evaluated.to_mesh_clear()
    if evaluated_objects == 0 or polygons == 0 or not _finite(lower + upper):
        raise RuntimeError("MODEL has no non-empty evaluated polygonal geometry")
    frame = task["frame"]
    allowed_min = [float(value) for value in frame["bounds_min"]]
    allowed_max = [float(value) for value in frame["bounds_max"]]
    epsilon = 1e-6
    if any(lower[i] < allowed_min[i] - epsilon or upper[i] > allowed_max[i] + epsilon for i in range(3)):
        raise RuntimeError(f"Evaluated bounds {lower}..{upper} exceed task bounds {allowed_min}..{allowed_max}")
    anchors = []
    by_name = {obj.name: obj for obj in objects}
    for anchor in task.get("anchors", []):
        name = anchor["name"]
        obj = by_name.get(f"ANCHOR_{name}") or by_name.get(name)
        if obj is None:
            raise RuntimeError(f"Required anchor object missing: ANCHOR_{name}")
        actual = list(obj.matrix_world.translation)
        expected = [float(value) for value in anchor["position"]]
        distance = math.dist(actual, expected)
        tolerance = float(anchor["tolerance"])
        if distance > tolerance:
            raise RuntimeError(f"Anchor {name} is {distance:g} from its required position (tolerance {tolerance:g})")
        anchors.append({"name": name, "actual": actual, "expected": expected, "distance": distance, "tolerance": tolerance})
    inventory = {
        "source_objects": [
            {
                "name": obj.name,
                "type": obj.type,
                "matrix_world": [[float(value) for value in row] for row in obj.matrix_world],
            }
            for obj in objects
        ],
        "evaluated_objects": per_object,
        "evaluated_object_count": evaluated_objects,
        "evaluated_polygons": polygons,
        "bounds_min": lower,
        "bounds_max": upper,
        "anchors": anchors,
    }
    return inventory, lower, upper


def _strict_configuration(profile: dict) -> tuple[object, str]:
    expected = tuple(int(value) for value in str(profile["blender_version"]).split("."))
    if tuple(bpy.app.version[: len(expected)]) != expected:
        raise RuntimeError(f"Blender {profile['blender_version']} required, found {bpy.app.version_string}")
    scene = bpy.context.scene
    if profile["engine"] != "CYCLES" or profile["device"] != "OPTIX":
        raise RuntimeError("Production contract requires Cycles through OptiX")
    scene.render.engine = "CYCLES"
    preferences = bpy.context.preferences.addons["cycles"].preferences
    try:
        preferences.compute_device_type = "OPTIX"
        preferences.get_devices()
    except Exception as exc:
        raise RuntimeError(f"OptiX is unavailable: {exc}") from exc
    target = profile["gpu_name"]
    matched = None
    for device in preferences.devices:
        use = device.type == "OPTIX" and device.name == target
        device.use = use
        if use:
            matched = device
    if matched is None:
        available = [f"{item.name} ({item.type})" for item in preferences.devices]
        raise RuntimeError(f"Required GPU {target!r} unavailable; found {available}")
    scene.cycles.device = "GPU"
    scene.cycles.samples = int(profile["samples"])
    scene.cycles.use_denoising = True
    if hasattr(scene.cycles, "denoiser"):
        scene.cycles.denoiser = "OPTIX"
    scene.render.resolution_x = int(profile["resolution"])
    scene.render.resolution_y = int(profile["resolution"])
    scene.render.resolution_percentage = int(profile.get("resolution_percentage", 100))
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = str(profile.get("color_depth", 8))
    scene.render.film_transparent = False
    scene.display_settings.display_device = profile["display_device"]
    try:
        scene.view_settings.view_transform = profile["view_transform"]
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Required color transform {profile['view_transform']!r} unavailable") from exc
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = float(profile.get("unit_scale", 1.0))
    return matched, scene


def _neutralize(collection):
    material = bpy.data.materials.new("MB_NEUTRAL_GRAY")
    material.diffuse_color = (0.5, 0.5, 0.5, 1.0)
    material.use_nodes = True
    principled = material.node_tree.nodes.get("Principled BSDF")
    principled.inputs["Base Color"].default_value = (0.42, 0.42, 0.42, 1.0)
    principled.inputs["Roughness"].default_value = 0.6
    principled.inputs["Metallic"].default_value = 0.0
    submitted_lights = [obj for obj in _collection_objects(collection) if obj.type == "LIGHT"]
    for obj in submitted_lights:
        bpy.data.objects.remove(obj, do_unlink=True)
    for obj in _collection_objects(collection):
        obj.hide_render = False
        obj.hide_viewport = False
        for property_name in ("visible_camera", "visible_diffuse", "visible_glossy", "visible_transmission"):
            if hasattr(obj, property_name):
                setattr(obj, property_name, True)
        for property_name in ("is_holdout", "is_shadow_catcher"):
            if hasattr(obj, property_name):
                setattr(obj, property_name, False)
        if hasattr(obj.data, "materials"):
            obj.data.materials.clear()
            obj.data.materials.append(material)
    pending = [collection]
    while pending:
        current = pending.pop()
        current.hide_render = False
        current.hide_viewport = False
        pending.extend(current.children)
    bpy.context.view_layer.material_override = material
    world = bpy.data.worlds.new("MB_NEUTRAL_WORLD")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.055, 0.055, 0.055, 1.0)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.35
    bpy.context.scene.world = world


def _look_at(obj, target: Vector, image_up: Vector) -> None:
    forward = (target - obj.location).normalized()
    right = forward.cross(image_up).normalized()
    if right.length < 1e-8:
        raise RuntimeError("Camera direction is parallel to requested image-up axis")
    up = right.cross(forward).normalized()
    obj.rotation_euler = Matrix((right, up, -forward)).transposed().to_euler()


def _camera(name: str):
    data = bpy.data.cameras.new(f"MB_{name}_DATA")
    obj = bpy.data.objects.new(f"MB_{name}", data)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _lights(camera, center: Vector, distance: float) -> list[object]:
    direction = (center - camera.location).normalized()
    right = direction.cross(Vector((0, 0, 1)))
    if right.length < 1e-6:
        right = Vector((1, 0, 0))
    right.normalize()
    up = right.cross(direction).normalized()
    specs = [
        ("KEY", camera.location + right * distance * 0.55 + up * distance * 0.45, 1100.0, 4.0),
        ("FILL", camera.location - right * distance * 0.55 + up * distance * 0.1, 650.0, 5.0),
        ("RIM", center - direction * distance * 0.3 - right * distance * 0.45 + up * distance * 0.65, 900.0, 3.0),
    ]
    result = []
    for name, location, energy, size in specs:
        data = bpy.data.lights.new(f"MB_{name}", "AREA")
        data.energy = energy
        data.shape = "DISK"
        data.size = size
        obj = bpy.data.objects.new(f"MB_{name}", data)
        bpy.context.scene.collection.objects.link(obj)
        obj.location = location
        _look_at(obj, center, up)
        result.append(obj)
    return result


def _remove(objects) -> None:
    for obj in objects:
        bpy.data.objects.remove(obj, do_unlink=True)


def _evaluation_camera(view: str, task: dict) -> tuple[object, Vector, float]:
    frame = task["frame"]
    center = Vector(frame["evaluation_center"])
    lower = Vector(frame["bounds_min"])
    upper = Vector(frame["bounds_max"])
    size = upper - lower
    radius = size.length * 0.5 * 1.1
    camera = _camera(view)
    if view in {"front", "back", "left", "right", "top", "bottom"}:
        directions = {
            "front": Vector((0, -1, 0)), "back": Vector((0, 1, 0)),
            "left": Vector((-1, 0, 0)), "right": Vector((1, 0, 0)),
            "top": Vector((0, 0, 1)), "bottom": Vector((0, 0, -1)),
        }
        image_ups = {"top": Vector((0, 1, 0)), "bottom": Vector((0, -1, 0))}
        direction = directions[view]
        camera.location = center + direction * max(radius * 2.5, 1.0)
        _look_at(camera, center, image_ups.get(view, Vector((0, 0, 1))))
        camera.data.type = "ORTHO"
        if view in {"front", "back"}:
            span = max(size.x, size.z)
        elif view in {"left", "right"}:
            span = max(size.y, size.z)
        else:
            span = max(size.x, size.y)
        camera.data.ortho_scale = float(span * 1.1)
        return camera, center, max(radius * 2.5, 1.0)
    angle = math.radians(int(view.rsplit("_", 1)[1]))
    camera.data.type = "PERSP"
    camera.data.lens = 50.0
    sensor = camera.data.sensor_width
    fov = 2 * math.atan(sensor / (2 * camera.data.lens))
    distance = max(radius / math.sin(fov / 2), 1.0)
    elevation = math.radians(15.0)
    horizontal = distance * math.cos(elevation)
    camera.location = center + Vector((math.sin(angle) * horizontal, -math.cos(angle) * horizontal, math.sin(elevation) * distance))
    _look_at(camera, center, Vector((0, 0, 1)))
    return camera, center, distance


def _diagnostic_camera(record: dict) -> tuple[object, Vector, float]:
    name = record.get("name")
    if not isinstance(name, str) or not name or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in name):
        raise RuntimeError("Diagnostic camera names must be safe alphanumeric identifiers")
    location = Vector(record["location"])
    target = Vector(record["target"])
    if not _finite((*location, *target)) or location == target:
        raise RuntimeError(f"Invalid diagnostic camera {name}")
    camera = _camera(f"diagnostic_{name}")
    camera.location = location
    _look_at(camera, target, Vector(record.get("image_up", [0, 0, 1])))
    projection = record.get("projection", "perspective")
    if projection == "orthographic":
        camera.data.type = "ORTHO"
        camera.data.ortho_scale = float(record["ortho_scale"])
    elif projection == "perspective":
        camera.data.type = "PERSP"
        camera.data.lens = float(record.get("lens_mm", 50.0))
    else:
        raise RuntimeError(f"Invalid diagnostic projection: {projection}")
    return camera, target, float((location - target).length)


def _render(scene, camera, center: Vector, distance: float, output: Path, seed: int) -> dict:
    scene.camera = camera
    scene.cycles.seed = seed
    lights = _lights(camera, center, distance)
    output.parent.mkdir(parents=True, exist_ok=True)
    scene.render.filepath = str(output)
    started = time.perf_counter()
    bpy.ops.render.render(write_still=True)
    elapsed = time.perf_counter() - started
    _remove(lights)
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError(f"Render was not written: {output}")
    return {"path": str(output), "sha256": _hash(output), "size": output.stat().st_size, "seconds": elapsed, "seed": seed}


def main(config: dict) -> dict:
    source = Path(config["source_blend"]).resolve()
    output = Path(config["output_dir"]).resolve()
    profile = config["render_profile"]
    task = config["task"]
    source_sha256 = _hash(source)
    _inspect_source(source)
    collection = _append_model(source)
    inventory, geometry_min, geometry_max = _geometry_inventory(collection, task)
    gpu, scene = _strict_configuration(profile)
    scene.unit_settings.scale_length = float(task["frame"]["unit_scale"])
    _neutralize(collection)
    render_records = []
    for index, view in enumerate(config["views"]):
        if view not in {"front", "back", "left", "right", "top", "bottom", "azimuth_000", "azimuth_045", "azimuth_090", "azimuth_135", "azimuth_180", "azimuth_225", "azimuth_270", "azimuth_315"}:
            raise RuntimeError(f"Unknown standardized view: {view}")
        camera, center, distance = _evaluation_camera(view, task)
        group = "orthographic" if view in {"front", "back", "left", "right", "top", "bottom"} else "turntable"
        render_records.append(_render(scene, camera, center, distance, output / group / f"{view}.png", 1701 + index * 104729))
        _remove([camera])
    diagnostics = []
    for index, record in enumerate(config.get("diagnostic_cameras", [])):
        camera, center, distance = _diagnostic_camera(record)
        diagnostics.append(_render(scene, camera, center, distance, output / "diagnostic" / f"{record['name']}.png", 900001 + index))
        _remove([camera])
    if _hash(source) != source_sha256:
        raise RuntimeError("Owned source artifact changed during Blender evaluation")
    return {
        "ok": True,
        "source_sha256": source_sha256,
        "completed_at": time.time(),
        "blender": {"version": bpy.app.version_string, "build_hash": bpy.app.build_hash.decode(errors="replace") if isinstance(bpy.app.build_hash, bytes) else str(bpy.app.build_hash)},
        "device": {"name": gpu.name, "type": gpu.type, "driver": None},
        "render": {"engine": scene.render.engine, "samples": scene.cycles.samples, "denoiser": profile["denoiser"], "view_transform": scene.view_settings.view_transform, "ocio_configuration": getattr(bpy.context.preferences.system, "ocio_config_override", "") or None},
        "inventory": inventory,
        "renders": render_records,
        "diagnostic_renders": diagnostics,
    }


if __name__ == "__main__":
    invocation_path = None
    config = None
    try:
        invocation_path, config = _args()
        result_path = Path(config["result_path"])
        _write(result_path, main(config))
    except Exception as exc:
        if config and config.get("result_path"):
            _write(Path(config["result_path"]), {"ok": False, "error": str(exc), "traceback": traceback.format_exc()})
        traceback.print_exc()
        raise
