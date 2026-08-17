# Destruction completeness — the access step learns to die (design)

Approved 2026-08-17 (evening), after live acceptance A found the one
orphan class the destruction arc's reviews could not: the room's own
AccessStep artifacts survive destruction. The destruction spec's
inventory was built from platform-side discovery and never listed what
kobo itself creates during provisioning.

## What the access step creates (the mirror to tear down)

Per agent, `AccessStep` (kobo_api/steps.py) creates:

1. GSA `agent-{name}@{project}` (`cloud.ensure_gsa`),
2. a workload-identity binding on that GSA's own policy (dies with the
   GSA),
3. membership of that GSA in the model-access group
   (`cloud.add_to_group(email, config.model_group)`).

Live evidence: the destroyed first production agent left its
`agent-{name}@` GSA alive and still in the model group.

## The fix

### CloudClient teardown verbs

`CloudClient` (kobo_api/clients.py) gains two check-then-act verbs,
mirroring the ensure-side derivations exactly (same name composition,
same group value from config — never re-derived differently):

- `remove_from_group(gsa_email: str, group: str) -> bool` — True when
  a membership was removed, False when it was already absent. Real
  failures (permission, quota, network) raise.
- `delete_gsa(email: str) -> bool` — True when deleted, False when
  already gone. Real failures raise. The workload-identity binding
  needs no separate verb: it lives on the GSA's own policy.

Not-found recognition is grounded in the real client's error surface
(the same discipline the platform teardown verbs used), never a
guessed string.

### The destroy-access step

`DESTROY_STEPS` gains `destroy-access` between `destroy-platform` and
`destroy-slack` (identity dies right after the workload that used it):

| tolerance | behavior |
|---|---|
| group membership absent | proceed |
| GSA absent | done |
| real failure | step fails with a plain reason; Destroy again resumes |

Order inside the step: remove the group membership first, then delete
the GSA (a deleted GSA's ghost membership would be unremovable by
email).

### Spec ripple

The destruction spec's "What dies" inventory gains the access
artifacts; its step table gains the row. The one already-destroyed
agent's orphan is cleaned up by hand (its room row is gone, so no
ledger can reach it) once the operator confirms.

## Testing

- Verb tests: absent-membership and absent-GSA no-ops, real-failure
  propagation, argv/derivation parity with the ensure side.
- Step matrix: fresh destroy runs access teardown; resume-after-access
  -failure retries access and skips platform; the four existing steps'
  matrices untouched.
- Live acceptance: destroy a scratch agent and verify by direct
  inspection that `agent-{name}@` is gone and the model group no
  longer lists it (the check acceptance A could not pass).

## Non-goals

- Tearing down shared identity (the model group itself, pools,
  cluster resources).
- Backfilling destroy-access rows for agents destroyed before this
  fix (one known orphan, handled by hand).
