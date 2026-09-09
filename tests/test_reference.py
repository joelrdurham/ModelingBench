"""Independent image-space residual tests (no camera refitting or Blender required)."""
import tempfile
import unittest
from pathlib import Path
from modelbench.reference import measure_image_fit

class ReferenceLossTests(unittest.TestCase):
    def setUp(self):
        try:
            from PIL import Image
            import scipy
        except ImportError: self.skipTest('Install reference extra')
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        Image.new('RGBA', (64,64), (150,150,150,255)).save(self.root/'render.png')
        Image.new('RGB',(64,64),'gray').save(self.root/'source.png')
    def tearDown(self):
        if hasattr(self,'temp'): self.temp.cleanup()
    def test_exact_then_wrong_geometry_residual_and_occlusion(self):
        ref={'bindings':[{'id':'feature','object':'BODY','vertex_index':0,'image':[.5,.5],'uncertainty':.01,'visible':True}]}
        rendered={'render':{'rgba':str(self.root/'render.png')},'bindings':[{'id':'feature','points':[{'pixel':[32,32],'visible':True}]}]}
        exact=measure_image_fit(ref,rendered,self.root/'source.png',self.root)
        self.assertEqual(exact['metrics']['point_rms_uncertainty'],0)
        rendered['bindings'][0]['points'][0].update(pixel=[48,32],visible=False)
        wrong=measure_image_fit(ref,rendered,self.root/'source.png',self.root)
        self.assertGreater(wrong['metrics']['point_rms_uncertainty'],1)
        self.assertEqual(wrong['metrics']['occlusion_mismatches'],1)
    def test_missing_binding_is_unknown_not_zero_loss(self):
        ref={'bindings':[{'id':'feature','object':'BODY','vertex_index':0,'image':[.5,.5]}]}
        result=measure_image_fit(ref,{'render':{'rgba':str(self.root/'render.png')},'bindings':[]},self.root/'source.png',self.root)
        self.assertIsNone(result['metrics']['point_rms_normalized'])
        self.assertEqual(result['missing_bindings'],['feature'])
    def test_wrong_silhouette_does_not_hide_behind_landmarks(self):
        ref={'bindings':[],'silhouette':[[0,0],[.5,0],[.5,1],[0,1]]}
        result=measure_image_fit(ref,{'render':{'rgba':str(self.root/'render.png')},'bindings':[]},self.root/'source.png',self.root)
        self.assertLess(result['metrics']['silhouette_iou'],.6)
        self.assertTrue((self.root/'silhouette_difference.png').is_file())

if __name__=='__main__': unittest.main()
