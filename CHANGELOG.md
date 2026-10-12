# Changelog

All notable changes to kobo are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

<<<<<<< HEAD
## [0.1.0] - 2026-10-12
=======
### Fixed

- **An agent can be created again under a destroyed agent's name.** Destroy renames the
  repository to `archived-agent-<name>-<id>`, and GitHub keeps answering at the old name
  through a redirect; the room read that as "a repository already exists and is not
  recorded for this agent" and refused. The existence check now compares the name GitHub
  answers with, so a redirected name counts as free, which GitHub allows a new repository
  to take.
>>>>>>> 06ac77d (A destroyed agent's name is free again: the existence check ignores GitHub's rename redirect)

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
