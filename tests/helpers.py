from __future__ import annotations

import shutil
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from modelbench.blender import STANDARD_VIEWS
from modelbench.util import sha256_file, atomic_write_json


class TemporaryProject:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        source = Path(__file__).resolve().parents[1]
        self.root = Path(self.temp.name) / "ModelingBench"
        self.root.mkdir()
        for name in ("tasks", "agents", "render_profiles", "schemas"):
            shutil.copytree(source / name, self.root / name)
        (self.root / "modelbench").mkdir()
        for filename in ('blender_driver.py', 'measure_driver.py'):
            shutil.copy2(source / 'modelbench' / filename, self.root / 'modelbench' / filename)
        (self.root / "tests" / "fixtures").mkdir(parents=True)
        shutil.copy2(source / "tests" / "fixtures" / "fake_agent.py", self.root / "tests" / "fixtures" / "fake_agent.py")
        shutil.copy2(source / "pyproject.toml", self.root / "pyproject.toml")

    def cleanup(self):
        self.temp.cleanup()


def fake_render(root, run_dir, source, output_dir, task_config, render_profile, *, mode, timeout, views=None, diagnostic_cameras=None, motion_frames=None):
    selected = views or STANDARD_VIEWS
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "invocation.json").write_text("{}\n", encoding="utf-8")
    records = []
    for view in selected:
        group = "orthographic" if view in STANDARD_VIEWS[:6] else "turntable"
        path = output_dir / group / f"{view}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"fake png {view}".encode())
        records.append({
            "path": str(path),
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
            "seconds": 0.001,
            "camera": view, "frame": task_config["frame"].get("evaluation_frame", 1),
            "artifact_sha256": sha256_file(source), "decoded": True,
            "resolution": [render_profile["resolution"], render_profile["resolution"]],
        })
    motion = []
    for frame in motion_frames or []:
        path = output_dir / "motion" / f"front_{frame:04d}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"fake motion frame {frame}".encode())
        motion.append({"path": str(path), "sha256": sha256_file(path), "size": path.stat().st_size,
                       "camera": "front", "frame": frame, "artifact_sha256": sha256_file(source),
                       "decoded": True, "resolution": [render_profile["resolution"], render_profile["resolution"]]})
    for camera in diagnostic_cameras or []:
        path = output_dir / "diagnostic" / f"{camera['name']}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"diagnostic")
    return {
        "ok": True,
        "source_sha256": sha256_file(source),
        "blender": {"version": "5.1.0", "build_hash": "fake"},
        "device": {"name": "NVIDIA GeForce RTX 3090", "type": "OPTIX", "driver": "fake"},
        "render": {"samples": render_profile["samples"], "denoiser": "OPTIX", "view_transform": "ACES 2.0"},
        "inventory": {"evaluated_polygons": 12, "bounds_min": [-0.5, -0.5, 0], "bounds_max": [0.5, 0.5, 1]},
        "renders": records,
        "motion_renders": motion,
    }


def patched_rendering():
    stack = ExitStack()
    stack.enter_context(patch("modelbench.orchestrator.run_blender", fake_render))
    stack.enter_context(patch("modelbench.checkpoints.run_blender", fake_render))
    def routed_render(*args, **kwargs):
        from modelbench.orchestrator import run_blender
        return run_blender(*args, **kwargs)
    stack.enter_context(patch('modelbench.automation.run_blender', routed_render))
    stack.enter_context(patch('modelbench.measurements.evaluate', fake_measurements))
    stack.enter_context(patch('modelbench.verifier.verify', fake_verify))
    return stack


def fake_measurements(root, run_dir, artifact, output_dir, **kwargs):
    result = {'ok': True, 'source_sha256': sha256_file(artifact), 'passed': True,
              'checks': [{'id': 'synthetic_geometry', 'requirement': 'fixture', 'status': 'pass', 'evidence_source': 'measured'}], 'coverage': {'synthetic': True}}
    atomic_write_json(output_dir / 'measurements.json', result)
    return result


def fake_verify(root, run_dir, profile, directory):
    from modelbench import review
    result = {'artifact_sha256': review.current(run_dir)['sha256'], 'acceptance_sha256': review.load(run_dir)['acceptance_sha256'],
              'verdict': 'pass', 'assessment': 'Synthetic fixture verifier; not a real asset assessment', 'findings': [], 'resolutions': []}
    atomic_write_json(directory / 'verification.json', result)
    return result
