"""Long-session timer and same-conversation wakeup regression tests."""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
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
    def setUp(self) -> None:
        FakeTimer.created.clear()
        with server._long_session_lock:
            for wakeup in server._long_session_wakeups.values():
                wakeup.cancel()
            server._long_session_wakeups.clear()
            server._long_session_started.clear()
            server._long_session_wakeup_results.clear()

    def tearDown(self) -> None:
        self.setUp()

    def test_all_ordinary_tools_accept_optional_conversation_url(self) -> None:
        tools = asyncio.run(server.mcp.list_tools())
        controls = {"start_timer", "end_timer", "register_wakeup"}
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

    def test_server_authoritatively_computes_twenty_minute_yield(self) -> None:
        url = "https://chatgpt.com/c/lease-test"
        started = server.start_timer(url)
        self.assertTrue(started["tracked"])
        self.assertEqual(
            started["yield_after_seconds"],
            server.CONFIG.long_session_yield_after_seconds,
        )
        self.assertFalse(started["should_yield"])

        with server._long_session_lock:
            server._long_session_started[url] = (
                time.monotonic() - server.CONFIG.long_session_yield_after_seconds - 1
            )
        expired = server._long_session_status(url)
        self.assertTrue(expired["should_yield"])
        self.assertEqual(expired["remaining_seconds"], 0.0)

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

    def test_register_wakeup_clears_timer_and_schedules_two_minute_callback(self) -> None:
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
        rendered = send.call_args.args[0]
        self.assertIn("LONG_SESSION_WAKEUP_", rendered)
        self.assertIn("start_timer", rendered)
        self.assertIn(url, rendered)
        self.assertIn("continue work", rendered)


if __name__ == "__main__":
    unittest.main()
