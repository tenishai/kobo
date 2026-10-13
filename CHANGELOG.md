# Changelog

All notable changes to kobo are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- **A connected agent no longer waits forever on an install with no Gojira webhook.** The
  waiting sweep read the platform's status, which moves only when the webhook reports the
  setup PR's merge, so the agent stayed at "waiting" after the merge (issue #118). The sweep
  now asks Gojira to check the pull request (`POST /api/v1/apps/{id}/sync`) before it reads
  the status. With a webhook the call is a no-op.
- **A re-pinned runtime image is named, not silent.** Intake, teach, tooling and Slack
  commits put the Dockerfile `FROM` back to the image the room has recorded for the agent,
  which reverted a hand-pinned `FROM` with nothing said (issue #140). The re-pin stays,
  because the recorded image is the authority, but the commit message and a warning log now
  name both images. Change an agent's image with `kobo_set_runtime_image`.
- **The GitHub token broker says why it has nothing to mint.** After the agent's own token
  verified, its 404 said "no such agent" (issue #11). The status is still 404, but the
  message now says whether repository access was never requested, is waiting for an
  operator, or was revoked, and names the route that fixes each.
- **"The switchboard is not configured" names the fix.** The 409, and the `kobo_feed` MCP
  tool's error for the same gate, now list the three settings it checks: `cabal_url` and `agent_endpoint_template` in the org config, and
  `CABAL_OPERATOR_TOKEN` in the room's environment (issue #123, item 1).
- **A waiting agent's card says what it waits on.** The agents list showed a bare
  `waiting` chip; the card now reads "Waiting on a pull request. Open for details."
  (issue #123, item 7).
- **Composed agents with skills now get `skills.load`.** daedalus 0.5.0 shows assigned
  skills as an index in the system prompt plus a `skills.load(name)` tool, but only when
  `tools.allow` admits the tool, and the composer never added it, so every composed agent
  with skills logged a warning at boot and lost the index (issue #137). The composer adds it
  for any agent born with skills, and the self-card names it. Adding a skill later, from the
  catalog or as a local skill, adds `skills.load` to `tools.allow` in the same commit when
  the allow list doesn't already admit it. An agent that already has skills and gets none
  added keeps its allow list: add `skills.load` to `tools.allow` in its `bundle/agent.yaml`.
  Older runtimes ignore the entry.
- **Changing an agent's colleague mode no longer locks its row across a GitHub push.** The
  route set `colleague_mode` before rendering the colleague document, so the roster query
  flushed the UPDATE and Postgres held the agent's row lock through the clone and push. The
  document now renders for the new mode without touching the row, and the row is written
  after the push lands (issue #133).
- **Colleague mode is documented.** The guide, glossary, README and `docs/what-the-room-does.md`
  said every agent asks its colleagues; they now describe `roster-only` and `none`, and the
  guide says where an owner sets the mode. The route and MCP tool docstrings, and a note under
  the Harness tab's Colleagues row, now say that `roster-only` also stops the agent answering
  from general knowledge. A stranger-gets-403 test now covers the colleague-mode, runtime,
  model and Slack wake routes.
- **An agent can be created again under a destroyed agent's name.** Destroy renames the
  repository to `archived-agent-<name>-<id>`, and GitHub keeps answering at the old name
  through a redirect; the room read that as "a repository already exists and is not
  recorded for this agent" and refused. The existence check now compares the name GitHub
  answers with, so a redirected name counts as free, which GitHub allows a new repository
  to take.

## [0.1.0] - 2026-10-12

### Changed

- **Two platform names.** The secrets platform the room hands per-agent Secrets to is
  **sero** (named ladon until 2026-10-12) and the observability platform it links to is
  **noto** (named argus). The deployment config keys are `sero_url`, `sero_audience`,
  `sero_namespace`; the org keys are `noto_url`, `noto_project`; the module is
  `kobo_api.sero`. The old key names are still honoured, each with a boot WARNING,
  so no running room changes behaviour before its config files catch up.

The first tagged release, `0.1.0`, is the room as the README describes it: create, connect,
chat and teach, memory, colleagues and threads, access, Slack, the agent page, personas and the
MCP surface. Everything before the tag is in the git history and in the design record under
[docs/superpowers/](docs/superpowers/).

### Added

- A quickstart that runs from a clean clone, community files (code of conduct, support,
  issue and pull request templates, CODEOWNERS) and this changelog, ahead of going public.

### Changed

- `docs/superpowers/` carries a README saying the specs and plans are a historical record,
  not a description of the room today.
- CI installs from the committed `api/uv.lock` with `uv sync --locked`.
- The README is a two-screen read; the operator's material moved to `docs/operating.md` and the
  long feature list to `docs/what-the-room-does.md`, both verbatim.
