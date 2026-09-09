from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from modelbench.errors import StateError, ValidationError
from modelbench.models import begin_invocation, change, confirm_invocation, finish_invocation, heartbeat_invocation, initialize, mutation_lock, settings
from modelbench.util import atomic_write_json, read_json


def profile(model="gpt-6-astra", effort="high"):
    return {"model": model, "effort": effort, "allowed_models": {"gpt-6-astra": ["low", "medium", "high", "xhigh", "max", "ultra"], "gpt-5.6-sol": ["medium"]}}


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = Path(self.temp.name) / "run_000001"
        self.run.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_invalid_selection_does_not_mutate_ledger(self):
        initialize(self.run, profile(), profile(), {})
        before = read_json(self.run / "model-settings.json")
        with self.assertRaises(ValidationError):
            change(self.run, "builder", "gpt-5.6-sol", "high")
        self.assertEqual(read_json(self.run / "model-settings.json"), before)

    def test_defaults_and_partial_override_merge_with_project_profile(self):
        allowed_only = {"allowed_models": {"gpt-6-astra": ["high", "medium"]}}
        initial = initialize(self.run, allowed_only, allowed_only, {"builder_model": "gpt-6-astra"})
        self.assertEqual(initial["profiles"]["builder"]["effort"], "high")
        self.assertEqual(initial["profiles"]["verifier"]["model"], "gpt-6-astra")
        self.assertEqual(initial["profiles"]["verifier"]["effort"], "high")

    def test_selected_role_applies_to_next_invocation_only(self):
        initialize(self.run, profile(), profile(), {})
        first = begin_invocation(self.run, "builder", "1.2.3")
        change(self.run, "builder", "gpt-5.6-sol", "medium")
        second = begin_invocation(self.run, "builder")
        self.assertEqual((first["requested_model"], first["requested_effort"]), ("gpt-6-astra", "high"))
        self.assertEqual(first["cli_version"], "1.2.3")
        self.assertEqual((second["requested_model"], second["requested_effort"]), ("gpt-5.6-sol", "medium"))
        with self.assertRaises(ValidationError):
            finish_invocation(self.run, "builder", first["id"], "gpt-5.6-sol", "medium")
        finished = finish_invocation(self.run, "builder", first["id"], "gpt-6-astra", "high", {"input_tokens": 3})
        self.assertEqual(finished["runtime_confirmed"], "Not recorded")
        confirmed = confirm_invocation(self.run, "builder", first["id"], "gpt-6-astra", "high")
        self.assertNotEqual(confirmed["runtime_confirmed"], "Not recorded")

    def test_initial_settings_are_immutable(self):
        initialize(self.run, profile(), profile(), {"verifier": {"model": "gpt-5.6-sol", "effort": "medium"}})
        original = read_json(self.run / "model-settings.initial.json")
        change(self.run, "verifier", "gpt-6-astra", "high")
        self.assertEqual(read_json(self.run / "model-settings.initial.json"), original)

    def test_heartbeat_records_live_pid_and_output_activity(self):
        initialize(self.run, profile(), profile(), {})
        invocation = begin_invocation(self.run, "builder")
        first = heartbeat_invocation(self.run, "builder", invocation["id"], pid=1234, last_output_at="2026-09-09T22:00:00Z")
        second = heartbeat_invocation(self.run, "builder", invocation["id"], pid=1234)
        self.assertEqual(second["pid"], 1234)
        self.assertEqual(second["last_output_at"], first["last_output_at"])
        self.assertGreaterEqual(second["heartbeat_at"], first["heartbeat_at"])
        finish_invocation(self.run, "builder", invocation["id"])
        with self.assertRaises(StateError):
            heartbeat_invocation(self.run, "builder", invocation["id"], pid=1234)

    def test_reentrant_mutation_lock(self):
        with mutation_lock(self.run):
            with mutation_lock(self.run):
                initialize(self.run, profile(), profile(), {})
        self.assertTrue((self.run / "model-settings.json").exists())

    def test_process_lock_waits_for_outer_mutation(self):
        child = (
            "from pathlib import Path\n"
            "import sys\n"
            "from modelbench.models import mutation_lock\n"
            "with mutation_lock(Path(sys.argv[1])):\n"
            "    print('locked', flush=True)\n"
        )
        with mutation_lock(self.run):
            process = subprocess.Popen([sys.executable, "-c", child, str(self.run)], stdout=subprocess.PIPE, text=True)
            time.sleep(0.15)
            self.assertIsNone(process.poll())
        output, _ = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(output.strip(), "locked")
    def test_legacy_settings_mark_runtime_not_recorded(self):
        value = settings(self.run)
        self.assertTrue(value["legacy"])
        self.assertEqual(value["runtime_confirmed"], "Not recorded")

    def test_completed_run_rejects_change(self):
        initialize(self.run, profile(), profile(), {})
        atomic_write_json(self.run / "state.json", {"state": "completed"})
        with self.assertRaises(StateError):
            change(self.run, "both", "gpt-5.6-sol", "medium")


if __name__ == "__main__":
    unittest.main()
