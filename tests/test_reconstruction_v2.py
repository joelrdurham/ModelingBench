"""Independent analytic ground truth; no ground truth is passed as a solver hint."""
from __future__ import annotations

import copy
import json
import math
import subprocess
import sys
import unittest

from modelbench.reconstruction import solve, validate_case
from modelbench.reconstruction.contracts import ReconstructionError
from modelbench.reconstruction.coordinates import to_crop_normalized, from_crop_normalized


def scene(unknown=False):
    world = [[-1,-1,0],[1,-1,0],[-1,1,0],[1,1,0],[0,0,1],[1,0,2],[0,1,3]]
    return {"case_version":"2.0", "units":"m", "images":[{"id":"source", "width":1280,"height":720}],
            "landmarks":[{"id":f"p{i}","coordinates":p} for i,p in enumerate(world)],
            "observations":[{"id":f"o{i}","landmark_id":f"p{i}","image":[613+800*x/(z+5),347+800*y/(z+5)],"sigma":.5} for i,(x,y,z) in enumerate(world)],
            "camera":{"model":"perspective", "intrinsics":{"focal_length":800,"principal_point":[613,347]},
                      "world_to_camera":{} if unknown else {"rotation_vector":[0,0,0],"translation":[0,0,5]}},
            "solver":{"starts":2,"max_nfev":500,"seed":42}}


def plane_unknown(metric=False):
    case = scene()
    case["landmarks"] = [{"id":f"p{i}","coordinates":[None,None,0], "initial":[x*.8,y*.8,0]} for i,(x,y) in enumerate([(0,0),(2,0),(0,1),(2,1)])]
    case["observations"] = [{"id":f"o{i}","landmark_id":f"p{i}","image":[613+160*x,347+160*y],"sigma":.5} for i,(x,y) in enumerate([(0,0),(2,0),(0,1),(2,1)])]
    case["camera"]["world_to_camera"]["translation"] = {"initial":[0,0,4],"bounds":[[-10,-10,1],[10,10,10]]}
    case["measurements"] = [{"id":"width","type":"distance","landmarks":["p0","p1"]}, {"id":"aspect","type":"ratio","segments":[["p0","p1"],["p0","p2"]]}]
    if metric: case["constraints"] = [{"id":"metric","type":"distance","landmarks":["p0","p1"],"value":2,"tolerance":.0001}]
    return case


class CoordinatesTests(unittest.TestCase):
    def test_round_trip_all_frames_rotations_and_rectangular_crop(self):
        for orientation in ("upright","rotate_90_cw","rotate_180","rotate_270_cw"):
            image={"id":"image","width":1280,"height":720,"orientation":orientation,"crop":[.1,.2,.7,.6]}
            for frame in ("source","oriented","crop"):
                for space in ("pixels","normalized"):
                    original=[113.5,201.25] if space=="pixels" else [.33,.67]
                    converted=to_crop_normalized(original,space=space,frame=frame,image=image)
                    back=from_crop_normalized(converted,space=space,frame=frame,image=image)
                    for a,b in zip(original,back): self.assertAlmostEqual(a,b,places=10)

    def test_known_rotation_mapping(self):
        image={"width":100,"height":50,"orientation":"rotate_90_cw","crop":[.1,.2,.8,.6]}
        # Source (20,10) rotates to (40,20), then crop origin is (5,20).
        self.assertEqual(to_crop_normalized([20,10],space="pixels",frame="source",image=image),[.875,0.0])


class EngineV2Tests(unittest.TestCase):
    def test_known_camera_and_joint_unknown_coordinates(self):
        case=scene(True)
        case["landmarks"].append({"id":"depth","coordinates":[1,.5,None],"initial":[1,.5,1],"bounds":[[-10,-10,0],[10,10,10]]})
        case["observations"].append({"id":"depth_obs","landmark_id":"depth","image":[613+800/7,347+400/7],"sigma":.5})
        case["measurements"]=[{"id":"diagonal","type":"distance","landmarks":["p0","p3"]}]
        raw=copy.deepcopy(case)
        result=solve(case)
        self.assertEqual(result["status"],"solved",result)
        self.assertEqual(case,raw)
        self.assertAlmostEqual(result["landmarks"]["depth"][2],2,places=5)
        for a,b in zip(result["camera"]["world_to_camera"]["translation"],[0,0,5]): self.assertAlmostEqual(a,b,places=5)
        self.assertAlmostEqual(result["measurements"][0]["value"],math.sqrt(8))
        json.dumps(result,allow_nan=False)

    def test_missing_scale_still_identifies_ratio(self):
        result=solve(plane_unknown())
        self.assertEqual(result["status"],"underconstrained",result)
        self.assertTrue(result["unresolved_degrees_of_freedom"]["missing_scale"])
        self.assertEqual(result["measurements"][0]["status"],"unresolved")
        self.assertIsNone(result["measurements"][0]["value"])
        self.assertAlmostEqual(result["measurements"][1]["value"],2,places=5)

    def test_metric_anchor_is_active_and_gauge_is_not_evidence(self):
        case=plane_unknown(True)
        case["constraints"].append({"id":"origin_gauge","type":"fixed_coordinate","landmark_id":"p0","coordinates":[0,0,None],"role":"gauge","tolerance":.0001})
        result=solve(case)
        self.assertAlmostEqual(result["measurements"][0]["value"],2,places=6)
        self.assertGreaterEqual(result["unresolved_degrees_of_freedom"]["world_frame_gauge_count"],2)
        self.assertFalse(result["unresolved_degrees_of_freedom"]["numerical_gauge_is_evidence"])
        self.assertNotIn("origin_gauge",[c["id"] for c in result["constraints"]])

    def test_conflicting_anchors_outliers_and_bounds(self):
        case=plane_unknown(True)
        case["constraints"].append({"id":"wrong_metric","type":"distance","landmarks":["p0","p1"],"value":3,"tolerance":.01})
        case["solver"]["loss"]="soft_l1"
        result=solve(case)
        self.assertEqual(result["status"],"inconsistent",result)
        self.assertTrue(any(c["id"]=="wrong_metric" and c["robustly_suppressed"] for c in result["conflicts"]))
        case=scene()
        case["landmarks"][1].update(coordinates=[None,-1,0],initial=[.5,-1,0],bounds=[[-1,-2,-1],[.8,2,1]])
        result=solve(case)
        self.assertEqual(result["status"],"inconsistent")
        self.assertIn("landmark.p1.0",result["camera_hypotheses"][0]["active_bounds"])

    def test_uncertainty_grows_with_observation_noise(self):
        intervals=[]
        for sigma in (.2,2):
            case=scene()
            case["landmarks"][1].update(coordinates=[None,None,0],initial=[.9,-.9,0])
            case["measurements"]=[{"id":"length","type":"distance","landmarks":["p0","p1"]}]
            case["observations"][1]["sigma"]=sigma
            case["solver"].update(starts=1,uncertainty_samples=16)
            result=solve(case)
            uncertainty=result["measurements"][0]["uncertainty"]
            self.assertTrue(uncertainty["available"],result)
            intervals.append(uncertainty["interval"][1]-uncertainty["interval"][0])
        self.assertGreater(intervals[1],intervals[0]*5)
        case["observations"][1]["tolerance"]=case["observations"][1].pop("sigma")
        self.assertFalse(solve(case)["measurements"][0]["uncertainty"]["available"])

    def test_optional_unsupported_required_errors_and_malformed_values(self):
        case=scene(); case["constraints"]=[{"id":"silhouette","type":"silhouette","required":False}]
        self.assertEqual(solve(case)["unsupported_constraints"][0]["id"],"silhouette")
        case["constraints"][0]["required"]=True
        self.assertEqual(solve(case)["status"],"invalid")
        for modify in (lambda c:c["landmarks"].append(None),lambda c:c["camera"]["intrinsics"].update(focal_length=-1),lambda c:c["observations"][0].update(sigma=0)):
            case=scene(); modify(case)
            self.assertEqual(solve(case)["status"],"invalid")

    def test_distinct_positive_depth_hypotheses_are_preserved(self):
        case=scene();case['solver'].update(starts=16)
        case['landmarks']=[{'id':'origin','coordinates':[0,0,0]},{'id':'point','coordinates':[0,0,None],'initial':[0,0,.1],'bounds':[[-1,-1,-3],[1,1,3]]},{'id':'other','coordinates':[0,0,1]}]
        case['observations']=[{'id':'ray','landmark_id':'point','image':[613,347],'sigma':.5}]
        case['constraints']=[{'id':'sphere','type':'distance','landmarks':['origin','point'],'value':2,'tolerance':.001}]
        case['measurements']=[{'id':'gap','type':'distance','landmarks':['other','point']}]
        result=solve(case)
        self.assertEqual(result['status'],'ambiguous',result)
        alternatives={round(h['measurements'][0]['value']) for h in result['camera_hypotheses'] if h['valid']}
        self.assertEqual(alternatives,{1,3})

    def test_bounded_focal_and_principal_point_recovery(self):
        case=scene(True)
        case['camera']['intrinsics']={'focal_length':{'initial':700,'bounds':[400,1200]},'principal_point':{'initial':[620,340],'bounds':[[550,300],[700,450]]}}
        result=solve(case)
        self.assertEqual(result['status'],'solved',result)
        self.assertAlmostEqual(result['camera']['intrinsics']['focal_length'],800,places=3)
        self.assertAlmostEqual(result['camera']['intrinsics']['principal_point'][0],613,places=3)

    def test_geometry_constraints_act_on_unknowns_and_degeneracy_is_reported(self):
        case=scene();case['observations']=[]
        case['landmarks']=[{'id':'a','coordinates':[0,0,0]},{'id':'b','coordinates':[1,0,0]},{'id':'c','coordinates':[0,1,0]},{'id':'d','coordinates':[None,1,None],'initial':[.3,1,.4]}]
        case['constraints']=[{'id':'plane','type':'coplanarity','landmarks':['a','b','c','d']}, {'id':'parallel','type':'parallelism','segments':[['a','c'],['b','d']]}]
        result=solve(case)
        self.assertEqual(result['status'],'solved',result)
        self.assertAlmostEqual(result['landmarks']['d'][0],1,places=6)
        self.assertAlmostEqual(result['landmarks']['d'][2],0,places=6)
        case['landmarks'][1]['coordinates']=[0,0,0]
        self.assertEqual(solve(case)['status'],'inconsistent')

    def test_nonconvergence_positive_depth_and_orthographic(self):
        case=scene(True);case["camera"]["world_to_camera"]["translation"]={"initial":[1,2,8],"bounds":[[-10,-10,1],[10,10,20]]};case["solver"].update(starts=1,max_nfev=1)
        result=solve(case)
        self.assertEqual(result["convergence_status"],"not_converged")
        self.assertNotEqual(result["status"],"solved")
        case=scene();case["camera"]["world_to_camera"]["translation"]=[0,0,-5]
        self.assertEqual(solve(case)["status"],"invalid_geometry")
        case=scene();case["camera"]["model"]="orthographic";case["camera"]["intrinsics"]={"scale":100,"principal_point":[613,347]}
        for point,obs in zip(case["landmarks"],case["observations"]):
            x,y,_=point["coordinates"];obs["image"]=[613+100*x,347+100*y]
        self.assertEqual(solve(case)["status"],"solved")

    def test_rectifies_every_plane_and_rejects_degenerate_plane(self):
        case=scene();case["planes"]=[{"id":"front","landmarks":["p0","p1","p2","p3"],"output_scale":50},{"id":"second","plane_points":[[0,0],[2,0],[0,1],[2,1]],"image_points":[[10,20],[110,20],[10,70],[110,70]],"output_scale":40}]
        result=solve(case)
        self.assertEqual([r["status"] for r in result["rectifications"]],["available","available"])
        self.assertEqual(result["rectifications"][1]["output_size"],[80,40])
        case["planes"][1]["image_points"]=[[0,0]]*4
        self.assertEqual(solve(case)["rectifications"][1]["status"],"unavailable")

    def test_import_does_not_load_harness_or_blender(self):
        code="import sys; from modelbench.reconstruction import solve; assert not any(x in sys.modules for x in ['bpy','modelbench.runs','modelbench.discovery','modelbench.blender'])"
        subprocess.run([sys.executable,"-c",code],check=True)


if __name__=="__main__": unittest.main()
