# GPTOvertime Agent Instructions

This file contains operational and repository-development instructions for agents working on GPTOvertime. The public README intentionally stays focused on the project model and user-facing concepts.

## Determine how this repository is being accessed

Before doing any repository work, first determine whether you are already operating on the canonical workspace through the GPTOvertime/Writer MCP tools.

### When operating through MCP / Writer

If the repository is being read or modified through the connected MCP workspace, **do not run `make sync-down`**.

The MCP workspace is already the active source of truth for this session. Running the desktop synchronization path from inside that workspace can overwrite or conflict with the state that the current agent is actively editing.

Use the structured workspace tools for ordinary repository operations:

- `list_workspace` and `search_workspace_text` for discovery.
- `read_workspace_file` and `read_workspace_range` for reads.
- `replace_workspace_text`, `insert_workspace_text`, `write_workspace_file`, and `apply_workspace_patch` for edits.
- `move_workspace_file` and `delete_workspace_file` for ordinary file mutations.
- `run_workspace_code` for Git, tests, builds, or commands that genuinely require a shell.

Do not attempt to synchronize the repository merely because these instructions mention a remote development workflow.

### When operating from a desktop/local checkout

If the agent is running directly against a separate local checkout, outside the MCP-managed workspace, synchronize before editing:

```bash
make sync-down
```

Remote synchronization is configured only through `.env`. If no `MCP_SYNC_HOST` / `MCP_SYNC_REMOTE_DIR` pair is configured, `make sync-down` is intentionally a no-op and development can continue locally.

When remote sync is configured, `make sync-down` must refuse to overwrite a repository with staged, unstaged, or untracked changes. If that happens, stop before making further changes and report that local work exists while the remote workspace has not yet been reconciled. Do not decide which copy should win without explicit user direction.

When the user explicitly says the remote copy should win, use:

```bash
make sync-down force
```

After a successful desktop sync, inspect Git status and relevant diffs before continuing.

## Environment and bootstrap

Use `.env.example` as the deployment configuration contract.

Machine-specific paths, credentials, UID/GID values, ports, optional mounts, browser mode, and optional remote sync settings belong in `.env`. Stable application policy belongs in `config.py`.

Useful deployment entry points:

```bash
make doctor
make bootstrap
```

`make doctor` validates deployment prerequisites and Compose configuration. `make bootstrap` builds, starts, waits for readiness, and runs the MCP smoke test.

ChatGPT Web automation requires an authenticated browser profile. Follow the Makefile login/debug targets when establishing or repairing that profile.

## Development workflow

Prefer the structured workspace APIs over ad-hoc shell mutations when MCP access is available. They provide bounded reads, exact-match edits, SHA-256 stale-write protection, and atomic patch application.

Use `run_workspace_code(background=true)` for long-running Shell or Python commands. Poll them with `get_workspace_task`; inspect stdout/stderr only when useful for completion details or diagnostics.

After modifying Python source inside a running deployment, `restart_mcp_server` can validate the updated import and restart the container without rebuilding the image. Dependency, Dockerfile, or Compose changes still require the appropriate rebuild/redeploy path.

## ChatGPT sub-agent development

`spawn_chatgpt_subagents` is the public delegation primitive. Preserve every returned task ID and output path, including partial-success batches.

A delegated ChatGPT task communicates through persistent workspace files:

- `input.md` is the authoritative task input.
- `output.md` is the authoritative business result.
- stdout/stderr are execution logs, not result channels.
- completion is recognized through the fixed sentinel protocol.

The persistent authenticated ChatGPT profile is a template. Individual tasks run in isolated task-profile slots so concurrent agents do not write to the same browser profile.

When changing browser automation, preserve the separation between task acceptance, browser-slot capacity, output-file acknowledgement, final completion, and background-task supervision. Those states exist to avoid losing already-started work when later launches fail.

## Long-session development

Long-session timing is server-authoritative.

A conversation calls `start_timer` with its own ChatGPT conversation URL, then passes that URL on ordinary MCP calls. The server appends current timing status. Once `should_yield` becomes true, the agent should complete only the current atomic operation, persist enough state to resume safely, register a wake-up, and end the turn.

Do not replace this with model-side elapsed-time guesses.

`register_wakeup` is a continuation mechanism for an existing conversation. Delegated temporary-chat sub-agents are independent tasks and do not automatically participate in the parent's long-session timer.

## Sync local changes back

This section applies only to the separate desktop/local-checkout workflow.

When local source changes should be propagated to a configured remote workspace:

```bash
make sync-up
```

If remote sync is not configured, the target is a no-op.

Do not add `--delete` to synchronization by default. Git remains the authority for version history and for reviewing exactly what changed.

## Repository hygiene

Do not commit or copy runtime or machine-local state into Git.

Keep secrets, `.env`, authenticated browser profiles, per-task browser profiles, background-task state, caches, temporary files, sync-conflict artifacts, and similar runtime data out of normal source synchronization.

Do not duplicate machine paths, SSH endpoints, ports, credentials, or deployment-specific values in source documentation. Put them in `.env`.

The repository's public naming is **GPTOvertime**. Historical internal identifiers may remain temporarily where changing them would affect compatibility, but new documentation and user-facing names should use GPTOvertime.
