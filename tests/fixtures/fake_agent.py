from __future__ import annotations

import json
import os
from pathlib import Path

workspace = Path(os.environ["MODELBENCH_WORKSPACE"])
result_file = Path(os.environ["MODELBENCH_RESULT_FILE"])
status = os.environ.get("MODELBENCH_FAKE_STATUS", "submitted")
blend = workspace / "fake_model.blend"
blend.write_bytes(b"BLENDER-v300-fake-modelingbench-fixture")
result = {
    "status": status,
    "blend_path": str(blend) if status != "failed" else None,
    "phase": "final_candidate" if status == "submitted" else "blockout",
    "reported_iterations": 2,
    "feedback_requested": status == "checkpoint",
    "notes": "Deterministic fake agent fixture",
}
result_file.write_text(json.dumps(result), encoding="utf-8")
print(json.dumps({"type": "fake_agent", "status": status}))


if status == 'checkpoint':
    control_path = Path(os.environ['MODELBENCH_RUN_DIR']) / 'control.json'
    control = json.loads(control_path.read_text())
    control['pause_requested'] = True
    control_path.write_text(json.dumps(control), encoding='utf-8')
