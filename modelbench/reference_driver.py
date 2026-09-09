"""Blender-side reference reconstruction renderer and evaluated-point projector."""
from __future__ import annotations
import hashlib, importlib.util, json, math, os, sys, traceback
from pathlib import Path
import bpy
from mathutils import Matrix, Vector

def _write(path,value):
 path=Path(path); temp=path.with_suffix(path.suffix+'.tmp'); temp.write_text(json.dumps(value,indent=2,sort_keys=True)+'\n',encoding='utf-8'); os.replace(temp,path)
def _hash(path):
 h=hashlib.sha256()
 with open(path,'rb') as f:
  for c in iter(lambda:f.read(1048576),b''): h.update(c)
 return h.hexdigest()
def _args(): return json.loads(Path(sys.argv[sys.argv.index('--')+1]).read_text(encoding='utf-8'))
def _driver():
 path=Path(__file__).resolve().with_name('blender_driver.py'); spec=importlib.util.spec_from_file_location('_mb_snapshot_blender_driver',path)
 if spec is None or spec.loader is None: raise RuntimeError('Missing sibling snapshotted blender_driver.py')
 module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
def _matrix(rotation):
 # Rodrigues vector for the documented source-camera convention p_c=R p_w+t.
 angle=math.sqrt(sum(float(x)*float(x) for x in rotation))
 if angle==0: return Matrix.Identity(3)
 axis=Vector(rotation)/angle; return Matrix.Rotation(angle,3,axis)
def _camera(config):
 c=config['camera']; world_to_camera=c['world_to_camera']; R=_matrix(world_to_camera['rotation_vector']); t=Vector(world_to_camera.get('translation',[0.,0.,0.])) if c.get('model')=='perspective' else Vector((0.,0.,0.)); render_t=t if c.get('model')=='perspective' else Vector((0.,0.,100.))
 # Blender uses local -Z forward and +Y up. C converts source camera coordinates
 # (right, down, forward) into Blender camera coordinates (right, up, back).
 C=Matrix.Diagonal((1.,-1.,-1.)); world_to_blender=C@R; translation=C@render_t
 world_to_blender4=world_to_blender.to_4x4(); world_to_blender4.translation=translation
 data=bpy.data.cameras.new('MB_REFERENCE_CAMERA_DATA'); camera=bpy.data.objects.new('MB_REFERENCE_CAMERA',data); bpy.context.scene.collection.objects.link(camera); camera.matrix_world=world_to_blender4.inverted()
 data.type='ORTHO' if c.get('model')=='orthographic' else 'PERSP'
 if data.type=='ORTHO': data.ortho_scale=1.0/float(c['scale'])
 else: data.sensor_width=36.0; data.lens=float(c.get('focal_length',1.0))*data.sensor_width; pp=c.get('principal_point',[.5,.5]); data.shift_x=.5-float(pp[0]); data.shift_y=float(pp[1])-.5
 return camera,R,t
def _projection(point,R,t,c,width,height):
 q=R@point+t
 if c.get('model')=='perspective' and q.z<=0: return None,float(q.z)
 if c.get('model')=='orthographic':
  scale=float(c['scale']); pp=c.get('principal_point',[.5,.5]); offset=c.get('image_offset',[0.,0.]); u=width*(scale*q.x+float(pp[0])+float(offset[0])); v=height*(scale*q.y+float(pp[1])+float(offset[1]))
 else:
  f=float(c['focal_length']); pp=c.get('principal_point',[.5,.5]); u=width*(f*q.x/q.z+float(pp[0])); v=height*(f*q.y/q.z+float(pp[1]))
 return [float(u),float(v)],float(q.z)
def _binding_points(binding,model,depsgraph):
 name=binding['object']; original=bpy.data.objects.get(name)
 if original is None or model.all_objects.get(name) is None: return []
 result=[]
 for inst in depsgraph.object_instances:
  if inst.object.original!=original: continue
  mesh=inst.object.to_mesh(preserve_all_data_layers=False,depsgraph=depsgraph)
  try:
   if not mesh or not mesh.vertices: continue
   if 'vertex_index' in binding:
    i=int(binding['vertex_index'])
    if i<0 or i>=len(mesh.vertices): raise RuntimeError('vertex_index out of evaluated range for '+name)
    local=mesh.vertices[i].co
   elif 'local_point' in binding:
    local=Vector(binding['local_point']); nearest=min(((_point.co-local).length for _point in mesh.vertices),default=math.inf)
    tolerance=float(binding.get('tolerance',1e-5))
    if nearest>tolerance: raise RuntimeError('local_point is not within tolerance of evaluated mesh for '+name)
   else: raise RuntimeError('binding needs vertex_index or local_point')
   result.append((inst.matrix_world@local,inst.object.name))
  finally: inst.object.to_mesh_clear()
 return result
def _main(inv):
 source=Path(inv['source_blend']); expected=inv.get('source_sha256')
 if expected and _hash(source)!=expected: raise RuntimeError('source_sha256 mismatch')
 reference=inv['reference']; width=int(reference['width']); height=int(reference['height']); profile=dict(inv['render_profile']); profile['resolution']=width
 bridge=_driver(); bridge._inspect_source(source); model=bridge._append_model(source)
 if model is None: raise RuntimeError('MODEL collection missing')
 scene=bpy.context.scene; bridge._strict_configuration(profile); scene.render.resolution_x=width; scene.render.resolution_y=height; scene.render.resolution_percentage=100; scene.render.pixel_aspect_x=float(height); scene.render.pixel_aspect_y=float(width); scene.render.image_settings.file_format='PNG'; scene.render.image_settings.color_mode='RGBA'; scene.render.film_transparent=True
 neutral=bridge._neutralize(model,profile.get('neutral',profile)); camera,R,t=_camera(reference); center=Vector((0,0,0)); distance=max((camera.location-center).length,1.0); lights=[]
 try: lights=bridge._lights(camera,center,distance)
 except RuntimeError: pass
 output=Path(inv['output_dir']); output.mkdir(parents=True,exist_ok=True); color=output/'reference.png'; scene.camera=camera; scene.render.filepath=str(color); bpy.ops.render.render(write_still=True)
 depth_path=None; depth_note='Depth EXR was not requested.'
 if reference.get('depth_exr'):
  scene.view_layers[0].use_pass_z=True; depth_path=output/'reference_depth.exr'; scene.use_nodes=True; tree=bpy.data.node_groups.new('MB_REFERENCE_DEPTH','CompositorNodeTree'); scene.compositing_node_group=tree; layers=tree.nodes.new('CompositorNodeRLayers'); output_node=tree.nodes.new('CompositorNodeOutputFile'); slot=output_node.file_output_items.new('FLOAT','depth'); output_node.directory=str(output)+os.sep; output_node.file_name='reference_depth.exr'; slot.override_node_format=True; slot.format.file_format='OPEN_EXR'; slot.format.color_depth='32'; tree.links.new(layers.outputs['Depth'],output_node.inputs[slot.name]); bpy.ops.render.render(write_still=False); depth_note='Compositor Z-pass OpenEXR.'
 depsgraph=bpy.context.evaluated_depsgraph_get(); records=[]
 for binding in reference.get('bindings',[]):
  points=[]
  for point,instance in _binding_points(binding,model,depsgraph):
   pixel,depth=_projection(point,R,t,reference['camera'],width,height); visible=False
   if pixel is not None:
    origin=camera.matrix_world.translation; direction=point-origin; hit=scene.ray_cast(depsgraph,origin,direction.normalized(),distance=direction.length+1e-5); visible=bool(hit[0] and (hit[1]-point).length<=1e-4)
   points.append({'instance':instance,'world':[float(x) for x in point],'pixel':pixel,'depth':depth,'visible':visible})
  records.append({'id':binding['id'],'object':binding['object'],'points':points,'status':'pass' if points else 'unknown'})
 _write(inv['result_path'],{'ok':True,'source_sha256':_hash(source),'camera':{'coordinate_system':'p_camera = R @ p_world + t; camera coordinates are right,+x; down,+y; forward,+z; pixel=[width*(f*x/z+cx), height*(f*y/z+cy)] where intrinsics/principal point are normalized; orthographic uses normalized scale. Blender rendering converts this to local right,+x; up,+y; back,+z.','width':width,'height':height,'model':reference['camera']['model'],'rotation_vector':reference['camera']['world_to_camera']['rotation_vector'],'world_to_camera':reference['camera']['world_to_camera']},'render':{'rgba':str(color),'depth_exr':str(depth_path) if depth_path is not None and depth_path.is_file() else None,'depth_note':depth_note,'transparent_background':True,'neutralized_materials':neutral},'bindings':records,'hash_bound':bool(expected)})
if __name__=='__main__':
 inv=_args()
 try: _main(inv)
 except Exception as exc: _write(inv.get('result_path','reference_error.json'),{'ok':False,'error':str(exc),'traceback':traceback.format_exc()}); raise
