from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from modelbench.adapter import _agent_environment
from modelbench.agent_cli import _context
from modelbench.checkpoints import create_checkpoint
from modelbench.errors import StateError, ValidationError
from modelbench.feedback import add_feedback, pending_feedback
from modelbench.orchestrator import continue_run, prepare_new_run, resume_evaluation, run_new
from modelbench.project import ensure_generated_root
from modelbench.reset import CONFIRMATION, reset_all
from modelbench.runs import resolve_run, verify_run_snapshot
from modelbench.state import load_state, save_state
from modelbench.util import atomic_write_json, file_inventory, read_json, sha256_bytes
from tests.helpers import TemporaryProject, fake_render, patched_rendering


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.project = TemporaryProject()
        self.rendering = patched_rendering()

    def tearDown(self):
        self.rendering.close()
        self.project.cleanup()

    def test_successive_runs_promote_without_losing_history(self):
        first = run_new(self.project.root, "example_drawing_object", "fake_test")
        second = run_new(self.project.root, "example_drawing_object", "fake_test")
        self.assertTrue(first["run"].endswith("run_000001"))
        self.assertTrue(second["run"].endswith("run_000002"))
        generated = ensure_generated_root(self.project.root)
        self.assertTrue((generated / "example_drawing_object" / "runs" / "run_000001" / "artifacts" / "model_v000001.blend").is_file())
        self.assertTrue((generated / "example_drawing_object" / "runs" / "run_000002" / "artifacts" / "model_v000002.blend").is_file())
        self.assertFalse((generated / "example_drawing_object" / "runs" / "run_000002" / ".agent-api.json").exists())
        current = read_json(generated / "example_drawing_object" / "current.json")
        self.assertEqual(current["run_id"], "run_000002")
        self.assertEqual(len(current["renders"]), 14)
        self.assertTrue(all(item["path"].endswith(".png") for item in current["renders"]))
        self.assertTrue((generated / "example_drawing_object" / "current" / "renders" / "invocation.json").is_file())

    def test_checkpoint_consumes_feedback_and_honors_pause(self):
        run_dir, _ = prepare_new_run(self.project.root, "example_greybox_level", "fake_test")
        workspace_model = run_dir / "workspace" / "working.blend"
        workspace_model.write_bytes(b"fake blend")
        feedback = add_feedback(run_dir, "Open the central route and fix the spawn clearance")
        control = read_json(run_dir / "control.json")
        control["pause_requested"] = True
        atomic_write_json(run_dir / "control.json", control)
        result = create_checkpoint(self.project.root, run_dir, workspace_model, "blockout", views=["front", "top"])
        self.assertEqual(result["checkpoint"], "checkpoint_0001")
        self.assertEqual(result["feedback_consumed"][0]["id"], feedback["id"])
        self.assertTrue(result["pause_requested"])
        self.assertEqual(pending_feedback(run_dir), [])
        self.assertEqual(load_state(run_dir)["state"], "awaiting_feedback")
        self.assertTrue((run_dir / "checkpoints" / "checkpoint_0001" / "model.blend").is_file())

    def test_checkpoint_render_failure_rolls_back_for_retry(self):
        run_dir, _ = prepare_new_run(self.project.root, "example_drawing_object", "fake_test")
        workspace_model = run_dir / "workspace" / "working.blend"
        workspace_model.write_bytes(b"fake blend")
        with patch("modelbench.checkpoints.run_blender", side_effect=RuntimeError("render failed")):
            with self.assertRaisesRegex(RuntimeError, "render failed"):
                create_checkpoint(self.project.root, run_dir, workspace_model, "blockout", views=["front"])
        self.assertEqual(load_state(run_dir)["state"], "modeling")
        self.assertFalse((run_dir / "checkpoints" / "checkpoint_0001").exists())
        self.assertFalse(list((run_dir / "checkpoints").glob(".checkpoint_0001-attempt-*")))
        result = create_checkpoint(self.project.root, run_dir, workspace_model, "blockout", views=["front"])
        self.assertEqual(result["checkpoint"], "checkpoint_0001")

    def test_checkpoint_result_can_continue_same_run(self):
        with patch.dict(os.environ, {"MODELBENCH_FAKE_STATUS": "checkpoint"}):
            started = run_new(self.project.root, "example_reference_object", "fake_test")
        self.assertEqual(started["state"], "awaiting_feedback")
        with patch.dict(os.environ, {"MODELBENCH_FAKE_STATUS": "submitted"}):
            completed = continue_run(self.project.root, started["run"])
        self.assertEqual(completed["run"], started["run"])
        self.assertEqual(completed["state"], "promoted")
        state = load_state(resolve_run(ensure_generated_root(self.project.root), started["run"]))
        self.assertEqual(state["observed_turns"], 2)

    def test_resume_uses_owned_artifact_without_agent(self):
        result = run_new(self.project.root, "example_drawing_object", "fake_test")
        run_dir = resolve_run(ensure_generated_root(self.project.root), result["run"])
        state = load_state(run_dir)
        state["state"] = "failed"
        state["failure"] = {"stage": "evaluation", "message": "simulated interruption", "details": {}}
        state["publication"] = None
        save_state(run_dir, state)
        shutil.rmtree(run_dir / "renders" / "final")
        publication = resume_evaluation(self.project.root, result["run"])
        self.assertEqual(publication["state"], "promoted")
        self.assertEqual(load_state(run_dir)["observed_turns"], 1)

    def test_resume_reuses_final_renders_after_publication_failure(self):
        with patch("modelbench.orchestrator.promote", side_effect=RuntimeError("publish failed")):
            with self.assertRaisesRegex(RuntimeError, "publish failed"):
                run_new(self.project.root, "example_drawing_object", "fake_test")
        generated = ensure_generated_root(self.project.root)
        run_dir = resolve_run(generated, "example_drawing_object/run_000001")
        self.assertEqual(load_state(run_dir)["state"], "failed")
        self.assertTrue((run_dir / "renders" / "final" / "blender_result.json").is_file())
        with patch("modelbench.orchestrator.run_blender") as rerender:
            publication = resume_evaluation(self.project.root, "example_drawing_object/run_000001")
        rerender.assert_not_called()
        self.assertEqual(publication["state"], "promoted")

    def test_render_interrupt_routes_to_evaluation_resume(self):
        with patch("modelbench.orchestrator.run_blender", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_new(self.project.root, "example_drawing_object", "fake_test")
        generated = ensure_generated_root(self.project.root)
        run_dir = resolve_run(generated, "example_drawing_object/run_000001")
        state = load_state(run_dir)
        self.assertEqual(state["state"], "interrupted")
        self.assertEqual(state["failure"]["stage"], "evaluation")
        with self.assertRaisesRegex(StateError, "must use resume"):
            continue_run(self.project.root, "example_drawing_object/run_000001")
        publication = resume_evaluation(self.project.root, "example_drawing_object/run_000001")
        self.assertEqual(publication["state"], "promoted")

    def test_artifact_mutation_during_render_blocks_publication(self):
        def mutate_artifact(*args, **kwargs):
            result = fake_render(*args, **kwargs)
            Path(args[2]).write_bytes(b"mutated after render")
            return result

        with patch("modelbench.orchestrator.run_blender", side_effect=mutate_artifact):
            with self.assertRaisesRegex(StateError, "hash changed"):
                run_new(self.project.root, "example_drawing_object", "fake_test")
        generated = ensure_generated_root(self.project.root)
        self.assertFalse((generated / "example_drawing_object" / "current.json").exists())

    def test_incomplete_final_render_set_blocks_publication(self):
        def incomplete_render(*args, **kwargs):
            result = fake_render(*args, **kwargs)
            missing = Path(result["renders"].pop()["path"])
            missing.unlink()
            return result

        with patch("modelbench.orchestrator.run_blender", side_effect=incomplete_render):
            with self.assertRaisesRegex(StateError, "exactly the 14"):
                run_new(self.project.root, "example_drawing_object", "fake_test")
        generated = ensure_generated_root(self.project.root)
        self.assertFalse((generated / "example_drawing_object" / "current.json").exists())

    def test_reset_only_removes_generated_data(self):
        before = file_inventory(self.project.root / "tasks")
        run_new(self.project.root, "example_drawing_object", "fake_test")
        dry_run = reset_all(self.project.root, dry_run=True)
        self.assertTrue(dry_run)
        self.assertTrue(all(str(self.project.root / "generated") in path for path in dry_run))
        removed = reset_all(self.project.root, dry_run=False, confirmation=CONFIRMATION)
        self.assertTrue(removed)
        self.assertEqual(file_inventory(self.project.root / "tasks"), before)
        generated = ensure_generated_root(self.project.root)
        self.assertEqual([path.name for path in generated.iterdir()], [".modelbench-generated-root"])

    def test_generated_root_refuses_to_adopt_unmarked_data(self):
        generated = self.project.root / "generated"
        generated.mkdir()
        unrelated = generated / "unrelated.txt"
        unrelated.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "nonempty unmarked"):
            ensure_generated_root(self.project.root)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")
        self.assertFalse((generated / ".modelbench-generated-root").exists())

    def test_agent_api_requires_a_capability_bound_to_the_run(self):
        run_dir, _ = prepare_new_run(self.project.root, "example_drawing_object", "fake_test")
        token = "test capability"
        atomic_write_json(run_dir / ".agent-api.json", {
            "version": 1,
            "run_id": "run_000001",
            "turn": 1,
            "token_sha256": sha256_bytes(token.encode("utf-8")),
        })
        env = {
            "MODELBENCH_AGENT_API": "1",
            "MODELBENCH_AGENT_TOKEN": token,
            "MODELBENCH_ROOT": str(self.project.root),
            "MODELBENCH_RUN_DIR": str(run_dir),
            "MODELBENCH_WORKSPACE": str(run_dir / "workspace"),
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(_context(), (self.project.root, run_dir, run_dir / "workspace"))
        env["MODELBENCH_AGENT_TOKEN"] = "wrong"
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaisesRegex(ValidationError, "capability"):
                _context()

    def test_agent_environment_drops_unrequested_values(self):
        run_dir, profile = prepare_new_run(self.project.root, "example_drawing_object", "fake_test")
        result_path = run_dir / "workspace" / "agent_result.json"
        schema = self.project.root / "schemas" / "agent_result.schema.json"
        with patch.dict(os.environ, {"MODELBENCH_TEST_SECRET": "do not inherit"}, clear=False):
            env = _agent_environment(self.project.root, run_dir, result_path, schema, profile, "token")
        self.assertNotIn("MODELBENCH_TEST_SECRET", env)
        self.assertEqual(env["MODELBENCH_AGENT_TOKEN"], "token")

    def test_snapshot_tampering_blocks_sensitive_operations(self):
        run_dir, _ = prepare_new_run(self.project.root, "example_drawing_object", "fake_test")
        (run_dir / "snapshot" / "blender_driver.py").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(StateError, "snapshot integrity"):
            verify_run_snapshot(run_dir)


if __name__ == "__main__":
    unittest.main()
