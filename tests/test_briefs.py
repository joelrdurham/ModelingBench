from __future__ import annotations

import tomllib
import unittest

from modelbench.briefs import create_brief_task
from modelbench.errors import ValidationError
from tests.helpers import TemporaryProject


class BriefTaskTests(unittest.TestCase):
    def setUp(self):
        self.project = TemporaryProject()

    def tearDown(self):
        self.project.cleanup()

    def test_creates_owned_sparse_v2_task(self):
        source = self.project.root / "source.png"
        source.write_bytes(b"reference bytes")
        task_id = create_brief_task(self.project.root, "Make a lantern", "Use the supplied image", [source])
        directory = self.project.root / "generated" / "briefs" / "tasks" / task_id
        config = tomllib.loads((directory / "task.toml").read_text())
        manifest = tomllib.loads((directory / "inputs" / "manifest.toml").read_text())
        self.assertEqual((config["version"], config["specification_mode"], config["research"]["enabled"]), (2, "discovered", True))
        self.assertNotIn("dimensions", config)
        item = manifest["inputs"][0]
        self.assertNotEqual(item["path"], source.name)
        self.assertEqual((directory / "inputs" / item["path"]).read_bytes(), b"reference bytes")

    def test_rejects_linked_or_unsupported_reference(self):
        bad = self.project.root / "bad.exe"; bad.write_bytes(b"x")
        with self.assertRaisesRegex(ValidationError, "unsupported"):
            create_brief_task(self.project.root, "A brief", references=[bad])


if __name__ == "__main__":
    unittest.main()
