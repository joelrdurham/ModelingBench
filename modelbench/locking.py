from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import LockError
from .util import utc_now


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == 'nt':
        # os.kill(pid, 0) is not a safe process probe on Windows.
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.WaitForSingleObject.restype = ctypes.c_ulong
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: conservatively alive
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class OperationLock:
    def __init__(self, generated: Path, operation: str, run: str | None = None):
        self.path = generated / ".modelbench-operation.lock"
        self.operation = operation
        self.run = run
        self.acquired = False

    def __enter__(self) -> "OperationLock":
        payload = {"pid": os.getpid(), "operation": self.operation, "run": self.run, "started_at": utc_now()}
        for attempt in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    existing = json.loads(self.path.read_text(encoding="utf-8"))
                    alive = _pid_alive(int(existing.get("pid", -1)))
                except Exception:
                    alive = True
                    existing = {"operation": "unknown", "pid": "unknown"}
                if alive or attempt:
                    raise LockError(
                        f"Another operation is active: {existing.get('operation')} (pid {existing.get('pid')})"
                    )
                self.path.unlink(missing_ok=True)
        raise LockError("Could not acquire operation lock")

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.acquired:
            try:
                current = json.loads(self.path.read_text(encoding="utf-8"))
                if current.get("pid") == os.getpid():
                    self.path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass


def assert_no_active_operation(generated: Path) -> None:
    lock = generated / ".modelbench-operation.lock"
    if not lock.exists():
        return
    try:
        value = json.loads(lock.read_text(encoding="utf-8"))
        if _pid_alive(int(value.get("pid", -1))):
            raise LockError(f"Operation active: {value.get('operation')} (pid {value.get('pid')})")
    except json.JSONDecodeError as exc:
        raise LockError("Unparseable operation lock; refusing destructive action") from exc
    lock.unlink(missing_ok=True)

