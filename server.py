"""MCP tools for shared writing skills and workspace file operations."""

from __future__ import annotations

import html
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, EmbeddedResource, TextResourceContents

from config import CONFIG


mcp = FastMCP(
    CONFIG.server_name,
    instructions=(
        "This server exposes skills and a persistent shared workspace at /opt/workspace. All file paths "
        "are relative to the workspace root. Workspace layout: skills/<name>/SKILL.md contains "
        "reusable writing workflows; draft/<year-month>/<article>.md contains article drafts and is a Git "
        "repository; mymcp/ contains this MCP server's code; scripts/ contains other content management scripts. Before a writing task, call "
        "list_skills and load the relevant skills. Use list_draft_articles to discover existing "
        "drafts before reading or editing them. Use render_diff_html when user asks to generate any diff on articles."
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


def _render_word_diff(text: str) -> str:
    rendered: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith("[-", index):
            end = text.find("-]", index + 2)
            if end != -1:
                rendered.append(
                    f'<span class="del">{html.escape(text[index + 2:end])}</span>'
                )
                index = end + 2
                continue
        if text.startswith("{+", index):
            end = text.find("+}", index + 2)
            if end != -1:
                rendered.append(
                    f'<span class="add">{html.escape(text[index + 2:end])}</span>'
                )
                index = end + 2
                continue
        character = text[index]
        rendered.append("\n" if character == "\n" else html.escape(character))
        index += 1
    return "".join(rendered)


def _validated_repo_paths(repo: Path, paths: list[str] | None) -> list[str]:
    validated: list[str] = []
    for value in paths or []:
        draft_prefix = f"{CONFIG.draft_dirname}/"
        normalized = value.removeprefix(draft_prefix)
        candidate = Path(normalized)
        if not normalized or candidate.is_absolute():
            raise ValueError(
                "diff paths must be relative to draft, optionally prefixed with 'draft/'"
            )
        resolved = (repo / candidate).resolve()
        if resolved != repo and repo not in resolved.parents:
            raise ValueError(f"diff path escapes the repository: {value}")
        validated.append(candidate.as_posix())
    return validated


@mcp.tool()
def list_skills() -> list[dict[str, str]]:
    """List available skills with names, versions, and descriptions."""
    return _all_skills()


@mcp.tool()
def list_draft_articles() -> list[dict[str, str]]:
    """List draft Markdown articles with workspace-relative paths and first-line titles.

    Use this to locate an existing draft from the user's description before reading
    or editing it. Returned paths begin with "draft/" and can be passed directly to
    read_workspace_file, write_workspace_file, or render_diff_html.
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
def render_diff_html(paths: list[str] | None = None) -> CallToolResult:
    """Return the draft repository's current Git word diff as an HTML file.

    Optional paths may be relative to draft or workspace-relative paths beginning with
    "draft/". The tool is read-only and does not write a diff artifact into the
    workspace. Omit paths to include every tracked draft change.
    """
    repo_root = CONFIG.draft_root
    if _run_checked(["git", "rev-parse", "--is-inside-work-tree"], repo_root).strip() != "true":
        raise ValueError("draft is not a Git work tree")

    selected_paths = _validated_repo_paths(repo_root, paths)
    path_args = ["--", *selected_paths] if selected_paths else []
    status = _run_checked(["git", "status", "--short", *path_args], repo_root)
    raw_diff = _run_checked(
        ["git", "diff", "--no-ext-diff", "--no-color", "--word-diff", *path_args],
        repo_root,
    )
    rendered_diff = _render_word_diff(raw_diff)

    html_document = f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>git diff</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 24px; color: #222; background: #fafafa; }}
    .meta {{ background: white; border: 1px solid #e5e7eb; border-radius: 12px; padding: 16px; margin-bottom: 16px; }}
    .code {{ background: white; border: 1px solid #e5e7eb; border-radius: 12px; padding: 16px; overflow-x: auto; white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; line-height: 1.6; }}
    .add {{ background: #dcfce7; color: #166534; font-weight: 700; padding: 0 2px; border-radius: 4px; }}
    .del {{ background: #fee2e2; color: #991b1b; text-decoration: line-through; padding: 0 2px; border-radius: 4px; }}
    code {{ background: #f3f4f6; padding: 2px 6px; border-radius: 6px; }}
    pre {{ margin: 0; white-space: pre-wrap; }}
  </style>
</head>
<body>
  <div class="meta">
    <h2 style="margin-top:0">Git diff HTML</h2>
    <div><strong>Repo:</strong> <code>{html.escape(str(repo_root))}</code></div>
    <div><strong>Paths:</strong> <code>{html.escape(' '.join(selected_paths) if selected_paths else '(all changed files)')}</code></div>
    <div><strong>Mode:</strong> <code>git diff --word-diff</code></div>
    <div style="margin-top:12px"><strong>Status</strong></div>
    <pre>{html.escape(status or '(clean)')}</pre>
  </div>
  <div class="code">{rendered_diff or '(no diff)'}</div>
</body>
</html>
'''
    return CallToolResult(
        content=[
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri="file:///draft-diff.html",
                    mimeType="text/html",
                    text=html_document,
                )
            )
        ]
    )


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


@mcp.resource("skill://{name}")
def skill_resource(name: str) -> str:
    """Expose a skill document as an MCP resource."""
    return _skill_file(name).read_text(encoding="utf-8")


if __name__ == "__main__":
    CONFIG.workspace_root.mkdir(parents=True, exist_ok=True)
    Path("/tmp/mcp-home").mkdir(parents=True, exist_ok=True)
    mcp.run(transport="streamable-http")
