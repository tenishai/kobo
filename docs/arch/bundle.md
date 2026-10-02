---
card: arch-card:kobo-bundle
updated: 2026-09-28
anchor: api/kobo_api/composer.py sha=3098f92681565fa9d0ea61a3b4745f8ee42287f5
anchor: api/kobo_api/vendor/bundle.schema.json sha=51b6e2ef218fb98df539b39783b3628ecc0ce0db
---
# bundle — the daedalus agent bundle kobo writes into each agent repo

WHAT / SEAM: api/kobo_api/composer.py. `BundleSpec` (dataclass) → `compose(spec, dest)` writes the tree; `validate_agent_yaml(bundle_dir)` checks it against api/kobo_api/vendor/bundle.schema.json, a verbatim copy of daedalus `docs/bundle.schema.json` (never hand-edit; re-copy from daedalus). Called by RepoStep (steps.py) for composed agents only; connected agents bring their own repo.
OPS / FLOW: `compose` writes `Dockerfile` (single `FROM {runtime_image}` + `COPY bundle/ /bundle`), `bundle/agent.yaml`, `bundle/agent-card.json`, `bundle/persona.md`, `bundle/knowledge/self.md`, `bundle/knowledge/colleagues.md`, `bundle/skills/<id>/**`, and `litestream.yml` + `entrypoint.sh` only when `memory_replica_bucket` is set. WireStep then adds `k8s/base/*` and edits `gojira.yaml` (steps.py); later lanes append `bundle/intake/*` (teach archive), tooling, and Slack transports.
agent.yaml shape (`_agent_yaml_data`): `model.id: ${AGENT_MODEL:-<id>}` (env override; the placeholder is substituted before schema validation), `max_tokens: 8192`, one a2a transport on :8080, `tools`, `memory: {personal: techne, home: /techne, revision: operator}`, `checkpoints: sqlite`, `approvals: {ceiling: {write: false}, gates, escalation: {route: cli}}`.
Switchboard-dependent parts (all gated on `spec.cabal_url` presence):
- a2a `auth`: `jwks_url = <cabal_url>/.well-known/jwks.json`, `issuer "cabal-core"`, `audience = <agent name>`. The agent accepts only switchboard-signed calls.
- `tools.provider: colleagues`, allow `colleagues.{list_colleagues,ask_colleague,remember}`, `token_env: CABAL_COLLEAGUE_TOKEN`; plus five explicit `github.` names via `broker_url` (`open_pr`, the one write, and the reads `list_prs`, `get_pr`, `list_releases`, `read_file`; never a `github.*` glob) and a `liminal` block via `liminal_url` when set. Team-guide archetype gets the colleague trio only.
- `approvals.gates: []` when cabal is on; otherwise one `match "*" → plan_approval` gate.
INVARIANTS:
- Unconfigured cabal ⇒ byte-identical output to the pre-switchboard shape. WHY: existing repos must not churn on recompose.
- Custom dumper forces flow style on the transport, tools and gates entries so each renders on ONE line; the `update_agent_yaml_*` editors (auth, tools, approvals_gates, memory_revision, max_tokens) edit by regex against that one-line shape and raise if not found. Never switch to a generic YAML re-dump here.
- Exactly one `FROM` line in the Dockerfile, even with litestream. WHY: `update_dockerfile_runtime_image` (runtime re-pin, used by runtime.py, tooling.py, intake.py, slack_setup.py) rewrites only the first `FROM` match (`count=1`), so with a second `FROM` the re-pin could hit the wrong stage.
- `team` ≠ `group`: `team` is the skills-catalog axis (card `metadata.team`), `group` the ACL namespace (self.md only). Never conflate.
- agent-card.json `metadata.capability` starts `""`; HelloStep earns it and `commit_card_metadata` (metadata.py) writes it. The switchboard directory reads the card from the repo, so the repo is the source of truth for it.
- Skill materialization rejects symlinks and paths escaping `dest` (`_materialize_skill` raises `ComposeError`).
- The approvals `ceiling.write: false` is never loosened by the cabal gate change; a write-capable tool must add its own gate.
CONSUMERS: api/kobo_api/steps.py (RepoStep `compose`/`validate_agent_yaml`, `_self_md`/`_colleagues_md` for roster refresh), api/kobo_api/runtime.py + tooling.py + intake.py + slack_setup.py (`update_dockerfile_runtime_image`), api/kobo_api/colleagues.py (`CABAL_COLLEAGUE_TOKEN_ENV`), api/tests/test_composer.py (incl. optional real-daedalus load via `pytest.importorskip("daedalus")`). Cross-repo: daedalus runtime loads this bundle at /bundle; gojira builds and deploys the repo.
GOTCHA: slack_setup.py `_update_agent_yaml_transports` round-trips agent.yaml through `yaml.safe_dump`, which rewrites the whole file in block style. After Slack is wired, the one-line shapes the `update_agent_yaml_*` regex editors need are gone. Those five editors have no production caller today (only tests/test_composer.py), so this is latent: don't reach for them on a Slack-wired agent without checking.
