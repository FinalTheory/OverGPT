"""Browser integration test that never connects to ChatGPT."""

from __future__ import annotations

import asyncio
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
    _wait_for_file_creation,
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
    # Debug UI hold is an operator aid and must not slow deterministic tests.
    chatgpt_playwright.DEBUG_UI_HOLD_FILE = Path("/tmp/mymcp-debug-ui/test-hold-disabled")
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
                acknowledgement_wait=lambda: _wait_for_file_creation(
                    completion_file, None, 10
                ),
            )
            completion_detected = completion_file.read_text(encoding="utf-8").endswith(
                Handler.completion_sentinel
            )
            if "acknowledgement_wait_seconds" not in result:
                raise RuntimeError("browser context did not wait for task acknowledgement")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    if "temporary-chat=true" not in result["page_url"]:
        raise RuntimeError(f"send button was not triggered: {result}")
    if not completion_detected:
        raise RuntimeError(f"completion sentinel was not detected: {result}")
    print("Playwright send and completion-sentinel flow: ok")

    # Transaction-level browser semantics: retry only known pre-send failures and
    # never retry an ambiguous click.
    Handler.completion_file = None
    retry_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    retry_thread = threading.Thread(target=retry_server.serve_forever, daemon=True)
    retry_thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="mymcp-browser-txn-") as temp_dir:
            profile = Path(temp_dir, "profile")
            original_fill = chatgpt_playwright._fill_verified_prompt
            fill_calls = 0

            def fail_first_fill(*args: object, **kwargs: object) -> None:
                nonlocal fill_calls
                fill_calls += 1
                if fill_calls == 1:
                    raise chatgpt_playwright.ChatGPTPreSendError("synthetic pre-send remount")
                original_fill(*args, **kwargs)

            with patch.object(
                chatgpt_playwright, "_fill_verified_prompt", side_effect=fail_first_fill
            ):
                retried = send_prompt(
                    "browser marker",
                    url=f"http://127.0.0.1:{retry_server.server_port}/?temporary-chat=true",
                    profile_dir=profile,
                    browser_channel="" if sys.platform == "linux" else "chrome",
                    headless=True,
                    timeout_seconds=10,
                    verification_markers=("browser marker",),
                )
            if retried["status"] != "sent" or fill_calls < 2:
                raise RuntimeError(f"known pre-send failure was not retried: {retried}")

            from playwright.sync_api import Error as PlaywrightError

            click_calls = 0

            def ambiguous_click(*args: object, **kwargs: object) -> bool:
                nonlocal click_calls
                click_calls += 1
                raise PlaywrightError("synthetic click transport loss")

            with patch.object(
                chatgpt_playwright,
                "_click_verified_send_button",
                side_effect=ambiguous_click,
            ):
                ambiguous = send_prompt(
                    "browser marker",
                    url=f"http://127.0.0.1:{retry_server.server_port}/?temporary-chat=true",
                    profile_dir=profile,
                    browser_channel="" if sys.platform == "linux" else "chrome",
                    headless=True,
                    timeout_seconds=10,
                    verification_markers=("browser marker",),
                )
            if ambiguous["status"] != "sent_ambiguous" or click_calls != 1:
                raise RuntimeError(
                    f"ambiguous Send was retried or misclassified: {ambiguous}, calls={click_calls}"
                )

            generic_click_calls = 0

            def ambiguous_generic_click(*args: object, **kwargs: object) -> bool:
                nonlocal generic_click_calls
                generic_click_calls += 1
                raise RuntimeError("synthetic post-click automation failure")

            with patch.object(
                chatgpt_playwright,
                "_click_verified_send_button",
                side_effect=ambiguous_generic_click,
            ):
                generic_ambiguous = send_prompt(
                    "browser marker",
                    url=f"http://127.0.0.1:{retry_server.server_port}/?temporary-chat=true",
                    profile_dir=profile,
                    browser_channel="" if sys.platform == "linux" else "chrome",
                    headless=True,
                    timeout_seconds=10,
                    verification_markers=("browser marker",),
                )
            if generic_ambiguous["status"] != "sent_ambiguous" or generic_click_calls != 1:
                raise RuntimeError(
                    "non-Playwright failure after the Send boundary was retried or "
                    f"misclassified: {generic_ambiguous}, calls={generic_click_calls}"
                )
    finally:
        retry_server.shutdown()
        retry_server.server_close()
        retry_thread.join()
    print("browser pre-send retry and post-click ambiguity semantics: ok")

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

    with tempfile.TemporaryDirectory(prefix="mymcp-profile-clone-") as temp_dir:
        source = Path(temp_dir, "source")
        destination = Path(temp_dir, "destination")
        profile = source / "Default"
        profile.mkdir(parents=True)
        source.joinpath("Local State").write_text("local-state", encoding="utf-8")
        profile.joinpath("Preferences").write_text("preferences", encoding="utf-8")
        profile.joinpath("Cookies").write_text("cookies", encoding="utf-8")
        cache = profile / "Cache"
        cache.mkdir()
        cache.joinpath("large-cache").write_text("cache", encoding="utf-8")
        chatgpt_playwright._clone_browser_profile(source, destination)
        if not destination.joinpath("Default", "Cookies").is_file():
            raise RuntimeError("browser profile clone omitted authenticated profile state")
        if destination.joinpath("Default", "Cache").exists():
            raise RuntimeError("browser profile clone copied volatile cache data")
        with patch.object(
            chatgpt_playwright,
            "TASK_PROFILES_ROOT",
            Path(temp_dir, "task-profiles"),
        ):
            with chatgpt_playwright._browser_task_profile(source) as leased_profile:
                if leased_profile.parent.name != "slot_00":
                    raise RuntimeError("browser task did not use a bounded profile slot")
                if not leased_profile.joinpath("Default", "Cookies").is_file():
                    raise RuntimeError("leased browser profile omitted session state")
            if leased_profile.exists():
                raise RuntimeError("leased browser profile was not cleaned up")

            reservations: list[tuple[str, int]] = []
            for index in range(chatgpt_playwright.BROWSER_TASK_CONCURRENCY):
                task_id = f"task_{index:032x}"
                task_dir = Path(temp_dir, task_id)
                task_dir.mkdir()
                slot = chatgpt_playwright.reserve_browser_slot(task_id, task_dir)
                reservations.append((task_id, slot))
            try:
                overflow_dir = Path(temp_dir, "task_overflow")
                overflow_dir.mkdir()
                try:
                    chatgpt_playwright.reserve_browser_slot(
                        "task_ffffffffffffffffffffffffffffffff", overflow_dir
                    )
                except chatgpt_playwright.ChatGPTCapacityError:
                    pass
                else:
                    raise RuntimeError("browser capacity accepted a sixth reservation")
            finally:
                for task_id, slot in reservations:
                    chatgpt_playwright.release_browser_slot(task_id, slot)
    print("browser tasks receive isolated cache-free profile clones: ok")
    print("browser capacity rejects reservations above five: ok")

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

        synchronous_timeout = asyncio.run(server.run_workspace_code(
            "python",
            (
                "import subprocess, sys, time; "
                "child = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']); "
                "open('sync-child.pid', 'w').write(str(child.pid)); "
                "time.sleep(60)"
            ),
            timeout_seconds=1,
        ))
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

        cancellable = asyncio.run(server.run_workspace_code(
            "python",
            "import time; time.sleep(60)",
            background=True,
            timeout_seconds=30,
        ))
        cancelled = asyncio.run(server.cancel_workspace_task(
            cancellable["task_id"],
            wait_seconds=3,
        ))
        if cancelled["status"] != "cancelled":
            raise RuntimeError(f"background cancellation failed: {cancelled}")

        # Foreground capture must stay bounded even for very noisy commands.
        noisy_foreground = asyncio.run(
            server.run_workspace_code(
                "python",
                "import sys; sys.stdout.write('x' * 3000000)",
                timeout_seconds=10,
            )
        )
        if not noisy_foreground["truncated"] or len(noisy_foreground["stdout"]) > server.CONFIG.max_output_chars:
            raise RuntimeError("foreground output capture was not bounded")

        # Background logs must stay within the configured on-disk budget.
        noisy_background = asyncio.run(
            server.run_workspace_code(
                "python",
                "import sys; sys.stdout.write('y' * 5000000)",
                background=True,
                timeout_seconds=20,
            )
        )
        noisy_done = asyncio.run(
            server.get_workspace_task(noisy_background["task_id"], wait_seconds=10)
        )
        if noisy_done["status"] != "succeeded" or not noisy_done.get("stdout_truncated"):
            raise RuntimeError(f"background log truncation was not reported: {noisy_done}")
        noisy_log = Path(workspace, noisy_done["stdout_path"])
        if noisy_log.stat().st_size > server.CONFIG.max_background_log_bytes:
            raise RuntimeError("background stdout log exceeded its storage budget")

        # A dead runner must not leave its separately-sessioned workload alive.
        orphaned = asyncio.run(
            server.run_workspace_code(
                "python",
                "import time; time.sleep(60)",
                background=True,
                timeout_seconds=60,
            )
        )
        orphan_status_path = Path(workspace, orphaned["task_dir"], "status.json")
        deadline = time.monotonic() + 5
        orphan_status = {}
        while time.monotonic() < deadline:
            orphan_status = json.loads(orphan_status_path.read_text(encoding="utf-8"))
            if orphan_status.get("status") == "running" and orphan_status.get("pid"):
                break
            time.sleep(0.05)
        else:
            raise RuntimeError(f"background task never reached running: {orphan_status}")
        workload_pid = int(orphan_status["pid"])
        os.kill(int(orphan_status["worker_pid"]), 9)
        recovered = asyncio.run(server.get_workspace_task(orphaned["task_id"], wait_seconds=3))
        if recovered["status"] != "failed":
            raise RuntimeError(f"dead-runner recovery did not fail task: {recovered}")
        time.sleep(0.1)
        try:
            os.kill(workload_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise RuntimeError("dead-runner recovery left workload running")

        # Large file reads must enforce response bounds without materializing all data.
        large_file = Path(workspace, file_api_root, "large.txt")
        large_file.write_text("z" * 3000000, encoding="utf-8")
        large_read = server.read_workspace_file(f"{file_api_root}/large.txt")
        if not large_read["truncated"] or len(large_read["content"]) != server.CONFIG.max_read_chars:
            raise RuntimeError("large workspace read did not enforce its bound")

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

        async def verify_nonblocking_task_wait() -> None:
            task = await server.run_workspace_code(
                "python", "import time; time.sleep(0.35)", background=True, timeout_seconds=5
            )
            poll = asyncio.create_task(
                server.get_workspace_task(task["task_id"], wait_seconds=2)
            )
            await asyncio.sleep(0.05)
            started_read = time.monotonic()
            server.read_workspace_file(f"{file_api_root}/probe.txt")
            if time.monotonic() - started_read > 0.2:
                raise RuntimeError("long poll blocked an unrelated file read")
            result = await poll
            if result["status"] != "succeeded":
                raise RuntimeError(f"long poll did not converge to success: {result}")

            started = time.monotonic()
            foreground = asyncio.create_task(
                server.run_workspace_code(
                    "python", "import time; time.sleep(0.35)", timeout_seconds=2
                )
            )
            await asyncio.sleep(0.05)
            if time.monotonic() - started > 0.2:
                raise RuntimeError("foreground command blocked the event loop")
            server.read_workspace_file(f"{file_api_root}/probe.txt")
            foreground_result = await foreground
            if foreground_result["exit_code"] != 0:
                raise RuntimeError(f"foreground command failed: {foreground_result}")

        server.write_workspace_file(f"{file_api_root}/probe.txt", "probe\n")
        asyncio.run(verify_nonblocking_task_wait())

        try:
            asyncio.run(
                server.get_workspace_task(
                    "task_00000000000000000000000000000001",
                    wait_seconds=server.CONFIG.task_max_wait_seconds + 1,
                )
            )
        except ValueError:
            pass
        else:
            raise RuntimeError("task wait above the configured maximum was accepted")

        draft_root = Path(workspace, "draft")
        draft_root.mkdir()
        import subprocess
        subprocess.run(["git", "init"], cwd=draft_root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=draft_root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=draft_root, check=True)
        draft_root.joinpath("a.md").write_text("a\n", encoding="utf-8")
        draft_root.joinpath("b.md").write_text("b\n", encoding="utf-8")
        draft_root.joinpath("[x].md").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "--", "a.md", "b.md", "[x].md"], cwd=draft_root, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=draft_root, check=True, capture_output=True)
        draft_root.joinpath("a.md").write_text("a2\n", encoding="utf-8")
        draft_root.joinpath("b.md").write_text("b2\n", encoding="utf-8")
        draft_root.joinpath("[x].md").write_text("x2\n", encoding="utf-8")
        try:
            server._git_diff_for_file("*")
        except ValueError:
            pass
        else:
            raise RuntimeError("literal single-file Git API expanded '*' as a pathspec")
        nested_dir = draft_root / "nested"
        nested_dir.mkdir()
        nested_dir.joinpath("c.md").write_text("c\n", encoding="utf-8")
        subprocess.run(["git", "add", "--", "nested/c.md"], cwd=draft_root, check=True)
        subprocess.run(["git", "commit", "-m", "add nested"], cwd=draft_root, check=True, capture_output=True)
        nested_dir.joinpath("c.md").write_text("c2\n", encoding="utf-8")
        try:
            server._git_diff_for_file("nested")
        except ValueError:
            pass
        else:
            raise RuntimeError("single-file Git API accepted a directory path")
        bracket_diff = server._git_diff_for_file("[x].md")
        if "[x].md" not in bracket_diff or "a.md" in bracket_diff or "b.md" in bracket_diff:
            raise RuntimeError(f"literal metacharacter filename diff leaked siblings: {bracket_diff}")

        if hasattr(server, "spawn_chatgpt_subagent"):
            raise RuntimeError("legacy single sub-agent API must not be exposed")
        registered_tool_names = {
            tool.name for tool in asyncio.run(server.mcp.list_tools())
        }
        if "spawn_chatgpt_subagent" in registered_tool_names:
            raise RuntimeError("legacy single sub-agent MCP tool must not be registered")
        if "spawn_chatgpt_subagents" not in registered_tool_names:
            raise RuntimeError("batch sub-agent MCP tool is not registered")

        delegated_task = "THIS_CONTENT_MUST_NOT_BE_IN_THE_BROWSER_PROMPT"
        demo_task_id = "task_" + "d" * 32
        demo_task_dir = Path(workspace, ".mcp-tasks", demo_task_id).resolve()
        demo_task_dir.mkdir()
        with (
            patch.object(
                server, "_reserve_workspace_task_dir", return_value=(demo_task_id, demo_task_dir)
            ),
            patch.object(
                server,
                "_start_workspace_task",
                return_value={
                    "task_id": demo_task_id,
                    "status": "queued",
                    "task_dir": f".mcp-tasks/{demo_task_id}",
                    "stdout_path": f".mcp-tasks/{demo_task_id}/stdout.log",
                    "stderr_path": f".mcp-tasks/{demo_task_id}/stderr.log",
                },
            ) as mocked_start,
            patch.object(
                chatgpt_playwright,
                "reserve_browser_slot",
                return_value=2,
            ),
        ):
            single_batch = server.spawn_chatgpt_subagents([delegated_task])
            delegated = single_batch["items"][0]
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
            or "reservation_slot=2" not in task_code
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
        if Path(workspace, delegated["output_path"]).exists():
            raise RuntimeError("sub-agent output was pre-created instead of child-created")
        if not delegated["input_path"].startswith(f".mcp-tasks/{demo_task_id}/"):
            raise RuntimeError(f"sub-agent artifacts are not task-owned: {delegated}")
        if (
            delegated["status"] != "started"
            or delegated["task_status"] != "queued"
            or delegated["task_id"] != demo_task_id
        ):
            raise RuntimeError(f"sub-agent was not queued asynchronously: {delegated}")
        if delegated["browser_slot"] != 2:
            raise RuntimeError(f"sub-agent did not return its reserved slot: {delegated}")
        try:
            server.spawn_chatgpt_subagents(["   "])
        except ValueError:
            pass
        else:
            raise RuntimeError("empty delegated task was accepted")

        full_task_id = "task_" + "e" * 32
        full_task_dir = Path(workspace, ".mcp-tasks", full_task_id).resolve()
        full_task_dir.mkdir()
        with (
            patch.object(
                server,
                "_reserve_workspace_task_dir",
                return_value=(full_task_id, full_task_dir),
            ),
            patch.object(
                chatgpt_playwright,
                "reserve_browser_slot",
                side_effect=chatgpt_playwright.ChatGPTCapacityError(
                    "ChatGPT browser capacity is full (5 concurrent tasks)"
                ),
            ),
        ):
            full_batch = server.spawn_chatgpt_subagents(["capacity probe"])
        if full_batch["status"] != "none_started":
            raise RuntimeError(f"full capacity batch should start nothing: {full_batch}")
        if full_batch["items"][0]["status"] != "rejected_capacity":
            raise RuntimeError(f"full capacity batch did not report rejection: {full_batch}")
        if full_task_dir.exists():
            raise RuntimeError("rejected sub-agent left an unstarted task directory")

        failed_start_task_id = "task_" + "f" * 32
        failed_start_task_dir = Path(
            workspace, ".mcp-tasks", failed_start_task_id
        ).resolve()
        failed_start_task_dir.mkdir()
        with (
            patch.object(
                server,
                "_reserve_workspace_task_dir",
                return_value=(failed_start_task_id, failed_start_task_dir),
            ),
            patch.object(
                chatgpt_playwright,
                "reserve_browser_slot",
                return_value=4,
            ),
            patch.object(
                chatgpt_playwright,
                "release_browser_slot",
            ) as mocked_release,
            patch.object(
                server,
                "_start_workspace_task",
                side_effect=RuntimeError("simulated background start failure"),
            ),
        ):
            try:
                server.spawn_chatgpt_subagents(["background start failure probe"])
            except RuntimeError:
                pass
            else:
                raise RuntimeError("background start failure was silently accepted")
        mocked_release.assert_called_once_with(failed_start_task_id, 4)
        if failed_start_task_dir.exists():
            raise RuntimeError("failed background start left an unowned task directory")

        started_a = {
            "task_id": "task_" + "a" * 32,
            "status": "queued",
            "output_path": ".mcp-tasks/task_a/output.md",
        }
        started_b = {
            "task_id": "task_" + "b" * 32,
            "status": "queued",
            "output_path": ".mcp-tasks/task_b/output.md",
        }
        with patch.object(
            server,
            "_spawn_one_chatgpt_subagent",
            side_effect=[
                started_a,
                started_b,
                chatgpt_playwright.ChatGPTCapacityError(
                    "ChatGPT browser capacity is full (5 concurrent tasks)"
                ),
            ],
        ) as mocked_batch_spawn:
            batch = server.spawn_chatgpt_subagents(
                ["task a", "task b", "task c", "task d"]
            )
        if batch["status"] != "partial" or batch["started"] != 2:
            raise RuntimeError(f"partial batch launch lost success state: {batch}")
        statuses = [item["status"] for item in batch["items"]]
        if statuses != [
            "started",
            "started",
            "rejected_capacity",
            "not_started_capacity",
        ]:
            raise RuntimeError(f"unexpected partial batch statuses: {batch}")
        if [
            batch["items"][0]["task_id"],
            batch["items"][1]["task_id"],
        ] != [started_a["task_id"], started_b["task_id"]]:
            raise RuntimeError(f"partial batch lost started task IDs: {batch}")
        if mocked_batch_spawn.call_count != 3:
            raise RuntimeError(
                "batch launch continued spawning after deterministic capacity rejection"
            )

        with patch.object(server, "_spawn_one_chatgpt_subagent") as mocked_invalid_batch:
            try:
                server.spawn_chatgpt_subagents(["valid first task", "   "])
            except ValueError:
                pass
            else:
                raise RuntimeError("batch accepted an invalid later task")
        if mocked_invalid_batch.called:
            raise RuntimeError("batch validation performed side effects before all inputs passed")

        with patch.object(
            server,
            "_spawn_one_chatgpt_subagent",
            side_effect=[started_a, RuntimeError("simulated launch failure")],
        ):
            failed_batch = server.spawn_chatgpt_subagents(
                ["task a", "task b", "task c"]
            )
        if [item["status"] for item in failed_batch["items"]] != [
            "started",
            "failed_to_start",
            "not_started_after_error",
        ]:
            raise RuntimeError(
                f"partial runtime failure hid started task state: {failed_batch}"
            )

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
