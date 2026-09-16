# Writing Workspace MCP

一个最小的远程 MCP 服务：让 ChatGPT 或其他 MCP 客户端发现写作 skills，
并读写由部署配置指定的共享 workspace。

![Writing Workspace MCP icon](assets/mcp-icon-10kb.png)

## Tools

- `list_skills`：列出 `skills/*/SKILL.md`
- `list_draft_articles`：列出 `draft` 内 Markdown 的 workspace 相对路径和第一行标题
- `load_skill`：加载完整的 `SKILL.md`
- `read_workspace_file`：读取共享目录内的文本文件，并返回内容 SHA-256
- `read_workspace_range`：按行号范围或唯一锚点读取局部上下文，并返回整文件 SHA-256
- `list_workspace`：按有限深度列出 workspace 文件和目录
- `search_workspace_text`：在源码文本中进行带路径和行号的 literal search
- `write_workspace_file`：原子创建文件；覆盖已有文件时必须带上之前读取到的 SHA-256
- `delete_workspace_file`：删除单个文件，可用 SHA-256 防止 stale delete
- `move_workspace_file`：安全移动/重命名单个文件，不覆盖已有目标，可用 SHA-256 防止 stale move
- `replace_workspace_text`：校验匹配次数后进行原子精确替换
- `insert_workspace_text`：校验锚点匹配次数后在其前后原子插入
- `apply_workspace_patch`：在全部 context 匹配时原子应用 unified diff
- `restart_mcp_server`：校验修改后的 Python 代码，然后退出并由 Docker 自动拉起
- `run_workspace_code`：运行 Python 或 Shell；`background=true` 可启动持久化后台任务
- `get_workspace_task`：服务端等待并查询后台任务状态，返回 stdout/stderr 文件路径
- `cancel_workspace_task`：请求取消后台任务，并终止其整个命令进程组
- `spawn_chatgpt_subagent`：接收完整任务，自动分配文件并异步委派给新的 ChatGPT 网页对话

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

首次部署建议先准备 `.env`，然后让 agent 或人工依次运行：

```bash
cp .env.example .env
# edit .env
make doctor
make bootstrap
```

日常操作：

```bash
make up
make ps
make logs
make test
make down
```

`make doctor` 检查 `.env`、Docker/Compose 配置以及可选 remote sync 所需的 SSH/rsync 依赖；
`make bootstrap` 会构建镜像、启动服务、等待 MCP readiness 并执行完整 smoke test。
ChatGPT Web automation 若启用，首次部署仍需要一次人工浏览器登录来生成持久 profile。

容器内部 workspace 固定挂载到 `/opt/workspace`。项目相对该 workspace 的目录由
`MCP_PROJECT_RELATIVE_PATH` 指定，默认是 `mymcp`；这些都是容器内部布局，不依赖宿主机
绝对路径。宿主机 workspace 由 `.env` 中的 `MCP_WORKSPACE_HOST_PATH` 选择。
修改 `.py` 文件后执行 `make restart` 即可，无需重新构建镜像；只有
`requirements.txt` 或 `Dockerfile` 改变时才需要 `make build`。
Docker image、项目入口脚本和后台子进程环境都会禁用 Python 自动写入 `.pyc`，因此正常
运行不会生成 `__pycache__`。直接用其他 Python 入口导入项目模块时，应显式设置
`PYTHONDONTWRITEBYTECODE=1`。`python -m compileall` 的目的就是生成 bytecode，不应用作
无缓存语法检查。

## Repository editing semantics

日常代码仓库操作优先使用结构化 workspace tools：用 `list_workspace` / `search_workspace_text`
定位文件，用 `read_workspace_file` / `read_workspace_range` 读取，再用
`replace_workspace_text`、`insert_workspace_text` 或 `apply_workspace_patch` 做带当前内容校验的
修改。`run_workspace_code` 保留为 Git、测试、构建和非常规查询的 escape hatch，不应默认用
shell `rm` / `mv` 代替已有的安全文件工具。

读取接口会返回整文件 SHA-256。`write_workspace_file` 覆盖已有文件时必须同时提供
`overwrite=true` 和最近一次 read 返回的 `expected_sha256`；如果文件在 read→write 之间被
另一个 agent 修改，覆盖会失败而不会产生 lost update。`delete_workspace_file` 和
`move_workspace_file` 也接受可选的 `expected_sha256`，用于同样的 stale-operation guard。

这些 guard 保护通过 MCP 文件 API 协作的并发 agent。任意外部程序或
`run_workspace_code` 中直接修改文件的 shell 命令仍然可以绕过这套协议，因此普通文件修改应
尽量留在结构化文件工具内。

## Background tasks

长时间运行的命令应使用 `run_workspace_code(background=true)`。调用会立即返回
`task_id`，随后用 `get_workspace_task` 查询 `queued`、`running`、`succeeded`、
`failed`、`timed_out` 或 `cancelled` 状态。任务元数据和完整日志保存在 workspace 下的
`.mcp-tasks/<task_id>/`，即使 MCP HTTP 连接断开也不会丢失。同步任务默认超时 30 秒、
最多 120 秒；后台任务默认 1 小时、最多 24 小时。同步和后台执行在超时时都会终止整个
子进程组，避免脚本 spawn 的后代进程残留。后台任务会把状态记为 `timed_out`；也可以用
`cancel_workspace_task` 主动取消，worker 会持久化取消请求并终止对应命令进程组。任务状态
和日志保存在磁盘，不依赖 MCP 进程内存；
因此重启 MCP 不会丢失记录。每次启动新任务前，服务会删除完成超过 30 天的标准
`.mcp-tasks/task_*` 目录；正在排队或运行、状态无法解析及名称不符合规范的目录不会删除。
这些运行策略集中定义在 `config.py`。

`get_workspace_task` 默认在服务端等待最多 30 秒；任务在等待期间结束会立即返回，因此
调用方不需要每 10 秒重新发起一次工具调用。状态接口只返回 `stdout_path` 和
`stderr_path`，不返回日志正文；只有状态变为 `failed` 或 `timed_out` 时，才建议通过
`read_workspace_file` 或 `read_workspace_range` 读取对应日志。单次等待上限默认 60 秒；
默认值和上限集中定义在 `config.py`。

直接在 macOS/Linux 运行时，子进程继承当前用户的 `HOME`，便于使用 Whisper 缓存和
DaVinci Resolve。Docker 部署通过 `MCP_EXECUTION_HOME=/tmp/mcp-home` 使用独立的临时 HOME。

Compose 使用 `restart: unless-stopped`；只要宿主机 Docker 服务会在启动时恢复，MCP
容器也会自动恢复。Agent 修改项目内的 Python 后，可以调用 `restart_mcp_server` 热加载
新代码；该工具会先在新 Python 进程中执行导入校验，避免明显的语法或导入错误触发
无休止的重启循环。

默认本机 endpoint 为 `http://127.0.0.1:8765/mcp`；可通过 `MCP_BIND_HOST` 和
`MCP_PORT` 调整宿主机绑定。

公网接入应通过宿主机 Nginx 将受保护的 HTTPS 路径反向代理到这个本机 endpoint。

## Configuration with `.env`

复制示例配置后，只修改 `.env`，无需编辑 `config.py`：

```bash
cp .env.example .env
```

Docker 部署至少应确认宿主机 workspace、容器 UID/GID 和 Diff 操作密码：

```dotenv
MCP_WORKSPACE_HOST_PATH=/absolute/path/to/workspace
MCP_PROJECT_RELATIVE_PATH=mymcp
MCP_UID=1000
MCP_GID=1000
MCP_SERVER_NAME='Writing Workspace MCP'
MCP_DRAFT_COMMIT_PASSWORD='replace-with-a-secret'
```

`.env.example` 是部署配置的 canonical contract；仓库中的 Makefile、Compose 和 Python 配置
不应保存某个维护者自己的宿主机路径、SSH endpoint 或凭据。`.env` 同时保持 shell-compatible，
因为 `make doctor` 和 remote-sync targets 会 source 它；包含空格或 shell metacharacter 的值
应使用引号，密码推荐使用单引号。

Makefile 会把 `ENV_FILE`（默认 `./.env`）显式传给 Compose；直接运行 `docker compose`
时 Compose 仍按默认规则读取项目目录的 `.env`。容器内部 workspace 保持为
`/opt/workspace`；`MCP_WORKSPACE_HOST_PATH` 只表示 bind mount 的宿主机路径。
`MCP_PORT` 会同时控制服务监听端口、端口映射、Makefile 健康检查和 smoke test。
ChatGPT profile 默认直接保存在代码库的 `chatgpt-profile/`，通过 workspace 挂载持久化；
该目录已被 Git 和 Docker build context 忽略。

如果同一个 checkout 还需要和远端工作副本双向同步，在 `.env` 中增加：

```dotenv
MCP_SYNC_HOST=user@example-host
MCP_SYNC_PORT=22
MCP_SYNC_REMOTE_DIR=/absolute/path/to/remote/mymcp
```

随后使用 `make sync-down` / `make sync-up`。如果 host 和 remote directory 都为空，这两个
目标会明确跳过，因此共享仓库不依赖任何特定的 SSH 环境。`sync-down` 在本地存在 staged、
unstaged 或 untracked 修改时会拒绝覆盖；当明确要让 remote 覆盖本地工作区时，使用
`make sync-down force`（或 `make sync-down FORCE=1`）跳过这一 Git dirty-worktree guard。
同步策略默认不使用 `--delete`。

可选的额外 bind mount 放在 `compose.additional.yaml`。默认不加载该文件，因此无需设置
额外路径也能正常运行。需要挂载时，在 `.env` 中启用 overlay，并配置宿主机路径、容器
路径和只读模式：

```dotenv
COMPOSE_FILE=compose.yaml:compose.additional.yaml
MCP_ADDITIONAL_HOST_PATH=/absolute/host/path
MCP_ADDITIONAL_CONTAINER_PATH=/opt/additional
MCP_ADDITIONAL_READ_ONLY=true
```

Docker 必须在启动容器之前解析挂载，因此这些宿主机配置属于 Compose/`.env`，不能由
容器内才加载的 `config.py` 决定。overlay 会把容器路径作为 `MCP_ADDITIONAL_ROOT` 传给
`config.py`；未启用时该配置为 `None`。这个机制支持一个可选额外挂载；任意更多的特殊
挂载应继续用 Compose long syntax 明确声明用途、source、target 和 `read_only`，不要把
一整段 volume 列表塞进单个环境变量。

直接运行 Python 时，`config.py` 会通过 `python-dotenv` 主动读取同目录 `.env`：

```dotenv
MCP_WORKSPACE_ROOT=/absolute/path/to/workspace
MCP_PORT=8765
MCP_EXECUTION_HOME=
```

对于仍支持外部配置的字段，优先级为：当前进程已有的环境变量 > `.env` > `config.py`
默认值。`.env` 已加入
`.gitignore` 和 `.dockerignore`，不会被提交到 Git 或复制进 Docker image；容器运行时
通过 workspace bind mount 仍可读取它。修改 Compose 使用的变量后执行
`make apply-config`，仅修改直接运行的 Python 配置则重启进程。

`.env` 只保留机器路径、SSH/sync endpoint、端口、密码、显示名、容器 UID/GID、资源额度
以及浏览器运行模式等部署差异。目录结构、协议路径、超时、大小限制、Git diff 策略和任务
保留期等稳定策略只在 `config.py` 中定义；sub-agent 生命周期标记是
`chatgpt_playwright.py` 中的固定协议常量。

## ChatGPT sub-agent automation

这个接口默认关闭。Docker 镜像包含 Playwright Chromium；本地直接运行时则需要另行安装
浏览器依赖。
调用 `spawn_chatgpt_subagent` 时直接传入完整任务即可。每个 sub-agent 复用标准后台任务目录：
`.mcp-tasks/task_<uuid>/` 同时保存 `request.json`、`status.json`、stdout/stderr、`input.md`
和最终的 `output.md`。`input.md` 在启动前创建，`output.md` 由 child 首次创建；任务正文不会
复制进浏览器。新的 ChatGPT 对话会使用名为 `writer` 的 MCP 读取输入，并将全部结果写入
该 task 自己的 output。整个目录随标准 task retention 一起清理。

安装本地可选依赖并进行一次登录：

```bash
python -m pip install -r requirements.txt -r requirements-playwright.txt
python tests/test_chatgpt_playwright.py
python chatgpt_playwright.py login
```

`login` 会启动普通 Chrome，而不是让 Playwright 控制登录过程。登录成功后关闭这个
Chrome 窗口，命令就会结束。登录状态保存在代码库的 `chatgpt-profile/`，不会读取日常
Chrome profile。随后在 `.env` 中设置本地 workspace 并启用接口：

```dotenv
MCP_CHATGPT_AUTOMATION_ENABLED=true
MCP_WORKSPACE_ROOT=/absolute/path/to/workspace
```

调用示例：

```json
{
  "task": "读取 draft/example.md，检查论证结构，并把修改建议写入结果文件。"
}
```

接口立即返回标准后台任务信息，以及自动生成的 `input_path` 和 `output_path`。
`input_path` 保存委派任务，`output_path` 是 subagent 的最终业务结果；`stdout_path` 和
`stderr_path` 只保存 Playwright/后台执行日志，不能替代结果文件。随后用
`get_workspace_task(task_id)` 查询状态，其异步状态、超时和日志机制与
`run_workspace_code(background=true)` 完全一致。

查询 subagent 时直接使用 `get_workspace_task` 的默认参数即可：服务端最多等待 30 秒，
任务完成则提前返回。失败或超时后再读取返回的 stdout/stderr 文件路径。

不启动本地 MCP 服务也可以直接调用同一套固定模板和 Playwright 发送逻辑。这里的路径
本地脚本仍支持传入一对已经分配好的远端路径，用于单独调试 Playwright 发送流程；它
不会尝试读取或等待这些文件：

```bash
python chatgpt_playwright.py send \
  --input-path .mcp-tasks/task_0123456789abcdef0123456789abcdef/input.md \
  --output-path .mcp-tasks/task_0123456789abcdef0123456789abcdef/output.md
```

CLI 只验证它们是安全的 workspace 相对路径，在临时对话中输入提示词并点击官方发送
按钮后打印 `sent`。它不要求本地存在远端文件，也不检测远端任务完成。

MCP 后台任务使用两个固定标记区分“网页端已经开始执行”和“结果已经完成”：

```text
started sentinel:    WRITERSUBAGENTSTARTED4C81E2B5
completion sentinel: WRITERSUBAGENTCOMPLETE7D3A9F6C
start timeout:       300 seconds
completion timeout:  3600 seconds
```

网页端的第一项操作是创建 `output.md` 并写入 started sentinel。Playwright 检测到该标记后即可
关闭页面；网页端随后用带 SHA-256 guard 的原子覆盖写入最终结果和 completion sentinel。默认
远端 completion wait 为 1 小时，而且这个计时从 started acknowledgement 后开始。外层后台
supervisor 使用更大的任务上限覆盖 browser queue/setup、started acknowledgement 和 completion
三个阶段；若外层任务最终超时，整个受控 process group 会被终止并进入 `timed_out`。

默认打开 `https://chatgpt.com/?temporary-chat=true`。脚本一次性填写提示词，并在点击
发送前确认当前编辑器仍包含唯一的 input 路径、output 路径和两个生命周期标记；缺失时不会发送。

通过 MCP 调用时，`spawn_chatgpt_subagent` 不阻塞等待结果，而是立即返回后台
`task_id`。把这个 id 传给现有的 `get_workspace_task` 轮询：`queued` 或 `running` 表示
仍在处理，`succeeded` 表示输出文件已经更新且检测到末尾标记；`failed` 或 `timed_out`
表示没有正常完成。完整执行结果和临时页面 URL 保存在该任务的 stdout 日志中。

登录 profile 是只读模板。每个浏览器任务先在短暂的跨进程锁内，把必要的登录状态复制到
repo 内 `chatgpt-task-profiles/slot_00` 至 `slot_09` 的独立运行槽，再填写并验证 prompt、点击
发送，并保持临时对话打开，直到 `output.md` 出现 started sentinel。随后会关闭 context、清空
槽内 profile，并在后台等待 completion sentinel。固定槽把浏览器并发限制为 10，也限制了临时
profile 的最大数量；该目录被 Git、Docker build、rsync 和 Syncthing 忽略。多个 sub-agent 及
递归委派不会并发写入持久登录 profile。`spawn_chatgpt_subagent` 会立即返回后台 task。ChatGPT
DOM 变化或登录过期时，需要重新登录或更新 `chatgpt_playwright.py` 中的选择器。

### VPS browser login

Compose 将独立浏览器 profile 挂载到宿主机，不会随容器重建而丢失。首次登录或登录
失效时启动临时 noVNC 服务：

```bash
make login-up
```

然后在已经配置 `MCP_SYNC_HOST` 和 `MCP_SYNC_PORT` 的本地仓库中运行：

```bash
make login-forward
```

该命令以前台阻塞方式建立 SSH tunnel，连接成功后自动打开 noVNC 页面；按 `Ctrl+C`
即可关闭 tunnel。默认把本地 `6080` 转发到 VPS 的 `6080`，本地端口冲突时可以执行
`MCP_VNC_LOCAL_PORT=6081 make login-forward`。完成 ChatGPT 登录后关闭 Chromium 窗口，
再在 VPS 执行 `make login-down`。noVNC 默认只绑定 VPS 回环地址，不应开放到公网。正式服务设置
`MCP_CHATGPT_BROWSER_HEADLESS=true`，并使用同一个持久 profile。Compose 只为隔离容器中
的登录浏览器启用 `--no-sandbox`；本地直接运行时默认保持浏览器 sandbox。

部分云服务器 IP 在纯 headless 模式下会停在浏览器验证页。这种情况下设置
`MCP_CHATGPT_BROWSER_HEADLESS=false`；主容器会自动启动不对外开放的 Xvfb 虚拟屏幕，
让 Playwright 使用 headed Chromium。noVNC 仍然只在人工登录时按需启动。

### Observe real sub-agent browser automation

当提示词已经发送但输出文件没有写回时，先在 VPS 项目目录运行：

```bash
make debug-ui-up
```

然后在 Mac 本地仓库运行：

```bash
make debug-ui
```

`debug-ui` 只在 Mac 建立到本地 `6081` 的 SSH tunnel 并自动打开页面，不会在 VPS 执行
任何命令。它观察的是实际执行 `spawn_chatgpt_subagent` 的 Xvfb，而不是会争用 profile 的
登录容器。连接完成后再触发一个 sub-agent；调试模式会在点击发送后保留浏览器 60 秒，
方便确认 ChatGPT 是否调用了 `writer` 工具。按 `Ctrl+C` 只关闭本地 tunnel。调试完成后，
在 VPS 项目目录运行：

```bash
make debug-ui-down
```

正常模式会在 sub-agent 写回 started sentinel 后关闭浏览器；调试模式额外保留页面 60 秒。
