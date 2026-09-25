"""Long-session timer and same-conversation wakeup regression tests."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chatgpt_playwright
import server


class FakeTimer:
    created: list["FakeTimer"] = []

    def __init__(self, interval: float, function, args=(), kwargs=None) -> None:
        self.interval = interval
        self.function = function
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = False
        self.started = False
        self.cancelled = False
        type(self).created.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


class LongSessionTests(unittest.TestCase):
    def _reset_long_session_state(self) -> None:
        FakeTimer.created.clear()
        with server._long_session_lock:
            for wakeup in server._long_session_wakeups.values():
                wakeup.cancel()
            server._long_session_wakeups.clear()
            server._long_session_started.clear()
            server._long_session_wakeup_results.clear()

    def setUp(self) -> None:
        self._log_temp = tempfile.TemporaryDirectory()
        self._config_patch = patch.object(
            server,
            "CONFIG",
            replace(server.CONFIG, project_root=Path(self._log_temp.name)),
        )
        self._config_patch.start()
        self._reset_long_session_state()

    def tearDown(self) -> None:
        self._reset_long_session_state()
        self._config_patch.stop()
        self._log_temp.cleanup()

    def test_all_ordinary_tools_accept_optional_conversation_url(self) -> None:
        tools = asyncio.run(server.mcp.list_tools())
        controls = {"start_timer", "end_timer", "register_wakeup", "list_long_sessions"}
        self.assertTrue(controls.issubset({tool.name for tool in tools}))
        for tool in tools:
            if tool.name in controls:
                continue
            properties = tool.inputSchema.get("properties", {})
            self.assertIn(
                "conversation_url",
                properties,
                f"{tool.name} is missing long-session conversation context",
            )
            self.assertNotIn(
                "conversation_url",
                tool.inputSchema.get("required", []),
                f"{tool.name} made conversation_url mandatory",
            )

    def test_project_conversation_url_is_accepted(self) -> None:
        url = "https://chatgpt.com/g/g-p-69ed598d4b80819193cbe418446ffaf4/c/6ab4c8ab-3dbc-83ea-9a4b-c5ae2fa35fbf"
        self.assertEqual(server._validate_conversation_url(url), url)

    def test_server_authoritatively_computes_twenty_minute_timeout(self) -> None:
        url = "https://chatgpt.com/c/lease-test"
        started = server.start_timer(url)
        self.assertTrue(started["tracked"])
        self.assertEqual(
            started["yield_after_seconds"],
            server.CONFIG.long_session_yield_after_seconds,
        )
        self.assertFalse(started["timed_out"])

        with server._long_session_lock:
            server._long_session_started[url] = (
                time.monotonic() - server.CONFIG.long_session_yield_after_seconds - 1
            )
        expired = server._long_session_status(url)
        self.assertTrue(expired["timed_out"])
        self.assertEqual(expired["remaining_seconds"], 0.0)

    def test_list_long_sessions_reports_active_timer_and_pending_wakeup(self) -> None:
        timer_url = "https://chatgpt.com/c/debug-active"
        wakeup_url = "https://chatgpt.com/c/debug-wakeup"

        server.start_timer(timer_url)
        with server._long_session_lock:
            server._long_session_started[timer_url] = (
                time.monotonic() - server.CONFIG.long_session_yield_after_seconds - 1
            )

        with patch.object(server.threading, "Timer", FakeTimer):
            server.register_wakeup(wakeup_url, prompt="continue")

        snapshot = server.list_long_sessions()
        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["count"], 2)
        self.assertEqual(snapshot["active_timer_count"], 1)
        self.assertEqual(snapshot["pending_wakeup_count"], 1)

        by_url = {
            session["conversation_url"]: session for session in snapshot["sessions"]
        }
        active = by_url[timer_url]
        self.assertEqual(active["phase"], "timer_active")
        self.assertTrue(active["timer"]["active"])
        self.assertTrue(active["timer"]["timed_out"])
        self.assertEqual(active["timer"]["remaining_seconds"], 0.0)
        self.assertFalse(active["wakeup"]["pending"])

        pending = by_url[wakeup_url]
        self.assertEqual(pending["phase"], "wakeup_scheduled")
        self.assertFalse(pending["timer"]["active"])
        self.assertTrue(pending["wakeup"]["pending"])
        self.assertIn("due_at_epoch_seconds", pending["wakeup"])
        self.assertGreaterEqual(pending["wakeup"]["remaining_seconds"], 0.0)

    def test_list_long_sessions_excludes_historical_wakeup_only_state(self) -> None:
        url = "https://chatgpt.com/c/debug-history"
        with server._long_session_lock:
            server._long_session_wakeup_results[url] = {
                "status": "sent",
                "finished_at_epoch_seconds": time.time(),
            }
        snapshot = server.list_long_sessions()
        self.assertEqual(snapshot["count"], 0)
        self.assertEqual(snapshot["sessions"], [])

    def test_ordinary_tool_returns_lease_in_text_and_structured_output(self) -> None:
        url = "https://chatgpt.com/c/result-test"
        server.start_timer(url)
        result = asyncio.run(
            server.mcp.call_tool(
                "read_workspace_file",
                {"path": "mymcp/README.md", "conversation_url": url},
            )
        )
        self.assertIsInstance(result, tuple)
        content, structured = result
        self.assertTrue(content[-1].text.startswith("LONG_SESSION_STATUS "))
        self.assertEqual(structured["long_session"]["conversation_url"], url)
        self.assertTrue(structured["long_session"]["tracked"])

    def test_timed_out_session_blocks_ordinary_tool_before_execution(self) -> None:
        url = "https://chatgpt.com/c/blocked-after-timeout"
        server.start_timer(url)
        with server._long_session_lock:
            server._long_session_started[url] = (
                time.monotonic() - server.CONFIG.long_session_yield_after_seconds - 1
            )

        with (
            patch.object(server, "_read_workspace_prefix") as read,
            patch.object(server, "_append_long_session_event") as log_event,
            patch.object(server.threading, "Timer", FakeTimer),
        ):
            result = asyncio.run(
                server.mcp.call_tool(
                    "read_workspace_file",
                    {"path": "mymcp/README.md", "conversation_url": url},
                )
            )

        read.assert_not_called()
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        text = result[0].text
        self.assertIn("LONG_SESSION_TIME_LIMIT_REACHED", text)
        self.assertIn("was NOT executed", text)
        self.assertIn("register_wakeup", text)
        self.assertIn("fallback wake-up has been scheduled", text)
        self.assertEqual(len(FakeTimer.created), 1)
        fallback = FakeTimer.created[0]
        self.assertEqual(
            fallback.interval,
            server.CONFIG.long_session_timeout_fallback_delay_seconds,
        )
        self.assertTrue(fallback.started)
        self.assertIn(url, server._long_session_started)
        self.assertEqual(
            server._long_session_wakeup_results[url]["kind"],
            "timeout_fallback",
        )
        self.assertTrue(
            any(
                call.args[0] == "tool_call_blocked_timeout"
                for call in log_event.call_args_list
            )
        )

    def test_timed_out_session_cannot_reset_itself_with_start_timer(self) -> None:
        url = "https://chatgpt.com/c/no-self-reset"
        server.start_timer(url)
        with server._long_session_lock:
            expired_started = (
                time.monotonic() - server.CONFIG.long_session_yield_after_seconds - 1
            )
            server._long_session_started[url] = expired_started

        with patch.object(server.threading, "Timer", FakeTimer):
            result = asyncio.run(
                server.mcp.call_tool(
                    "start_timer",
                    {"conversation_url": url},
                )
            )

        self.assertIsInstance(result, list)
        self.assertIn("LONG_SESSION_TIME_LIMIT_REACHED", result[0].text)
        self.assertIn("was NOT executed", result[0].text)
        self.assertEqual(server._long_session_started[url], expired_started)
        self.assertEqual(len(FakeTimer.created), 1)
        self.assertEqual(
            server._long_session_wakeup_results[url]["kind"],
            "timeout_fallback",
        )

    def test_start_timer_is_allowed_after_timeout_fallback_wakeup_was_sent(self) -> None:
        url = "https://chatgpt.com/c/restart-after-fallback"
        with server._long_session_lock:
            server._long_session_started[url] = (
                time.monotonic() - server.CONFIG.long_session_yield_after_seconds - 1
            )
            server._long_session_wakeup_results[url] = {
                "status": "sent",
                "kind": "timeout_fallback",
                "finished_at_epoch_seconds": time.time(),
            }

        result = asyncio.run(
            server.mcp.call_tool(
                "start_timer",
                {"conversation_url": url},
            )
        )

        content, structured = result
        self.assertEqual(structured["status"], "started")
        self.assertTrue(structured["tracked"])
        self.assertFalse(structured["timed_out"])

    def test_timeout_gate_does_not_schedule_duplicate_fallbacks(self) -> None:
        url = "https://chatgpt.com/c/single-fallback"
        server.start_timer(url)
        with server._long_session_lock:
            server._long_session_started[url] = (
                time.monotonic() - server.CONFIG.long_session_yield_after_seconds - 1
            )

        with patch.object(server.threading, "Timer", FakeTimer):
            for _ in range(2):
                asyncio.run(
                    server.mcp.call_tool(
                        "read_workspace_file",
                        {"path": "mymcp/README.md", "conversation_url": url},
                    )
                )

        self.assertEqual(len(FakeTimer.created), 1)
        self.assertTrue(FakeTimer.created[0].started)
        snapshot = server.list_long_sessions()
        session = next(
            item for item in snapshot["sessions"]
            if item["conversation_url"] == url
        )
        self.assertEqual(session["phase"], "timed_out_fallback_scheduled")
        self.assertTrue(session["timer"]["timed_out"])
        self.assertTrue(session["wakeup"]["pending"])
        self.assertEqual(session["wakeup"]["kind"], "timeout_fallback")

    def test_agent_registered_wakeup_replaces_timeout_fallback(self) -> None:
        url = "https://chatgpt.com/c/control-after-timeout"
        server.start_timer(url)
        with server._long_session_lock:
            server._long_session_started[url] = (
                time.monotonic() - server.CONFIG.long_session_yield_after_seconds - 1
            )

        with patch.object(server.threading, "Timer", FakeTimer):
            asyncio.run(
                server.mcp.call_tool(
                    "read_workspace_file",
                    {"path": "mymcp/README.md", "conversation_url": url},
                )
            )
            self.assertEqual(len(FakeTimer.created), 1)
            fallback = FakeTimer.created[0]
            self.assertEqual(
                fallback.interval,
                server.CONFIG.long_session_timeout_fallback_delay_seconds,
            )
            self.assertFalse(fallback.cancelled)

            result = asyncio.run(
                server.mcp.call_tool(
                    "register_wakeup",
                    {"conversation_url": url, "prompt": "continue later"},
                )
            )

        content, structured = result
        self.assertEqual(structured["status"], "scheduled")
        self.assertTrue(structured["previous_wakeup_replaced"])
        self.assertEqual(structured["previous_wakeup_kind"], "timeout_fallback")
        self.assertNotIn(url, server._long_session_started)
        self.assertTrue(fallback.cancelled)
        self.assertEqual(len(FakeTimer.created), 2)
        normal = FakeTimer.created[1]
        self.assertEqual(normal.interval, server.CONFIG.long_session_wakeup_delay_seconds)
        self.assertTrue(normal.started)
        self.assertEqual(
            server._long_session_wakeup_results[url]["kind"],
            "agent_registered",
        )

    def test_tool_crossing_time_limit_returns_result_then_stop_directive(self) -> None:
        url = "https://chatgpt.com/c/cross-timeout"
        before = {
            "tracked": True,
            "conversation_url": url,
            "elapsed_seconds": 1199.0,
            "remaining_seconds": 1.0,
            "yield_after_seconds": 1200,
            "timed_out": False,
        }
        after = {
            "tracked": True,
            "conversation_url": url,
            "elapsed_seconds": 1201.0,
            "remaining_seconds": 0.0,
            "yield_after_seconds": 1200,
            "timed_out": True,
        }
        with (
            patch.object(server, "_long_session_status", side_effect=[before, after]),
            patch.object(server, "_append_long_session_event"),
            patch.object(server.threading, "Timer", FakeTimer),
        ):
            result = asyncio.run(
                server.mcp.call_tool(
                    "read_workspace_file",
                    {"path": "mymcp/README.md", "conversation_url": url},
                )
            )

        self.assertIsInstance(result, tuple)
        content, structured = result
        self.assertTrue(any("OverGPT" in item.text for item in content[:-1]))
        self.assertIn("LONG_SESSION_TIME_LIMIT_REACHED", content[-1].text)
        self.assertIn("was allowed to finish", content[-1].text)
        self.assertNotIn("long_session", structured)

    def test_long_session_event_log_persists_lifecycle_trace(self) -> None:
        url = "https://chatgpt.com/c/persistent-trace"
        with tempfile.TemporaryDirectory() as temp_dir:
            test_config = replace(server.CONFIG, project_root=Path(temp_dir))
            with (
                patch.object(server, "CONFIG", test_config),
                patch.object(server.threading, "Timer", FakeTimer),
            ):
                server.start_timer(url)
                asyncio.run(
                    server.mcp.call_tool(
                        "read_workspace_file",
                        {"path": "mymcp/README.md", "conversation_url": url},
                    )
                )
                server.register_wakeup(url, prompt="continue")

                log_path = test_config.long_session_event_log
                self.assertTrue(log_path.is_file())
                events = [
                    json.loads(line)
                    for line in log_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]

        names = [event["event"] for event in events]
        self.assertEqual(
            names,
            [
                "timer_started",
                "tool_call_requested",
                "tool_call_started",
                "tool_call_completed",
                "wakeup_registered",
            ],
        )
        self.assertTrue(all(event["conversation_url"] == url for event in events))

    def test_ordinary_tool_without_url_is_unmodified_for_clean_subagents(self) -> None:
        result = asyncio.run(
            server.mcp.call_tool(
                "read_workspace_file",
                {"path": "mymcp/README.md"},
            )
        )
        self.assertIsInstance(result, tuple)
        content, structured = result
        self.assertNotIn("long_session", structured)
        self.assertFalse(
            any(
                getattr(item, "text", "").startswith("LONG_SESSION_STATUS ")
                for item in content
            )
        )

    def test_invalid_url_fails_before_underlying_tool_runs(self) -> None:
        with patch.object(server, "_read_workspace_prefix") as read:
            with self.assertRaises(ValueError):
                asyncio.run(
                    server.mcp.call_tool(
                        "read_workspace_file",
                        {
                            "path": "mymcp/README.md",
                            "conversation_url": "https://example.com/not-chatgpt",
                        },
                    )
                )
        read.assert_not_called()

    def test_register_wakeup_clears_timer_and_schedules_configured_callback(self) -> None:
        url = "https://chatgpt.com/c/wakeup-test"
        server.start_timer(url)
        with patch.object(server.threading, "Timer", FakeTimer):
            result = server.register_wakeup(url, prompt="continue exactly")
        self.assertEqual(result["status"], "scheduled")
        self.assertTrue(result["active_timer_cleared"])
        self.assertEqual(
            result["delay_seconds"],
            server.CONFIG.long_session_wakeup_delay_seconds,
        )
        self.assertNotIn(url, server._long_session_started)
        self.assertEqual(len(FakeTimer.created), 1)
        wakeup = FakeTimer.created[0]
        self.assertEqual(
            wakeup.interval,
            server.CONFIG.long_session_wakeup_delay_seconds,
        )
        self.assertTrue(wakeup.started)
        self.assertEqual(wakeup.args, (url, "continue exactly"))

    def test_failed_wakeup_is_retained_for_diagnostics(self) -> None:
        url = "https://chatgpt.com/c/failure-test"
        with patch.object(
            chatgpt_playwright,
            "send_prompt",
            side_effect=RuntimeError("synthetic browser failure"),
        ):
            server._send_registered_wakeup(url, "continue work")
        status = server._long_session_status(url)
        self.assertEqual(status["last_wakeup"]["status"], "failed")
        self.assertEqual(status["last_wakeup"]["error_type"], "RuntimeError")
        self.assertIn("synthetic browser failure", status["last_wakeup"]["error"])

    def test_wakeup_uses_existing_browser_automation_for_history_url(self) -> None:
        url = "https://chatgpt.com/c/history-test"
        with patch.object(chatgpt_playwright, "send_prompt", return_value={"status": "sent"}) as send:
            server._send_registered_wakeup(url, "continue work")
        kwargs = send.call_args.kwargs
        self.assertEqual(server._long_session_wakeup_results[url]["status"], "sent")
        self.assertEqual(kwargs["url"], url)
        self.assertFalse(kwargs["require_temporary_chat"])
        self.assertEqual(kwargs["verification_markers"], (url,))
        self.assertEqual(kwargs["mcp_app_name"], server.CONFIG.chatgpt_mcp_app_name)
        rendered = send.call_args.args[0]
        self.assertIn("start_timer", rendered)
        self.assertIn(server.CONFIG.chatgpt_mcp_app_name, rendered)
        self.assertIn(url, rendered)
        self.assertIn("continue work", rendered)
        self.assertNotIn("marker", server._long_session_wakeup_results[url])


if __name__ == "__main__":
    unittest.main()
