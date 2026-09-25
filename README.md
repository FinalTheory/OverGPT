# GPTOvertime

**Durable execution and recursive sub-agents for ChatGPT over MCP.**

[English](README.md) · [中文](README.zh-CN.md)

GPTOvertime extends ChatGPT's MCP execution model beyond a single bounded turn.

A capable ChatGPT session can already inspect repositories, edit files, run commands, and use arbitrary MCP tools. The remaining constraint is execution lifetime: a sufficiently large task may outlive one model turn, and complex work often benefits from delegating independent reasoning to fresh contexts.

GPTOvertime adds those capabilities as an MCP-side execution layer:

- **Continuation across turns** — checkpoint work near the end of a model execution window and automatically wake the same ChatGPT conversation to continue.
- **Recursive sub-agents** — delegate independent tasks to fresh ChatGPT conversations, persist their outputs, and allow those agents to delegate again.
- **Durable execution state** — keep background task metadata, outputs, and logs outside the lifetime of a single MCP request.

## Why GPTOvertime

The useful unit of work for an agent is often larger than one model turn.

A repository migration, research pass, test-and-fix loop, or multi-agent audit can require many tool calls and may need several independent contexts. Without an external execution layer, the parent conversation has to finish before its execution budget expires, and delegated work is difficult to supervise reliably.

GPTOvertime treats a ChatGPT turn as one slice of a longer logical task.

```text
logical task
    │
    ├── ChatGPT turn
    │      ├── MCP work
    │      ├── durable background tasks
    │      └── spawn sub-agents
    │
    ├── checkpoint
    │
    ├── automatic continuation
    │
    └── next ChatGPT turn
```

The logical task can therefore continue even when any individual model turn must stop.

## Long-running conversations

GPTOvertime maintains a server-authoritative execution timer for a ChatGPT conversation.

While a long session is active, ordinary MCP tool results include the remaining execution budget. When the configured yield threshold is reached, the model can finish its current atomic step, persist enough state to resume safely, and register a wake-up.

GPTOvertime then sends a continuation message back to the same conversation after a short delay.

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

The continuation protocol is explicit: timing comes from the server, progress is persisted before yielding, and the next turn resumes the existing task rather than reconstructing it from an implicit in-memory loop.

## Recursive ChatGPT sub-agents

GPTOvertime can launch fresh ChatGPT conversations as isolated workers.

Each delegated task receives its own persistent input and output files, background task state, and browser execution slot. The parent receives a task identifier immediately and can continue working while the child runs independently.

A child can use the same MCP server and recursively delegate further work when useful.

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
              │
              └───────────┬───────────┘
                          ▼
                    parent consumes
                       results
```

This is useful when work benefits from clean context boundaries: independent code review, adversarial verification, parallel research, alternative designs, or any task where one context should not inherit the parent's entire reasoning history.

Sub-agents use the same mechanism recursively, so delegation forms a bounded execution tree instead of a one-level helper call.

## Durable task execution

Long-running shell or Python work is represented as persistent background tasks instead of long-lived MCP requests.

Task state and logs are written to the shared workspace, with explicit lifecycle states such as `queued`, `running`, `succeeded`, `failed`, `timed_out`, and `cancelled`. Execution remains observable across normal client disconnects and MCP request boundaries.

The same task abstraction is used by ChatGPT sub-agents, so delegated model work and ordinary background computation share one supervision model.

## Where it fits

GPTOvertime is intentionally narrow.

It does not try to replace a repository agent, coding harness, or general-purpose MCP environment. Existing tools can already give ChatGPT access to files, Git, shells, tests, browsers, databases, and external services.

GPTOvertime adds an execution layer around that environment:

```text
                         ChatGPT
                            │
                            │ MCP
                            ▼
                      GPTOvertime
                ┌───────────┼───────────┐
                │           │           │
          continuation   durable     recursive
                         tasks       sub-agents
                │           │           │
                └───────────┴───────────┘
                            │
                            ▼
                workspace / repo / tools
```

The result is a ChatGPT session that can keep working for longer, hand off independent work to clean contexts, and recover results through explicit durable state.

## Design principles

**Durable state over hidden orchestration.** Inputs, outputs, task status, and checkpoints live in the shared workspace whenever practical.

**Explicit lifecycle over best-effort prompting.** Continuation, timeout, cancellation, capacity, and completion are represented as protocol state rather than inferred from chat text.

**Clean delegation boundaries.** Sub-agents start from explicit task inputs and return explicit outputs instead of inheriting an opaque parent context.

**Bounded recursion and concurrency.** Delegation is recursive, but execution remains supervised by finite task and browser capacity.

**MCP-native composition.** GPTOvertime is designed to sit beside existing MCP capabilities rather than absorb every tool into a new agent framework.

## Current implementation

The current implementation targets ChatGPT with MCP access.

Conversation continuation and sub-agent spawning use authenticated ChatGPT Web sessions driven through Playwright. Persistent workspace files provide task handoff and result collection, while the MCP server provides timing, task supervision, workspace operations, and background execution.

The implementation is intentionally separable from the core idea: the durable continuation and delegation model does not depend on a particular repository tool or coding-agent harness.

---

Operational, deployment, and contributor instructions live in [AGENTS.md](AGENTS.md).
