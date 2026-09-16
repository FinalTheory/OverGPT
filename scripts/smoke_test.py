"""Small protocol-level smoke test for the deployed Streamable HTTP server."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from config import CONFIG


PROJECT_RELATIVE_PATH = os.getenv("MCP_PROJECT_RELATIVE_PATH", "mymcp").strip().strip("/") or "."


def project_path(name: str) -> str:
    return name if PROJECT_RELATIVE_PATH == "." else f"{PROJECT_RELATIVE_PATH}/{name}"


SMOKE_FILE = project_path(".mcp-smoke-test.md")
MOVE_FILE = project_path(".mcp-move-smoke-test.md")
MOVED_FILE = project_path(".mcp-moved-smoke-test.md")
GUARD_FILE = project_path(".mcp-guard-smoke-test.md")


def post_json(url: str, payload: dict[str, str]) -> dict[str, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def structured_result(result) -> dict[str, object]:
    payload = result.structuredContent or {}
    nested = payload.get("result")
    return nested if isinstance(nested, dict) else payload


async def call_tool_once(url: str, name: str, arguments: dict[str, object]):
    async with streamablehttp_client(url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return await session.call_tool(name, arguments)


async def wait_for_task(session: ClientSession, task_id: str) -> dict[str, object]:
    for _ in range(100):
        result = await session.call_tool(
            "get_workspace_task",
            {"task_id": task_id, "wait_seconds": 1},
        )
        if result.isError:
            raise RuntimeError(f"get_workspace_task failed: {result}")
        payload = structured_result(result)
        if payload["status"] in {"succeeded", "failed", "timed_out", "cancelled"}:
            return payload
        await asyncio.sleep(0.1)
    raise RuntimeError(f"background task did not finish: {task_id}")


async def main(url: str) -> None:
    async with streamablehttp_client(url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = {tool.name for tool in tools.tools}
            expected_tools = {
                "list_skills",
                "list_draft_articles",
                "load_skill",
                "read_workspace_file",
                "read_workspace_range",
                "list_workspace",
                "search_workspace_text",
                "write_workspace_file",
                "delete_workspace_file",
                "move_workspace_file",
                "replace_workspace_text",
                "insert_workspace_text",
                "apply_workspace_patch",
                "get_workspace_task",
                "cancel_workspace_task",
                "restart_mcp_server",
                "run_workspace_code",
                "spawn_chatgpt_subagent",
            }
            if tool_names != expected_tools:
                raise RuntimeError(
                    f"unexpected tools: expected {sorted(expected_tools)}, got {sorted(tool_names)}"
                )
            skills = await session.call_tool("list_skills", {})
            articles = await session.call_tool("list_draft_articles", {})
            loaded = await session.call_tool("load_skill", {"name": "red"})
            for fixture_path in (
                SMOKE_FILE,
                "draft/.mcp-revert-smoke-test.md",
                MOVE_FILE,
                MOVED_FILE,
                GUARD_FILE,
            ):
                existing = await session.call_tool(
                    "read_workspace_file", {"path": fixture_path}
                )
                if not existing.isError:
                    existing_payload = structured_result(existing)
                    removed = await session.call_tool(
                        "delete_workspace_file",
                        {
                            "path": fixture_path,
                            "expected_sha256": existing_payload["sha256"],
                        },
                    )
                    if removed.isError:
                        raise RuntimeError(
                            f"could not clean stale smoke fixture {fixture_path}: {removed}"
                        )

            written = await session.call_tool(
                "write_workspace_file",
                {
                    "path": SMOKE_FILE,
                    "content": "# MCP smoke test\n\n写入成功。\n",
                },
            )
            revert_fixture = await session.call_tool(
                "write_workspace_file",
                {
                    "path": "draft/.mcp-revert-smoke-test.md",
                    "content": "# Revert smoke test\n",
                },
            )
            read_back = await session.call_tool(
                "read_workspace_file", {"path": SMOKE_FILE}
            )
            # Protocol-level responsiveness: one blocking-style operation on one
            # connection must not head-of-line-block an unrelated connection.
            responsiveness_task = await session.call_tool(
                "run_workspace_code",
                {
                    "language": "python",
                    "code": "import time; time.sleep(1.2)",
                    "background": True,
                    "timeout_seconds": 5,
                },
            )
            if responsiveness_task.isError:
                raise RuntimeError(f"responsiveness task failed to start: {responsiveness_task}")
            responsiveness_id = str(structured_result(responsiveness_task)["task_id"])
            poll_call = asyncio.create_task(
                call_tool_once(
                    url,
                    "get_workspace_task",
                    {"task_id": responsiveness_id, "wait_seconds": 2},
                )
            )
            await asyncio.sleep(0.1)
            loop = asyncio.get_running_loop()
            started = loop.time()
            concurrent_read = await call_tool_once(
                url, "read_workspace_file", {"path": SMOKE_FILE}
            )
            read_elapsed = loop.time() - started
            if concurrent_read.isError or read_elapsed > 0.75:
                raise RuntimeError(
                    f"long poll blocked unrelated MCP traffic for {read_elapsed:.3f}s"
                )
            await poll_call

            foreground_call = asyncio.create_task(
                call_tool_once(
                    url,
                    "run_workspace_code",
                    {
                        "language": "python",
                        "code": "import time; time.sleep(1.2)",
                        "timeout_seconds": 5,
                    },
                )
            )
            await asyncio.sleep(0.1)
            started = loop.time()
            concurrent_read = await call_tool_once(
                url, "read_workspace_file", {"path": SMOKE_FILE}
            )
            foreground_read_elapsed = loop.time() - started
            foreground_result = await foreground_call
            if (
                concurrent_read.isError
                or foreground_result.isError
                or foreground_read_elapsed > 0.75
            ):
                raise RuntimeError(
                    "foreground execution blocked unrelated MCP traffic: "
                    f"{foreground_read_elapsed:.3f}s"
                )
            guard_created = await session.call_tool(
                "write_workspace_file",
                {
                    "path": GUARD_FILE,
                    "content": "guard v1\n",
                },
            )
            guard_read = await session.call_tool(
                "read_workspace_file", {"path": GUARD_FILE}
            )
            guard_sha = structured_result(guard_read)["sha256"]
            guard_overwrite = await session.call_tool(
                "write_workspace_file",
                {
                    "path": GUARD_FILE,
                    "content": "guard v2\n",
                    "overwrite": True,
                    "expected_sha256": guard_sha,
                },
            )
            stale_overwrite = await session.call_tool(
                "write_workspace_file",
                {
                    "path": GUARD_FILE,
                    "content": "stale\n",
                    "overwrite": True,
                    "expected_sha256": guard_sha,
                },
            )
            listed_workspace = await session.call_tool(
                "list_workspace",
                {"path": PROJECT_RELATIVE_PATH, "depth": 1, "include_hidden": True},
            )
            searched_workspace = await session.call_tool(
                "search_workspace_text",
                {
                    "path": PROJECT_RELATIVE_PATH,
                    "query": "MCP smoke test",
                    "file_glob": ".mcp-smoke-test.md",
                },
            )
            move_fixture = await session.call_tool(
                "write_workspace_file",
                {
                    "path": MOVE_FILE,
                    "content": "move me\n",
                },
            )
            move_fixture_payload = structured_result(move_fixture)
            moved_fixture = await session.call_tool(
                "move_workspace_file",
                {
                    "source": MOVE_FILE,
                    "destination": MOVED_FILE,
                    "expected_sha256": move_fixture_payload["sha256"],
                },
            )
            moved_payload = structured_result(moved_fixture)
            deleted_fixture = await session.call_tool(
                "delete_workspace_file",
                {
                    "path": MOVED_FILE,
                    "expected_sha256": moved_payload["sha256"],
                },
            )

            ranged = await session.call_tool(
                "read_workspace_range",
                {"path": SMOKE_FILE, "start_line": 1, "end_line": 2},
            )
            anchored = await session.call_tool(
                "read_workspace_range",
                {"path": SMOKE_FILE, "anchor": "写入成功", "context_lines": 1},
            )
            replaced = await session.call_tool(
                "replace_workspace_text",
                {
                    "path": SMOKE_FILE,
                    "old_text": "写入成功。",
                    "new_text": "精确替换成功。",
                    "expected_count": 1,
                },
            )
            rejected = await session.call_tool(
                "replace_workspace_text",
                {
                    "path": SMOKE_FILE,
                    "old_text": "不存在",
                    "new_text": "不应写入",
                    "expected_count": 1,
                },
            )
            inserted = await session.call_tool(
                "insert_workspace_text",
                {
                    "path": SMOKE_FILE,
                    "anchor": "精确替换成功。",
                    "text": "原子",
                    "position": "before",
                    "expected_count": 1,
                },
            )
            edited = await session.call_tool(
                "read_workspace_file", {"path": SMOKE_FILE}
            )
            patch = (
                f"--- a/{SMOKE_FILE}\n"
                f"+++ b/{SMOKE_FILE}\n"
                "@@ -1,3 +1,3 @@\n"
                " # MCP smoke test\n"
                " \n"
                "-原子精确替换成功。\n"
                "+Patch 应用成功。\n"
            )
            patched = await session.call_tool("apply_workspace_patch", {"patch": patch})
            bad_patch = patch.replace("原子精确替换成功。", "不存在的上下文。")
            patch_rejected = await session.call_tool(
                "apply_workspace_patch", {"patch": bad_patch}
            )
            patched_file = await session.call_tool(
                "read_workspace_file", {"path": SMOKE_FILE}
            )
            executed = await session.call_tool(
                "run_workspace_code",
                {"language": "python", "code": "print('python execution ok')"},
            )
            background = await session.call_tool(
                "run_workspace_code",
                {
                    "language": "python",
                    "code": (
                        "import sys, time; "
                        "print('background stdout'); "
                        "print('background stderr', file=sys.stderr); "
                        "time.sleep(0.2)"
                    ),
                    "background": True,
                    "timeout_seconds": 10,
                },
            )
            if background.isError:
                raise RuntimeError(f"background run_workspace_code failed: {background}")
            background_task_id = str(structured_result(background)["task_id"])
            background_result = await wait_for_task(session, background_task_id)
            background_stdout = await session.call_tool(
                "read_workspace_file", {"path": str(background_result["stdout_path"])}
            )
            background_stderr = await session.call_tool(
                "read_workspace_file", {"path": str(background_result["stderr_path"])}
            )
            if background_stdout.isError or background_stderr.isError:
                raise RuntimeError("background task logs could not be read as workspace files")

            timeout_task = await session.call_tool(
                "run_workspace_code",
                {
                    "language": "python",
                    "code": "import time; time.sleep(5)",
                    "background": True,
                    "timeout_seconds": 1,
                },
            )
            if timeout_task.isError:
                raise RuntimeError(f"timed background run failed to start: {timeout_task}")
            timeout_task_id = str(structured_result(timeout_task)["task_id"])
            timeout_result = await wait_for_task(session, timeout_task_id)
            cancellable_task = await session.call_tool(
                "run_workspace_code",
                {
                    "language": "python",
                    "code": "import time; time.sleep(30)",
                    "background": True,
                    "timeout_seconds": 60,
                },
            )
            if cancellable_task.isError:
                raise RuntimeError(f"cancellable background task failed to start: {cancellable_task}")
            cancellable_task_id = str(structured_result(cancellable_task)["task_id"])
            cancelled_result = await session.call_tool(
                "cancel_workspace_task",
                {"task_id": cancellable_task_id, "wait_seconds": 3},
            )
            if cancelled_result.isError:
                raise RuntimeError(f"cancel_workspace_task failed: {cancelled_result}")
            cancelled_payload = structured_result(cancelled_result)


            revert_url = f"{url.removesuffix('/mcp')}/diff/api/revert"
            wrong_password_rejected = False
            try:
                post_json(
                    revert_url,
                    {"path": ".mcp-revert-smoke-test.md", "password": "definitely-wrong"},
                )
            except urllib.error.HTTPError as error:
                wrong_password_rejected = error.code == 401
            reverted = post_json(
                revert_url,
                {
                    "path": ".mcp-revert-smoke-test.md",
                    "password": CONFIG.draft_commit_password,
                },
            )
            reverted_file = await session.call_tool(
                "read_workspace_file", {"path": "draft/.mcp-revert-smoke-test.md"}
            )

            for name, result in {
                "list_skills": skills,
                "list_draft_articles": articles,
                "load_skill": loaded,
                "write_workspace_file": written,
                "guard_created": guard_created,
                "guard_read": guard_read,
                "guard_overwrite": guard_overwrite,
                "revert_fixture": revert_fixture,
                "read_workspace_file": read_back,
                "list_workspace": listed_workspace,
                "search_workspace_text": searched_workspace,
                "move_workspace_file": moved_fixture,
                "delete_workspace_file": deleted_fixture,
                "read_workspace_range": ranged,
                "read_workspace_range_anchor": anchored,
                "replace_workspace_text": replaced,
                "insert_workspace_text": inserted,
                "apply_workspace_patch": patched,
                "run_workspace_code": executed,
            }.items():
                if result.isError:
                    raise RuntimeError(f"{name} failed: {result}")

            print("tools:", ", ".join(sorted(tool_names)))
            print("skill count:", len(skills.structuredContent["result"]))
            print("draft article count:", len(articles.structuredContent["result"]))
            article_paths = [
                article["path"] for article in articles.structuredContent["result"]
            ]
            if not article_paths or not all(path.startswith("draft/") for path in article_paths):
                raise RuntimeError(f"draft paths are not workspace-relative: {article_paths}")
            print("workspace-relative draft paths:", True)
            print("loaded red skill:", "小红书深度入口文章写作" in loaded.content[0].text)
            print("file round trip:", "写入成功" in read_back.content[0].text)
            read_back_payload = structured_result(read_back)
            listed_payload = structured_result(listed_workspace)
            searched_payload = structured_result(searched_workspace)
            print("read SHA-256:", len(str(read_back_payload.get("sha256", ""))) == 64)
            print("stale full overwrite rejected:", stale_overwrite.isError)
            print(
                "workspace listing:",
                any(
                    entry.get("path") == SMOKE_FILE
                    for entry in listed_payload.get("entries", [])
                ),
            )
            print(
                "workspace search:",
                bool(searched_payload.get("results")),
            )
            print("line range:", "# MCP smoke test" in ranged.content[0].text)
            print("anchor range:", "写入成功" in anchored.content[0].text)
            print("mismatch rejected:", rejected.isError)
            print("atomic edits:", "原子精确替换成功" in edited.content[0].text)
            print("unified patch:", "Patch 应用成功" in patched_file.content[0].text)
            print("bad patch rejected:", patch_rejected.isError)
            print("revert password protected:", wrong_password_rejected)
            print(
                "untracked revert:",
                reverted.get("action") == "deleted" and reverted_file.isError,
            )
            print("python execution:", "python execution ok" in executed.content[0].text)
            print("protocol long-poll concurrency:", read_elapsed <= 0.75)
            print("protocol foreground concurrency:", foreground_read_elapsed <= 0.75)
            print(
                "background execution:",
                background_result["status"] == "succeeded"
                and "background stdout" in background_stdout.content[0].text
                and "background stderr" in background_stderr.content[0].text,
            )
            print("background timeout:", timeout_result["status"] == "timed_out")
            print("background cancellation:", cancelled_payload["status"] == "cancelled")
            cleaned = await session.call_tool(
                "run_workspace_code",
                {
                    "language": "shell",
                    "code": (
                        "rm -f -- "
                        f"{shlex.quote(SMOKE_FILE)} {shlex.quote(GUARD_FILE)} "
                        "draft/.mcp-revert-smoke-test.md; "
                        f"rm -rf -- .mcp-tasks/{background_task_id} "
                        f".mcp-tasks/{timeout_task_id} "
                        f".mcp-tasks/{cancellable_task_id} "
                        f".mcp-tasks/{responsiveness_id}"
                    ),
                    "cwd": ".",
                },
            )
            if cleaned.isError:
                raise RuntimeError(f"cleanup failed: {cleaned}")
            if not rejected.isError:
                raise RuntimeError("replace_workspace_text accepted a mismatched expected_count")
            if not patch_rejected.isError:
                raise RuntimeError("apply_workspace_patch accepted mismatched context")
            if not wrong_password_rejected:
                raise RuntimeError("revert endpoint accepted the wrong password")
            if reverted.get("action") != "deleted" or not reverted_file.isError:
                raise RuntimeError("revert endpoint did not delete the untracked fixture")
            if background_result["status"] != "succeeded":
                raise RuntimeError(f"background task failed: {background_result}")
            if "background stdout" not in background_stdout.content[0].text:
                raise RuntimeError("background stdout was not captured")
            if "background stderr" not in background_stderr.content[0].text:
                raise RuntimeError("background stderr was not captured")
            if timeout_result["status"] != "timed_out":
                raise RuntimeError(f"background timeout failed: {timeout_result}")
            if len(str(read_back_payload.get("sha256", ""))) != 64:
                raise RuntimeError("read_workspace_file did not return SHA-256")
            if not stale_overwrite.isError:
                raise RuntimeError("write_workspace_file accepted a stale expected_sha256")
            if not any(
                entry.get("path") == SMOKE_FILE
                for entry in listed_payload.get("entries", [])
            ):
                raise RuntimeError(f"list_workspace missed smoke fixture: {listed_payload}")
            if not searched_payload.get("results"):
                raise RuntimeError(f"search_workspace_text missed smoke fixture: {searched_payload}")
            if cancelled_payload["status"] != "cancelled":
                raise RuntimeError(f"background cancellation failed: {cancelled_payload}")


if __name__ == "__main__":
    default_url = f"http://127.0.0.1:{CONFIG.port}{CONFIG.mcp_path}"
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else default_url))
