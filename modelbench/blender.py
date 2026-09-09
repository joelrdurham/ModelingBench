from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .errors import BlenderError, ValidationError
from .util import atomic_write_json, file_inventory, read_json, utc_now

STANDARD_VIEWS = [
    "front", "back", "left", "right", "top", "bottom",
    "azimuth_000", "azimuth_045", "azimuth_090", "azimuth_135",
    "azimuth_180", "azimuth_225", "azimuth_270", "azimuth_315",
]
BLENDER_ENV_KEYS = {
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "NUMBER_OF_PROCESSORS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
}


def _blender_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key.upper() in BLENDER_ENV_KEYS}


def find_blender(profile: dict[str, Any]) -> str:
    configured = profile.get("blender_executable") or os.environ.get("MODELBENCH_BLENDER")
    executable = configured or shutil.which("blender")
    if not executable:
        raise BlenderError("Blender executable not found; set MODELBENCH_BLENDER or blender_executable")
    return str(executable)


def _driver(run_dir: Path) -> Path:
    path = run_dir / "snapshot" / "blender_driver.py"
    if not path.is_file():
        raise BlenderError(f"Blender driver missing: {path}")
    return path


def run_blender(
    root: Path,
    run_dir: Path,
    source: Path,
    output_dir: Path,
    task_config: dict[str, Any],
    render_profile: dict[str, Any],
    *,
    mode: str,
    timeout: int,
    views: list[str] | None = None,
    diagnostic_cameras: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if mode not in {"checkpoint", "final"}:
        raise ValidationError(f"Invalid Blender mode: {mode}")
    output_dir.mkdir(parents=True, exist_ok=False)
    invocation = output_dir / "invocation.json"
    result_path = output_dir / "blender_result.json"
    config = {
        "schema_version": 1,
        "mode": mode,
        "source_blend": str(source.resolve()),
        "output_dir": str(output_dir.resolve()),
        "result_path": str(result_path.resolve()),
        "task": task_config,
        "render_profile": render_profile,
        "views": views or STANDARD_VIEWS,
        "diagnostic_cameras": diagnostic_cameras or [],
        "started_at": utc_now(),
    }
    atomic_write_json(invocation, config)
    executable = find_blender(render_profile)
    command = [
        executable,
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        "--python",
        str(_driver(run_dir)),
        "--",
        str(invocation),
    ]
    log_prefix = run_dir / "logs" / f"blender_{mode}_{output_dir.parent.name}_{output_dir.name}"
    started = utc_now()
    try:
        process = subprocess.run(
            command,
            cwd=root,
            env=_blender_environment(),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        log_prefix.with_suffix(".stdout.log").write_text(exc.stdout or "", encoding="utf-8")
        log_prefix.with_suffix(".stderr.log").write_text(exc.stderr or "", encoding="utf-8")
        raise BlenderError(f"Blender {mode} timed out after {timeout} seconds") from exc
    log_prefix.with_suffix(".stdout.log").write_text(process.stdout, encoding="utf-8")
    log_prefix.with_suffix(".stderr.log").write_text(process.stderr, encoding="utf-8")
    if process.returncode != 0:
        raise BlenderError(f"Blender {mode} failed with exit code {process.returncode}; see {log_prefix.with_suffix('.stderr.log')}")
    result = read_json(result_path)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise BlenderError(f"Blender did not produce a successful result: {result_path}")
    result["command"] = command
    result["process_started_at"] = started
    result["files"] = file_inventory(output_dir, exclude={"invocation.json", "blender_result.json"})
    atomic_write_json(result_path, result)
    return result
