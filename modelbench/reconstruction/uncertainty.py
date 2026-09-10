"""Local evidence rank and measurement-specific identifiability.

All derivatives exclude numerical depth penalties and explicit gauge constraints.
Bounds constrain search, but are never counted as measured equations.
"""
from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation

from .constraints import measurement

CONDITIONAL = "conditional on supplied correspondences, uncertainties, constraints, and this hypothesis"


def jacobian(function, x, scales):
    base = np.atleast_1d(function(x))
    result = np.empty((len(base), len(x)))
    for i, scale in enumerate(scales):
        step = max(abs(x[i]), scale) * 1e-5
        a, b = x.copy(), x.copy()
        a[i] += step; b[i] -= step
        result[:, i] = (np.atleast_1d(function(a)) - np.atleast_1d(function(b))) / (2*step) * scale
    return result


def _gauge_directions(parameters, values):
    """Similarity action on all coordinates, only if fixed values also permit it."""
    camera, points = parameters.decode(values)
    rotation = Rotation.from_rotvec(camera["world_to_camera"]["rotation_vector"])
    r = rotation.as_matrix()
    t = np.asarray(camera["world_to_camera"]["translation"])
    candidates = []
    eps = 1e-5
    for kind in ("translation", "rotation", "scale"):
        for axis in range(1 if kind == "scale" else 3):
            changed = {}
            q = np.eye(3)
            shift = np.zeros(3)
            factor = 1.0
            if kind == "translation": shift[axis] = eps
            if kind == "rotation":
                rot = np.zeros(3); rot[axis] = eps
                q = Rotation.from_rotvec(rot).as_matrix()
            if kind == "scale": factor += eps
            for ident, point in points.items():
                for i, value in enumerate(factor * (q @ point) + shift):
                    changed[f"landmark.{ident}.{i}"] = value
            new_r = r @ q.T
            new_t = factor * t - new_r @ shift
            if camera["model"] == "orthographic" and kind == "scale":
                changed["camera.scale.0"] = camera["intrinsics"]["scale"] / factor
            for i, value in enumerate(Rotation.from_matrix(new_r).as_rotvec()):
                changed[f"camera.rotation_vector.{i}"] = value
            for i, value in enumerate(new_t):
                changed[f"camera.translation.{i}"] = value
            if any(abs(changed.get(name, fixed) - fixed) > eps * 1e-4 for name, fixed in parameters.fixed.items()):
                continue
            direction = np.asarray([(changed.get(name, values[i]) - values[i]) / eps / parameters.scales[i] for i, name in enumerate(parameters.names)])
            if np.linalg.norm(direction) > 1e-8:
                candidates.append((kind, direction))
    return candidates


def analyze(problem, values):
    p = problem.parameters
    j = jacobian(lambda x: problem.residual(x, evidence_only=True), values, p.scales)
    _, singular, vt = np.linalg.svd(j, full_matrices=True)
    threshold = max(float(singular[0])*problem.case["solver"]["rank_rtol"], 1e-8) if len(singular) else 1e-8
    rank = int(np.sum(singular > threshold))
    nullspace = vt[rank:]
    rigid, scale = [], []
    for kind, direction in _gauge_directions(p, values):
        # Compare to the computed null subspace; a near-gauge is not evidence.
        norm = np.linalg.norm(direction)
        projection = nullspace.T @ (nullspace @ direction) if len(nullspace) else np.zeros(len(values))
        if np.linalg.norm(direction - projection) / norm < 1e-3:
            (scale if kind == "scale" else rigid).append(direction / norm)
    def basis_rank(directions):
        return int(np.linalg.matrix_rank(np.asarray(directions), tol=1e-5)) if directions else 0
    rigid_rank = basis_rank(rigid)
    projection_gauges = []
    # An orthographic camera's axial translation cannot affect any projection.
    # Count only gauge directions independent of the world-frame gauges.
    if problem.case["camera"]["model"] == "orthographic" and "camera.translation.2" in p.slots:
        direction = np.zeros(len(values))
        direction[p.slots["camera.translation.2"]] = 1.0
        projected = nullspace.T @ (nullspace @ direction) if len(nullspace) else np.zeros(len(values))
        if np.linalg.norm(direction - projected) < 1e-3:
            projection_gauges.append(direction)
    gauge_rank = basis_rank(rigid + projection_gauges)
    projection_rank = gauge_rank - rigid_rank
    scale_rank = basis_rank(rigid + projection_gauges + scale) - gauge_rank
    combos = []
    for vector in nullspace:
        leading = sorted(zip(p.names, vector.tolist()), key=lambda pair: abs(pair[1]), reverse=True)[:8]
        combos.append({name: value for name, value in leading if abs(value) > 1e-4})
    report = {"scaled_jacobian_rank": rank, "parameter_count": len(values), "singular_values": singular.tolist(),
              "unresolved_count": len(values)-rank, "world_frame_gauge_count": rigid_rank,
              "projection_gauge_count": projection_rank,
              "missing_scale": bool(scale_rank), "shape_or_camera_unresolved_count": max(0, len(values)-rank-gauge_rank-scale_rank),
              "parameter_combinations": combos, "numerical_gauge_is_evidence": False,
              "scope": "local linear analysis; multistart does not exhaust global solutions"}
    return report, nullspace


def measurements(problem, values, nullspace):
    p = problem.parameters
    _, points = p.decode(values)
    out = []
    for item in problem.case["measurements"]:
        value = measurement(item, points)
        resolved = value is not None
        if resolved and len(nullspace):
            def fun(x):
                v = measurement(item, p.decode(x)[1])
                return [v if v is not None else 1e12]
            gradient = jacobian(fun, values, p.scales)[0]
            resolved = np.linalg.norm(nullspace @ gradient) <= max(1e-7, np.linalg.norm(gradient)*1e-5)
        out.append({"id": item["id"], "type": item["type"], "value": value if resolved else None,
                    "metric_units": problem.case["units"] != "arbitrary" if item["type"] == "distance" else False,
                    "conditional_value": value, "units": "ratio" if item["type"] == "ratio" else problem.case["units"],
                    "status": "resolved" if resolved else "unresolved", "uncertainty": {
                        "available": False, "reason": "observation uncertainty/refits unavailable" if resolved else "measurement is not identifiable",
                        "conditional_on": CONDITIONAL}})
    return out
