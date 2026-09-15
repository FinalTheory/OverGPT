.PHONY: doctor bootstrap build up down restart apply-config wait logs ps test login-up login-logs login-down sync-up sync-down

ENV_FILE ?= ./.env
COMPOSE = docker compose --env-file $(ENV_FILE)

RSYNC_FLAGS = -av --itemize-changes \
	--exclude='.git/' \
	--exclude='.env' \
	--exclude='chatgpt-profile/' \
	--exclude='.mcp-tasks/' \
	--exclude='.ruff_cache/' \
	--exclude='temp/' \
	--exclude='assets/' \
	--exclude='__pycache__/' \
	--exclude='*.py[cod]' \
	--exclude='*.sync-conflict-*' \
	--exclude='.DS_Store'

doctor:
	@test -f "$(ENV_FILE)" || { \
		echo "Missing $(ENV_FILE). Copy .env.example to .env and configure it first."; \
		exit 1; \
	}
	@command -v docker >/dev/null 2>&1 || { echo "docker is required"; exit 1; }
	@$(COMPOSE) version >/dev/null 2>&1 || { echo "docker compose is required"; exit 1; }
	@set -a; . "$(ENV_FILE)"; set +a; \
	[ -n "$${MCP_WORKSPACE_HOST_PATH:-}" ] || { echo "MCP_WORKSPACE_HOST_PATH must be set."; exit 1; }; \
	[ -d "$${MCP_WORKSPACE_HOST_PATH}" ] || { echo "MCP_WORKSPACE_HOST_PATH does not exist: $$MCP_WORKSPACE_HOST_PATH"; exit 1; }; \
	project_rel="$${MCP_PROJECT_RELATIVE_PATH:-mymcp}"; \
	case "$$project_rel" in ''|/*|..|../*|*/../*|*/..) \
		echo "MCP_PROJECT_RELATIVE_PATH must stay inside MCP_WORKSPACE_HOST_PATH: $$project_rel"; exit 1 ;; \
	esac; \
	[ -d "$${MCP_WORKSPACE_HOST_PATH}/$$project_rel" ] || { \
		echo "Configured project directory does not exist: $${MCP_WORKSPACE_HOST_PATH}/$$project_rel"; \
		exit 1; \
	}; \
	[ -n "$${MCP_DRAFT_COMMIT_PASSWORD:-}" ] || { echo "MCP_DRAFT_COMMIT_PASSWORD must be set."; exit 1; }; \
	[ "$${MCP_DRAFT_COMMIT_PASSWORD}" != "change-this-password" ] || { \
		echo "Replace the example MCP_DRAFT_COMMIT_PASSWORD before bootstrap."; \
		exit 1; \
	}; \
	if [ -n "$${MCP_SYNC_HOST:-}" ] || [ -n "$${MCP_SYNC_REMOTE_DIR:-}" ]; then \
		[ -n "$${MCP_SYNC_HOST:-}" ] && [ -n "$${MCP_SYNC_REMOTE_DIR:-}" ] || { \
			echo "MCP_SYNC_HOST and MCP_SYNC_REMOTE_DIR must either both be set or both be empty."; \
			exit 1; \
		}; \
		command -v rsync >/dev/null 2>&1 || { echo "rsync is required when remote sync is configured"; exit 1; }; \
		command -v ssh >/dev/null 2>&1 || { echo "ssh is required when remote sync is configured"; exit 1; }; \
	fi
	@$(COMPOSE) config >/dev/null
	@echo "Environment looks valid."

bootstrap: doctor
	$(COMPOSE) up -d --build
	$(MAKE) wait ENV_FILE=$(ENV_FILE)
	$(COMPOSE) exec -T mcp python scripts/smoke_test.py

sync-down:
	@set -a; [ ! -f "$(ENV_FILE)" ] || . "$(ENV_FILE)"; set +a; \
	if [ -z "$${MCP_SYNC_HOST:-}" ] && [ -z "$${MCP_SYNC_REMOTE_DIR:-}" ]; then \
		echo "Remote sync is not configured; skipping sync-down."; \
		exit 0; \
	fi; \
	[ -n "$${MCP_SYNC_HOST:-}" ] && [ -n "$${MCP_SYNC_REMOTE_DIR:-}" ] || { \
		echo "MCP_SYNC_HOST and MCP_SYNC_REMOTE_DIR must both be configured."; \
		exit 1; \
	}; \
	if [ -n "$$(git status --porcelain)" ]; then \
		echo "Refusing sync-down: local repository has uncommitted changes."; \
		echo "Resolve or save the local changes before pulling the remote workspace."; \
		exit 1; \
	fi; \
	port="$${MCP_SYNC_PORT:-22}"; \
	rsync $(RSYNC_FLAGS) -e "ssh -p $$port" \
		"$$MCP_SYNC_HOST:$$MCP_SYNC_REMOTE_DIR/" \
		"$(CURDIR)/"

sync-up:
	@set -a; [ ! -f "$(ENV_FILE)" ] || . "$(ENV_FILE)"; set +a; \
	if [ -z "$${MCP_SYNC_HOST:-}" ] && [ -z "$${MCP_SYNC_REMOTE_DIR:-}" ]; then \
		echo "Remote sync is not configured; skipping sync-up."; \
		exit 0; \
	fi; \
	[ -n "$${MCP_SYNC_HOST:-}" ] && [ -n "$${MCP_SYNC_REMOTE_DIR:-}" ] || { \
		echo "MCP_SYNC_HOST and MCP_SYNC_REMOTE_DIR must both be configured."; \
		exit 1; \
	}; \
	port="$${MCP_SYNC_PORT:-22}"; \
	rsync $(RSYNC_FLAGS) -e "ssh -p $$port" \
		"$(CURDIR)/" \
		"$$MCP_SYNC_HOST:$$MCP_SYNC_REMOTE_DIR/"

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart mcp
	$(MAKE) wait ENV_FILE=$(ENV_FILE)

apply-config:
	$(COMPOSE) up -d --force-recreate mcp
	$(MAKE) wait ENV_FILE=$(ENV_FILE)

wait:
	@attempt=0; until $(COMPOSE) exec -T mcp python -c 'import socket; from config import CONFIG; socket.create_connection(("127.0.0.1", CONFIG.port), 1).close()' >/dev/null 2>&1; do \
		attempt=$$((attempt + 1)); \
		if [ $$attempt -ge 30 ]; then echo "MCP server did not become ready"; exit 1; fi; \
		sleep 1; \
	done

logs:
	$(COMPOSE) logs -f --tail=100

ps:
	$(COMPOSE) ps

test: wait
	$(COMPOSE) exec -T mcp python scripts/smoke_test.py

login-up:
	$(COMPOSE) --profile login up -d browser-login

login-logs:
	$(COMPOSE) --profile login logs -f --tail=100 browser-login

login-down:
	$(COMPOSE) --profile login stop browser-login
	$(COMPOSE) --profile login rm -f browser-login
