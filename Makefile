# Load local settings (DATABASE_URL, POSTGRES_*) from .env if it exists and
# pass them on to every command. Create it with: cp .env.example .env
-include .env
export

COMPOSE := docker compose -f infra/docker-compose.yml

.PHONY: dev down seed poll work ingest test lint eval eval-holdout

# Start Postgres, wait for its healthcheck, then apply migrations.
# Starting the API gets added here in M4.
dev:
	$(COMPOSE) up --detach --wait
	cd pipeline && uv run alembic upgrade head

# Load pipeline/seed/companies.csv into the boards table. Safe to rerun.
seed:
	cd pipeline && uv run python -m jobradar.seed

# Enqueue a poll_board job for every enabled board not polled in the last 6 hours.
# Safe to run twice: at most one active job per board exists (jobs_one_active_per_board_idx).
poll:
	cd pipeline && uv run python -m jobradar.poller

# Process every due job (one board at a time, 1 s apart), then exit.
work:
	cd pipeline && uv run python -m jobradar.worker

# One full ingestion cycle. Run hourly from cron to build history (see docs/runbook.md).
ingest: poll work

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
