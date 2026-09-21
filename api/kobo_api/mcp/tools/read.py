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
from kobo_api.marketplace import MarketplaceError, list_tools
from kobo_api.mcp import authz
from kobo_api.mcp.audit import audited
from kobo_api.mcp.server import get_user_email
from kobo_api.purpose_tags import decode_tags
from kobo_api.search import read_marketplace_catalog
from kobo_api.slack_state import decode_channels, derive_state

# The one error message both agent-scoped tools raise for "doesn't exist"
# and "exists but you can't see it" alike -- see `kobo_api.mcp.authz.
# NotFound`'s own docstring for why those two cases must always answer
# identically. Named here once so `kobo_get_agent` and `kobo_feed` can
# never drift apart on the wording.
_AGENT_NOT_FOUND = "agent not found"

_SWITCHBOARD_NOT_CONFIGURED = "the switchboard is not configured"
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
    """Registers this module's eight read tools on `mcp`. Each closes over
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
        """Get one agent's card: its kind, persona, team, purpose, model,
        status, and status checklist -- plus its skills, Slack wiring,
        avatar/repo facts, and pending-learnings state, the same detail
        the room's own agent page shows. A `connected` agent is a team's
        own repo the room only deploys, so its persona, skills and chat
        fields carry nothing to act on. Answers "agent not found" both
        for a name that does not exist and for one you are not allowed to
        see -- the two are never told apart.
        """
        # Deferred import: kobo_api.main is a large module, and the same
        # circularity concern this module's own docstring already
        # documents for kobo_feed's imports from kobo_api.main applies
        # here too -- this module is imported at kobo_api.mcp.server load
        # time, well before kobo_api.main is guaranteed to have finished
        # importing everything it does.
        from kobo_api.main import _decider_or_403, _model_label, _operator_block
        from kobo_api.models import LEARNING_PENDING, AgentLearning

        email = get_user_email(ctx)
        cfg = app.state.config
        session = app.state.session_factory()
        try:
            try:
                agent = authz.visible_agent_or_not_found(session, app, email, name)
            except authz.NotFound as exc:
                raise ToolError(_AGENT_NOT_FOUND) from exc

            # Same synthetic room Request every write tool already builds
            # (`authz.room_request`'s own docstring) -- `_operator_block`/
            # `_decider_or_403` below were both written against a real HTTP
            # `Request`, and this is the one bridge that lets them run
            # unmodified for an MCP caller too.
            req = authz.room_request(app, email)

            channels = decode_channels(agent.slack_channels)
            slack = {
                "app_id": agent.slack_app_id,
                "channels": channels,
                "state": derive_state(channels, wired=agent.slack_wired),
            }

            return {
                "name": agent.name,
                "persona": agent.persona,
                "group": agent.group_name,
                "team": agent.team,
                "owner": agent.created_by,
                "purpose": decode_tags(agent.purpose_tags),
                "capability": agent.capability,
                "model": agent.model_id,
                "model_label": _model_label(cfg, agent.model_id),
                "skills": agent.skills,
                "status": agent.status,
                "kind": agent.kind,
                "repo_full_name": agent.repo_full_name,
                "steps": [
                    {"name": step.name, "status": step.status, "error": step.error}
                    for step in agent.steps
                ],
                "first_reply": agent.first_reply,
                "error": agent.error,
                "created_at": agent.created_at.isoformat(),
                "slack": slack,
                "destroy_visible": _operator_block(req, cfg) is None,
                "has_avatar": agent.avatar_png is not None,
                "pending_learnings": session.query(AgentLearning)
                .filter_by(agent_name=agent.name, status=LEARNING_PENDING)
                .count(),
                "can_decide_learnings": _decider_or_403(agent, req, session) is None,
            }
        finally:
            session.close()

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
            raise ToolError(_SWITCHBOARD_NOT_CONFIGURED)

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
    from kobo_api.mcp.tools.write import _endpoint, _off_loop, _parse

    search_personas_endpoint = _endpoint(app, "GET", "/api/personas/search")
    list_learnings_endpoint = _endpoint(app, "GET", "/api/agents/{name}/learnings")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_list_personas")
    async def kobo_list_personas(
        ctx: Context, q: str = "", team: str | None = None
    ) -> dict:
        """List every persona in the room's catalog: id, name,
        description, owning team, tags, and whether it is a room
        builtin. Optional q matches name/description; optional team
        filters to one team. Before creating a new persona, ALWAYS
        list and check fit here first -- prefer an existing persona
        over a near-twin. Any signed-in caller may list.
        """
        session = app.state.session_factory()
        try:
            body = await _off_loop(search_personas_endpoint, q=q, team=team, session=session)
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
