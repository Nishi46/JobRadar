# Load local settings (DATABASE_URL, POSTGRES_*) from .env if it exists and
# pass them on to every command. Create it with: cp .env.example .env
-include .env
export

COMPOSE := docker compose -f infra/docker-compose.yml

.PHONY: dev down test lint eval eval-holdout

# Start Postgres, wait for its healthcheck, then apply migrations.
# Starting the API gets added here in M4.
dev:
	$(COMPOSE) up --detach --wait
	cd pipeline && uv run alembic upgrade head

# Stop Postgres. The data volume is kept.
down:
	$(COMPOSE) down

test:
	cd pipeline && uv run pytest

# web/ does not exist until M4; its lint and type check get added then.
lint:
	cd pipeline && uv run ruff check .
	cd pipeline && uv run ruff format --check .
	cd pipeline && uv run mypy

# Stubs exit non-zero so a stub can never be mistaken for a passing eval.
eval:
	@echo "make eval: the eval harness arrives in M3." >&2
	@exit 1

eval-holdout:
	@echo "make eval-holdout: the eval harness arrives in M3." >&2
	@exit 1
