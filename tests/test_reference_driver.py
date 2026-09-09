import os, shutil, subprocess, tempfile, unittest
from pathlib import Path

class ReferenceProjectionIntegrationTests(unittest.TestCase):
 def test_rectangular_offset_projection_matches_blender(self):
  if os.environ.get('MODELBENCH_BLENDER_INTEGRATION')!='1': self.skipTest('set MODELBENCH_BLENDER_INTEGRATION=1')
  blender=shutil.which('blender') or r'C:\Program Files\Blender Foundation\Blender 5.1\blender.exe'
  if not Path(blender).is_file(): self.skipTest('Blender unavailable')
  driver=Path(__file__).resolve().parents[1]/'modelbench'/'reference_driver.py'
  code="""import importlib.util,json,sys,bpy
from mathutils import Vector
from bpy_extras.object_utils import world_to_camera_view
p=sys.argv[-1]; x=importlib.util.spec_from_file_location('r',p); m=importlib.util.module_from_spec(x); x.loader.exec_module(m); s=bpy.context.scene; s.render.resolution_x=96; s.render.resolution_y=64; s.render.pixel_aspect_x=64; s.render.pixel_aspect_y=96
for c in ({'model':'perspective','world_to_camera':{'rotation_vector':[.15,-.1,.08],'translation':[.2,-.3,5]},'focal_length':.9,'principal_point':[.4,.6]},{'model':'orthographic','world_to_camera':{'rotation_vector':[.15,-.1,.08]},'scale':.16,'image_offset':[.1,-.1],'principal_point':[.4,.6]}):
 cam,R,t=m._camera({'camera':c}); s.camera=cam; p=Vector((.3,-.2,-.4)); a,_=m._projection(p,R,t,c,96,64); q=world_to_camera_view(s,cam,p); assert max(abs(a[0]-q.x*96),abs(a[1]-(1-q.y)*64))<1e-3
"""
  with tempfile.TemporaryDirectory() as temp:
   probe=Path(temp)/'probe.py'; probe.write_text(code,encoding='utf-8')
   subprocess.run([blender,'--background','--factory-startup','--disable-autoexec','--python',str(probe),'--',str(driver)],check=True,capture_output=True,text=True)
if __name__=='__main__': unittest.main()
