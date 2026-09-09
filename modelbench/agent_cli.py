from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
from pathlib import Path
from typing import Any

from .checkpoints import create_checkpoint
from .evidence import register_evidence, register_measurement
from .errors import ModelBenchError, ValidationError
from .project import find_project_root
from .runs import RUN_RE
from .state import load_state
from .util import read_json, resolve_within, sha256_bytes


def _context() -> tuple[Path, Path, Path]:
    if os.environ.get("MODELBENCH_AGENT_API") != "1":
        raise ValidationError("modelbench-agent is available only inside an active agent process")
    root = find_project_root()
    run_value = os.environ.get("MODELBENCH_RUN_DIR")
    workspace_value = os.environ.get("MODELBENCH_WORKSPACE")
    if not run_value or not workspace_value:
        raise ValidationError("Agent environment is incomplete")
    run_dir = Path(run_value).resolve()
    workspace = Path(workspace_value).resolve()
    generated = (root / "generated").resolve()
    try:
        relative = run_dir.relative_to(generated)
    except ValueError as exc:
        raise ValidationError("Agent run is outside this project's generated root") from exc
    if len(relative.parts) != 3 or relative.parts[1] != "runs" or not RUN_RE.fullmatch(relative.parts[2]):
        raise ValidationError("Agent run path is not a canonical ModelingBench run")
    if workspace != (run_dir / "workspace").resolve():
        raise ValidationError("Agent workspace does not match the active run")
    state = load_state(run_dir)
    if relative.parts[0] != state.get("task_id") or relative.parts[2] != state.get("run_id"):
        raise ValidationError("Agent run path does not match its recorded identity")
    if state.get("state") not in {"modeling", "researching", "checkpointing"}:
        raise ValidationError("Agent-facing API is unavailable outside an active agent turn")
    token = os.environ.get("MODELBENCH_AGENT_TOKEN")
    capability = read_json(run_dir / ".agent-api.json")
    actual = sha256_bytes(token.encode("utf-8")) if token else ""
    expected = capability.get("token_sha256", "") if isinstance(capability, dict) else ""
    if not expected or not hmac.compare_digest(actual, expected):
        raise ValidationError("Agent capability is missing or invalid")
    return root, run_dir, workspace


def _json_file_in_workspace(workspace: Path, value: str | None, label: str) -> Any:
    if not value:
        return None
    path = resolve_within(workspace, value)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label} must be valid JSON: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="modelbench-agent", description="Run-local API for modeling agents")
    sub = parser.add_subparsers(dest="command", required=True)

    checkpoint = sub.add_parser("checkpoint", help="Own and preview a modeling checkpoint")
    checkpoint.add_argument("--source", required=True)
    checkpoint.add_argument("--phase", required=True)
    checkpoint.add_argument("--views", nargs="*", help="Named evaluation view subset")
    checkpoint.add_argument("--diagnostic-cameras", help="Workspace-relative JSON file containing camera records")
    checkpoint.add_argument("--observations", help="Workspace-relative JSON object with free-form observations")

    evidence = sub.add_parser("evidence", help="Register an agent-researched local file")
    evidence.add_argument("--file", required=True)
    evidence.add_argument("--title")
    evidence.add_argument("--url")
    evidence.add_argument("--media-type")
    evidence.add_argument("--view", default="unknown", choices=["orthographic", "perspective", "section", "plan", "unknown"])
    evidence.add_argument("--notes")
    evidence.add_argument("--confidence", type=float)
    evidence.add_argument("--known-dimension", help="Workspace-relative JSON object describing a calibrated dimension")

    measure = sub.add_parser("measure", help="Register a measurement and its evidence provenance")
    measure.add_argument("--name", required=True)
    measure.add_argument("--value", required=True, type=float)
    measure.add_argument("--unit", required=True)
    measure.add_argument("--provenance", required=True, choices=["authoritative", "derived", "visual_estimate"])
    measure.add_argument("--evidence", nargs="+", required=True)
    measure.add_argument("--notes")
    response = sub.add_parser('respond', help='Record a task-specific builder response; does not resolve the task')
    response.add_argument('--task', required=True)
    response.add_argument('--message', required=True)
    check = sub.add_parser('self-check', help='Run the independent geometry evaluator on a workspace artifact')
    check.add_argument('--source', required=True)
    for name in ('validate-spec', 'propose-spec'):
        spec = sub.add_parser(name, help='Validate or propose an independently audited specification amendment')
        spec.add_argument('--file', required=True)
        if name == 'propose-spec': spec.add_argument('--reason', required=True)
    reconstruction = sub.add_parser('submit-reconstruction', help='Execute a case against owned evidence; approval requires a specification audit')
    reconstruction.add_argument('--file', required=True)
    reconstruction.add_argument('--evidence', required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        root, run_dir, workspace = _context()
        if args.command == 'submit-reconstruction':
            from .reconstruction_bridge import submit_case
            result = submit_case(run_dir, _json_file_in_workspace(workspace, args.file, 'Reconstruction case'), args.evidence)
        elif args.command in {'validate-spec', 'propose-spec'}:
            from .discovery import validate_spec, propose_amendment, evidence_inventory
            spec = _json_file_in_workspace(workspace, args.file, 'Specification')
            result = propose_amendment(run_dir, spec, args.reason) if args.command == 'propose-spec' else {'valid': bool(validate_spec(spec, {e['id'] for e in evidence_inventory(run_dir)}))}
        elif args.command == 'respond':
            from .review import respond
            result = respond(run_dir, args.task, args.message)
        elif args.command == 'self-check':
            from .measurements import evaluate
            from .util import sha256_file
            source = resolve_within(workspace, args.source)
            result = evaluate(root, run_dir, source, workspace / 'self-check' / sha256_file(source), owned=False)
        elif args.command == "checkpoint":
            diagnostics = _json_file_in_workspace(workspace, args.diagnostic_cameras, "Diagnostic cameras") or []
            observations = _json_file_in_workspace(workspace, args.observations, "Observations") or {}
            if not isinstance(diagnostics, list) or not isinstance(observations, dict):
                raise ValidationError("Diagnostic cameras must be an array and observations must be an object")
            result = create_checkpoint(
                root,
                run_dir,
                Path(args.source),
                args.phase,
                views=args.views,
                diagnostic_cameras=diagnostics,
                observations=observations,
            )
        elif args.command == "evidence":
            metadata = read_json(run_dir / "run.json")
            result = register_evidence(
                run_dir,
                resolve_within(workspace, args.file),
                origin="agent_researched",
                title=args.title,
                source_url=args.url,
                media_type=args.media_type,
                view_classification=args.view,
                known_dimension=_json_file_in_workspace(workspace, args.known_dimension, "Known dimension"),
                notes=args.notes,
                confidence=args.confidence,
                byte_limit=int(metadata["agent_profile"]["data"].get("limits", {}).get("research_bytes", 1_000_000_000)),
            )
        else:
            result = register_measurement(
                run_dir,
                name=args.name,
                value=args.value,
                unit=args.unit,
                provenance=args.provenance,
                evidence_ids=args.evidence,
                notes=args.notes,
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except ModelBenchError as exc:
        print(f"modelbench-agent: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
