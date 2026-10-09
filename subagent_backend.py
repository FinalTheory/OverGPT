"""Preferred Codex CLI sub-agent backend with ChatGPT Web fallback."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from config import CONFIG
from chatgpt_playwright import (
    SUBAGENT_COMPLETED_SENTINEL,
    normalize_workspace_file,
    normalize_workspace_relative_path,
    reserve_browser_slot,
    send_subagent_task,
    validate_subagent_path_pair,
)


class CodexPreflightError(RuntimeError):
    """Codex could not prove availability before the delegated task began."""


def _codex_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(CONFIG.codex_home)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _resolve_codex_binary() -> str:
    configured = CONFIG.codex_binary.strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute() and candidate.is_file():
            return str(candidate)
        resolved = shutil.which(configured)
        if resolved:
            return resolved

    # Development bootstrap: this persisted workspace-local installation lets the
    # currently running container use Codex before the next Docker image rebuild.
    bootstrap = CONFIG.workspace_root / ".codex-cli" / "node_modules" / ".bin" / "codex"
    if bootstrap.is_file():
        return str(bootstrap)
    raise CodexPreflightError("Codex CLI is not installed")


def _run_logged(command: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        input=stdin,
        cwd=CONFIG.workspace_root,
        env=_codex_environment(),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(
            result.stderr,
            end="" if result.stderr.endswith("\n") else "\n",
            file=sys.stderr,
        )
    return result


def _codex_base_command(binary: str, *, sandbox: str | None) -> list[str]:
    command = [
        binary,
        "exec",
        "--model",
        CONFIG.codex_model,
        "--cd",
        str(CONFIG.workspace_root),
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--color",
        "never",
        "--config",
        f'model_reasoning_effort="{CONFIG.codex_reasoning_effort}"',
    ]
    if sandbox is None:
        # This worker already runs inside the MCP Docker container, which is the
        # authoritative outer sandbox. Codex's bubblewrap sandbox cannot create
        # namespaces under the container's no-new-privileges/cap-drop policy.
        command.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        command.extend(
            [
                "--sandbox",
                sandbox,
                "--config",
                'approval_policy="never"',
            ]
        )
    return command


def _codex_preflight(binary: str, task_dir: Path) -> None:
    """Prove auth/model/quota/service availability before user task execution."""
    CONFIG.codex_home.mkdir(parents=True, exist_ok=True)
    login = _run_logged([binary, "login", "status"])
    if login.returncode != 0:
        raise CodexPreflightError("Codex is not authenticated")

    result_file = task_dir / ".codex-preflight.txt"
    result_file.unlink(missing_ok=True)
    command = [
        *_codex_base_command(binary, sandbox="read-only"),
        "--output-last-message",
        str(result_file),
        "-",
    ]
    result = _run_logged(
        command,
        stdin="Reply with exactly CODEX_READY. Do not use tools or inspect files.",
    )
    if result.returncode != 0 or not result_file.is_file():
        raise CodexPreflightError(
            f"Codex availability preflight failed with exit code {result.returncode}"
        )


def _write_codex_output(output_file: Path, final_message: str) -> None:
    text = final_message.rstrip()
    payload = (
        f"{text}\n\n{SUBAGENT_COMPLETED_SENTINEL}"
        if text
        else SUBAGENT_COMPLETED_SENTINEL
    )
    temporary = output_file.with_name(f".{output_file.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, output_file)
    finally:
        temporary.unlink(missing_ok=True)


def _run_codex_subagent(
    binary: str,
    input_path: str,
    output_path: str,
    task_dir: Path,
) -> dict[str, Any]:
    last_message = task_dir / ".codex-last-message.md"
    last_message.unlink(missing_ok=True)
    prompt = (
        "You are a delegated sub-agent working independently for another agent. "
        f"Your authoritative task is in /opt/workspace/{input_path}. Read that file completely, "
        "then complete the task autonomously. You may inspect and modify files under "
        "/opt/workspace as required by the task. Do not ask follow-up questions; make reasonable "
        "assumptions when necessary. Do not write the parent output file yourself. Your final "
        "response must be the complete result/checkpoint that the parent agent should receive."
    )
    command = [
        *_codex_base_command(binary, sandbox=None),
        "--output-last-message",
        str(last_message),
        "-",
    ]
    result = _run_logged(command, stdin=prompt)
    if result.returncode != 0:
        raise RuntimeError(f"Codex sub-agent failed with exit code {result.returncode}")
    if not last_message.is_file():
        raise RuntimeError("Codex completed without producing a final message")
    final_message = last_message.read_text(encoding="utf-8")
    if not final_message.strip():
        raise RuntimeError("Codex produced an empty final message")

    output_file = normalize_workspace_file(
        CONFIG.workspace_root, output_path, must_exist=False
    )
    _write_codex_output(output_file, final_message)
    return {
        "status": "completed",
        "backend": "codex",
        "model": CONFIG.codex_model,
        "reasoning_effort": CONFIG.codex_reasoning_effort,
        "output_path": output_path,
    }


def run_subagent_task(input_path: str, output_path: str) -> dict[str, Any]:
    """Run Codex when available; otherwise fall back to the browser backend."""
    normalized_input = normalize_workspace_relative_path(input_path)
    normalized_output = normalize_workspace_relative_path(output_path)
    if normalized_input == normalized_output:
        raise ValueError("input_path and output_path must be different files")
    task_id = validate_subagent_path_pair(
        normalized_input, normalized_output, CONFIG.tasks_workspace_path
    )
    input_file = normalize_workspace_file(
        CONFIG.workspace_root, normalized_input, must_exist=True
    )
    task_dir = input_file.parent

    if CONFIG.codex_subagent_enabled:
        try:
            binary = _resolve_codex_binary()
            _codex_preflight(binary, task_dir)
        except CodexPreflightError as error:
            print(
                f"Codex preflight unavailable ({error}); trying ChatGPT Web fallback.",
                file=sys.stderr,
            )
        else:
            return _run_codex_subagent(
                binary, normalized_input, normalized_output, task_dir
            )

    if not CONFIG.chatgpt_automation_enabled:
        raise RuntimeError(
            "Codex is unavailable and ChatGPT browser fallback is disabled"
        )

    slot = reserve_browser_slot(task_id, task_dir)
    result = send_subagent_task(
        normalized_input,
        normalized_output,
        wait_for_completion=True,
        reservation_slot=slot,
    )
    return {
        **result,
        "backend": "chatgpt_browser_fallback",
        "browser_slot": slot,
    }


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: subagent_backend.py <input_path> <output_path>")
    print(json.dumps(run_subagent_task(sys.argv[1], sys.argv[2]), ensure_ascii=False))


if __name__ == "__main__":
    main()
