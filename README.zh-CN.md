<p align="center">
  <img src="assets/mcp-icon-10kb.png" alt="OverGPT icon" width="96" />
</p>

<h1 align="center">OverGPT</h1>

<p align="center"><strong>通过 MCP 为 ChatGPT 提供持久执行长任务以及孵化干净上下文 sub-agent 的能力。</strong></p>

<p align="center">
  <a href="README.md">English</a> · <a href="README.zh-CN.md">中文</a>
</p>

OverGPT 是 [WebCodex](https://github.com/yyjeqhc/webcodex) 的配套增强。WebCodex 让 ChatGPT 拥有真实的开发环境；OverGPT 则让 ChatGPT 自己能够跨越单次模型执行周期继续工作，并把独立任务委派给新的 ChatGPT sub-agent。

## 为什么会有这个项目

OpenAI Codex 已经提供了很强的 coding agent 体验，但 Codex 有自己独立的套餐使用额度，本地 Codex 消息和云端任务会共享这部分额度。对于普通的 plus 订阅，这部分额度很快会消耗干净。

[WebCodex](https://github.com/yyjeqhc/webcodex) 走的是另一条路径：它通过 MCP 把真实开发环境暴露给 ChatGPT。普通 ChatGPT 对话因此就可以直接读取代码库、修改文件、使用 Git、运行测试，并调用你自己机器上的开发工具。

这样一来，ChatGPT 本身就可以充当 coding agent，而不需要把每个开发任务都交给 Codex，而这种用法几乎不会遇到额度上限。

WebCodex 已经很好地解决了开发环境这一侧的问题：

- 代码库访问
- 安全文件编辑
- Git
- Shell 命令与测试
- 真实本地工具链
- 可持久运行的后台任务

还剩下两个模型侧的问题。

第一，ChatGPT 的单次对话执行时长仍然有边界。一个足够大的任务，可能需要的推理和工具调用超过一次对话能够完成的范围（大概 25 分钟左右）。

第二，一个对话的上下文不一定适合复杂任务中的所有分支。独立 review、验证、研究或者不同实现方案，很多时候放进新的 sub-agent 干净上下文里效果更好。

OverGPT 主要补上这两个能力。

## OverGPT 和 WebCodex 怎么配合

从概念上看，WebCodex 是 **开发环境**，OverGPT 是 ChatGPT 一侧的 **执行控制层**。

```text
                         ChatGPT
                            │
              ┌─────────────┴─────────────┐
              │                           │
              ▼                           ▼
         OverGPT                  WebCodex
           执行控制                    开发工具
              │                           │
      ┌───────┼────────┐                  ▼
      │       │        │                你的机器
    续接     委派      监督                │
      │       │        │          ┌────────┼────────┐
      │       │        │          │        │        │
      │       │        │        repo      Git    测试/工具
      │       │        │
      │       ▼        │
      │    新的 ChatGPT sub-agent
      │       │
      └───────┴─── 使用同一套 MCP 环境
```

所以 WebCodex 用户不需要改变现有代码库、Git checkout、测试或者本地工具链。OverGPT 直接在这套工作流上补充更长的 ChatGPT 执行生命周期和递归委派能力。

OverGPT 不取代 WebCodex。两个项目解决的是同一个工作流里的不同层次。

## 跨越单次 ChatGPT turn 继续执行

OverGPT 会为一个 ChatGPT 对话维护由服务端决定的执行计时。

长 session 启用以后，MCP 工具返回值会包含当前剩余执行预算。当这一轮接近执行边界时，ChatGPT 可以完成当前原子步骤，保存足够的恢复状态，然后注册下一次唤醒。

OverGPT 随后会向同一个对话发送继续执行的消息。

```text
开始
 │
 ▼
工作 ──► 工作 ──► 工作
                 │
              到达阈值
                 │
                 ▼
              保存状态
                 │
                 ▼
              注册唤醒
                 │
                 ▼
          同一对话继续执行
```

这里最重要的是续接过程是显式的：剩余时间由服务端提供，让出当前执行窗口前先保存进度，下一轮再继续已有任务。

因此一个逻辑任务可以跨越多个 ChatGPT turn，而不需要假设一次模型执行能够无限持续。

## 孵化新的递归 sub-agent

OverGPT 可以启动新的 ChatGPT 对话作为独立 worker。

每个委派任务都有明确的输入、持久化输出、任务状态和独立浏览器执行槽。父代理会立即拿到 task ID，因此可以在子任务独立运行时继续自己的工作。

```text
                    父 ChatGPT
                         │
                   委派独立任务
                         │
             ┌───────────┴───────────┐
             ▼                       ▼
        sub-agent A               sub-agent B
             │                       │
         持久输出                 持久输出
             │                       │
             └───────────┬───────────┘
                         ▼
                    父代理读取结果
```

sub-agent 还可以继续使用同一套机制向下委派，因此整个过程可以递归展开。

新的干净上下文尤其适合独立 code review、对抗性验证、并行研究、不同实现方案探索，或者任何不希望每个分支都继承父对话完整历史的任务。

## 持久化执行状态

OverGPT 会把任务交接和完成状态保存在单次模型 turn 之外。

sub-agent 的输入和输出都会写入共享 workspace。后台任务拥有明确的状态，例如 `queued`、`running`、`succeeded`、`failed`、`timed_out` 和 `cancelled`。

这样父对话可以通过真实状态监督被委派的工作，而不需要依赖隐藏在内存里的 agent orchestration。

WebCodex 可以继续负责代码库执行和耗时较长的开发任务。OverGPT 的持久状态主要服务于模型侧生命周期：续接、委派、监督和结果回收。

## 当前实现

当前实现面向能够访问 MCP 的 ChatGPT。

会话续接和 sub-agent 孵化通过 Playwright 驱动已经登录的 ChatGPT Web 会话完成。持久 workspace 文件负责任务交接和结果回收，MCP server 提供计时、任务监督以及继续执行和委派所需要的控制能力。

OverGPT 自己也带有基础 workspace 工具，但它的目标是与 WebCodex 配合使用。对于软件工程场景，WebCodex 提供更完整的代码库和开发工具层，OverGPT 则负责让 ChatGPT 能够跨 turn 持续工作，并把独立任务拆到新的对话里执行。

---

部署、运维与贡献者说明见 [AGENTS.md](AGENTS.md)。
