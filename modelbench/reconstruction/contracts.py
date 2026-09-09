"""Detached JSON validation for the single-view v2 contract. No filesystem I/O."""
from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping

CASE_VERSION = "2.0"
ENGINE_VERSION = "2.0.0"
SUPPORTED = {"point_reprojection", "fixed_coordinate", "distance", "ratio",
             "coplanarity", "collinearity", "parallelism", "perpendicularity"}
ALIASES = {"point": "point_reprojection", "parallel": "parallelism",
           "perpendicular": "perpendicularity", "metric_segment": "distance"}
DEFAULT_SETTINGS = {"seed": 0, "starts": 8, "max_nfev": 2000, "loss": "linear",
                    "f_scale": 1.0, "conflict_threshold": 3.0, "rank_rtol": 1e-7,
                    "hypothesis_tolerance": 1e-4, "uncertainty_samples": 0}


class ReconstructionError(ValueError):
    """Invalid case or unverifiable execution evidence."""


def finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ReconstructionError(f"{name} must be a finite number")
    return float(value)


def positive(value, name):
    value = finite(value, name)
    if value <= 0:
        raise ReconstructionError(f"{name} must be positive")
    return value


def vector(value, size, name, allow_none=False):
    if not isinstance(value, list) or len(value) != size:
        raise ReconstructionError(f"{name} requires {size} values")
    return [None if allow_none and x is None else finite(x, name) for x in value]


def records(value, name):
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise ReconstructionError(f"{name} must be an array of objects")
    ids = []
    for item in value:
        ident = item.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", ident):
            raise ReconstructionError(f"{name} requires stable IDs (letters, digits, _, ., :, -)")
        ids.append(ident)
    if len(set(ids)) != len(ids):
        raise ReconstructionError(f"duplicate {name} ID")
    return value


def parameter(value, name, size=None, positive_only=False):
    """Numbers/vectors are fixed. Unknowns require initial and finite bounds."""
    check = (lambda x: vector(x, size, name)) if size else (lambda x: finite(x, name))
    if not isinstance(value, dict):
        raw = check(value)
        out = {"value": raw, "fixed": True}
    elif value.get("fixed", "value" in value):
        out = {"value": check(value.get("value", value.get("initial"))), "fixed": True}
    else:
        raw = check(value.get("initial"))
        bounds = value.get("bounds")
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise ReconstructionError(f"{name} unknown parameter requires [lower, upper] bounds")
        lo, hi = check(bounds[0]), check(bounds[1])
        triples = zip(lo, raw, hi) if size else [(lo, raw, hi)]
        if any(not a < b or not a <= x <= b for a, x, b in triples):
            raise ReconstructionError(f"{name} bounds must increase and contain initial")
        out = {"initial": raw, "bounds": [lo, hi], "fixed": False}
    vals = out.get("value", out.get("initial"))
    if positive_only:
        if any(x <= 0 for x in (vals if size else [vals])):
            raise ReconstructionError(f"{name} must be positive")
        if not out["fixed"] and any(x <= 0 for x in (out["bounds"][0] if size else [out["bounds"][0]])):
            raise ReconstructionError(f"{name} bounds must be positive")
    return out


def validate_case(case):
    if not isinstance(case, Mapping) or case.get("case_version") != CASE_VERSION:
        raise ReconstructionError("case must be an object with case_version '2.0'")
    data = copy.deepcopy(dict(case))
    images = records(data.get("images"), "images")
    if len(images) != 1:
        raise ReconstructionError("v2.0 supports exactly one image per case (joint multi-view is unsupported)")
    im = images[0]
    for key in ("width", "height"):
        if type(im.get(key)) is not int or not 0 < im[key] <= 100000:
            raise ReconstructionError(f"image.{key} must be a positive integer <= 100000")
    digest = im.get("sha256")
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest)):
        raise ReconstructionError("image.sha256 must be a lowercase SHA256 digest")
    im.setdefault("orientation", "upright")
    if im["orientation"] not in {"upright", "rotate_90_cw", "rotate_180", "rotate_270_cw"}:
        raise ReconstructionError("unsupported image orientation")
    im["crop"] = vector(im.get("crop", [0, 0, 1, 1]), 4, "image.crop")
    x, y, w, h = im["crop"]
    if min(x, y) < 0 or min(w, h) <= 0 or x + w > 1 or y + h > 1:
        raise ReconstructionError("image.crop must be within the oriented image")
    from .coordinates import crop_size
    width, height = crop_size(im)
    data.setdefault("units", "arbitrary")
    if not isinstance(data["units"], str) or not data["units"].strip():
        raise ReconstructionError("units must be a nonempty string")
    data.setdefault("world_frame", {"handedness": "right"})
    if not isinstance(data["world_frame"], dict) or data["world_frame"].get("handedness") != "right":
        raise ReconstructionError("world_frame must declare right handedness")
    records(data.setdefault("assumptions", []), "assumptions")
    assumption_ids = {x["id"] for x in data["assumptions"]}
    landmarks = records(data.setdefault("landmarks", []), "landmarks")
    lids = {x["id"] for x in landmarks}
    for item in landmarks:
        item["coordinates"] = vector(item.get("coordinates", item.get("world", [None]*3)), 3, "landmark.coordinates", True)
        item["initial"] = vector(item.get("initial", [0 if x is None else x for x in item["coordinates"]]), 3, "landmark.initial")
        bounds = item.get("bounds", [[-1e6]*3, [1e6]*3])
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise ReconstructionError("landmark.bounds must be [lower, upper]")
        lo, hi = vector(bounds[0], 3, "landmark.bounds"), vector(bounds[1], 3, "landmark.bounds")
        if any(not a < b or not a <= (v if v is not None else i) <= b for a, b, v, i in zip(lo, hi, item["coordinates"], item["initial"])):
            raise ReconstructionError("landmark bounds must increase and contain coordinates/initial")
        item["bounds"] = [lo, hi]

    def refs(ids, count=None):
        if not isinstance(ids, list) or (count is not None and len(ids) != count) or any(not isinstance(i, str) or i not in lids for i in ids):
            raise ReconstructionError("constraint/measurement references unknown or malformed landmarks")
        if len(set(ids)) < min(len(ids), 2):
            raise ReconstructionError("segment endpoints must differ")
        return ids

    def segment(item):
        return refs(item.get("landmarks", [item.get("a"), item.get("b")]), 2)

    def segments(item):
        raw = item.get("segments", [[item.get("a"), item.get("b")], [item.get("c"), item.get("d")]])
        if not isinstance(raw, list) or len(raw) != 2:
            raise ReconstructionError("two segments are required")
        return [refs(x, 2) for x in raw]

    terms = records(data.setdefault("constraints", []), "constraints")
    observations = records(data.get("observations", data.get("correspondences", [])), "observations")
    data["observations"] = observations
    unsupported = []
    for term in terms:
        term["type"] = ALIASES.get(term.get("type"), term.get("type"))
        kind = term["type"]
        if type(term.get("required", True)) is not bool:
            raise ReconstructionError("constraint.required must be boolean")
        if kind == "curve":
            for sample in records(term.get("samples"), "curve samples"):
                observations.append({**sample, "constraint_id": term["id"]})
            continue
        if kind not in SUPPORTED:
            if term.get("required", True):
                raise ReconstructionError(f"unsupported required constraint {term['id']}: {kind}")
            unsupported.append({"id": term["id"], "type": kind, "status": "unsupported"})
            continue
        if kind == "point_reprojection":
            observations.append({**term, "constraint_id": term["id"]})
            continue
        term["tolerance"] = positive(term.get("tolerance", term.get("sigma", 1e-6)), "constraint.tolerance")
        term["weight"] = positive(term.get("weight", 1), "constraint.weight")
        if term.get("role", "evidence") not in {"evidence", "gauge"}:
            raise ReconstructionError("constraint.role must be evidence or gauge")
        if term.get("role") == "gauge" and kind != "fixed_coordinate":
            raise ReconstructionError("only fixed_coordinate constraints can numerically fix a gauge")
        if kind == "fixed_coordinate":
            refs([term.get("landmark_id")], 1)
            term["coordinates"] = vector(term.get("coordinates"), 3, "fixed_coordinate.coordinates", True)
            if all(v is None for v in term["coordinates"]):
                raise ReconstructionError("fixed_coordinate requires at least one component")
        elif kind == "distance":
            term["landmarks"] = segment(term)
            term["value"] = positive(term.get("value", term.get("length")), "distance.value")
        elif kind in {"ratio", "parallelism", "perpendicularity"}:
            term["segments"] = segments(term)
            if kind == "ratio":
                term["value"] = positive(term.get("value"), "ratio.value")
        else:
            refs(term.get("landmarks"))
            if len(term["landmarks"]) < (4 if kind == "coplanarity" else 3):
                raise ReconstructionError(f"{kind} requires more landmarks")
        if any(a not in assumption_ids for a in term.get("assumption_ids", [])):
            raise ReconstructionError("constraint references unknown assumption")
    records(observations, "observations")
    for item in observations:
        refs([item.get("landmark_id")], 1)
        item.setdefault("image_id", im["id"])
        if item["image_id"] != im["id"]:
            raise ReconstructionError("observation references unknown image")
        item["image"] = vector(item.get("image"), 2, "observation.image")
        item.setdefault("space", "pixels")
        item.setdefault("frame", "source")
        if item["space"] not in {"pixels", "normalized"} or item["frame"] not in {"source", "oriented", "crop"}:
            raise ReconstructionError("unsupported observation coordinates")
        item["uncertainty_supplied"] = item.get("uncertainty_supplied", "sigma" in item)
        if "sigma" not in item and "tolerance" not in item:
            raise ReconstructionError("observations require explicit sigma or tolerance")
        sigma = item.get("sigma", item.get("tolerance"))
        item["sigma"] = vector(sigma, 2, "observation.sigma") if isinstance(sigma, list) else [positive(sigma, "observation.sigma")]*2
        if min(item["sigma"]) <= 0:
            raise ReconstructionError("observation.sigma must be positive")
        item["weight"] = positive(item.get("weight", 1), "observation.weight")

    camera = data.get("camera")
    if not isinstance(camera, dict) or camera.get("model") not in {"perspective", "orthographic"}:
        raise ReconstructionError("camera.model must be perspective or orthographic")
    intr = camera.setdefault("intrinsics", {})
    ext = camera.setdefault("world_to_camera", {})
    if not isinstance(intr, dict) or not isinstance(ext, dict):
        raise ReconstructionError("camera intrinsics/extrinsics must be objects")
    if intr.get("units", "pixels") != "pixels":
        raise ReconstructionError("v2 intrinsics must use pixels in the oriented crop")
    intr["units"] = "pixels"
    key = "focal_length" if camera["model"] == "perspective" else "scale"
    intr[key] = parameter(intr.get(key), f"intrinsics.{key}", positive_only=True)
    intr["principal_point"] = parameter(intr.get("principal_point", [width/2, height/2]), "principal_point", 2)
    intr["aspect_ratio"] = positive(intr.get("aspect_ratio", 1), "intrinsics.aspect_ratio")
    for key, initial, bounds in (("rotation_vector", [0,0,0], [[-2*math.pi]*3,[2*math.pi]*3]), ("translation", [0,0,5], [[-1e6]*3,[1e6]*3])):
        ext[key] = parameter(ext.get(key, {"initial": initial, "bounds": bounds}), key, 3)
    measurements = records(data.setdefault("measurements", data.pop("requested_measurements", [])), "measurements")
    for item in measurements:
        item.setdefault("type", "distance")
        if item["type"] == "distance":
            item["landmarks"] = segment(item)
        elif item["type"] == "ratio":
            item["segments"] = segments(item)
        else:
            raise ReconstructionError("only distance and ratio measurements are supported")
        if item.get("units", data["units"]) != data["units"] and item["type"] != "ratio":
            raise ReconstructionError("measurement units must equal case units; convert explicitly")
    for plane in records(data.setdefault("planes", []), "planes"):
        r = plane.get("rectification", plane)
        if "plane_points" in r:
            if not isinstance(r["plane_points"], list) or len(r["plane_points"]) < 4 or len(r.get("image_points", [])) != len(r["plane_points"]):
                raise ReconstructionError("plane rectification requires >=4 paired 2D points")
            r["plane_points"] = [vector(x, 2, "plane_points") for x in r["plane_points"]]
            r["image_points"] = [vector(x, 2, "image_points") for x in r["image_points"]]
            if r.get("space", "pixels") not in {"pixels", "normalized"} or r.get("frame", "crop") not in {"source", "oriented", "crop"}:
                raise ReconstructionError("unsupported plane coordinates")
        else:
            refs(plane.get("landmarks"))
            if len(plane["landmarks"]) < 4:
                raise ReconstructionError("plane requires >=4 landmarks")
        plane["output_scale"] = positive(plane.get("output_scale", 100), "plane.output_scale")
    settings = data.get("solver", {})
    if not isinstance(settings, dict) or set(settings) - DEFAULT_SETTINGS.keys():
        raise ReconstructionError("unsupported solver settings")
    settings = {**DEFAULT_SETTINGS, **settings}
    for key, lo, hi in (("seed",0,2**32-1),("starts",1,64),("max_nfev",1,100000),("uncertainty_samples",0,200)):
        if type(settings[key]) is not int or not lo <= settings[key] <= hi:
            raise ReconstructionError(f"solver.{key} must be an integer in [{lo}, {hi}]")
    for key in ("f_scale", "conflict_threshold", "rank_rtol", "hypothesis_tolerance"):
        settings[key] = positive(settings[key], f"solver.{key}")
    if settings["loss"] not in {"linear", "soft_l1", "huber", "cauchy", "arctan"}:
        raise ReconstructionError("unsupported robust loss")
    data["solver"] = settings
    data["unsupported_constraints"] = unsupported
    return data
