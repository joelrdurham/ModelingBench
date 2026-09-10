"""Harness-owned reconstruction evidence and candidate evaluation identities.

This module may import lifecycle code; the reconstruction package never does.
"""
from __future__ import annotations

import copy
import uuid
from pathlib import Path

from .errors import StateError, ValidationError
from .util import atomic_write_json, canonical_hash, file_inventory, read_json, resolve_within, sha256_file

BRIDGE_VERSION = "2.0.0"


def selected_solution(reference, result):
    selected = reference.get("hypothesis_id")
    if selected:
        hypothesis = next((h for h in result.get("camera_hypotheses", []) if h.get("id") == selected), None)
        if not hypothesis:
            raise ValidationError("Selected reconstruction hypothesis does not exist: " + str(selected))
        if not hypothesis.get("valid") or hypothesis.get("mathematical_status") != "solved":
            raise ValidationError("Selected reconstruction hypothesis does not support a fixed identifiable camera")
        # Switch the complete assessment, not just the camera. The immutable
        # execution receipt still describes the original multihypothesis solve.
        chosen = copy.deepcopy(result)
        for key in ("camera", "landmarks", "measurements", "mathematical_status", "convergence_status",
                    "constraints", "numerical_gauges", "landmark_depths", "rectifications", "projections", "depth"):
            chosen[key] = copy.deepcopy(hypothesis.get(key))
        chosen.update(selected_hypothesis=selected, status="solved", reason=None,
                      unresolved_degrees_of_freedom=copy.deepcopy(hypothesis["identifiability"]),
                      conflicts=[c for c in chosen["constraints"] if c["status"] != "satisfied"],
                      rectification=chosen["rectifications"][0] if chosen["rectifications"] else None,
                      diagnostic_artifacts=[],
                      hypothesis_selection={"id": selected, "explicit": True,
                                            "original_execution_hypothesis": result.get("selected_hypothesis")})
        for rect in chosen["rectifications"]: rect["artifacts"] = {}
        return chosen
    return result


def solve_references(run_dir, spec, directory):
    from PIL import Image, ImageOps
    from .discovery import evidence_inventory
    from .reconstruction import execute, validate_case
    from .reconstruction.contracts import ReconstructionError
    sources = {item["id"]: item for item in evidence_inventory(run_dir)}
    records = []
    for index, reference in enumerate(spec.get("references", [])):
        owned = sources[reference["evidence_id"]]
        source = resolve_within(run_dir, owned["local_path"])
        folder = directory / f"reference_{index+1:04d}"
        folder.mkdir(parents=True, exist_ok=False)
        case = copy.deepcopy(reference["case"])
        case["source_hashes"] = {reference["evidence_id"]: owned["sha256"]}
        with Image.open(source) as opened:
            original_size = list(opened.size)
            if case.get("case_version") == "2.0":
                if len(case.get("images", [])) != 1:
                    raise ValidationError("Harness reference cases require exactly one owned image")
                image = case["images"][0]
                if (image.get("width"), image.get("height")) != opened.size or image.get("sha256", owned["sha256"]) != owned["sha256"]:
                    raise ValidationError("Proposed reconstruction image identity differs from owned evidence")
                image["sha256"] = owned["sha256"]
                try: normalized = validate_case(case)
                except ReconstructionError as exc: raise ValidationError(str(exc)) from exc
                metadata = normalized["images"][0]
                rendered = opened.convert("RGB")
                method = {"rotate_90_cw": Image.Transpose.ROTATE_270, "rotate_180": Image.Transpose.ROTATE_180, "rotate_270_cw": Image.Transpose.ROTATE_90}.get(metadata["orientation"])
                if method is not None: rendered = rendered.transpose(method)
                crop = metadata["crop"]
                inputs = {image["id"]: source}
            else:
                rendered = ImageOps.exif_transpose(opened).convert("RGB")
                crop = case.get("image_coordinates", {}).get("crop", [0,0,1,1])
                inputs = [source]
            x,y,w,h = crop
            # Resampling uses continuous pixel edges, exactly as engine coordinates do.
            box = (x*rendered.width,y*rendered.height,(x+w)*rendered.width,(y+h)*rendered.height)
            dimensions = [max(1, round(w*rendered.width)), max(1, round(h*rendered.height))]
            rendered.resize(tuple(dimensions), resample=Image.Resampling.BICUBIC, box=box).save(folder/"reference.png")
        execution = execute(case, folder/"execution", inputs, preserve_environment=True)
        solution = read_json(execution/"result.json")
        selected = selected_solution(reference, solution)
        if reference.get("hypothesis_id") and case.get("case_version") == "2.0":
            from .reconstruction.diagnostics import write_artifacts
            manifest = read_json(execution/"manifest.json")
            copied_inputs = {item["id"]: execution/item["file"] for item in manifest["images"]}
            artifacts = write_artifacts(read_json(execution/"normalized_case.json"), selected, copied_inputs, folder/"selected-diagnostics")
            for artifact in artifacts:
                for key in ("image", "validity_mask"):
                    if key in artifact: artifact[key] = "selected-diagnostics/" + artifact[key]
            selected["diagnostic_artifacts"] = artifacts
            selected["diagnostic_root"] = "."
            for rect in selected.get("rectifications", []):
                rect["artifacts"] = next((a for a in artifacts if a.get("plane_id") == rect["plane_id"]), {})
        else:
            selected["diagnostic_root"] = "execution"
        atomic_write_json(folder/"solution.json", selected)
        records.append({"id": reference["id"], "evidence_id": reference["evidence_id"], "source_sha256": owned["sha256"],
                        "status": selected["status"], "solution": selected, "size": dimensions, "original_size": original_size,
                        "execution_path": execution.relative_to(run_dir).as_posix(),
                        "execution_receipt_sha256": sha256_file(execution/"execution.json"),
                        "result_sha256": sha256_file(execution/"result.json"),
                        "case_sha256": sha256_file(execution/"case.json"),
                        "proposal_case_sha256": canonical_hash(reference["case"]),
                        "selected_hypothesis": selected.get("selected_hypothesis"),
                        "engine_version": solution["receipt"].get("engine_version"),
                        "engine_implementation_sha256": solution["receipt"].get("implementation_sha256"),
                        "feature_binding_sha256": canonical_hash(reference.get("bindings", [])),
                        "bridge_version": BRIDGE_VERSION, "path": folder.relative_to(run_dir).as_posix(),
                        "files": file_inventory(folder)})
    if records: atomic_write_json(directory/"index.json", records)
    return records


def validate_records(run_dir, records):
    from .reconstruction import load_bundle
    from .reconstruction.contracts import ReconstructionError
    for record in records:
        base = resolve_within(run_dir, record["path"])
        if file_inventory(base) != record["files"]:
            raise StateError("Reconstruction evidence changed")
        if record.get("execution_path"):
            selected = read_json(base/"solution.json")
            if selected != record.get("solution") or selected.get("status") != record.get("status") or selected.get("selected_hypothesis") != record.get("selected_hypothesis"):
                raise StateError("Reconstruction ledger differs from owned solution evidence")
            execution = resolve_within(run_dir, record["execution_path"])
            if sha256_file(execution/"execution.json") != record["execution_receipt_sha256"]:
                raise StateError("Reconstruction execution receipt changed")
            try: load_bundle(execution)
            except ReconstructionError as exc: raise StateError(str(exc)) from exc
            for key, name in (("result_sha256","result.json"),("case_sha256","case.json")):
                if sha256_file(execution/name) != record[key]:
                    raise StateError("Approved reconstruction identity changed")


def identities(records):
    keys = ("id", "execution_receipt_sha256", "result_sha256", "case_sha256", "engine_version",
            "engine_implementation_sha256", "selected_hypothesis", "feature_binding_sha256")
    return [{key:r.get(key) for key in keys} for r in records]


def evaluation_identity(run_dir, artifact_sha256, record, spec):
    driver = run_dir/"snapshot/reference_driver.py"
    return {"bridge_version": BRIDGE_VERSION, "artifact_sha256": artifact_sha256,
            "specification_sha256": record["specification"]["sha256"],
            "contract_sha256": record["contract"]["sha256"],
            "reconstructions": identities(record.get("reconstructions", [])),
            "feature_binding_sha256": canonical_hash([{k:v for k,v in ref.items() if k != "case"} for ref in spec.get("references", [])]),
            "reference_driver_sha256": sha256_file(driver) if driver.is_file() else None,
            "reference_evaluator_sha256": sha256_file(Path(__file__).with_name("reference.py")),
            "bridge_implementation_sha256": sha256_file(Path(__file__))}


def assert_evaluation_compatible(run_dir, revision):
    from .discovery import enabled, active_record, active_spec
    if not enabled(run_dir): return
    spec = active_spec(run_dir)
    if not spec.get("references"): return
    record = active_record(run_dir)
    # Pre-v2 completed evidence retains its historical behavior.
    if not any(r.get("execution_receipt_sha256") for r in record.get("reconstructions", [])): return
    expected = evaluation_identity(run_dir, revision["sha256"], record, spec)
    actual = (revision.get("evaluation") or {}).get("reference_identity")
    if actual != expected or (revision.get("evaluation") or {}).get("reference_cache_key") != canonical_hash(expected):
        raise StateError("Reference evaluation identity changed; explicitly reevaluate in a revision run")


def submit_case(run_dir, case, evidence_id):
    """Capability-protected CLI entry point; a submission is a claim awaiting audit."""
    from .state import load_state
    from .discovery import evidence_inventory
    if load_state(run_dir)["state"] in {"completed", "promoted", "cancelled"}:
        raise StateError("Completed runs retain history; create a revision run")
    if evidence_id not in {r["id"] for r in evidence_inventory(run_dir)}:
        raise ValidationError("Reconstruction submission requires owned image evidence")
    directory = run_dir/"discovery/submissions"/("case_"+uuid.uuid4().hex)
    atomic_write_json(directory/"proposal.json", {"case":case,"evidence_id":evidence_id,"authority":"claim_pending_specification_audit"})
    records = solve_references(run_dir, {"references":[{"id":"submitted", "evidence_id":evidence_id,"case":case,"bindings":[]}]}, directory/"reconstructions")
    return {"status":"executed_pending_specification_audit", "path":directory.relative_to(run_dir).as_posix(), "reconstruction":records[0],
            "approval": "bind this case in a specification proposal or amendment"}
