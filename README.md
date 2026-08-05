# Writing Workspace MCP

一个最小的远程 MCP 服务：让 ChatGPT 或其他 MCP 客户端发现写作 skills，
并读写 `/home/god/Dropbox/workspace` 中的文档。

![Writing Workspace MCP icon](assets/mcp-icon.png)

## Tools

- `list_skills`：列出 `skills/*/SKILL.md`
- `list_draft_articles`：列出 `draft` 内 Markdown 的 workspace 相对路径和第一行标题
- `load_skill`：加载完整的 `SKILL.md`
- `read_workspace_file`：读取共享目录内的文本文件
- `write_workspace_file`：原子写入共享目录内的文本文件
- `render_diff_html`：把 `draft` 仓库的完整 word-diff 作为 `draft-diff.html` 文件返回
- `run_workspace_code`：在容器中运行 Python 或 Shell

所有客户端路径都相对于共享目录，例如 `draft/demo.md`。服务会拒绝绝对路径和
`../` 目录逃逸。

`render_diff_html.paths` 同时接受 `draft/demo.md` 和 `demo.md`；前者会在安全校验前
移除开头的 `draft/`。

## Operations

```bash
make up
make ps
make logs
make test
make down
```

容器内代码目录是 `/opt/workspace/mymcp`，它来自宿主机共享目录的直接挂载。
修改 `.py` 文件后执行 `make restart` 即可，无需重新构建镜像；只有
`requirements.txt` 或 `Dockerfile` 改变时才需要 `make build`。

本机 endpoint: `http://127.0.0.1:8765/mcp`

公网接入应通过宿主机 Nginx 将受保护的 HTTPS 路径反向代理到这个本机 endpoint。
