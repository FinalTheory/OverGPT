# MyMCP Agent Instructions

## Development startup

Before making code or documentation changes, run:

```bash
make sync-down
```

Remote synchronization is configured only through `.env`. If no `MCP_SYNC_HOST` / `MCP_SYNC_REMOTE_DIR` pair is configured, `make sync-down` is intentionally a no-op and development can continue locally.

When remote sync is configured, `make sync-down` must refuse to overwrite a repository with staged, unstaged, or untracked changes. If that happens, stop before making further changes and tell the user that local work exists while the remote workspace has not yet been reconciled. Do not decide which copy should win without explicit user direction.

After a successful sync, inspect `git status` and relevant diffs before continuing development.

Do not duplicate machine paths, SSH endpoints, ports, or other deployment-specific values in agent instructions. Those belong in `.env`; operational behavior belongs in the Makefile.

## Environment and bootstrap

For a new deployment, use `.env.example` as the configuration contract. Machine-specific paths, credentials, UID/GID values, ports, optional mounts, browser mode, and optional remote sync settings belong in `.env`.

Useful entry points:

```bash
make doctor
make bootstrap
```

`make doctor` validates the local deployment prerequisites and Compose configuration. `make bootstrap` builds, starts, waits for readiness, and runs the MCP smoke test. ChatGPT Web automation still requires an authenticated browser profile; use the documented login flow when enabling that feature.

## Sync local changes back to a configured remote workspace

When local source changes should be propagated to the configured remote workspace, run:

```bash
make sync-up
```

If remote sync is not configured, the target is a no-op.

Do not add `--delete` to the sync targets by default. Git remains the authority for version history and for reviewing exactly what changed after synchronization.

## Repository hygiene

Do not copy runtime or machine-local state into Git. Keep secrets, browser profiles, background-task state, temporary files, generated caches, sync-conflict artifacts, and similar runtime data out of normal source synchronization. The concrete exclusion list belongs in the Makefile sync policy.
