"""Invertible image orientation and crop coordinate transforms."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import ReconstructionError


def crop_size(image):
    width, height = oriented_size(image)
    crop = image.get("crop", [0, 0, 1, 1])
    return width * crop[2], height * crop[3]


def to_crop_pixels(point, *, space, frame, image):
    normalized = to_crop_normalized(point, space=space, frame=frame, image=image)
    return [x * size for x, size in zip(normalized, crop_size(image))]


def from_crop_pixels(point, *, space, frame, image):
    return from_crop_normalized([x / size for x, size in zip(point, crop_size(image))],
                                space=space, frame=frame, image=image)


def observation_sigma_pixels(observation, image):
    """Transport diagonal observation covariance through the affine image transform."""
    import numpy as np
    kwargs = {"space": observation["space"], "frame": observation["frame"], "image": image}
    origin = np.asarray(to_crop_pixels([0, 0], **kwargs))
    matrix = np.column_stack([np.asarray(to_crop_pixels(p, **kwargs)) - origin for p in ([1, 0], [0, 1])])
    covariance = matrix @ np.diag(np.square(observation["sigma"])) @ matrix.T
    return np.sqrt(np.diag(covariance))


def oriented_size(image: Mapping[str, Any]) -> tuple[int, int]:
    width, height = int(image["width"]), int(image["height"])
    return (height, width) if image.get("orientation") in {"rotate_90_cw", "rotate_270_cw"} else (width, height)


def _source_to_oriented(point: tuple[float, float], image: Mapping[str, Any]) -> tuple[float, float]:
    x, y = point
    width, height = float(image["width"]), float(image["height"])
    orientation = image.get("orientation", "upright")
    if orientation == "upright":
        return x, y
    if orientation == "rotate_90_cw":
        return height - y, x
    if orientation == "rotate_180":
        return width - x, height - y
    if orientation == "rotate_270_cw":
        return y, width - x
    raise ReconstructionError(f"unsupported orientation {orientation!r}")


def _oriented_to_source(point: tuple[float, float], image: Mapping[str, Any]) -> tuple[float, float]:
    x, y = point
    width, height = float(image["width"]), float(image["height"])
    orientation = image.get("orientation", "upright")
    if orientation == "upright":
        return x, y
    if orientation == "rotate_90_cw":
        return y, height - x
    if orientation == "rotate_180":
        return width - x, height - y
    if orientation == "rotate_270_cw":
        return width - y, x
    raise ReconstructionError(f"unsupported orientation {orientation!r}")


def to_crop_normalized(point: list[float], *, space: str, frame: str, image: Mapping[str, Any]) -> list[float]:
    """Map a point from its declared frame to normalized post-orientation crop coordinates."""
    ow, oh = oriented_size(image)
    x, y = float(point[0]), float(point[1])
    if space == "normalized":
        fw, fh = (image["width"], image["height"]) if frame == "source" else (ow, oh)
        x, y = x * fw, y * fh
    if frame == "source":
        x, y = _source_to_oriented((x, y), image)
    elif frame == "crop":
        crop = image["crop"]
        return [x / (ow * crop[2]), y / (oh * crop[3])] if space == "pixels" else [float(point[0]), float(point[1])]
    elif frame != "oriented":
        raise ReconstructionError(f"unsupported coordinate frame {frame!r}")
    left, top, width, height = image["crop"]
    return [(x / ow - left) / width, (y / oh - top) / height]


def from_crop_normalized(point: list[float], *, space: str, frame: str, image: Mapping[str, Any]) -> list[float]:
    """Inverse of :func:`to_crop_normalized`."""
    ow, oh = oriented_size(image)
    left, top, width, height = image["crop"]
    if frame == "crop":
        if space == "normalized":
            return [float(point[0]), float(point[1])]
        return [point[0] * ow * width, point[1] * oh * height]
    x, y = (left + point[0] * width) * ow, (top + point[1] * height) * oh
    if frame == "source":
        x, y = _oriented_to_source((x, y), image)
        fw, fh = image["width"], image["height"]
    elif frame == "oriented":
        fw, fh = ow, oh
    else:
        raise ReconstructionError(f"unsupported coordinate frame {frame!r}")
    return [x / fw, y / fh] if space == "normalized" else [x, y]
