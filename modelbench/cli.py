from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_task
from .errors import ModelBenchError, StateError
from .feedback import add_feedback
from .orchestrator import continue_run, resume_automated, resume_evaluation, run_new
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
    run.add_argument("task_id", nargs='?')
    run.add_argument('--brief', help='Named-model sparse brief; mutually exclusive with task_id')
    run.add_argument('--notes-file', type=Path)
    run.add_argument('--reference', type=Path, action='append', default=[])
    run.add_argument('--research-mode', choices=['live', 'offline'], default='live')
    run.add_argument("--agent", required=True)
    run.add_argument('--verifier', default='codex', help='Independent verifier profile (default: codex)')
    for role in ('builder', 'verifier'):
        run.add_argument(f'--{role}-model', help='Model override; otherwise project role profile')
        run.add_argument(f'--{role}-effort', help='Reasoning effort override; otherwise project role profile')
    run.add_argument('--budget-seconds', type=int, help='Optional overall invocation budget; no default cycle cap')
    settings = sub.add_parser('set-model', help='Choose or change model and reasoning effort for the next selected-role invocation')
    settings.add_argument('run_id')
    settings.add_argument('--role', required=True, choices=['builder','verifier','both'])
    settings.add_argument('--model', required=True)
    settings.add_argument('--effort', required=True)
    for name, help_text in [('verify','Resume independent verification'), ('cancel','Cancel a run'), ('revise','Create a new run seeded from an owned artifact')]:
        action = sub.add_parser(name, help=help_text)
        action.add_argument('run_id')
        if name == 'revise': action.add_argument('--no-run', action='store_true', help='Create the revision without executing it')
    serve = sub.add_parser('serve', help='Open the loopback review application')
    serve.add_argument('--port', type=int, default=8765)

    status = sub.add_parser("status", help="Show run status")
    status.add_argument("run_id", nargs="?")
    status.add_argument("--details", action="store_true", help="Include the full evidence ledger")

    steer = sub.add_parser("steer", help="Append human feedback to a run")
    steer.add_argument("run_id")
    steer.add_argument("--message", required=True)

    pause = sub.add_parser("pause", help="Request pause at the next checkpoint")
    pause.add_argument("run_id")

    continuing = sub.add_parser("continue", help="Start another agent turn")
    continuing.add_argument("run_id")

    resume = sub.add_parser("resume", help="Resume evaluation from an owned artifact")
    resume.add_argument("run_id")
    resume.add_argument('--additional-budget-seconds', type=int, help='Explicit additional active-work budget for an exhausted automated run')

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
            if bool(args.task_id) == bool(args.brief):
                raise ValueError('Supply exactly one task ID or --brief')
            if args.brief:
                from .briefs import create_brief_task
                args.task_id = create_brief_task(root, args.brief,
                    notes=args.notes_file.read_text(encoding='utf-8-sig') if args.notes_file else '',
                    references=args.reference, research_mode=args.research_mode)
            elif args.notes_file or args.reference:
                raise ValueError('--notes-file and --reference require --brief')
            if args.budget_seconds is not None and args.budget_seconds <= 0:
                raise ValueError('Budget must be positive')
            overrides = {f'{role}_{key}': getattr(args, f'{role}_{key}') for role in ('builder','verifier') for key in ('model','effort') if getattr(args, f'{role}_{key}') is not None}
            _print(run_new(root, args.task_id, args.agent, args.verifier, overrides, args.budget_seconds))
        elif args.command == "status":
            records = status_records(generated, args.run_id)
            if not args.details:
                for record in records:
                    ledger = record.get('models', {})
                    record['models'] = {'pending': ledger.get('profiles', 'Not recorded'),
                        'active_invocations': [i for i in ledger.get('invocations', []) if not i.get('finished_at')],
                        'configuration_revision': ledger.get('configuration_revision', 'Not recorded')}
                    review = record.get('review', {})
                    record['review'] = {'operation': review.get('operation', 'Not recorded'),
                        'latest_verified_revision': next((r['id'] for r in reversed(review.get('revisions', [])) if (r.get('verification') or {}).get('verdict') == 'pass'), None),
                        'unresolved_tasks': [{'id':t['id'], 'status':t['status'], 'instruction':t['instruction']} for t in review.get('tasks', []) if t['status'] != 'verifier_resolved']}
            _print(records)
        elif args.command == "steer":
            run_dir = resolve_run(generated, args.run_id)
            state = load_state(run_dir)
            if state["state"] in {"promoted", "failed"}:
                raise StateError(f"Cannot steer a {state['state']} run")
            _print(add_feedback(run_dir, args.message))
        elif args.command in {'pause', 'cancel'}:
            from .review import lock
            run_dir = resolve_run(generated, args.run_id)
            with lock(run_dir):
                state = load_state(run_dir)
                if state['state'] in {'completed', 'promoted', 'cancelled'}:
                    raise StateError('Run is immutable; create a revision')
                control = read_json(run_dir / 'control.json', {})
                control[args.command + '_requested'] = True
                control['updated_at'] = utc_now()
                atomic_write_json(run_dir / 'control.json', control)
            _print(control)
        elif args.command == 'set-model':
            from .models import change
            _print(change(resolve_run(generated, args.run_id), args.role, args.model, args.effort))
        elif args.command == 'revise':
            from .orchestrator import create_revision
            _print(create_revision(root, args.run_id, execute=not args.no_run))
        elif args.command == 'verify':
            from .orchestrator import resume_automated
            _print(resume_automated(root, args.run_id))
        elif args.command == 'serve':
            from .server import serve
            serve(root, args.port)
        elif args.command == "continue":
            _print(continue_run(root, args.run_id))
        elif args.command == "resume":
            _print(resume_evaluation(root, args.run_id) if args.additional_budget_seconds is None else resume_automated(root, args.run_id, args.additional_budget_seconds))
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

