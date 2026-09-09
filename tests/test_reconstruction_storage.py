from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modelbench.reconstruction.contracts import ReconstructionError
from modelbench.reconstruction import storage


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Jp0sAAAAASUVORK5CYII=")


def case_for(data: bytes):
    return {"case_version": "2.0", "images": [{"id": "front", "width": 1, "height": 1, "sha256": hashlib.sha256(data).hexdigest()}], "landmarks": [], "observations": [], "camera": {"model": "perspective", "intrinsics": {"focal_length": 1}}}


class StorageTests(unittest.TestCase):
    def test_execution_is_immutable_and_detects_corruption(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "reference.png"; source.write_bytes(PNG)
            with patch.object(storage, "_solver", return_value=lambda _: {"status": "invalid", "receipt": {}}):
                execution = storage.execute(case_for(PNG), root / "run", {"front": source})
            self.assertTrue((execution / "execution.json").is_file())
            with self.assertRaises(ReconstructionError):
                storage.execute(case_for(PNG), execution, {"front": source})
            (execution / "result.json").write_text("{}")
            with self.assertRaisesRegex(ReconstructionError, "identity changed"):
                storage.load_bundle(execution)

    def test_sequence_inputs_allow_duplicate_source_basenames_and_failure_is_atomic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); first = root / "a" / "same.png"; second = root / "b" / "same.png"; first.parent.mkdir(); second.parent.mkdir(); first.write_bytes(PNG); second.write_bytes(PNG)
            case = case_for(PNG)
            with patch.object(storage, "_solver", return_value=lambda _: {"status": "invalid", "receipt": {}}):
                execution = storage.execute(case, root / "run", [first])
            self.assertTrue((execution / "result.json").is_file())
            wrong = dict(case); wrong["images"] = [dict(case["images"][0], width=2)]
            failed = storage.execute(wrong, root / "failed", [first])
            self.assertEqual("invalid", json.loads((failed / "result.json").read_text())["status"])

    def test_replay_reports_unavailable_for_other_interpreter(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "reference.png"; source.write_bytes(PNG)
            with patch.object(storage, "_solver", return_value=lambda _: {"status": "invalid", "receipt": {}}):
                execution = storage.execute(case_for(PNG), root / "run", {"front": source})
            unavailable = storage.replay(execution, root / "new", python_executable=root / "other-python")
            self.assertEqual("unavailable", unavailable["status"])


if __name__ == "__main__":
    unittest.main()
