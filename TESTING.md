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
implementation. They were purged on 2026-10-01 and are not to come back.

## Size

The suite is read far more often than it is run. Keep the test smaller than
the thing it tests: one fixture, one act, one assertion where that is
enough, and a docstring only when the contract is not obvious from the name.
