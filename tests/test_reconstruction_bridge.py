"""Harness evidence ownership, candidate agreement, and publication acceptance."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from modelbench import discovery, review
from modelbench.automation import configure
from modelbench.briefs import create_brief_task
from modelbench.errors import StateError, ValidationError
from modelbench.orchestrator import prepare_new_run
from modelbench.reconstruction_bridge import solve_references, identities, evaluation_identity
from modelbench.util import atomic_write_json, canonical_hash, sha256_file
from tests.helpers import TemporaryProject
from tests.test_discovery import fixture_spec
from tests.test_reconstruction_v2 import scene


class ReconstructionBridgeTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.project=TemporaryProject();self.root=self.project.root
        source=self.root/'source.png';Image.new('RGB',(1280,720),'gray').save(source)
        self.task_id=create_brief_task(self.root,'Synthetic measured image',references=[source],research_mode='offline')
        self.run,builder=prepare_new_run(self.root,self.task_id,'fake_test')
        configure(self.root,self.run,builder,'fake_test')
        self.spec=fixture_spec();case=scene()
        case['planes']=[{'id':'front','landmarks':['p0','p1','p2','p3'],'output_scale':20}]
        self.spec['references']=[{'id':'front','evidence_id':'ev_0001','case':case,'applicability':'Independent synthetic image','shape_confidence':1.,'identity_confidence':1.,
                                  'bindings':[{'id':'vertex','object':'BODY','vertex_index':0,'image':[453/1280,187/720],'uncertainty':.002}]}]
        self.spec['requirements'].append({'id':'image_agreement','criterion':'Model vertex matches image','entity_ids':['body'],'claim_ids':['width'],'critical':True,
                                          'method':'reference','reference_id':'front','acceptance':'Within annotated uncertainty','tolerance_reason':'Synthetic image accuracy',
                                          'check':{'metric':'point_rms_uncertainty','threshold':1.}})
        self.records=solve_references(self.run,self.spec,self.run/'reconstruction-test')

    def tearDown(self): self.project.cleanup()

    def approve(self):
        path=self.run/'proposal.json';atomic_write_json(path,self.spec)
        audit={'decision':'APPROVE','specification_sha256':sha256_file(path),'reviewed_requirements':['width','image_agreement'],
               'coverage_complete':True,'identity_supported':True,'inferences_reviewed':True,'tolerances_reviewed':True,
               'assessment':'Independent test audit','reconstruction_evidence_sha256':canonical_hash(identities(self.records))}
        return discovery.approve(self.run,self.spec,audit,path,self.records)

    def test_contract_binds_execution_and_requires_audit_identity(self):
        record=self.approve();contract=discovery.effective_task(self.run)
        self.assertEqual(contract['reference_requirements'][0]['id'],'image_agreement')
        self.assertEqual(contract['approved_reconstructions'],identities(self.records))
        ledger=discovery.ledger(self.run)
        ledger['versions'][0]['reconstructions'][0]['solution']['camera']['intrinsics']['focal_length']=999
        atomic_write_json(self.run/'discovery/ledger.json',ledger)
        with self.assertRaisesRegex(StateError,'ledger differs'):discovery.active_record(self.run)

    def test_candidate_identity_changes_for_artifact_spec_engine_bindings(self):
        record=self.approve();base=evaluation_identity(self.run,'artifact-a',record,self.spec)
        self.assertNotEqual(base,evaluation_identity(self.run,'artifact-b',record,self.spec))
        other=copy.deepcopy(record);other['reconstructions'][0]['engine_implementation_sha256']='new-engine'
        self.assertNotEqual(base,evaluation_identity(self.run,'artifact-a',other,self.spec))
        other=copy.deepcopy(record);other['specification']['sha256']='new-spec'
        self.assertNotEqual(base,evaluation_identity(self.run,'artifact-a',other,self.spec))
        spec=copy.deepcopy(self.spec);spec['references'][0]['bindings'][0]['vertex_index']=1
        self.assertNotEqual(base,evaluation_identity(self.run,'artifact-a',record,spec))

    def test_selected_alternative_owns_consistent_assessment_and_images(self):
        from modelbench.reconstruction import solve
        case=scene();case['solver']['starts']=16
        case['landmarks'].extend([{'id':'origin','coordinates':[0,0,0]}, {'id':'point','coordinates':[0,0,None],'initial':[0,0,.1],'bounds':[[-1,-1,-3],[1,1,3]]}])
        case['observations'].append({'id':'ray','landmark_id':'point','image':[613,347],'sigma':.5})
        case['constraints']=[{'id':'sphere','type':'distance','landmarks':['origin','point'],'value':2,'tolerance':.001}]
        case['measurements']=[{'id':'gap','type':'distance','landmarks':['p4','point']}]
        case['planes']=[{'id':'front','landmarks':['p0','p1','p2','p3'],'output_scale':20}]
        result=solve(case)
        self.assertEqual(result['status'],'ambiguous',result)
        alternative=next(h for h in result['camera_hypotheses'] if h['id'] != result['selected_hypothesis'] and h['valid'])
        spec=copy.deepcopy(self.spec);spec['references'][0].update(case=case,hypothesis_id=alternative['id'])
        record=solve_references(self.run,spec,self.run/'alternative')[0];selected=record['solution']
        for key in ('landmarks','measurements','constraints','projections','depth','landmark_depths'):
            self.assertEqual(selected[key],alternative[key],key)
        self.assertEqual(selected['mathematical_status'],'solved')
        self.assertEqual(selected['unresolved_degrees_of_freedom'],alternative['identifiability'])
        self.assertTrue(selected['diagnostic_artifacts'])
        for artifact in selected['diagnostic_artifacts']:
            self.assertEqual(artifact['hypothesis_id'],alternative['id'])
            self.assertTrue((self.run/record['path']/artifact['image']).is_file())
        original=json.loads((self.run/record['execution_path']/'result.json').read_text())
        self.assertEqual(original['status'],'ambiguous')
        self.assertEqual(original['selected_hypothesis'],result['selected_hypothesis'])

    def test_audit_must_identify_the_exact_execution_evidence(self):
        path=self.run/'proposal.json';atomic_write_json(path,self.spec)
        audit={'decision':'APPROVE','specification_sha256':sha256_file(path),'reviewed_requirements':['width','image_agreement'],
               'coverage_complete':True,'identity_supported':True,'inferences_reviewed':True,'tolerances_reviewed':True,'assessment':'Fixture'}
        with self.assertRaisesRegex(ValidationError,'exact reconstruction execution'):
            discovery.approve(self.run,self.spec,audit,path,self.records)

    def fake_reference(self, root, run, artifact, output, reference, solution, task, budget_deadline=None):
        from PIL import Image
        output.mkdir(parents=True,exist_ok=True);Image.new('RGBA',(1280,720),(128,128,128,255)).save(output/'reference.png')
        return {'source_sha256':sha256_file(artifact),'render':{'rgba':str(output/'reference.png')},
                'bindings':[{'id':'vertex','points':[{'pixel':[453,187],'visible':True}]}]}

    def test_wrong_candidate_fails_despite_exact_reconstruction(self):
        from modelbench.reference import evaluate_references
        self.approve();artifact=self.run/'workspace/model.blend';artifact.write_bytes(b'model')
        def wrong(*args,**kwargs):
            result=self.fake_reference(*args,**kwargs);result['bindings'][0]['points'][0]['pixel']=[800,400];return result
        with patch('modelbench.reference.render_reference',wrong):
            result=evaluate_references(self.root,self.run,artifact,self.run/'candidate-check',{'passed':True,'checks':[]})
        checks={x['id']:x for x in result['checks']}
        self.assertEqual(checks['reconstruction_support_front']['status'],'pass')
        self.assertEqual(checks['image_agreement']['status'],'fail')
        self.assertFalse(result['passed'])

    def test_reconstruction_only_cannot_satisfy_candidate_gate(self):
        record=self.approve();digest=record['specification']['sha256']
        identity=evaluation_identity(self.run,'model',record,self.spec)
        revision={'sha256':'model','specification_sha256':digest,'evaluation':{'reference_identity':identity,'reference_cache_key':canonical_hash(identity),
                  'checks':[{'id':'width','status':'pass'},{'id':'image_agreement','status':'pass','evidence_source':'reconstruction'}]}}
        audit={'specification_sha256':digest,'verdict':'pass','requirement_results':[{'id':i,'status':'pass','reason':'Test audit','evidence':['proof.png']} for i in ['width','image_agreement']]}
        with self.assertRaisesRegex(ValidationError,'cannot satisfy'):discovery.verify_requirements(self.run,revision,audit)

    def test_complete_reference_publication_exports_self_contained_evidence(self):
        from modelbench.automation import run_loop
        from modelbench.adapter import AgentResult
        from tests.helpers import fake_render
        self.approve()
        def build(root,run,profile,**kwargs):
            artifact=run/'workspace/built.blend';artifact.write_bytes(b'owned synthetic model')
            return AgentResult('submitted',str(artifact),'modeling',1,False,'Fixture')
        def measure(root,run,artifact,output_dir,**kwargs):
            result={'ok':True,'source_sha256':sha256_file(artifact),'passed':True,'checks':[{'id':'width','status':'pass','actual':2.,'expected':2.},
                     {'id':'evaluated_bounds','status':'pass','actual':{'min':[-1,-1,-1],'max':[1,1,1]}}]}
            atomic_write_json(output_dir/'result.json',result);return result
        def verify(root,run,profile,directory,**kwargs):
            directory.mkdir(parents=True);(directory/'inspection.txt').write_text('Independent assessment')
            return {'artifact_sha256':review.current(run)['sha256'],'acceptance_sha256':review.load(run)['acceptance_sha256'],
                    'specification_sha256':discovery.active_record(run)['specification']['sha256'],'verdict':'pass','decision':'ACCEPT','assessment':'Fixture inspected',
                    'findings':[],'resolutions':[],'requirement_results':[{'id':i,'status':'pass','reason':'Independent measured geometry','evidence':['inspection.txt'],'deviation':0.} for i in ['width','image_agreement']]}
        with patch('modelbench.automation.run_adapter',build),patch('modelbench.measurements.evaluate',measure),patch('modelbench.automation.run_blender',fake_render),patch('modelbench.reference.render_reference',self.fake_reference),patch('modelbench.verifier.verify',verify):
            result=run_loop(self.root,self.root/'generated',self.run)
        self.assertEqual(result['state'],'promoted',result)
        package=self.root/'generated'/self.task_id/'current/review'
        self.assertIn('Reconstruction evidence',(package/'index.html').read_text())
        self.assertIn('Model-to-reference agreement',(package/'index.html').read_text())
        # Reconstruction stored outside discovery in this fixture still must be exported.
        record=self.records[0]
        self.assertTrue((package/record['execution_path']/'execution.json').is_file())


if __name__=='__main__':unittest.main()
