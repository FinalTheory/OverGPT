.PHONY: build up down restart wait logs ps test

build:
	docker compose build

up:
	docker compose up -d --build

down:
	docker compose down

restart:
	docker compose restart mcp
	$(MAKE) wait

wait:
	@attempt=0; until docker compose exec -T mcp python -c 'import socket; socket.create_connection(("127.0.0.1", 8765), 1).close()' >/dev/null 2>&1; do \
		attempt=$$((attempt + 1)); \
		if [ $$attempt -ge 30 ]; then echo "MCP server did not become ready"; exit 1; fi; \
		sleep 1; \
	done

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

test: wait
	docker compose exec -T mcp python scripts/smoke_test.py http://127.0.0.1:8765/mcp
