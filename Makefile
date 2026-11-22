# Load local settings (DATABASE_URL, POSTGRES_*) from .env if it exists and
# pass them on to every command. Create it with: cp .env.example .env
-include .env
export

COMPOSE := docker compose -f infra/docker-compose.yml

.PHONY: dev down seed poll work ingest extract match profile cost test lint eval eval-holdout

# Start Postgres, wait for its healthcheck, then apply migrations.
# Starting the API gets added here in M4.
dev:
	$(COMPOSE) up --detach --wait
	cd pipeline && uv run alembic upgrade head

# Load pipeline/seed/companies.csv into the boards table. Safe to rerun.
seed:
	cd pipeline && uv run python -m jobradar.seed

# Enqueue a poll_board job for every enabled board not polled in the last 6 hours.
# Safe to run twice: at most one active job per board exists (jobs_one_active_poll_idx).
poll:
	cd pipeline && uv run python -m jobradar.poller

# Process every due job (one board at a time, 1 s apart), then exit.
work:
	cd pipeline && uv run python -m jobradar.worker

# One full ingestion cycle. Run hourly from cron to build history (see docs/runbook.md).
ingest: poll work

# Extract PostingFacts for every stale posting (new, edited, or older prompt version),
# through the job queue. Costs money: LIMIT caps how many postings are enqueued and run.
#   make extract LIMIT=25
extract:
	cd pipeline && uv run python -m jobradar.extract_worker $(if $(LIMIT),--limit $(LIMIT))

# Run matching for one profile and print its feed:  make match PROFILE=me
match:
	@test -n "$(PROFILE)" || { echo "usage: make match PROFILE=<name>" >&2; exit 1; }
	cd pipeline && uv run python -m jobradar.match --profile "$(PROFILE)"

# Profile commands, passed straight through. Examples:
#   make profile ARGS="create --name me --resume ~/resume.md --min-pay 150000"
#   make profile ARGS="show --name me"
profile:
	cd pipeline && uv run python -m jobradar.profiles $(ARGS)

# LLM spend for the last 7 days, by purpose / model / prompt version (from llm_runs).
cost:
	cd pipeline && uv run python -m jobradar.llm_runs

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
