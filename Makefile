.PHONY: dependencies install migrate admin api web market-backfill market-worker check

# .env.development 仅保存本地隔离配置，不提交仓库。
dependencies:
	docker compose --env-file .env.development -f compose.development.yaml up --wait

install:
	uv sync --project backend --locked
	npm --prefix frontend ci

migrate:
	uv run --project backend --env-file .env.development alembic -c backend/alembic.ini upgrade head

admin:
	uv run --project backend --env-file .env.development python -m marketmind.cli create-admin

api:
	uv run --project backend --env-file .env.development uvicorn marketmind.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers

web:
	npm --prefix frontend run dev -- --host 127.0.0.1

market-backfill:
	uv run --project backend --env-file .env.development python -m marketmind.market_worker backfill

market-worker:
	uv run --project backend --env-file .env.development python -m marketmind.market_worker serve


check:
	uv run --project backend ruff check backend
	uv run --project backend ruff format --check backend
	npm --prefix frontend run lint
	npm --prefix frontend run typecheck
	npm --prefix frontend run build
