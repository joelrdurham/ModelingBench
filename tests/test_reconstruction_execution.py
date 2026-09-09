"""Execution acceptance: real images, archived engine replay, and regression truth isolation."""
import copy
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from modelbench.reconstruction import execute, load_bundle, replay, solve, compare_receipts, ReconstructionError
from modelbench.reconstruction.regression import regress
from modelbench.reconstruction.storage import _sha
from tests.test_reconstruction_v2 import scene


class ExecutionAcceptanceTests(unittest.TestCase):
    def test_execution_rectifies_image_and_mask_and_preserves_wheel_lock(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); source=root/'source.png'; Image.new('RGB',(1280,720),'white').save(source)
            case=scene();case['images'][0]['sha256']=_sha(source)
            case['planes']=[{'id':'front','landmarks':['p0','p1','p2','p3'],'output_scale':20}]
            run=execute(case,root/'run',{'source':source})
            result=json.loads((run/'result.json').read_text())
            self.assertEqual(result['status'],'solved',result)
            plane=result['diagnostic_artifacts'][0]
            with Image.open(run/plane['image']) as image: self.assertEqual(image.size,(40,40))
            with Image.open(run/plane['validity_mask']) as image: self.assertEqual(image.getextrema(),(255,255))
            wheel=next((run/'environment').glob('*.whl'))
            with zipfile.ZipFile(wheel) as archive:
                self.assertIn('modelbench/reconstruction/core.py',archive.namelist())
                self.assertFalse(any('modelbench/discovery.py' in name for name in archive.namelist()))
            self.assertIn('numpy==',(run/'environment/requirements.lock').read_text())
            self.assertEqual(load_bundle(run),case)
            rerun=replay(run,root/'replay')
            self.assertIsInstance(rerun,Path)
            self.assertEqual(load_bundle(rerun),case)
            self.assertEqual(result['receipt']['case_hash'],json.loads((rerun/'result.json').read_text())['receipt']['case_hash'])

    def test_failure_receipts_and_missing_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            run=execute({'case_version':'unsupported'},root/'invalid')
            self.assertEqual(json.loads((run/'execution.json').read_text())['completion_status'],'failed')
            self.assertEqual(load_bundle(run),{'case_version':'unsupported'})
            with self.assertRaises(ReconstructionError): execute(scene(),run)
            (run/'manifest.json').unlink()
            with self.assertRaises(ReconstructionError): load_bundle(run)

    def test_links_and_manifest_escape_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);run=execute(scene(),root/'run')
            content=(run/'case.json').read_bytes(); original=root/'other.json'; original.write_bytes(content)
            (run/'case.json').unlink();os.link(original,run/'case.json')
            with self.assertRaises(ReconstructionError): load_bundle(run)
            legacy=root/'legacy';legacy.mkdir();(legacy/'case.json').write_text('{}')
            from modelbench.reconstruction.provenance import json_hash
            (legacy/'manifest.json').write_text(json.dumps({'case_hash':json_hash({}),'images':[{'file':'../other.json','sha256':_sha(original)}]}))
            with self.assertRaises(ReconstructionError): load_bundle(legacy)

    def test_duplicate_basenames_have_independent_stable_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);a=root/'a';b=root/'b';a.mkdir();b.mkdir();(a/'same.bin').write_bytes(b'first');(b/'same.bin').write_bytes(b'second')
            legacy={'case_version':'1.0','image_coordinates':{'space':'normalized','origin':'top-left'},'camera':{'model':'perspective'},'correspondences':[]}
            run=execute(legacy,root/'run',{'front':a/'same.bin','back':b/'same.bin'})
            manifest=json.loads((run/'manifest.json').read_text())
            self.assertEqual({x['id'] for x in manifest['images']},{'front','back'})
            self.assertEqual(len({x['file'] for x in manifest['images']}),2)
            self.assertEqual(load_bundle(run),legacy)

    def test_replay_restores_archived_code_when_current_digest_differs(self):
        from modelbench.reconstruction import environment
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);run=execute(scene(),root/'run')
            actual=environment.current_environment();actual['implementation_sha256']='changed'
            with patch.object(environment,'current_environment',return_value=actual):
                result=replay(run,root/'restored')
            self.assertIsInstance(result,Path,result)
            self.assertEqual(load_bundle(result),scene())

    def test_cross_version_compare_and_explicit_migration(self):
        left=solve(scene());right=copy.deepcopy(left);right['receipt']['engine_version']='3.0.0'
        self.assertTrue(compare_receipts(left,right)['compatible'])
        right['receipt']['case_hash']='another-case'
        with self.assertRaises(ReconstructionError):compare_receipts(left,right)
        mapping={'left_case_hash':left['receipt']['case_hash'],'right_case_hash':'another-case','measurement_ids':{'old':'new'}}
        self.assertTrue(compare_receipts(left,right,migration=mapping)['compatible'])

    def test_real_regression_processes_keep_truth_out_of_solver_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);case=scene();case['measurements']=[{'id':'width','type':'distance','landmarks':['p0','p1']}]
            report=regress([{'case':case,'ground_truth':{'status':'solved','measurements':{'width':2}}}],sys.executable,sys.executable,destination=root/'suite')
            self.assertTrue(report['correctness_passed'],report)
            for side in ('left','right'):
                owned=json.loads((root/'suite/case_0000'/side/'case.json').read_text())
                self.assertNotIn('ground_truth',owned)
            self.assertTrue((root/'suite/regression.json').is_file())


if __name__=='__main__':unittest.main()
