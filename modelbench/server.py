"""Loopback-only review UI for ModelBench runs.

The server deliberately has no general file browser.  Images are exposed only
when they are registered in a review revision and still match their ledger
hashes.  Mutations are delegated to the command-line interface in supervised
child processes so a browser request never executes a shell command.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import secrets
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .blender import find_blender
from .briefs import MEDIA_TYPES, MAX_REFERENCE_BYTES, create_brief_task
from .errors import ValidationError
from .project import ensure_generated_root, find_project_root
from .runs import resolve_run, status_records
from .system_feedback import add_system_feedback
from .util import is_link, read_json, sha256_file

UI_ROOT = Path(__file__).with_name("ui")
STATIC_FILES = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
MAX_BODY = 16_384
MAX_UPLOAD_BODY = 24 * 1024 * 1024
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


@dataclass
class Worker:
    command: list[str]
    process: subprocess.Popen[str]
    stdout: str = ""
    stderr: str = ""


class ReviewServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, root: Path, port: int = 8765):
        self.root = Path(root).resolve()
        self.generated = ensure_generated_root(self.root)
        self.action_token = secrets.token_urlsafe(32)
        self.assets: dict[str, tuple[Path, str]] = {}
        self.asset_keys: dict[tuple[Any, ...], str] = {}
        self.workers: list[Worker] = []
        self.uploads: dict[str, tuple[Path, str, str]] = {}
        self._worker_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), ReviewHandler)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def asset_index(self, run_name: str) -> list[dict[str, str]]:
        """Return only still-intact registered image evidence for one run."""
        run_dir = resolve_run(self.generated, run_name)
        review = read_json(run_dir / "review.json", {})
        revisions = review.get("revisions", []) if isinstance(review, dict) else []
        indexed: list[dict[str, Any]] = []
        seen: set[str] = set()
        for revision in revisions:
            revision_id = revision.get("id")
            if not isinstance(revision_id, str):
                continue
            for record in revision.get("evidence", []):
                if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                    continue
                path = (run_dir / record["path"]).resolve()
                try:
                    path.relative_to(run_dir.resolve())
                except ValueError:
                    continue
                if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                expected = record.get("sha256")
                if not isinstance(expected, str) or sha256_file(path) != expected:
                    continue
                kind = str(record.get('kind', 'evidence'))
                mode = _evidence_mode(kind, path)
                camera = _camera_name(path)
                frame = _frame_number(path)
                if frame is None:
                    frame = read_json(run_dir / 'snapshot/task.json', {}).get('frame', {}).get('evaluation_frame')
                asset_id = self._register_asset((run_name, "revision", revision_id, expected, mode, camera, frame), path, expected)
                if asset_id in seen:
                    continue
                seen.add(asset_id)
                kind = str(record.get("kind", "evidence"))
                indexed.append({"id": asset_id, "revision": revision_id, "kind": kind, "mode": mode, "name": path.name, "alias": record["path"], "camera": camera, "frame": frame, "url": f"/api/assets/{asset_id}"})
        return indexed

    def _register_asset(self, key: tuple[Any, ...], path: Path, expected: str) -> str:
        asset_id = self.asset_keys.get(key)
        if asset_id is None:
            asset_id = secrets.token_urlsafe(18)
            self.asset_keys[key] = asset_id
        self.assets[asset_id] = (path, expected)
        return asset_id

    def reference_index(self, run_name: str) -> list[dict[str, Any]]:
        """List intact reference-image inputs from the run's frozen task snapshot."""
        run_dir = resolve_run(self.generated, run_name)
        metadata = read_json(run_dir / "run.json", {})
        manifest = metadata.get("input_manifest", {}) if isinstance(metadata, dict) else {}
        inventory = metadata.get("task", {}).get("files", []) if isinstance(metadata, dict) else []
        hashes = {item.get("path"): item.get("sha256") for item in inventory if isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("sha256"), str)}
        root = run_dir / "snapshot" / "task" / "inputs"
        if not root.is_dir() or is_link(root):
            return []
        result: list[dict[str, Any]] = []
        for entry in manifest.get("inputs", []) if isinstance(manifest, dict) else []:
            if not isinstance(entry, dict) or entry.get("role") != "reference_image":
                continue
            input_id, relative = entry.get("id"), entry.get("path")
            if not isinstance(input_id, str) or not isinstance(relative, str):
                continue
            path = (root / relative).resolve()
            try:
                path.relative_to(root.resolve())
            except ValueError:
                continue
            expected = hashes.get("inputs/" + Path(relative).as_posix())
            if not isinstance(expected, str) or not path.is_file() or is_link(path) or path.suffix.lower() not in IMAGE_SUFFIXES or sha256_file(path) != expected:
                continue
            asset_id = self._register_asset((run_name, "reference", input_id, expected), path, expected)
            result.append({"id": asset_id, "url": f"/api/assets/{asset_id}", "name": path.name,
                           "title": entry.get("title") if isinstance(entry.get("title"), str) else input_id,
                           "category": "reference", "notes": entry.get("notes") if isinstance(entry.get("notes"), str) else None,
                           "alias": Path(relative).as_posix()})
        from .evidence import evidence_records
        for entry in evidence_records(run_dir):
            if not isinstance(entry, dict) or entry.get("origin") != "agent_researched" or not str(entry.get("media_type", "")).startswith("image/"):
                continue
            relative, expected = entry.get("local_path"), entry.get("sha256")
            if not isinstance(relative, str) or not isinstance(expected, str):
                continue
            path = (run_dir / relative).resolve()
            try: path.relative_to((run_dir / "evidence" / "files").resolve())
            except ValueError: continue
            if not path.is_file() or is_link(path) or path.suffix.lower() not in IMAGE_SUFFIXES or sha256_file(path) != expected:
                continue
            asset_id = self._register_asset((run_name, "research", entry.get("id"), expected), path, expected)
            result.append({"id": asset_id, "url": f"/api/assets/{asset_id}", "name": path.name,
                           "title": entry.get("title") if isinstance(entry.get("title"), str) else "Research reference",
                           "category": "research", "notes": entry.get("notes") if isinstance(entry.get("notes"), str) else None,
                           "alias": relative})
        return result

    def store_upload(self, *, data: bytes, media_type: str) -> str:
        if media_type not in MEDIA_TYPES.values() or not data or len(data) > MAX_REFERENCE_BYTES: raise ValidationError("unsupported or oversized upload")
        suffix = next(ext for ext, mime in MEDIA_TYPES.items() if mime == media_type); upload_id = "up_" + secrets.token_hex(16)
        directory = self.generated / "uploads" / upload_id; directory.mkdir(parents=True, exist_ok=False); path = directory / ("source" + suffix); path.write_bytes(data)
        if is_link(path) or not path.is_file(): raise ValidationError("could not safely store upload")
        self.uploads[upload_id] = (path, sha256_file(path), media_type); return upload_id

    def upload_path(self, upload_id: str) -> Path:
        record = self.uploads.get(upload_id)
        if record is None: raise ValidationError("unknown upload")
        path, expected, _ = record
        if not path.is_file() or is_link(path) or sha256_file(path) != expected: raise ValidationError("upload is unavailable")
        return path

    def revision_provenance(self, run_name: str) -> dict[str, dict[str, Any]]:
        """Builder logs are not revision-bound; verifier receipts are when registered and intact."""
        run_dir = resolve_run(self.generated, run_name)
        review = read_json(run_dir / "review.json", {})
        result: dict[str, dict[str, Any]] = {}
        for revision in review.get("revisions", []) if isinstance(review, dict) else []:
            if not isinstance(revision, dict) or not isinstance(revision.get("id"), str):
                continue
            revision_id = revision["id"]
            receipt_path = run_dir / "revisions" / revision_id / "verification" / "invocation.json"
            relative = receipt_path.relative_to(run_dir).as_posix()
            record = next((item for item in revision.get("evidence", []) if isinstance(item, dict) and item.get("path") == relative and isinstance(item.get("sha256"), str)), None)
            receipt = read_json(receipt_path, {}) if record and receipt_path.is_file() and sha256_file(receipt_path) == record["sha256"] else {}
            builder = revision.get("builder_provenance")
            exact = builder if isinstance(builder, dict) and builder.get("artifact_sha256") == revision.get("sha256") else None
            context = None
            if exact is None and isinstance(revision.get("at"), str):
                ledger = read_json(run_dir / "model-settings.json", {})
                candidates = [item for item in ledger.get("invocations", []) if isinstance(item, dict) and item.get("role") == "builder" and isinstance(item.get("requested_at"), str) and item["requested_at"] <= revision["at"]]
                if candidates:
                    context = {"scope": "run", "invocation": max(candidates, key=lambda item: item["requested_at"])}
            result[revision_id] = {"builder": exact, "builder_context": context,
                                  "verifier": receipt.get("settings") if isinstance(receipt.get("settings"), dict) else None}
        return result

    def owned_artifact(self, run_name: str) -> Path:
        run_dir = resolve_run(self.generated, run_name)
        artifact = read_json(run_dir / "state.json", {}).get("artifact")
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str) or not isinstance(artifact.get("sha256"), str):
            raise ValueError("no owned artifact is available")
        path = (run_dir / artifact["path"]).resolve()
        try:
            path.relative_to(run_dir.resolve())
        except ValueError as exc:
            raise ValueError("unsafe owned artifact") from exc
        if not path.is_file() or sha256_file(path) != artifact["sha256"]:
            raise ValueError("owned artifact hash changed")
        return path
    def start_worker(self, args: list[str]) -> dict[str, Any]:
        command = [sys.executable, "-m", "modelbench", *args]
        process = subprocess.Popen(command, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace")
        worker = Worker(command, process)
        with self._worker_lock:
            self.workers.append(worker)
        threading.Thread(target=self._collect, args=(worker,), daemon=True).start()
        return {"accepted": True, "pid": process.pid}

    def _collect(self, worker: Worker) -> None:
        stdout, stderr = worker.process.communicate()
        worker.stdout, worker.stderr = stdout[-20_000:], stderr[-20_000:]

    def worker_records(self) -> list[dict[str, Any]]:
        with self._worker_lock:
            return [{"command": worker.command[3:], "pid": worker.process.pid, "returncode": worker.process.poll(),
                     "stdout": worker.stdout, "stderr": worker.stderr} for worker in self.workers[-20:]]


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer

    def handle(self):
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # Browser navigation may close an in-flight response.

    def log_message(self, format: str, *args: object) -> None:
        return

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        return host in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _deny(self, status: HTTPStatus = HTTPStatus.FORBIDDEN, message: str = "forbidden") -> None:
        self._json({"error": message}, status)

    def do_GET(self) -> None:
        if not self._host_ok():
            self._deny()
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            try:
                records = status_records(self.server.generated)
                for record in records:
                    record["assets"] = self.server.asset_index(record["run"])
                    record["references"] = self.server.reference_index(record["run"])
                    record["provenance"] = self.server.revision_provenance(record["run"])
                    try:
                        from .discovery import summary
                        record["discovery"] = summary(resolve_run(self.server.generated, record["run"]))
                    except (ImportError, AttributeError, ValidationError):
                        record["discovery"] = {}
                self._json({"tasks": sorted(path.name for path in (self.server.root / "tasks").iterdir() if path.is_dir()), "runs": records, "workers": self.server.worker_records()})
            except Exception as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path.startswith("/api/assets/"):
            asset = self.server.assets.get(parsed.path.rsplit("/", 1)[-1])
            if asset is None:
                self._deny(HTTPStatus.NOT_FOUND, "asset not found")
                return
            path, expected = asset
            if not path.is_file() or sha256_file(path) != expected:
                self._deny(HTTPStatus.NOT_FOUND, "asset unavailable")
                return
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        filename = STATIC_FILES.get(parsed.path)
        if filename is None:
            self._deny(HTTPStatus.NOT_FOUND, "not found")
            return
        data = (UI_ROOT / filename).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(filename)[0] or "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/api/action", "/api/upload"}:
            self._deny(HTTPStatus.NOT_FOUND, "not found")
            return
        if not self._host_ok() or self.headers.get("Origin") != self.server.origin or self.headers.get("X-ModelBench-Action") != self.server.action_token:
            self._deny()
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self._deny(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON required")
            return
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._deny(HTTPStatus.BAD_REQUEST, "invalid request size")
            return
        if size <= 0 or size > (MAX_UPLOAD_BODY if path == "/api/upload" else MAX_BODY):
            self._deny(HTTPStatus.BAD_REQUEST, "invalid request size")
            return
        try:
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("object required")
            response = self._upload(body) if path == "/api/upload" else self._action(body)
            self._json(response, HTTPStatus.ACCEPTED)
        except (ValueError, KeyError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _action(self, body: dict[str, Any]) -> dict[str, Any]:
        action = body.get("action")
        if action == "run":
            command = ["run", _text(body, "task"), "--agent", _text(body, "agent"), "--verifier", _text(body, "verifier")]
            for role in ("builder", "verifier"):
                key = f"{role}_effort"
                if key in body:
                    effort = _text(body, key)
                    if effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
                        raise ValueError(f"Unsupported {role} effort")
                    command += [f"--{role}-effort", effort]
            command += _budget_args(body)
            return self.server.start_worker(command)
        if action == "brief-run":
            references = body.get("references", [])
            if not isinstance(references, list) or len(references) > 16 or not all(isinstance(item, str) for item in references): raise ValueError("references must be a list of upload IDs")
            task_id = create_brief_task(self.server.root, _text(body, "brief"), _optional_text(body, "notes", 16000), [self.server.upload_path(item) for item in references], _research_mode(body))
            command = ["run", task_id, "--agent", _text(body, "agent"), "--verifier", _text(body, "verifier")]
            for role in ("builder", "verifier"):
                key = f"{role}_effort"
                if key in body:
                    effort = _text(body, key)
                    if effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}: raise ValueError(f"Unsupported {role} effort")
                    command += [f"--{role}-effort", effort]
            command += _budget_args(body)
            return {**self.server.start_worker(command), "task": task_id}
        run = _text(body, "run")
        if action == "system-feedback":
            run_dir = resolve_run(self.server.generated, run)
            if read_json(run_dir / "state.json", {}).get("state") != "awaiting_feedback":
                raise ValueError("system feedback is available only while awaiting feedback")
            event = add_system_feedback(self.server.generated, run, _text(body, "message"))
            return {"accepted": True, "id": event["id"]}
        if action == "open":
            run_dir = resolve_run(self.server.generated, run)
            profile = read_json(run_dir / "run.json", {}).get("render_profiles", {}).get("final", {}).get("data", {})
            artifact = self.server.owned_artifact(run)
            process = subprocess.Popen([find_blender(profile), str(artifact)], cwd=self.server.root)
            return {"accepted": True, "pid": process.pid}
        if action in {"resume", "pause", "cancel", "revise", "verify"}:
            command = [action, run]
            if action == "resume" and "additional_budget_seconds" in body:
                import math
                grant = body["additional_budget_seconds"]
                if isinstance(grant, bool) or not isinstance(grant, (int, float)) or not math.isfinite(grant) or grant <= 0:
                    raise ValueError("additional_budget_seconds must be positive and finite")
                command += ["--additional-budget-seconds", str(grant)]
            return self.server.start_worker(command)
        if action == "steer":
            return self.server.start_worker(["steer", run, "--message", _text(body, "message")])
        if action == "set-model":
            return self.server.start_worker(["set-model", run, "--role", _text(body, "role"), "--model", _text(body, "model"), "--effort", _text(body, "effort")])
        raise ValueError("unsupported action")

    def _upload(self, body: dict[str, Any]) -> dict[str, Any]:
        media_type, encoded = body.get("media_type"), body.get("data")
        if not isinstance(media_type, str) or media_type not in MEDIA_TYPES.values() or not isinstance(encoded, str): raise ValueError("supported media_type and base64 data are required")
        if len(encoded) > (MAX_REFERENCE_BYTES * 4 // 3) + 8: raise ValueError("upload exceeds size limit")
        try: data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc: raise ValueError("invalid base64 upload") from exc
        return {"id": self.server.store_upload(data=data, media_type=media_type)}

def _text(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(f"{name} is required")
    return value.strip()

def _optional_text(body: dict[str, Any], name: str, limit: int) -> str:
    value = body.get(name, "")
    if not isinstance(value, str) or len(value) > limit: raise ValueError(f"{name} must be text up to {limit} characters")
    return value.strip()

def _budget_args(body: dict[str, Any]) -> list[str]:
    value = body.get("budget_seconds")
    if value is None: return []
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 86_400:
        raise ValueError("budget_seconds must be a positive integer up to 86400")
    return ["--budget-seconds", str(value)]

def _research_mode(body: dict[str, Any]) -> str:
    value = body.get("research_mode", "live")
    if value not in {"live", "offline"}: raise ValueError("research_mode must be live or offline")
    return value

def _frame_number(path: Path) -> int | None:
    match = re.search(r"(?:frame|f)[_-]?(\d{1,6})(?:\D|$)", path.stem, re.IGNORECASE) or re.match(r"^.*_(\d{4,6})$", path.stem)
    return int(match.group(1)) if match else None


def _evidence_mode(kind: str, path: Path) -> str:
    if kind in {'provided', 'builder_claim', 'workflow_record'}:
        return {'provided': 'reference', 'builder_claim': 'builder evidence', 'workflow_record': 'checkpoint'}[kind]
    if 'motion' in path.parts or kind == 'motion':
        return 'motion'
    if 'final' in path.parts or kind == 'final':
        return 'final'
    return 'diagnostics' if 'diagnostic' in kind or 'diagnostics' in path.parts else kind


def _camera_name(path: Path) -> str:
    value = re.sub(r"(?:frame|f)[_-]?\d{1,6}(?:\D|$)", "", path.stem, flags=re.IGNORECASE)
    value = re.sub(r"_\d{4,6}$", "", value).strip("_-. ")
    return value or "default"

def serve(root: Path | None = None, port: int = 8765) -> None:
    server = ReviewServer(root or find_project_root(), port)
    print(f"ModelBench review: {server.origin}?action={server.action_token}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the local ModelBench review UI")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    serve(port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
