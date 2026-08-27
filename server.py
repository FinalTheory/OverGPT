"""MCP tools for shared writing skills and workspace file operations."""

from __future__ import annotations

import hmac
import os
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
        "drafts before reading or editing them."
    ),
    host=CONFIG.host,
    port=CONFIG.port,
    streamable_http_path=CONFIG.mcp_path,
    stateless_http=True,
    json_response=True,
)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


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


def _git_diff_for_file(relative_path: str) -> str:
    resolved, normalized = _draft_file(relative_path, must_exist=False)
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
        ["git", "diff", "--no-ext-diff", "--no-color", *word_diff_args, "HEAD", "--", normalized]
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
    resolved = _workspace_path(path, must_exist=True)
    if not resolved.is_file():
        raise ValueError("path is not a file")
    content = resolved.read_text(encoding="utf-8")
    truncated = len(content) > CONFIG.max_read_chars
    return {
        "path": path,
        "content": content[: CONFIG.max_read_chars],
        "truncated": truncated,
        "size_bytes": resolved.stat().st_size,
    }


@mcp.tool()
def write_workspace_file(path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    """Write a UTF-8 document atomically inside the shared workspace.

    Set overwrite=true only when replacing an existing file is intentional.
    """
    resolved = _workspace_path(path)
    if resolved.exists() and not overwrite:
        raise FileExistsError("file already exists; set overwrite=true to replace it")
    _write_text_atomic(resolved, content)
    return {"path": path, "size_bytes": resolved.stat().st_size, "created": True}


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


@mcp.custom_route("/diff/api/diff", methods=["GET"], include_in_schema=False)
async def draft_diff_content(request: Request) -> Response:
    try:
        relative_path = request.query_params.get("path", "")
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
                    {"path": relative_path, "diff": "", "has_diff": True, "status": "deleted"}
                )
        diff = _git_diff_for_file(relative_path)
        return JSONResponse(
            {"path": relative_path, "diff": diff, "has_diff": bool(diff), "status": "changed"}
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


@mcp.resource("skill://{name}")
def skill_resource(name: str) -> str:
    """Expose a skill document as an MCP resource."""
    return _skill_file(name).read_text(encoding="utf-8")


if __name__ == "__main__":
    CONFIG.workspace_root.mkdir(parents=True, exist_ok=True)
    Path("/tmp/mcp-home").mkdir(parents=True, exist_ok=True)
    mcp.run(transport="streamable-http")
