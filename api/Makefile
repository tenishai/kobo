.PHONY: install test lint check run bootstrap

venv/bin/pip:
	python3.12 -m venv venv

install: venv/bin/pip
	venv/bin/pip install -e ".[dev]"
	# Optional: the sibling daedalus checkout, for the real-daedalus bundle-load
	# test in tests/test_wire_bundle.py (skipped via pytest.importorskip if absent).
	test -d ../../daedalus && venv/bin/pip install -e ../../daedalus || true

test:
	venv/bin/python -m pytest -q

lint:
	venv/bin/ruff check kobo_api tests

check: lint test

run:
	venv/bin/uvicorn --factory kobo_api.main:build_default_app --port 8100

bootstrap:
	venv/bin/python -m kobo_api.bootstrap

# A local room for UI work: real routes, fake platforms, seeded agents (:8100).
lab:
	.venv/bin/python scripts/ui_lab.py
