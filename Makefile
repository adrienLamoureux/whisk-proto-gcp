.PHONY: help install dev api web test smoke lint clean

PY := backend/.venv/bin/python
PIP := backend/.venv/bin/pip

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install both halves
	python3 -m venv backend/.venv
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -e "backend[dev]"
	cd frontend && npm install

dev: ## Build the frontend, then serve everything from the API on :8080
	cd frontend && npm run build
	cd backend && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080

api: ## Backend only, with reload (pair with `make web`)
	cd backend && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

web: ## Frontend dev server on :5173, proxying to :8080
	cd frontend && npm run dev

test: ## Backend tests + frontend typecheck/build
	cd backend && .venv/bin/python -m pytest -q
	cd frontend && npm run build

smoke: ## Drive six agent turns through the real ADK loop
	cd backend && .venv/bin/python -m scripts.smoke_agent

hosting: ## Preview the Firebase Hosting config locally (needs firebase-tools)
	firebase emulators:start --only hosting

lint:
	cd backend && .venv/bin/ruff check app tests
	cd frontend && npm run lint

clean:
	rm -rf backend/.localdata backend/.pytest_cache frontend/dist
	find backend -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
