.PHONY: up down nuke provision logs check \
       incident-bad-deploy incident-pool incident-flags incident-leak resolve demo

COMPOSE := docker compose
SIGNOZ_COMPOSE := deploy/signoz/docker-compose.yaml
SIGNOZ_NETWORK := signoz

# ──────────────────── Lifecycle ────────────────────

setup-signoz:
	@echo "📡 Setting up SigNoz self-host..."
	@mkdir -p deploy/signoz
	@if [ ! -f $(SIGNOZ_COMPOSE) ]; then \
		echo "Downloading SigNoz docker-compose..."; \
		curl -sL https://github.com/SigNoz/signoz/releases/latest/download/docker-compose.yaml -o $(SIGNOZ_COMPOSE); \
	fi

up: setup-signoz
	@echo "🚀 Starting SigNoz..."
	$(COMPOSE) -f $(SIGNOZ_COMPOSE) -p signoz up -d
	@echo "⏳ Waiting for SigNoz to be ready..."
	@sleep 15
	@echo "🚀 Starting Agent K stack..."
	$(COMPOSE) up -d --build
	@echo "✅ Stack is up! SigNoz UI → http://localhost:8080 | Agent K → http://localhost:9000"

down:
	$(COMPOSE) down
	$(COMPOSE) -f $(SIGNOZ_COMPOSE) -p signoz down

nuke:
	$(COMPOSE) down -v --remove-orphans
	$(COMPOSE) -f $(SIGNOZ_COMPOSE) -p signoz down -v --remove-orphans

logs:
	$(COMPOSE) logs -f

logs-%:
	$(COMPOSE) logs -f $*

# ──────────────────── Provisioning ────────────────────

provision:
	@echo "📊 Provisioning dashboards & alert rules..."
	$(COMPOSE) exec agent python -m provisioning.provision

# ──────────────────── Chaos / Incidents ────────────────────

incident-bad-deploy:
	@echo "💥 Triggering bad-deploy scenario..."
	$(COMPOSE) exec loadgen python -m chaos bad-deploy

incident-pool:
	@echo "💥 Triggering pool-exhaustion scenario..."
	$(COMPOSE) exec loadgen python -m chaos pool-exhaustion

incident-flags:
	@echo "💥 Triggering flag-combo scenario..."
	$(COMPOSE) exec loadgen python -m chaos flag-combo

incident-leak:
	@echo "💥 Triggering secret-leak scenario..."
	$(COMPOSE) exec loadgen python -m chaos secret-leak

resolve:
	@echo "✅ Resolving all chaos scenarios..."
	$(COMPOSE) exec loadgen python -m chaos resolve

# ──────────────────── Demo ────────────────────

demo: up
	@echo "⏳ Waiting for telemetry pipeline to warm up (30s)..."
	@sleep 30
	@echo "📊 Provisioning..."
	@$(MAKE) provision
	@echo "⏳ Letting baseline traffic flow (60s)..."
	@sleep 60
	@echo "💥 Triggering bad-deploy incident..."
	@$(MAKE) incident-bad-deploy
	@echo "🕶️  Agent K is on the case. Watch Slack for the RCA!"

# ──────────────────── Code Quality ────────────────────

check:
	@echo "🔍 Running ruff..."
	ruff check .
	ruff format --check .
	@echo "🧪 Running tests..."
	pytest -x -q
