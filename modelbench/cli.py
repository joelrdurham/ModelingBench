from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_task
from .errors import ModelBenchError, StateError
from .feedback import add_feedback
from .orchestrator import continue_run, resume_evaluation, run_new
from .project import ensure_generated_root, find_project_root
from .reset import CONFIRMATION, reset_all
from .runs import resolve_run, status_records
from .state import load_state
from .util import atomic_write_json, read_json, utc_now


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="modelbench", description="Iterative AI modeling harness")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate-task", help="Validate a task and its typed inputs")
    validate.add_argument("task_id")

    run = sub.add_parser("run", help="Start a new modeling run")
    run.add_argument("task_id")
    run.add_argument("--agent", required=True)

    status = sub.add_parser("status", help="Show run status")
    status.add_argument("run_id", nargs="?")

    steer = sub.add_parser("steer", help="Append human feedback to a run")
    steer.add_argument("run_id")
    steer.add_argument("--message", required=True)

    pause = sub.add_parser("pause", help="Request pause at the next checkpoint")
    pause.add_argument("run_id")

    continuing = sub.add_parser("continue", help="Start another agent turn")
    continuing.add_argument("run_id")

    resume = sub.add_parser("resume", help="Resume evaluation from an owned artifact")
    resume.add_argument("run_id")

    reset = sub.add_parser("reset", help="Guarded generated-data reset")
    reset.add_argument("--all", action="store_true", required=True)
    reset.add_argument("--dry-run", action="store_true")
    reset.add_argument("--confirm")
    return parser


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        root = find_project_root()
        generated = ensure_generated_root(root)
        if args.command == "validate-task":
            task = load_task(root, args.task_id)
            _print({
                "valid": True,
                "task_id": task.id,
                "digest": task.digest,
                "files": len(task.inventory),
                "inputs": [item.record() for item in task.inputs],
            })
        elif args.command == "run":
            _print(run_new(root, args.task_id, args.agent))
        elif args.command == "status":
            _print(status_records(generated, args.run_id))
        elif args.command == "steer":
            run_dir = resolve_run(generated, args.run_id)
            state = load_state(run_dir)
            if state["state"] in {"promoted", "failed"}:
                raise StateError(f"Cannot steer a {state['state']} run")
            _print(add_feedback(run_dir, args.message))
        elif args.command == "pause":
            run_dir = resolve_run(generated, args.run_id)
            state = load_state(run_dir)
            if state["state"] not in {"modeling", "researching", "checkpointing"}:
                raise StateError(f"Cannot request pause while run is {state['state']}")
            control = read_json(run_dir / "control.json", {})
            control.update({"pause_requested": True, "updated_at": utc_now()})
            atomic_write_json(run_dir / "control.json", control)
            _print({"run": f"{state['task_id']}/{state['run_id']}", "pause_requested": True})
        elif args.command == "continue":
            _print(continue_run(root, args.run_id))
        elif args.command == "resume":
            _print(resume_evaluation(root, args.run_id))
        else:
            paths = reset_all(root, dry_run=args.dry_run, confirmation=args.confirm)
            _print({"dry_run": args.dry_run, "paths": paths, "removed": 0 if args.dry_run else len(paths)})
        return 0
    except KeyboardInterrupt:
        print("modelbench: interrupted", file=sys.stderr)
        return 130
    except ModelBenchError as exc:
        print(f"modelbench: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"modelbench: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

