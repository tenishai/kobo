---
card: arch-card:kobo-provisioning
updated: 2026-10-06
anchor: api/kobo_api/provisioner.py sha=2c576e43a245aff3eee2c26ac15fc14b8491187f
anchor: api/kobo_api/steps.py sha=cf59f4920ce61445ccb72d5450ff9948817eb1a2
---
# provisioning — the Create → running-agent step engine

WHAT / SEAM: `Step` Protocol (provisioner.py: `name`, `label`, `run(ctx: Ctx) -> str`); `Ctx(agent, session, config, clients, workdir)`. Engine: `run_pipeline` (daemon thread + `_Heartbeat` ticker), `_run_pipeline_body`, `_claim_step`, `reset_failed_steps`, `sweep_interrupted`, `pipeline_for(agent)`, `STEP_LABELS`. Steps live in steps.py; `StepSkipped` marks a best-effort step SKIPPED; `StepWaiting` parks a connected agent on its setup PR (status WAITING, resumed by `sweep_waiting`). Since 2026-10-01 the process has a ROLE (roles.py, `KOBO_ROLE`): `all` (default, every test) launches pipelines inline from the routes; `api` only leaves the row PENDING with no heartbeat; `worker` runs `queue_loop` (`claim_queued`: one conditional UPDATE marks the first heartbeat, so two workers never run one agent; `queue_tick` launches up to 4), the sweeps and the nightly learnings pass, and `drain_pipelines` waits up to 25 minutes on shutdown so a roll never fails a build.
OPS / FLOW:
- composed `PIPELINE`: repo → avatar → onboard → access → wire → build → deploy → directory → hello.
- connected `CONNECTED_PIPELINE`: connect → avatar → onboard → access → wire(`ConnectedWireStep`, proposes a PR on branch `kobo/wire`) → build → deploy. No directory/hello: a connected agent has no switchboard identity.
- repo: compose bundle (composer.py) + create repo. onboard: gojira `create_app`, wait active. access: GSA `agent-<name>`, workload identity, `config.model_group` membership. wire: KSA + techne PVC manifests, register in kustomization.yaml, edit gojira.yaml, deliver colleague pass BEFORE push, reprovision, wait active. build: watch GH Actions run for the CURRENT head sha. deploy: rollout. directory: cabal graduate (a no-op when the switchboard is off). hello: first contact via switchboard relay, or the direct path (`clients.agent.hello`) when the switchboard is off, then earns the capability line.
- Each step's row (`AgentStep`, unique (agent_id, name)) is persisted before the next runs. Route polls rows for the UI checklist; the request returns immediately.
- On READY (composed only): `sync_dispatch_pairs_best_effort` + `refresh_colleague_rosters` (provisioner.py ~l.380-410), both fail-soft.
INVARIANTS:
- Order is load-bearing: each step reads state an earlier one recorded on `Agent` (`repo_full_name`, `gojira_app_id`, ...). directory must precede hello because agents compose LOCKED (only switchboard-signed calls accepted).
- `_claim_step` flips PENDING→RUNNING with one conditional UPDATE and checks rowcount. WHY: two replicas (or a retry racing a live run) must never execute the same step twice. Plain SQL, valid on SQLite and Postgres; do not replace with an in-process lock.
- A running pipeline beats `Agent.run_heartbeat_at` every 10s; `sweep_interrupted` fails only rows silent > 60s (`RUN_STALE_AFTER_S`), at boot and every 60s in every worker. In queue mode (`queue_mode=True`, any role but `all`) a never-started row is QUEUED, not dead, and is left alone. WHY: rolling updates start a new pod while the old one still provisions; failing all in-flight rows would kill live runs.
- Retry = `reset_failed_steps`: FAILED → PENDING, DONE/SKIPPED are skipped on re-run. If build or deploy failed (composed), wire and build are reset too (`REWIRE_AFTER_FAILED`/`REWIRE_RESETS`) so the rebuild is watched.
- WireStep: writing a manifest and registering it in kustomization.yaml are one act (gojira's generator preserves listed entries, it does not discover files; P0 lost a rollout to this). WireStep never completes without confirming gojira `active`, including the no-change resume path.
- repo/onboard steps re-check the outside world and refuse to adopt a same-named repo/app the row did not record (steps.py module docstring).
- BuildStep polls `runs_for_commit(head)` re-resolved each poll (gojira's setup PR auto-merges mid-provision), with one auto-rerun per watched sha for any non-success conclusion.
- AvatarStep never fails the pipeline (raises `StepSkipped`).
- Engine-level failures (workdir, commit, missing row) mark the agent FAILED directly; they have no step boundary to resume from.
CONSUMERS: api/kobo_api/main.py (`POST /api/agents`, `/api/agents/connect`, `/api/agents/{name}/retry` call `run_pipeline(..., steps=pipeline_for(agent))` in role `all`, and only set PENDING otherwise; `build_default_app` calls `sweep_interrupted`, `_room_lifespan` runs `interrupted_sweep_loop`); api/kobo_api/mcp/tools/write.py (`kobo_create_agent` calls the create route function directly); tests/test_engine.py (toy steps, two-real-process claim test), tests/test_steps.py, tests/test_api_agents.py (end-to-end with fakes), tests/test_connected_pipeline.py.
API: `run_pipeline(session_factory, agent_id, *, config, clients, steps) -> Thread`; `reset_failed_steps(session, agent_id)`; `sweep_interrupted(session_factory, *, now=None, queue_mode=False) -> int`; `claim_queued(session_factory, *, limit, now) -> list[int]`; `queue_tick(...) -> int`; `drain_pipelines(app, *, timeout_s) -> int`; `pipeline_for(agent)`; `STEP_LABELS` (UI checklist vocabulary, union of both lists).
