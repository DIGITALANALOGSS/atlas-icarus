.RECIPEPREFIX := >

COMPOSE := docker compose -f compose.yaml
PYTHON ?= python3

.PHONY: help doctor up down test integration smoke verify status logs

help:
>@printf '%s\n' 'Atlas ICARUS developer commands:' '  make doctor  Check local prerequisites and repository state' '  make up      Build, start, and wait for Atlas Control readiness' '  make down    Stop services while preserving PostgreSQL data' '  make test    Run containerized Atlas Control tests' '  make integration  Run isolated PostgreSQL transaction tests' '  make smoke   Run the live intake/evidence/audit workflow' '  make verify  Run integration tests and existing verification' '  make status  Show Compose and Git status' '  make logs    Follow Atlas Control logs'

doctor:
>@$(PYTHON) scripts/doctor.py

up:
>@$(COMPOSE) up -d --build atlas-control
>@$(PYTHON) scripts/smoke_evidence_workflow.py --ready-only

down:
>@$(COMPOSE) down

test:
>@$(COMPOSE) --profile test run --rm --build atlas-control-tests

integration:
>@$(COMPOSE) --profile integration run --rm --build atlas-control-integration-tests

smoke:
>@$(PYTHON) scripts/smoke_evidence_workflow.py

verify:
>@$(MAKE) integration
>@bash scripts/verify.sh

status:
>@$(COMPOSE) ps
>@printf '\nGit status:\n'
>@git status --short
>@printf '\nRecent commits:\n'
>@git log -3 --oneline

logs:
>@$(COMPOSE) logs -f atlas-control
