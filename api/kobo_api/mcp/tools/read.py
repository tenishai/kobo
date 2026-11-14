"""kobo's read-only MCP tools: the original four from the MCP server arc
(Task 7) -- whoami, list, get, and feed -- plus two marketplace-catalog
listers added by the MCP marketplace wave's Task 2 -- `kobo_list_skills`
and `kobo_list_tools` -- plus the persona wave's Task 4 -- `kobo_list_personas`
-- plus the MCP learnings trio's own read half (Task 8) -- `kobo_list_learnings`,
the decide-gated view over one agent's learnings queue; its write half,
`kobo_decide_learning`/`kobo_decide_learnings_bulk`, lives in
`kobo_api.mcp.tools.write` since they mutate, the same read/write split
this pair of modules already holds for everything else.
Every one of these is a thin shim over the room's own choke points --
`kobo_api.mcp.authz` for visibility/admin, `kobo_api.main` for the feed's own
flattening pipeline, the persona search route, and the learnings list route,
`kobo_api.search`/`kobo_api.marketplace` for the two marketplace catalogs --
never a second implementation of logic that already exists and is already
tested there.

Every tool wears `@mcp.tool(annotations=ToolAnnotations(readOnlyHint=
True))` on the outside and `@audited("kobo_x")` immediately under it --
`kobo_api.mcp.audit`'s own docstring explains why that order is
load-bearing (the SDK's schema/context introspection must see straight
through the audit wrapper to the real tool). Each tool function's own
docstring is what an MCP client sees as that tool's description
(`functools.wraps` copies `__doc__` onto the audited wrapper, and
`Tool.from_function` reads `fn.__doc__` when no explicit `description` is
given) -- written as plain, client-facing sentences, not internal
commentary.

`get_user_email` comes from `kobo_api.mcp.server` as a plain module-level
import -- safe here because that module defers ITS OWN import of this one
until `build_mcp_server` actually runs, well after `kobo_api.mcp.server`
has finished loading (see that function's docstring for the full reasoning
on why the deferral has to live on that side of the seam, not this one).
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from kobo_api.clients import ClientError
from kobo_api.guide import find_section, load_guide
from kobo_api.kinds import KIND_CONNECTED
from kobo_api.marketplace import MarketplaceError, list_tools
from kobo_api.mcp import authz
from kobo_api.mcp.audit import audited
from kobo_api.mcp.server import get_user_email
from kobo_api.purpose_tags import decode_tags
from kobo_api.search import read_marketplace_catalog
from kobo_api.timefmt import rfc3339_str

# The one error message both agent-scoped tools raise for "doesn't exist"
# and "exists but you can't see it" alike -- see `kobo_api.mcp.authz.
# NotFound`'s own docstring for why those two cases must always answer
# identically. Named here once so `kobo_get_agent` and `kobo_feed` can
# never drift apart on the wording.
_AGENT_NOT_FOUND = "agent not found"

# `kobo_get_agent_access` answers a repository search with this many names
# at most; an installation can carry thousands.
_REPOSITORY_MATCHES = 50

# `kobo_agent_activity` cuts each of its two lists here whatever is asked.
_ACTIVITY_ROWS_MAX = 200

_SWITCHBOARD_DID_NOT_ANSWER = "The switchboard did not answer. Try again shortly."

# `read_marketplace_catalog` answers "not the marketplace dialect" with a
# plain `None` rather than a typed error (kobo_api.search's own docstring):
# no index published at all, a legacy-dialect bucket, or unreadable JSON
# all collapse to the same signal. `kobo_list_skills` below is the one
# place that turns that `None` into the caller-facing error every other
# tool in this module gets from a typed exception instead.
_CATALOG_UNAVAILABLE = "the marketplace catalog is unavailable"

# Config.skills_bucket's own "absent = feature off" convention -- unlike
# `_CATALOG_UNAVAILABLE` above (a bucket that IS configured but carries no
# marketplace-dialect index yet), this is "the room was never pointed at a
# skills bucket at all", distinct enough from that case to earn its own
# message, mirroring `marketplace_repo`'s identical-shaped "the marketplace
# repo is not configured" for `kobo_list_tools` below -- the two tools
# agree on phrasing for the same underlying "unwired" state.
_SKILLS_BUCKET_NOT_CONFIGURED = "the marketplace skills bucket is not configured"

# Review fix round 1: a transport-level failure underneath either catalog
# (a GCS 403 on the skills bucket, a failed `gh repo clone` on an expired
# token or a renamed repo) surfaces from `kobo_api.clients` as a bare
# `ClientError` carrying internal detail -- a call signature, a bucket
# name, gh's own stderr -- never meant for an MCP client's eyes. Both
# tools below catch it and answer with this one sanitized message instead,
# the same "never leak the module's own error string" discipline
# `_SWITCHBOARD_DID_NOT_ANSWER` above already holds for `kobo_feed`.
_MARKETPLACE_DID_NOT_ANSWER = "The marketplace did not answer. Try again shortly."


def register(mcp: FastMCP, app: Any) -> None:
    """Registers this module's read tools on `mcp`. Each closes over
    `app` -- the same FastAPI app `kobo_api.mcp.authz.room_request` builds
    its synthetic `Request` from, and the source of the session factory,
    switchboard client, storage client, and GitHub client every tool
    below reaches through `app.state`.
    """

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_whoami")
    async def kobo_whoami(ctx: Context) -> dict:
        """Report who the room thinks you are: your verified email,
        whether you hold operator standing, and the directory groups you
        belong to. Not scoped to any one agent, and nothing here is
        gated -- every signed-in caller can ask this.
        """
        email = get_user_email(ctx)
        clients = app.state.clients

        groups: list[str] = []
        groups_unavailable = False
        resolver = clients.groups if clients is not None else None
        if email and resolver is not None:
            resolved = resolver.groups_for(email)
            if resolved is None:
                groups_unavailable = True
            else:
                groups = resolved

        return {
            "email": email,
            "operator": authz.is_admin(app, email),
            "groups": groups,
            "groups_unavailable": groups_unavailable,
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_guide")
    async def kobo_guide(ctx: Context, section: str | None = None) -> dict:
        """Start here when someone asks what kobo is, what it can do for
        them, or which options it offers: `section="what-kobo-is"` is the
        overview, with where the room fits beside daedalus, cabal, techne,
        liminal, gojira and sero, and every choice a person makes with why
        they would make it. This is the room's own user guide -- the same
        text the Getting started page shows. With no `section`, returns
        every section's id, title and one-line summary in reading order;
        with a `section` id, returns that section's full markdown body. Read the relevant
        section BEFORE creating, wiring or changing an agent on someone's
        behalf: it says what each step does, what the room asks for, and
        which tool here matches which button in the room. Open to every
        signed-in caller.
        """
        if section is None:
            return {
                "sections": [entry.listing() for entry in load_guide()],
                "hint": "call again with section=<id> for the full text; what-kobo-is is the overview",
            }
        found = find_section(section)
        if found is None:
            known = ", ".join(entry.id for entry in load_guide())
            raise ToolError(f"no guide section {section!r}; sections are: {known}")
        return found.full()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_list_agents")
    async def kobo_list_agents(ctx: Context) -> list[dict]:
        """List every agent you may see in this room: its name, ACL
        group, skills team, owner, status, purpose, and kind. The same
        visibility rules the room's own agent list applies. A `composed`
        agent was built by the room and has a persona, skills and chat; a
        `connected` one is a team's own repo the room only deploys, and
        has none of those.
        """
        email = get_user_email(ctx)
        session = app.state.session_factory()
        try:
            agents = authz.visible_agents(session, app, email)
            return [
                {
                    "name": agent.name,
                    "group": agent.group_name,
                    "team": agent.team,
                    "owner": agent.created_by,
                    "status": agent.status,
                    "purpose": decode_tags(agent.purpose_tags),
                    "kind": agent.kind,
                }
                for agent in agents
            ]
        finally:
            session.close()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_get_agent")
    async def kobo_get_agent(ctx: Context, name: str) -> dict:
        """Get one agent's page: its kind, persona, team, purpose, model,
        status and status checklist, plus what the page's header and tabs
        show. `health` is the room's last look at the running agent (`null`
        before the first look). `identity` is the cloud identity it runs as,
        the one an access request to another project names. `runtime_image`
        is what it runs on, `room_runtime_image` what the room builds on
        today, `runtime_pinned` whether it deliberately holds its own.
        `colleague_mode` and `archetype` are as the page shows them.
        `slack` carries the listened channels, `wake`, `wake_emoji`, `command_public`, and whether this room
        makes Slack apps itself (`relay`); `kobo_get_slack` has the rest.

        A `connected` agent is a team's own repository the room deploys, so
        its persona and skills carry nothing to act on. For one that is
        ready, `checks` is what its page says is missing, in order: whether
        it is `running`, whether it is set up for the tool `gateway`, and
        whether it takes messages from `slack`. Each has a `state` (`ok`,
        `waiting`, `missing`, `idle`), one sentence in `line` to tell the
        person as written, `at` (when that was last seen, or null) and
        `pull_request` (the one its team merges, when it waits on one).
        Empty for every other agent. Answers "agent not
        found" both for a name that does not exist and for one you are not
        allowed to see; the two are never told apart.
        """
        # The page's own route, not a second copy of it: this tool rebuilt
        # the card field by field and fell behind the page twice (health,
        # identity, runtime, the Slack wake). `get_agent_endpoint` is
        # resolved further down `register`, with the other routes; a closure
        # reads it at call time.
        req = authz.room_request(app, get_user_email(ctx))
        session = app.state.session_factory()
        try:
            response = await _off_loop(get_agent_endpoint, name=name, request=req, session=session)
            _refuse_agent_route(response)
            body = _parse(response)
            checks: list = []
            if body["kind"] == KIND_CONNECTED:
                checks = _parse(
                    await _off_loop(checks_endpoint, name=name, request=req, session=session)
                ).get("checks", [])
        finally:
            session.close()
        # The names this tool has always answered with (`group`, `owner`,
        # `purpose`) stay; a client reads them next to `kobo_list_agents`.
        return {
            "name": body["name"],
            "persona": body["persona"],
            "group": body["group_name"],
            "team": body["team"],
            "owner": body["created_by"],
            "purpose": body["purpose_tags"],
            "capability": body["capability"],
            "archetype": body["archetype"],
            "model": body["model"],
            "model_label": body["model_label"],
            "skills": body["skills"],
            "status": body["status"],
            "health": body["health"],
            "kind": body["kind"],
            "checks": checks,
            "repo_full_name": body["repo_full_name"],
            "identity": body["identity"],
            "steps": [
                {"name": step["name"], "status": step["status"], "error": step["error"]}
                for step in body["steps"]
            ],
            "first_reply": body["first_reply"],
            "error": body["error"],
            "created_at": body["created_at"],
            "slack": body["slack"],
            "colleague_mode": body["colleague_mode"],
            "runtime_image": body["runtime_image"],
            "runtime_pinned": body["runtime_pinned"],
            "room_runtime_image": body["room_runtime_image"],
            "destroy_visible": body["destroy_visible"],
            "has_avatar": body["has_avatar"],
            "pending_learnings": body["pending_learnings"],
            "can_decide_learnings": body["can_decide_learnings"],
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_feed")
    async def kobo_feed(ctx: Context, agent: str, limit: int = 50) -> dict:
        """Read one agent's own colleague feed: every conversation it has
        had with other agents, either direction, flattened into
        chronological lines, oldest first. Visibility is checked before
        anything else -- an agent you cannot see answers "agent not
        found", the same as one that does not exist, before the
        switchboard is ever asked.
        """
        # Deferred import: kobo_api.main is a large module, and the same
        # circularity concern kobo_api.mcp.authz's own module docstring
        # already documents for its own imports from kobo_api.main
        # applies here too -- this module is imported at kobo_api.mcp.
        # server load time, well before kobo_api.main is guaranteed to
        # have finished importing everything it does.
        import httpx

        from kobo_api.clients import CabalError
        from kobo_api.main import (
            _FEED_SCROLLBACK_CAP,
            _SWITCHBOARD_NOT_CONFIGURED,
            _colleagues_gate,
            _conversation_has_participant,
            _flatten_conversation_entries,
        )

        email = get_user_email(ctx)
        session = app.state.session_factory()
        try:
            try:
                authz.visible_agent_or_not_found(session, app, email, agent)
            except authz.NotFound as exc:
                raise ToolError(_AGENT_NOT_FOUND) from exc
        finally:
            session.close()

        cfg = app.state.config
        clients = app.state.clients
        if _colleagues_gate(cfg, clients) is not None:
            raise ToolError(_SWITCHBOARD_NOT_CONFIGURED["error"])

        # Two server-side-scoped queries, merged and deduped by task_id --
        # the EXACT same fetch `kobo_api.main.colleague_feed` runs (see
        # that route's own docstring for why this is scoped at the
        # switchboard rather than capped after a room-wide fetch).
        try:
            incoming = clients.cabal.list_conversations(limit=200, target=agent)
            outgoing = clients.cabal.list_conversations(limit=200, caller=f"agent:{agent}")
        except (CabalError, httpx.HTTPError) as exc:
            raise ToolError(_SWITCHBOARD_DID_NOT_ANSWER) from exc

        seen_ids: set[str] = set()
        conversations: list[dict] = []
        for row in incoming + outgoing:
            task_id = row.get("task_id")
            if isinstance(task_id, str) and task_id in seen_ids:
                continue
            if isinstance(task_id, str):
                seen_ids.add(task_id)
            conversations.append(row)

        # Belt, same as the route: the switchboard already scoped both
        # queries, but this is the one definition of "participates".
        scoped = [c for c in conversations if _conversation_has_participant(c, agent)]

        entries: list[dict] = []
        for row in scoped:
            task_id = row.get("task_id")
            if not isinstance(task_id, str):
                continue
            try:
                detail = clients.cabal.get_conversation(task_id)
            except (CabalError, httpx.HTTPError) as exc:
                raise ToolError(_SWITCHBOARD_DID_NOT_ANSWER) from exc
            entries.extend(_flatten_conversation_entries(detail))

        entries.sort(key=lambda e: e["ts"])
        # `limit` is the caller's own ask (default 50); `_FEED_SCROLLBACK_
        # CAP` (200) is the room's own hard ceiling on flattened entries.
        # Whichever is smaller wins -- a caller may ask for less than the
        # room's own scrollback, never more. `max(0, ...)` floors a
        # non-positive `limit` at 0: without it, `cap=0` would slice
        # `entries[-0:]` (Python's -0 == 0) and return the WHOLE feed while
        # claiming truncation -- the opposite of a zero-length ask.
        cap = max(0, min(limit, _FEED_SCROLLBACK_CAP))
        truncated = len(entries) > cap
        if truncated:
            entries = entries[-cap:] if cap else []

        return {"entries": entries, "truncated": truncated}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_list_skills")
    async def kobo_list_skills(ctx: Context) -> dict:
        """List every skill in the org marketplace: id, description,
        when to use it, tags, owning team, version. Before creating a
        new skill, ALWAYS list and check fit here first -- prefer an
        existing skill over a near-twin. Any signed-in caller may list.
        """
        clients = app.state.clients
        cfg = app.state.config
        if not cfg.skills_bucket:
            raise ToolError(_SKILLS_BUCKET_NOT_CONFIGURED)

        try:
            catalog = read_marketplace_catalog(clients.storage, cfg.skills_bucket)
        except ClientError as exc:
            raise ToolError(_MARKETPLACE_DID_NOT_ANSWER) from exc
        if catalog is None:
            raise ToolError(_CATALOG_UNAVAILABLE)

        # Trust choice, deliberate: this index is machine-generated by the
        # org's own skills-publish pipeline (persona-metadata plan Task 1b),
        # not hand-authored per team -- unlike `marketplace.list_tools`'s
        # TOOL.md frontmatter below, which DOES defensively skip a
        # malformed entry (one team's typo must never take the whole
        # catalog down for every other team), a malformed entry here would
        # mean the publish pipeline itself is broken, which is exactly the
        # kind of failure that SHOULD be loud rather than silently skipped.
        return {
            "result": [
                {
                    "id": skill_id,
                    "description": entry.get("description", ""),
                    "when_to_use": entry.get("when_to_use", ""),
                    "tags": list(entry.get("tags", [])),
                    "owner": entry.get("owner", ""),
                    "version": entry.get("version", ""),
                }
                for skill_id, entry in sorted(catalog.items())
            ]
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_list_tools")
    async def kobo_list_tools(ctx: Context) -> dict:
        """List every tool in the shared tooling catalog: id, name,
        description, owning team, tags. Before creating a new tool,
        ALWAYS list and check fit here first -- prefer an existing tool
        over a near-twin. A new agent should always carry tooling; when
        nothing fits, see kobo_add_agent_tool and kobo_create_tool.
        Any signed-in caller may list.
        """
        clients = app.state.clients
        cfg = app.state.config
        try:
            tools = list_tools(clients.github, cfg)
        except MarketplaceError as exc:
            raise ToolError(str(exc)) from exc
        except ClientError as exc:
            raise ToolError(_MARKETPLACE_DID_NOT_ANSWER) from exc

        return {"result": tools}

    # Deferred import: `_endpoint`/`_off_loop`/`_parse` are `kobo_api.mcp.
    # tools.write`'s own helpers (see that module's docstring for the full
    # mechanism) -- a real read->write->read cycle exists: write.py line
    # ~331 module-level imports three constants FROM this module
    # (_CATALOG_UNAVAILABLE, _MARKETPLACE_DID_NOT_ANSWER,
    # _SKILLS_BUCKET_NOT_CONFIGURED). A module-level import would fail
    # loudly at import time, so this module IS imported at `kobo_api.mcp.
    # server` load time (from `register`'s own module-level position in
    # that file), well before `kobo_api.mcp.tools.write` is guaranteed to
    # have finished importing everything it does. Deferring the import to
    # inside `register` (which only ever runs once `build_mcp_server` is
    # actually called) sidesteps that ordering entirely.
    from kobo_api.mcp.tools.write import (
        _endpoint,
        _off_loop,
        _parse,
        _room_route_result,
    )

    search_personas_endpoint = _endpoint(app, "GET", "/api/personas/search")
    list_learnings_endpoint = _endpoint(app, "GET", "/api/agents/{name}/learnings")
    list_secrets_endpoint = _endpoint(app, "GET", "/api/agents/{name}/secrets")
    list_env_endpoint = _endpoint(app, "GET", "/api/agents/{name}/env")
    get_agent_endpoint = _endpoint(app, "GET", "/api/agents/{name}")
    checks_endpoint = _endpoint(app, "GET", "/api/agents/{name}/checks")
    models_endpoint = _endpoint(app, "GET", "/api/catalog/models")
    access_endpoint = _endpoint(app, "GET", "/api/agents/{name}/access")
    repositories_endpoint = _endpoint(app, "GET", "/api/agents/{name}/github/repositories")
    slack_endpoint = _endpoint(app, "GET", "/api/agents/{name}/slack")
    activity_endpoint = _endpoint(app, "GET", "/api/agents/{name}/activity")
    leases_endpoint = _endpoint(app, "GET", "/api/agents/{name}/leases")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_list_personas")
    async def kobo_list_personas(
        ctx: Context, q: str = "", team: str | None = None, scope: str = "mine"
    ) -> dict:
        """List personas in the room's catalog: id, name, description,
        owning team, tags, and whether it is a room builtin. `scope`
        "mine" (the default) is the shared personas plus those filed
        under your own groups (a room operator sees all); "all" is the
        whole catalog. Optional q matches name/description; optional team
        filters to one team. Before creating a new persona, ALWAYS list
        and check fit here first (try scope="all" too) -- prefer an
        existing persona over a near-twin. Any signed-in caller may list.
        """
        if scope not in ("mine", "all"):
            raise ToolError("scope must be mine or all")
        req = authz.room_request(app, get_user_email(ctx))
        session = app.state.session_factory()
        try:
            body = await _off_loop(
                search_personas_endpoint,
                request=req,
                q=q,
                team=team,
                scope=scope,
                session=session,
            )
        finally:
            session.close()
        return {"result": body["results"]}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_list_learnings")
    async def kobo_list_learnings(ctx: Context, agent: str, status: str = "all") -> dict:
        """List the learnings queued for `agent`: candidate memories the
        nightly summarizer proposed, awaiting a human verdict -- the same
        rows the room's own Learnings tab shows, straight from GET
        .../learnings. Optional status filters to one of
        pending/approved/denied/expired; the default "all" returns every
        row regardless of status. Visibility is checked before anything
        else, the same as kobo_get_agent: an agent you cannot see answers
        "agent not found". `can_decide` reports whether YOU may approve or
        deny a learning for this agent right now -- the identical gate
        kobo_decide_learning/kobo_decide_learnings_bulk enforce (owner,
        room operator, or a per-user grant) -- check it before assuming a
        decide call on one of these rows will actually succeed.
        """
        from kobo_api.main import _decider_or_403

        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        session = app.state.session_factory()
        try:
            response = await _off_loop(
                list_learnings_endpoint, name=agent, request=req, status=status, session=session
            )
            if response.status_code == 404:
                raise ToolError(_AGENT_NOT_FOUND)
            if response.status_code != 200:
                body = _parse(response)
                raise ToolError(
                    str(body.get("error", "that request could not be completed"))
                )
            body = _parse(response)

            try:
                agent_row = authz.visible_agent_or_not_found(session, app, email, agent)
            except authz.NotFound as exc:
                raise ToolError(_AGENT_NOT_FOUND) from exc

            return {
                "learnings": body["learnings"],
                "can_decide": _decider_or_403(agent_row, req, session) is None,
            }
        finally:
            session.close()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_list_agent_secrets")
    async def kobo_list_agent_secrets(ctx: Context, agent: str) -> dict:
        """List what `agent` runs with: the NAMES of its secrets, and its
        plain environment variables with their values.

        A secret's value is never returned here or anywhere else in the
        room, not even masked -- it is written once and read only by the
        running agent. What you get back is `secrets` (a list of key
        names), `secrets_updated_at` (when that list last changed, one
        date for the whole list), `secrets_available` (false in a room
        with no secrets platform configured, where secrets cannot be kept
        at all), and `env` (a name-to-value mapping of the variables that
        are not sensitive).

        `env_readable` is false for an agent that runs from its team's own
        repository unless you are a room operator: its variables live in
        that repository, the room shows them to operators only, and `env`
        comes back empty. Its secrets are still listed.

        You must own `agent` or be a room operator; an agent you cannot
        see answers "agent not found", the same as kobo_get_agent.
        """
        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        session = app.state.session_factory()
        try:
            secrets = await _off_loop(
                list_secrets_endpoint, name=agent, request=req, session=session
            )
            _refuse_agent_route(secrets)
            env = await _off_loop(
                list_env_endpoint, name=agent, request=req, session=session
            )
            # The secrets read above already settled that this caller may
            # see the agent, so a refusal here is not about who they are: it
            # is the operator-only variables of a connected repository, and
            # it must not take the secrets down with it as "agent not found".
            env_readable = env.status_code != 403
            if env_readable:
                _refuse_agent_route(env)
        finally:
            session.close()

        held = _parse(secrets)
        return {
            "secrets": held.get("keys", []),
            "secrets_updated_at": rfc3339_str(held["updated_at"]) if held.get("updated_at") else None,
            "secrets_available": bool(held.get("platform")),
            "delivered_as": held.get("secret_name"),
            "env": _parse(env).get("env", {}) if env_readable else {},
            "env_readable": env_readable,
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_list_models")
    async def kobo_list_models(ctx: Context) -> dict:
        """List the models this room offers: the ids `kobo_create_agent`
        and `kobo_set_agent_model` accept, each with its label. `default` is
        the one a new agent gets when none is named. In a room that takes
        its models from connected clouds each entry also says which cloud
        serves it and whether it can be picked now.
        """
        get_user_email(ctx)
        session = app.state.session_factory()
        try:
            return await _off_loop(models_endpoint, session=session)
        finally:
            session.close()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_get_agent_access")
    async def kobo_get_agent_access(
        ctx: Context, agent: str, repository_search: str | None = None
    ) -> dict:
        """What `agent` may reach outside the room: the Access tab, from
        chat. `grants` is what it holds, `pending` what waits for a
        reviewer, and `vendors` what it can ask for, each with its `id` and
        its tree of permission ids (what `kobo_request_access` takes).
        `can_request` says whether you may ask on its behalf. For an agent
        that runs its team's own code, `wiring` says whether its repository
        carries what it needs to use a grant.

        `github` is its repository set, when you may read it: `selected` is
        the set in force, `waiting` a pick held for review, `total` how many
        repositories the room's GitHub connection can see. Pass
        `repository_search` (part of a name) to get up to 50 `matches` to
        choose from with `kobo_set_agent_repositories`; the whole list is
        never returned.
        """
        req = authz.room_request(app, get_user_email(ctx))
        session = app.state.session_factory()
        try:
            access = await _off_loop(access_endpoint, name=agent, request=req, session=session)
            body = _room_route_result(access, agent)
            repos = await _off_loop(repositories_endpoint, name=agent, request=req, session=session)
        finally:
            session.close()
        github = None
        if repos.status_code == 200:
            held = _parse(repos)
            github = {
                "available": held.get("available"),
                "selected": held.get("selected", []),
                "waiting": held.get("waiting"),
                "total": held.get("total", 0),
                "note": held.get("note"),
            }
            if repository_search:
                needle = repository_search.strip().lower()
                github["matches"] = [
                    r["full_name"]
                    for r in held.get("repositories", [])
                    if needle in r["full_name"].lower()
                ][:_REPOSITORY_MATCHES]
        return {
            "agent": body.get("agent", agent),
            "grants": body.get("grants", []),
            "pending": body.get("pending", []),
            "vendors": body.get("vendors", []),
            "wiring": body.get("wiring"),
            "can_request": body.get("can_request", False),
            # Null when the set is not this caller's to read (the owner's,
            # a manager's or an operator's), or the read failed.
            "github": github,
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_get_slack")
    async def kobo_get_slack(ctx: Context, agent: str) -> dict:
        """Where `agent` stands with Slack. `room_makes_apps` says whether
        this room makes Slack apps itself. When it does, `state` is `none`
        (no app; `kobo_create_slack_app` makes one), `created` (made, and
        waiting for a person to open `install_url` and allow it in Slack) or
        `installed`, with the `workspace` it is in, the `channels` it
        listens to, its `wake`, the `wake_emoji` whose reaction also
        wakes it, and `command_public` (whether its slash command is answered
        in public channels it is not in). `update_due` true means the room asks
        Slack for more today than this app was allowed when it was
        installed; `kobo_update_slack_app` starts that. `legacy` marks an
        older app a person made by hand, and `own_app` an agent that runs
        its team's own code on its team's own Slack app.

        When `room_makes_apps` is false, Slack is the older paste flow: a
        person makes the app from the agent's manifest and
        `kobo_wire_slack` saves its tokens.
        """
        req = authz.room_request(app, get_user_email(ctx))
        session = app.state.session_factory()
        try:
            response = await _off_loop(slack_endpoint, name=agent, request=req, session=session)
            if response.status_code == 404 and _parse(response).get("error") != f"no such agent: {agent!r}":
                # The room does not make Slack apps. That is an answer, not
                # an error; say what the agent's page says instead.
                detail = await _off_loop(get_agent_endpoint, name=agent, request=req, session=session)
                _refuse_agent_route(detail)
                return {"agent": agent, "room_makes_apps": False, **_parse(detail)["slack"]}
        finally:
            session.close()
        view = _room_route_result(response, agent)
        view.pop("enabled", None)
        return {"agent": agent, "room_makes_apps": True, **view}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_agent_activity")
    async def kobo_agent_activity(ctx: Context, agent: str, days: int = 7, limit: int = 50) -> dict:
        """What `agent` did outside the room over the last `days` (1 to
        90): the Activity tab, from chat. `calls` is every tool call the
        gateway let through or refused, newest first, with the tool, the
        outcome and why a refusal was refused. `leases` is every credential
        it borrowed and the task it named. Each list is cut to `limit` (at
        most 200) and `*_total` says how many there were. A list the room
        cannot read comes back empty with `*_available` false and a note
        saying why, never as if nothing happened.
        """
        limit = max(1, min(limit, _ACTIVITY_ROWS_MAX))
        req = authz.room_request(app, get_user_email(ctx))
        session = app.state.session_factory()
        try:
            activity = await _off_loop(
                activity_endpoint, name=agent, request=req, days=days, session=session
            )
            _refuse_agent_route(activity)
            leases = await _off_loop(
                leases_endpoint, name=agent, request=req, days=days, session=session
            )
            _refuse_agent_route(leases)
        finally:
            session.close()
        calls, borrowed = _parse(activity), _parse(leases)
        return {
            "agent": agent,
            "days": days,
            "calls_available": calls.get("available", False),
            "calls_note": calls.get("note"),
            "calls_total": len(calls.get("rows", [])),
            "calls": calls.get("rows", [])[:limit],
            "leases_available": borrowed.get("available", False),
            "leases_note": borrowed.get("note"),
            "leases_total": len(borrowed.get("leases", [])),
            "leases": borrowed.get("leases", [])[:limit],
        }

    def _refuse_agent_route(response) -> None:
        """Turn one agent-scoped route's non-2xx into the ToolError this
        module's own table calls for.

        403 and 404 collapse to one sentence on purpose (the
        anti-enumeration rule `kobo_api.mcp.authz.NotFound` documents): an
        agent somebody else owns must not be tellable from one that does
        not exist. Everything else says what the route said, because it is
        about the caller's own request, and a 5xx says nothing about the
        cause at all.
        """
        if response.status_code // 100 == 2:
            return
        if response.status_code in (403, 404):
            raise ToolError(_AGENT_NOT_FOUND)
        if response.status_code >= 500:
            raise ToolError("the room did not answer. Try again.")
        raise ToolError(
            str(_parse(response).get("error", "that request could not be completed"))
        )
