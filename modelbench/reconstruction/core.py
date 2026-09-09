"""Pure-JSON perspective/orthographic camera fitting.

This module intentionally has no Blender or ModelingBench lifecycle dependency.
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import time
from pathlib import Path
from typing import Any, Mapping

CASE_VERSION = "1.0"
SOLVER_VERSION = "1.0"


class ReconstructionError(ValueError):
    """Raised when a reconstruction case is malformed or cannot be compared."""


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
    result = list(raw)
    for index, term in enumerate(constraints):
        if not isinstance(term, Mapping):
            raise ReconstructionError(f"constraints[{index}] must be an object")
        kind = term.get("type")
        if kind in ("point", "curve", "silhouette", "conic"):
            if kind == "point":
                result.append(term)
            else:
                samples = term.get("samples", term.get("correspondences", []))
                if not isinstance(samples, list):
                    raise ReconstructionError(f"constraints[{index}].samples must be an array")
                result.extend(samples)
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
    for i, item in enumerate(_term_correspondences(case)):
        if not isinstance(item, Mapping):
            raise ReconstructionError(f"correspondence {i} must be an object")
        _validate_vec(item.get("world"), 3, f"correspondence {i}.world")
        image = _validate_vec(item.get("image"), 2, f"correspondence {i}.image")
        if not (0 <= image[0] <= 1 and 0 <= image[1] <= 1):
            raise ReconstructionError(f"correspondence {i}.image must be normalized to [0, 1]")
    return dict(case)


def _rotation_matrix(rotvec, np):
    theta = float(np.linalg.norm(rotvec))
    if theta < 1e-12:
        return np.eye(3)
    axis = rotvec / theta
    cross = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    return np.eye(3) + math.sin(theta) * cross + (1 - math.cos(theta)) * (cross @ cross)


def _project(params, world, model, fixed_focal, np):
    rotation = _rotation_matrix(params[:3], np)
    if model == "perspective":
        focal = fixed_focal if fixed_focal is not None else math.exp(float(params[6]))
        translated = (rotation @ world.T).T + params[3:6]
        z = translated[:, 2]
        # Keep optimization finite behind the camera while heavily penalizing it.
        safe_z = np.where(np.abs(z) < 1e-8, np.where(z < 0, -1e-8, 1e-8), z)
        return np.column_stack((0.5 + focal * translated[:, 0] / safe_z, 0.5 + focal * translated[:, 1] / safe_z)), z
    scale = math.exp(float(params[3]))
    translated = (rotation @ world.T).T
    return np.column_stack((0.5 + scale * translated[:, 0] + params[4], 0.5 + scale * translated[:, 1] + params[5])), translated[:, 2]


def _fit(world, image, camera, config):
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
    starts = int(config.get("starts", 8))
    if not 1 <= starts <= 64:
        raise ReconstructionError("solver.starts must be between 1 and 64")
    max_nfev = int(config.get("max_nfev", 1000))
    if not 20 <= max_nfev <= 20000:
        raise ReconstructionError("solver.max_nfev must be between 20 and 20000")
    rng = np.random.default_rng(int(config.get("seed", 0)))
    base = np.array([0, 0, 0, 0, 0, 4, math.log(1.0)] if model == "perspective" else [0, 0, 0, math.log(1.0), 0, 0], dtype=float)
    def residual(params):
        projected, depth = _project(params, world, model, fixed_focal, np)
        values = (projected - image).ravel()
        if model == "perspective":
            values = np.concatenate((values, np.minimum(depth - 1e-4, 0.0) * 10))
        return values
    trials = []
    for n in range(starts):
        initial = base if n == 0 else base + rng.normal(0, 0.35, len(base))
        if model == "perspective": initial[5] = max(0.5, initial[5])
        result = least_squares(residual, initial, loss=str(config.get("loss", "soft_l1")), f_scale=float(config.get("f_scale", .01)), max_nfev=max_nfev)
        trials.append(result)
    best = min(trials, key=lambda result: float(np.mean(result.fun ** 2)))
    projected, depth = _project(best.x, world, model, fixed_focal, np)
    camera_out = {"model": model, "rotation_vector": best.x[:3].tolist(), "translation": (best.x[3:6].tolist() if model == "perspective" else [float(best.x[4]), float(best.x[5]), 0.0])}
    if model == "perspective": camera_out["focal_length"] = fixed_focal if fixed_focal is not None else math.exp(float(best.x[6]))
    else: camera_out["scale"] = math.exp(float(best.x[3]))
    rms = float(math.sqrt(float(np.mean((projected - image) ** 2))))
    return camera_out, projected, depth, rms, {"starts": starts, "successful_starts": sum(bool(x.success) for x in trials), "nfev": int(best.nfev)}


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


def solve(case: Mapping[str, Any]) -> dict[str, Any]:
    """Fit a camera from known 3D-to-normalized-2D correspondences; returns JSON-safe data."""
    started = time.perf_counter()
    try:
        case = validate_case(case)
        np = _require_array()
        entries = _term_correspondences(case)
        world = np.asarray([x["world"] for x in entries], dtype=float)
        image = np.asarray([x["image"] for x in entries], dtype=float)
        model = case["camera"]["model"]
        required = 6 if model == "perspective" else 4
        rank = int(np.linalg.matrix_rank(world - world.mean(axis=0))) if len(world) else 0
        if len(entries) < required or (model == "perspective" and rank < 3):
            status = "underconstrained"
            reason = f"{model} solve needs at least {required} correspondences" if len(entries) < required else "perspective solve needs non-coplanar 3D correspondences"
            fit = (None, None, None, None, {})
        else:
            status, reason = "solved", None
            fit = _fit(world, image, case["camera"], case.get("solver", {}))
        term_receipts = []
        for term in case.get("constraints", []):
            kind = term.get("type") if isinstance(term, Mapping) else None
            if kind in ("point", "curve", "silhouette", "conic"): term_receipts.append({"type": kind, "status": "sampled"})
            elif kind == "metric_segment":
                a, b = _validate_vec(term.get("a"), 3, "metric_segment.a"), _validate_vec(term.get("b"), 3, "metric_segment.b")
                measured = math.dist(a, b); expected = float(term["length"])
                term_receipts.append({"type": kind, "status": "measured", "length": measured, "error": measured - expected})
            elif kind == "occlusion": term_receipts.append({"type": kind, "status": "validated_only", "note": "requires solved camera and explicit surface depth samples"})
            elif kind not in (None,): term_receipts.append({"type": kind, "status": "unsupported"})
        camera, projected, depth, rms, fit_info = fit
        source_hashes = case.get("source_hashes", {})
        if not isinstance(source_hashes, Mapping): raise ReconstructionError("source_hashes must be an object")
        receipt = {"case_version": CASE_VERSION, "solver_version": SOLVER_VERSION, "case_hash": _json_hash(case), "source_hashes": dict(source_hashes), "dependencies": _deps(), "solver_config": dict(case.get("solver", {})), "runtime_ms": round((time.perf_counter() - started) * 1000, 3), "residual": ({"rms_normalized": rms, "count": len(entries)} if rms is not None else None), "terms": term_receipts, "fit": fit_info}
        return {"status": status, "reason": reason, "camera": camera, "rectification": _rectify(case), "projections": ([] if projected is None else projected.tolist()), "depth": ([] if depth is None else depth.tolist()), "receipt": receipt}
    except ReconstructionError as exc:
        return {"status": "invalid", "reason": str(exc), "camera": None, "rectification": None, "projections": [], "depth": [], "receipt": {"case_version": CASE_VERSION, "solver_version": SOLVER_VERSION, "runtime_ms": round((time.perf_counter() - started) * 1000, 3)}}


def save_bundle(case: Mapping[str, Any], destination: str | Path, input_images: list[str | Path] = ()) -> Path:
    """Save a replayable case and optional source images. Filesystem wrapper around pure solve."""
    root = Path(destination); root.mkdir(parents=True, exist_ok=True)
    (root / "case.json").write_text(json.dumps(case, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    images = root / "images"; images.mkdir(exist_ok=True)
    manifest = []
    for item in input_images:
        source = Path(item)
        if not source.is_file(): raise ReconstructionError(f"input image does not exist: {source}")
        target = images / source.name; shutil.copy2(source, target)
        manifest.append({"file": f"images/{target.name}", "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    (root / "manifest.json").write_text(json.dumps({"case_hash": _json_hash(case), "images": manifest}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return root


def load_bundle(bundle: str | Path) -> dict[str, Any]:
    return json.loads((Path(bundle) / "case.json").read_text(encoding="utf-8"))


def compare_receipts(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    a, b = left.get("receipt", left), right.get("receipt", right)
    for key in ("case_version", "solver_version", "source_hashes"):
        if a.get(key) != b.get(key): raise ReconstructionError(f"incompatible receipts: {key} differs")
    ar, br = a.get("residual") or {}, b.get("residual") or {}
    return {"compatible": True, "case_hash_equal": a.get("case_hash") == b.get("case_hash"), "rms_normalized": {"left": ar.get("rms_normalized"), "right": br.get("rms_normalized")}, "runtime_ms": {"left": a.get("runtime_ms"), "right": b.get("runtime_ms")}}
