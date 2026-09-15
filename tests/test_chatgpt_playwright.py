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
from chatgpt_playwright import (
    _browser_profile_lock,
    _wait_for_file_completion,
    send_prompt,
)

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

    with tempfile.TemporaryDirectory(prefix="mymcp-lock-") as temp_dir:
        profile = Path(temp_dir, "profile")
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()

        def hold_first_profile_lock() -> None:
            with _browser_profile_lock(profile):
                first_entered.set()
                if not release_first.wait(2):
                    raise RuntimeError("timed out waiting to release the first profile lock")

        def acquire_second_profile_lock() -> None:
            if not first_entered.wait(2):
                raise RuntimeError("first profile lock was never acquired")
            with _browser_profile_lock(profile):
                second_entered.set()

        first = threading.Thread(target=hold_first_profile_lock)
        second = threading.Thread(target=acquire_second_profile_lock)
        first.start()
        second.start()
        if not first_entered.wait(2):
            raise RuntimeError("first profile lock was never acquired")
        time.sleep(0.1)
        if second_entered.is_set():
            raise RuntimeError("shared browser profile lock did not serialize access")
        release_first.set()
        first.join(2)
        second.join(2)
        if first.is_alive() or second.is_alive() or not second_entered.is_set():
            raise RuntimeError("shared browser profile lock did not hand off cleanly")
    print("shared browser profile access is serialized across callers: ok")

    with tempfile.TemporaryDirectory(prefix="mymcp-workspace-") as workspace:
        os.environ["MCP_WORKSPACE_ROOT"] = workspace
        os.environ["MCP_CHATGPT_AUTOMATION_ENABLED"] = "true"
        import server

        file_api_root = "file-api"
        created_file = server.write_workspace_file(
            f"{file_api_root}/nested/example.py",
            "alpha = 1\nneedle = 'present'\n",
        )
        initial_read = server.read_workspace_file(
            f"{file_api_root}/nested/example.py"
        )
        if created_file["sha256"] != initial_read["sha256"]:
            raise RuntimeError("write/read SHA-256 values disagree")
        if not initial_read["sha256"] or len(initial_read["sha256"]) != 64:
            raise RuntimeError("read_workspace_file did not return a SHA-256 digest")

        overwritten_file = server.write_workspace_file(
            f"{file_api_root}/nested/example.py",
            "alpha = 2\nneedle = 'present'\n",
            overwrite=True,
            expected_sha256=initial_read["sha256"],
        )
        try:
            server.write_workspace_file(
                f"{file_api_root}/nested/example.py",
                "stale overwrite\n",
                overwrite=True,
                expected_sha256=initial_read["sha256"],
            )
        except ValueError:
            pass
        else:
            raise RuntimeError("stale full-file overwrite was accepted")

        listed = server.list_workspace(file_api_root, depth=2)
        listed_paths = {entry["path"] for entry in listed["entries"]}
        if f"{file_api_root}/nested/example.py" not in listed_paths:
            raise RuntimeError(f"list_workspace missed nested file: {listed}")

        searched = server.search_workspace_text(
            file_api_root,
            "needle",
            file_glob="*.py",
        )
        if not searched["results"] or searched["results"][0]["line"] != 2:
            raise RuntimeError(f"search_workspace_text missed expected result: {searched}")

        moved = server.move_workspace_file(
            f"{file_api_root}/nested/example.py",
            f"{file_api_root}/renamed.py",
            expected_sha256=overwritten_file["sha256"],
        )
        deleted = server.delete_workspace_file(
            f"{file_api_root}/renamed.py",
            expected_sha256=moved["sha256"],
        )
        if not moved["moved"] or not deleted["deleted"]:
            raise RuntimeError("move/delete workspace file operations failed")

        synchronous_timeout = server.run_workspace_code(
            "python",
            (
                "import subprocess, sys, time; "
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                "open('sync-child.pid', 'w').write(str(child.pid)); "
                "time.sleep(60)"
            ),
            timeout_seconds=1,
        )
        if not synchronous_timeout.get("timed_out"):
            raise RuntimeError(f"synchronous timeout was not reported: {synchronous_timeout}")
        child_pid = int(Path(workspace, "sync-child.pid").read_text(encoding="utf-8"))
        time.sleep(0.2)
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise RuntimeError("synchronous timeout left a descendant process running")

        cancellable = server.run_workspace_code(
            "python",
            "import time; time.sleep(60)",
            background=True,
            timeout_seconds=30,
        )
        cancelled = server.cancel_workspace_task(
            cancellable["task_id"],
            wait_seconds=3,
        )
        if cancelled["status"] != "cancelled":
            raise RuntimeError(f"background cancellation failed: {cancelled}")

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
        if "runpy" in task_code or "send_subagent_task" not in task_code:
            raise RuntimeError(
                "background task does not directly import the sub-agent runner"
            )
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
