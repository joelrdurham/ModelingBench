"""Pure-Python representation-aware evaluated-mesh checks."""
from __future__ import annotations
import math
from collections import defaultdict, deque
def _s(a,b): return (a[0]-b[0],a[1]-b[1],a[2]-b[2])
def _d(a,b): return sum(x*y for x,y in zip(a,b))
def _x(a,b): return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
def _n(a): return math.sqrt(_d(a,a))
def _box(p): return tuple(min(q[i] for q in p) for i in range(3))+tuple(max(q[i] for q in p) for i in range(3))
def _over(a,b,p=0): return all(a[i]<=b[i+3]+p and b[i]<=a[i+3]+p for i in range(3))
def _segtri(a,b,t,e):
 p,q,r=t; d=_s(b,a); u=_s(q,p); v=_s(r,p); h=_x(d,v); z=_d(u,h)
 if abs(z)<=e: return 'ambiguous' if abs(_d(_x(u,v),_s(a,p)))<=e else False
 k=1/z; w=_s(a,p); i=k*_d(w,h)
 if i<-e or i>1+e: return False
 j=k*_d(d,_x(w,u))
 if j<-e or i+j>1+e: return False
 g=k*_d(v,_x(w,u)); return True if -e<=g<=1+e else False
def _inter(a,b,e):
 uncertain=False
 for t,o in ((a,b),(b,a)):
  for i in range(3):
   h=_segtri(t[i],t[(i+1)%3],o,e)
   if h is True: return 'intersect'
   uncertain|=h=='ambiguous'
 return 'ambiguous' if uncertain else False
def _ray_contains(point, triangles, vertices, eps):
 """Conservative odd/even ray test: boundary hits are unknown."""
 votes=[]
 for direction in ((1.0,.371,.619),(.287,1.0,.509),(.433,.271,1.0)):
  hits=0; uncertain=False
  for ids in triangles:
   a,b,c=(vertices[k] for k in ids); normal=_x(_s(b,a),_s(c,a)); den=_d(normal,direction)
   if abs(den)<=eps: continue
   t=_d(normal,_s(a,point))/den
   if t<=eps: continue
   h=tuple(point[j]+t*direction[j] for j in range(3)); area=_n(normal)
   weights=[_n(_x(_s(b,h),_s(c,h)))/area,_n(_x(_s(c,h),_s(a,h)))/area,_n(_x(_s(a,h),_s(b,h)))/area]
   if abs(sum(weights)-1)>eps*8: continue
   if any(w<=eps for w in weights): uncertain=True; continue
   hits+=1
  if uncertain: return None
  votes.append(bool(hits%2))
 return votes[0] if len(set(votes))==1 else None
def evaluate_mesh(vertices, faces, representation='solid', tolerance=1e-6):
 """Return pass/fail/unknown for plain world-space vertices and face indices.

 A uniform-grid broad phase bounds ordinary triangle-pair work. Pairs sharing an
 edge are omitted as normal mesh adjacency. Coplanar and tolerance-near cases
 become unknown; exact arithmetic and robust ngon tessellation are out of scope.
 """
 e=max(float(tolerance),1e-12); issues=[]; limits=[]; v=[]
 for i,q in enumerate(vertices):
  try: p=tuple(float(q[j]) for j in range(3))
  except (TypeError,ValueError,IndexError): issues.append({'kind':'non_finite_vertex','vertex':i}); continue
  if not all(math.isfinite(x) for x in p): issues.append({'kind':'non_finite_vertex','vertex':i})
  v.append(p)
 if len(v)!=len(vertices) or not v: return {'status':'fail','issues':issues or [{'kind':'empty_mesh'}],'components':[],'limitations':limits}
 if representation not in ('solid','thickened_shell','open_surface'): return {'status':'fail','issues':[{'kind':'unknown_representation'}],'components':[],'limitations':limits}
 tri=[]; users=defaultdict(list); direct=defaultdict(list)
 for fi,f in enumerate(faces):
  if len(f)<3 or any(not isinstance(k,int) or k<0 or k>=len(v) for k in f): issues.append({'kind':'invalid_face','face':fi}); continue
  if len(set(f))!=len(f): issues.append({'kind':'degenerate','face':fi,'reason':'repeated vertex'})
  for a,b in zip(f,f[1:]+f[:1]): users[tuple(sorted((a,b)))].append(fi); direct[tuple(sorted((a,b)))].append((a,b))
  for j in range(1,len(f)-1):
   t=(f[0],f[j],f[j+1])
   if _n(_x(_s(v[t[1]],v[t[0]]),_s(v[t[2]],v[t[0]])))<=e*e: issues.append({'kind':'degenerate','face':fi,'reason':'zero area'}); continue
   tri.append((t,fi))
 boundary=[a for a,b in users.items() if len(b)==1]; non=[a for a,b in users.items() if len(b)>2]; winding=[a for a,b in direct.items() if len(b)==2 and b[0]==b[1]]
 if non: issues.append({'kind':'nonmanifold_edges','count':len(non)})
 if winding: issues.append({'kind':'winding_conflicts','count':len(winding)})
 if boundary and representation in ('solid','thickened_shell'): issues.append({'kind':'boundary_edges','count':len(boundary)})
 boxes=[_box([v[k] for k in t]) for t,_ in tri]; extent=max((max(b[i+3] for b in boxes)-min(b[i] for b in boxes) for i in range(3)),default=0); cell=max(extent/max(round(len(boxes)**(1/3)),1),4*e); grid=defaultdict(list); pairs=set()
 for i,b in enumerate(boxes):
  rr=[range(math.floor(b[k]/cell),math.floor(b[k+3]/cell)+1) for k in range(3)]; cells=[(x,y,z) for x in rr[0] for y in rr[1] for z in rr[2]]
  if len(cells)>4096: cells=[('large',i,0)]
  for c in cells: pairs.update(tuple(sorted((i,j))) for j in grid[c]); grid[c].append(i)
 large=[i for i,b in enumerate(boxes) if math.prod(math.floor(b[k+3]/cell)-math.floor(b[k]/cell)+1 for k in range(3))>4096]
 for i in large: pairs.update(tuple(sorted((i,j))) for j in range(len(tri)) if i!=j)
 hits=0; uncertain=False
 for i,j in pairs:
  a,fa=tri[i]; b,fb=tri[j]
  if fa==fb or len(set(a)&set(b))>=1 or not _over(boxes[i],boxes[j],e): continue
  h=_inter([v[k] for k in a],[v[k] for k in b],e)
  if h=='intersect': hits+=1
  elif h=='ambiguous': uncertain=True
 if hits: issues.append({'kind':'self_intersections','count':hits})
 graph=defaultdict(set)
 for fs in users.values():
  for a in fs: graph[a].update(b for b in fs if b!=a)
 seen=set(); comps=[]
 for root in graph:
  if root in seen: continue
  q=deque([root]); seen.add(root); group=[]
  while q:
   a=q.popleft(); group.append(a)
   for b in graph[a]:
    if b not in seen: seen.add(b); q.append(b)
  points={k for t,f in tri if f in group for k in t}; own=[t for t,f in tri if f in group]; volume=sum(_d(v[a],_x(v[b],v[c]))/6 for a,b,c in own); comps.append({'faces':len(group),'signed_volume':volume,'bounds':_box([v[k] for k in points]),'vertices':sorted(points),'triangles':own})
  if representation in ('solid','thickened_shell') and abs(volume)<=e**3: issues.append({'kind':'zero_signed_volume','component':len(comps)-1})
 if representation=='solid' and not boundary and not non and not winding:
  # A negative shell is a cavity only if every vertex is proven inside a
  # positive shell. AABB nesting alone cannot establish this.
  bad=[]; unproved=[]
  for ci,component in enumerate(comps):
   if component['signed_volume']>=-e**3: continue
   contained=False; uncertain=False
   for outer in comps:
    if outer['signed_volume']<=e**3: continue
    outcomes=[_ray_contains(v[k],outer['triangles'],v,e) for k in component['vertices']]
    if all(value is True for value in outcomes): contained=True; break
    uncertain|=any(value is None for value in outcomes)
   (unproved if uncertain and not contained else bad if not contained else []).append(ci)
  if bad: issues.append({'kind':'inverted_closed_component','components':bad})
  if unproved: limits.append('Cavity containment could not be proven away from a ray-boundary tolerance case.')
 if uncertain: limits.append('Near-parallel or coplanar triangle candidate is within tolerance; self-intersection is unknown.')
 if not tri: issues.append({'kind':'no_non_degenerate_triangles'})
 return {'status':'fail' if issues else ('unknown' if limits else 'pass'),'issues':issues,'components':comps,'triangles':len(tri),'boundary_edges':len(boundary),'limitations':limits}
