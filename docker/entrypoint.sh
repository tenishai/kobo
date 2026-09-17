#!/bin/sh
# Container boot: bring the schema to head, then serve.
#
# The deploy path is `kubectl set image` — nothing else ever runs
# migrations, so a new image whose ORM expects a column the database does
# not have yet crashloops on its first query (lived three times). Running
# `alembic upgrade head` here closes that gap structurally: the pod that
# needs the schema is the pod that migrates it. Idempotent — a database
# already at head is a no-op. env.py resolves the URL from the same
# mounted KOBO_CONFIG the app itself uses.
#
# A failed upgrade exits non-zero and the pod restarts with the alembic
# error in its logs — a loud, attributable failure instead of a silent
# UndefinedColumn crashloop mid-request.
set -e

cd /app/api
alembic upgrade head

exec uvicorn --factory kobo_api.main:build_default_app --host 0.0.0.0 --port "${PORT:-8080}"
