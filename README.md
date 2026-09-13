# Writing Workspace MCP

一个最小的远程 MCP 服务：让 ChatGPT 或其他 MCP 客户端发现写作 skills，
并读写 `/home/god/Dropbox/workspace` 中的文档。

![Writing Workspace MCP icon](assets/mcp-icon.png)

## Tools

- `list_skills`：列出 `skills/*/SKILL.md`
- `list_draft_articles`：列出 `draft` 内 Markdown 的 workspace 相对路径和第一行标题
- `load_skill`：加载完整的 `SKILL.md`
- `read_workspace_file`：读取共享目录内的文本文件
- `read_workspace_range`：按行号范围或唯一锚点读取局部上下文
- `write_workspace_file`：原子写入共享目录内的文本文件
- `replace_workspace_text`：校验匹配次数后进行原子精确替换
- `insert_workspace_text`：校验锚点匹配次数后在其前后原子插入
- `apply_workspace_patch`：在全部 context 匹配时原子应用 unified diff
- `restart_mcp_server`：校验修改后的 Python 代码，然后退出并由 Docker 自动拉起
- `run_workspace_code`：运行 Python 或 Shell；`background=true` 可启动持久化后台任务
- `get_workspace_task`：查询后台任务状态以及 stdout/stderr 日志尾部

所有客户端路径都相对于共享目录，例如 `draft/demo.md`。服务会拒绝绝对路径和
`../` 目录逃逸。

## Draft Diff 网页

访问 `/diff/` 可按 `draft` 的目录结构浏览相对 `HEAD` 有改动的文件；没有 diff 的
文件不显示。点击普通文件只显示 Git diff，不会显示完整文件内容；已删除文件只显示
删除状态。词级高亮由 Git `--word-diff=porcelain` 生成，并使用 UTF-8 locale 处理中文。
选择文件后可以把比较基准从 `HEAD` 切换到该文件的任意历史 commit。Commit 只提交
当前文件；Revert 将 tracked 文件恢复到 `HEAD`，或删除 untracked 文件。两项操作使用
同一个后端密码校验；查看历史基准时均不可用。

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

## Background tasks

长时间运行的命令应使用 `run_workspace_code(background=true)`。调用会立即返回
`task_id`，随后用 `get_workspace_task` 查询 `queued`、`running`、`succeeded`、
`failed` 或 `timed_out` 状态。任务元数据和完整日志保存在 workspace 下的
`.mcp-tasks/<task_id>/`，即使 MCP HTTP 连接断开也不会丢失。同步任务默认最多运行
120 秒；后台任务默认 6 小时、最多 24 小时，可通过 Compose 环境变量调整。

直接在 macOS/Linux 运行时，子进程继承当前用户的 `HOME`，便于使用 Whisper 缓存和
DaVinci Resolve。Docker 部署则通过 `MCP_EXECUTION_HOME=/tmp/mcp-home` 保持原行为。

Compose 使用 `restart: unless-stopped`，且宿主机 Docker 服务已设置为开机启动，
所以 VPS 或 Docker 重启后会自动恢复 MCP。GPT 修改 `mymcp` 内的 Python 后，可以调用
`restart_mcp_server` 热加载新代码；该工具会先在新 Python 进程中执行导入校验，避免
明显的语法或导入错误触发无休止的重启循环。

本机 endpoint: `http://127.0.0.1:8765/mcp`

公网接入应通过宿主机 Nginx 将受保护的 HTTPS 路径反向代理到这个本机 endpoint。

## Configuration with `.env`

复制示例配置后，只修改 `.env`，无需编辑 `config.py`：

```bash
cp .env.example .env
```

Docker 部署至少应设置宿主机 workspace 和 Diff 操作密码：

```dotenv
MCP_WORKSPACE_HOST_PATH=/absolute/path/to/workspace
MCP_DRAFT_COMMIT_PASSWORD=replace-with-a-secret
```

Compose 会自动读取项目目录的 `.env` 并把变量映射到容器。容器内部路径保持为
`/opt/workspace`；`MCP_WORKSPACE_HOST_PATH` 只表示 bind mount 的宿主机路径。
`MCP_PORT` 会同时控制服务监听端口、端口映射、Makefile 健康检查和 smoke test。

直接运行 Python 时，`config.py` 会通过 `python-dotenv` 主动读取同目录 `.env`：

```dotenv
MCP_WORKSPACE_ROOT=/absolute/path/to/workspace
MCP_HOST=127.0.0.1
MCP_PORT=8765
MCP_EXECUTION_HOME=
```

配置优先级为：当前进程已有的环境变量 > `.env` > `config.py` 默认值。`.env` 已加入
`.gitignore` 和 `.dockerignore`，不会被提交到 Git 或复制进 Docker image；容器运行时
通过 workspace bind mount 仍可读取它。修改 Compose 使用的变量后执行
`make apply-config`，仅修改直接运行的 Python 配置则重启进程。
