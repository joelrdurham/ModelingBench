"""Failure-oriented contracts and lifecycle tests for discovered asset requirements."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from modelbench import discovery, review
from modelbench.briefs import create_brief_task
from modelbench.config import load_task
from modelbench.errors import ValidationError, StateError
from modelbench.orchestrator import prepare_new_run
from modelbench.automation import configure
from modelbench.util import atomic_write_json, sha256_file
from tests.helpers import TemporaryProject


def fixture_spec(evidence='ev_0001'):
    return {'version': 1, 'target': {'identity': 'Synthetic asymmetric block', 'scope': 'Static block', 'status': 'established'},
        'entities': [{'id': 'body', 'name': 'Body', 'modeled': True, 'representation': 'solid', 'representation_reason': 'Closed reference object', 'patterns': ['BODY']}],
        'claims': [{'id': 'width', 'kind': 'sourced', 'statement': 'Width is 2 meters', 'entity_ids': ['body'], 'evidence_ids': [evidence], 'source_location': 'fixture line 1'}],
        'relationships': [], 'requirements': [{'id': 'width', 'criterion': 'Correct overall X extent', 'entity_ids': ['body'], 'claim_ids': ['width'], 'critical': True, 'method': 'extent', 'acceptance': '2 m plus or minus 0.01 m', 'tolerance_reason': 'Synthetic fixture accuracy', 'check': {'axis': 0, 'expected': 2., 'tolerance': .01}}], 'references': []}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.project = TemporaryProject(); self.root = self.project.root
        source = self.root / 'fixture.txt'; source.write_text('Width is 2 meters')
        ident = create_brief_task(self.root, 'Synthetic asymmetric block', references=[source], research_mode='offline')
        self.task_id = ident
        self.run, builder = prepare_new_run(self.root, ident, 'fake_test')
        configure(self.root, self.run, builder, 'fake_test')
        self.spec = fixture_spec()
    def tearDown(self): self.project.cleanup()
    def approve(self, spec=None):
        spec = spec or self.spec
        path = self.run / 'proposal.json'; atomic_write_json(path, spec)
        audit = {'decision': 'APPROVE', 'specification_sha256': sha256_file(path), 'reviewed_requirements': [r['id'] for r in spec['requirements']], 'coverage_complete': True, 'identity_supported': True, 'inferences_reviewed': True, 'tolerances_reviewed': True, 'assessment': 'Independent fixture audit'}
        return discovery.approve(self.run, spec, audit, path)
    def test_sparse_task_no_predefined_dimensions(self):
        task = load_task(self.root, self.task_id)
        self.assertTrue(task.frame['auto_frame']); self.assertNotIn('features', task.config)
        self.assertIsNone(review.load(self.run)['exploration']['selected_concept_id'])
    def test_requires_audited_active_spec(self):
        with self.assertRaises(StateError): discovery.effective_task(self.run)
    def test_owned_contract_and_tamper_detection(self):
        record = self.approve()
        contract = discovery.effective_task(self.run)
        self.assertEqual(contract['verification']['dimensions'][0]['expected'], 2)
        self.assertEqual(contract['geometry_requirements'][0]['representation'], 'solid')
        (self.run / record['contract']['path']).write_text('{}')
        with self.assertRaises(StateError): discovery.active_record(self.run)
    def test_initial_snapshot_unchanged_by_amendment(self):
        before = sha256_file(self.run / 'snapshot/task.json'); acceptance = sha256_file(self.run / 'acceptance.json')
        first = self.approve(); amended = copy.deepcopy(self.spec); amended['requirements'][0]['check']['expected'] = 3
        discovery.propose_amendment(self.run, amended, 'New independent source corrects dimension')
        self.assertEqual(discovery.active_record(self.run)['id'], first['id'])
        second = self.approve(amended)
        self.assertEqual(second['parent'], first['id']); self.assertEqual(before, sha256_file(self.run / 'snapshot/task.json'))
        self.assertEqual(acceptance, sha256_file(self.run / 'acceptance.json'))
    def test_unknown_and_omitted_coverage_cannot_approve(self):
        self.spec['claims'][0]['kind'] = 'unknown'
        with self.assertRaises(ValidationError): self.approve()
        self.spec = fixture_spec(); self.spec['requirements'] = []
        with self.assertRaises(ValidationError): discovery.validate_spec(self.spec)
    def test_prior_requires_neighbor_constraints(self):
        claim = self.spec['claims'][0]; claim['kind'] = 'prior'
        with self.assertRaises(ValidationError): discovery.validate_spec(self.spec)
        claim.update(basis='Object class', neighbor_constraints='Observed adjacent plane', alternatives='Rounded or flat hidden face', contradiction_test='Rear view reveals hole', uncertainty='Unseen profile')
        discovery.validate_spec(self.spec)
    def test_bad_selector_representation_and_cycles(self):
        self.spec['entities'][0]['representation'] = 'anything'
        with self.assertRaises(ValidationError): discovery.validate_spec(self.spec)
        self.spec = fixture_spec(); self.spec['entities'][0]['parent_id'] = 'body'
        with self.assertRaises(ValidationError): discovery.validate_spec(self.spec)
    def test_auditor_cannot_waive_measurements(self):
        record = self.approve(); digest = record['specification']['sha256']
        revision = {'specification_sha256': digest, 'evaluation': {'checks': [{'id': 'width', 'status': 'fail'}]}}
        result = {'specification_sha256': digest, 'verdict': 'pass', 'requirement_results': [{'id': 'width', 'status': 'pass', 'reason': 'Looks right', 'evidence': ['inspection.png']}]}
        with self.assertRaises(ValidationError): discovery.verify_requirements(self.run, revision, result)
        result['specification_sha256'] = 'stale'
        with self.assertRaises(StateError): discovery.verify_requirements(self.run, revision, result)
    def test_corrupt_research_cannot_support_contract(self):
        self.approve(); source = discovery.evidence_inventory(self.run)[0]
        (self.run / source['local_path']).write_text('altered')
        with self.assertRaises(StateError): discovery.active_record(self.run)
    def test_offline_rejects_new_source(self):
        with self.assertRaises(ValidationError): discovery.register_sources(self.run, self.run, [{'path': 'new.txt'}], 'offline')
    def test_complete_discovered_publication(self):
        from modelbench.automation import run_loop
        from modelbench.adapter import AgentResult
        from tests.helpers import fake_render
        self.approve()
        def build(root, run, profile, **kwargs):
            artifact = run / 'workspace' / 'built.blend'; artifact.write_bytes(b'owned synthetic Blender artifact')
            return AgentResult('submitted', str(artifact), 'modeling', 1, False, 'Fixture')
        def measure(root, run, artifact, output_dir, **kwargs):
            result = {'ok': True, 'source_sha256': sha256_file(artifact), 'passed': True,
                'checks': [{'id': 'width', 'status': 'pass', 'actual': 2., 'expected': 2.},
                           {'id': 'evaluated_bounds', 'status': 'pass', 'actual': {'min': [-1,-1,-1], 'max': [1,1,1]}}]}
            atomic_write_json(output_dir / 'result.json', result); return result
        def verify(root, run, profile, directory, **kwargs):
            directory.mkdir(parents=True); (directory / 'inspection.txt').write_text('Independent fixture inspection')
            return {'artifact_sha256': review.current(run)['sha256'], 'acceptance_sha256': review.load(run)['acceptance_sha256'],
                'specification_sha256': discovery.active_record(run)['specification']['sha256'], 'verdict': 'pass', 'decision': 'ACCEPT',
                'assessment': 'Fixture independently measured', 'findings': [], 'resolutions': [],
                'requirement_results': [{'id': 'width', 'status': 'pass', 'reason': 'Measured extent', 'evidence': ['inspection.txt'], 'deviation': 0.}]}
        with patch('modelbench.automation.run_adapter', build), patch('modelbench.measurements.evaluate', measure), patch('modelbench.automation.run_blender', fake_render), patch('modelbench.verifier.verify', verify):
            result = run_loop(self.root, self.root / 'generated', self.run)
        self.assertEqual(result['state'], 'promoted')
        self.assertTrue((self.root / 'generated' / self.task_id / 'current' / 'review' / 'discovery').is_dir())
        self.assertEqual(discovery.effective_task(self.run)['frame']['auto_frame'], False)

    def test_discovery_independent_pass_before_proposal(self):
        from modelbench.automation import profile
        builder = profile(self.run, 'builder'); verifier = profile(self.run, 'verifier')
        builder.data['capabilities'] = verifier.data['capabilities'] = {'image_inspection': True, 'offline': True}
        calls = []
        def invoke(root, run, prof, directory, prompt, images=(), **kwargs):
            calls.append(directory.name)
            if directory.name == 'independent': return {'assessment': 'Independent expected width', 'expected_coverage': ['body'], 'conflicts': [], 'sources': []}
            if directory.name == 'proposal': return {'specification_json': json.dumps(self.spec), 'sources': [], 'research_log': 'Supplied evidence only'}
            context = json.loads(prompt[prompt.rfind('\n') + 1:])
            return {'decision': 'APPROVE', 'specification_sha256': context['specification_sha256'], 'reviewed_requirements': ['width'], 'coverage_complete': True, 'identity_supported': True, 'inferences_reviewed': True, 'tolerances_reviewed': True, 'assessment': 'Independent fixture audit'}
        with patch('modelbench.verifier.invoke', invoke):
            self.assertTrue(discovery.ensure_specification(self.root, self.run, builder, verifier))
        self.assertEqual(calls, ['independent', 'proposal', 'audit'])
        self.assertEqual(discovery.summary(self.run)['stage'], 'modeling')

if __name__ == '__main__': unittest.main()
