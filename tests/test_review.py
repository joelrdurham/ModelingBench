"""Acceptance tests for the completion gate, correction loop and recovery."""
import copy
import shutil
from pathlib import Path
import unittest
from unittest.mock import patch
from modelbench import review
from modelbench.adapter import AgentResult
from modelbench.automation import configure, run_loop, render_stage
from modelbench.errors import StateError, ValidationError
from modelbench.orchestrator import prepare_new_run, _owned_submission
from modelbench.project import ensure_generated_root
from modelbench.runs import promote
from modelbench.state import load_state
from modelbench.util import atomic_write_json, sha256_file, read_json
from tests.helpers import TemporaryProject, fake_render, fake_measurements, fake_verify


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.project = TemporaryProject()
        self.root = self.project.root
        self.generated = ensure_generated_root(self.root)
        self.run, builder = prepare_new_run(self.root, 'example_drawing_object', 'fake_test')
        configure(self.root, self.run, builder, 'fake_test')

    def tearDown(self):
        self.project.cleanup()

    def submit(self, data=b'owned fixture', run=None):
        run = run or self.run
        source = run / 'workspace/model.blend'
        source.write_bytes(data)
        owned = _owned_submission(run, AgentResult('submitted', str(source), 'test', None, False, ''))
        return review.register_revision(run, owned)

    def candidate(self, run=None):
        run = run or self.run
        revision = self.submit(run=run)
        base = run / 'revisions' / revision['id']
        result = fake_measurements(self.root, run, review.integrity(run), base / 'measurements')
        review.record_evaluation(run, result)
        with patch('modelbench.automation.run_blender', fake_render):
            for kind in ('diagnostics', 'final'):
                render_stage(self.root, run, revision, kind)
                review.record_evidence(run, base / kind, kind)
        verdict = fake_verify(self.root, run, None, base / 'verification')
        review.record_verification(run, verdict)
        return revision, verdict

    def test_revision_preserves_seed_reports_and_recorded_invocations(self):
        from modelbench.evidence import register_evidence, evidence_records
        from modelbench.orchestrator import create_revision
        self.submit()
        claim = self.run / 'workspace/construction.json'
        claim.write_text('{"claim": "construction commands"}')
        record = register_evidence(self.run, claim, origin='agent_researched', title='Construction report')
        trace = self.run / 'logs/agent_turn_0001.stdout.log'
        trace.write_text('Original construction command')
        settings = read_json(self.run / 'model-settings.json')
        result = create_revision(self.root, 'example_drawing_object/' + self.run.name, execute=False)
        revision_run = self.generated / 'example_drawing_object/runs' / result['run'].split('/')[-1]
        records = evidence_records(revision_run)
        inherited_claim = next(r for r in records if r['sha256'] == record['sha256'])
        self.assertEqual(inherited_claim['origin'], 'agent_researched')
        provenance = next(r for r in records if r['title'] == 'Historical seed invocation settings')
        self.assertEqual(read_json(revision_run / provenance['local_path']), settings)
        self.assertTrue(any(r['sha256'] == sha256_file(trace) for r in records))
        self.assertEqual(review.current(revision_run)['sha256'], review.current(self.run)['sha256'])
        self.assertIsNone(review.current(revision_run)['verification'])

    def test_enabled_benchmark_persists_manifest_and_advances_concept(self):
        run, builder = prepare_new_run(self.root, 'goodyear_optitrac_lsw_1400_30r46', 'fake_test')
        configure(self.root, run, builder, 'fake_test')
        acceptance = read_json(run / 'acceptance.json')
        self.assertIn('interface:bead_rim_mating', acceptance['design_manifest']['requirement_ids'])
        revision = self.submit(run=run)
        result = fake_measurements(self.root, run, review.integrity(run), run / 'revisions' / revision['id'] / 'measurements')
        review.record_evaluation(run, result)
        verdict = {'artifact_sha256': revision['sha256'], 'acceptance_sha256': review.load(run)['acceptance_sha256'],
                   'verdict': 'repair', 'decision': 'REFINE', 'assessment': 'Explore a structurally different tread architecture',
                   'scores': {'accuracy': 70, 'precision': 65, 'assembly': 80, 'physical_viability': 75, 'aesthetic': 60, 'evidence': 55},
                   'findings': [], 'resolutions': []}
        review.record_verification(run, verdict)
        value = review.load(run)
        self.assertEqual((value['exploration']['phase'], value['exploration']['concepts']), ('diverge', 2))
        self.assertEqual(value['revisions'][-1]['design_audit']['decision'], 'REFINE')

    def test_declared_motion_frame_is_required_by_publication_gate(self):
        run, builder = prepare_new_run(self.root, 'goodyear_optitrac_lsw_1400_30r46', 'fake_test')
        configure(self.root, run, builder, 'fake_test')
        revision, _ = self.candidate(run)
        review.assert_publishable(run, revision['sha256'])
        value = review.load(run)
        value['revisions'][-1]['evidence'] = [r for r in value['revisions'][-1]['evidence'] if not r['path'].endswith('/motion/front_0001.png')]
        atomic_write_json(run / 'review.json', value)
        with self.assertRaisesRegex(StateError, 'Required render evidence'):
            review.assert_publishable(run, revision['sha256'])

    def test_registered_submission_resumes_without_reinvoking_builder(self):
        self.submit()
        self.assertEqual(review.load(self.run)['operation']['stage'], 'measurements')
        with patch('modelbench.automation.run_adapter') as builder, patch('modelbench.automation.run_blender', fake_render), patch('modelbench.measurements.evaluate', fake_measurements), patch('modelbench.verifier.verify', fake_verify):
            result = run_loop(self.root, self.generated, self.run)
        builder.assert_not_called()
        self.assertEqual(result['state'], 'promoted')

    def test_cached_render_receipt_recovers_after_directory_rename(self):
        from modelbench.config import load_toml
        revision = self.submit()
        parent = self.run / 'revisions' / revision['id']
        attempt = parent / '.diagnostics-attempt-0001'
        task = read_json(self.run / 'snapshot/task.json')
        profile = load_toml(self.run / 'snapshot/checkpoint_profile.toml')
        rendered = fake_render(self.root, self.run, review.integrity(self.run), attempt, task, profile,
            mode='checkpoint', timeout=60, views=['front','right','top','azimuth_045'],
            motion_frames=task.get('verification', {}).get('frames', []))
        atomic_write_json(attempt / 'blender_result.json', rendered)
        directory = parent / 'diagnostics'; attempt.rename(directory)
        recovered = render_stage(self.root, self.run, revision, 'diagnostics')
        self.assertTrue(all(Path(r['path']).is_relative_to(directory) for r in recovered['renders']))
        self.assertEqual(read_json(directory / 'blender_result.json'), recovered)

    def test_publication_rebuilds_divergent_final_mirror(self):
        revision, _ = self.candidate()
        source = self.run / 'revisions' / revision['id'] / 'final'
        mirror = self.run / 'renders/final'; shutil.copytree(source, mirror)
        corrupted = mirror / 'orthographic/front.png'; corrupted.write_bytes(b'corrupt')
        with self.assertRaises(StateError):
            promote(self.generated, self.run, review.integrity(self.run), expected_artifact_sha256=revision['sha256'])
        with patch('modelbench.automation.run_adapter') as builder:
            result = run_loop(self.root, self.generated, self.run)
        builder.assert_not_called(); self.assertEqual(result['state'], 'promoted')
        self.assertEqual(sha256_file(self.generated / 'example_drawing_object/current/renders/orthographic/front.png'), sha256_file(source / 'orthographic/front.png'))
        published = self.generated / 'example_drawing_object/current/renders/orthographic/front.png'
        published.write_bytes(b'corrupted after interrupted publication')
        promote(self.generated, self.run, review.integrity(self.run), expected_artifact_sha256=revision['sha256'])
        self.assertEqual(sha256_file(published), sha256_file(source / 'orthographic/front.png'))

    def test_publication_gates_reject_open_tasks_failed_measurements_missing_evidence_hash_mismatch(self):
        revision, verdict = self.candidate()
        review.assert_publishable(self.run, revision['sha256'])
        baseline = review.load(self.run)
        mutations = [lambda v: v['tasks'].append({'status': 'open'}),
                     lambda v: v['revisions'][-1]['evaluation'].update(passed=False),
                     lambda v: v['revisions'][-1].update(evidence=[]),
                     lambda v: v['revisions'][-1]['verification'].update(artifact_sha256='wrong')]
        for mutate in mutations:
            value = copy.deepcopy(baseline)
            mutate(value)
            atomic_write_json(self.run / 'review.json', value)
            with self.assertRaises(StateError):
                review.assert_publishable(self.run, revision['sha256'])
        atomic_write_json(self.run / 'review.json', baseline)
        review.integrity(self.run).write_bytes(b'changed')
        with self.assertRaises(StateError): review.assert_publishable(self.run, revision['sha256'])

    def test_registered_builder_reports_remain_claims_and_are_packaged(self):
        from modelbench.evidence import register_evidence
        source = self.run / 'workspace/report.json'
        atomic_write_json(source, {'builder_claim': 'Correct'})
        claim = register_evidence(self.run, source, origin='agent_researched')
        revision, verdict = self.candidate()
        record = next(r for r in review.current(self.run)['evidence'] if r['path'] == claim['local_path'])
        self.assertEqual(record['kind'], 'builder_claim')
        task = review.add_task(self.run, 'Verify the supplied report')
        review.respond(self.run, task['id'], 'Report is supplied')
        verdict['resolutions'] = [{'task_id':task['id'], 'resolved':True, 'reason':'Inspected report', 'evidence':[claim['local_path']]}]
        with self.assertRaisesRegex(ValidationError, 'independent evidence'):
            review.record_verification(self.run, verdict)
        verdict['resolutions'][0]['evidence'].append(f"revisions/{revision['id']}/measurements/measurements.json")
        review.record_verification(self.run, verdict)
        destination = self.run / 'test-package'
        review.package(self.run, destination)
        self.assertEqual(sha256_file(destination / claim['local_path']), claim['sha256'])

    def test_completed_verification_recovers_mutable_claim_links_without_new_invocation(self):
        from modelbench.evidence import register_evidence
        from modelbench.verifier import recover_completed_verification
        source = self.run / 'workspace/report.json'
        atomic_write_json(source, {'builder_claim': 'Correct'})
        owned = register_evidence(self.run, source, origin='agent_researched')
        revision, verdict = self.candidate()
        task = review.add_task(self.run, 'Verify report ownership')
        review.respond(self.run, task['id'], 'Supplied report')
        verdict['resolutions'] = [{'task_id':task['id'], 'resolved':True, 'reason':'Independently checked',
            'evidence':['workspace/report.json', 'evidence/registry.jsonl', f"revisions/{revision['id']}/measurements/measurements.json"]}]
        value = review.load(self.run)
        value['revisions'][-1]['verification'] = None
        value['revisions'][-1].pop('verification_receipt')
        atomic_write_json(self.run / 'review.json', value)
        attempt = self.run / 'revisions' / revision['id'] / 'verification/attempt_0001'
        atomic_write_json(attempt / 'verification.json', verdict)
        self.assertTrue(recover_completed_verification(self.run))
        current = review.current(self.run)
        links = current['verification']['resolutions'][0]['evidence']
        self.assertIn(owned['local_path'], links)
        self.assertNotIn('workspace/report.json', links)
        snapshots = [r for r in current['evidence'] if '/claim_snapshots/' in r['path']]
        self.assertTrue(snapshots)
        self.assertTrue(all(r['kind'] == 'builder_claim' for r in snapshots))
        with (self.run / 'evidence/registry.jsonl').open('a') as stream: stream.write('\n')
        review.assert_publishable(self.run, revision['sha256'])

    def test_first_revision_compares_workflow_claims_after_independent_assessment(self):
        from modelbench.evidence import register_evidence
        from modelbench.verifier import verify
        source = self.run / 'workspace/checkpoint-observations.json'
        atomic_write_json(source, {'observations':'Builder workflow record'})
        register_evidence(self.run, source, origin='agent_researched')
        revision, verdict = self.candidate()
        initial = copy.deepcopy(verdict)
        initial['verdict'] = 'repair'
        initial['findings'] = [{'requirement':'workflow', 'instruction':'Inspect checkpoint observations', 'severity':'error', 'evidence':[]}]
        attempt = self.run / 'revisions' / revision['id'] / 'verification/independent-fixture'
        with patch('modelbench.verifier.invoke', side_effect=[initial, verdict]) as invocation:
            result = verify(self.root, self.run, None, attempt)
        self.assertEqual(invocation.call_count, 2)
        self.assertIn('Do not read builder claims', invocation.call_args_list[0].args[4])
        self.assertIn('builder_claims_directory', invocation.call_args_list[1].args[4])
        self.assertEqual(result['verdict'], 'pass')

    def test_claimed_ineffective_measured_correction_stays_open(self):
        revision, verdict = self.candidate()
        task = review.add_task(self.run, 'Correct panel geometry', source='measured', task_id='measured_panel')
        review.respond(self.run, task['id'], 'Claimed fixed dimensions')
        value = review.load(self.run)
        value['revisions'][-1]['evaluation']['checks'].append({'id':'panel','status':'fail'})
        atomic_write_json(self.run / 'review.json', value)
        verdict['resolutions'] = [{'task_id': task['id'], 'resolved': True, 'reason': 'Looks fine', 'evidence':[f"revisions/{revision['id']}/measurements/measurements.json"]}]
        review.record_verification(self.run, verdict)
        self.assertEqual(review.load(self.run)['tasks'][0]['status'], 'open')
        with self.assertRaises(StateError): review.assert_publishable(self.run, revision['sha256'])

    def test_regression_creates_task_and_loop_retains_matched_evidence(self):
        calls = {'builder':0, 'verifier':0}
        def builder(root, run, profile):
            calls['builder'] += 1
            for task in review.load(run)['tasks']:
                if task['status'] != 'verifier_resolved': review.respond(run, task['id'], 'Changed geometry for ' + task['instruction'])
            source = run / 'workspace/model.blend'
            source.write_bytes(f"revision {calls['builder']}".encode())
            return AgentResult('submitted', str(source), 'repair', 1, False, '')
        def verifier(root, run, profile, directory):
            calls['verifier'] += 1
            result = fake_verify(root, run, profile, directory)
            revision = review.current(run)
            result['resolutions'] = [{'task_id':t['id'], 'resolved':True, 'reason':'Independently verified',
                'evidence':[f"revisions/{revision['id']}/measurements/measurements.json"]}
                for t in review.load(run)['tasks'] if t['status']=='builder_addressed']
            if calls['verifier'] < 3:
                result['verdict'] = 'repair'
                result['findings'] = [{'requirement':'panel' if calls['verifier']==1 else 'regression',
                    'instruction':'Fix panel' if calls['verifier']==1 else 'Fix new regression', 'evidence': []}]
            atomic_write_json(directory / 'verification.json', result)
            return result
        with patch('modelbench.automation.run_adapter', builder), patch('modelbench.automation.run_blender', fake_render), patch('modelbench.measurements.evaluate', fake_measurements), patch('modelbench.verifier.verify', verifier):
            result = run_loop(self.root, self.generated, self.run)
        self.assertEqual(result['state'], 'promoted')
        self.assertEqual(calls, {'builder':3, 'verifier':3})
        ledger = review.load(self.run)
        self.assertEqual(len(ledger['revisions']),3)
        self.assertTrue(all(t['status']=='verifier_resolved' for t in ledger['tasks']))
        self.assertTrue(ledger['tasks'][0]['comparisons']['revision_000002'])
        self.assertTrue((self.generated / 'example_drawing_object/current/review/manifest.json').is_file())

    def test_malformed_verification_retries_twice_then_resumes_without_builder(self):
        calls = {'builder':0}
        def builder(root, run, profile):
            calls['builder'] += 1
            source = run / 'workspace/model.blend'; source.write_bytes(b'fixture')
            return AgentResult('submitted', str(source), 'test', None, False, '')
        with patch('modelbench.automation.run_adapter', builder), patch('modelbench.automation.run_blender', fake_render), patch('modelbench.measurements.evaluate', fake_measurements):
            with patch('modelbench.verifier.verify', return_value={}) as bad:
                with self.assertRaises(ValidationError): run_loop(self.root, self.generated, self.run)
            self.assertEqual(bad.call_count, 3)
            self.assertEqual(load_state(self.run)['state'], 'interrupted')
            from modelbench.automation import reset_retries
            reset_retries(self.run)
            with patch('modelbench.verifier.verify', fake_verify):
                result = run_loop(self.root, self.generated, self.run)
        self.assertEqual(result['state'], 'promoted')
        self.assertEqual(calls['builder'], 1)

    def test_mid_checkpoint_feedback_is_not_resolved(self):
        from modelbench.feedback import add_feedback, pending_feedback
        from modelbench.checkpoints import create_checkpoint
        source = self.run / 'workspace/model.blend'; source.write_bytes(b'fixture')
        first = add_feedback(self.run, 'First correction')
        def render(*args, **kwargs):
            add_feedback(self.run, 'Arrived during rendering')
            return fake_render(*args, **kwargs)
        with patch('modelbench.checkpoints.run_blender', render):
            create_checkpoint(self.root, self.run, source, 'checkpoint')
        self.assertEqual(len(pending_feedback(self.run)), 2)
        self.assertEqual(len(review.load(self.run)['tasks']), 2)
        self.assertTrue(all(t['status']=='open' for t in review.load(self.run)['tasks']))

    def test_cancel_never_publishes(self):
        self.candidate()
        atomic_write_json(self.run / 'control.json', {'cancel_requested': True})
        result = run_loop(self.root, self.generated, self.run)
        self.assertEqual(result['state'], 'cancelled')
        self.assertFalse((self.run.parents[1] / 'current.json').exists())

    def test_ledger_cannot_diverge_from_result_receipt(self):
        revision, _ = self.candidate()
        value = review.load(self.run)
        value['revisions'][-1]['verification']['assessment'] = 'Forged replacement'
        atomic_write_json(self.run / 'review.json', value)
        with self.assertRaisesRegex(StateError, 'diverges'):
            review.assert_publishable(self.run, revision['sha256'])

    def test_crash_after_revision_receipt_recovers_idempotently(self):
        source = self.run / 'workspace/model.blend'; source.write_bytes(b'fixture')
        artifact = _owned_submission(self.run, AgentResult('submitted', str(source), 'test', None, False, ''))
        with patch('modelbench.review.save', side_effect=OSError('crash before ledger commit')):
            with self.assertRaises(OSError): review.register_revision(self.run, artifact)
        self.assertEqual(review.load(self.run)['revisions'], [])
        revision = review.register_revision(self.run, artifact)
        self.assertEqual(revision['id'], 'revision_000001')
        self.assertEqual(len(review.load(self.run)['revisions']), 1)

    def test_pass_with_actionable_findings_is_rejected_atomically(self):
        _, verdict = self.candidate()
        before = review.load(self.run)
        verdict['findings'] = [{'instruction':'Still broken', 'requirement':'panel', 'evidence':[]}]
        with self.assertRaises(ValidationError): review.record_verification(self.run, verdict)
        self.assertEqual(review.load(self.run), before)

    def test_retry_attempts_survive_process_restart(self):
        value = review.load(self.run)
        value['retries'] = {'initial:diagnostic': {'attempts':2, 'status':'running'}}
        atomic_write_json(self.run / 'review.json', value)
        from modelbench.automation import retry
        with patch('builtins.print'):
            operation = unittest.mock.Mock(side_effect=OSError('temporary failure'))
            with self.assertRaises(OSError): retry(self.run, 'diagnostic', operation)
        self.assertEqual(operation.call_count, 1)
        self.assertEqual(review.load(self.run)['retries']['initial:diagnostic']['attempts'], 3)

    def test_readme_commands_match_cli(self):
        import re, shlex
        from modelbench.cli import build_parser
        text = (Path(__file__).resolve().parents[1] / 'README.md').read_text()
        commands = re.findall(r'```powershell\n(.*?)```', text, re.S)
        tested = 0
        for block in commands:
            block = block.replace('`\n', '')
            for line in block.splitlines():
                if line.startswith('modelbench '):
                    build_parser().parse_args(shlex.split(line)[1:])
                    tested += 1
        self.assertGreater(tested, 10)


if __name__ == '__main__': unittest.main()
