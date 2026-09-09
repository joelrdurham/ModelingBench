"""Preserve an installable engine wheel and replay with explicitly verified dependencies."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import platform
import subprocess
import sys
import zipfile
from pathlib import Path

from .contracts import ENGINE_VERSION
from .provenance import dependencies, implementation_hash


def current_environment():
    return {"python_executable": sys.executable, "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(), "platform": platform.platform(),
            "machine": platform.machine(), "dependencies": dependencies(), "engine_version": ENGINE_VERSION,
            "implementation_sha256": implementation_hash()}


def preserve(root):
    """Build a pure-Python wheel without build tooling or the harness lifecycle."""
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    name = "modelingbench_reconstruction_engine"
    dist = f"{name}-{ENGINE_VERSION}.dist-info"
    versions = dependencies()
    metadata = ["Metadata-Version: 2.1", "Name: modelingbench-reconstruction-engine", f"Version: {ENGINE_VERSION}", "Requires-Python: >=3.11"]
    metadata.extend(f"Requires-Dist: {key}=={value}" for key, value in versions.items() if value)
    contents = {"modelbench/__init__.py": b'"""Standalone reconstruction engine distribution."""\n',
                f"{dist}/METADATA": ("\n".join(metadata)+"\n").encode(),
                f"{dist}/WHEEL": b"Wheel-Version: 1.0\nGenerator: modelbench.reconstruction\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                f"{dist}/entry_points.txt": b"[console_scripts]\nmodelbench-reconstruct = modelbench.reconstruction.cli:main\n"}
    for source in sorted(Path(__file__).parent.glob("*.py")):
        contents["modelbench/reconstruction/"+source.name] = source.read_bytes()
    rows = []
    for path, content in contents.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        rows.append([path, "sha256="+digest, str(len(content))])
    rows.append([f"{dist}/RECORD", "", ""])
    record = io.StringIO(); csv.writer(record, lineterminator="\n").writerows(rows)
    contents[f"{dist}/RECORD"] = record.getvalue().encode()
    wheel = root/f"{name}-{ENGINE_VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in contents.items():
            info = zipfile.ZipInfo(path, date_time=(2020,1,1,0,0,0))
            archive.writestr(info, content)
    (root/"requirements.lock").write_text("\n".join(f"{k}=={v}" for k,v in versions.items() if v)+"\n", encoding="utf-8")
    (root/"environment.json").write_text(json.dumps(current_environment(), indent=2, sort_keys=True)+"\n", encoding="utf-8")
    return {"wheel": wheel.name, "lock": "requirements.lock", "environment": "environment.json"}


def probe(python):
    script = "import json,platform,importlib.metadata as m; names=['numpy','scipy','pillow']; print(json.dumps({'python_version':platform.python_version(),'python_implementation':platform.python_implementation(),'platform':platform.platform(),'machine':platform.machine(),'dependencies':{n:m.version(n) for n in names}}))"
    try:
        result = subprocess.run([str(python), "-I", "-c", script], text=True, capture_output=True, timeout=30, check=False)
        return json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def matches(actual, expected):
    return bool(actual) and all(actual.get(k) == expected.get(k) for k in ("python_version", "python_implementation", "platform", "machine", "dependencies"))
