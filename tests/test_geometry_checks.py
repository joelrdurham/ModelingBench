import unittest
from modelbench.geometry_checks import evaluate_mesh
def cube(offset=(0,0,0), inward=False):
 x,y,z=offset; v=[(x+a,y+b,z+c) for a,b,c in ((0,0,0),(1,0,0),(1,1,0),(0,1,0),(0,0,1),(1,0,1),(1,1,1),(0,1,1))]; f=[(0,3,2,1),(4,5,6,7),(0,1,5,4),(1,2,6,5),(2,3,7,6),(3,0,4,7)]
 return v,[tuple(reversed(q)) for q in f] if inward else f
class GeometryChecksTests(unittest.TestCase):
 def test_solid_open_and_cavity(self):
  v,f=cube(); self.assertEqual(evaluate_mesh(v,f)['status'],'pass'); self.assertEqual(evaluate_mesh(v,[(0,1,2)],representation='open_surface')['status'],'pass'); self.assertEqual(evaluate_mesh(v,[(0,1,2)],representation='solid')['status'],'fail')
  iv,inf=cube(inward=True); iv=[tuple(.25+.5*k for k in point) for point in iv]; self.assertEqual(evaluate_mesh(v+iv,f+[tuple(k+len(v) for k in q) for q in inf])['status'],'pass')
 def test_inverted_component_and_self_intersection(self):
  v,f=cube(); iv,inf=cube((3,0,0),True); self.assertTrue(any(i['kind']=='inverted_closed_component' for i in evaluate_mesh(v+iv,f+[tuple(k+len(v) for k in q) for q in inf])['issues']))
  v=[(0,0,0),(2,0,0),(0,2,0),(0,0,-1),(0,0,1),(2,2,0)]; self.assertTrue(any(i['kind']=='self_intersections' for i in evaluate_mesh(v,[(0,1,2),(0,2,5),(3,4,5)],'open_surface')['issues']))
