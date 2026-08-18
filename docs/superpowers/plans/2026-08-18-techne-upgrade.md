# Techne 0.14 Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. NOTE: this plan is zero-code and fully operational — every task is controller-driven command execution with evidence gates; no implementer subagents are required, and the "review" for each task is the recorded evidence in the ledger.

**Goal:** Both live agents run techne 0.14.0 with their memories intact, migrated in place, with backups banked and retrieval proven.

**Architecture:** Rebuild the runtime image with the SAME daedalus commit that built the live runtime (`0b2b44c`, branch feat/enforcement, verified checked out) and the techne build context moved to the `v0.14.0` tag. Deliver through the existing ripple: org `runtime_image` bump via App token, per-agent Dockerfile FROM re-pin committed via the composer helper, CI rebuilds and rolls. Migration is automatic on pod boot (schema 18→24, guarded, backed up).

**Tech Stack:** docker buildx (linux/amd64), Artifact Registry, the room's App-token httpx flow, kubectl (read + exec verification only).

## Global Constraints

- The daedalus tree contributes byte-identical code to the live runtime: branch feat/enforcement @ 0b2b44c, clean tree (delete the stray untracked uv.lock before building). If HEAD differs from 0b2b44c, STOP.
- Techne builds from the `v0.14.0` TAG exactly, never main.
- Image tag: `daedalus-runtime:0b2b44c-t0140` (traceable to both inputs). Push the pinned tag only; `:latest` untouched.
- No client/vendor coordinates in any tracked file; operational commands live here in the session and the untracked ledger only.
- Order: headroom check BEFORE the roll; org bump and re-pins AFTER the image is pushed and pull-verified.
- Rollback: re-pin agents to the previous runtime tag; the pre-migration backup on each volume covers the database.

---

### Task 1: Build and push the runtime image

- [ ] **Step 1**: In the techne repo: `git checkout v0.14.0` (leave main untouched for later; note the detached state in the ledger for restoration afterward).
- [ ] **Step 2**: In the daedalus repo: confirm `git rev-parse --short=7 HEAD` = 0b2b44c, `git status --porcelain` empty after removing the stray uv.lock.
- [ ] **Step 3**: Build for amd64 with the named context, tagged for the registry:

```bash
docker buildx build --platform linux/amd64 \
  -f docker/Dockerfile \
  --build-context techne=../techne \
  -t <registry>/daedalus-runtime:0b2b44c-t0140 \
  --push .
```

(`<registry>` = the same Artifact Registry path the current runtime tag lives under; read it from the live agent Dockerfiles rather than retyping.)

- [ ] **Step 4**: Evidence gate: pull-verify the pushed digest exists (`gcloud artifacts docker images describe ...:0b2b44c-t0140`), and confirm the embedded techne version by running the image locally if runnable, else defer to the in-pod check in Task 2. Restore the techne repo to main.

### Task 2: Deliver, migrate, verify

- [ ] **Step 1 — headroom**: for each agent (network, ward): read the techne db size and volume free space in-pod (`du` on /techne, `df` on the mount). The automatic backup needs roughly one database's worth of space. Record numbers.
- [ ] **Step 2 — org bump**: App-token PUT updating `runtime_image` to the new tag in the org config (the add-operators script pattern; GET, edit the one line, PUT with sha). Record old and new values.
- [ ] **Step 3 — re-pin**: for each agent repo, one commit bumping the Dockerfile FROM to the new tag via the composer helper (script with the room's App token, same internals the intake re-pin uses). CI builds and rolls each agent.
- [ ] **Step 4 — watch the boot**: for each agent pod after roll: capture the techne migration log lines (guarded chain 19→24, backfill counts, backup announcement) from pod logs/stderr.
- [ ] **Step 5 — verify**: in-pod `importlib.metadata.version("techne")` == 0.14.0 for both agents; in-pod direct `techne.core.search.search()` against the live db for a known pre-upgrade memory (network carries the oldest corpus) returns sane hits; one room-chat smoke question to each agent answered normally.
- [ ] **Step 6 — ledger close**: record image digest, org config commit, both re-pin commits, migration evidence, verification results, and the rollback coordinates (previous tag, backup filenames).
