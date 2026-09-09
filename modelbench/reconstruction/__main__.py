from __future__ import annotations

import argparse
import json
from pathlib import Path

from .cli import main


def _read(path: str): return json.loads(Path(path).read_text(encoding="utf-8"))
def _write(value, path: str | None):
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path: Path(path).write_text(text, encoding="utf-8")
    else: print(text, end="")

def legacy_main() -> int:
    parser = argparse.ArgumentParser(prog="python -m modelbench.reconstruction")
    commands = parser.add_subparsers(dest="command", required=True)
    solve_p = commands.add_parser("solve"); solve_p.add_argument("case"); solve_p.add_argument("--output"); solve_p.add_argument("--bundle"); solve_p.add_argument("--image", action="append", default=[])
    replay_p = commands.add_parser("replay"); replay_p.add_argument("bundle"); replay_p.add_argument("--output")
    compare_p = commands.add_parser("compare"); compare_p.add_argument("left"); compare_p.add_argument("right"); compare_p.add_argument("--output")
    args = parser.parse_args()
    try:
        if args.command == "solve":
            case = _read(args.case)
            if args.bundle: save_bundle(case, args.bundle, args.image)
            _write(solve(case), args.output)
        elif args.command == "replay": _write(solve(load_bundle(args.bundle)), args.output)
        else: _write(compare_receipts(_read(args.left), _read(args.right)), args.output)
    except (OSError, json.JSONDecodeError, ReconstructionError) as exc:
        parser.error(str(exc))
    return 0

if __name__ == "__main__": raise SystemExit(main())
