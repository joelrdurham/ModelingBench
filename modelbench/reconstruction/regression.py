"""Saved-case regression in two explicitly supplied Python environments."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from .contracts import ReconstructionError
from .storage import _write, compare_receipts


def _accuracy(result, truth):
    if not truth: return {"available": False, "passed": None, "checks": []}
    measurements = {m["id"]:m for m in result.get("measurements", [])}
    checks = []
    if "status" in truth:
        checks.append({"id":"status", "passed":result.get("status") == truth["status"], "expected":truth["status"],"actual":result.get("status")})
    for ident, expected in truth.get("measurements", {}).items():
        if isinstance(expected, dict): value,tolerance = expected["value"],expected.get("tolerance", 1e-6)
        else: value,tolerance = expected,1e-6
        actual = measurements.get(ident,{}).get("value")
        checks.append({"id":ident,"expected":value,"actual":actual,"tolerance":tolerance,
                       "passed":actual is not None and abs(actual-value) <= tolerance})
    return {"available":True,"passed":bool(checks) and all(c["passed"] for c in checks),"checks":checks}


def regress(cases, left_python, right_python, *, destination=None):
    """Cases are paths or suite entries {case, images, ground_truth, tolerances}.

    Truth is stored separately and is never passed to either solver process.
    """
    root = Path(destination).absolute() if destination else Path(tempfile.mkdtemp(prefix="reconstruction-regress-"))/"runs"
    if root.exists(): raise ReconstructionError("regression destination must be new")
    root.mkdir(parents=True)
    if not cases: raise ReconstructionError("regression suite must contain cases")
    report = {"left_python":str(Path(left_python).absolute()), "right_python":str(Path(right_python).absolute()), "cases":[]}
    for index, entry in enumerate(cases):
        item = entry if isinstance(entry, dict) else {"case":str(entry)}
        case = item["case"] if isinstance(item["case"], dict) else json.loads(Path(item["case"]).read_text(encoding="utf-8"))
        directory = root/f"case_{index:04d}"; directory.mkdir()
        _write(directory/"case.json", case)
        _write(directory/"ground_truth.json", item.get("ground_truth", {}))
        outputs = {}
        for side, python in (("left", left_python),("right",right_python)):
            destination_run = directory/side
            command = [str(Path(python).absolute()), "-I", "-m", "modelbench.reconstruction", "solve", str(directory/"case.json"), "--destination",str(destination_run)]
            for ident,path in item.get("images",{}).items(): command.extend(["--image",str(ident)+"="+str(Path(path).absolute())])
            try:
                run = subprocess.run(command, text=True, capture_output=True, cwd=directory, timeout=3600, check=False)
                (directory/(side+".stdout.log")).write_text(run.stdout,encoding="utf-8")
                (directory/(side+".stderr.log")).write_text(run.stderr,encoding="utf-8")
                result_path = destination_run/"result.json"
                result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
                outputs[side] = {"returncode":run.returncode,"execution":side,"result":result,"accuracy":_accuracy(result,item.get("ground_truth"))}
            except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
                outputs[side] = {"returncode":None,"result":{},"error":str(exc),"accuracy":{"available":False,"passed":False}}
        l,r = outputs["left"]["result"], outputs["right"]["result"]
        gates = []
        comparison = None
        try:
            comparison = compare_receipts(l,r)
            gates.append({"id":"identity", "passed":True})
            gates.append({"id":"status", "passed":l.get("status") == r.get("status")})
            for ident, change in comparison["measurements"].items():
                tolerance = item.get("tolerances",{}).get(ident, 1e-6)
                passed = (change["left"] is None and change["right"] is None) or (change["delta"] is not None and abs(change["delta"]) <= tolerance)
                gates.append({"id":ident,"passed":passed and change["units"]["left"] == change["units"]["right"],"tolerance":tolerance,"delta":change["delta"]})
        except ReconstructionError as exc:
            gates.append({"id":"identity", "passed":False,"reason":str(exc)})
        for side in ("left","right"):
            gates.append({"id":side+"_execution","passed":outputs[side]["returncode"] == 0 and bool(outputs[side]["result"])})
            if outputs[side]["accuracy"]["available"]: gates.append({"id":side+"_accuracy","passed":outputs[side]["accuracy"]["passed"]})
        report["cases"].append({"id":item.get("id", f"case_{index:04d}"),"runs":outputs,"comparison":comparison,
                                "correctness_gates":gates,"correctness_passed":all(g["passed"] for g in gates),
                                "runtime_ms":{s:outputs[s]["result"].get("receipt",{}).get("runtime_ms") for s in ("left","right")}})
    report["correctness_passed"] = all(c["correctness_passed"] for c in report["cases"])
    report["runtime_policy"] = "Runtime is reported separately and never gates correctness."
    _write(root/"regression.json", report)
    return report
