"""Immutable execution receipts, verified replay, and identity-aware comparison."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from .contracts import ReconstructionError
from .provenance import ENGINE_VERSION, dependencies, implementation_hash, json_hash

FORMAT = "modelbench.reconstruction.execution.v2"


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _safe_path(path, *, file=False, root=None):
    path = Path(path).absolute()
    for component in (path, *path.parents):
        if component.is_symlink() or (component.exists() and getattr(component.lstat(), "st_file_attributes", 0) & 0x400):
            raise ReconstructionError(f"linked/reparse input is unsafe: {component}")
    if file and (not path.is_file() or path.stat().st_nlink != 1):
        raise ReconstructionError(f"regular unlinked input required: {path}")
    if root:
        try: path.resolve().relative_to(Path(root).resolve())
        except ValueError as exc: raise ReconstructionError("input escapes execution") from exc
    return path


def _relative(value):
    if not isinstance(value, str) or not value or chr(92) in value or ":" in value:
        raise ReconstructionError("unsafe recorded path")
    rel = PurePosixPath(value)
    if rel.is_absolute() or any(p in {".", "..", ""} for p in value.split("/")):
        raise ReconstructionError("unsafe recorded path")
    return rel


def _read(path):
    _safe_path(path, file=True)
    try: return json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc: raise ReconstructionError(f"invalid JSON evidence: {path}") from exc


def _write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)+"\n", encoding="utf-8")


def _files(root, exclude=()):
    records = []
    for path in sorted(Path(root).rglob("*")):
        _safe_path(path, root=root)
        if path.is_dir(): continue
        relative = path.relative_to(root).as_posix()
        if relative in exclude: continue
        _safe_path(path, file=True, root=root)
        records.append({"file": relative, "sha256": _sha(path), "size": path.stat().st_size})
    return records


def _solver():
    from . import solve
    return solve


def execute(case, destination, input_images=(), *, preserve_environment=True):
    """Write a new atomic execution directory, including receipts for failed attempts.

    Source paths must be explicitly supplied (never taken from case strings).
    Pure synthetic v2 cases may omit files only when no owned hashes are claimed.
    """
    from .environment import current_environment, preserve
    destination = _safe_path(destination)
    if destination.exists(): raise ReconstructionError("execution destination must be new; completed executions are immutable")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="."+destination.name+"-pending-", dir=destination.parent))
    started = time.perf_counter()
    records, artifact_refs = [], []
    environment = current_environment()
    manifest = {"format": FORMAT, "images": records, "environment": environment, "preserve_environment": bool(preserve_environment)}
    raw = copy.deepcopy(case)
    result = None
    try:
        _write(staging/"case.json", raw)
        manifest["case_hash"] = json_hash(raw)
        if not isinstance(raw, Mapping): raise ReconstructionError("case must be a JSON object")
        from .core import validate_case, _ENGINE_DIGEST
        if _ENGINE_DIGEST != environment['implementation_sha256']:
            raise ReconstructionError('Engine source files changed after import; restart before creating new evidence')
        normalized = validate_case(raw)
        _write(staging/"normalized_case.json", normalized)
        images = normalized.get("images", []) if raw.get("case_version") == "2.0" else []
        if isinstance(input_images, Mapping):
            pairs = [(str(k), Path(v)) for k,v in input_images.items()]
        else:
            values = list(input_images)
            ids = [im["id"] for im in images] if images else [f"image_{i:04d}" for i in range(len(values))]
            if images and values and len(values) != len(images): raise ReconstructionError("input image count does not match case")
            pairs = list(zip(ids, map(Path, values)))
        if images:
            if pairs and {p[0] for p in pairs} != {im["id"] for im in images}:
                raise ReconstructionError("input image IDs must match the case")
            if not pairs and any("sha256" in im for im in images):
                raise ReconstructionError("owned image bytes required for declared image hashes")
        (staging/"images").mkdir()
        for index, (ident, source) in enumerate(pairs):
            _safe_path(source, file=True)
            content = source.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            image = next((im for im in images if im["id"] == ident), None)
            if image:
                from PIL import Image
                import io
                with Image.open(io.BytesIO(content)) as opened:
                    dimensions = opened.size; opened.verify()
                if dimensions != (image["width"],image["height"]) or image.get("sha256", digest) != digest:
                    raise ReconstructionError(f"input image {ident} identity changed (hash or dimensions)")
            target = staging/"images"/f"image_{index:04d}_{digest[:16]}.bin"
            target.write_bytes(content)
            records.append({"id":ident,"file":target.relative_to(staging).as_posix(),"sha256":digest,"source_basename":source.name})
        result = _solver()(raw)
        if images and records and result.get("status") != "invalid":
            from .diagnostics import write_artifacts
            artifact_refs = write_artifacts(normalized, result, {i["id"]:staging/i["file"] for i in records}, staging/"diagnostics")
            for record in artifact_refs:
                for key in ("image", "validity_mask"):
                    if key in record: record[key] = "diagnostics/" + record[key]
            result["diagnostic_artifacts"] = artifact_refs
            for rect in result.get("rectifications", []):
                rect["artifacts"] = next((r for r in artifact_refs if r.get("plane_id") == rect.get("plane_id")), {})
    except Exception as exc:
        # Preserve malformed JSON-shaped input as a diagnostic if canonical JSON is impossible.
        if not (staging/"case.json").is_file():
            (staging/"invalid-input.txt").write_text(repr(raw), encoding="utf-8")
        result = {"result_version":"2.0", "status":"invalid", "mathematical_status":"invalid",
                  "convergence_status":"not_run", "reason":str(exc), "failure_type":type(exc).__name__, "receipt":{}}
    finally:
        if result is None:
            # KeyboardInterrupt/SystemExit: leave a recorded interrupted execution.
            result = {"result_version":"2.0", "status":"failed", "mathematical_status":"failed", "convergence_status":"not_run", "reason":"execution interrupted", "receipt":{}}
        if preserve_environment:
            try: manifest["engine_artifact"] = preserve(staging/"environment")
            except Exception as exc: manifest["environment_preservation_error"] = str(exc)
        receipt = result.setdefault("receipt", {})
        receipt.update(engine_version=ENGINE_VERSION, implementation_sha256=environment["implementation_sha256"],
                       dependencies=dependencies(), case_hash=manifest.get("case_hash"),
                       source_hashes={**(raw.get("source_hashes", {}) if isinstance(raw, Mapping) and isinstance(raw.get("source_hashes", {}), Mapping) else {}),
                                      **{r["id"]:r["sha256"] for r in records}})
        receipt.setdefault("solver_version", ENGINE_VERSION)
        receipt.setdefault("case_version", raw.get("case_version") if isinstance(raw, Mapping) else None)
        receipt.setdefault("runtime_ms", round((time.perf_counter()-started)*1000,3))
        result["provenance_receipt"] = copy.deepcopy(receipt)
        _write(staging/"result.json", result)
        _write(staging/"diagnostics.json", {"constraints":result.get("constraints", []), "conflicts":result.get("conflicts", []), "artifacts":artifact_refs})
        _write(staging/"manifest.json", manifest)
        execution = {"format":FORMAT, "completion_status":"failed" if result.get("status") in {"invalid","failed"} else "completed",
                     "mathematical_status":result.get("mathematical_status", result.get("status")),
                     "environment":environment, "effective_settings":receipt.get("effective_settings", receipt.get("solver_config", {})),
                     "seed":receipt.get("effective_settings", {}).get("seed"), "runtime_ms":round((time.perf_counter()-started)*1000,3),
                     "case_hash":manifest.get("case_hash"), "result_sha256":_sha(staging/"result.json"), "files":_files(staging)}
        _write(staging/"execution.json", execution)
        if destination.exists(): raise ReconstructionError("execution destination appeared during solve; staging evidence retained")
        staging.rename(destination)
    return destination


def load_bundle(bundle):
    root = _safe_path(bundle)
    if not root.is_dir(): raise ReconstructionError("bundle must be a regular directory")
    manifest = _read(root/"manifest.json")
    if manifest.get("format") == FORMAT:
        execution = _read(root/"execution.json")
        if execution.get("format") != FORMAT or execution.get("files") != _files(root, exclude={"execution.json"}):
            raise ReconstructionError("execution file identity changed or required files are missing")
        required = {"result.json", "diagnostics.json", "manifest.json"}
        if not required.issubset({r["file"] for r in execution["files"]}):
            raise ReconstructionError("execution required files are missing")
        if execution.get("result_sha256") != _sha(root/"result.json"):
            raise ReconstructionError("execution result identity changed")
    case = _read(root/"case.json")
    if manifest.get("case_hash") != json_hash(case): raise ReconstructionError("bundle case identity changed")
    ids = set()
    for image in manifest.get("images", []):
        rel = _relative(image.get("file"))
        path = _safe_path(root.joinpath(*rel.parts), file=True, root=root)
        if _sha(path) != image.get("sha256"): raise ReconstructionError("bundle input image identity changed")
        if image.get("id") is not None:
            if image["id"] in ids: raise ReconstructionError("duplicate owned image ID")
            ids.add(image["id"])
    return case


def save_bundle(case, destination, input_images=()):
    """Historical import retained; new bundles are full immutable executions."""
    return execute(case, destination, input_images)


def replay(bundle, destination, *, python_executable=None):
    from .environment import current_environment, matches, probe
    root = _safe_path(bundle)
    case = load_bundle(root)
    manifest = _read(root/"manifest.json")
    if manifest.get("format") != FORMAT:
        return {"status":"unavailable", "reason":"historical bundle has no restorable environment receipt"}
    expected = manifest["environment"]
    python = Path(python_executable or sys.executable).absolute()
    actual = probe(python)
    if not matches(actual, expected):
        return {"status":"unavailable", "reason":"recorded Python/platform/dependencies cannot be restored in the supplied environment", "expected_environment":expected, "available_environment":actual}
    inputs = {r["id"]:str(root/r["file"]) for r in manifest["images"]}
    if python.resolve() == Path(sys.executable).resolve() and current_environment()["implementation_sha256"] == expected["implementation_sha256"]:
        return execute(case, destination, inputs, preserve_environment=manifest.get("preserve_environment", True))
    artifact = manifest.get("engine_artifact")
    if not artifact:
        return {"status":"unavailable", "reason":"recorded engine artifact was not preserved"}
    wheel = _safe_path(root/"environment"/artifact["wheel"], file=True, root=root)
    destination = _safe_path(destination)
    if destination.exists(): raise ReconstructionError("replay destination must be new")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reconstruction-replay-", dir=destination.parent) as temp:
        with zipfile.ZipFile(wheel) as archive:
            for item in archive.infolist():
                _relative(item.filename)
                if stat.S_ISLNK(item.external_attr >> 16): raise ReconstructionError("linked engine archive entry")
            archive.extractall(temp)
        script = "import sys,json;sys.path.insert(0,sys.argv[1]);from modelbench.reconstruction.provenance import implementation_hash;assert implementation_hash()==sys.argv[5];from modelbench.reconstruction import execute;execute(json.load(open(sys.argv[2],encoding='utf-8')),sys.argv[3],json.loads(sys.argv[4]))"
        run = subprocess.run([str(python),"-I","-c",script,temp,str(root/"case.json"),str(destination),json.dumps(inputs),expected["implementation_sha256"]], text=True, capture_output=True, timeout=3600, check=False)
        if run.returncode:
            return {"status":"unavailable", "reason":"recorded engine replay failed", "stderr":run.stderr[-4000:]}
    return destination


def compare_receipts(left, right, *, migration=None):
    a,b = left.get("receipt",left), right.get("receipt",right)
    same_case = bool(a.get("case_hash")) and a.get("case_hash") == b.get("case_hash")
    if a.get("source_hashes") != b.get("source_hashes"):
        if not migration or migration.get("source_hashes") != {"left":a.get("source_hashes"), "right":b.get("source_hashes")}:
            raise ReconstructionError("incompatible receipts: source_hashes differs")
    if not same_case:
        if not migration or migration.get("left_case_hash") != a.get("case_hash") or migration.get("right_case_hash") != b.get("case_hash") or not migration.get("measurement_ids"):
            raise ReconstructionError("incompatible receipts: case_hash differs; explicit migration mapping required")
    def delta(x,y):
        return {"left":x,"right":y,"delta":y-x if isinstance(x,(int,float)) and isinstance(y,(int,float)) else None}
    lm = {m["id"]:m for m in left.get("measurements",[])}
    rm = {m["id"]:m for m in right.get("measurements",[])}
    changes = {}
    for key in sorted(set(lm)|set(rm)):
        other = (migration or {}).get("measurement_ids",{}).get(key,key)
        x,y = lm.get(key,{}),rm.get(other,{})
        changes[key] = {**delta(x.get("value"),y.get("value")),"units":{"left":x.get("units"),"right":y.get("units")},
                        "status":{"left":x.get("status"),"right":y.get("status")},"uncertainty":{"left":x.get("uncertainty"),"right":y.get("uncertainty")}}
    return {"compatible":True,"same_case":same_case,"migration":migration,"versions":{"left":a.get("engine_version",a.get("solver_version")),"right":b.get("engine_version",b.get("solver_version"))},
            "implementation_sha256":{"left":a.get("implementation_sha256"),"right":b.get("implementation_sha256"),"equal":a.get("implementation_sha256")==b.get("implementation_sha256")},
            "status":{"left":left.get("status"),"right":right.get("status")}, "convergence":{"left":left.get("convergence_status"),"right":right.get("convergence_status")},
            "measurements":changes, "rms_normalized":delta((a.get("residual") or {}).get("rms_normalized"),(b.get("residual") or {}).get("rms_normalized")),
            "constraints":{"left":left.get("constraints",[]),"right":right.get("constraints",[])},
            "hypotheses":{"left":left.get("camera_hypotheses",[]),"right":right.get("camera_hypotheses",[])},
            "runtime_ms":delta(a.get("runtime_ms"),b.get("runtime_ms"))}
