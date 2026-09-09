"""Command interface; every solve/replay owns an immutable execution directory."""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from .contracts import ReconstructionError
from .regression import regress
from .storage import execute, replay, compare_receipts


def _read(path):
    path = Path(path)
    if path.is_dir(): path = path/"result.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _write(value,path):
    text = json.dumps(value,indent=2,sort_keys=True,allow_nan=False)+"\n"
    if path: Path(path).write_text(text,encoding="utf-8")
    else: print(text,end="")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="modelbench-reconstruct")
    commands = parser.add_subparsers(dest="command",required=True)
    p = commands.add_parser("validate");p.add_argument("case");p.add_argument("--output")
    p = commands.add_parser("solve");p.add_argument("case");p.add_argument("--destination","--bundle",dest="destination");p.add_argument("--image",action="append",default=[]);p.add_argument("--output")
    p = commands.add_parser("replay");p.add_argument("execution");p.add_argument("--destination");p.add_argument("--python");p.add_argument("--output")
    p = commands.add_parser("compare");p.add_argument("left");p.add_argument("right");p.add_argument("--migration");p.add_argument("--output")
    p = commands.add_parser("regress");p.add_argument("cases",nargs="*");p.add_argument("--suite");p.add_argument("--left-python",required=True);p.add_argument("--right-python",required=True);p.add_argument("--destination");p.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            from .core import validate_case
            value = {"valid":True,"normalized_case":validate_case(_read(args.case))}
        elif args.command == "solve":
            case_path = Path(args.case)
            destination = args.destination or case_path.parent/(case_path.stem+".execution-"+uuid.uuid4().hex)
            if args.image and all("=" in i for i in args.image):
                images = dict(item.split("=",1) for item in args.image)
                if len(images) != len(args.image): raise ReconstructionError("duplicate image IDs")
            elif any("=" in i for i in args.image): raise ReconstructionError("use ID=path for all images or positional paths for all images")
            else: images = args.image
            # Invalid JSON still gets a failed execution receipt with its source text.
            try: case = _read(case_path)
            except ValueError: case = {"invalid_json_source":case_path.read_text(encoding="utf-8"),"case_version":None}
            root = execute(case,destination,images)
            value = {**_read(root/"result.json"),"execution":str(root)}
        elif args.command == "replay":
            destination = args.destination or str(args.execution)+".replay-"+uuid.uuid4().hex
            root = replay(args.execution,destination,python_executable=args.python)
            value = root if isinstance(root,dict) else {**_read(root/"result.json"),"execution":str(root)}
        elif args.command == "compare":
            value = compare_receipts(_read(args.left),_read(args.right),migration=_read(args.migration) if args.migration else None)
        else:
            cases = args.cases
            if args.suite:
                suite_path = Path(args.suite).absolute()
                cases = _read(suite_path)["cases"]
                for item in cases:
                    if isinstance(item["case"],str): item["case"] = str(suite_path.parent/item["case"])
                    item["images"] = {i:str(suite_path.parent/p) for i,p in item.get("images",{}).items()}
            value = regress(cases,args.left_python,args.right_python,destination=args.destination)
        _write(value,args.output)
        if value.get("status") in {"invalid","failed","unavailable"} or value.get("correctness_passed") is False: return 1
        return 0
    except (OSError,ValueError,KeyError,ReconstructionError) as exc:
        parser.error(str(exc))


if __name__ == "__main__": raise SystemExit(main())
