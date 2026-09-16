"""MCP tools for shared writing skills and workspace file operations."""

from __future__ import annotations

import asyncio
import codecs
import fnmatch
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

sys.dont_write_bytecode = True

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from config import CONFIG

mcp = FastMCP(
    CONFIG.server_name,
    instructions=(
        f"This server exposes skills and a persistent shared workspace at {CONFIG.workspace_root}. "
        "All file paths are relative to the workspace root. Workspace layout: "
        f"{CONFIG.skills_dirname}/<name>/SKILL.md contains reusable writing workflows; "
        f"{CONFIG.draft_dirname}/<year-month>/<article>.md contains article drafts and is a Git "
        f"repository; {CONFIG.project_root.name}/ contains this MCP server's code; scripts/ contains "
        "other content management scripts. Before a writing task, call "
        "list_skills and load the relevant skills. Use list_draft_articles to discover existing "
        "drafts before reading or editing them. For repository work, use list_workspace and "
        "search_workspace_text for routine navigation before falling back to shell discovery. "
        "Prefer read_workspace_range plus replace_workspace_text or insert_workspace_text for "
        "precise edits; their match-count guards prevent accidental broad changes. Full-file "
        "overwrites must use expected_sha256 from a prior read to reject stale writes. Use "
        "delete_workspace_file and move_workspace_file for ordinary file mutations instead of "
        "shell rm/mv. Use run_workspace_code(background=true) for "
        "long-running commands, then poll get_workspace_task with the returned task_id; background "
        "commands default to a one-hour timeout. To delegate to a ChatGPT sub-agent, pass its "
        "complete task directly to spawn_chatgpt_subagent. Preserve the separately returned "
        "output_path as the sub-agent's result file; stdout_path and stderr_path are execution "
        "logs. Call get_workspace_task with its defaults: it waits server-side for up to 30 "
        "seconds, returning sooner when the task finishes. If a task fails or times out, read "
        "its returned stderr_path with the workspace file tools for diagnostics."
    ),
    host=CONFIG.host,
    port=CONFIG.port,
    streamable_http_path=CONFIG.mcp_path,
    stateless_http=True,
    json_response=True,
)

_workspace_write_lock = threading.RLock()


def _workspace_path(relative_path: str, *, must_exist: bool = False) -> Path:
    """Resolve a user path and prevent it from escaping the mounted workspace."""
    if not relative_path or Path(relative_path).is_absolute():
        raise ValueError("path must be a non-empty path relative to the workspace")

    resolved = (CONFIG.workspace_root / relative_path).resolve()
    if resolved != CONFIG.workspace_root and CONFIG.workspace_root not in resolved.parents:
        raise ValueError("path escapes the workspace")
    if must_exist and not resolved.exists():
        raise FileNotFoundError(relative_path)
    return resolved


def _skill_file(name: str) -> Path:
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError("skill name must be a single directory name")
    path = (CONFIG.skills_root / name / "SKILL.md").resolve()
    if CONFIG.skills_root not in path.parents or not path.is_file():
        raise FileNotFoundError(f"unknown skill: {name}")
    return path


def _skill_summary(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    fields: dict[str, str] = {"name": path.parent.name, "description": "", "version": ""}
    if text.startswith("---"):
        for line in text.split("---", 2)[1].splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() in fields:
                fields[key.strip()] = value.strip().strip('"\'')
    return fields


def _all_skills() -> list[dict[str, str]]:
    if not CONFIG.skills_root.is_dir():
        return []
    return [
        _skill_summary(path)
        for path in sorted(CONFIG.skills_root.glob("*/SKILL.md"))
        if path.is_file()
    ]


def _truncate(value: str) -> tuple[str, bool]:
    if len(value) <= CONFIG.max_output_chars:
        return value, False
    marker = "\n... output truncated; middle omitted ...\n"
    remaining = max(0, CONFIG.max_output_chars - len(marker))
    head = remaining // 2
    tail = remaining - head
    return f"{value[:head]}{marker}{value[-tail:] if tail else ''}", True


def _write_text_atomic(path: Path, content: str) -> None:
    with _workspace_write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if path.exists():
                os.chmod(temp_name, path.stat().st_mode & 0o7777)
            os.replace(temp_name, path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise


def _read_workspace_text(path: str) -> tuple[Path, str]:
    resolved = _workspace_path(path, must_exist=True)
    if not resolved.is_file():
        raise ValueError("path is not a file")
    return resolved, resolved.read_text(encoding="utf-8")


def _read_workspace_prefix(path: str) -> tuple[Path, str, bool]:
    """Read at most max_read_chars + 1 characters without materializing the file."""
    resolved = _workspace_path(path, must_exist=True)
    if not resolved.is_file():
        raise ValueError("path is not a file")
    with resolved.open("r", encoding="utf-8") as handle:
        content = handle.read(CONFIG.max_read_chars + 1)
    return resolved, content[: CONFIG.max_read_chars], len(content) > CONFIG.max_read_chars


def _read_line_range_bounded(
    resolved: Path, start_line: int, end_line: int
) -> tuple[str, bool, int]:
    """Scan a line range with bounded retained data, even for pathological long lines."""
    capture_limit = CONFIG.max_read_chars * 4 + 4
    captured = bytearray()
    total_lines = 0
    truncated = False
    with resolved.open("rb") as handle:
        while True:
            segment = handle.readline(capture_limit + 1)
            if not segment:
                break
            total_lines += 1
            selected = start_line <= total_lines <= end_line
            if selected:
                remaining = max(0, capture_limit - len(captured))
                captured.extend(segment[:remaining])
                if len(segment) > remaining:
                    truncated = True

            if len(segment) >= capture_limit + 1 and not segment.endswith(b"\n"):
                if selected:
                    truncated = True
                while segment and not segment.endswith(b"\n"):
                    segment = handle.readline(capture_limit + 1)
                    if not segment:
                        break

    if end_line > total_lines:
        raise ValueError(f"end_line exceeds file length of {total_lines} lines")
    decoder = codecs.getincrementaldecoder("utf-8")()
    text = decoder.decode(bytes(captured), final=False)
    if len(text) > CONFIG.max_read_chars:
        text = text[: CONFIG.max_read_chars]
        truncated = True
    return text, truncated, total_lines


def _file_sha256(path: Path) -> str:
    """Return a stable content hash for optimistic-concurrency checks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_sha256(value: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("expected_sha256 must be a lowercase 64-character SHA-256 hex digest")


def _validate_expected_count(expected_count: int) -> None:
    if expected_count < 1:
        raise ValueError("expected_count must be at least 1")


def _render_chatgpt_subagent_prompt(input_path: str, output_path: str) -> str:
    from chatgpt_playwright import render_subagent_prompt

    return render_subagent_prompt(
        CONFIG.chatgpt_prompt_file,
        input_path,
        output_path,
    )


def _chatgpt_subagent_task_code(input_path: str, output_path: str) -> str:
    return "\n".join(
        [
            "import sys",
            f"sys.path.insert(0, {str(CONFIG.project_root)!r})",
            "from chatgpt_playwright import send_subagent_task",
            (
                "result = send_subagent_task("
                f"{input_path!r}, {output_path!r}, wait_for_completion=True)"
            ),
            "print(result)",
        ]
    )


def _execution_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if CONFIG.execution_home:
        environment["HOME"] = CONFIG.execution_home
    return environment


_TERMINAL_TASK_STATES = {"succeeded", "failed", "timed_out", "cancelled"}


def _proc_snapshot(pid: int) -> dict[str, Any] | None:
    """Read Linux process identity without depending on external ps/procps."""
    if os.name != "posix" or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        close = stat_text.rfind(")")
        if close < 0:
            return None
        fields = stat_text[close + 2 :].split()
        if len(fields) <= 19:
            return None
        cmdline_raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        cmdline = " ".join(
            part.decode("utf-8", errors="replace")
            for part in cmdline_raw.split(b"\0")
            if part
        )
        return {
            "state": fields[0],
            "pgrp": int(fields[2]),
            "session": int(fields[3]),
            "start_time": int(fields[19]),
            "cmdline": cmdline,
        }
    except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, OSError):
        pass

    # macOS has no /proc. Keep a stable start-time token plus the command line
    # so PID reuse is still detected when the service runs directly on macOS.
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


def _process_group_members(pgid: int) -> list[int]:
    if os.name != "posix" or not isinstance(pgid, int) or pgid <= 0:
        return []
    members: list[int] = []
    proc_root = Path("/proc")
    try:
        entries = list(proc_root.iterdir())
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
        snapshot = _proc_snapshot(int(entry.name))
        if snapshot is not None and snapshot["pgrp"] == pgid and snapshot["state"] != "Z":
            members.append(int(entry.name))
    return members


def _terminate_process_group_id(pgid: int, grace_seconds: float = 5.0) -> bool:
    """Terminate every live member of one process group, escalating after grace."""
    if os.name != "posix":
        return False
    if not _process_group_members(pgid):
        return True
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _process_group_members(pgid):
            return True
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _process_group_members(pgid):
            return True
        time.sleep(0.05)
    return not _process_group_members(pgid)


def _stop_process_group(process: subprocess.Popen[Any]) -> None:
    """Terminate one command and all descendants started in its process group."""
    if os.name == "posix":
        _terminate_process_group_id(process.pid)
        try:
            process.wait(timeout=1)
        except (subprocess.TimeoutExpired, ChildProcessError):
            pass
        return
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _task_dir(task_id: str) -> Path:
    if not re.fullmatch(r"task_[0-9a-f]{32}", task_id):
        raise ValueError("invalid task_id")
    resolved = (CONFIG.tasks_root / task_id).resolve()
    if CONFIG.tasks_root not in resolved.parents:
        raise ValueError("task path escapes the task directory")
    return resolved


@contextmanager
def _task_state_lock(task_dir: Path):
    lock_path = task_dir / ".status.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_task_json(path: Path, payload: dict[str, Any]) -> None:
    _write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _read_task_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"could not read task status: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError("task status is not a JSON object")
    return value


def _worker_is_alive(pid: Any, start_time: Any, task_id: str) -> bool:
    if not isinstance(pid, int) or not isinstance(start_time, (int, str)):
        return False
    snapshot = _proc_snapshot(pid)
    return bool(
        snapshot
        and snapshot["start_time"] == start_time
        and "workspace_task_runner.py" in snapshot["cmdline"]
        and task_id in snapshot["cmdline"]
    )


def _terminate_recorded_workload(status: dict[str, Any]) -> bool:
    pid = status.get("pid")
    start_time = status.get("pid_start_time")
    pgid = status.get("pgid")
    if not isinstance(pgid, int) or pgid <= 0:
        return True
    if not isinstance(pid, int) or not isinstance(start_time, (int, str)):
        # Legacy/incomplete records do not contain enough identity to safely
        # signal a possibly reused process group.
        return not _process_group_members(pgid)
    snapshot = _proc_snapshot(pid)
    if snapshot is not None and snapshot["start_time"] != start_time:
        return False
    return _terminate_process_group_id(pgid)


def _recover_workspace_task(task_dir: Path, task_id: str) -> dict[str, Any]:
    status_path = task_dir / "status.json"
    with _task_state_lock(task_dir):
        status = _read_task_json(status_path)
        state = status.get("status")
        if state in _TERMINAL_TASK_STATES:
            return status
        if state not in {"queued", "running"}:
            return status
        if _worker_is_alive(
            status.get("worker_pid"), status.get("worker_start_time"), task_id
        ):
            return status

        cleanup_ok = _terminate_recorded_workload(status)
        # Re-read while holding the interprocess task lock. A healthy runner uses
        # the same lock for every transition, so a newer terminal state wins.
        current = _read_task_json(status_path)
        if current.get("status") in _TERMINAL_TASK_STATES:
            return current
        current.update(
            status="failed",
            finished_at=_utc_now(),
            error=(
                "background worker is no longer running"
                if cleanup_ok
                else "background worker is gone and workload identity could not be safely cleaned up"
            ),
        )
        _write_task_json(status_path, current)
        return current


def _prune_old_workspace_tasks() -> int:
    """Reconcile stale tasks and delete terminal task directories past retention."""
    if CONFIG.task_retention_days < 0:
        raise ValueError("MCP_TASK_RETENTION_DAYS must not be negative")
    if not CONFIG.tasks_root.is_dir():
        return 0

    cutoff = datetime.now(UTC) - timedelta(days=CONFIG.task_retention_days)
    removed = 0
    for task_dir in list(CONFIG.tasks_root.iterdir()):
        if (
            not re.fullmatch(r"task_[0-9a-f]{32}", task_dir.name)
            or task_dir.is_symlink()
            or not task_dir.is_dir()
        ):
            continue
        status_path = task_dir / "status.json"
        if not status_path.is_file():
            continue
        try:
            status = _read_task_json(status_path)
            if status.get("status") in {"queued", "running"}:
                status = _recover_workspace_task(task_dir, task_dir.name)
            if status.get("status") not in _TERMINAL_TASK_STATES:
                continue
            timestamp = status.get("finished_at") or status.get("created_at")
            if not isinstance(timestamp, str):
                continue
            last_activity = datetime.fromisoformat(timestamp)
            if last_activity.tzinfo is None:
                last_activity = last_activity.replace(tzinfo=UTC)
            if last_activity.astimezone(UTC) >= cutoff:
                continue
            with _workspace_write_lock:
                shutil.rmtree(task_dir)
            removed += 1
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            continue
    return removed


def _reserve_workspace_task_dir() -> tuple[str, Path]:
    _prune_old_workspace_tasks()
    CONFIG.tasks_root.mkdir(parents=True, exist_ok=True)
    while True:
        task_id = f"task_{uuid.uuid4().hex}"
        task_dir = _task_dir(task_id)
        try:
            task_dir.mkdir(parents=False, exist_ok=False)
            return task_id, task_dir
        except FileExistsError:
            continue


def _start_workspace_task(
    language: Literal["python", "shell"],
    code: str,
    working_dir: Path,
    cwd_relative: str,
    timeout_seconds: int,
    *,
    task_id: str | None = None,
    task_dir: Path | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if (task_id is None) != (task_dir is None):
        raise ValueError("task_id and task_dir must be supplied together")
    if task_id is None or task_dir is None:
        task_id, task_dir = _reserve_workspace_task_dir()
    else:
        expected_dir = _task_dir(task_id)
        if task_dir.resolve() != expected_dir or not task_dir.is_dir():
            raise ValueError("reserved task directory does not match task_id")

    request_path = task_dir / "request.json"
    status_path = task_dir / "status.json"
    created_at = _utc_now()
    metadata = dict(metadata or {})
    request = {
        "task_id": task_id,
        "language": language,
        "code": code,
        "cwd": str(working_dir),
        "cwd_relative": cwd_relative,
        "timeout_seconds": timeout_seconds,
        "execution_home": CONFIG.execution_home or None,
        "log_limit_bytes": CONFIG.max_background_log_bytes,
        "created_at": created_at,
        "metadata": metadata,
    }
    status: dict[str, Any] = {
        "task_id": task_id,
        "status": "queued",
        "language": language,
        "cwd": cwd_relative,
        "timeout_seconds": timeout_seconds,
        "created_at": created_at,
        "started_at": None,
        "finished_at": None,
        "worker_pid": None,
        "worker_start_time": None,
        "pid": None,
        "pid_start_time": None,
        "pgid": None,
        "exit_code": None,
        "error": None,
        "stdout_truncated": False,
        "stderr_truncated": False,
        **metadata,
    }
    runner = CONFIG.project_root / "scripts" / "workspace_task_runner.py"
    _write_task_json(request_path, request)
    with _task_state_lock(task_dir):
        _write_task_json(status_path, status)
        if not runner.is_file():
            status.update(status="failed", finished_at=_utc_now(), error="task runner is unavailable")
            _write_task_json(status_path, status)
            raise RuntimeError(f"task runner is unavailable: {runner}")
        try:
            runner_process = subprocess.Popen(
                [sys.executable, str(runner), str(request_path)],
                cwd=CONFIG.project_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_execution_environment(),
                start_new_session=True,
                close_fds=True,
            )
        except OSError as error:
            status.update(status="failed", finished_at=_utc_now(), error=str(error))
            _write_task_json(status_path, status)
            raise RuntimeError(f"could not start background task: {error}") from error

        # Publish queued state and reliable runner identity atomically with
        # respect to recovery/pruning. The runner blocks on this same lock until
        # the identity record is durable.
        runner_snapshot = _proc_snapshot(runner_process.pid)
        status["worker_pid"] = runner_process.pid
        status["worker_start_time"] = (
            runner_snapshot["start_time"] if runner_snapshot is not None else None
        )
        _write_task_json(status_path, status)

    relative_task_dir = task_dir.relative_to(CONFIG.workspace_root).as_posix()
    return {
        "task_id": task_id,
        "status": "queued",
        "task_dir": relative_task_dir,
        "stdout_path": f"{relative_task_dir}/stdout.log",
        "stderr_path": f"{relative_task_dir}/stderr.log",
    }

def _run_checked(command: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=CONFIG.max_timeout_seconds,
            check=False,
        )
    except FileNotFoundError as error:
        raise RuntimeError(f"required executable is unavailable: {command[0]}") from error
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"command failed: {' '.join(command)}")
    return result.stdout


def _git_apply(patch: str, *, check: bool) -> str:
    command = ["git", "apply"]
    if check:
        command.append("--check")
    command.extend(["--whitespace=nowarn", "-"])
    try:
        result = subprocess.run(
            command,
            cwd=CONFIG.workspace_root,
            input=patch,
            text=True,
            capture_output=True,
            timeout=CONFIG.max_timeout_seconds,
            check=False,
        )
    except FileNotFoundError as error:
        raise RuntimeError("required executable is unavailable: git") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git apply failed"
        raise ValueError(detail)
    return result.stdout


def _draft_file(relative_path: str, *, must_exist: bool = True) -> tuple[Path, str]:
    """Resolve one file beneath draft and return its normalized repository path."""
    candidate = Path(relative_path)
    if not relative_path or candidate.is_absolute():
        raise ValueError("path must be relative to draft")
    resolved = (CONFIG.draft_root / candidate).resolve()
    if CONFIG.draft_root not in resolved.parents:
        raise ValueError("path is not a file inside draft")
    if resolved.exists() and not resolved.is_file():
        raise ValueError("path is not a file inside draft")
    if must_exist and not resolved.is_file():
        raise ValueError("path is not a file inside draft")
    return resolved, resolved.relative_to(CONFIG.draft_root).as_posix()


def _draft_files() -> list[str]:
    if not CONFIG.draft_root.is_dir():
        return []
    tracked_changes = _run_checked(
        ["git", "diff", "--name-only", "-z", "HEAD", "--"], CONFIG.draft_root
    ).split("\0")
    untracked_changes = _run_checked(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], CONFIG.draft_root
    ).split("\0")
    files = [
        path
        for path in {*tracked_changes, *untracked_changes}
        if path and not any(part.startswith(".") for part in Path(path).parts)
    ]
    return sorted(files, key=lambda value: (value.casefold(), value))


def _validated_revision(revision: str) -> str:
    if revision == "HEAD":
        return revision
    if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise ValueError("revision must be HEAD or a full commit hash")
    return _run_checked(
        ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"], CONFIG.draft_root
    ).strip()


def _git_commits_for_file(relative_path: str) -> list[dict[str, str]]:
    _, normalized = _draft_file(relative_path, must_exist=False)
    head = _run_checked(["git", "rev-parse", "HEAD"], CONFIG.draft_root).strip()
    output = _run_checked(
        [
            "git",
            "--literal-pathspecs",
            "log",
            "--follow",
            f"--max-count={CONFIG.draft_history_limit + 1}",
            "--format=%H%x1f%h%x1f%cs%x1f%s%x1e",
            "--",
            normalized,
        ],
        CONFIG.draft_root,
    )
    commits: list[dict[str, str]] = []
    for record in output.split("\x1e"):
        fields = record.strip().split("\x1f", 3)
        if len(fields) == 4 and fields[0] != head:
            commits.append(
                {"hash": fields[0], "short_hash": fields[1], "date": fields[2], "subject": fields[3]}
            )
            if len(commits) >= CONFIG.draft_history_limit:
                break
    return commits


def _git_diff_for_file(relative_path: str, revision: str = "HEAD") -> str:
    resolved, normalized = _draft_file(relative_path, must_exist=False)
    revision = _validated_revision(revision)
    tracked = subprocess.run(
        ["git", "--literal-pathspecs", "ls-files", "--error-unmatch", "--", normalized],
        cwd=CONFIG.draft_root,
        text=True,
        capture_output=True,
        timeout=CONFIG.max_timeout_seconds,
        check=False,
    ).returncode == 0
    if not tracked and not resolved.is_file():
        raise ValueError("path is not a tracked or untracked file inside draft")
    word_diff_args = [
        "--word-diff=porcelain",
        f"--word-diff-regex={CONFIG.git_word_diff_regex}",
    ]
    command = (
        ["git", "--literal-pathspecs", "diff", "--no-ext-diff", "--no-color", *word_diff_args, revision, "--", normalized]
        if tracked
        else [
            "git",
            "diff",
            "--no-index",
            "--no-color",
            *word_diff_args,
            "/dev/null",
            str(resolved),
        ]
    )
    result = subprocess.run(
        command,
        cwd=CONFIG.draft_root,
        text=True,
        capture_output=True,
        timeout=CONFIG.max_timeout_seconds,
        check=False,
        env={**os.environ, "LANG": CONFIG.git_locale, "LC_ALL": CONFIG.git_locale},
    )
    allowed_codes = {0} if tracked else {0, 1}
    if result.returncode not in allowed_codes:
        raise RuntimeError(result.stderr.strip() or "git diff failed")
    return result.stdout


@mcp.tool()
def list_skills() -> list[dict[str, str]]:
    """List available skills with names, versions, and descriptions."""
    return _all_skills()


@mcp.tool()
def list_draft_articles() -> list[dict[str, str]]:
    """List draft Markdown articles with workspace-relative paths and first-line titles.

    Use this to locate an existing draft from the user's description before reading
    or editing it. Returned paths begin with "draft/" and can be passed directly to
    read_workspace_file or write_workspace_file.
    """
    if not CONFIG.draft_root.is_dir():
        return []

    articles: list[dict[str, str]] = []
    for path in sorted(CONFIG.draft_root.rglob("*.md")):
        relative = path.relative_to(CONFIG.draft_root)
        if ".git" in relative.parts or not path.is_file():
            continue
        with path.open(encoding="utf-8") as handle:
            title = handle.readline().rstrip("\r\n")
        articles.append(
            {
                "path": f"{CONFIG.draft_dirname}/{relative.as_posix()}",
                "title": title,
            }
        )
    return articles


@mcp.tool()
def load_skill(name: str) -> str:
    """Load the complete SKILL.md for a named skill and follow its workflow."""
    return _skill_file(name).read_text(encoding="utf-8")


@mcp.tool()
def read_workspace_file(path: str) -> dict[str, Any]:
    """Read a bounded UTF-8 prefix of one workspace file."""
    resolved, content, truncated = _read_workspace_prefix(path)
    return {
        "path": path,
        "content": content,
        "truncated": truncated,
        "size_bytes": resolved.stat().st_size,
        "sha256": _file_sha256(resolved),
    }


@mcp.tool()
def read_workspace_range(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    anchor: str | None = None,
    context_lines: int = 20,
) -> dict[str, Any]:
    """Read a bounded line range or exact-anchor context without unbounded memory."""
    resolved = _workspace_path(path, must_exist=True)
    if not resolved.is_file():
        raise ValueError("path is not a file")

    if anchor is not None:
        if start_line is not None or end_line is not None:
            raise ValueError("provide either anchor or a line range, not both")
        if not anchor:
            raise ValueError("anchor must not be empty")
        if context_lines < 0:
            raise ValueError("context_lines must be non-negative")
        size_bytes = resolved.stat().st_size
        if size_bytes > CONFIG.max_anchor_scan_bytes:
            raise ValueError(
                "anchor mode is disabled for very large files; use an explicit line range "
                "or run_workspace_code for targeted large-file inspection"
            )
        content = resolved.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)
        total_lines = len(lines)
        actual_count = content.count(anchor)
        if actual_count != 1:
            raise ValueError(f"anchor matched {actual_count} times; expected exactly 1")
        match_start = content.find(anchor)
        match_end = match_start + len(anchor)
        anchor_start_line = content.count("\n", 0, match_start) + 1
        anchor_end_line = content.count("\n", 0, max(match_start, match_end - 1)) + 1
        selected_start = max(1, anchor_start_line - context_lines)
        selected_end = min(total_lines, anchor_end_line + context_lines)
        excerpt = "".join(lines[selected_start - 1 : selected_end])
        truncated = len(excerpt) > CONFIG.max_read_chars
        excerpt = excerpt[: CONFIG.max_read_chars]
        mode = "anchor"
    else:
        if start_line is None or end_line is None:
            raise ValueError("provide anchor or both start_line and end_line")
        if start_line < 1 or end_line < start_line:
            raise ValueError("line range must be 1-based with end_line >= start_line")
        excerpt, truncated, total_lines = _read_line_range_bounded(
            resolved, start_line, end_line
        )
        selected_start = start_line
        selected_end = end_line
        mode = "lines"

    return {
        "path": path,
        "mode": mode,
        "start_line": selected_start,
        "end_line": selected_end,
        "total_lines": total_lines,
        "content": excerpt,
        "truncated": truncated,
        "sha256": _file_sha256(resolved),
    }


@mcp.tool()
def list_workspace(
    path: str = ".",
    depth: int = 1,
    include_hidden: bool = False,
    max_entries: int = 500,
) -> dict[str, Any]:
    """List files and directories under one workspace-relative directory.

    depth=1 lists immediate children; larger values recurse to that many levels.
    Symlinks are skipped rather than followed. Hidden entries are omitted by default.
    """
    if depth < 1:
        raise ValueError("depth must be at least 1")
    if not 1 <= max_entries <= CONFIG.max_list_entries:
        raise ValueError(
            f"max_entries must be between 1 and {CONFIG.max_list_entries}"
        )
    root = _workspace_path(path, must_exist=True)
    if not root.is_dir():
        raise ValueError("path is not a directory")

    entries: list[dict[str, Any]] = []
    truncated = False
    for current_root, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(current_root)
        current_depth = len(current.relative_to(root).parts)
        if current_depth >= depth:
            dirnames[:] = []
            continue

        visible_dirs: list[str] = []
        for name in sorted(dirnames):
            candidate = current / name
            if candidate.is_symlink() or (not include_hidden and name.startswith(".")):
                continue
            visible_dirs.append(name)
        dirnames[:] = visible_dirs

        for name in visible_dirs:
            candidate = current / name
            entries.append(
                {
                    "path": candidate.relative_to(CONFIG.workspace_root).as_posix(),
                    "type": "directory",
                }
            )
            if len(entries) >= max_entries:
                truncated = True
                break
        if truncated:
            break

        for name in sorted(filenames):
            if not include_hidden and name.startswith("."):
                continue
            candidate = current / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            entries.append(
                {
                    "path": candidate.relative_to(CONFIG.workspace_root).as_posix(),
                    "type": "file",
                    "size_bytes": candidate.stat().st_size,
                }
            )
            if len(entries) >= max_entries:
                truncated = True
                break
        if truncated:
            break

    return {
        "path": path,
        "depth": depth,
        "entries": entries,
        "truncated": truncated,
    }


@mcp.tool()
def search_workspace_text(
    path: str,
    query: str,
    file_glob: str = "*",
    case_sensitive: bool = True,
    max_results: int = 100,
) -> dict[str, Any]:
    """Search UTF-8 source text under one workspace path using a literal substring.

    Results include workspace-relative path, 1-based line number, and matching line.
    Common generated/runtime directories are skipped. Large or non-UTF-8 files are
    ignored. Use file_glob such as '*.py' or '*.md' to narrow the search.
    """
    if not query:
        raise ValueError("query must not be empty")
    if not file_glob:
        raise ValueError("file_glob must not be empty")
    if not 1 <= max_results <= CONFIG.max_search_results:
        raise ValueError(
            f"max_results must be between 1 and {CONFIG.max_search_results}"
        )

    root = _workspace_path(path, must_exist=True)
    skipped_dirnames = {
        ".git",
        ".mcp-tasks",
        "chatgpt-profile",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
    }
    candidates: list[Path] = []
    if root.is_file():
        candidates.append(root)
    elif root.is_dir():
        for current_root, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(current_root)
            dirnames[:] = [
                name
                for name in sorted(dirnames)
                if name not in skipped_dirnames and not (current / name).is_symlink()
            ]
            for name in sorted(filenames):
                candidate = current / name
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                relative_to_root = candidate.relative_to(root).as_posix()
                if fnmatch.fnmatch(relative_to_root, file_glob):
                    candidates.append(candidate)
    else:
        raise ValueError("path is neither a file nor a directory")

    needle = query if case_sensitive else query.casefold()
    results: list[dict[str, Any]] = []
    skipped_files = 0
    for candidate in candidates:
        try:
            if candidate.stat().st_size > CONFIG.max_search_file_bytes:
                skipped_files += 1
                continue
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped_files += 1
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            haystack = line if case_sensitive else line.casefold()
            if needle not in haystack:
                continue
            results.append(
                {
                    "path": candidate.relative_to(CONFIG.workspace_root).as_posix(),
                    "line": line_number,
                    "text": line[:1000],
                }
            )
            if len(results) >= max_results:
                return {
                    "path": path,
                    "query": query,
                    "results": results,
                    "truncated": True,
                    "skipped_files": skipped_files,
                }

    return {
        "path": path,
        "query": query,
        "results": results,
        "truncated": False,
        "skipped_files": skipped_files,
    }


@mcp.tool()
def delete_workspace_file(path: str, expected_sha256: str | None = None) -> dict[str, Any]:
    """Delete one workspace file, optionally guarded by its previously read SHA-256.

    This tool never deletes directories. Pass expected_sha256 when the file was read
    earlier and deletion should fail if another agent changed it in the meantime.
    """
    with _workspace_write_lock:
        resolved = _workspace_path(path, must_exist=True)
        if not resolved.is_file():
            raise ValueError("path is not a file")
        actual_sha256 = _file_sha256(resolved)
        if expected_sha256 is not None:
            _validate_sha256(expected_sha256)
            if actual_sha256 != expected_sha256:
                raise ValueError(
                    "file changed since it was read; expected_sha256 does not match; file unchanged"
                )
        size_bytes = resolved.stat().st_size
        resolved.unlink()
    return {
        "path": path,
        "deleted": True,
        "size_bytes": size_bytes,
        "sha256": actual_sha256,
    }


@mcp.tool()
def move_workspace_file(
    source: str,
    destination: str,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Atomically move or rename one workspace file without overwriting a destination.

    Pass expected_sha256 to reject the move if the source changed after it was read.
    Destination parent directories are created when needed.
    """
    with _workspace_write_lock:
        source_path = _workspace_path(source, must_exist=True)
        destination_path = _workspace_path(destination)
        if not source_path.is_file():
            raise ValueError("source is not a file")
        if destination_path.exists():
            raise FileExistsError("destination already exists")
        actual_sha256 = _file_sha256(source_path)
        if expected_sha256 is not None:
            _validate_sha256(expected_sha256)
            if actual_sha256 != expected_sha256:
                raise ValueError(
                    "source changed since it was read; expected_sha256 does not match; file unchanged"
                )
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source_path, destination_path)
    return {
        "source": source,
        "destination": destination,
        "moved": True,
        "sha256": actual_sha256,
    }


@mcp.tool()
def write_workspace_file(
    path: str,
    content: str,
    overwrite: bool = False,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Write a UTF-8 document atomically inside the shared workspace.

    New files are created by default. Replacing an existing file requires
    overwrite=true and expected_sha256 from a prior read_workspace_file or
    read_workspace_range call. The hash guard prevents one agent from silently
    overwriting changes made after it read the file.
    """
    with _workspace_write_lock:
        resolved = _workspace_path(path)
        existed = resolved.exists()
        if existed:
            if not resolved.is_file():
                raise ValueError("path exists and is not a file")
            if not overwrite:
                raise FileExistsError("file already exists; set overwrite=true to replace it")
            if expected_sha256 is None:
                raise ValueError(
                    "overwriting an existing file requires expected_sha256 from a prior read"
                )
            _validate_sha256(expected_sha256)
            actual_sha256 = _file_sha256(resolved)
            if actual_sha256 != expected_sha256:
                raise ValueError(
                    "file changed since it was read; expected_sha256 does not match; file unchanged"
                )
        elif expected_sha256 is not None:
            raise ValueError("expected_sha256 was provided but the destination file does not exist")
        _write_text_atomic(resolved, content)
        current_sha256 = _file_sha256(resolved)
    return {
        "path": path,
        "size_bytes": resolved.stat().st_size,
        "sha256": current_sha256,
        "created": not existed,
        "overwritten": existed,
    }


@mcp.tool()
def replace_workspace_text(
    path: str,
    old_text: str,
    new_text: str,
    expected_count: int = 1,
) -> dict[str, Any]:
    """Atomically replace exact text only when its match count is as expected.

    The complete file is validated before any write. If old_text occurs a
    different number of times, the operation fails and leaves the file untouched.
    Paths are relative to the shared workspace.
    """
    if not old_text:
        raise ValueError("old_text must not be empty")
    _validate_expected_count(expected_count)
    with _workspace_write_lock:
        resolved, content = _read_workspace_text(path)
        actual_count = content.count(old_text)
        if actual_count != expected_count:
            raise ValueError(
                f"old_text matched {actual_count} times; expected {expected_count}; file unchanged"
            )
        updated = content.replace(old_text, new_text)
        _write_text_atomic(resolved, updated)
    return {
        "path": path,
        "replacements": actual_count,
        "size_bytes": resolved.stat().st_size,
        "sha256": _file_sha256(resolved),
    }


@mcp.tool()
def insert_workspace_text(
    path: str,
    anchor: str,
    text: str,
    position: Literal["before", "after"],
    expected_count: int = 1,
) -> dict[str, Any]:
    """Atomically insert text before or after an exact anchor.

    The complete file is validated before any write. If anchor occurs a
    different number of times, the operation fails and leaves the file untouched.
    When expected_count is greater than one, insertion occurs at every match.
    """
    if not anchor:
        raise ValueError("anchor must not be empty")
    _validate_expected_count(expected_count)
    with _workspace_write_lock:
        resolved, content = _read_workspace_text(path)
        actual_count = content.count(anchor)
        if actual_count != expected_count:
            raise ValueError(
                f"anchor matched {actual_count} times; expected {expected_count}; file unchanged"
            )
        replacement = f"{text}{anchor}" if position == "before" else f"{anchor}{text}"
        updated = content.replace(anchor, replacement)
        _write_text_atomic(resolved, updated)
    return {
        "path": path,
        "insertions": actual_count,
        "position": position,
        "size_bytes": resolved.stat().st_size,
        "sha256": _file_sha256(resolved),
    }


@mcp.tool()
def apply_workspace_patch(patch: str) -> dict[str, Any]:
    """Apply one unified diff atomically relative to the shared workspace.

    Use standard Git-style paths such as a/draft/article.md and
    b/draft/article.md. The complete patch is checked before application. If
    any file path, hunk, or context does not match, the operation fails without
    applying any part of the patch. Three-way merging and partial rejects are
    intentionally disabled.
    """
    if not patch.strip():
        raise ValueError("patch must not be empty")
    if "\0" in patch:
        raise ValueError("patch must not contain NUL bytes")
    if len(patch) > CONFIG.max_patch_chars:
        raise ValueError(f"patch exceeds the {CONFIG.max_patch_chars} character limit")
    if not CONFIG.workspace_root.is_dir():
        raise FileNotFoundError("workspace root does not exist")

    with _workspace_write_lock:
        _git_apply(patch, check=True)
        _git_apply(patch, check=False)
    return {"applied": True, "patch_chars": len(patch)}


@mcp.tool()
def spawn_chatgpt_subagent(task: str) -> dict[str, Any]:
    """Asynchronously delegate one complete task to a new ChatGPT Web conversation.

    The delegated input, authoritative output, execution logs, and lifecycle state
    all live under the returned task_dir. output.md is absent while queued, created
    by the child with a fixed started marker, and atomically replaced by the final
    result plus a fixed completion marker.
    """
    if not CONFIG.chatgpt_automation_enabled:
        raise RuntimeError(
            "ChatGPT browser automation is disabled; set "
            "MCP_CHATGPT_AUTOMATION_ENABLED=true for the local MCP process"
        )
    if not task.strip():
        raise ValueError("task must not be empty")
    if len(task) > CONFIG.max_read_chars:
        raise ValueError(f"task exceeds the {CONFIG.max_read_chars} character limit")

    task_id, task_dir = _reserve_workspace_task_dir()
    relative_task_dir = task_dir.relative_to(CONFIG.workspace_root).as_posix()
    normalized_input = f"{relative_task_dir}/input.md"
    normalized_output = f"{relative_task_dir}/output.md"
    try:
        _write_text_atomic(task_dir / "input.md", task)
        _render_chatgpt_subagent_prompt(normalized_input, normalized_output)
    except BaseException:
        shutil.rmtree(task_dir, ignore_errors=True)
        raise

    # The delegated runner owns the authoritative one-hour post-send completion
    # timeout. Give the outer background supervisor the full configured maximum so
    # browser-profile queueing/setup cannot consume that completion budget.
    result = _start_workspace_task(
        "python",
        _chatgpt_subagent_task_code(normalized_input, normalized_output),
        CONFIG.workspace_root,
        ".",
        CONFIG.max_background_timeout_seconds,
        task_id=task_id,
        task_dir=task_dir,
        metadata={
            "kind": "chatgpt_subagent",
            "input_path": normalized_input,
            "output_path": normalized_output,
        },
    )
    return {
        **result,
        "input_path": normalized_input,
        "output_path": normalized_output,
        "completion": "task-owned output file with started and completed sentinels",
        "message": "Sub-agent queued; poll get_workspace_task with task_id.",
    }


@mcp.tool()
def restart_mcp_server() -> dict[str, Any]:
    """Reload modified MCP Python code by safely restarting this Docker container.

    Call this after changing the MCP server's Python source files. The tool first imports
    the updated server in a fresh Python process. If validation succeeds, it returns a response and
    then exits; Docker's restart policy starts it again. This does not rebuild the image,
    so dependency, requirements.txt, Dockerfile, or Compose changes need host deployment.
    """
    if not Path("/.dockerenv").exists():
        raise RuntimeError("self-restart is only available inside Docker")

    _run_checked(
        ["python", "-c", "import server"],
        CONFIG.project_root,
    )

    timer = threading.Timer(CONFIG.restart_delay_seconds, os._exit, args=(0,))
    timer.daemon = True
    timer.start()
    return {
        "status": "restarting",
        "validated": True,
        "delay_seconds": CONFIG.restart_delay_seconds,
        "message": "Updated Python code validated; reconnect after the container restarts.",
    }


class _BoundedBytesCapture:
    def __init__(self, limit_bytes: int) -> None:
        self.limit = max(1024, int(limit_bytes))
        marker = b"\n... output truncated; middle omitted ...\n"
        payload = max(0, self.limit - len(marker))
        self.head_limit = payload // 2
        self.tail_limit = payload - self.head_limit
        self.marker = marker
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def consume(self, data: bytes) -> None:
        if not data:
            return
        self.total += len(data)
        offset = 0
        if len(self.head) < self.head_limit:
            take = min(len(data), self.head_limit - len(self.head))
            self.head.extend(data[:take])
            offset = take
        if offset < len(data):
            self.tail.extend(data[offset:])
            if len(self.tail) > self.tail_limit:
                del self.tail[: len(self.tail) - self.tail_limit]

    @property
    def truncated(self) -> bool:
        return self.total > self.head_limit + self.tail_limit

    def value(self) -> bytes:
        if self.truncated:
            return bytes(self.head) + self.marker + bytes(self.tail)
        return bytes(self.head) + bytes(self.tail)


def _drain_capture_pipe(stream: Any, capture: _BoundedBytesCapture) -> None:
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            capture.consume(chunk)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _run_foreground_command(
    language: Literal["python", "shell"],
    code: str,
    working_dir: Path,
    timeout: int,
) -> dict[str, Any]:
    command = ["python", "-c", code] if language == "python" else ["/bin/sh", "-c", code]
    process = subprocess.Popen(
        command,
        cwd=working_dir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_execution_environment(),
        start_new_session=True,
        close_fds=True,
    )
    stdout_capture = _BoundedBytesCapture(CONFIG.max_foreground_capture_bytes)
    stderr_capture = _BoundedBytesCapture(CONFIG.max_foreground_capture_bytes)
    assert process.stdout is not None and process.stderr is not None
    threads = [
        threading.Thread(
            target=_drain_capture_pipe, args=(process.stdout, stdout_capture), daemon=True
        ),
        threading.Thread(
            target=_drain_capture_pipe, args=(process.stderr, stderr_capture), daemon=True
        ),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        process.wait(timeout=timeout)
        if os.name == "posix" and _process_group_members(process.pid):
            _stop_process_group(process)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_process_group(process)
    finally:
        try:
            process.wait(timeout=1)
        except (subprocess.TimeoutExpired, ChildProcessError):
            pass
        for thread in threads:
            thread.join(timeout=10)

    stdout_text = stdout_capture.value().decode("utf-8", errors="replace")
    stderr_text = stderr_capture.value().decode("utf-8", errors="replace")
    stdout_value, stdout_chars_truncated = _truncate(stdout_text)
    stderr_value, stderr_chars_truncated = _truncate(stderr_text)
    result = {
        "exit_code": process.returncode,
        "stdout": stdout_value,
        "stderr": stderr_value,
        "truncated": (
            stdout_capture.truncated
            or stderr_capture.truncated
            or stdout_chars_truncated
            or stderr_chars_truncated
        ),
    }
    if timed_out:
        result["timed_out"] = True
    return result


@mcp.tool()
async def run_workspace_code(
    language: Literal["python", "shell"],
    code: str,
    cwd: str = ".",
    timeout_seconds: int | None = None,
    background: bool = False,
) -> dict[str, Any]:
    """Run Python or POSIX shell code inside the container and shared workspace.

    Foreground subprocess waiting is offloaded so unrelated MCP requests remain
    responsive. Set background=true for persistent task execution.
    """
    working_dir = _workspace_path(cwd, must_exist=True)
    if not working_dir.is_dir():
        raise ValueError("cwd is not a directory")

    if background:
        timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else CONFIG.default_background_timeout_seconds
        )
        if not 1 <= timeout <= CONFIG.max_background_timeout_seconds:
            raise ValueError(
                "background timeout_seconds must be between 1 and "
                f"{CONFIG.max_background_timeout_seconds}"
            )
        cwd_relative = working_dir.relative_to(CONFIG.workspace_root).as_posix()
        return _start_workspace_task(language, code, working_dir, cwd_relative, timeout)

    timeout = timeout_seconds if timeout_seconds is not None else CONFIG.default_timeout_seconds
    if not 1 <= timeout <= CONFIG.max_timeout_seconds:
        raise ValueError(f"timeout_seconds must be between 1 and {CONFIG.max_timeout_seconds}")
    return await asyncio.to_thread(
        _run_foreground_command, language, code, working_dir, timeout
    )


@mcp.tool()
async def cancel_workspace_task(
    task_id: str,
    wait_seconds: int = 5,
) -> dict[str, Any]:
    """Request cancellation of a queued or running background task."""
    if not 0 <= wait_seconds <= CONFIG.task_max_wait_seconds:
        raise ValueError(
            f"wait_seconds must be between 0 and {CONFIG.task_max_wait_seconds}"
        )
    task_dir = _task_dir(task_id)
    status_path = task_dir / "status.json"
    if not status_path.is_file():
        raise FileNotFoundError(f"unknown task: {task_id}")
    status = _read_task_json(status_path)
    if status.get("status") in {"queued", "running"}:
        _write_text_atomic(task_dir / "cancel.requested", f"{_utc_now()}\n")
    return await get_workspace_task(task_id, wait_seconds=wait_seconds)


@mcp.tool()
async def get_workspace_task(
    task_id: str,
    wait_seconds: int = CONFIG.task_default_wait_seconds,
) -> dict[str, Any]:
    """Wait without blocking unrelated MCP requests and return task status."""
    if not 0 <= wait_seconds <= CONFIG.task_max_wait_seconds:
        raise ValueError(
            f"wait_seconds must be between 0 and {CONFIG.task_max_wait_seconds}"
        )
    task_dir = _task_dir(task_id)
    status_path = task_dir / "status.json"
    if not status_path.is_file():
        raise FileNotFoundError(f"unknown task: {task_id}")

    deadline = time.monotonic() + wait_seconds
    while True:
        status = await asyncio.to_thread(_recover_workspace_task, task_dir, task_id)
        if status.get("status") not in {"queued", "running"}:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(0.5, remaining))

    relative_task_dir = task_dir.relative_to(CONFIG.workspace_root).as_posix()
    return {
        **status,
        "task_dir": relative_task_dir,
        "stdout_path": f"{relative_task_dir}/stdout.log",
        "stderr_path": f"{relative_task_dir}/stderr.log",
    }


@mcp.custom_route("/diff/", methods=["GET"], include_in_schema=False)
async def draft_diff_page(request: Request) -> Response:
    return HTMLResponse(CONFIG.draft_diff_page.read_text(encoding="utf-8"))


@mcp.custom_route("/diff/api/files", methods=["GET"], include_in_schema=False)
async def draft_diff_files(request: Request) -> Response:
    return JSONResponse({"files": _draft_files()})


@mcp.custom_route("/diff/api/commits", methods=["GET"], include_in_schema=False)
async def draft_diff_commits(request: Request) -> Response:
    try:
        relative_path = request.query_params.get("path", "")
        return JSONResponse({"path": relative_path, "commits": _git_commits_for_file(relative_path)})
    except (ValueError, RuntimeError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


@mcp.custom_route("/diff/api/diff", methods=["GET"], include_in_schema=False)
async def draft_diff_content(request: Request) -> Response:
    try:
        relative_path = request.query_params.get("path", "")
        revision = request.query_params.get("revision", "HEAD")
        revision = _validated_revision(revision)
        resolved, normalized = _draft_file(relative_path, must_exist=False)
        if not resolved.exists():
            tracked = subprocess.run(
                ["git", "--literal-pathspecs", "ls-files", "--error-unmatch", "--", normalized],
                cwd=CONFIG.draft_root,
                text=True,
                capture_output=True,
                timeout=CONFIG.max_timeout_seconds,
                check=False,
            ).returncode == 0
            if tracked:
                return JSONResponse(
                    {
                        "path": relative_path,
                        "revision": revision,
                        "diff": "",
                        "has_diff": True,
                        "status": "deleted",
                    }
                )
        diff = _git_diff_for_file(relative_path, revision)
        return JSONResponse(
            {
                "path": relative_path,
                "revision": revision,
                "diff": diff,
                "has_diff": bool(diff),
                "status": "changed",
            }
        )
    except (ValueError, RuntimeError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


@mcp.custom_route("/diff/api/commit", methods=["POST"], include_in_schema=False)
async def draft_diff_commit(request: Request) -> Response:
    try:
        payload = await request.json()
        password = str(payload.get("password", ""))
        if not hmac.compare_digest(password, CONFIG.draft_commit_password):
            return JSONResponse({"error": "密码错误"}, status_code=401)

        _, relative_path = _draft_file(str(payload.get("path", "")), must_exist=False)
        if not _git_diff_for_file(relative_path):
            return JSONResponse({"error": "这个文件没有可提交的 diff"}, status_code=409)

        message = str(payload.get("message", "")).strip() or f"Update {Path(relative_path).name}"
        if "\n" in message or "\r" in message or len(message) > 200:
            return JSONResponse({"error": "提交说明必须是 1–200 个字符的单行文本"}, status_code=400)

        _run_checked(["git", "--literal-pathspecs", "add", "--", relative_path], CONFIG.draft_root)
        result = _run_checked(
            ["git", "--literal-pathspecs", "commit", "-m", message, "--", relative_path],
            CONFIG.draft_root,
        )
        commit_hash = _run_checked(["git", "rev-parse", "--short", "HEAD"], CONFIG.draft_root).strip()
        return JSONResponse(
            {"status": "committed", "path": relative_path, "commit": commit_hash, "output": result}
        )
    except (ValueError, RuntimeError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


@mcp.custom_route("/diff/api/revert", methods=["POST"], include_in_schema=False)
async def draft_diff_revert(request: Request) -> Response:
    try:
        payload = await request.json()
        password = str(payload.get("password", ""))
        if not hmac.compare_digest(password, CONFIG.draft_commit_password):
            return JSONResponse({"error": "密码错误"}, status_code=401)

        resolved, relative_path = _draft_file(str(payload.get("path", "")), must_exist=False)
        with _workspace_write_lock:
            if not _git_diff_for_file(relative_path):
                return JSONResponse({"error": "这个文件没有可撤销的 diff"}, status_code=409)

            tracked = subprocess.run(
                ["git", "--literal-pathspecs", "ls-files", "--error-unmatch", "--", relative_path],
                cwd=CONFIG.draft_root,
                text=True,
                capture_output=True,
                timeout=CONFIG.max_timeout_seconds,
                check=False,
            ).returncode == 0
            if tracked:
                _run_checked(
                    [
                        "git",
                        "--literal-pathspecs",
                        "restore",
                        "--source=HEAD",
                        "--staged",
                        "--worktree",
                        "--",
                        relative_path,
                    ],
                    CONFIG.draft_root,
                )
                action = "restored"
            else:
                if not resolved.is_file():
                    raise ValueError("untracked path is not a file")
                resolved.unlink()
                action = "deleted"

        return JSONResponse({"status": "reverted", "path": relative_path, "action": action})
    except (OSError, ValueError, RuntimeError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


@mcp.resource("skill://{name}")
def skill_resource(name: str) -> str:
    """Expose a skill document as an MCP resource."""
    return _skill_file(name).read_text(encoding="utf-8")


if __name__ == "__main__":
    CONFIG.workspace_root.mkdir(parents=True, exist_ok=True)
    CONFIG.tasks_root.mkdir(parents=True, exist_ok=True)
    _prune_old_workspace_tasks()
    if CONFIG.execution_home:
        Path(CONFIG.execution_home).mkdir(parents=True, exist_ok=True)
    mcp.run(transport="streamable-http")
