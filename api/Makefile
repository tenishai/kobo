.PHONY: install test lint check run

venv/bin/pip:
	python3.12 -m venv venv

install: venv/bin/pip
	venv/bin/pip install -e ".[dev]"

test:
	venv/bin/python -m pytest -q

lint:
	venv/bin/ruff check cortex_api tests

check: lint test

run:
	venv/bin/uvicorn --factory cortex_api.main:build_default_app --port 8100
