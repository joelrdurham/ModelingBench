import os, shutil, subprocess, tempfile, unittest
from pathlib import Path

class ReferenceProjectionIntegrationTests(unittest.TestCase):
 def _blender(self):
  if os.environ.get('MODELBENCH_BLENDER_INTEGRATION')!='1': self.skipTest('set MODELBENCH_BLENDER_INTEGRATION=1')
  blender=shutil.which('blender') or r'C:\Program Files\Blender Foundation\Blender 5.1\blender.exe'
  if not Path(blender).is_file(): self.skipTest('Blender unavailable')
  return blender
 def _probe(self,code):
  blender=self._blender(); driver=Path(__file__).resolve().parents[1]/'modelbench'/'reference_driver.py'
  with tempfile.TemporaryDirectory() as temp:
   probe=Path(temp)/'probe.py'; probe.write_text(code,encoding='utf-8')
   return subprocess.run([blender,'--background','--factory-startup','--disable-autoexec','--python',str(probe),'--',str(driver)],check=True,capture_output=True,text=True)
 def test_rectangular_offset_projection_matches_blender(self):
  code="""import importlib.util,json,sys,bpy
from mathutils import Vector
from bpy_extras.object_utils import world_to_camera_view
p=sys.argv[-1]; x=importlib.util.spec_from_file_location('r',p); m=importlib.util.module_from_spec(x); x.loader.exec_module(m); s=bpy.context.scene; s.render.resolution_x=96; s.render.resolution_y=64; s.render.pixel_aspect_x=64; s.render.pixel_aspect_y=96
for c in ({'model':'perspective','world_to_camera':{'rotation_vector':[.15,-.1,.08],'translation':[.2,-.3,5]},'focal_length':.9,'principal_point':[.4,.6]},{'model':'orthographic','world_to_camera':{'rotation_vector':[.15,-.1,.08]},'scale':.16,'image_offset':[.1,-.1],'principal_point':[.4,.6]}):
 cam,R,t=m._camera({'camera':c}); s.camera=cam; p=Vector((.3,-.2,-.4)); a,_=m._projection(p,R,t,c,96,64); q=world_to_camera_view(s,cam,p); assert max(abs(a[0]-q.x*96),abs(a[1]-(1-q.y)*64))<1e-3
"""
  self._probe(code)
 def test_v2_pixel_intrinsics_projection_matches_blender(self):
  code="""import importlib.util,sys,bpy
from mathutils import Vector
from bpy_extras.object_utils import world_to_camera_view
p=sys.argv[-1]; x=importlib.util.spec_from_file_location('r',p); m=importlib.util.module_from_spec(x); x.loader.exec_module(m)
s=bpy.context.scene; s.render.resolution_x=320; s.render.resolution_y=180; s.render.resolution_percentage=100
points=[Vector((-.7,.4,-.2)),Vector((.3,-.6,.5)),Vector((1.1,.2,-.4)),Vector((-.1,.8,.7))]
for camera in ({'model':'perspective','world_to_camera':{'rotation_vector':[.22,-.31,.14],'translation':[.4,-.25,6.]},'intrinsics':{'units':'pixels','focal_length':213.,'principal_point':[137.,71.],'aspect_ratio':.73}}, {'model':'orthographic','world_to_camera':{'rotation_vector':[-.18,.27,.11],'translation':[.35,.1,3.]},'intrinsics':{'units':'pixels','scale':94.,'principal_point':[201.,119.],'aspect_ratio':1.31}}):
 config={'camera':camera,'width':320,'height':180}; cam,R,t=m._camera(config); s.camera=cam
 for point in points:
  actual=world_to_camera_view(s,cam,point); projected,_=m._projection(point,R,t,camera,320,180)
  assert projected is not None
  error=max(abs(projected[0]-actual.x*320),abs(projected[1]-(1-actual.y)*180))
  assert error<=.5,(camera['model'],point[:],projected,[actual.x*320,(1-actual.y)*180],error)
"""
  self._probe(code)
 def test_local_point_returns_evaluated_nearest_vertex_not_requested_coordinate(self):
  code="""import importlib.util,sys,bpy
from mathutils import Vector
p=sys.argv[-1]; x=importlib.util.spec_from_file_location('r',p); m=importlib.util.module_from_spec(x); x.loader.exec_module(m)
mesh=bpy.data.meshes.new('M'); mesh.from_pydata([(-1,0,0),(1,0,0),(0,2,0)],[],[]); obj=bpy.data.objects.new('subject',mesh); bpy.context.scene.collection.objects.link(obj)
model=bpy.data.collections.new('MODEL'); bpy.context.scene.collection.children.link(model); model.objects.link(obj); bpy.context.scene.collection.objects.unlink(obj)
deps=bpy.context.evaluated_depsgraph_get(); requested=[.8,.05,0]; points=m._binding_points({'id':'p','object':'subject','local_point':requested,'tolerance':.3},model,deps)
assert len(points)==1; world,_=points[0]; assert (world-Vector((1,0,0))).length<1e-7; assert (world-Vector(requested)).length>.1
"""
  self._probe(code)
if __name__=='__main__': unittest.main()
