.PHONY: install test lint check run bootstrap

venv/bin/pip:
	python3.12 -m venv venv

install: venv/bin/pip
	venv/bin/pip install -e ".[dev]"
	# Optional: the sibling daedalus checkout, for the real-daedalus bundle-load
	# test in tests/test_composer.py (skipped via pytest.importorskip if absent).
	test -d ../../daedalus && venv/bin/pip install -e ../../daedalus || true

test:
	venv/bin/python -m pytest -q

lint:
	venv/bin/ruff check pnyx_api tests

check: lint test

run:
	venv/bin/uvicorn --factory pnyx_api.main:build_default_app --port 8100

bootstrap:
	venv/bin/python -m pnyx_api.bootstrap
