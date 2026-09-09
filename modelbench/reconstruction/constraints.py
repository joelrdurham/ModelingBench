"""Observation-weighted residuals and active geometric constraints."""
from __future__ import annotations

import numpy as np

from .coordinates import observation_sigma_pixels, to_crop_pixels
from .projection import project


def measurement(item, points):
    def length(segment):
        a, b = segment
        return float(np.linalg.norm(points[b] - points[a]))
    if item["type"] == "distance":
        return length(item["landmarks"])
    numerator, denominator = item["segments"]
    d = length(denominator)
    return length(numerator) / d if d > 1e-12 else None


class Problem:
    def __init__(self, case, parameters):
        self.case, self.parameters = case, parameters
        self.image = case["images"][0]
        self.observations = case["observations"]
        self.observed = np.asarray([to_crop_pixels(o["image"], space=o["space"], frame=o["frame"], image=self.image) for o in self.observations]).reshape((-1, 2))
        self.sigma = np.asarray([observation_sigma_pixels(o, self.image) for o in self.observations]).reshape((-1, 2))
        self.weights = np.sqrt([o["weight"] for o in self.observations])

    def groups(self, values, observed=None, evidence_only=False):
        camera, points = self.parameters.decode(values)
        observed = self.observed if observed is None else observed
        groups = []
        if self.observations:
            pixels, depth = project(camera, [points[o["landmark_id"]] for o in self.observations])
            for index, item in enumerate(self.observations):
                error = pixels[index] - observed[index]
                groups.append({"id": item["id"], "type": "point_reprojection", "required": item.get("required", True),
                               "raw": error, "units": "pixels", "values": error / self.sigma[index] * self.weights[index]})
        if not evidence_only and camera["model"] == "perspective" and points:
            _, all_depths = project(camera, list(points.values()))
            groups.append({"id": "__positive_depth", "type": "numerical", "raw": all_depths,
                           "values": np.minimum(all_depths - 1e-5, 0) * 1e4})
        for term in self.case["constraints"]:
            kind = term["type"]
            if kind in {"point_reprojection", "curve"} or kind not in {"fixed_coordinate", "distance", "ratio", "coplanarity", "collinearity", "parallelism", "perpendicularity"}:
                continue
            if evidence_only and term.get("role") == "gauge":
                continue
            degenerate = False
            if kind == "fixed_coordinate":
                error = np.asarray([points[term["landmark_id"]][i] - value for i, value in enumerate(term["coordinates"]) if value is not None])
            elif kind == "distance":
                error = np.asarray([measurement(term, points) - term["value"]])
            elif kind == "ratio":
                value = measurement(term, points)
                degenerate = value is None
                error = np.asarray([(value if value is not None else 1e12) - term["value"]])
            elif kind in {"parallelism", "perpendicularity"}:
                (a, b), (c, d) = term["segments"]
                u, v = points[b] - points[a], points[d] - points[c]
                norm = np.linalg.norm(u) * np.linalg.norm(v)
                degenerate = norm < 1e-12
                error = np.cross(u, v) / max(norm, 1e-12) if kind == "parallelism" else np.asarray([np.dot(u, v) / max(norm, 1e-12)])
            else:
                p = [points[i] for i in term["landmarks"]]
                if kind == "coplanarity":
                    normal = np.cross(p[1] - p[0], p[2] - p[0])
                    norm = np.linalg.norm(normal)
                    degenerate = norm < 1e-12
                    error = np.asarray([np.dot(x - p[0], normal) / max(norm, 1e-12) for x in p[3:]])
                else:
                    direction = p[1] - p[0]
                    norm = np.linalg.norm(direction)
                    degenerate = norm < 1e-12
                    error = np.concatenate([np.cross(direction, x - p[0]) / max(norm, 1e-12) for x in p[2:]])
            groups.append({"id": term["id"], "type": kind, "required": term.get("required", True),
                           "role": term.get("role", "evidence"), "degenerate": bool(degenerate),
                           "raw": error, "units": "dimensionless" if kind in {"ratio", "parallelism", "perpendicularity"} else self.case["units"],
                           "values": error * np.sqrt(term["weight"]) / term["tolerance"]})
        return groups

    def residual(self, values, observed=None, evidence_only=False):
        groups = self.groups(values, observed, evidence_only)
        return np.concatenate([g["values"] for g in groups]) if groups else np.zeros(0)

    def reports(self, values, gauges=False):
        settings = self.case["solver"]
        reports = []
        for g in self.groups(values, evidence_only=not gauges):
            if gauges and g.get("role") != "gauge": continue
            peak = float(np.max(np.abs(g["values"])))
            reports.append({"id": g["id"], "type": g["type"], "required": g.get("required", True),
                            "residual": g["raw"].tolist(), "units": g["units"],
                            "normalized_rms": float(np.sqrt(np.mean(g["values"]**2))),
                            "max_abs_normalized": peak, "degenerate": g.get("degenerate", False),
                            "status": "degenerate" if g.get("degenerate") else "conflict" if peak > settings["conflict_threshold"] else "satisfied",
                            "robustly_suppressed": settings["loss"] != "linear" and peak > settings["f_scale"]})
        return sorted(reports, key=lambda g: g["max_abs_normalized"], reverse=True)
