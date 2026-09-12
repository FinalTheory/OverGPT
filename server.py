"""MCP tools for shared writing skills and workspace file operations."""

from __future__ import annotations

import hmac
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from config import CONFIG


mcp = FastMCP(
    CONFIG.server_name,
    instructions=(
        "This server exposes skills and a persistent shared workspace at /opt/workspace. All file paths "
        "are relative to the workspace root. Workspace layout: skills/<name>/SKILL.md contains "
        "reusable writing workflows; draft/<year-month>/<article>.md contains article drafts and is a Git "
        "repository; mymcp/ contains this MCP server's code; scripts/ contains other content management scripts. Before a writing task, call "
        "list_skills and load the relevant skills. Use list_draft_articles to discover existing "
        "drafts before reading or editing them. Prefer read_workspace_range plus "
        "replace_workspace_text or insert_workspace_text for precise edits; their match-count "
        "guards prevent accidental broad changes."
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
    return value[: CONFIG.max_output_chars], True


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


def _validate_expected_count(expected_count: int) -> None:
    if expected_count < 1:
        raise ValueError("expected_count must be at least 1")


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
    if CONFIG.draft_root not in resolved.parents or (must_exist and not resolved.is_file()):
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
        ["git", "ls-files", "--error-unmatch", "--", normalized],
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
        ["git", "diff", "--no-ext-diff", "--no-color", *word_diff_args, revision, "--", normalized]
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
    """Read a UTF-8 text file using a path relative to the shared workspace."""
    resolved, content = _read_workspace_text(path)
    truncated = len(content) > CONFIG.max_read_chars
    return {
        "path": path,
        "content": content[: CONFIG.max_read_chars],
        "truncated": truncated,
        "size_bytes": resolved.stat().st_size,
    }


@mcp.tool()
def read_workspace_range(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    anchor: str | None = None,
    context_lines: int = 20,
) -> dict[str, Any]:
    """Read an inclusive line range or context surrounding one exact anchor.

    Paths are relative to the workspace. For line mode, provide both start_line
    and end_line using 1-based inclusive line numbers. For anchor mode, provide
    only anchor; it must occur exactly once, and context_lines lines are returned
    before and after it. The returned start_line/end_line identify the excerpt.
    """
    _, content = _read_workspace_text(path)
    lines = content.splitlines(keepends=True)
    total_lines = len(lines)

    if anchor is not None:
        if start_line is not None or end_line is not None:
            raise ValueError("provide either anchor or a line range, not both")
        if not anchor:
            raise ValueError("anchor must not be empty")
        if context_lines < 0:
            raise ValueError("context_lines must be non-negative")
        actual_count = content.count(anchor)
        if actual_count != 1:
            raise ValueError(f"anchor matched {actual_count} times; expected exactly 1")
        match_start = content.find(anchor)
        match_end = match_start + len(anchor)
        anchor_start_line = content.count("\n", 0, match_start) + 1
        anchor_end_line = content.count("\n", 0, max(match_start, match_end - 1)) + 1
        selected_start = max(1, anchor_start_line - context_lines)
        selected_end = min(total_lines, anchor_end_line + context_lines)
        mode = "anchor"
    else:
        if start_line is None or end_line is None:
            raise ValueError("provide anchor or both start_line and end_line")
        if start_line < 1 or end_line < start_line:
            raise ValueError("line range must be 1-based with end_line >= start_line")
        if end_line > total_lines:
            raise ValueError(f"end_line exceeds file length of {total_lines} lines")
        selected_start = start_line
        selected_end = end_line
        mode = "lines"

    excerpt = "".join(lines[selected_start - 1 : selected_end])
    truncated = len(excerpt) > CONFIG.max_read_chars
    return {
        "path": path,
        "mode": mode,
        "start_line": selected_start,
        "end_line": selected_end,
        "total_lines": total_lines,
        "content": excerpt[: CONFIG.max_read_chars],
        "truncated": truncated,
    }


@mcp.tool()
def write_workspace_file(path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    """Write a UTF-8 document atomically inside the shared workspace.

    Set overwrite=true only when replacing an existing file is intentional.
    """
    with _workspace_write_lock:
        resolved = _workspace_path(path)
        if resolved.exists() and not overwrite:
            raise FileExistsError("file already exists; set overwrite=true to replace it")
        _write_text_atomic(resolved, content)
    return {"path": path, "size_bytes": resolved.stat().st_size, "created": True}


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
def restart_mcp_server() -> dict[str, Any]:
    """Reload modified MCP Python code by safely restarting this Docker container.

    Call this after changing Python files in mymcp. The tool first imports the updated
    server in a fresh Python process. If validation succeeds, it returns a response and
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


@mcp.tool()
def run_workspace_code(
    language: Literal["python", "shell"],
    code: str,
    cwd: str = ".",
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Run Python or POSIX shell code inside the container and shared workspace.

    The working directory must stay inside the workspace. Use this for flexible
    document processing when the dedicated file tools are insufficient.
    """
    working_dir = _workspace_path(cwd, must_exist=True)
    if not working_dir.is_dir():
        raise ValueError("cwd is not a directory")

    timeout = timeout_seconds or CONFIG.default_timeout_seconds
    if not 1 <= timeout <= CONFIG.max_timeout_seconds:
        raise ValueError(f"timeout_seconds must be between 1 and {CONFIG.max_timeout_seconds}")

    command = ["python", "-c", code] if language == "python" else ["/bin/sh", "-c", code]
    try:
        result = subprocess.run(
            command,
            cwd=working_dir,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "HOME": "/tmp/mcp-home"},
        )
        stdout, stdout_truncated = _truncate(result.stdout)
        stderr, stderr_truncated = _truncate(result.stderr)
        return {
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
        }
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        return {
            "exit_code": None,
            "stdout": _truncate(stdout)[0],
            "stderr": _truncate(stderr)[0],
            "timed_out": True,
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
                ["git", "ls-files", "--error-unmatch", "--", normalized],
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

        _run_checked(["git", "add", "--", relative_path], CONFIG.draft_root)
        result = _run_checked(
            ["git", "commit", "-m", message, "--", relative_path],
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
                ["git", "ls-files", "--error-unmatch", "--", relative_path],
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
    Path("/tmp/mcp-home").mkdir(parents=True, exist_ok=True)
    mcp.run(transport="streamable-http")
