"""Small protocol-level smoke test for the deployed Streamable HTTP server."""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from config import CONFIG


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


async def wait_for_task(session: ClientSession, task_id: str) -> dict[str, object]:
    for _ in range(100):
        result = await session.call_tool(
            "get_workspace_task",
            {"task_id": task_id, "wait_seconds": 1},
        )
        if result.isError:
            raise RuntimeError(f"get_workspace_task failed: {result}")
        payload = structured_result(result)
        if payload["status"] in {"succeeded", "failed", "timed_out"}:
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
                "replace_workspace_text",
                "insert_workspace_text",
                "apply_workspace_patch",
                "get_workspace_task",
                "restart_mcp_server",
                "write_workspace_file",
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
            written = await session.call_tool(
                "write_workspace_file",
                {
                    "path": "mymcp/.mcp-smoke-test.md",
                    "content": "# MCP smoke test\n\n写入成功。\n",
                    "overwrite": True,
                },
            )
            revert_fixture = await session.call_tool(
                "write_workspace_file",
                {
                    "path": "draft/.mcp-revert-smoke-test.md",
                    "content": "# Revert smoke test\n",
                    "overwrite": True,
                },
            )
            read_back = await session.call_tool(
                "read_workspace_file", {"path": "mymcp/.mcp-smoke-test.md"}
            )
            ranged = await session.call_tool(
                "read_workspace_range",
                {"path": "mymcp/.mcp-smoke-test.md", "start_line": 1, "end_line": 2},
            )
            anchored = await session.call_tool(
                "read_workspace_range",
                {"path": "mymcp/.mcp-smoke-test.md", "anchor": "写入成功", "context_lines": 1},
            )
            replaced = await session.call_tool(
                "replace_workspace_text",
                {
                    "path": "mymcp/.mcp-smoke-test.md",
                    "old_text": "写入成功。",
                    "new_text": "精确替换成功。",
                    "expected_count": 1,
                },
            )
            rejected = await session.call_tool(
                "replace_workspace_text",
                {
                    "path": "mymcp/.mcp-smoke-test.md",
                    "old_text": "不存在",
                    "new_text": "不应写入",
                    "expected_count": 1,
                },
            )
            inserted = await session.call_tool(
                "insert_workspace_text",
                {
                    "path": "mymcp/.mcp-smoke-test.md",
                    "anchor": "精确替换成功。",
                    "text": "原子",
                    "position": "before",
                    "expected_count": 1,
                },
            )
            edited = await session.call_tool(
                "read_workspace_file", {"path": "mymcp/.mcp-smoke-test.md"}
            )
            patch = (
                "--- a/mymcp/.mcp-smoke-test.md\n"
                "+++ b/mymcp/.mcp-smoke-test.md\n"
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
                "read_workspace_file", {"path": "mymcp/.mcp-smoke-test.md"}
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
                "revert_fixture": revert_fixture,
                "read_workspace_file": read_back,
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
            print(
                "background execution:",
                background_result["status"] == "succeeded"
                and "background stdout" in background_stdout.content[0].text
                and "background stderr" in background_stderr.content[0].text,
            )
            print("background timeout:", timeout_result["status"] == "timed_out")
            cleaned = await session.call_tool(
                "run_workspace_code",
                {
                    "language": "shell",
                    "code": (
                        "rm -f mymcp/.mcp-smoke-test.md draft/.mcp-revert-smoke-test.md; "
                        f"rm -rf -- .mcp-tasks/{background_task_id} "
                        f".mcp-tasks/{timeout_task_id}"
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


if __name__ == "__main__":
    default_url = f"http://127.0.0.1:{CONFIG.port}{CONFIG.mcp_path}"
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else default_url))
