from __future__ import annotations

import math

import pytest

from modelbench.reconstruction import compare_receipts, solve
from modelbench.reconstruction.core import ReconstructionError


def _case(points, **extra):
    return {
        "case_version": "1.0",
        "image_coordinates": {"space": "normalized", "origin": "top-left", "orientation": "upright"},
        "camera": {"model": "perspective", "intrinsics": {"focal_length": 0.8}},
        "correspondences": points,
        "source_hashes": {"reference.png": "abc"},
        "solver": {"starts": 4, "max_nfev": 500, "seed": 3},
        **extra,
    }


def test_solves_known_perspective_camera_and_records_receipt():
    # Camera has identity rotation, translation (0, 0, 5), focal length .8.
    world = [[-1, -1, 0], [1, -1, 0], [-1, 1, 0], [1, 1, 0], [0, 0, 1], [1, 0, 2], [0, 1, 3]]
    points = []
    for x, y, z in world:
        points.append({"world": [x, y, z], "image": [0.5 + .8 * x / (z + 5), 0.5 + .8 * y / (z + 5)]})
    result = solve(_case(points))
    assert result["status"] == "solved"
    assert result["receipt"]["residual"]["rms_normalized"] < 1e-5
    assert result["receipt"]["case_hash"]
    assert result["camera"]["model"] == "perspective"


def test_rejects_insufficient_and_conflicting_coordinate_assumptions():
    result = solve(_case([{"world": [0, 0, 0], "image": [.5, .5]}] * 3))
    assert result["status"] == "underconstrained"
    bad = _case([], image_coordinates={"space": "pixels", "origin": "bottom-left"})
    assert solve(bad)["status"] == "invalid"


def test_constraint_receipts_and_homography_rectification():
    points = [{"world": [x, y, z], "image": [.5, .5]} for x, y, z in [[0,0,0],[1,0,0],[0,1,0],[1,1,0],[0,0,1],[1,1,1]]]
    result = solve(_case(points, planes=[{"id": "front", "rectification": {"plane_points": [[0,0],[1,0],[0,1],[1,1]], "image_points": [[.1,.2],[.9,.2],[.1,.8],[.9,.8]]}}], constraints=[{"type": "metric_segment", "a": [0,0,0], "b": [3,4,0], "length": 5}, {"type": "curve", "samples": []}]))
    assert result["rectification"]["plane_id"] == "front"
    assert any(x.get("error") == 0 for x in result["receipt"]["terms"])


def test_compare_rejects_source_identity_change():
    left = solve(_case([]))
    right = solve(_case([], source_hashes={"reference.png": "different"}))
    with pytest.raises(ReconstructionError, match="source_hashes"):
        compare_receipts(left, right)
