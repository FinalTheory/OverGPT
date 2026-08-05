.PHONY: build up down restart logs ps test

build:
	docker compose build

up:
	docker compose up -d --build

down:
	docker compose down

restart:
	docker compose restart mcp

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

test:
	docker compose exec -T mcp python scripts/smoke_test.py http://127.0.0.1:8765/mcp
