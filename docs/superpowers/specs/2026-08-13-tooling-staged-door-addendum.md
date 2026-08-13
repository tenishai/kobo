# Tooling — the staged door (addendum; enforcement contract follows separately)

Operator decision 2026-08-13: ship the per-agent Tooling surface now, staged —
files are stored, validated, and delivered to the bundle, and the UI states
plainly that enforcement has not arrived yet. The runtime enforcement
contract (hooks the model cannot skip) remains its own design, next in the
spec queue; this door is deliberately built so that contract slots in
without rework.

## What Tooling is (and is not)

Tooling files are the agent's LAW-to-be: orchestrators and guardrails the
runtime will always execute around a reply once the enforcement contract
lands. They are not memories (Memory learns; Tooling governs) and not
catalog skills (skills inform the model; tools bind it). Per-agent for now —
files live in the agent's own bundle; the team-level shared-tools catalog
from the 2026-08-03 constraints is a later layer on the same validator.

## Storage and delivery

- Files land at `bundle/tools/<filename>` in the agent repo via the same
  commit + delivery-sha machinery knowledge/intake use. The runtime's bundle
  loader ignores unknown top-level dirs, so a stored tool is inert until the
  enforcement contract ships — that inertness is the honesty guarantee.
- Uploads re-pin the runtime image in the same commit (same helper intake
  uses) so the eventual enforcement flip never meets a stale runtime.

## Validation (mechanical, before any commit)

- Extensions: `.py`, `.go`. Filename slug rules as elsewhere
  (`^[a-z][a-z0-9_-]{0,63}\.(py|go)$` — underscores allowed; code names use
  them).
- Size cap 524288 bytes (512KB — the reference orchestrator is ~334KB).
- UTF-8 decodable, non-empty.
- `.py`: must `ast.parse` — a syntax error is a 422 with the parse error's
  line number in plain language.
- `.go`: no toolchain in the room; syntax validation deferred to the
  enforcement contract's arrival (copy says so).
- The enforcement-contract entrypoint check from the 2026-08-03 constraints
  joins this validator when that contract exists; the validator function is
  the single place it will be added.

## Two doors, one validator

1. **Upload** — file picker (`.py`/`.go`) + editable filename, same form
   family as Teach-from-a-file.
2. **Repo import** — `owner/repo` + folder path; kobo clones via its GitHub
   App identity, validates EVERY matching file in the folder, and commits
   the ones that pass in one delivery (per-file results reported; one bad
   file does not sink the rest). Import is explicit — re-import is a button
   press, never a webhook (locked 2026-08-03 constraint).

## API

- `GET /api/agents/{name}/tooling` → `{"files": [{"name", "content_bytes",
  "delivered": bool}]}` — delivered derives from the same ancestor check
  intake uses; there is NO "enforced" state to claim.
- `POST /api/agents/{name}/tooling` body `{"filename", "content"}` → `{"sha"}`.
- `POST /api/agents/{name}/tooling/import` body `{"repo", "path"}` →
  `{"sha" | null, "imported": [names], "rejected": [{"name", "reason"}]}`
  (sha null when nothing passed).
- `DELETE /api/agents/{name}/tooling/{filename}` → `{"sha"}`.
- ClientError → the standard 502 body; validation → 422 plain language.

## UI

A **Tooling** panel between Skills and Slack: heading copy "The rules this
agent will be held to."; a standing notice — verbatim: **"Stored and
delivered, not yet enforced — enforcement arrives with the runtime's next
upgrade."**; upload + repo-import forms; file list with delivery chip and a
fixed `stored` badge (never a fake "active"). Remove works at any time
(these are files, not memories — deleting removes the file, full stop).

## Non-goals (this door)

- Any runtime execution of stored tools (the enforcement contract's job).
- Team-level shared tools catalog.
- `.go` syntax validation.
- Versioning/history UI beyond git's own.

## Tests

Validator matrix (extension/size/UTF-8/ast errors with line numbers),
import partial-pass semantics (one sha, rejected list, nothing-passed null),
delivered-derivation reuse, route shapes + copy verbatim, UI factories.
