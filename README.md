# OverGPT

**Durable execution and recursive sub-agents for ChatGPT over MCP.**

[English](README.md) · [中文](README.zh-CN.md)

OverGPT is a companion to [WebCodex](https://github.com/yyjeqhc/webcodex). WebCodex gives ChatGPT a real development environment; OverGPT lets the ChatGPT side keep working beyond one model turn and delegate work to fresh ChatGPT sub-agents.

## Why this exists

OpenAI Codex already provides a strong coding-agent experience, but Codex has its own plan usage allowance. Local Codex messages and cloud tasks share that allowance. On a regular Plus subscription, that allowance can be used up fairly quickly.

[WebCodex](https://github.com/yyjeqhc/webcodex) takes a different path: it exposes your real development environment through MCP, so an ordinary ChatGPT conversation can inspect repositories, edit files, use Git, run tests, and execute developer tools on your own machine.

That means you can use ChatGPT itself as the coding agent instead of routing every coding task through Codex, and in practice this workflow rarely runs into the same kind of usage ceiling.

WebCodex already solves the environment side very well:

- repository access
- safe file editing
- Git
- shell commands and tests
- real local toolchains
- durable long-running machine jobs

Two model-side problems remain.

First, a single ChatGPT conversation still has a bounded execution window. A large task may need more reasoning and tool calls than one execution window can finish — roughly around 25 minutes in practice.

Second, one conversation is not always the best context for every branch of a complex task. Independent review, verification, research, or alternative implementations often work better in a fresh sub-agent context.

OverGPT fills those two gaps.

## How OverGPT fits with WebCodex

Conceptually, WebCodex is the **development environment** and OverGPT is the **execution control layer** around the ChatGPT side.

```text
                         ChatGPT
                            │
              ┌─────────────┴─────────────┐
              │                           │
              ▼                           ▼
         OverGPT                  WebCodex
      execution control            development tools
              │                           │
      ┌───────┼────────┐                  ▼
      │       │        │              your machine
 continue  delegate  supervise            │
      │       │        │          ┌────────┼────────┐
      │       │        │          │        │        │
      │       │        │        repo      Git    tests/tools
      │       │        │
      │       ▼        │
      │   fresh ChatGPT sub-agents
      │       │
      └───────┴─── use the same MCP environment
```

A WebCodex user can therefore keep the same repository, Git checkout, tests, and local tools. OverGPT adds longer-lived ChatGPT execution and recursive delegation on top of that workflow.

OverGPT is not a replacement for WebCodex. The two projects solve different layers of the same problem.

## Continue beyond one ChatGPT turn

OverGPT keeps server-authoritative timing for a ChatGPT conversation.

While a long session is active, MCP tool results include the remaining execution budget. When the current turn is near its limit, ChatGPT can finish the current atomic step, persist enough state to resume safely, and register a wake-up.

OverGPT then sends a continuation message back to the same conversation.

```text
start
  │
  ▼
work ──► work ──► work
                  │
             yield threshold
                  │
                  ▼
             checkpoint
                  │
                  ▼
           register wake-up
                  │
                  ▼
        same conversation resumes
```

The important part is that continuation is explicit. Timing comes from the server, progress is saved before yielding, and the next turn resumes the existing task.

A long logical task can therefore span multiple ChatGPT turns without pretending that one model invocation can run forever.

## Spawn fresh recursive sub-agents

OverGPT can launch fresh ChatGPT conversations as isolated workers.

Each delegated task gets explicit input, persistent output, task state, and its own browser execution slot. The parent receives a task ID immediately and can keep working while the child runs independently.

```text
                    parent ChatGPT
                          │
                spawn independent work
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
        sub-agent A               sub-agent B
              │                       │
        durable output           durable output
              │                       │
              └───────────┬───────────┘
                          ▼
                    parent consumes
                       results
```

A sub-agent can use the same mechanism again, so delegation can be recursive.

Fresh contexts are useful for work such as independent code review, adversarial verification, parallel research, or exploring alternative implementations without carrying the parent's full conversation history into every branch.

## Durable execution state

OverGPT keeps task handoff and completion state outside any single model turn.

Sub-agent inputs and outputs are persisted in the shared workspace. Background work has explicit states such as `queued`, `running`, `succeeded`, `failed`, `timed_out`, and `cancelled`.

This gives the parent conversation a concrete way to supervise delegated work instead of relying on hidden in-memory orchestration.

WebCodex can continue to own repository execution and long-running developer jobs. OverGPT uses durable state for the model-side lifecycle: continuation, delegation, supervision, and result collection.

## Current implementation

The current implementation targets ChatGPT with MCP access.

Conversation continuation and sub-agent spawning use authenticated ChatGPT Web sessions driven through Playwright. Persistent workspace files provide task handoff and result collection. The MCP server provides timing, task supervision, and the control primitives needed to continue or delegate work.

OverGPT can run with its own workspace tools, but it is designed to complement WebCodex. For software-engineering use, WebCodex provides the richer repository and developer-tool layer while OverGPT focuses on keeping ChatGPT working across turns and splitting independent work into fresh conversations.

---

Operational, deployment, and contributor instructions live in [AGENTS.md](AGENTS.md).
