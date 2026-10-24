---
card: arch-card:kobo-platform-clients
updated: 2026-10-24
anchor: api/kobo_api/clients.py sha=2905cda14565a73870e110c1fd566f2d0b017d59
anchor: api/tests/fakes.py sha=20503d0f4c28a4c384fd80b929ce6f24371cb0b2
---
# platform-clients — one Protocol per outside system, one real impl, one fake

WHAT / SEAM: api/kobo_api/clients.py. The outside systems in the table below are reached through a `typing.Protocol` (cabal is a concrete class), and code depends on the Protocol, never the backend. Six outside calls do NOT: they use httpx directly, are not in `Clients`, and have no fake in tests/fakes.py: pr_broker.py (GitHub App token mint), jira_broker.py (Jira GET), mcp/oauth_as.py (Google token exchange), iap.py (JWKS fetch), github_app.py (installation token), bootstrap.py (catalog refresh POST). The `Clients` dataclass (l.~4782) is the bag handed to steps and routes (`app.state.clients`, `Ctx.clients`).
| field | Protocol | real impl | fake (tests/fakes.py) |
|---|---|---|---|
| github | `GitHubClient` | `GhCLI` (shells `gh`/`git`) | `FakeGitHub` |
| gojira | `GojiraClient` | `HttpxGojiraClient` (`/api/v1/apps...`) | `FakeGojira` |
| cloud | `CloudClient` | `GcloudClient` (GSA, WI binding, groups, bucket IAM) | `FakeCloud` |
| agent | `AgentClient` | `KubectlAgentClient` (port-forward a2a, legacy direct path) | `FakeAgent` |
| kubectl | `KubectlClient` | `KubectlCLI` (rollout, secrets, ExternalSecret status, `deployment_status_json`/`pods_json` for health.py) | `FakeKubectl` (set `.pods` for pod fixtures) |
| slack | `SlackClient` + `SlackTokenStore` | `HttpxSlackClient`; `FileSlackTokenStore` / `KubeSlackTokenStore` | `FakeSlack` |
| image / name | `ImageClient` / `NameClient` | `VertexImageClient` / `VertexNameClient` | `FakeImage` / `FakeName` |
| storage | `StorageClient` | `HttpxStorageClient` (GCS JSON API) | `FakeStorage` |
| cabal | (concrete) `CabalClient` | same | `FakeCabal` — see switchboard.md |
| groups / people_search | `GroupResolver` / `OktaPeopleSearch` (groups.py) | `OktaGroupResolver` / `OktaPeopleSearch` | `FakePeopleSearch`; resolvers are ad hoc per test file |
| sero | `SeroClient` (sero.py) | `HttpxSeroClient` | `FakeSero` |
OPS / FLOW: `Clients.real(config)` is called once per process in `main.build_default_app`; tests build `Clients(...)` from fakes and pass it to `create_app`. `CommandRunner.run()` raises `ClientError` on non-zero exit; callers that tolerate one failure catch it and inspect `.stderr` (e.g. `_gh_returned_404`). GitHub auth: a GitHub App installation token (`installation_token_provider`, github_app.py) passed as `GH_TOKEN` in the subprocess env when the App is configured, else the ambient `gh auth` session. Vertex/GCS tokens come from `CachedGcloudToken` (metadata server, then `gcloud` CLI fallback).
INVARIANTS:
- slack, cabal, groups, people_search and sero are None when unconfigured, and None means "feature off". Check for None; never construct a client ad hoc in a route. `image`, `name` and `storage` are always built by `Clients.real`; their None default exists only so older test constructions of `Clients(...)` keep working.
- Never put a token or secret value in a raised message or log. WHY: raised messages can reach route responses. `CabalClient` and `HttpxStorageClient` go further and keep response bodies out of raised messages (logged truncated, `resp.text[:200]`); `HttpxGojiraClient` does NOT (its `ClientError` includes `resp.text`), so don't copy it for a client whose responses may echo secrets.
- A new external call gets a Protocol method + a fake method in tests/fakes.py in the same change; the fakes mirror P0-observed backend behavior (e.g. gojira's generated kustomization) so step tests mean something.
- `GojiraConflict` (409 on create) is distinct from `ClientError` so onboard can decide whether the app is its own.
CONSUMERS: api/kobo_api/steps.py, provisioner.py, health.py (ready agents probed once a minute; verdict kept in memory per replica), destroy.py, main.py (routes), directory.py, colleagues.py, pairs.py, learnings.py, knowledge.py, slack_setup.py, agent_env.py, mcp/tools/*.py; api/tests/* via tests/fakes.py.
API: `Clients.real(config)`; `cabal_enabled(config)`; `installation_token_provider(config)`; `probe_slack_token_store(clients)` (boot write-probe of the Slack token Secret); `gsa_email(name, project)`; errors `ClientError`, `GojiraConflict`, `SlackApiError`, `StorageNotFound`, `CabalError`.

2026-10-12: `GhCLI.repo_exists` reads `gh api repos/{repo}` and compares the `full_name` GitHub answers with; a name GitHub only redirects (a destroyed agent's repo, renamed to `archived-agent-<name>-<id>`) counts as free.
- `CabalClient.mint_service` (2026-10-24): the generic switchboard mint behind the room's own gateway admin token (`kobo_api.liminal_admin.RoomAdminToken`: sub and persona `kobo-room`, aud `liminal`, `admin:*`). `Clients.liminal_admin` is that admin-plane client, typed loosely to keep `liminal_admin` importing this module and not the reverse.
- `CabalClient.put_dispatch_pairs` (2026-10-24, issue #194): never sends an empty set; an empty policy goes out as `DENY_ALL_PAIR` (see switchboard).
- `KubectlClient.namespace_created_at` (2026-10-24, issue #193): a namespace's creation time as naive UTC, None when absent, `ClientError` when the cluster cannot be asked. Create and connect refuse a name whose namespace exists; destroy keeps the namespace, the stored secrets and the `agent-<name>` GSA of an agent whose namespace is older than its row. `FakeKubectl(namespaces={name: created_at}, namespace_error=...)` seeds it.
