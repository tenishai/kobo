"""kobo's four MCP read tools (MCP server arc, Task 7): whoami, list,
get, and feed. Every one of these is a thin shim over the room's own
choke points -- `kobo_api.mcp.authz` for visibility/admin, `kobo_api.main`
for the feed's own flattening pipeline -- never a second implementation
of logic that already exists and is already tested there.

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

from kobo_api.mcp import authz
from kobo_api.mcp.audit import audited
from kobo_api.mcp.server import get_user_email
from kobo_api.purpose_tags import decode_tags

# The one error message both agent-scoped tools raise for "doesn't exist"
# and "exists but you can't see it" alike -- see `kobo_api.mcp.authz.
# NotFound`'s own docstring for why those two cases must always answer
# identically. Named here once so `kobo_get_agent` and `kobo_feed` can
# never drift apart on the wording.
_AGENT_NOT_FOUND = "agent not found"

_SWITCHBOARD_NOT_CONFIGURED = "the switchboard is not configured"
_SWITCHBOARD_DID_NOT_ANSWER = "The switchboard did not answer. Try again shortly."


def register(mcp: FastMCP, app: Any) -> None:
    """Registers the four read tools on `mcp`. Each closes over `app` --
    the same FastAPI app `kobo_api.mcp.authz.room_request` builds its
    synthetic `Request` from, and the source of the session factory and
    switchboard client every tool below reaches through `app.state`.
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
        group, skills team, owner, status, and purpose. The same
        visibility rules the room's own agent list applies.
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
                }
                for agent in agents
            ]
        finally:
            session.close()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_get_agent")
    async def kobo_get_agent(ctx: Context, name: str) -> dict:
        """Get one agent's card: its persona, team, purpose, model,
        status, and status checklist. Answers "agent not found" both for
        a name that does not exist and for one you are not allowed to
        see -- the two are never told apart.
        """
        email = get_user_email(ctx)
        session = app.state.session_factory()
        try:
            try:
                agent = authz.visible_agent_or_not_found(session, app, email, name)
            except authz.NotFound as exc:
                raise ToolError(_AGENT_NOT_FOUND) from exc

            return {
                "name": agent.name,
                "persona": agent.persona,
                "group": agent.group_name,
                "team": agent.team,
                "owner": agent.created_by,
                "purpose": decode_tags(agent.purpose_tags),
                "capability": agent.capability,
                "model": agent.model_id,
                "status": agent.status,
                "steps": [
                    {"name": step.name, "status": step.status, "error": step.error}
                    for step in agent.steps
                ],
                "first_reply": agent.first_reply,
                "error": agent.error,
                "created_at": agent.created_at.isoformat(),
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
