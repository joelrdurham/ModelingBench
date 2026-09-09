"""Pure-JSON perspective/orthographic camera fitting.

This module intentionally has no Blender or ModelingBench lifecycle dependency.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

CASE_VERSION = "1.0"
SOLVER_VERSION = "1.0"
_SAMPLED_TERM_TYPES = {"point", "curve", "silhouette", "conic"}
_PRIOR_TERM_TYPES = {"shared_point", "parallel", "perpendicular", "symmetry", "repetition"}
_SUPPORTED_TERM_TYPES = _SAMPLED_TERM_TYPES | {"metric_segment"} | _PRIOR_TERM_TYPES


from .contracts import ReconstructionError


def _implementation_hash() -> str:
    """Hash this package's Python sources so receipts identify exact solver code."""
    digest = hashlib.sha256()
    package = Path(__file__).parent
    for source in sorted(package.glob("*.py"), key=lambda path: path.name):
        digest.update(source.name.encode("utf-8") + b"\0")
        digest.update(source.read_bytes())
    return digest.hexdigest()

def _json_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _deps() -> dict[str, str | None]:
    try:
        import numpy
        numpy_version = numpy.__version__
    except ImportError:
        numpy_version = None
    try:
        import scipy
        scipy_version = scipy.__version__
    except ImportError:
        scipy_version = None
    return {"numpy": numpy_version, "scipy": scipy_version, "pillow": _pillow_version()}


def _pillow_version() -> str | None:
    try:
        import PIL
        return PIL.__version__
    except ImportError:
        return None


def _require_array():
    try:
        import numpy as np
    except ImportError as exc:
        raise ReconstructionError("solve requires the optional reconstruction extra: pip install 'modelingbench[reconstruction]' (NumPy and SciPy)") from exc
    return np


def _validate_vec(value: Any, size: int, name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != size or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in value):
        raise ReconstructionError(f"{name} must be {size} finite numbers")
    return [float(x) for x in value]


def _term_correspondences(case: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = case.get("correspondences", [])
    constraints = case.get("constraints", [])
    if not isinstance(raw, list) or not isinstance(constraints, list):
        raise ReconstructionError("correspondences and constraints must be arrays")
    result = [dict(item) for item in raw]
    for index, term in enumerate(constraints):
        if not isinstance(term, Mapping):
            raise ReconstructionError(f"constraints[{index}] must be an object")
        kind = term.get("type")
        if kind in _SAMPLED_TERM_TYPES:
            if kind == "point":
                result.append(term)
            else:
                samples = term.get("samples", term.get("correspondences", []))
                if not isinstance(samples, list):
                    raise ReconstructionError(f"constraints[{index}].samples must be an array")
                result.extend({**sample, "_term_weight": term.get("weight", 1.0)} for sample in samples)
    return result


def validate_case(case: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(case, Mapping):
        raise ReconstructionError("case must be a JSON object")
    if case.get("case_version") != CASE_VERSION:
        raise ReconstructionError(f"case_version must be {CASE_VERSION!r}")
    coords = case.get("image_coordinates")
    if not isinstance(coords, Mapping) or coords.get("space") != "normalized" or coords.get("origin") != "top-left":
        raise ReconstructionError("image_coordinates must explicitly use normalized, top-left coordinates")
    if coords.get("orientation", "upright") != "upright":
        raise ReconstructionError("only upright image orientation is supported; pre-orient the image first")
    if "crop" in coords:
        crop = _validate_vec(coords["crop"], 4, "image_coordinates.crop")
        if crop[2] <= 0 or crop[3] <= 0:
            raise ReconstructionError("image_coordinates.crop width and height must be positive")
    camera = case.get("camera", {})
    if not isinstance(camera, Mapping) or camera.get("model") not in ("perspective", "orthographic"):
        raise ReconstructionError("camera.model must be perspective or orthographic")
    intrinsics = camera.get("intrinsics", {})
    if not isinstance(intrinsics, Mapping):
        raise ReconstructionError("camera.intrinsics must be an object")
    if "principal_point" in intrinsics:
        principal = _validate_vec(intrinsics["principal_point"], 2, "camera.intrinsics.principal_point")
        if not (0 <= principal[0] <= 1 and 0 <= principal[1] <= 1):
            raise ReconstructionError("camera.intrinsics.principal_point must be normalized to [0, 1]")
    for index, term in enumerate(case.get("constraints", [])):
        if not isinstance(term, Mapping) or term.get("type") not in _SUPPORTED_TERM_TYPES:
            kind = term.get("type") if isinstance(term, Mapping) else None
            raise ReconstructionError(f"constraints[{index}] has unsupported type {kind!r}; v1 supports point, curve, silhouette, conic, and metric_segment")
    for i, item in enumerate(_term_correspondences(case)):
        if not isinstance(item, Mapping):
            raise ReconstructionError(f"correspondence {i} must be an object")
        _validate_vec(item.get("world"), 3, f"correspondence {i}.world")
        image = _validate_vec(item.get("image"), 2, f"correspondence {i}.image")
        if not (0 <= image[0] <= 1 and 0 <= image[1] <= 1):
            raise ReconstructionError(f"correspondence {i}.image must be normalized to [0, 1]")
        weight = item.get("weight", item.get("_term_weight", 1.0))
        if not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight <= 0:
            raise ReconstructionError(f"correspondence {i}.weight must be a positive finite number")
    return dict(case)


def _crop_normalized_image(case: Mapping[str, Any], point: list[float]) -> list[float]:
    """Convert an optional full-image normalized point into the declared crop frame."""
    coords = case["image_coordinates"]
    if coords.get("frame", "crop") == "crop":
        return point
    if coords.get("frame") != "full_image":
        raise ReconstructionError("image_coordinates.frame must be crop or full_image")
    crop = coords.get("crop")
    if crop is None:
        raise ReconstructionError("image_coordinates.frame full_image requires image_coordinates.crop")
    converted = [(point[0] - crop[0]) / crop[2], (point[1] - crop[1]) / crop[3]]
    if not (0 <= converted[0] <= 1 and 0 <= converted[1] <= 1):
        raise ReconstructionError("full-image correspondence lies outside image_coordinates.crop")
    return converted

def _rotation_matrix(rotvec, np):
    theta = float(np.linalg.norm(rotvec))
    if theta < 1e-12:
        return np.eye(3)
    axis = rotvec / theta
    cross = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    return np.eye(3) + math.sin(theta) * cross + (1 - math.cos(theta)) * (cross @ cross)


def _project(params, world, model, fixed_focal, principal_point, np):
    rotation = _rotation_matrix(params[:3], np)
    if model == "perspective":
        focal = fixed_focal if fixed_focal is not None else math.exp(float(params[6]))
        translated = (rotation @ world.T).T + params[3:6]
        z = translated[:, 2]
        # Keep optimization finite behind the camera while heavily penalizing it.
        safe_z = np.where(np.abs(z) < 1e-8, np.where(z < 0, -1e-8, 1e-8), z)
        return np.column_stack((principal_point[0] + focal * translated[:, 0] / safe_z, principal_point[1] + focal * translated[:, 1] / safe_z)), z
    scale = math.exp(float(params[3]))
    translated = (rotation @ world.T).T
    return np.column_stack((principal_point[0] + scale * translated[:, 0] + params[4], principal_point[1] + scale * translated[:, 1] + params[5])), translated[:, 2]


def _camera_out(params, model, fixed_focal, principal_point):
    camera_out = {"model": model, "world_to_camera": {"rotation_vector": params[:3].tolist(), "convention": "X_camera = Rodrigues(rotation_vector) @ X_world + translation"}, "principal_point": list(principal_point)}
    if model == "perspective":
        camera_out["world_to_camera"]["translation"] = params[3:6].tolist()
        camera_out["focal_length"] = fixed_focal if fixed_focal is not None else math.exp(float(params[6]))
    else:
        camera_out["image_offset"] = [float(params[4]), float(params[5])]
        camera_out["scale"] = math.exp(float(params[3]))
    return camera_out

def _fit(world, image, weights, camera, config):
    np = _require_array()
    try:
        from scipy.optimize import least_squares
    except ImportError as exc:
        raise ReconstructionError("solve requires SciPy; install the optional reconstruction extra") from exc
    model = camera["model"]
    intrinsics = camera.get("intrinsics", {})
    if not isinstance(intrinsics, Mapping):
        raise ReconstructionError("camera.intrinsics must be an object")
    focal = intrinsics.get("focal_length")
    if focal is not None and (not isinstance(focal, (int, float)) or focal <= 0):
        raise ReconstructionError("camera.intrinsics.focal_length must be positive")
    fixed_focal = float(focal) if focal is not None else None
    principal_point = intrinsics.get("principal_point", [0.5, 0.5])
    starts = int(config.get("starts", 8))
    if not 1 <= starts <= 64:
        raise ReconstructionError("solver.starts must be between 1 and 64")
    max_nfev = int(config.get("max_nfev", 1000))
    if not 20 <= max_nfev <= 20000:
        raise ReconstructionError("solver.max_nfev must be between 20 and 20000")
    rng = np.random.default_rng(int(config.get("seed", 0)))
    if model == "perspective":
        base = np.array([0, 0, 0, 0, 0, 4] + ([] if fixed_focal is not None else [math.log(1.0)]), dtype=float)
        lower = np.array([-math.pi, -math.pi, -math.pi, -1e4, -1e4, 1e-4] + ([] if fixed_focal is not None else [math.log(.01)]))
        upper = np.array([math.pi, math.pi, math.pi, 1e4, 1e4, 1e4] + ([] if fixed_focal is not None else [math.log(10.0)]))
    else:
        base = np.array([0, 0, 0, math.log(1.0), 0, 0], dtype=float)
        lower = np.array([-math.pi, -math.pi, -math.pi, math.log(1e-4), -10, -10])
        upper = np.array([math.pi, math.pi, math.pi, math.log(1e4), 10, 10])
    def residual(params):
        projected, depth = _project(params, world, model, fixed_focal, principal_point, np)
        values = ((projected - image) * np.sqrt(weights)[:, None]).ravel()
        if model == "perspective":
            values = np.concatenate((values, np.minimum(depth - 1e-4, 0.0) * 10))
        return values
    trials = []
    for n in range(starts):
        initial = base if n == 0 else np.clip(base + rng.normal(0, 0.35, len(base)), lower + 1e-7, upper - 1e-7)
        result = least_squares(residual, initial, bounds=(lower, upper), loss=str(config.get("loss", "soft_l1")), f_scale=float(config.get("f_scale", .01)), max_nfev=max_nfev)
        trials.append(result)
    best = min(trials, key=lambda result: float(np.mean(result.fun ** 2)))
    projected, depth = _project(best.x, world, model, fixed_focal, principal_point, np)
    camera_out = _camera_out(best.x, model, fixed_focal, principal_point)
    rms = float(math.sqrt(float(np.mean((projected - image) ** 2))))
    singular = np.linalg.svd(best.jac, compute_uv=False)
    threshold = max(float(singular[0]) * 1e-8, 1e-10) if len(singular) else float("inf")
    jacobian_rank = int(np.count_nonzero(singular > threshold))
    hypotheses = [{"camera": _camera_out(trial.x, model, fixed_focal, principal_point), "rms_normalized": float(math.sqrt(float(np.mean((_project(trial.x, world, model, fixed_focal, principal_point, np)[0] - image) ** 2)))), "nfev": int(trial.nfev), "success": bool(trial.success)} for trial in sorted(trials, key=lambda item: float(np.mean(item.fun ** 2)))[:3]]
    comparable = [trial for trial in trials if float(math.sqrt(float(np.mean((_project(trial.x, world, model, fixed_focal, principal_point, np)[0] - image) ** 2)))) <= rms + max(1e-6, rms * .05)]
    unresolved = any(np.linalg.norm(trial.x[:3] - best.x[:3]) > .05 or (model == "perspective" and np.linalg.norm(trial.x[3:6] - best.x[3:6]) > .1) for trial in comparable)
    return camera_out, projected, depth, rms, {"starts": starts, "successful_starts": sum(bool(x.success) for x in trials), "nfev": int(best.nfev), "bounds_applied": True, "identifiability": {"jacobian_rank": jacobian_rank, "parameter_count": len(best.x), "smallest_singular_value": float(singular[-1]) if len(singular) else 0.0}, "ambiguity": {"comparable_hypotheses": len(comparable), "unresolved": bool(unresolved)}, "hypotheses": hypotheses}


def _rectify(case: Mapping[str, Any]) -> dict[str, Any] | None:
    np = _require_array()
    planes = case.get("planes", [])
    if not isinstance(planes, list): raise ReconstructionError("planes must be an array")
    for plane in planes:
        if not isinstance(plane, Mapping) or "rectification" not in plane: continue
        r = plane["rectification"]
        if not isinstance(r, Mapping): raise ReconstructionError("plane.rectification must be an object")
        world = np.asarray(r.get("plane_points"), float); image = np.asarray(r.get("image_points"), float)
        if world.shape != (4, 2) or image.shape != (4, 2): raise ReconstructionError("rectification requires four 2D plane_points and image_points")
        rows = []
        for (x, y), (u, v) in zip(world, image):
            rows.extend([[x, y, 1, 0, 0, 0, -u*x, -u*y, -u], [0, 0, 0, x, y, 1, -v*x, -v*y, -v]])
        _, _, vt = np.linalg.svd(np.asarray(rows))
        H = (vt[-1] / vt[-1, -1]).reshape(3, 3)
        return {"plane_id": plane.get("id"), "homography_plane_to_image": H.tolist()}
    return None


def _verify_prior(term: Mapping[str, Any]) -> dict[str, Any]:
    kind = term["type"]
    if kind == "shared_point":
        error = math.dist(_validate_vec(term.get("a"), 3, "shared_point.a"), _validate_vec(term.get("b"), 3, "shared_point.b"))
    elif kind in {"parallel", "perpendicular"}:
        a = _validate_vec(term.get("a"), 3, f"{kind}.a"); b = _validate_vec(term.get("b"), 3, f"{kind}.b")
        norm = math.dist(a, [0, 0, 0]) * math.dist(b, [0, 0, 0])
        if norm <= 1e-12: raise ReconstructionError(f"{kind} directions must be non-zero")
        dot = sum(x * y for x, y in zip(a, b)) / norm
        error = math.sqrt(max(0.0, 1 - dot * dot)) if kind == "parallel" else abs(dot)
    elif kind == "symmetry":
        center = _validate_vec(term.get("center"), 3, "symmetry.center"); left = _validate_vec(term.get("left"), 3, "symmetry.left"); right = _validate_vec(term.get("right"), 3, "symmetry.right")
        error = math.dist(center, [(x + y) / 2 for x, y in zip(left, right)])
    else:  # repetition
        points = term.get("points")
        if not isinstance(points, list) or len(points) < 3: raise ReconstructionError("repetition.points needs at least three known 3D points")
        vectors = [[b[i] - a[i] for i in range(3)] for a, b in zip([_validate_vec(x, 3, "repetition.points") for x in points], [_validate_vec(x, 3, "repetition.points") for x in points][1:])]
        error = max(math.dist(vector, vectors[0]) for vector in vectors[1:])
    tolerance = float(term.get("tolerance", 1e-6))
    if not math.isfinite(tolerance) or tolerance < 0: raise ReconstructionError(f"{kind}.tolerance must be non-negative")
    return {"type": kind, "status": "verified" if error <= tolerance else "violated", "error": error, "tolerance": tolerance, "constrains_solver": False}

def _geometry_scale(case: Mapping[str, Any]) -> tuple[float, dict[str, Any] | None]:
    config = case.get("geometry_scale", {})
    if config in (None, False) or (isinstance(config, Mapping) and not config.get("solve", False)):
        return 1.0, None
    if config is not True and not isinstance(config, Mapping):
        raise ReconstructionError("geometry_scale must be true or an object")
    segments = [term for term in case.get("constraints", []) if term.get("type") == "metric_segment"]
    if not segments:
        raise ReconstructionError("geometry_scale.solve requires at least one metric_segment")
    observed = []
    for term in segments:
        a = _validate_vec(term.get("a"), 3, "metric_segment.a")
        b = _validate_vec(term.get("b"), 3, "metric_segment.b")
        length = term.get("length")
        if not isinstance(length, (int, float)) or not math.isfinite(length) or length <= 0:
            raise ReconstructionError("metric_segment.length must be a positive finite number for geometry_scale")
        distance = math.dist(a, b)
        if distance <= 1e-12:
            raise ReconstructionError("metric_segment endpoints must differ for geometry_scale")
        observed.append((distance, float(length)))
    denominator = sum(distance * distance for distance, _ in observed)
    scale = sum(distance * length for distance, length in observed) / denominator
    errors = [scale * distance - length for distance, length in observed]
    rms = math.sqrt(sum(error * error for error in errors) / len(errors))
    tolerance = float(config.get("max_rms_error", 1e-6)) if isinstance(config, Mapping) else 1e-6
    if tolerance < 0: raise ReconstructionError("geometry_scale.max_rms_error must be non-negative")
    return scale, {"enabled": True, "uniform_scale": scale, "source_coordinates": "case world coordinates", "assumption": "one global linear scale converts case world units to metric_segment.length units", "rms_metric_error": rms, "max_rms_error": tolerance, "consistent": rms <= tolerance}

def solve(case: Mapping[str, Any]) -> dict[str, Any]:
    """Fit a camera from known 3D-to-normalized-2D correspondences; returns JSON-safe data."""
    started = time.perf_counter()
    try:
        case = validate_case(case)
        np = _require_array()
        entries = _term_correspondences(case)
        scale, scale_receipt = _geometry_scale(case)
        world = np.asarray([x["world"] for x in entries], dtype=float) * scale
        image = np.asarray([_crop_normalized_image(case, x["image"]) for x in entries], dtype=float)
        model = case["camera"]["model"]
        rectification = _rectify(case)
        required = 6 if model == "perspective" else 4
        rank = int(np.linalg.matrix_rank(world - world.mean(axis=0))) if len(world) else 0
        if not entries and rectification is not None:
            status, reason = "rectified", "planar homography computed; no 3D camera was requested"
            fit = (None, None, None, None, {})
        elif len(entries) < required or (model == "perspective" and rank < 3):
            status = "underconstrained"
            reason = f"{model} solve needs at least {required} correspondences" if len(entries) < required else "perspective solve needs non-coplanar 3D correspondences"
            fit = (None, None, None, None, {})
        else:
            status, reason = "solved", None
            fit = _fit(world, image, np.asarray([x.get("weight", x.get("_term_weight", 1.0)) for x in entries], dtype=float), case["camera"], case.get("solver", {}))
            if fit[4]["identifiability"]["jacobian_rank"] < fit[4]["identifiability"]["parameter_count"]:
                status, reason = "underconstrained", "local Jacobian is rank deficient at the fitted camera"
            elif fit[4]["ambiguity"]["unresolved"]:
                status, reason = "underconstrained", "comparable multi-start cameras disagree"
            elif scale_receipt and not scale_receipt["consistent"]:
                status, reason = "inconsistent", "metric segments disagree with the requested uniform geometry scale"
        if scale_receipt and not scale_receipt["consistent"]:
            status, reason = "inconsistent", "metric segments disagree with the requested uniform geometry scale"
        term_receipts = []
        for term in case.get("constraints", []):
            kind = term.get("type") if isinstance(term, Mapping) else None
            if kind in _SAMPLED_TERM_TYPES: term_receipts.append({"type": kind, "status": "sampled"})
            elif kind in _PRIOR_TERM_TYPES: term_receipts.append(_verify_prior(term))
            elif kind == "metric_segment":
                a, b = _validate_vec(term.get("a"), 3, "metric_segment.a"), _validate_vec(term.get("b"), 3, "metric_segment.b")
                if not isinstance(term.get("length"), (int, float)) or not math.isfinite(term["length"]):
                    raise ReconstructionError("metric_segment.length must be a finite number")
                measured = math.dist(a, b); expected = float(term["length"])
                term_receipts.append({"type": kind, "status": "measured", "length": measured, "error": measured - expected})

        camera, projected, depth, rms, fit_info = fit
        source_hashes = case.get("source_hashes", {})
        if not isinstance(source_hashes, Mapping): raise ReconstructionError("source_hashes must be an object")
        receipt = {"case_version": CASE_VERSION, "solver_version": SOLVER_VERSION, "implementation_sha256": _implementation_hash(), "case_hash": _json_hash(case), "source_hashes": dict(source_hashes), "dependencies": _deps(), "solver_config": dict(case.get("solver", {})), "runtime_ms": round((time.perf_counter() - started) * 1000, 3), "residual": ({"rms_normalized": rms, "count": len(entries)} if rms is not None else None), "terms": term_receipts, "fit": fit_info, "geometry_scale": scale_receipt}
        return {"status": status, "reason": reason, "camera": camera, "rectification": rectification, "projections": ([] if projected is None else projected.tolist()), "depth": ([] if depth is None else depth.tolist()), "receipt": receipt}
    except (ReconstructionError, ValueError) as exc:
        return {"status": "invalid", "reason": str(exc), "camera": None, "rectification": None, "projections": [], "depth": [], "receipt": {"case_version": CASE_VERSION, "solver_version": SOLVER_VERSION, "implementation_sha256": _implementation_hash(), "runtime_ms": round((time.perf_counter() - started) * 1000, 3)}}


def save_bundle(case: Mapping[str, Any], destination: str | Path, input_images: list[str | Path] = ()) -> Path:
    """Save a replayable case with opaque copied files and a strict manifest."""
    root = Path(destination)
    if root.is_symlink(): raise ReconstructionError("bundle destination may not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    images = root / "images"
    if images.is_symlink(): raise ReconstructionError("bundle images directory may not be a symlink")
    images.mkdir(exist_ok=True)
    sources = [Path(item) for item in input_images]
    names = [source.name.casefold() for source in sources]
    if len(names) != len(set(names)): raise ReconstructionError("input images have duplicate basenames")
    manifest = []
    for index, source in enumerate(sources):
        if source.is_symlink() or not source.is_file(): raise ReconstructionError(f"input image must be a regular non-symlink file: {source}")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        target = images / f"image_{index:03d}_{digest[:16]}{source.suffix.lower()}"
        if target.exists() or target.is_symlink(): raise ReconstructionError(f"owned bundle image already exists: {target.name}")
        shutil.copy2(source, target)
        manifest.append({"file": f"images/{target.name}", "sha256": digest, "source_basename": source.name})
    (root / "case.json").write_text(json.dumps(case, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps({"case_hash": _json_hash(case), "images": manifest}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return root


def load_bundle(bundle: str | Path) -> dict[str, Any]:
    root = Path(bundle)
    if root.is_symlink() or not root.is_dir(): raise ReconstructionError("bundle must be a regular directory")
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink(): raise ReconstructionError("bundle manifest may not be a symlink")
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in manifest.get("images", []):
            relative = PurePosixPath(item.get("file", ""))
            if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 2 or relative.parts[0] != "images":
                raise ReconstructionError("bundle manifest image path is unsafe")
            path = root.joinpath(*relative.parts)
            if path.is_symlink() or not path.is_file(): raise ReconstructionError(f"bundle input image identity changed: {item.get('file')}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != item.get("sha256"): raise ReconstructionError(f"bundle input image identity changed: {item.get('file')}")
    case_path = root / "case.json"
    if case_path.is_symlink() or not case_path.is_file(): raise ReconstructionError("bundle case file is missing or unsafe")
    case = json.loads(case_path.read_text(encoding="utf-8"))
    if manifest_path.exists() and manifest.get("case_hash") != _json_hash(case): raise ReconstructionError("bundle case identity changed")
    return case

def compare_receipts(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    a, b = left.get("receipt", left), right.get("receipt", right)
    for key in ("case_version", "solver_version", "source_hashes", "case_hash"):
        if a.get(key) != b.get(key): raise ReconstructionError(f"incompatible receipts: {key} differs")
    ar, br = a.get("residual") or {}, b.get("residual") or {}
    return {"compatible": True, "implementation_sha256": {"left": a.get("implementation_sha256"), "right": b.get("implementation_sha256"), "equal": a.get("implementation_sha256") == b.get("implementation_sha256")}, "rms_normalized": {"left": ar.get("rms_normalized"), "right": br.get("rms_normalized"), "delta": (None if ar.get("rms_normalized") is None or br.get("rms_normalized") is None else br["rms_normalized"] - ar["rms_normalized"])}, "runtime_ms": {"left": a.get("runtime_ms"), "right": b.get("runtime_ms"), "delta": (None if a.get("runtime_ms") is None or b.get("runtime_ms") is None else b["runtime_ms"] - a["runtime_ms"])}}
