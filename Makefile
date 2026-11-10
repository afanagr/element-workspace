SHELL := /bin/sh
COMPOSE := docker compose

.PHONY: help up down restart logs ps build reset seed-local pull-model backend-shell worker-shell psql

help:
	@echo "Element Workspace"
	@echo "  make up          Build and start the whole stack (http://localhost:3000)"
	@echo "  make logs        Tail logs of all services"
	@echo "  make down        Stop the stack (keeps volumes)"
	@echo "  make reset       Stop and remove ALL data (postgres, qdrant, workspace)"
	@echo "  make build       Rebuild images"
	@echo "  make ps          Show container status"
	@echo "  make pull-model  Pull OLLAMA_MODEL into the local-llm profile"
	@echo "  make backend-shell / worker-shell / psql   Debug shells"

up:
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example - add your API key and re-run make up")
	$(COMPOSE) up -d --build
	@echo "Open http://localhost:$${APP_PORT:-3000}"

down:
	$(COMPOSE) --profile local-llm down

restart:
	$(COMPOSE) restart

logs:
	$(COMPOSE) logs -f --tail=200

ps:
	$(COMPOSE) ps

build:
	$(COMPOSE) build

reset:
	@echo "This deletes postgres, qdrant, redis and the workspace volume."
	$(COMPOSE) --profile local-llm down -v --remove-orphans

pull-model:
	$(COMPOSE) --profile local-llm up -d ollama
	$(COMPOSE) exec ollama ollama pull $${OLLAMA_MODEL:-llama3.2}

backend-shell:
	$(COMPOSE) exec backend bash

worker-shell:
	$(COMPOSE) exec worker bash

psql:
	$(COMPOSE) exec postgres psql -U workspace -d workspace
