"""Background Blender geometry evaluator; it never reads agent-authored reports."""
from __future__ import annotations
import fnmatch,hashlib,json,math,os,sys,traceback
from pathlib import Path
import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

def _args(): return json.loads(Path(sys.argv[sys.argv.index('--')+1]).read_text(encoding='utf-8-sig'))
def _hash(path):
 h=hashlib.sha256()
 with open(path,'rb') as f:
  for c in iter(lambda:f.read(1048576),b''): h.update(c)
 return h.hexdigest()
def _write(path,value):
 p=Path(path); t=p.with_suffix(p.suffix+'.tmp'); t.write_text(json.dumps(value,indent=2,sort_keys=True)+'\n',encoding='utf-8'); os.replace(t,p)
def _check(i,req,status,expected,actual,tol=0,geometry=None,frame=None,instruction=''):
 dev=abs(actual-expected) if isinstance(actual,(int,float)) and isinstance(expected,(int,float)) else None
 return {'id':i,'requirement':req,'status':status,'expected':expected,'actual':actual,'tolerance':tol,'deviation':dev,'geometry':geometry or [],'coordinate_frame':'world_meters','frame':frame,'evidence_source':'measured','instruction':instruction}
def _mesh(obj,dg):
 ev=obj.evaluated_get(dg); me=ev.to_mesh(); verts=[ev.matrix_world@v.co for v in me.vertices]; faces=[p.vertices[:] for p in me.polygons]; ev.to_mesh_clear()
 if not verts: raise RuntimeError('No evaluated mesh vertices: '+obj.name)
 return verts,faces
def _center(obj,dg):
 verts,_=_mesh(obj,dg); return sum(verts,Vector())/len(verts)
def _bvh(obj,dg):
 v,f=_mesh(obj,dg); return BVHTree.FromPolygons(v,f)
def _panel_size(obj,dg):
 verts,_=_mesh(obj,dg); rot=obj.evaluated_get(dg).matrix_world.to_quaternion().inverted(); pts=[rot@v for v in verts]; dims=sorted([max(p[i] for p in pts)-min(p[i] for p in pts) for i in range(3)],reverse=True); return dims[:2]
_MATERIAL_SURFACE_TYPES={'MESH','CURVE','SURFACE','FONT','META'}
_MATERIAL_UNSUPPORTED_VISIBLE_TYPES={'VOLUME','POINTCLOUD','CURVES','GREASEPENCIL'}
def _material_slots(obj,dg):
 """Return polygon-used evaluated object slots, including object-level overrides."""
 ev=obj.evaluated_get(dg)
 if ev.type not in _MATERIAL_SURFACE_TYPES: return []
 mesh=ev.to_mesh()
 try:
  used=sorted({polygon.material_index for polygon in mesh.polygons})
  return [(index,ev.material_slots[index].material if index<len(ev.material_slots) else None) for index in used]
 finally:
  ev.to_mesh_clear()
def _material_class(material,node):
 name=material.name.casefold()
 if node.type=='BSDF_GLASS' or 'glass' in name: return 'glass'
 transmission=next((socket for socket in node.inputs if socket.name in ('Transmission Weight','Transmission')),None)
 if transmission and not transmission.is_linked and float(transmission.default_value)>0: return 'glass'
 if 'rubber' in name: return 'rubber'
 return 'default'
def _shader_state(material):
 """Inspect the output-linked surface shader; material custom properties are ignored."""
 if material is None: return None,{'reason':'missing material assignment'}
 if not material.use_nodes or material.node_tree is None: return None,{'reason':'material has no node-based surface shader'}
 outputs=[n for n in material.node_tree.nodes if n.type=='OUTPUT_MATERIAL' and n.is_active_output]
 if len(outputs)!=1: return None,{'reason':'material has no single active Material Output'}
 surface=outputs[0].inputs.get('Surface')
 if surface is None or not surface.is_linked: return None,{'reason':'Material Output Surface is unlinked'}
 node=surface.links[0].from_node
 if node.type not in ('BSDF_PRINCIPLED','BSDF_GLASS'): return None,{'reason':'unknown output-linked surface shader '+node.bl_idname}
 transmission=next((socket for socket in node.inputs if socket.name in ('Transmission Weight','Transmission')),None)
 if transmission and transmission.is_linked: return None,{'reason':'linked transmission cannot be classified as glass or default'}
 color=node.inputs.get('Color') if node.type=='BSDF_GLASS' else node.inputs.get('Base Color')
 roughness=node.inputs.get('Roughness')
 if color is None or roughness is None: return None,{'reason':'surface shader lacks color or roughness input'}
 if color.is_linked or roughness.is_linked:
  linked=[]
  if color.is_linked: linked.append('base color linked to '+color.links[0].from_node.bl_idname)
  if roughness.is_linked: linked.append('roughness linked to '+roughness.links[0].from_node.bl_idname)
  return None,{'reason':'; '.join(linked)+'; texture/procedural bypass is not accepted'}
 return node,{'base_color':list(color.default_value[:3]),'roughness':float(roughness.default_value),'classification':_material_class(material,node),'shader':node.bl_idname,'roughness_override':material.get('modelbench_roughness_override',False) is True}
def _material_checks(policy,model,dg,frame):
 """Evaluate each polygon-used material assignment at every configured frame."""
 checks=[]; color_expected=list(policy.get('base_color_linear_rgb',[0.18,0.18,0.18])); color_tol=float(policy.get('base_color_tolerance',0.001)); rough_default=float(policy.get('default_roughness',0.36)); rough_tol=float(policy.get('roughness_tolerance',0.01)); ranges=policy.get('roughness_ranges',{}); seen={}
 for obj in model.all_objects:
  for slot,material in _material_slots(obj,dg):
   key=('missing',obj.name,slot) if material is None else ('material',material.name_full)
   seen.setdefault(key,{'material':material,'geometry':[]})['geometry'].append('%s [slot %d]'%(obj.name,slot))
 for key,entry in seen.items():
  material=entry['material']; node,actual=_shader_state(material); geometry=entry['geometry']; label='missing slot '+geometry[0] if material is None else material.name; ident='material_'+hashlib.sha256(repr(key).encode()).hexdigest()[:12]
  if node is None:
   check=_check(ident,'Surface shader policy for '+label,'fail',{'base_color_linear_rgb':color_expected,'roughness':'policy range'},actual,0,geometry,frame,'Evaluated polygon-used material slot and output-linked surface shader; missing, unlinked, and unknown shaders fail explicit policy')
   check['coordinate_frame']='scene_linear_shader_inputs'; check['deviation']={'base_color_linear_rgb':None,'roughness':None}; checks.append(check)
   continue
  category=actual['classification']; low,high=(ranges.get(category) or [rough_default-rough_tol,rough_default+rough_tol])
  color_ok=all(abs(actual['base_color'][i]-color_expected[i])<=color_tol for i in range(3)); rough_ok=float(low)<=actual['roughness']<=float(high)
  if math.isclose(actual['roughness'],0.5,rel_tol=0.0,abs_tol=1e-6) and not actual['roughness_override']: rough_ok=False
  expected={'base_color_linear_rgb':color_expected,'base_color_tolerance':color_tol,'roughness_range':[float(low),float(high)],'classification':category}
  check=_check(ident,'Surface shader policy for '+label,'pass' if color_ok and rough_ok else 'fail',expected,actual,0,geometry,frame,'Evaluated polygon-used material slot and direct output-linked Principled/Glass shader; linked color or roughness inputs are rejected')
  check['coordinate_frame']='scene_linear_shader_inputs'
  check['deviation']={'base_color_linear_rgb':[abs(actual['base_color'][i]-color_expected[i]) for i in range(3)],'roughness':0.0 if rough_ok else (float(low)-actual['roughness'] if actual['roughness']<float(low) else actual['roughness']-float(high))}
  checks.append(check)
 for obj in model.all_objects:
  if not obj.hide_render and obj.type in _MATERIAL_UNSUPPORTED_VISIBLE_TYPES:
   ident='material_unsupported_'+hashlib.sha256(obj.name.encode()).hexdigest()[:12]
   check=_check(ident,'Surface shader policy for unsupported renderable '+obj.name,'fail','supported renderable surface geometry',{'object_type':obj.type},0,[obj.name],frame,'Visible renderable geometry type cannot be converted to an evaluated material-slot mesh')
   check['coordinate_frame']='scene_linear_shader_inputs'; check['deviation']={'base_color_linear_rgb':None,'roughness':None}; checks.append(check)
 return checks
def _circle_upper(b,c,rbd,rcd):
 dx, dz=c.x-b.x,c.z-b.z; d=math.hypot(dx,dz)
 if d==0 or d>rbd+rcd or d<abs(rbd-rcd): return None
 x=(rbd*rbd-rcd*rcd+d*d)/(2*d); h=math.sqrt(max(0,rbd*rbd-x*x)); ux,uz=dx/d,dz/d
 p1=Vector((b.x+x*ux-h*uz,0,b.z+x*uz+h*ux)); p2=Vector((b.x+x*ux+h*uz,0,b.z+x*uz-h*ux)); return max((p1,p2),key=lambda p:p.z)
def _main(inv):
 source=Path(inv['source_blend']); bpy.ops.wm.open_mainfile(filepath=str(source),load_ui=False); model=bpy.data.collections.get('MODEL')
 if model is None: raise RuntimeError('MODEL collection missing')
 task=inv['task']; cfg=task.get('verification',{}); by={o.name:o for o in model.all_objects}; roles={k:by.get(v) for k,v in cfg.get('roles',{}).items()}; frames=cfg.get('frames',list(range(int(bpy.context.scene.frame_start),int(bpy.context.scene.frame_end)+1))); checks=[]; baseline={}
 for name,obj in roles.items():
  if obj is None: checks.append(_check('missing_role_'+name,'Declared role '+name,'unknown','existing geometry',None,0,[],None,'Declared geometry role is missing; dependent measurements are unavailable'))
 actual_range=[int(bpy.context.scene.frame_start),int(bpy.context.scene.frame_end)]; expected_range=[min(frames),max(frames)]; checks.append(_check('scene_frame_range','Scene frame range covers required verification frames','pass' if actual_range[0]<=expected_range[0] and actual_range[1]>=expected_range[1] else 'fail',expected_range,actual_range,0,[],None,'Measured source scene frame_start/frame_end'))
 # Existing anchors/dimensions are always evaluated, even without verification configuration.
 for anchor in task.get('anchors',[]):
  obj=by.get('ANCHOR_'+anchor['name']) or by.get(anchor['name']); expected=anchor['position']; actual=None if not obj else list(obj.matrix_world.translation); dist=None if actual is None else (Vector(actual)-Vector(expected)).length; checks.append(_check('anchor_'+anchor['name'],'Anchor '+anchor['name'],'unknown' if obj is None else ('fail' if dist>float(anchor['tolerance']) else 'pass'),0,dist,float(anchor['tolerance']),[obj.name] if obj else [],None,'Measured anchor transform; missing geometry makes the measurement unavailable'))
 material_policy=cfg.get('materials')
 material_policy_active=isinstance(material_policy,dict) and material_policy.get('enabled') is True # Declarative assembly checks inspect evaluated geometry, never custom properties.
 assembly=task.get('assembly')
 if isinstance(assembly,dict):
  component_roles={c.get('id'):c.get('roles',[]) for c in assembly.get('components',[]) if isinstance(c,dict)}
  for component, declared in component_roles.items():
   found=[roles.get(role) for role in declared]; ok=bool(found) and all(obj is not None and obj.type=='MESH' for obj in found)
   checks.append(_check('assembly_component_'+str(component),'Assembly component '+str(component)+' has evaluated declared geometry','pass' if ok else 'fail','mesh geometry for declared roles',[obj.name if obj else None for obj in found],0,[obj.name for obj in found if obj],None,'Evaluated component role meshes; custom properties are ignored'))
  for interface in assembly.get('interfaces',[]):
   if isinstance(interface,dict):
    obj=roles.get(interface.get('role')); checks.append(_check('assembly_interface_'+str(interface.get('id')),'Assembly interface '+str(interface.get('id'))+' has pivot geometry','pass' if obj is not None and obj.type=='MESH' else 'fail','evaluated pivot mesh',obj.name if obj else None,0,[obj.name] if obj else [],None,'Evaluated interface mesh; custom properties are ignored'))
  ground=assembly.get('ground_resolution')
  if isinstance(ground,dict):
   matches=[obj.name for obj in model.all_objects if obj.type=='MESH' and any(fnmatch.fnmatchcase(obj.name.casefold(),pattern.casefold()) for pattern in ground.get('prohibited_geometry_patterns',[]))]
   # Reject a renamed redundant ground link: a non-environment mesh reaching both fixed pivots.
   pivot_roles=[assembly_interface.get('role') for assembly_interface in assembly.get('interfaces',[]) if isinstance(assembly_interface,dict) and assembly_interface.get('id') in ground.get('interfaces',[])]
   pivots=[roles.get(role) for role in pivot_roles]; pivot_centers=[_center(pivot,dg) if pivot and pivot.type=='MESH' else None for pivot in pivots]
   environment_names={roles.get(role).name for role in component_roles.get(ground.get('environment_component'),[]) if roles.get(role)}
   excluded=environment_names | {pivot.name for pivot in pivots if pivot} 
   spanning=[]; proximity=float(ground.get('redundant_link_tolerance', ground.get('interface_tolerance',0.08)))
   if len(pivot_centers)==2 and all(center is not None for center in pivot_centers):
    for candidate in model.all_objects:
     if candidate.type!='MESH' or candidate.name in excluded or candidate.hide_render: continue
     candidate_bvh=_bvh(candidate,dg); distances=[candidate_bvh.find_nearest(center)[3] if candidate_bvh.find_nearest(center) else None for center in pivot_centers]
     if all(distance is not None and distance<=proximity for distance in distances): spanning.append(candidate.name)
   matches=sorted(set(matches+spanning))
   env=component_roles.get(ground.get('environment_component'),[]); sub=component_roles.get(ground.get('subframe_component'),[])
   env_ok=all(roles.get(role) is not None and roles[role].type=='MESH' for role in env); sub_present=any(roles.get(role) is not None and roles[role].type=='MESH' for role in sub)
   # A named chassis must actually spatially reach both fixed pivot meshes.
   chassis=roles.get(env[0]) if env else None; pivot_roles=[assembly_interface.get('role') for assembly_interface in assembly.get('interfaces',[]) if isinstance(assembly_interface,dict) and assembly_interface.get('id') in ground.get('interfaces',[])]
   spatial=[]
   if chassis and chassis.type=='MESH':
    chassis_bvh=_bvh(chassis,dg)
    for role in pivot_roles:
     pivot=roles.get(role); center=_center(pivot,dg) if pivot and pivot.type=='MESH' else None; nearest=chassis_bvh.find_nearest(center) if center else None; spatial.append(None if nearest is None else nearest[3])
   spatial_ok=len(spatial)==2 and all(distance is not None and distance<=float(ground.get('interface_tolerance',0.08)) for distance in spatial)
   env_ok=env_ok and spatial_ok
   checks.append(_check('assembly_ground_resolution','A-B fixed constraint resolves through a declared environment strategy','pass' if (env_ok or sub_present) and not matches else 'fail',{'environment_component':ground.get('environment_component'),'or_subframe_component':ground.get('subframe_component')},{'environment_geometry':env_ok,'subframe_geometry':sub_present,'interface_distances':spatial,'prohibited_geometry':matches},0,matches,None,'Geometry-derived ground strategy; forbidden redundant plate names fail'))
 for frame in frames:
  bpy.context.scene.frame_set(int(frame)); dg=bpy.context.evaluated_depsgraph_get(); centers={k:(_center(o,dg) if o and o.type=='MESH' else None) for k,o in roles.items()}
  if material_policy_active: checks.extend(_material_checks(material_policy,model,dg,frame))
  for feature in task.get('features',{}).get('repeated',[]):
   if feature.get('kind')!='radial_instances': continue
   patterns=[pattern.casefold() for pattern in feature.get('patterns',[])]; candidates=[obj for obj in model.all_objects if obj.type=='MESH' and not obj.hide_render and any(fnmatch.fnmatchcase(obj.name.casefold(),pattern) for pattern in patterns)]
   center=Vector(feature['center']); axis=int(feature['axis']); transverse=[index for index in range(3) if index!=axis]
   positions=[_center(obj,dg) for obj in candidates]; angles=sorted(math.atan2(point[transverse[1]]-center[transverse[1]],point[transverse[0]]-center[transverse[0]]) for point in positions)
   gaps=[(angles[(index+1)%len(angles)]-angles[index])%(2*math.pi) for index in range(len(angles))] if angles else []
   expected_gap=2*math.pi/float(feature['count']); spacing=max((abs(gap-expected_gap) for gap in gaps),default=None)
   count_ok=len(candidates)==int(feature['count']); spacing_ok=spacing is not None and spacing<=float(feature['spacing_tolerance'])
   checks.append(_check('feature_'+feature['id']+'_count','Repeated feature '+feature['id']+' has exact geometry instance count','pass' if count_ok else 'fail',feature['count'],len(candidates),0,[obj.name for obj in candidates],frame,'Case-insensitive evaluated mesh-name pattern count; custom properties are ignored'))
   checks.append(_check('feature_'+feature['id']+'_spacing','Repeated feature '+feature['id']+' has radial angular spacing','pass' if spacing_ok else 'fail',expected_gap,spacing,float(feature['spacing_tolerance']),[obj.name for obj in candidates],frame,'Evaluated mesh centroids projected around declared axis'))
   radii=[max(math.hypot(vertex[transverse[0]]-center[transverse[0]],vertex[transverse[1]]-center[transverse[1]]) for vertex in _mesh(obj,dg)[0]) for obj in candidates] if candidates else []
   depth=(sum(radii)/len(radii)-float(feature.get('base_radius',0))) if radii else None; depth_ok=depth is not None and abs(depth-float(feature['depth']))<=float(feature['depth_tolerance'])
   checks.append(_check('feature_'+feature['id']+'_depth','Repeated feature '+feature['id']+' radial projection depth','pass' if depth_ok else 'fail',feature['depth'],depth,float(feature['depth_tolerance']),[obj.name for obj in candidates],frame,'Evaluated outer radial vertex projection from declared tread-base radius'))
  for anchor in task.get('anchors',[]):
   obj=by.get('ANCHOR_'+anchor['name']) or by.get(anchor['name']); expected=anchor['position']; actual=None if not obj else list(obj.evaluated_get(dg).matrix_world.translation); dist=None if actual is None else (Vector(actual)-Vector(expected)).length; checks.append(_check('anchor_'+anchor['name']+'_frame','Anchor '+anchor['name']+' evaluated at frame '+str(frame),'fail' if obj is None or dist>float(anchor['tolerance']) else 'pass',0,dist,float(anchor['tolerance']),[obj.name] if obj else [],frame,'Measured evaluated anchor transform'))
  # Existing task dimensions refer to anchor positions; bind them to anchor geometry each frame.
  for dim in task.get('dimensions',[]):
   a=by.get('ANCHOR_'+dim['from_anchor']) or by.get(dim['from_anchor']); b=by.get('ANCHOR_'+dim['to_anchor']) or by.get(dim['to_anchor']); actual=None if not a or not b else (a.matrix_world.translation-b.matrix_world.translation).length; checks.append(_check(dim['name'],dim['name'],'fail' if actual is None or abs(actual-float(dim['value']))>float(dim['tolerance']) else 'pass',float(dim['value']),actual,float(dim['tolerance']),[x.name for x in (a,b) if x],frame,'Measured task anchor distance'))
  for s in cfg.get('dimensions',[]):
   if s.get('frame') is not None and int(s['frame'])!=int(frame): continue
   ident=s['id']; req=s.get('requirement',ident); kind=s.get('kind','distance'); tol=float(s.get('tolerance',0)); names=[]
   if kind in ('distance','angle_xz'):
    a,b=s.get('from_role'),s.get('to_role'); p,q=centers.get(a),centers.get(b); names=[x for x in (roles.get(a),roles.get(b)) if x]
    if p is None or q is None: checks.append(_check(ident,req,'unknown',s.get('expected'),None,tol,[x.name for x in names],frame,'Required role mesh missing; measurement unavailable')); continue
    actual=(p-q).length if kind=='distance' else math.degrees(math.atan2(q.z-p.z,q.x-p.x)); expected=float(s['expected']); checks.append(_check(ident,req,'pass' if abs(actual-expected)<=tol else 'fail',expected,actual,tol,[x.name for x in names],frame,'Evaluated pin-mesh center geometry'))
   elif kind=='panel_size':
    o=roles.get(s.get('role')); actual=None if not o or o.type!='MESH' else _panel_size(o,dg); expected=s['expected']; ok=actual is not None and all(abs(actual[i]-expected[i])<=tol for i in range(2)); checks.append(_check(ident,req,'pass' if ok else 'fail',expected,actual,tol,[o.name] if o else [],frame,'Rotation-aligned evaluated vertex extent with world scale'))
   elif kind=='extent':
    selected=[roles.get(s['role'])] if 'role' in s else list(model.all_objects)
    meshes=[o for o in selected if o and o.type=='MESH']; verts=[v for o in meshes for v in _mesh(o,dg)[0]]; axis=int(s['axis']); expected=float(s['expected'])
    actual=max(v[axis] for v in verts)-min(v[axis] for v in verts) if verts else None
    check=_check(ident,req,'pass' if actual is not None and abs(actual-expected)<=tol else 'fail',expected,actual,tol,[o.name for o in meshes],frame,'Evaluated mesh vertex extent along world axis; transforms and modifiers included')
    check['measurement_axis']=axis; checks.append(check)
   elif kind=='bounds':
    meshes=[o for o in model.all_objects if o.type=='MESH']; verts=[v for o in meshes for v in _mesh(o,dg)[0]]; lo=[min(v[i] for v in verts) for i in range(3)]; hi=[max(v[i] for v in verts) for i in range(3)]; e=s['expected']; ok=all(lo[i]>=e['min'][i]-tol and hi[i]<=e['max'][i]+tol for i in range(3)); checks.append(_check(ident,req,'pass' if ok else 'fail',e,{'min':lo,'max':hi},tol,[o.name for o in meshes],frame,'All evaluated mesh vertices'))
   elif kind=='relative_transform':
    a,b=roles.get(s.get('role')),roles.get(s.get('relative_to_role')); names=[x for x in (a,b) if x]
    if not a or not b: checks.append(_check(ident,req,'unknown',0,None,tol,[x.name for x in names],frame,'Required role missing; measurement unavailable')); continue
    cur=b.evaluated_get(dg).matrix_world.inverted()@a.evaluated_get(dg).matrix_world; base=baseline.setdefault(ident,cur.copy()); delta=max(abs((base-cur)[r][c]) for r in range(4) for c in range(4)); checks.append(_check(ident,req,'pass' if delta<=tol else 'fail',0,delta,tol,[x.name for x in names],frame,'Relative evaluated matrices, including scale/shear'))
   elif kind=='upper_branch':
    b,c,d=centers.get(s.get('b_role')),centers.get(s.get('c_role')),centers.get(s.get('d_role')); names=[roles.get(x) for x in (s.get('b_role'),s.get('c_role'),s.get('d_role')) if roles.get(x)]
    if b is None or c is None or d is None: checks.append(_check(ident,req,'unknown',0,None,tol,[x.name for x in names],frame,'Required pin mesh missing; measurement unavailable')); continue
    expected=_circle_upper(b,c,(d-b).length,(d-c).length); actual=None if expected is None else (d-expected).length; checks.append(_check(ident,req,'pass' if actual is not None and actual<=tol else 'fail',0,actual,tol,[x.name for x in names],frame,'Upper X-Z circle-intersection candidate from evaluated pin centers'))
  if not any(s.get('kind')=='bounds' for s in cfg.get('dimensions',[])):
   meshes=[o for o in model.all_objects if o.type=='MESH']; verts=[v for o in meshes for v in _mesh(o,dg)[0]]; lo=[min(v[i] for v in verts) for i in range(3)]; hi=[max(v[i] for v in verts) for i in range(3)]; limits=task.get('frame',{}); expected={'min':limits.get('bounds_min'),'max':limits.get('bounds_max')}; ok=expected['min'] is not None and all(lo[i]>=expected['min'][i] and hi[i]<=expected['max'][i] for i in range(3)); checks.append(_check('evaluated_bounds','All evaluated meshes remain within task bounds','pass' if ok else 'fail',expected,{'min':lo,'max':hi},0,[o.name for o in meshes],frame,'Unconditional evaluated mesh bounds'))
  for s in cfg.get('intersections',[]):
   a,b=roles.get(s.get('a_role')),roles.get(s.get('b_role')); allowed=bool(s.get('allowed_mating',False)); ident=s['id']; names=[x for x in (a,b) if x]
   if not a or not b or a.type!='MESH' or b.type!='MESH': checks.append(_check(ident,s.get('requirement',ident),'unknown',None,None,0,[x.name for x in names],frame,'Required pair mesh missing; measurement unavailable')); continue
   av,_=_mesh(a,dg); bv,_=_mesh(b,dg); aa,bb=_bvh(a,dg),_bvh(b,dg); overlap=bool(aa.overlap(bb)); upper=min((bb.find_nearest(v)[3] for v in av if bb.find_nearest(v)),default=None); status='pass' if allowed or not overlap else 'fail'; checks.append(_check(ident,s.get('requirement',ident),status,{'intersection_allowed':allowed},{'intersects':overlap,'vertex_surface_upper_bound':upper},0,[a.name,b.name],frame,'BVH overlap plus vertex-to-surface upper-bound clearance; allowed mating does not require contact'))
 for s in cfg.get('controllers',[]):
  o=roles.get(s.get('role')); vals=[]
  try:
   if o:
    for frame in frames: bpy.context.scene.frame_set(int(frame)); vals.append(float(o.path_resolve(s['data_path'])))
  except (ValueError, KeyError, TypeError):
   vals=[]
  actual=[min(vals),max(vals)] if vals else None; expected=s['expected']; tol=float(s.get('tolerance',0)); ok=actual is not None and all(abs(actual[i]-expected[i])<=tol for i in range(2)); checks.append(_check(s['id'],s.get('requirement',s['id']),'pass' if ok else 'fail',expected,actual,tol,[o.name] if o else [],None,'Measured controller source property across every required frame'))
 _write(inv['result_path'],{'ok':True,'source_sha256':_hash(source),'checks':checks,'passed':all(c['status']=='pass' for c in checks),'coverage':{'frames':frames,'roles':{k:(v.name if v else None) for k,v in roles.items()},'missing_roles':[k for k,v in roles.items() if not v],'materials':{'status':'assessed' if material_policy_active else 'unassessed','frames':frames if material_policy_active else [],'convention':'Direct output-linked Principled or Glass shaders only. Glass is inferred from a Glass shader, non-zero literal Principled transmission, or material name; rubber from material name. Linked color/roughness inputs, missing assignments, unlinked outputs, and unknown shaders fail an enabled policy.'},'measurement_limit':'Pivot centers are mesh-vertex centroids; clearance is a vertex-to-surface upper bound, not exact mesh-to-mesh distance.'}})
if __name__=='__main__':
 inv=_args()
 try: _main(inv)
 except Exception as exc: _write(inv['result_path'],{'ok':False,'error':str(exc),'traceback':traceback.format_exc()}); raise
