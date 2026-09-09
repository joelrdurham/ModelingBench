"""Compatibility facade for v1 imports and the independently versioned v2 engine."""
from __future__ import annotations

import copy
import time
from collections.abc import Mapping

from .contracts import CASE_VERSION, ENGINE_VERSION, ReconstructionError
from .provenance import dependencies, implementation_hash, json_hash

SOLVER_VERSION = ENGINE_VERSION
_implementation_hash = implementation_hash
_json_hash = json_hash
_deps = dependencies
_ENGINE_DIGEST = implementation_hash()
_DEPENDENCIES = dependencies()


def validate_case(case):
    if isinstance(case, Mapping) and case.get("case_version") == "1.0":
        from .legacy import validate_case as validate_legacy
        return validate_legacy(copy.deepcopy(case))
    from .contracts import validate_case as validate_v2
    return validate_v2(case)


def solve(case):
    """Evaluate a case in memory. Never creates files or imports harness/Blender code.

    V1 uses an explicit compatibility executor to preserve its normalized-axis
    semantics and historical constraint behavior. Original inputs are never rewritten.
    """
    started = time.perf_counter()
    receipt = {"engine_version": ENGINE_VERSION, "solver_version": ENGINE_VERSION,
               "implementation_sha256": _ENGINE_DIGEST, "dependencies": dict(_DEPENDENCIES)}
    try:
        if not isinstance(case, Mapping):
            raise ReconstructionError("case must be a JSON object")
        receipt["case_hash"] = json_hash(case)
        receipt["case_version"] = case.get("case_version")
        receipt["source_hashes"] = {**case.get("source_hashes", {}), **{i["id"]: i["sha256"] for i in case.get("images", []) if isinstance(i, dict) and "sha256" in i}}
        if case.get("case_version") == "1.0":
            from .legacy import solve as solve_legacy
            result = solve_legacy(copy.deepcopy(case))
            legacy_receipt = result.pop("receipt")
            receipt.update({k:v for k,v in legacy_receipt.items() if k not in {"solver_version", "case_hash", "implementation_sha256"}})
            receipt["adaptation"] = {"adapter": "v1-compatibility-executor/1", "original_case_hash": json_hash(case),
                                     "coordinate_semantics": "legacy independent normalized x/y axes; crop frame behavior unchanged",
                                     "historical_case_unchanged": True}
            result.update(result_version="2.0", mathematical_status=result["status"],
                          convergence_status="legacy_unavailable", measurements=[],
                          camera_hypotheses=legacy_receipt.get("fit", {}).get("hypotheses", []),
                          constraints=legacy_receipt.get("terms", []), assumptions=case.get("assumptions", []),
                          rectifications=[result["rectification"]] if result.get("rectification") else [],
                          unsupported_constraints=[], diagnostic_artifacts=[])
        else:
            normalized = validate_case(case)
            receipt["effective_settings"] = normalized["solver"]
            try:
                from .solving import solve_validated
            except ImportError as exc:
                raise ReconstructionError("Install numerical dependencies with pip install 'modelingbench[reconstruction]' (or [reference])") from exc
            result = solve_validated(normalized)
            receipt["residual"] = result.pop("residual_summary")
            receipt["fit"] = result.pop("fit_summary")
    except (ReconstructionError, ValueError, TypeError, KeyError, OverflowError) as exc:
        result = {"result_version": "2.0", "status": "invalid", "mathematical_status": "invalid",
                  "convergence_status": "not_run", "reason": str(exc), "camera": None,
                  "camera_hypotheses": [], "measurements": [], "constraints": [], "conflicts": [],
                  "unsupported_constraints": [], "rectifications": [], "rectification": None,
                  "projections": [], "depth": [], "diagnostic_artifacts": []}
    receipt["runtime_ms"] = round((time.perf_counter()-started)*1000, 3)
    result["receipt"] = receipt
    result["provenance_receipt"] = dict(receipt)
    return result


def save_bundle(*args, **kwargs):
    from .storage import save_bundle as save
    return save(*args, **kwargs)


def load_bundle(*args, **kwargs):
    from .storage import load_bundle as load
    return load(*args, **kwargs)


def compare_receipts(*args, **kwargs):
    from .storage import compare_receipts as compare
    return compare(*args, **kwargs)
