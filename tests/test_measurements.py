from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modelbench.measurements import evaluate
from modelbench.util import atomic_write_json, sha256_file


class MeasurementTests(unittest.TestCase):
    def test_cache_binds_artifact_task_and_evaluator(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / "run"
            (run / "snapshot").mkdir(parents=True)
            (run / "logs").mkdir()
            artifact = run / "artifact.blend"
            artifact.write_bytes(b"blend")
            atomic_write_json(run / "state.json", {"artifact": {"sha256": sha256_file(artifact)}})
            atomic_write_json(run / "snapshot" / "task.json", {"verification": {}})
            atomic_write_json(run / "run.json", {"render_profiles": {"final": {"data": {}}}})
            (run / "snapshot" / "measure_driver.py").write_text("driver", encoding="utf-8")
            def fake_process(command, **kwargs):
                invocation = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
                atomic_write_json(Path(invocation["result_path"]), {"ok": True, "source_sha256": sha256_file(artifact), "checks": [], "passed": True, "coverage": {}})
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch("modelbench.measurements.verify_run_snapshot"), patch("modelbench.measurements.find_blender", return_value="blender"), patch("modelbench.measurements.subprocess.run", side_effect=fake_process) as process_mock:
                result = evaluate(root, run, artifact, root / "out")
                self.assertEqual(evaluate(root, run, artifact, root / "out"), result)
                self.assertEqual(process_mock.call_count, 1)
                atomic_write_json(run / "snapshot/task.json", {"verification": {"frames": [1]}})
                changed_task = evaluate(root, run, artifact, root / "out")
                (run / "snapshot/measure_driver.py").write_text("changed driver")
                changed_driver = evaluate(root, run, artifact, root / "out")
                self.assertEqual(process_mock.call_count, 3)
                self.assertEqual(len({r["cache_key"] for r in (result, changed_task, changed_driver)}), 3)
                self.assertTrue(all(len(r["cache_key"]) == 64 for r in (result, changed_task, changed_driver)))
            self.assertTrue(result["passed"])
            self.assertEqual(len([p for p in (root / "out").glob("measurement_*.json") if "." not in p.stem]), 3)


if __name__ == "__main__":
    unittest.main()


class BlenderRegressionTests(unittest.TestCase):
    def test_blender_detects_mid_animation_panel_scale_defect(self):
        import json, os, shutil, subprocess
        blender = os.environ.get("MODELBENCH_BLENDER") or shutil.which("blender") or r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"
        if not Path(blender).is_file():
            self.skipTest("Blender is not installed")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); blend = root / "fixture.blend"; invoke = root / "invoke.json"; result = root / "result.json"
            create = "import bpy; bpy.ops.wm.read_factory_settings(use_empty=True); c=bpy.data.collections.new('MODEL'); bpy.context.scene.collection.children.link(c); bpy.ops.mesh.primitive_cube_add(); o=bpy.context.object; o.name='PANEL'; o['claimed_width']=1.0; c.objects.link(o); o.scale=(0.5,0.25,0.01); o.keyframe_insert(data_path='scale',frame=1); o.scale=(1.0,0.25,0.01); o.keyframe_insert(data_path='scale',frame=40); bpy.ops.mesh.primitive_cube_add(location=(0,0,0)); a=bpy.context.object; a.name='PIVOT_A'; c.objects.link(a); bpy.ops.mesh.primitive_cube_add(location=(1,0,0)); b=bpy.context.object; b.name='PIVOT_B'; c.objects.link(b); b.keyframe_insert(data_path='location',frame=1); b.location.x=2; b.keyframe_insert(data_path='location',frame=40); bpy.ops.wm.save_as_mainfile(filepath=%r)" % str(blend)
            subprocess.run([blender, "--background", "--factory-startup", "--disable-autoexec", "--python-expr", create], check=True, capture_output=True, text=True)
            invoke.write_text(json.dumps({"source_blend": str(blend), "result_path": str(result), "task": {"verification": {"frames": [1, 40], "roles": {"lid_panel": "PANEL", "a": "PIVOT_A", "b": "PIVOT_B"}, "dimensions": [{"id": "panel_extent", "kind": "extent", "role": "lid_panel", "axis": 0, "expected": 1.0, "tolerance": 0.02}, {"id": "panel", "kind": "panel_size", "role": "lid_panel", "expected": [1.0, 0.5], "tolerance": 0.02}, {"id": "pivot_distance", "kind": "distance", "from_role": "a", "to_role": "b", "expected": 1.0, "tolerance": 0.01}]}}}), encoding="utf-8")
            payload = json.loads(invoke.read_text(encoding="utf-8"))
            payload["task"]["assembly"] = {
                "components": [{"id": "chassis", "roles": ["a"]}],
                "interfaces": [
                    {"id": "pivot_a_i", "role": "a"},
                    {"id": "pivot_b_i", "role": "b"},
                ],
                "ground_resolution": {
                    "environment_component": "chassis",
                    "interfaces": ["pivot_a_i", "pivot_b_i"],
                    "interface_tolerance": 2.0,
                    "redundant_link_tolerance": 0.01,
                    "prohibited_geometry_patterns": [],
                },
            }
            invoke.write_text(json.dumps(payload), encoding="utf-8")
            driver = Path(__file__).resolve().parents[1] / "modelbench" / "measure_driver.py"
            subprocess.run([blender, "--background", "--factory-startup", "--disable-autoexec", "--python", str(driver), "--", str(invoke)], check=True, capture_output=True, text=True)
            findings = json.loads(result.read_text(encoding="utf-8"))
            self.assertIn("passed", findings, findings)
            self.assertFalse(findings["passed"])
            self.assertEqual([c["status"] for c in findings["checks"] if c["id"] == "panel" and c["frame"] == 40], ["fail"]); self.assertEqual([c["status"] for c in findings["checks"] if c["id"] == "pivot_distance" and c["frame"] == 40], ["fail"])

            self.assertEqual([(c["frame"], c["status"]) for c in findings["checks"] if c["id"] == "panel_extent"], [(1, "pass"), (40, "fail")])
