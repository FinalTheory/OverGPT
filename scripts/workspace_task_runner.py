"""Detached worker for one persistent workspace command."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

sys.dont_write_bytecode = True

TERMINAL_STATES = {"succeeded", "failed", "timed_out", "cancelled"}
LOG_TRUNCATION_MARKER = b"\n... log truncated; middle omitted ...\n"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("task status is not a JSON object")
    return value


@contextmanager
def task_state_lock(task_dir: Path):
    lock_path = task_dir / ".status.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def proc_snapshot(pid: int) -> dict[str, Any] | None:
    if os.name != "posix" or pid <= 0:
        return None
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        close = stat_text.rfind(")")
        if close < 0:
            return None
        fields = stat_text[close + 2 :].split()
        if len(fields) <= 19:
            return None
        return {
            "state": fields[0],
            "pgrp": int(fields[2]),
            "session": int(fields[3]),
            "start_time": int(fields[19]),
        }
    except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, OSError):
        pass

    try:
        result = subprocess.run(
            [
                "ps", "-p", str(pid), "-o", "stat=", "-o", "pgid=", "-o", "sess=",
                "-o", "lstart=", "-o", "command=",
            ],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
        parts = result.stdout.strip().split(None, 8)
        if result.returncode != 0 or len(parts) < 9:
            return None
        return {
            "state": parts[0][0],
            "pgrp": int(parts[1]),
            "session": int(parts[2]),
            "start_time": " ".join(parts[3:8]),
            "cmdline": parts[8],
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        return None


def process_group_members(pgid: int) -> list[int]:
    if os.name != "posix" or pgid <= 0:
        return []
    members: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        try:
            result = subprocess.run(
                ["ps", "-axo", "pid=,pgid=,stat="],
                text=True,
                capture_output=True,
                timeout=2,
                check=False,
            )
            if result.returncode != 0:
                return []
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 3 and int(parts[1]) == pgid and not parts[2].startswith("Z"):
                    members.append(int(parts[0]))
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
            return []
        return members
    for entry in entries:
        if not entry.name.isdigit():
            continue
        snapshot = proc_snapshot(int(entry.name))
        if snapshot is not None and snapshot["pgrp"] == pgid and snapshot["state"] != "Z":
            members.append(int(entry.name))
    return members


def stop_process(process: subprocess.Popen[bytes]) -> bool:
    if os.name != "posix":
        if process.poll() is not None:
            return True
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return True

    pgid = process.pid
    if not process_group_members(pgid):
        try:
            process.wait(timeout=0.1)
        except (subprocess.TimeoutExpired, ChildProcessError):
            pass
        return True
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not process_group_members(pgid):
            break
        time.sleep(0.05)
    if process_group_members(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and process_group_members(pgid):
            time.sleep(0.05)
    try:
        process.wait(timeout=1)
    except (subprocess.TimeoutExpired, ChildProcessError):
        pass
    return not process_group_members(pgid)


class BoundedLogCapture:
    """Drain one pipe continuously while keeping a bounded head/tail log file."""

    def __init__(self, path: Path, limit_bytes: int) -> None:
        self.path = path
        self.limit = max(1024, int(limit_bytes))
        marker_bytes = len(LOG_TRUNCATION_MARKER)
        payload = max(0, self.limit - marker_bytes)
        self.head_limit = payload // 2
        self.tail_limit = payload - self.head_limit
        self.head_written = 0
        self.tail = bytearray()
        self.total = 0
        self.truncated = False
        self.handle = path.open("wb")

    def consume(self, data: bytes) -> None:
        if not data:
            return
        self.total += len(data)
        offset = 0
        if self.head_written < self.head_limit:
            take = min(len(data), self.head_limit - self.head_written)
            self.handle.write(data[:take])
            self.handle.flush()
            self.head_written += take
            offset = take
        if offset < len(data):
            self.tail.extend(data[offset:])
            if len(self.tail) > self.tail_limit:
                del self.tail[: len(self.tail) - self.tail_limit]
            if self.total > self.head_limit + self.tail_limit:
                self.truncated = True

    def finalize(self) -> None:
        if self.truncated:
            self.handle.write(LOG_TRUNCATION_MARKER)
        self.handle.write(self.tail)
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()


def drain_pipe(stream: BinaryIO, capture: BoundedLogCapture) -> None:
    try:
        while True:
            chunk = stream.read1(64 * 1024) if hasattr(stream, "read1") else stream.read(64 * 1024)
            if not chunk:
                break
            capture.consume(chunk)
    finally:
        try:
            stream.close()
        finally:
            capture.finalize()


def finalize_status(task_dir: Path, status_path: Path, status: dict[str, Any]) -> None:
    status["finished_at"] = utc_now()
    with task_state_lock(task_dir):
        current = read_json(status_path)
        if current.get("status") in TERMINAL_STATES:
            return
        current.update(status)
        write_json_atomic(status_path, current)


def main(request_path: Path) -> int:
    task_dir = request_path.parent
    status_path = task_dir / "status.json"
    stdout_path = task_dir / "stdout.log"
    stderr_path = task_dir / "stderr.log"
    cancel_path = task_dir / "cancel.requested"
    request = read_json(request_path)
    metadata = request.get("metadata") if isinstance(request.get("metadata"), dict) else {}

    worker_snapshot = proc_snapshot(os.getpid())
    with task_state_lock(task_dir):
        status = read_json(status_path)
        if status.get("status") in TERMINAL_STATES:
            return 0
        status.update(
            status="running",
            started_at=status.get("started_at") or utc_now(),
            worker_pid=os.getpid(),
            worker_start_time=(
                worker_snapshot["start_time"] if worker_snapshot is not None else None
            ),
            **metadata,
        )
        write_json_atomic(status_path, status)

    command = (
        [sys.executable, "-c", request["code"]]
        if request["language"] == "python"
        else ["/bin/sh", "-c", request["code"]]
    )
    environment = os.environ.copy()
    execution_home = request.get("execution_home")
    if execution_home:
        environment["HOME"] = execution_home

    process: subprocess.Popen[bytes] | None = None
    stdout_capture: BoundedLogCapture | None = None
    stderr_capture: BoundedLogCapture | None = None
    threads: list[threading.Thread] = []
    runtime_status: dict[str, Any] = {
        "status": "running",
        "pid": None,
        "pid_start_time": None,
        "pgid": None,
        "exit_code": None,
        "error": None,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }

    try:
        stdout_path.touch(exist_ok=True)
        stderr_path.touch(exist_ok=True)
        if cancel_path.exists():
            runtime_status["status"] = "cancelled"
            runtime_status["error"] = "background task was cancelled before execution"
        else:
            process = subprocess.Popen(
                command,
                cwd=request["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                start_new_session=True,
                close_fds=True,
            )
            snapshot = proc_snapshot(process.pid)
            runtime_status.update(
                pid=process.pid,
                pid_start_time=(snapshot["start_time"] if snapshot is not None else None),
                pgid=process.pid,
            )
            with task_state_lock(task_dir):
                current = read_json(status_path)
                if current.get("status") not in TERMINAL_STATES:
                    current.update(runtime_status)
                    write_json_atomic(status_path, current)

            log_limit = int(request.get("log_limit_bytes") or 2_000_000)
            stdout_capture = BoundedLogCapture(stdout_path, log_limit)
            stderr_capture = BoundedLogCapture(stderr_path, log_limit)
            assert process.stdout is not None and process.stderr is not None
            threads = [
                threading.Thread(
                    target=drain_pipe, args=(process.stdout, stdout_capture), daemon=True
                ),
                threading.Thread(
                    target=drain_pipe, args=(process.stderr, stderr_capture), daemon=True
                ),
            ]
            for thread in threads:
                thread.start()

            deadline = time.monotonic() + request["timeout_seconds"]
            while True:
                exit_code = process.poll()
                if exit_code is not None:
                    runtime_status["exit_code"] = exit_code
                    contained = stop_process(process) if process_group_members(process.pid) else True
                    runtime_status["status"] = (
                        "succeeded" if exit_code == 0 and contained else "failed"
                    )
                    if not contained:
                        runtime_status["error"] = (
                            "command exited but its process group could not be contained"
                        )
                    break
                if cancel_path.exists():
                    contained = stop_process(process)
                    runtime_status["exit_code"] = process.returncode
                    runtime_status["status"] = "cancelled" if contained else "failed"
                    runtime_status["error"] = (
                        "background task was cancelled"
                        if contained
                        else "background task cancellation could not contain the process group"
                    )
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    contained = stop_process(process)
                    runtime_status["exit_code"] = process.returncode
                    runtime_status["status"] = "timed_out" if contained else "failed"
                    runtime_status["error"] = (
                        "background task exceeded timeout_seconds"
                        if contained
                        else "background task timed out and its process group could not be contained"
                    )
                    break
                time.sleep(min(0.25, remaining))
    except BaseException as error:
        if process is not None and process.poll() is None:
            stop_process(process)
        runtime_status["status"] = "failed"
        runtime_status["exit_code"] = process.returncode if process is not None else None
        runtime_status["error"] = f"{type(error).__name__}: {error}"
    finally:
        for thread in threads:
            thread.join(timeout=10)
        if stdout_capture is not None:
            runtime_status["stdout_truncated"] = stdout_capture.truncated
        if stderr_capture is not None:
            runtime_status["stderr_truncated"] = stderr_capture.truncated
        finalize_status(task_dir, status_path, runtime_status)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: workspace_task_runner.py REQUEST_JSON")
    raise SystemExit(main(Path(sys.argv[1]).resolve()))
