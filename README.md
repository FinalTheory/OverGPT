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
- `restart_mcp_server`：校验修改后的 Python 代码，然后退出并由 Docker 自动拉起
- `run_workspace_code`：在容器中运行 Python 或 Shell

所有客户端路径都相对于共享目录，例如 `draft/demo.md`。服务会拒绝绝对路径和
`../` 目录逃逸。

## Draft Diff 网页

访问 `/diff/` 可按 `draft` 的目录结构浏览相对 `HEAD` 有改动的文件；没有 diff 的
文件不显示。点击普通文件只显示 Git diff，不会显示完整文件内容；已删除文件只显示
删除状态。词级高亮由 Git `--word-diff=porcelain` 生成，并使用 UTF-8 locale 处理中文。
Commit 只提交当前文件，并要求后端密码校验。

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

Compose 使用 `restart: unless-stopped`，且宿主机 Docker 服务已设置为开机启动，
所以 VPS 或 Docker 重启后会自动恢复 MCP。GPT 修改 `mymcp` 内的 Python 后，可以调用
`restart_mcp_server` 热加载新代码；该工具会先在新 Python 进程中执行导入校验，避免
明显的语法或导入错误触发无休止的重启循环。

本机 endpoint: `http://127.0.0.1:8765/mcp`

公网接入应通过宿主机 Nginx 将受保护的 HTTPS 路径反向代理到这个本机 endpoint。
