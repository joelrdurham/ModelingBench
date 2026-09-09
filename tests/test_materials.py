from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class MaterialPolicyBlenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blender = os.environ.get("MODELBENCH_BLENDER") or shutil.which("blender") or r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"
        if not Path(cls.blender).is_file():
            raise unittest.SkipTest("Blender is not installed")

    def _run_driver(self, blend: Path, result: Path) -> dict:
        invocation = result.with_suffix(".invoke.json")
        policy = {
            "enabled": True,
            "base_color_linear_rgb": [0.18, 0.18, 0.18],
            "base_color_tolerance": 0.001,
            "default_roughness": 0.36,
            "roughness_tolerance": 0.01,
            "roughness_ranges": {"default": [0.2, 0.8], "rubber": [0.2, 0.8], "glass": [0.1, 0.8]},
        }
        invocation.write_text(json.dumps({"source_blend": str(blend), "result_path": str(result), "task": {"frame": {"bounds_min": [-10, -10, -10], "bounds_max": [10, 10, 10]}, "verification": {"frames": [1], "materials": policy}}}), encoding="utf-8")
        driver = Path(__file__).resolve().parents[1] / "modelbench" / "measure_driver.py"
        subprocess.run([self.blender, "--background", "--factory-startup", "--disable-autoexec", "--python", str(driver), "--", str(invocation)], check=True, capture_output=True, text=True)
        return json.loads(result.read_text(encoding="utf-8"))

    def _save(self, blend: Path, expression: str) -> None:
        load = "bpy.ops.wm.open_mainfile(filepath=" + repr(str(blend)) + ");" if blend.exists() else ""
        subprocess.run([self.blender, "--background", "--factory-startup", "--disable-autoexec", "--python-expr", "import bpy;" + load + expression + ";bpy.ops.wm.save_as_mainfile(filepath=" + repr(str(blend)) + ")"], check=True, capture_output=True, text=True)

    def test_actual_shader_values_override_matching_metadata_and_rubber_range_is_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            blend = root / "fixture.blend"
            create = "import bpy;bpy.ops.wm.read_factory_settings(use_empty=True);c=bpy.data.collections.new('MODEL');bpy.context.scene.collection.children.link(c);bpy.ops.mesh.primitive_cube_add();o=bpy.context.object;c.objects.link(o);o.name='BODY';m=bpy.data.materials.new('Neutral');m.use_nodes=True;n=m.node_tree.nodes.get('Principled BSDF');n.inputs['Base Color'].default_value=(0.20,0.18,0.18,1);n.inputs['Roughness'].default_value=0.36;m['claimed_base_color']=[0.18,0.18,0.18];o.data.materials.append(m)"
            self._save(blend, create)
            failed = self._run_driver(blend, root / "failed.json")
            self.assertFalse(failed["passed"])
            self.assertEqual(failed["coverage"]["materials"]["status"], "assessed")
            check = next(c for c in failed["checks"] if c["id"].startswith("material_"))
            self.assertEqual(check["status"], "fail")
            self.assertAlmostEqual(check["actual"]["base_color"][0], 0.2, places=6)
            self.assertAlmostEqual(check["actual"]["base_color"][1], 0.18, places=6)
            self.assertAlmostEqual(check["actual"]["base_color"][2], 0.18, places=6)

            self._save(blend, "import bpy;n=bpy.data.materials['Neutral'].node_tree.nodes.get('Principled BSDF');n.inputs['Base Color'].default_value=(0.18,0.18,0.18,1)")
            corrected = self._run_driver(blend, root / "corrected.json")
            self.assertTrue(corrected["passed"], corrected)

            self._save(blend, "import bpy;m=bpy.data.materials['Neutral'];m.name='Rubber_Tire';n=m.node_tree.nodes.get('Principled BSDF');n.inputs['Roughness'].default_value=0.65")
            rubber = self._run_driver(blend, root / "rubber.json")
            self.assertTrue(rubber["passed"])
            check = next(c for c in rubber["checks"] if c["id"].startswith("material_"))
            self.assertEqual(check["actual"]["classification"], "rubber")
            self.assertEqual(check["expected"]["roughness_range"], [0.2, 0.8])

            self._save(blend, "import bpy;m=bpy.data.materials['Rubber_Tire'];m.name='Neutral';n=m.node_tree.nodes.get('Principled BSDF');n.inputs['Roughness'].default_value=0.25")
            varied = self._run_driver(blend, root / "varied.json")
            self.assertTrue(varied["passed"], varied)

            self._save(blend, "import bpy;m=bpy.data.materials['Neutral'];n=m.node_tree.nodes.get('Principled BSDF');n.inputs['Roughness'].default_value=0.5")
            untouched_default = self._run_driver(blend, root / "untouched_default.json")
            self.assertFalse(untouched_default["passed"])
            self._save(blend, "import bpy;m=bpy.data.materials['Neutral'];m['modelbench_roughness_override']=True")
            intentional_half = self._run_driver(blend, root / "intentional_half.json")
            self.assertTrue(intentional_half["passed"], intentional_half)

            override = "import bpy;o=bpy.data.objects['BODY'];m=bpy.data.materials.new('Object_Override');m.use_nodes=True;n=m.node_tree.nodes.get('Principled BSDF');n.inputs['Base Color'].default_value=(0.2,0.18,0.18,1);n.inputs['Roughness'].default_value=0.36;o.material_slots[0].link='OBJECT';o.material_slots[0].material=m"
            self._save(blend, override)
            overridden = self._run_driver(blend, root / "overridden.json")
            self.assertFalse(overridden["passed"])
            check = next(c for c in overridden["checks"] if c["id"].startswith("material_"))
            self.assertEqual(check["actual"]["base_color"][0], 0.20000000298023224)

    def test_render_neutralizer_preserves_allowed_roughness_and_sets_background(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            probe = root / "probe.py"
            result = root / "result.json"
            driver = Path(__file__).resolve().parents[1] / "modelbench" / "blender_driver.py"
            probe.write_text(
                """
import importlib.util, json, sys
from pathlib import Path
import bpy

driver_path = Path(sys.argv[sys.argv.index('--') + 1])
result_path = Path(sys.argv[sys.argv.index('--') + 2])
spec = importlib.util.spec_from_file_location('modelbench_blender_driver', driver_path)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
bpy.ops.wm.read_factory_settings(use_empty=True)
collection = bpy.data.collections.new('MODEL')
bpy.context.scene.collection.children.link(collection)
mesh = bpy.data.meshes.new('UnassignedMesh')
unassigned = bpy.data.objects.new('Unassigned', mesh)
collection.objects.link(unassigned)

def material(name, roughness, override=False, transmission=0.0):
    value = bpy.data.materials.new(name)
    value.use_nodes = True
    shader = value.node_tree.nodes.get('Principled BSDF')
    shader.inputs['Base Color'].default_value = (0.8, 0.1, 0.05, 1.0)
    shader.inputs['Roughness'].default_value = roughness
    shader.inputs['Metallic'].default_value = 1.0
    transmission_input = shader.inputs.get('Transmission Weight') or shader.inputs.get('Transmission')
    if transmission_input is not None:
        transmission_input.default_value = transmission
    if override:
        value['modelbench_roughness_override'] = True
    return value

material('Stone', 0.25)
material('UntouchedHalf', 0.5)
material('IntentionalHalf', 0.5, override=True)
material('Glass', 0.1, transmission=1.0)
profile = {
    'base_color': [0.18, 0.18, 0.18, 1.0],
    'roughness_default': 0.36,
    'roughness_min': 0.2,
    'roughness_max': 0.8,
    'glass_roughness': 0.1,
    'background_color': [0.12, 0.12, 0.12, 1.0],
    'background_strength': 1.0,
}
records = driver._neutralize(collection, profile)
background = bpy.context.scene.world.node_tree.nodes['Background']
result_path.write_text(json.dumps({
    'records': records,
    'assigned_default': unassigned.data.materials[0].name,
    'background_color': list(background.inputs['Color'].default_value),
    'background_strength': background.inputs['Strength'].default_value,
}), encoding='utf-8')
""",
                encoding="utf-8",
            )
            subprocess.run(
                [self.blender, "--background", "--factory-startup", "--disable-autoexec", "--python", str(probe), "--", str(driver), str(result)],
                check=True,
                capture_output=True,
                text=True,
            )
            actual = json.loads(result.read_text(encoding="utf-8"))
            records = {record["name"]: record for record in actual["records"]}
            self.assertAlmostEqual(records["Stone"]["roughness"], 0.25)
            self.assertAlmostEqual(records["UntouchedHalf"]["roughness"], 0.36)
            self.assertAlmostEqual(records["IntentionalHalf"]["roughness"], 0.5)
            self.assertAlmostEqual(records["Glass"]["roughness"], 0.1)
            self.assertEqual(records["Stone"]["base_color"], [0.18, 0.18, 0.18, 1.0])
            self.assertEqual(records["Stone"]["metallic"], 0.0)
            self.assertTrue(actual["assigned_default"].startswith("MB_NEUTRAL_GRAY_R0.360"))
            for value, expected in zip(actual["background_color"], [0.12, 0.12, 0.12, 1.0]):
                self.assertAlmostEqual(value, expected)
            self.assertAlmostEqual(actual["background_strength"], 1.0)


if __name__ == "__main__":
    unittest.main()
