"""Small protocol-level smoke test for the deployed Streamable HTTP server."""

from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


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
                "restart_mcp_server",
                "write_workspace_file",
                "run_workspace_code",
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

            for name, result in {
                "list_skills": skills,
                "list_draft_articles": articles,
                "load_skill": loaded,
                "write_workspace_file": written,
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
            print("python execution:", "python execution ok" in executed.content[0].text)
            cleaned = await session.call_tool(
                "run_workspace_code",
                {
                    "language": "shell",
                    "code": "rm -f .mcp-smoke-test.md",
                    "cwd": "mymcp",
                },
            )
            if cleaned.isError:
                raise RuntimeError(f"cleanup failed: {cleaned}")
            if not rejected.isError:
                raise RuntimeError("replace_workspace_text accepted a mismatched expected_count")
            if not patch_rejected.isError:
                raise RuntimeError("apply_workspace_patch accepted mismatched context")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/mcp"))
