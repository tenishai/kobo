# Techne 0.14 upgrade — the memory grows up in place (design)

Approved in brainstorm 2026-08-18, first arc of the colleagues
decomposition (order ruling: this upgrade, then persona metadata, then
colleagues). Discovery grounded the whole path in the techne repo's own
code: a 0.12.0 database opens directly under 0.14.0, every migration
step (schema 18 to 24, including the timestamp backfill) runs
automatically inside the normal connect chain under a cross-process
lock, and an automatic pre-migration backup with bounded retention
lands on the same volume first. The runtime's call surface into techne
(search, store, delete, init, connect) is unchanged between the tags.

## What ships

1. **The runtime image**: the local techne checkout moves to the
   `v0.14.0` tag exactly (not main, which sits 16 commits past it);
   the runtime image builds with the existing named-build-context
   command from the runtime repo's current main; the pinned tag is
   pushed. Nothing about the runtime's own code changes.
2. **Delivery**: `runtime_image` bumps in the org config through the
   App-token flow, and each agent's Dockerfile FROM re-pins through the
   existing composer mechanism so CI rebuilds and rolls them — the
   same ripple every runtime bump rides.
3. **Migration, watched, per agent**: before the roll, check each
   agent volume's headroom for the automatic backup. After each pod
   boots: read the migration and backfill log lines, verify the
   installed techne version from inside the pod, and run one
   memory-recall smoke test against an agent that carries real
   memories to prove retrieval survived the timestamp rewrite.
4. **Rollback posture**: the previous runtime tag stays in the
   registry (a re-pin restores the old code) and the pre-migration
   backup on the volume covers the database itself.

## Acceptance

- Both live agents run techne 0.14.0 (verified in-pod), answer
  normally, and recall pre-upgrade memories correctly.
- The migration logs show the guarded chain completing and the backup
  landing; no manual intervention anywhere.
- The codex note from discovery does not apply (agents do not use the
  codex index database); recorded here so nobody chases it.

## Non-goals

- No reflection-queue consumers (that is the teach-from-chat arc's
  rail; this upgrade only makes it available).
- No runtime code changes and no techne-main features beyond the tag.
- No new health/liveness surfacing in the room's UI (available later
  if wanted).
