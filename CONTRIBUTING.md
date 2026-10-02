# Contributing to kobo

Issues and pull requests are welcome.

## Set up

- API (Python 3.12): `make -C api install`, then copy `api/config.example.yaml` to
  `api/config.local.yaml` and fill it in. Run it with `make -C api run` (serves on `:8100`).
- UI (Node): `make -C ui install`, then `make -C ui dev` (serves on `:5173`, proxying to the API).

## Run the tests

- API: `make -C api check` (ruff lint plus pytest)
- UI: `make -C ui check` (svelte-check plus vitest)

[TESTING.md](TESTING.md) says what earns a place in the suite. Tests under `api/tests/scaffold/`
and `ui/src/scaffold/` are for driving a change and must not be part of a pull request; CI
fails any PR that carries one.

## Open a pull request

Branch from `main`, keep the change focused, add or update tests for behavior you change, and
open a PR describing what changed and why. Commit messages follow the conventional style used in
the history (`feat(scope): ...`, `fix(scope): ...`).

Report security problems as described in [SECURITY.md](SECURITY.md), not in a public issue.
