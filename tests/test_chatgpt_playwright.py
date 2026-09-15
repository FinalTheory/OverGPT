"""Browser integration test that never connects to ChatGPT."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chatgpt_playwright
from chatgpt_playwright import _wait_for_file_completion, send_prompt

HTML = """
<!doctype html>
<meta charset="utf-8">
<div id="prompt-textarea" contenteditable="true"></div>
<button data-testid="send-button"
  onclick="location.href = document.querySelector('#prompt-textarea').innerText.includes('browser marker') ? '/?temporary-chat=true&amp;sent=1' : '/empty'">
  Send
</button>
<script>
document.querySelector('#prompt-textarea').addEventListener('input', () => {
  setTimeout(() => {
    const replacement = document.createElement('div');
    replacement.id = 'prompt-textarea';
    replacement.contentEditable = 'true';
    replacement.innerText = 'default barbecue suggestion';
    document.querySelector('#prompt-textarea').replaceWith(replacement);
  }, 0);
}, { once: true });
</script>
"""


class Handler(BaseHTTPRequestHandler):
    completion_file: Path | None = None
    completion_sentinel = "TESTCOMPLETE"

    def do_GET(self) -> None:
        if (
            "temporary-chat=true" in self.path
            and "sent=1" in self.path
            and self.completion_file is not None
        ):
            self.completion_file.write_text(
                f"test result\n{self.completion_sentinel}", encoding="utf-8"
            )
        body = (
            b'<div data-message-author-role="user">browser smoke test</div>'
            if "temporary-chat=true" in self.path and "sent=1" in self.path
            else HTML.encode()
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="mymcp-playwright-") as temp_dir:
            profile = Path(temp_dir, "profile")
            completion_file = Path(temp_dir, "result.md")
            Handler.completion_file = completion_file
            result = send_prompt(
                "browser marker",
                url=f"http://127.0.0.1:{server.server_port}/?temporary-chat=true",
                profile_dir=profile,
                browser_channel="" if sys.platform == "linux" else "chrome",
                headless=True,
                timeout_seconds=10,
                verification_markers=("browser marker",),
            )
            _wait_for_file_completion(
                completion_file, None, 10, Handler.completion_sentinel
            )
            completion_detected = completion_file.read_text(encoding="utf-8").endswith(
                Handler.completion_sentinel
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    if "temporary-chat=true" not in result["page_url"]:
        raise RuntimeError(f"send button was not triggered: {result}")
    if not completion_detected:
        raise RuntimeError(f"completion sentinel was not detected: {result}")
    print("Playwright send and completion-sentinel flow: ok")

    with tempfile.TemporaryDirectory(prefix="mymcp-workspace-") as workspace:
        os.environ["MCP_WORKSPACE_ROOT"] = workspace
        os.environ["MCP_CHATGPT_AUTOMATION_ENABLED"] = "true"
        import server

        old_finished = (datetime.now(UTC) - timedelta(days=31)).isoformat()
        recent_finished = datetime.now(UTC).isoformat()
        task_fixtures = {
            "task_00000000000000000000000000000001": ("succeeded", old_finished),
            "task_00000000000000000000000000000002": ("failed", recent_finished),
            "task_00000000000000000000000000000003": ("running", old_finished),
        }
        for task_id, (status, finished_at) in task_fixtures.items():
            task_dir = Path(workspace, ".mcp-tasks", task_id)
            task_dir.mkdir(parents=True)
            task_dir.joinpath("status.json").write_text(
                json.dumps({"status": status, "finished_at": finished_at}),
                encoding="utf-8",
            )
        removed = server._prune_old_workspace_tasks()
        if removed != 1:
            raise RuntimeError(f"expected one expired task removal, got {removed}")
        if Path(workspace, ".mcp-tasks", next(iter(task_fixtures))).exists():
            raise RuntimeError("expired completed task was not removed")
        for task_id in list(task_fixtures)[1:]:
            if not Path(workspace, ".mcp-tasks", task_id).is_dir():
                raise RuntimeError(f"active or recent task was removed: {task_id}")

        long_poll_task_id = "task_00000000000000000000000000000004"
        long_poll_dir = Path(workspace, ".mcp-tasks", long_poll_task_id)
        long_poll_dir.mkdir()
        long_poll_status = long_poll_dir / "status.json"
        long_poll_status.write_text(
            json.dumps({"task_id": long_poll_task_id, "status": "queued"}),
            encoding="utf-8",
        )

        def complete_long_poll_task() -> None:
            time.sleep(0.2)
            server._write_task_json(
                long_poll_status,
                {"task_id": long_poll_task_id, "status": "succeeded"},
            )

        updater = threading.Thread(target=complete_long_poll_task)
        updater.start()
        started = time.monotonic()
        long_poll_result = server.get_workspace_task(
            long_poll_task_id, wait_seconds=2
        )
        elapsed = time.monotonic() - started
        updater.join()
        if long_poll_result["status"] != "succeeded" or elapsed >= 2:
            raise RuntimeError(
                f"long poll did not return after task completion: {long_poll_result}"
            )
        if "stdout_tail" in long_poll_result or "stderr_tail" in long_poll_result:
            raise RuntimeError("task status unexpectedly returned log content")
        try:
            server.get_workspace_task(
                long_poll_task_id,
                wait_seconds=server.CONFIG.task_max_wait_seconds + 1,
            )
        except ValueError:
            pass
        else:
            raise RuntimeError("task wait above the configured maximum was accepted")

        delegated_task = "THIS_CONTENT_MUST_NOT_BE_IN_THE_BROWSER_PROMPT"
        with patch.object(
            server,
            "_start_workspace_task",
            return_value={"task_id": "task_demo", "status": "queued"},
        ) as mocked_start:
            delegated = server.spawn_chatgpt_subagent(delegated_task)
        task_code = mocked_start.call_args.args[1]
        if delegated_task in task_code:
            raise RuntimeError("input contents leaked into the browser prompt")
        if (
            delegated["input_path"] not in task_code
            or delegated["output_path"] not in task_code
        ):
            raise RuntimeError(
                "normalized paths were not passed to the background task"
            )
        input_file = Path(workspace, delegated["input_path"])
        if input_file.read_text(encoding="utf-8") != delegated_task:
            raise RuntimeError("delegated task was not persisted to the allocated input")
        if Path(delegated["input_path"]).parent != Path(
            delegated["output_path"]
        ).parent:
            raise RuntimeError(f"allocated paths do not share one directory: {delegated}")
        if delegated["status"] != "queued" or delegated["task_id"] != "task_demo":
            raise RuntimeError(f"sub-agent was not queued asynchronously: {delegated}")
        try:
            server.spawn_chatgpt_subagent("   ")
        except ValueError:
            pass
        else:
            raise RuntimeError("empty delegated task was accepted")
        with (
            patch.object(
                chatgpt_playwright,
                "send_prompt",
                return_value={
                    "status": "sent",
                    "page_url": "https://chatgpt.com/?temporary-chat=true",
                },
            ) as mocked_cli_send,
            patch.object(
                sys,
                "argv",
                [
                    "chatgpt_playwright.py",
                    "send",
                    "--input-path",
                    delegated["input_path"],
                    "--output-path",
                    delegated["output_path"],
                ],
            ),
        ):
            chatgpt_playwright.main()
        markers = mocked_cli_send.call_args.kwargs["verification_markers"]
        if (
            delegated["input_path"] not in markers
            or delegated["output_path"] not in markers
        ):
            raise RuntimeError(f"delegation markers were not verified: {markers}")
    print("MCP asynchronous delegation and path validation: ok")
    print("local send uses remote paths without local file access: ok")


if __name__ == "__main__":
    main()
