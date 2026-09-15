"""Detached worker for one persistent workspace command."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


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


def stop_process(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()


def main(request_path: Path) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    task_dir = request_path.parent
    status_path = task_dir / "status.json"
    stdout_path = task_dir / "stdout.log"
    stderr_path = task_dir / "stderr.log"
    command = (
        [sys.executable, "-c", request["code"]]
        if request["language"] == "python"
        else ["/bin/sh", "-c", request["code"]]
    )
    status: dict[str, Any] = {
        "task_id": request["task_id"],
        "status": "running",
        "language": request["language"],
        "cwd": request["cwd_relative"],
        "timeout_seconds": request["timeout_seconds"],
        "created_at": request["created_at"],
        "started_at": utc_now(),
        "finished_at": None,
        "worker_pid": os.getpid(),
        "pid": None,
        "exit_code": None,
        "error": None,
    }

    environment = os.environ.copy()
    execution_home = request.get("execution_home")
    if execution_home:
        environment["HOME"] = execution_home

    process: subprocess.Popen[bytes] | None = None
    try:
        with stdout_path.open("ab", buffering=0) as stdout, stderr_path.open(
            "ab", buffering=0
        ) as stderr:
            process = subprocess.Popen(
                command,
                cwd=request["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                env=environment,
                start_new_session=True,
                close_fds=True,
            )
            status["pid"] = process.pid
            write_json_atomic(status_path, status)
            try:
                exit_code = process.wait(timeout=request["timeout_seconds"])
                status["exit_code"] = exit_code
                status["status"] = "succeeded" if exit_code == 0 else "failed"
            except subprocess.TimeoutExpired:
                stop_process(process)
                status["exit_code"] = process.returncode
                status["status"] = "timed_out"
                status["error"] = "background task exceeded timeout_seconds"
    except BaseException as error:
        if process is not None and process.poll() is None:
            stop_process(process)
        status["status"] = "failed"
        status["exit_code"] = process.returncode if process is not None else None
        status["error"] = f"{type(error).__name__}: {error}"
    finally:
        status["finished_at"] = utc_now()
        write_json_atomic(status_path, status)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: workspace_task_runner.py REQUEST_JSON")
    raise SystemExit(main(Path(sys.argv[1]).resolve()))
