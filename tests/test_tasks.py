from __future__ import annotations

import json
import unittest

from modelbench.adapter import AgentResult
from modelbench.config import load_render_profile, load_task
from modelbench.errors import ValidationError
from tests.helpers import TemporaryProject


class TaskContractTests(unittest.TestCase):
    def setUp(self):
        self.project = TemporaryProject()

    def tearDown(self):
        self.project.cleanup()

    def test_all_example_tasks_validate(self):
        tasks = [
            load_task(self.project.root, "example_drawing_object"),
            load_task(self.project.root, "example_reference_object"),
            load_task(self.project.root, "example_greybox_level"),
        ]
        self.assertEqual([task.inputs[0].role for task in tasks], ["drawing", "reference_image", "greybox"])
        self.assertEqual(tasks[2].inputs[0].use, "editable_seed")
        self.assertTrue(tasks[1].research_enabled)

    def test_render_profiles_are_strict(self):
        checkpoint = load_render_profile(self.project.root, "checkpoint_cycles_aces_v1")
        final = load_render_profile(self.project.root, "final_cycles_aces_v1")
        self.assertEqual((checkpoint.data["samples"], checkpoint.data["resolution"]), (64, 512))
        self.assertEqual((final.data["samples"], final.data["resolution"]), (500, 1024))
        self.assertEqual(final.data["view_transform"], "ACES 2.0")
        self.assertEqual(final.data["gpu_name"], "NVIDIA GeForce RTX 3090")

    def test_task_rejects_path_escape(self):
        manifest = self.project.root / "tasks" / "example_reference_object" / "inputs" / "manifest.toml"
        manifest.write_text('version = 1\n[[inputs]]\nid="escape"\nrole="other"\nuse="reference"\npath="../../../../pyproject.toml"\n', encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "escapes"):
            load_task(self.project.root, "example_reference_object")

    def test_json_schemas_parse(self):
        for path in (self.project.root / "schemas").glob("*.json"):
            self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["$schema"].endswith("2020-12/schema"))

    def test_agent_result_runtime_validation_matches_schema_limits(self):
        result = self.project.root / "agent_result.json"
        payload = {
            "status": "submitted",
            "blend_path": "model.blend",
            "phase": "final_candidate",
            "reported_iterations": 1,
            "feedback_requested": False,
            "notes": "",
        }
        result.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(AgentResult.load(result).status, "submitted")
        del payload["notes"]
        result.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "Missing agent result fields"):
            AgentResult.load(result)
        payload["notes"] = ""
        payload["phase"] = "x" * 129
        result.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "1-128"):
            AgentResult.load(result)


if __name__ == "__main__":
    unittest.main()
