# MyMCP Agent Instructions

## Development startup sync

Before making any code or documentation changes, first reconcile the local Git checkout with the remote MCP workspace by running:

```bash
make sync-down
```

`make sync-down` owns the sync configuration, including the remote host, SSH port, source/destination paths, and exclusions. Do not duplicate those details in agent instructions.

The target must refuse to sync when `git status --porcelain` reports staged, unstaged, or untracked changes. If that happens, stop before making further changes and tell the user that the local repository has uncommitted work while the remote workspace has not yet been pulled. Do not decide which copy should win; let the user resolve or explicitly authorize how to reconcile them.

If the sync succeeds, inspect `git status` and the diff before continuing development.

Do not add `--delete` to the sync targets by default. Git remains the authority for version history and for reviewing exactly what changed after synchronization.

## Sync local changes back to the remote workspace

When the local source changes should be propagated to the remote workspace, run:

```bash
make sync-up
```

`make sync-up` uses the same centrally defined sync policy with the transfer direction reversed.

## Repository hygiene

Do not copy runtime or machine-local state into Git. Keep secrets, browser profiles, background-task state, temporary files, generated caches, sync-conflict artifacts, and similar runtime data out of normal source synchronization. The concrete exclusion list belongs in the Makefile sync configuration.
