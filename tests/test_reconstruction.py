from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from modelbench.reconstruction import compare_receipts, load_bundle, save_bundle, solve
from modelbench.reconstruction.core import ReconstructionError


def _case(points, **extra):
    return {"case_version": "1.0", "image_coordinates": {"space": "normalized", "origin": "top-left", "orientation": "upright"}, "camera": {"model": "perspective", "intrinsics": {"focal_length": 0.8}}, "correspondences": points, "source_hashes": {"reference.png": "abc"}, "solver": {"starts": 4, "max_nfev": 500, "seed": 3}, **extra}


class ReconstructionTests(unittest.TestCase):
    def test_solves_known_perspective_camera_and_records_receipt(self):
        world = [[-1, -1, 0], [1, -1, 0], [-1, 1, 0], [1, 1, 0], [0, 0, 1], [1, 0, 2], [0, 1, 3]]
        points = [{"world": [x, y, z], "image": [0.5 + .8 * x / (z + 5), 0.5 + .8 * y / (z + 5)]} for x, y, z in world]
        result = solve(_case(points))
        self.assertEqual(result["status"], "solved")
        self.assertLess(result["receipt"]["residual"]["rms_normalized"], 1e-5)
        self.assertTrue(result["receipt"]["fit"]["bounds_applied"])
        self.assertEqual(result["camera"]["world_to_camera"]["convention"], "X_camera = Rodrigues(rotation_vector) @ X_world + translation")

    def test_rejects_underconstraint_bad_coordinates_and_unknown_constraint(self):
        self.assertEqual(solve(_case([{"world": [0, 0, 0], "image": [.5, .5]}] * 3))["status"], "underconstrained")
        self.assertEqual(solve(_case([], image_coordinates={"space": "pixels", "origin": "bottom-left"}))["status"], "invalid")
        self.assertEqual(solve(_case([], constraints=[{"type": "occlusion"}]))["status"], "invalid")

    def test_metric_receipt_rectification_and_compare_identity(self):
        points = [{"world": [x, y, z], "image": [.5, .5]} for x, y, z in [[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0], [0, 0, 1], [1, 1, 1]]]
        result = solve(_case(points, planes=[{"id": "front", "rectification": {"plane_points": [[0, 0], [1, 0], [0, 1], [1, 1]], "image_points": [[.1, .2], [.9, .2], [.1, .8], [.9, .8]]}}], constraints=[{"type": "metric_segment", "a": [0, 0, 0], "b": [3, 4, 0], "length": 5}]))
        self.assertEqual(result["rectification"]["plane_id"], "front")
        self.assertIn(0, [item.get("error") for item in result["receipt"]["terms"]])
        other = solve(_case([], source_hashes={"reference.png": "different"}))
        with self.assertRaisesRegex(ReconstructionError, "source_hashes"):
            compare_receipts(result, other)



    def test_full_image_crop_coordinates_are_converted_to_crop_frame(self):
        world = [[-1, -1, 0], [1, -1, 0], [-1, 1, 0], [1, 1, 0], [0, 0, 1], [1, 0, 2]]
        local = [[.5 + .8 * x / (z + 5), .5 + .8 * y / (z + 5)] for x, y, z in world]
        full = [[.25 + .5 * u, .25 + .5 * v] for u, v in local]
        result = solve(_case([{"world": xyz, "image": uv} for xyz, uv in zip(world, full)], image_coordinates={"space": "normalized", "origin": "top-left", "orientation": "upright", "frame": "full_image", "crop": [.25, .25, .5, .5]}))
        self.assertEqual(result["status"], "solved")
        self.assertLess(result["receipt"]["residual"]["rms_normalized"], 1e-5)

    def test_homography_only_case_and_metric_scale_conflict(self):
        rectified = solve(_case([], planes=[{"id": "plane", "rectification": {"plane_points": [[0, 0], [2, 0], [0, 1], [2, 1]], "image_points": [[.1, .2], [.9, .2], [.1, .8], [.9, .8]]}}]))
        self.assertEqual(rectified["status"], "rectified")
        self.assertIsNone(rectified["camera"])
        h = rectified["rectification"]["homography_plane_to_image"]; x, y = 2, 1; d = h[2][0] * x + h[2][1] * y + h[2][2]; self.assertAlmostEqual((h[0][0] * x + h[0][1] * y + h[0][2]) / d, .9, places=6); self.assertAlmostEqual((h[1][0] * x + h[1][1] * y + h[1][2]) / d, .8, places=6)
        conflict = solve(_case([], geometry_scale={"solve": True}, constraints=[{"type": "metric_segment", "a": [0, 0, 0], "b": [1, 0, 0], "length": 2}, {"type": "metric_segment", "a": [0, 0, 0], "b": [0, 1, 0], "length": 3}]))
        self.assertEqual(conflict["status"], "inconsistent")
        self.assertFalse(conflict["receipt"]["geometry_scale"]["consistent"])

    def test_weighted_noisy_points_and_bundle_identity(self):
        world = [[-1, -1, 0], [1, -1, 0], [-1, 1, 0], [1, 1, 0], [0, 0, 1], [1, 0, 2], [0, 1, 3]]
        points = [{"world": [x, y, z], "image": [0.5 + .8 * x / (z + 5), 0.5 + .8 * y / (z + 5)]} for x, y, z in world]
        points[-1] = {**points[-1], "image": [.99, .01], "weight": .001}
        result = solve(_case(points))
        self.assertEqual(result["status"], "solved")
        self.assertTrue(result["receipt"]["fit"]["hypotheses"][0]["camera"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "reference.bin"; source.write_bytes(b"original")
            bundle = save_bundle(_case([]), root / "bundle", [source])
            next((bundle / "images").iterdir()).write_bytes(b"changed")
            with self.assertRaisesRegex(ReconstructionError, "identity changed"):
                load_bundle(bundle)

    def test_coplanar_camera_points_report_ambiguity_as_underconstrained(self):
        points = [{"world": [x, y, 0], "image": [.5 + .1 * x, .5 + .1 * y]} for x, y in [[-1, -1], [1, -1], [-1, 1], [1, 1], [0, 0], [.5, -.5]]]
        result = solve(_case(points))
        self.assertEqual(result["status"], "underconstrained")
        self.assertIn("non-coplanar", result["reason"])

    def test_verifiable_geometry_priors_do_not_create_camera_geometry(self):
        result = solve(_case([], constraints=[{"type": "shared_point", "a": [1, 2, 3], "b": [1, 2, 3]}, {"type": "parallel", "a": [1, 0, 0], "b": [2, 0, 0]}, {"type": "perpendicular", "a": [1, 0, 0], "b": [0, 1, 0]}, {"type": "symmetry", "center": [0, 0, 0], "left": [-1, 0, 0], "right": [1, 0, 0]}, {"type": "repetition", "points": [[0, 0, 0], [1, 0, 0], [2, 0, 0]]}]))
        self.assertEqual(result["status"], "underconstrained")
        self.assertEqual([term["status"] for term in result["receipt"]["terms"]], ["verified"] * 5)
if __name__ == "__main__":
    unittest.main()