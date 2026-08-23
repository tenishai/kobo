"""kobo's six MCP write tools (MCP server arc, Task 8): create_agent,
teach, chat, request_colleague, accept_invite, and wire_slack.

THE REUSE MECHANISM -- this is the entire design of this module. Every
tool below is a thin shim that calls the SAME route handler the room's own
HTTP surface calls, never a second implementation of a gate or a mutation
that already exists and is already tested in `kobo_api.main`. Concretely:

- `_endpoint(app, method, path)` walks `app.routes` (built once, at
  `create_app` time) and returns the matching route's `.endpoint` -- the
  plain, undecorated Python function FastAPI stored when `@app.post(...)`/
  `@app.put(...)` registered it (`route.endpoint`, NOT the ASGI-wrapping
  dependency-injection callable `APIRoute.get_route_handler()` builds
  separately). Calling that function directly, with real arguments instead
  of letting FastAPI resolve them from a live HTTP request, is exactly
  what every tool below does -- so every inline gate the route already
  carries (create's group-consent check, the owner-or-operator gates on
  teach/chat/invites/slack, the 404-before-403 visibility discipline)
  fires for real, unmodified, every single call. A route missing from
  `app.routes` at `register()` time raises immediately (`RuntimeError`) --
  a wiring bug, never a runtime surprise on some caller's first attempt.
- `authz.room_request(app, email)` (Task 5) builds the synthetic room
  `Request` every route's own gates already know how to read
  (`request.state.iap_email`, `request.app.state.config`/`.clients`) --
  the exact same bridge the read tools use, so a write tool's caller is
  authenticated and gated identically to a real HTTP request from that
  same email.
- A path parameter (`name`, `invite_id`) is passed as the handler's own
  keyword argument, never smuggled into the request or the payload model
  -- exactly how FastAPI itself would have resolved it from the URL.
- The pydantic payload model each route declares (`CreateAgentRequest`,
  `TeachPayload`, `ChatRequest`, `InviteCreateRequest`,
  `SlackConnectRequest`, `SlackChannelsRequest` -- all module-level in
  `kobo_api.main`) is built straight from the tool's own arguments and
  handed to the route function exactly as FastAPI's own body-parsing would
  have produced it.
- A session is opened off `app.state.session_factory()` (the same plain
  sessionmaker the read tools and `kobo_api.main.get_session` both read
  off `app.state`), passed to the route function directly, and closed in
  a `finally` -- the same lifecycle `get_session`'s dependency generator
  gives a real request, just driven by hand instead of FastAPI's
  dependency resolution.

Every route function below returns a `fastapi.responses.JSONResponse` in
every branch (verified against each route's own source) -- `_parse` reads
its `.body` (already-serialized JSON bytes) back into a dict, and the two
status-code translators below (`_agent_route_result` for the five
agent-scoped tools, `_create_result` for `kobo_create_agent`) turn that
status code into either the tool's return value or a `ToolError`.

## Status -> ToolError translation

**(A) The five agent-scoped tools** (`kobo_teach`, `kobo_chat`,
`kobo_request_colleague`, `kobo_accept_invite`, `kobo_wire_slack`) --
every one of these resolves an existing agent by name (or, for
`kobo_accept_invite`, an existing invite naming two agents) before doing
anything else, so the anti-enumeration property the read tools already
pin (`kobo_api.mcp.authz.NotFound`'s own docstring) applies here too:

| Route status | Tool outcome |
|---|---|
| 2xx | success -- parsed body becomes the tool's return dict |
| 403 or 404 | `ToolError("agent not found")` -- a visible-but-not-owned agent (403, the owner-or-operator gate) must never be told apart from one that plain does not exist (404, the visibility gate) or an invite naming nothing real (`_no_such_invite_404`, also 404). A route that ever answered "not visible" with a 409 would fold in here too, on the identical principle -- none of today's six routes do, but the rule is about the MEANING of the status, not just its number. |
| 409 (a real conflict, e.g. "invite is already accepted", "a colleague request ... is already pending", "agent is not ready to ... yet") | `ToolError(<the route's own message>)` -- the caller already has standing here (visibility passed, and for the not-ready case the caller can already see this exact state through `kobo_get_agent`), so the state itself is not a leak; it is the legitimate answer to "why didn't this work." |
| 422 (validation -- bad input shape, an unknown role, text too long, a malformed Slack token/app id, "an agent cannot request colleague access to itself", ...) | `ToolError(<the route's own message>)` -- this is about the CALLER's own request, not about any hidden resource. |
| 5xx (switchboard/relay/agent unreachable, Slack wiring failed) | `ToolError` with the house "did not answer, try again" phrasing -- the underlying cause is never echoed, matching every other switchboard-facing caller in this codebase (`kobo_api.mcp.tools.read`'s own `_SWITCHBOARD_DID_NOT_ANSWER`). |

**(B) `kobo_create_agent`** has no existing agent to protect -- there is
nothing yet to enumerate, so none of its non-2xx statuses collapse to a
generic "not found." Every one of them (403 group-consent rejection, 409
name-taken, 422 bad name/team/persona/skill/model/tags) surfaces the
route's own message verbatim, because every one of them is a legitimate
answer to something the CALLER asked for (a name, a persona id, a group)
being invalid or unavailable -- proof, not a hidden-resource leak. Only
the 503 launch-failure path (`_fail_launch`, whose body embeds the raw
exception text) maps to the same generic "did not answer" phrasing 5xx
gets everywhere else in this module, so an internal exception string
never reaches an MCP client verbatim.

## `kobo_wire_slack`'s connect/channels handling

`POST .../slack/connect` (`SlackConnectRequest`) requires THREE fields --
`app_id`, `bot_token`, `app_token` -- but this tool's own signature (fixed
by the design spec's tool-surface table) takes only `bot_token`,
`app_token`, and `channel_id`; there is no `app_id` parameter to fill that
fourth required field from, and the room's Slack self-service flow
(2026-08-17) means an app's id is only ever minted by a human at Slack's
own app-creation page, never by kobo itself -- there is no server-side
seam this tool could call to mint one.  The channel list, meanwhile, is
NOT part of `/slack/connect`'s own body at all (Two-lane Slack): it is
whatever `PUT .../slack/channels` (`SlackChannelsRequest`) has already
saved on the agent (`decode_channels(agent.slack_channels)`), read fresh
by `connect_slack` at call time.

This tool therefore does exactly two real-route calls, in this order,
reusing the SAME two handlers the room's own Slack tab drives:

1. `PUT .../slack/channels` with `channels=[channel_id]` -- a full
   replacement (never a delta, matching `SlackChannelsRequest`'s own
   contract), landing the caller's `channel_id` as the sole listened
   channel. Any failure here (not ready, not owner, a malformed channel
   id) is translated and the tool stops -- `connect` is never attempted.
2. The agent's OWN already-stored `slack_app_id` is read fresh off its row
   (a plain query through the same session, after the channels call has
   already proven the caller has standing) and used as `SlackConnectRequest.
   app_id` -- reusing self-service's own already-established app, never
   inventing one. If the agent has never been connected before at all
   (`slack_app_id` is still `None` -- self-service's own starting state),
   this tool raises `ToolError` with a plain instruction to connect a
   Slack app for this agent in the room's own web UI first: creating a
   NEW Slack app has no server-side seam to call at all (a human must
   visit Slack's own site), so first-time Slack setup is deliberately out
   of this tool's reach -- only ROTATING/re-supplying tokens for an
   already-connected agent is.
3. `POST .../slack/connect` with that `app_id` plus the caller's
   `bot_token`/`app_token` -- exactly the second half of what a human
   reconnecting in the UI would submit.

Neither token is ever echoed back: `connect_slack`'s own success body is
just `{"sha": ...}` (a git commit sha, not a secret), and this tool's
return value never includes anything from `bot_token`/`app_token`.
`kobo_api.mcp.audit.SENSITIVE_KEYS` already redacts both kwarg names
(`bot_token`, `app_token`) whole in the audit log; `channel_id` is not
sensitive and is logged as given, same as every other non-secret argument
in this module.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import BackgroundTasks
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from kobo_api.mcp import authz
from kobo_api.mcp.audit import audited
from kobo_api.mcp.server import get_user_email

# Shared with kobo_api.mcp.tools.read -- see that module's own module
# docstring for why this exact string is the one answer every agent-scoped
# tool gives for "doesn't exist" and "exists but you can't touch it" alike.
_AGENT_NOT_FOUND = "agent not found"

_DID_NOT_ANSWER = "The room did not answer. Try again shortly."

_SLACK_APP_NOT_CONNECTED = (
    "no Slack app is connected for this agent yet. Connect one for this "
    "agent in the room's web UI first, then wire tokens here."
)

_OK_STATUSES = (200, 201)


def _endpoint(app: Any, method: str, path: str) -> Callable[..., Any]:
    """The plain Python function `app.routes` recorded for `method`/`path`
    at `create_app` time -- see this module's own docstring for why this,
    and not a second implementation, is the whole point. Raised at
    `register()` time (not on first call) when nothing matches: a route
    that has moved or been renamed out from under this module is a wiring
    bug this arc must never discover from a caller's failed tool call.
    """
    for route in app.routes:
        route_path = getattr(route, "path", None)
        route_methods = getattr(route, "methods", None) or ()
        if route_path == path and method in route_methods:
            return route.endpoint
    raise RuntimeError(f"MCP write tools: no route wired for {method} {path!r}")


def _parse(response: JSONResponse) -> dict:
    """`response.body` is already-serialized JSON bytes (every route this
    module calls returns a `JSONResponse` in every branch) -- this is
    just reading that back into the dict the route itself built.
    """
    return json.loads(response.body)


def _agent_route_result(response: JSONResponse) -> dict:
    """Table (A) from this module's own docstring -- the shared status
    translation for every agent-scoped write tool.
    """
    if response.status_code in _OK_STATUSES:
        return _parse(response)
    if response.status_code in (403, 404):
        raise ToolError(_AGENT_NOT_FOUND)
    if response.status_code >= 500:
        raise ToolError(_DID_NOT_ANSWER)
    body = _parse(response)
    raise ToolError(str(body.get("error", "that request could not be completed")))


def _create_result(response: JSONResponse) -> dict:
    """Table (B) from this module's own docstring -- `kobo_create_agent`'s
    own status translation, distinct from the shared agent-scoped table
    above because there is no existing agent here to protect from
    enumeration.
    """
    if response.status_code in _OK_STATUSES:
        return _parse(response)
    if response.status_code >= 500:
        raise ToolError(_DID_NOT_ANSWER)
    body = _parse(response)
    raise ToolError(str(body.get("error", "that request could not be completed")))


def register(mcp: FastMCP, app: Any) -> None:
    """Registers the six write tools on `mcp`. Every route this module
    calls is looked up exactly once, here, at registration time -- never
    re-resolved per call.
    """
    create_agent_endpoint = _endpoint(app, "POST", "/api/agents")
    name_suggestions_endpoint = _endpoint(app, "POST", "/api/agents/name-suggestions")
    teach_endpoint = _endpoint(app, "POST", "/api/agents/{name}/teach")
    chat_endpoint = _endpoint(app, "POST", "/api/agents/{name}/chat")
    create_invite_endpoint = _endpoint(app, "POST", "/api/agents/{name}/invites")
    accept_invite_endpoint = _endpoint(app, "POST", "/api/invites/{invite_id}/accept")
    connect_slack_endpoint = _endpoint(app, "POST", "/api/agents/{name}/slack/connect")
    save_slack_channels_endpoint = _endpoint(app, "PUT", "/api/agents/{name}/slack/channels")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @audited("kobo_create_agent")
    async def kobo_create_agent(
        ctx: Context,
        who_they_are: str,
        group: str,
        name: str | None = None,
        skills: list[dict] | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        """Create a new agent in the room -- the same one-click create the
        room's own UI drives. `who_they_are` is the catalog persona id
        this agent should be built from (the room's persona catalog, e.g.
        "helpful-generalist"); `group` is the ACL group this agent
        belongs to, and you must already be a member of it (an operator
        may name any configured group). `name` is optional -- when
        omitted, the room's own naming station picks one for you.
        `skills`, when given, is a list of `{"id", "team"}` picks from the
        room's skills catalog; `tags` is a short list of purpose tags.
        Returns the agent's name and status; watch the room (or
        `kobo_get_agent`) for its checklist to finish in the background.
        """
        from kobo_api.main import CreateAgentRequest, NameSuggestionsRequest, SkillPick

        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        session = app.state.session_factory()
        try:
            resolved_name = name
            if resolved_name is None:
                # No second free-text field on this tool to seed
                # `what_they_do` with -- `who_they_are` stands in for both,
                # same as the route's own `finalize_suggestions` fallback
                # pool cares only that the seed is deterministic, not that
                # it is semantically distinct text. See module docstring.
                suggestion_response = name_suggestions_endpoint(
                    payload=NameSuggestionsRequest(
                        who_they_are=who_they_are, what_they_do=who_they_are
                    ),
                    session=session,
                )
                if suggestion_response.status_code not in _OK_STATUSES:
                    if suggestion_response.status_code >= 500:
                        raise ToolError(_DID_NOT_ANSWER)
                    body = _parse(suggestion_response)
                    raise ToolError(
                        str(body.get("error", "couldn't come up with a name right now"))
                    )
                suggestions = _parse(suggestion_response).get("suggestions") or []
                if not suggestions:
                    raise ToolError("couldn't come up with a name right now. Try again.")
                resolved_name = suggestions[0]["name"]

            try:
                payload = CreateAgentRequest(
                    name=resolved_name,
                    persona=who_they_are,
                    skills=[SkillPick(**pick) for pick in (skills or [])],
                    purpose_tags=list(tags or []),
                    group_name=group,
                )
            except (TypeError, ValidationError) as exc:
                raise ToolError(f"that request wasn't quite right: {exc}") from exc

            response = create_agent_endpoint(payload=payload, request=req, session=session)
        finally:
            session.close()

        body = _create_result(response)
        return {
            "name": body.get("name", resolved_name),
            "status": body.get("status"),
            "note": "Watch the room; the checklist finishes in the background.",
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @audited("kobo_teach")
    async def kobo_teach(ctx: Context, agent: str, note: str) -> dict:
        """Teach `agent` a new memory -- the same fast lane the room's own
        chat has: a distilled note that lands instantly if the agent is
        reachable right now, or gets archived into its intake folder to
        pick up on its next delivery otherwise. Open to anyone who can
        see `agent`, the same as chatting with it.
        """
        from kobo_api.main import TeachPayload

        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        session = app.state.session_factory()
        background_tasks = BackgroundTasks()
        try:
            response = teach_endpoint(
                name=agent,
                payload=TeachPayload(text=note),
                request=req,
                background_tasks=background_tasks,
                session=session,
            )
            body = _agent_route_result(response)
            # Mirrors what FastAPI itself does after a route handler that
            # took a `BackgroundTasks` parameter returns: run whatever got
            # queued (the instant lane's best-effort archive commit) --
            # see this module's own docstring; nothing here re-implements
            # `_archive_teach_note`, this just runs it.
            await background_tasks()
        finally:
            session.close()

        return {"filename": body.get("filename"), "mode": body.get("mode")}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @audited("kobo_chat")
    async def kobo_chat(ctx: Context, agent: str, message: str) -> dict:
        """Send `agent` one chat message and get its reply -- relayed
        through the switchboard exactly the same way the room's own chat
        panel does. Open to anyone who can see `agent`.
        """
        from kobo_api.main import ChatMessagePayload, ChatRequest

        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        session = app.state.session_factory()
        try:
            response = chat_endpoint(
                name=agent,
                payload=ChatRequest(messages=[ChatMessagePayload(role="user", text=message)]),
                request=req,
                session=session,
            )
        finally:
            session.close()

        body = _agent_route_result(response)
        return {"reply": body.get("reply")}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @audited("kobo_request_colleague")
    async def kobo_request_colleague(ctx: Context, agent: str, colleague: str) -> dict:
        """Ask `colleague` for cross-group colleague access on behalf of
        `agent` -- the same request-and-accept handshake the room's
        Access tab drives. You must own `agent` (or be a room operator);
        `colleague`'s own owner accepts or declines separately
        (`kobo_accept_invite`).
        """
        from kobo_api.main import InviteCreateRequest

        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        session = app.state.session_factory()
        try:
            response = create_invite_endpoint(
                name=agent,
                payload=InviteCreateRequest(to_agent=colleague),
                request=req,
                session=session,
            )
        finally:
            session.close()

        body = _agent_route_result(response)
        to_agent = body.get("to_agent") or {}
        return {
            "id": body.get("id"),
            "from_agent": body.get("from_agent"),
            "to_agent": to_agent.get("name"),
            "to_group": to_agent.get("group_name"),
            "status": body.get("status"),
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @audited("kobo_accept_invite")
    async def kobo_accept_invite(ctx: Context, invite_id: int) -> dict:
        """Accept a pending colleague request -- completing the handshake
        `kobo_request_colleague` started. You must own the RECEIVING
        agent (or be a room operator); the wire opens as soon as this
        succeeds.
        """
        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        session = app.state.session_factory()
        try:
            response = accept_invite_endpoint(invite_id=invite_id, request=req, session=session)
        finally:
            session.close()

        body = _agent_route_result(response)
        to_agent = body.get("to_agent") or {}
        return {
            "id": body.get("id"),
            "from_agent": body.get("from_agent"),
            "to_agent": to_agent.get("name"),
            "status": body.get("status"),
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
    @audited("kobo_wire_slack")
    async def kobo_wire_slack(
        ctx: Context, agent: str, bot_token: str, app_token: str, channel_id: str
    ) -> dict:
        """Wire (or rewire) Slack tokens for an agent that already has a
        Slack app connected -- see this module's own docstring for why
        this tool cannot create a NEW Slack app (that step is a human-only
        action at Slack's own site). Sets `channel_id` as the agent's sole
        listened channel and applies both tokens. You must own `agent` (or
        be a room operator). Neither token is ever returned or logged in
        the clear.
        """
        from kobo_api.main import SlackChannelsRequest, SlackConnectRequest
        from kobo_api.models import Agent

        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        session = app.state.session_factory()
        try:
            channels_response = save_slack_channels_endpoint(
                name=agent,
                payload=SlackChannelsRequest(channels=[channel_id]),
                request=req,
                session=session,
            )
            _agent_route_result(channels_response)

            row = session.query(Agent).filter_by(name=agent).first()
            app_id = row.slack_app_id if row is not None else None
            if not app_id:
                raise ToolError(_SLACK_APP_NOT_CONNECTED)

            connect_response = connect_slack_endpoint(
                name=agent,
                payload=SlackConnectRequest(app_id=app_id, bot_token=bot_token, app_token=app_token),
                request=req,
                session=session,
            )
            body = _agent_route_result(connect_response)
        finally:
            session.close()

        return {"agent": agent, "connected": True, "sha": body.get("sha")}
