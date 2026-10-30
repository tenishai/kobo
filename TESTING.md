# Tests in kobo

Two kinds of test, and the directory says which.

## Contract tests (kept)

`api/tests/` and `ui/src/**/*.test.ts`. A contract test guards something a
person or another product depends on:

- an authentication, authorization, ownership or visibility gate
- the shape of a wire payload another product reads (gojira, cabal, sero,
  GitHub, noto, an assistant over MCP)
- data integrity: a claim that must never run twice, idempotent re-runs,
  migrations, concurrency
- destruction safety and secrets handling (never logged, never echoed)
- the guide's house rules
- a bug that bit production, with the incident named in the docstring

Its first line says which of those it guards. If you cannot name one, it is
not a contract test.

## Layout

`api/tests/` is eleven files, one per kind and seam, plus `fakes.py` (the
fake clients, one per Protocol) and `conftest.py` (the shared fixtures).
A new test goes into the file whose first line it could carry.

- `test_gates.py`: the route gates (owner-or-operator, operator-only,
  the connected-repository guard, visibility 404, not-ready 409,
  unknown-agent 404), each one parametrized over a route table.
- `test_auth.py`: authentication (IAP assertions and the middleware's
  coverage, the https guard, the OAuth 2.1 server, MCP JWTs and refresh
  families, the brokers' token checks).
- `test_access.py`: who may see, change and pair with what (visibility,
  grants and readers, admin groups, persona team gates, the consent model
  between agents, thread privacy, the permission service's leases).
- `test_wire_shapes.py`: payloads the UI, assistants over MCP and other
  rooms read from the API.
- `test_wire_clients.py`: requests the room sends to gojira, cabal, sero,
  GitHub and Slack, and the task envelopes an agent reads.
- `test_wire_bundle.py`: what the room writes into an agent's repository
  (the daedalus bundle, the Dockerfile pin, gojira.yaml, kustomization).
- `test_integrity.py`: never-runs-twice, idempotent re-runs, the
  two-process race, sweeps and heartbeats, locks and single-flight
  caches, the dedupe walls, migrations, writes that stay in their box.
- `test_destroy.py`: destruction safety, step by step and end to end.
- `test_secrets.py`: a secret value is never logged, echoed or written
  where it does not belong; the room's own keys stay the room's.
- `test_guide.py`: the house rules (the guide's words, no em dashes, no
  vendor names, the CI fork guards).
- `test_incidents.py`: production bugs, each named in its docstring,
  kept verbatim.

A gate is tested once, over the route table, never per route. A new route
that carries a gate gets a row in the matching table in `test_gates.py`; a
new `test_<route>_stranger_is_403` is the restatement this layout exists
to prevent. The same goes for the not-ready 409 and the unknown-agent 404.

The 2026-10-29 purge that produced this layout is recorded in
`api/tests/PURGE-2026-10-29.md`.

## Scaffold tests (deleted before merge)

`api/tests/scaffold/` and `ui/src/scaffold/`. A scaffold test is written to
drive a change: it fails, you make it pass, you move on. It runs locally and
on the branch like any other test. It does not merge: CI fails any pull
request that still carries a file in either directory. Promote one to a
contract test only by moving it out and giving it the first line above.

## What is not a test

Exact command argument lists, exact copy or labels, CSS and typography
rules, poll intervals and cache keys, name registries, tests of the fakes
themselves, and near-duplicates of a sibling test. These restate the
implementation. They were purged on 2026-10-01 and again on 2026-10-29 and
are not to come back.

## Size

The suite is read far more often than it is run. Keep the test smaller than
the thing it tests: one fixture, one act, one assertion where that is
enough, and a docstring only when the contract is not obvious from the name.
