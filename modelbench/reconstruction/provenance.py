"""Engine identity (filesystem reads are isolated here from mathematical routines)."""
import hashlib
import json
from importlib import metadata
from pathlib import Path

from .contracts import ENGINE_VERSION


def json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def implementation_hash():
    digest = hashlib.sha256()
    for source in sorted(Path(__file__).parent.glob("*.py"), key=lambda p: p.name):
        digest.update(source.name.encode() + b"\0" + source.read_bytes())
    return digest.hexdigest()


def dependencies():
    result = {}
    for name in ("numpy", "scipy", "pillow"):
        try:
            result[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            result[name] = None
    return result
