"""Right-handed world to right/down/forward camera with crop-pixel intrinsics."""
from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation

from .coordinates import crop_size


def project(camera, points):
    points = np.asarray(points, dtype=float).reshape((-1, 3))
    ext = camera["world_to_camera"]
    rotation = Rotation.from_rotvec(ext["rotation_vector"]).as_matrix()
    transformed = points @ rotation.T + ext["translation"]
    intr = camera["intrinsics"]
    if camera["model"] == "perspective":
        z = transformed[:, 2]
        safe = np.where(np.abs(z) < 1e-10, np.where(z < 0, -1e-10, 1e-10), z)
        xy = transformed[:, :2] / safe[:, None]
    else:
        xy = transformed[:, :2]
    scale = intr["focal_length"] if camera["model"] == "perspective" else intr["scale"]
    pixels = xy * [scale, scale * intr["aspect_ratio"]] + intr["principal_point"]
    return pixels, transformed[:, 2]


def description(camera, image):
    width, height = crop_size(image)
    return {**camera, "image_id": image["id"], "image_size": [width, height],
            "translation_z_observable": camera["model"] != "orthographic",
            "pixel_convention": "continuous pixel edges; center of first pixel is (0.5, 0.5)",
            "camera_frame": {"handedness": "right", "axes": "+X right, +Y down, +Z forward"},
            "projection": "u=cx+fx*Xc/Zc, v=cy+fy*Yc/Zc" if camera["model"] == "perspective" else "u=cx+sx*Xc, v=cy+sy*Yc",
            "intrinsics": {**camera["intrinsics"], "units": "pixels", "frame": "oriented_crop"},
            "world_to_camera": {**camera["world_to_camera"], "convention": "X_camera = Rodrigues(rotation_vector) @ X_world + translation"}}


class Parameters:
    """Pack only declared unknowns; fixed values and finite bounds stay explicit."""
    def __init__(self, case):
        self.case = case
        self.names, self.initial, self.lower, self.upper = [], [], [], []
        self.slots = {}
        self.fixed = {}
        camera = case["camera"]
        for section, key, size in (("world_to_camera", "rotation_vector", 3), ("world_to_camera", "translation", 3),
                                   ("intrinsics", "principal_point", 2), ("intrinsics", "focal_length" if camera["model"] == "perspective" else "scale", 1)):
            spec = camera[section][key]
            raw = spec.get("value", spec.get("initial"))
            for axis in range(size):
                name = f"camera.{key}.{axis}"
                val = raw[axis] if size > 1 else raw
                if spec["fixed"]:
                    self.fixed[name] = val
                else:
                    lo, hi = spec["bounds"]
                    self.add(name, val, lo[axis] if size > 1 else lo, hi[axis] if size > 1 else hi)
        for point in case["landmarks"]:
            for axis, value in enumerate(point["coordinates"]):
                name = f"landmark.{point['id']}.{axis}"
                if value is None:
                    self.add(name, point["initial"][axis], point["bounds"][0][axis], point["bounds"][1][axis])
                else:
                    self.fixed[name] = value
        self.initial = np.asarray(self.initial)
        self.lower, self.upper = np.asarray(self.lower), np.asarray(self.upper)
        self.scales = np.maximum(1.0, np.abs(self.initial))
        for i, name in enumerate(self.names):
            if "rotation_vector" in name:
                self.scales[i] = 1.0

    def add(self, name, initial, lower, upper):
        self.slots[name] = len(self.initial)
        self.names.append(name)
        self.initial.append(initial)
        self.lower.append(lower)
        self.upper.append(upper)

    def get(self, x, name):
        return float(x[self.slots[name]]) if name in self.slots else self.fixed[name]

    def decode(self, x):
        cam = self.case["camera"]
        vector = lambda key, size: [self.get(x, f"camera.{key}.{i}") for i in range(size)]
        key = "focal_length" if cam["model"] == "perspective" else "scale"
        camera = {"model": cam["model"], "intrinsics": {key: self.get(x, f"camera.{key}.0"),
                  "principal_point": vector("principal_point", 2), "aspect_ratio": cam["intrinsics"]["aspect_ratio"]},
                  "world_to_camera": {"rotation_vector": vector("rotation_vector", 3), "translation": vector("translation", 3)}}
        points = {item["id"]: np.asarray([self.get(x, f"landmark.{item['id']}.{i}") for i in range(3)]) for item in self.case["landmarks"]}
        return camera, points
