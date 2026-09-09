import io
import shutil
import time
import unittest
from unittest.mock import patch

from modelbench import review
from modelbench.automation import configure, render_stage, run_loop
from modelbench.orchestrator import _owned_submission, prepare_new_run, resume_automated
from modelbench.adapter import AgentResult
from modelbench.budgets import BudgetExhausted
from modelbench.blender import run_blender
from modelbench.errors import BlenderError
from modelbench.runs import promote
from modelbench.project import ensure_generated_root
from modelbench.util import atomic_write_json, read_json
from tests.helpers import TemporaryProject, fake_measurements, fake_render, fake_verify


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.project = TemporaryProject()
        self.root = self.project.root
        self.generated = ensure_generated_root(self.root)
        self.run, builder = prepare_new_run(self.root, 'example_drawing_object', 'fake_test')
        configure(self.root, self.run, builder, 'fake_test', budget_seconds=1)

    def tearDown(self):
        self.project.cleanup()

    def candidate(self):
        source = self.run / 'workspace/model.blend'
        source.write_bytes(b'budget candidate')
        artifact = _owned_submission(self.run, AgentResult('submitted', str(source), 'test', None, False, ''))
        revision = review.register_revision(self.run, artifact)
        base = self.run / 'revisions' / revision['id']
        review.record_evaluation(self.run, fake_measurements(self.root, self.run, review.integrity(self.run), base / 'measurements'))
        with patch('modelbench.automation.run_blender', fake_render):
            for kind in ('diagnostics', 'final'):
                render_stage(self.root, self.run, revision, kind)
                review.record_evidence(self.run, base / kind, kind)
        review.record_verification(self.run, fake_verify(self.root, self.run, None, base / 'verification'))

    def test_exhaustion_blocks_publication_and_explicit_grant_resumes(self):
        self.candidate()
        value = review.load(self.run)
        value['budget_used_seconds'] = 1
        review.save(self.run, value)
        result = run_loop(self.root, self.generated, self.run)
        self.assertEqual(result['state'], 'interrupted')
        self.assertFalse((self.generated / 'example_drawing_object' / 'current').exists())
        self.assertEqual(read_json(self.run / 'automation.json')['budget_seconds'], 1)
        result = resume_automated(self.root, f"example_drawing_object/{self.run.name}", additional_budget_seconds=10)
        self.assertEqual(result['state'], 'promoted')
        self.assertEqual(review.load(self.run)['budget_grants'][0]['seconds'], 10)


    def test_expiry_after_packaging_preserves_existing_publication(self):
        self.candidate()
        revision = review.current(self.run)
        shutil.copytree(self.run / 'revisions' / revision['id'] / 'final', self.run / 'renders' / 'final')
        task_root = self.generated / 'example_drawing_object'
        current = task_root / 'current'
        current.mkdir()
        marker = current / 'previous.txt'
        marker.write_text('preserve me')
        (task_root / 'current.json').write_text('{"previous": true}')
        artifact = review.integrity(self.run)
        with patch('modelbench.budgets.require_remaining', side_effect=[None, BudgetExhausted('User budget exhausted')]), \
             patch('modelbench.review.package', wraps=__import__('modelbench.review', fromlist=['package']).package) as packaged:
            with self.assertRaises(BudgetExhausted):
                promote(self.generated, self.run, artifact, expected_artifact_sha256=review.current(self.run)['sha256'], budget_deadline=time.monotonic() + 60)
        self.assertTrue(packaged.called)
        self.assertEqual(marker.read_text(), 'preserve me')
        self.assertEqual((task_root / 'current.json').read_text(), '{"previous": true}')

    def test_builder_budget_exhaustion_terminates_started_process(self):
        from modelbench import adapter
        profile = adapter.AgentProfile('fake', self.run / 'snapshot/agent_profile.toml', {'command': ['fake'], 'limits': {'wall_clock_seconds': 60}}, 'digest')
        class Process:
            pid = 42
            returncode = 1
            stdin = io.StringIO()
            stdout = io.StringIO()
            stderr = io.StringIO()
            def poll(self): return None
            def wait(self, timeout=None): raise AssertionError('budget check should happen before wait')
        process = Process()
        def launch(*args, **kwargs):
            time.sleep(0.6)
            return process
        with patch.object(adapter, 'cli_version', return_value='test'), \
             patch.object(adapter.subprocess, 'Popen', side_effect=launch), \
             patch.object(adapter, 'terminate_process_tree') as terminate:
            with self.assertRaises(BudgetExhausted):
                adapter.run_adapter(self.root, self.run, profile, budget_deadline=time.monotonic() + 0.5)
        terminate.assert_called_once_with(process)

    def test_stage_timeout_with_budget_remaining_is_blender_error(self):
        source = self.run / 'workspace/model.blend'
        source.write_bytes(b'fixture')
        output = self.run / 'budget-timeout-render'
        with patch('modelbench.blender.find_blender', return_value='blender'), \
             patch('modelbench.blender.subprocess.run', side_effect=__import__('subprocess').TimeoutExpired(['blender'], 1)):
            with self.assertRaises(BlenderError):
                run_blender(self.root, self.run, source, output, {}, {}, mode='checkpoint', timeout=1, budget_deadline=time.monotonic() + 60)


if __name__ == '__main__':
    unittest.main()
