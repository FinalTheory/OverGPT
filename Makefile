.PHONY: build up down restart apply-config wait logs ps test login-up login-logs login-down sync-up sync-down

SYNC_HOST ?= god@finaltheory.me
SYNC_PORT ?= 10023
SYNC_REMOTE_DIR ?= /home/god/Dropbox/workspace/mymcp
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

sync-down:
	@if [ -n "$$(git status --porcelain)" ]; then \
		echo "Refusing sync-down: local repository has uncommitted changes."; \
		echo "Resolve or save the local changes before pulling the remote workspace."; \
		exit 1; \
	fi
	rsync $(RSYNC_FLAGS) -e 'ssh -p $(SYNC_PORT)' \
		$(SYNC_HOST):$(SYNC_REMOTE_DIR)/ \
		$(CURDIR)/

sync-up:
	rsync $(RSYNC_FLAGS) -e 'ssh -p $(SYNC_PORT)' \
		$(CURDIR)/ \
		$(SYNC_HOST):$(SYNC_REMOTE_DIR)/

build:
	docker compose build

up:
	docker compose up -d --build

down:
	docker compose down

restart:
	docker compose restart mcp
	$(MAKE) wait

apply-config:
	docker compose up -d --force-recreate mcp
	$(MAKE) wait

wait:
	@attempt=0; until docker compose exec -T mcp python -c 'import socket; from config import CONFIG; socket.create_connection(("127.0.0.1", CONFIG.port), 1).close()' >/dev/null 2>&1; do \
		attempt=$$((attempt + 1)); \
		if [ $$attempt -ge 30 ]; then echo "MCP server did not become ready"; exit 1; fi; \
		sleep 1; \
	done

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

test: wait
	docker compose exec -T mcp python scripts/smoke_test.py

login-up:
# ssh -N -L 6080:127.0.0.1:6080 -p 10023 god@170.9.29.89
	docker compose --profile login up -d browser-login

login-logs:
	docker compose --profile login logs -f --tail=100 browser-login

login-down:
	docker compose --profile login stop browser-login
	docker compose --profile login rm -f browser-login
