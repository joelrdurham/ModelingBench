"""Provenance shown in the viewer must follow the artifact, not later settings."""
import unittest

from modelbench import models, review
from modelbench.adapter import AgentResult, run_adapter
from modelbench.automation import configure
from modelbench.errors import ValidationError
from modelbench.orchestrator import _owned_submission, prepare_new_run
from modelbench.server import ReviewServer
from modelbench.util import atomic_write_json, read_json
from tests.helpers import TemporaryProject


class ViewerProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.project = TemporaryProject()
        self.run, self.builder = prepare_new_run(self.project.root, 'goodyear_optitrac_lsw_1400_30r46', 'fake_test')
        configure(self.project.root, self.run, self.builder, 'fake_test')
        self.server = ReviewServer(self.project.root, 0)
        self.run_name = self.run.parent.parent.name + '/' + self.run.name

    def tearDown(self):
        self.server.server_close()
        self.project.cleanup()

    def test_successful_builder_receipt_stays_bound_after_settings_change(self):
        result = run_adapter(self.project.root, self.run, self.builder)
        self.assertEqual(result.invocation['role'], 'builder')
        artifact = _owned_submission(self.run, result)
        revision = review.register_revision(self.run, artifact, builder_invocation=result.invocation)
        self.assertEqual(revision['builder_provenance']['artifact_sha256'], revision['sha256'])
        ledger = read_json(self.run / 'model-settings.json')
        ledger['profiles']['builder'].update(model='later-model', effort='low')
        atomic_write_json(self.run / 'model-settings.json', ledger)
        displayed = self.server.revision_provenance(self.run_name)[revision['id']]
        self.assertEqual(displayed['builder']['requested_model'], result.invocation['requested_model'])
        self.assertEqual(displayed['builder']['requested_effort'], result.invocation['requested_effort'])
        self.assertIsNone(displayed['builder_context'])
        self.assertEqual(read_json(self.run / 'revisions' / revision['id'] / 'revision.json')['builder_provenance'], revision['builder_provenance'])

    def test_mismatched_receipt_is_only_shown_as_historical_run_context(self):
        result = run_adapter(self.project.root, self.run, self.builder)
        revision = review.register_revision(self.run, _owned_submission(self.run, result), builder_invocation=result.invocation)
        ledger = review.load(self.run)
        ledger['revisions'][0]['builder_provenance']['artifact_sha256'] = 'mismatch'
        review.save(self.run, ledger)
        displayed = self.server.revision_provenance(self.run_name)[revision['id']]
        self.assertIsNone(displayed['builder'])
        self.assertEqual(displayed['builder_context']['scope'], 'run')
        self.assertEqual(displayed['builder_context']['invocation']['id'], result.invocation['id'])

    def test_agent_result_cannot_supply_its_own_provenance(self):
        result = self.run / 'workspace' / 'agent_result.json'
        atomic_write_json(result, {'status': 'submitted', 'blend_path': 'model.blend',
            'phase': 'test', 'reported_iterations': None, 'feedback_requested': False,
            'notes': '', 'invocation': {'requested_model': 'forged'}})
        with self.assertRaisesRegex(ValidationError, 'Unknown agent result fields'):
            AgentResult.load(result)


if __name__ == '__main__':
    unittest.main()
