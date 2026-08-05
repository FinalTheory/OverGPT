"""Small protocol-level smoke test for the deployed Streamable HTTP server."""

from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import EmbeddedResource, TextResourceContents


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
                "render_diff_html",
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
            rendered_diff = await session.call_tool(
                "render_diff_html",
                {"paths": ["draft/2026-04/casi-snow.md"]},
            )
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
            executed = await session.call_tool(
                "run_workspace_code",
                {"language": "python", "code": "print('python execution ok')"},
            )

            for name, result in {
                "list_skills": skills,
                "list_draft_articles": articles,
                "load_skill": loaded,
                "render_diff_html": rendered_diff,
                "write_workspace_file": written,
                "read_workspace_file": read_back,
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
            diff_file = rendered_diff.content[0]
            if not isinstance(diff_file, EmbeddedResource):
                raise RuntimeError(f"render_diff_html did not return a file: {diff_file}")
            if not isinstance(diff_file.resource, TextResourceContents):
                raise RuntimeError(f"render_diff_html returned a non-text file: {diff_file.resource}")
            print(
                "rendered diff file:",
                str(diff_file.resource.uri).endswith("/draft-diff.html")
                and diff_file.resource.mimeType == "text/html"
                and "<!doctype html>" in diff_file.resource.text,
            )
            print("file round trip:", "写入成功" in read_back.content[0].text)
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


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/mcp"))
