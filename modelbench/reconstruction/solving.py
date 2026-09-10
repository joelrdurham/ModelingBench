"""Deterministic bounded joint camera/landmark optimization."""
from __future__ import annotations

import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from .constraints import Problem, measurement
from .diagnostics import rectifications
from .projection import Parameters, project, description
from .uncertainty import analyze, measurements, CONDITIONAL


def _signature(parameters, values, remove_scale=False):
    camera, points = parameters.decode(values)
    # Camera-frame geometry is invariant under simultaneous world-frame rigid gauges.
    r = Rotation.from_rotvec(camera["world_to_camera"]["rotation_vector"]).as_matrix()
    transformed = np.asarray(list(points.values())).reshape((-1,3)) @ r.T + camera["world_to_camera"]["translation"]
    if camera["model"] == "orthographic" and len(transformed):
        # Axial camera placement has no orthographic image consequence, but
        # relative landmark depth remains part of the physical hypothesis.
        transformed[:, 2] -= np.mean(transformed[:, 2])
    unit = max(float(np.linalg.norm(transformed)), 1e-12) if remove_scale else 1.0
    transformed /= unit
    intr = camera["intrinsics"]
    focal = intr.get("focal_length", intr.get("scale"))
    if remove_scale and camera["model"] == "orthographic": focal *= unit
    return np.asarray(transformed.ravel().tolist() + [focal] + intr["principal_point"])


def solve_validated(case):
    parameters = Parameters(case)
    problem = Problem(case, parameters)
    settings = case["solver"]
    rng = np.random.default_rng(settings["seed"])
    options = dict(bounds=(parameters.lower, parameters.upper), x_scale=parameters.scales,
                   loss=settings["loss"], f_scale=settings["f_scale"], max_nfev=settings["max_nfev"],
                   ftol=1e-10, xtol=1e-10, gtol=1e-10)
    candidates = []
    if len(parameters.initial) and len(problem.residual(parameters.initial)):
        for index in range(settings["starts"]):
            initial = parameters.initial.copy()
            if index:
                jitter = rng.normal(size=len(initial)) * parameters.scales * (0.25 if index < settings["starts"]//2 else 1.0)
                initial = np.clip(initial+jitter, parameters.lower+1e-9, parameters.upper-1e-9)
            trial = least_squares(problem.residual, initial, **options)
            camera, points = parameters.decode(trial.x)
            _, depths = project(camera, list(points.values()))
            positive = camera["model"] != "perspective" or bool(np.all(depths > 1e-6))
            candidates.append({"values": trial.x, "success": bool(trial.success), "message": trial.message,
                               "nondegenerate": not any(g.get("degenerate") for g in problem.reports(trial.x)),
                               "cost": float(trial.cost), "nfev": int(trial.nfev), "positive_depth": positive})
    else:
        camera, points = parameters.decode(parameters.initial)
        _, depth = project(camera, list(points.values()))
        candidates = [{"values": parameters.initial, "success": True, "message": "no free parameters" if not len(parameters.initial) else "no equations",
                       "cost": float(np.sum(problem.residual(parameters.initial)**2)/2), "nfev": 0,
                       "nondegenerate": not any(g.get("degenerate") for g in problem.reports(parameters.initial)),
                       "positive_depth": camera["model"] != "perspective" or bool(np.all(depth>1e-6))}]
    candidates.sort(key=lambda t: (not t["positive_depth"], not t["success"], not t["nondegenerate"], t["cost"]))
    best = candidates[0]
    selected, signatures = [], []
    remove_scale = analyze(problem, best["values"])[0]["missing_scale"]
    for trial in candidates:
        if not trial["positive_depth"] or not trial["success"] or not trial["nondegenerate"]: continue
        # Retain separate physically valid solutions. The finite starts are not exhaustive.
        sig = _signature(parameters, trial["values"], remove_scale)
        if any(np.linalg.norm((sig-s)/(1+np.abs(s))) < settings["hypothesis_tolerance"] for s in signatures):
            continue
        selected.append(trial); signatures.append(sig)
    if not selected: selected = [best]
    hypotheses = []
    for index, trial in enumerate(selected):
        values = trial["values"]
        ident, nullspace = analyze(problem, values)
        reports = problem.reports(values)
        gauge_reports = problem.reports(values, gauges=True)
        gauge_conflict = any(g["status"] != "satisfied" for g in gauge_reports)
        camera, points = parameters.decode(values)
        pixels, depths = project(camera, [points[o["landmark_id"]] for o in problem.observations])
        ms = measurements(problem, values, nullspace)
        conflict = any(r["status"] != "satisfied" and r["required"] for r in reports)
        if not trial["success"] or gauge_conflict:
            status = "indeterminate"
        elif not trial["positive_depth"]:
            status = "invalid_geometry"
        elif conflict:
            status = "inconsistent"
        elif ident["unresolved_count"] > ident["world_frame_gauge_count"] + ident["projection_gauge_count"] or not (reports or case["planes"]):
            status = "underconstrained"
        else:
            status = "solved"
        valid = trial["positive_depth"] and trial["success"] and not conflict and not gauge_conflict
        active = [name for name, x, lo, hi in zip(parameters.names, values, parameters.lower, parameters.upper) if min(abs(x-lo), abs(hi-x)) < 1e-6*max(1,abs(x))]
        hypothesis = {"id": f"hypothesis_{index+1:03d}", "camera": description(camera, case["images"][0]),
                      "landmarks": {k:v.tolist() for k,v in points.items()}, "measurements": ms,
                      "mathematical_status": status, "convergence_status": "infeasible_gauge" if gauge_conflict else "converged" if trial["success"] else "not_converged",
                      "valid": valid, "positive_depth": trial["positive_depth"], "objective": trial["cost"],
                      "constraints": reports, "numerical_gauges": gauge_reports, "identifiability": ident, "active_bounds": active,
                      "landmark_depths": dict(zip(points, project(camera, list(points.values()))[1].tolist())),
                      "nfev": trial["nfev"], "message": trial["message"], "projections": pixels.tolist(), "depth": depths.tolist(),
                      "rectifications": rectifications(case, camera, points)}
        # Refit uncertainty separately around each valid branch. Missing scale/shape suppresses intervals.
        count = settings["uncertainty_samples"]
        supplied = bool(problem.observations) and all(o["uncertainty_supplied"] for o in problem.observations)
        if count >= 2 and supplied and valid and len(values) and any(m["status"] == "resolved" for m in ms):
            samples = {m["id"]: [] for m in ms}
            sample_rng = np.random.default_rng(settings["seed"] + index*1009 + 65537)
            for _ in range(count):
                perturbed = problem.observed + sample_rng.normal(size=problem.observed.shape)*problem.sigma
                fit = least_squares(lambda x: problem.residual(x, perturbed), values, **options)
                fitcam, fitpoints = parameters.decode(fit.x)
                _, z = project(fitcam, list(fitpoints.values()))
                # A branch jump is rejected rather than blended into a misleading interval.
                signature = _signature(parameters, fit.x, remove_scale)
                branch_distances = [np.linalg.norm((signature-s)/(1+np.abs(s))) for s in signatures]
                near = not branch_distances or int(np.argmin(branch_distances)) == index
                nominal = _signature(parameters, values, remove_scale)
                displacement = np.linalg.norm((signature-nominal)/(1+np.abs(nominal)))
                radius = settings['uncertainty_branch_radius']
                separations = [np.linalg.norm((s-nominal)/(1+np.abs(nominal))) for j,s in enumerate(signatures) if j != index]
                if separations: radius = min(radius, .45*min(separations))
                near = near and displacement <= radius
                feasible = not any(g.get('degenerate') for g in problem.reports(fit.x)) and all(g['status'] == 'satisfied' for g in problem.reports(fit.x, gauges=True))
                if fit.success and near and feasible and (fitcam["model"] != "perspective" or np.all(z>1e-6)):
                    for item in case["measurements"]:
                        v = measurement(item, fitpoints)
                        if v is not None: samples[item["id"]].append(v)
            for item in ms:
                vals = samples[item["id"]]
                if item["status"] == "resolved" and len(vals) >= max(2, int(.8*count)):
                    item["uncertainty"] = {"available": True, "interval": np.quantile(vals,[.025,.975]).tolist(),
                                           "confidence": .95, "samples": len(vals), "requested_samples": count,
                                           "method": "seeded observation perturbation and conditional refitting", "conditional_on": CONDITIONAL}
                elif item['status'] == 'resolved':
                    item['uncertainty']['reason'] = 'Too few converged feasible refits stayed within the local hypothesis radius'
        elif not valid:
            for item in ms:
                item["uncertainty"]["reason"] = "hypothesis is nonconverged, conflicting, or invalid"
        hypotheses.append(hypothesis)
    first = hypotheses[0]
    comparable = [h["id"] for h in hypotheses if h["valid"] and h["objective"] <= first["objective"] + max(1e-6, .05*first["objective"])]
    status = first["mathematical_status"]
    if len(comparable) > 1 and status == "solved": status = "ambiguous"
    rects = first["rectifications"]
    if not problem.observations and not case["landmarks"] and rects and all(r["status"] == "available" for r in rects):
        status = "rectified"
    raw = problem.residual(best["values"], evidence_only=True)
    reason = {"underconstrained": "evidence leaves scale, shape, or camera parameters unresolved", "ambiguous": "distinct comparable camera/geometry hypotheses remain",
              "inconsistent": "required constraints conflict or are degenerate", "indeterminate": "optimization did not converge", "invalid_geometry": "positive-depth validation failed"}.get(status)
    return {"result_version": "2.0", "status": status, "mathematical_status": status,
            "convergence_status": first["convergence_status"], "reason": reason,
            "camera": first["camera"] if status != "rectified" else None, "camera_hypotheses": hypotheses,
            "selected_hypothesis": first["id"], "comparable_hypotheses": comparable,
            "landmarks": first["landmarks"], "measurements": first["measurements"],
            "landmark_depths": first["landmark_depths"], "numerical_gauges": first["numerical_gauges"],
            "constraints": first["constraints"], "conflicts": [r for r in first["constraints"] if r["status"] != "satisfied"],
            "unsupported_constraints": case["unsupported_constraints"], "assumptions": case["assumptions"],
            "unresolved_degrees_of_freedom": first["identifiability"], "rectifications": rects,
            "rectification": rects[0] if rects else None, "projections": first["projections"], "depth": first["depth"],
            "diagnostic_artifacts": [], "residual_summary": {"rms_normalized": float(np.sqrt(np.mean(raw**2))) if len(raw) else None, "count": len(raw)},
            "fit_summary": {"starts": len(candidates), "successful_starts": sum(t["success"] for t in candidates), "bounds_applied": True,
                            "hypotheses": [{"id":h["id"], "objective":h["objective"]} for h in hypotheses]}}
