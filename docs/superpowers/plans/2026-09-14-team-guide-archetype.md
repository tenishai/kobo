# Team Guide Archetype + Observability Links Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One-click per-team informational agent (archetype-forced persona/tools/tags, team-named, optional voice) plus argus dashboard links on agent cards.

**Architecture:** The archetype lane renders a packaged persona template at create time and stores it as an ordinary room persona (`team-guide-<group>`), so compose (`_resolve_persona_body`) and every downstream step run unchanged. Forcing (tools, tags, visibility, skills) happens in the create route before the shared create body. Observability links mirror the `catalog/runtimes` pattern end to end.

**Tech Stack:** FastAPI + SQLAlchemy (kobo api), Svelte + tanstack-query (kobo ui), acme-skills marketplace (publish law), gojira agent-class pipeline (untouched).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-09-14-team-guide-archetype-design.md`.
- Kiosk tools: colleagues + techne memory ONLY — no github/jira/cloudtrail/slack_read/liminal allow entries.
- Canonical discovery tags forced and unremovable at create: `team-guide` + the group name.
- Voice is style-not-identity, subordinate to the constitution; absent voice → no overlay section at all.
- Duplicate guard: one guide per group, 409 naming the existing agent.
- Button copy: **"Deploy your team's guide"**.
- Observability URL shape: `{argus_url}/projects/{argus_project}/dashboards/{agent_name}-agent-health`; links hidden when config absent.
- Skill `team-guide` 1.0.0 obeys the publish law: frontmatter name/description/version/tags(≥1 domain)/profiles(non-empty, fixed set)/owner.
- GATE: first live kiosk only after the fleet runtime re-pin containing daedalus #49 (+ #52).
- Test envs: kobo api = `cd api && PYTHONPATH=. .venv/bin/python -m pytest`; UI tests not run (Kyle's standing ruling).

---

### Task 1: Archetype module + persona template

**Files:**
- Create: `api/kobo_api/archetypes.py`
- Create: `api/kobo_api/templates/archetypes/team-guide.md`
- Test: `api/tests/test_archetypes.py`

**Interfaces:**
- Produces: `ARCHETYPE_TEAM_GUIDE = "team_guide"`, `TEAM_GUIDE_TAG = "team-guide"`,
  `render_team_guide(name: str, group: str, voice: str | None) -> str`,
  `team_guide_persona_id(group: str) -> str` (returns `f"team-guide-{group}"`).

- [ ] **Step 1: Write the template** at `api/kobo_api/templates/archetypes/team-guide.md`:

```markdown
You are {name}, the informational guide for the {group} team.

You exist to learn everything about this team and answer questions about it —
for new teammates finding their feet, for other teams wondering what {group}
does, and for other agents. You are the team's front desk, not its hands.

Hunger to learn. In every conversation with team members, look for a chance
to learn something new about the team: its mission, the services and systems
it owns, its repos, the tooling and vendors it uses day to day, its
technologies, its oncall and escalation paths, its rituals and meetings, its
glossary, who holds which role, and how people get access to the things the
team uses. Ask one question at a time and follow the thread. When someone
shares something worth keeping, encourage them to use the Teach gesture so
it is durably remembered.

Never act. You do not create tickets, open pull requests, change systems, or
take actions of any kind, for anyone. When asked to, decline warmly, explain
that you are purely informational, and offer what you CAN do: answer,
learn, and introduce the team.

Answer honestly from what you have been taught. When you don't know
something, say so plainly, say you'd love to be taught it, and never invent.
Never present uncertain knowledge as certain.

Other teams are welcome. Answer their questions about {group} gladly. For
anything sensitive or decision-shaped, direct the asker to the team itself.
{voice_section}
```

- [ ] **Step 2: Write the failing tests** at `api/tests/test_archetypes.py`:

```python
"""The team-guide archetype: template rendering and the voice overlay's bounds."""

from kobo_api.archetypes import render_team_guide, team_guide_persona_id


def test_render_interpolates_name_and_group():
    body = render_team_guide("atlas", "sre", None)
    assert "You are atlas, the informational guide for the sre team." in body
    assert "{name}" not in body and "{group}" not in body


def test_no_voice_leaves_no_residue():
    body = render_team_guide("atlas", "sre", None)
    assert "{voice_section}" not in body
    assert "Style:" not in body
    assert not body.rstrip().endswith("}")


def test_voice_overlay_is_subordinate_style():
    body = render_team_guide("atlas", "sre", "Dolly Parton")
    assert "Style: channel the conversational voice and cadence of Dolly Parton" in body
    assert "never claim to be Dolly Parton" in body
    # constitution precedes voice: Never act appears before Style
    assert body.index("Never act.") < body.index("Style:")


def test_persona_id_is_per_group():
    assert team_guide_persona_id("sre") == "team-guide-sre"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd api && PYTHONPATH=. .venv/bin/python -m pytest tests/test_archetypes.py -q`
Expected: FAIL — `ModuleNotFoundError: kobo_api.archetypes`

- [ ] **Step 4: Implement** `api/kobo_api/archetypes.py`:

```python
"""Archetype agents: pre-shaped, one-click deployables (team-guide spec 2026-09-14).

An archetype is a create-time recipe, not a runtime concept: the route renders
the packaged template into an ordinary room persona and forces the bundle
surface (tools, tags, visibility, skills), then the standard pipeline runs
unchanged. `_resolve_persona_body` never learns archetypes exist.
"""

from pathlib import Path

ARCHETYPE_TEAM_GUIDE = "team_guide"
TEAM_GUIDE_TAG = "team-guide"

_TEMPLATE = Path(__file__).parent / "templates" / "archetypes" / "team-guide.md"

_VOICE_SECTION = (
    "\nStyle: channel the conversational voice and cadence of {voice} — the wit,"
    "\nwarmth, and rhythm, not the identity. You are always {name}, {group}'s"
    "\nguide; you never claim to be {voice}, and the style never overrides your"
    "\nconduct: you still refuse actions, still cite what you've been taught,"
    "\nstill interview.\n"
)


def team_guide_persona_id(group: str) -> str:
    return f"team-guide-{group}"


def render_team_guide(name: str, group: str, voice: str | None) -> str:
    body = _TEMPLATE.read_text()
    voice = (voice or "").strip()
    section = (
        _VOICE_SECTION.replace("{voice}", voice) if voice else ""
    )
    return (
        body.replace("{voice_section}", section)
        .replace("{name}", name)
        .replace("{group}", group)
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd api && PYTHONPATH=. .venv/bin/python -m pytest tests/test_archetypes.py -q`
Expected: `4 passed`

- [ ] **Step 6: Commit**

```bash
git add api/kobo_api/archetypes.py api/kobo_api/templates/archetypes/team-guide.md api/tests/test_archetypes.py
git commit -m "feat(archetypes): team-guide persona template + renderer"
```

### Task 2: The forced create lane (API)

**Files:**
- Modify: `api/kobo_api/main.py` (CreateAgentRequest ~line 202; create_agent route ~line 2893)
- Test: `api/tests/test_api_agents.py` (append)

**Interfaces:**
- Consumes: Task 1's `ARCHETYPE_TEAM_GUIDE`, `TEAM_GUIDE_TAG`, `render_team_guide`, `team_guide_persona_id`.
- Consumes (existing, verify by grep at implementation): the persona-write helper used by
  the create-persona route (`grep -n "def create_persona" api/kobo_api/personas.py`) —
  call it exactly the way `POST /api/personas`' route body does (storage client, bucket
  from `cfg`, id, body, metadata `{"team": group, "creator": "kobo"}`, tags
  `[TEAM_GUIDE_TAG]`), including its search-ingest follow-up if the route does one.
- Produces: `POST /api/agents` accepting `archetype: str | None` and `voice: str | None`;
  409 body `{"error": "…", "existing": "<agent name>"}` on duplicate.

- [ ] **Step 1: Write the failing tests** (append to `api/tests/test_api_agents.py`, reusing its existing `client` fixture and create helpers — mirror the style of the runtime_image tests already in the file):

```python
# --- team-guide archetype (spec 2026-09-14) ---------------------------------


def _create_guide(client, name="atlas", group="sre", voice=None, **extra):
    payload = {
        "name": name,
        "persona": "helpful-generalist",  # ignored by the archetype path
        "archetype": "team_guide",
        "group_name": group,
        **({"voice": voice} if voice else {}),
        **extra,
    }
    return client.post("/api/agents", json=payload)


def test_archetype_forces_tags_persona_and_commons_namespace(client):
    r = _create_guide(client)
    assert r.status_code == 201, r.text
    agent = client.get(f"/api/agents/{r.json()['id']}").json()
    assert "team-guide" in agent["purpose_tags"] and "sre" in agent["purpose_tags"]
    assert agent["persona"] == "team-guide-sre"
    # shared visibility, mechanically: the guide lives in the commons
    assert agent["group_name"] == "shared"


def test_archetype_ignores_client_supplied_skills_and_runtime(client):
    r = _create_guide(client, skills=[{"id": "anything"}], runtime_image="evil/img:1")
    assert r.status_code == 201, r.text
    agent = client.get(f"/api/agents/{r.json()['id']}").json()
    assert agent.get("runtime_image") in (None, "")


def test_archetype_duplicate_guard_409_names_existing(client):
    assert _create_guide(client, name="atlas").status_code == 201
    r = _create_guide(client, name="beacon")
    assert r.status_code == 409
    assert r.json()["existing"] == "atlas"


def test_archetype_voice_lands_in_stored_persona(client, storage):
    _create_guide(client, voice="Anthony Bourdain")
    body = storage.read("team-guide-sre/persona.md")
    assert "Anthony Bourdain" in body and "Never act." in body
```

(Adapt fixture names at implementation to the file's actual conventions — the
`storage` fake is whatever fixture the persona-create tests in this suite
already use to read bucket writes back; groups: use whatever group the wired
`config` fixture already configures, or configure `sre` the way the group
tests at the top of the file do.)

- [ ] **Step 2: Run to verify failures**

Run: `cd api && PYTHONPATH=. .venv/bin/python -m pytest tests/test_api_agents.py -q -k archetype`
Expected: 4 failures (unknown field accepted silently → assertions fail).

- [ ] **Step 3: Implement.** In `CreateAgentRequest` add:

```python
    # Archetype create lane (team-guide spec 2026-09-14): when set, the route
    # FORCES persona/tools/tags/visibility/skills and ignores the client's
    # values for them — "cannot act" is enforced in the composed bundle, not
    # in UI defaults. Only "team_guide" exists today.
    archetype: str | None = None
    # Optional style overlay for archetype personas (style-not-identity;
    # subordinate to the constitution — see kobo_api/archetypes.py).
    voice: str | None = None
```

In `create_agent`, immediately after the group-consent block (so group
validation/consent has already run) and before persona/skill resolution,
insert the archetype branch:

```python
        if payload.archetype is not None:
            if payload.archetype != ARCHETYPE_TEAM_GUIDE:
                return JSONResponse(
                    {"error": f"unknown archetype: {payload.archetype!r}"},
                    status_code=422,
                )
            # The PICKED group is the team this guide serves — the group
            # validation + membership-consent block above already ran on it,
            # so only that team's members (or admins) can deploy its guide.
            group = payload.group_name or SHARED_GROUP
            if payload.voice is not None and len(payload.voice) > 120:
                return JSONResponse(
                    {"error": "voice should be a short name, not a biography"},
                    status_code=422,
                )
            # One guide per team: the team identity lives in the TAG PAIR
            # (team-guide + group), because the agent's ACL namespace is
            # forced to the commons below.
            for existing in session.query(Agent):
                tags = decode_tags(existing.purpose_tags)
                if TEAM_GUIDE_TAG in tags and group in tags:
                    return JSONResponse(
                        {
                            "error": f"{group} already has a team guide",
                            "existing": existing.name,
                        },
                        status_code=409,
                    )
            # Shared visibility, mechanically: the guide lives in the
            # commons namespace so every team's humans (and agents) can find
            # and chat with it — the spec's "other teams could then chat
            # with it". Its team identity is the tag pair + persona, not its
            # ACL namespace.
            payload.group_name = SHARED_GROUP
            persona_id = team_guide_persona_id(group)
            body = render_team_guide(payload.name, group, payload.voice)
            # Store as an ordinary room persona so _resolve_persona_body and
            # everything downstream run unchanged. Call the same helper the
            # create-persona route uses (see Interfaces), overwrite-safe:
            # re-creating a guide after a destroy re-renders the persona.
            _write_room_persona(  # replace with the actual helper per Interfaces
                app, persona_id, body, team=group, tags=[TEAM_GUIDE_TAG]
            )
            # Force the surface: the rest of the route reads payload.*.
            payload.persona = persona_id
            payload.skills = [SkillPick(id="team-guide")] if _skill_exists(engine, "team-guide") else []
            payload.runtime_image = None
            payload.team = "shared"
            payload.purpose_tags = clean_tags(
                [TEAM_GUIDE_TAG, group, *payload.purpose_tags]
            )
```

Implementation notes (resolve at implementation, they are lookups not
decisions): `_write_room_persona` = the exact body of the create-persona
route's storage write + index merge + ingest, factored or called directly;
`_skill_exists` = the same `get_entry(engine, "skill", "team-guide", team=None)`
idiom the skill-resolution loop below already uses — attach when present,
skip silently when the marketplace hasn't published it yet (a kiosk without
the skill still has its constitution from the persona). Skills forcing must
run BEFORE the route's normal `resolved_skills` loop so the loop resolves
the forced pick. Tag forcing composes with `clean_tags`' cap by putting the
two canonical tags first.

- [ ] **Step 4: Run the archetype tests + the file's existing suite**

Run: `cd api && PYTHONPATH=. .venv/bin/python -m pytest tests/test_api_agents.py -q`
Expected: all pass (existing + 4 new).

- [ ] **Step 5: Commit**

```bash
git add api/kobo_api/main.py api/tests/test_api_agents.py
git commit -m "feat(api): team_guide archetype - forced persona/tools/tags, one per group"
```

### Task 3: MCP parity

**Files:**
- Modify: `api/kobo_api/mcp/tools/write.py` (kobo_create_agent, ~line 679 region)
- Test: `api/tests/test_mcp_write.py` or wherever kobo_create_agent's param tests live (`grep -rn "runtime_image" api/tests/test_mcp*.py` and mirror that test's shape)

- [ ] **Step 1: Failing test** — mirror the existing runtime_image MCP param test: call the tool with `archetype="team_guide"`, `group_name`, `voice="X"`, assert the created agent carries the forced tags (read back via the same session/fixture that test uses).

- [ ] **Step 2: Implement** — add `archetype: str | None = None` and `voice: str | None = None` params to `kobo_create_agent`, passed through to the same internal create call the tool already makes (exactly how `runtime_image` was added — copy that plumbing line for line, docstring included: "archetype='team_guide' deploys a team's informational guide; forces persona/tools/tags").

- [ ] **Step 3: Run** the MCP write tests: `cd api && PYTHONPATH=. .venv/bin/python -m pytest tests/ -q -k "mcp and create"` → pass.

- [ ] **Step 4: Commit** `feat(mcp): kobo_create_agent gains archetype + voice`.

### Task 4: Observability catalog (API)

**Files:**
- Modify: `api/kobo_api/config.py` (beside `runtimes: list[RuntimeChoice]`)
- Modify: `api/kobo_api/org.py` (ORG_KEYS), `api/kobo_api/bootstrap.py` (org token map), `api/templates/org_config/org.yaml.tmpl` if present (`grep -rn "liminal_url" api/` shows every site the last org key touched — touch the same set)
- Modify: `api/kobo_api/main.py` (beside `GET /api/catalog/runtimes`)
- Test: `api/tests/test_api_agents.py` or the file holding the runtimes-catalog tests (`grep -rn "catalog/runtimes" api/tests/`)

- [ ] **Step 1: Failing tests**:

```python
def test_observability_catalog_empty_without_config(client):
    r = client.get("/api/catalog/observability")
    assert r.status_code == 200
    assert r.json() == {"url": None, "project": None}


def test_observability_catalog_returns_configured_values(client):
    cfg = client.app.state.config
    old = (cfg.argus_url, cfg.argus_project)
    cfg.argus_url, cfg.argus_project = "https://argus.example", "ai-agents-dev"
    try:
        assert client.get("/api/catalog/observability").json() == {
            "url": "https://argus.example",
            "project": "ai-agents-dev",
        }
    finally:
        cfg.argus_url, cfg.argus_project = old
```

- [ ] **Step 2: Implement** — `config.py`: `argus_url: str | None = None` and `argus_project: str | None = None`; wire through ORG_KEYS/bootstrap/template exactly as `liminal_url` is; route beside the runtimes catalog:

```python
    @app.get("/api/catalog/observability")
    def observability_catalog() -> dict:
        cfg: Config = app.state.config
        both = bool(cfg.argus_url and cfg.argus_project)
        return {
            "url": cfg.argus_url if both else None,
            "project": cfg.argus_project if both else None,
        }
```

- [ ] **Step 3: Run** the touched test file → pass. **Step 4: Commit** `feat(api): observability catalog - argus link config`.

### Task 5: UI — guide button + modal

**Files:**
- Create: `ui/src/components/TeamGuideModal.svelte`
- Modify: the component hosting the Create agent button (`grep -rn "CreateModal" ui/src` for the mount point), `ui/src/lib/api.ts`, `ui/src/lib/queries.ts`

**Interfaces:**
- Consumes: `POST /api/agents` with `{name, archetype: "team_guide", group_name, voice?, persona: "helpful-generalist"}` (persona ignored server-side but the field is required by the request model); 409 → `{error, existing}`.

- [ ] **Step 1: api.ts** — extend `createAgent`'s payload type with `archetype?: string; voice?: string` (same optional-field pattern `runtime_image` used).

- [ ] **Step 2: TeamGuideModal.svelte** — same visual skeleton as `CreateModal.svelte`'s simplest station: Group `<select>` (reuse the groups source CreateModal uses), Name text input (validated non-empty), Voice text input with helper copy "name a famous person, artist, or regional character your guide should sound like (optional)", submit "Deploy guide". On group change, query the agents list the UI already holds (the same store AgentCard renders from) for one whose `purpose_tags` includes `team-guide` and whose `group_name` matches — if found, replace submit with a link-button "Open {name}" to that agent's detail route. On 409, show `error` with a link to `existing`. On 201, reuse whatever CreateModal does post-create (navigate to the new agent / progress checklist).

- [ ] **Step 3: Mount the button** under the Create agent button: secondary style, copy **"Deploy your team's guide"**, opens TeamGuideModal.

- [ ] **Step 4: Build check** (no vitest per standing ruling): `cd ui && npm run build` → succeeds.

- [ ] **Step 5: Commit** `feat(ui): Deploy your team's guide - archetype modal`.

### Task 6: UI — observability links

**Files:**
- Modify: `ui/src/lib/api.ts` (`fetchObservability`), `ui/src/lib/queries.ts` (`observabilityQuery`, mirroring `runtimesQuery` at :263), `ui/src/components/AgentCard.svelte`, the agent detail page component (`grep -rn "runtime" ui/src/routes` or wherever detail renders).

- [ ] **Step 1: api.ts + queries.ts** — `fetchObservability(): Promise<{url: string | null, project: string | null}>` hitting `/api/catalog/observability`; `observabilityQuery()` with a long staleTime (config changes only on roll).

- [ ] **Step 2: AgentCard.svelte** — when the query resolves with both values non-null, render a small external link "Observability" → `` `${url}/projects/${project}/dashboards/${agent.name}-agent-health` `` (target `_blank`, `rel="noreferrer"`), styled like the card's existing secondary affordances. Detail page: a labeled row with the same href.

- [ ] **Step 3: Build check**: `cd ui && npm run build` → succeeds. **Step 4: Commit** `feat(ui): per-agent observability links`.

### Task 7: The team-guide skill (marketplace)

**Files:**
- Create (fresh clone): `<clone>/skills/team-guide/SKILL.md` in `example-org/acme-skills`

- [ ] **Step 1: Clone + branch** (kyle-example identity): `git clone https://github.com/example-org/acme-skills ~/Documents/GitHub/acme-skills && cd ~/Documents/GitHub/acme-skills && git checkout -b feat/team-guide-skill`. Read one existing skill's frontmatter (`skills/kobo-mcp-usage/SKILL.md`) and the validator (`tools/skill_catalog/schema.py`) to copy the exact frontmatter field shapes.

- [ ] **Step 2: Write** `skills/team-guide/SKILL.md` with frontmatter (exact keys per the validator; values): `name: team-guide`, `description: How a team's informational guide interviews, answers, and never acts`, `version: 1.0.0`, `tags: [sre-core, onboarding]` (adjust the domain tag to one the validator's fixed set actually contains — read `schema.py` first), `profiles: [agent-sdk]`, `owner: sre`. Body: the interview knowledge map (mission / services owned / repos / tooling+vendors / technologies / oncall+escalation / rituals / glossary / roles / access how-tos), one-question-at-a-time + follow-the-thread + periodic summarize-and-ask-what's-missing + Teach-gesture nudge rules; the conduct half (refuse actions with the friendly why, provenance honesty, cross-team etiquette, report repeated untaught questions as teaching gaps; you may use list_colleagues/ask_colleague to introduce OTHER teams' guides — find them by the team-guide purpose tag).

- [ ] **Step 3: Validate locally** exactly as CI does: `python tools/ci_publish.py --validate-only` if that flag exists (read the script header), else run the schema/frontmatter modules over the corpus the way the script's main does. Expected: zero errors (the brick law).

- [ ] **Step 4: PR** — push branch, `gh pr create -R example-org/acme-skills` titled `feat: team-guide 1.0.0 - the informational kiosk skill`; body notes it's additive (no existing-skill edits → no version-bump interactions). Merge is Kyle's/Rich's; the kiosk create lane attaches the skill only once published (Task 2's `_skill_exists` fallback covers the gap).

### Task 8: Rollout (operator-gated where marked)

- [ ] **Step 1 (GATE, build):** fleet runtime re-pin with daedalus #49+#52: `cd ~/Documents/GitHub/daedalus && git checkout main && git pull`, build the fleet runtime image with the standing build shape (`docker buildx build --platform linux/amd64 --build-context liminal=../liminal -t us-east4-docker.pkg.dev/example-project/ai-agents/acme-runtime:<sha7>-t<HHMM> --push docker/` — copy the exact command from the last fleet build, `git log` in daedalus or the argus-deploy-arc memory), then update `runtime_image` in kobo-config org.yaml to the new tag.
- [ ] **Step 2:** org.yaml adds `argus_url: https://argus-dev.example.net` and `argus_project: ai-agents-dev` (same commit as the pin).
- [ ] **Step 3:** build kobo image `kobo:<sha7>` (standard buildx --push), hand Kyle the roll command. **KYLE:** roll.
- [ ] **Step 4 (dogfood):** via UI or MCP, deploy the SRE group's guide (name Kyle's choice, voice optional), verify: pod Ready on the NEW runtime tag; chat greets with mission + first interview question; asking it to open a PR → warm refusal; second create attempt for sre → 409; agent card shows Observability link resolving to its argus dashboard; `list_colleagues` from another agent shows the guide.
- [ ] **Step 5:** memory + spec status update; register follow-ups (discovery UX arc, ambient absorption revisit).
