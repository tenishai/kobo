"""kobo's MCP write tools: the original six from the MCP server arc (Task
8) -- create_agent, teach, chat, request_colleague, accept_invite, and
wire_slack -- plus `kobo_set_agent_skills` (MCP marketplace wave Task 3,
staging/removing marketplace catalog skills on an owned agent),
`kobo_add_agent_tool` (Task 4, staging a `bundle/tools/*.py` file on an
owned agent, from the marketplace or given inline), `kobo_create_skill`/
`kobo_create_tool` (Task 5, publishing a NEW skill or tool into the org
marketplace catalog itself, membership-gated -- see that pair's own
docstring section below), `kobo_request_repo_access`/`kobo_list_repo_access`/
`kobo_decide_repo_access` (the agent PR broker's request/list/decide trio,
gating which GitHub repositories an agent's own github.open_pr tool may
mint scoped tokens against), `kobo_create_persona` (persona wave Task
5, publishing a NEW persona into the room's own catalog -- see that tool's
own docstring section below), and the write half of the MCP learnings
trio (Task 8) -- `kobo_decide_learning`/`kobo_decide_learnings_bulk` --
its read half, `kobo_list_learnings`, lives in `kobo_api.mcp.tools.read`
since it never mutates, the same split every other read/write pair in
this pair of modules already holds.

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
its `.body` (already-serialized JSON bytes) back into a dict, and the
four status-code translators below (`_agent_route_result` for the
agent-scoped tools, `_skill_route_result` for `kobo_set_agent_skills`
alone, `_learning_route_result` for `kobo_decide_learning` alone,
`_create_result` for `kobo_create_agent`) turn that status code into
either the tool's return value or a `ToolError`.

## Status -> ToolError translation

**(A) The agent-scoped tools** (`kobo_teach`, `kobo_chat`,
`kobo_request_colleague`, `kobo_accept_invite`, `kobo_wire_slack`,
`kobo_set_agent_skills`, `kobo_add_agent_tool`, `kobo_request_repo_access`,
`kobo_decide_learning`, `kobo_decide_learnings_bulk`) -- every one of
these resolves an existing agent by name (or, for `kobo_accept_invite`, an
existing invite naming two agents) before doing anything else, so the
anti-enumeration property the read tools already pin
(`kobo_api.mcp.authz.NotFound`'s own docstring) applies here too.
`kobo_set_agent_skills`, `kobo_decide_learning`, and `kobo_decide_
learnings_bulk` each carry their own narrow, documented, operator-ruled
deviation from this table -- see their own sections below.

| Route status | Tool outcome |
|---|---|
| 2xx | success -- parsed body becomes the tool's return dict |
| 403 or 404 | `ToolError("agent not found")` -- a visible-but-not-owned agent (403, the owner-or-operator gate) must never be told apart from one that plain does not exist (404, the visibility gate) or an invite naming nothing real (`_no_such_invite_404`, also 404). A route that ever answered "not visible" with a 409 would fold in here too, on the identical principle -- none of today's nine routes do, but the rule is about the MEANING of the status, not just its number. |
| 409 (a real conflict, e.g. "invite is already accepted", "a colleague request ... is already pending", "agent is not ready to ... yet") | `ToolError(<the route's own message>)` -- the caller already has standing here (visibility passed, and for the not-ready case the caller can already see this exact state through `kobo_get_agent`), so the state itself is not a leak; it is the legitimate answer to "why didn't this work." |
| 422 (validation -- bad input shape, an unknown role, text too long, a malformed Slack token/app id, "an agent cannot request colleague access to itself", ...) | `ToolError(<the route's own message>)` -- this is about the CALLER's own request, not about any hidden resource. |
| 5xx (switchboard/relay/agent unreachable, Slack wiring failed) | `ToolError` with the house "did not answer, try again" phrasing -- the underlying cause is never echoed, the same generic-error discipline `kobo_api.mcp.tools.read` follows. The write side says "The room did not answer" rather than read's switchboard-specific text, because a write 5xx may come from gojira or Slack, not only the switchboard. |

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

## `kobo_set_agent_skills`'s catalog resolution and partial application

Unlike this module's other five agent-scoped tools, `kobo_set_agent_skills`
loops over TWO caller-given lists (`add`, then `remove`), each entry a bare
skill id -- never a `{"id", "team"}` pair the way `kobo_create_agent`'s own
`skills` argument takes one. `POST .../skills/catalog`'s own
`CatalogSkillPayload` needs a `team`, though, so for every id in `add` this
tool reads `kobo_api.search.read_marketplace_catalog` (once, up front, the
same hoisted-read idiom `kobo_api.skills.add_catalog_skill` itself already
uses) and resolves that id's `team` from the catalog entry's own `owner`
field -- `"shared"` when the entry carries none. An id absent from the
catalog raises `ToolError` naming it before THAT id's own route call --
this tool's own gate, not a status table (A) translates, since there is no
route response yet to translate for that id. This is a PER-id check, run
just before each add in the loop, NOT a single upfront scan of the whole
`add` list: an earlier id in the same call may already have been applied
(and stays applied) before a later, unknown id raises -- see partial
application below. A `None` catalog (`kobo_api.mcp.tools.read`'s own
"unavailable" wording, imported rather than duplicated), a `ClientError`
off the storage read (that module's own sanitized "did not answer"
wording, same reuse), or an unconfigured `skills_bucket` (this module's
own `_SKILLS_BUCKET_NOT_CONFIGURED`, mirroring that same read tool's own
guard) all raise before any add is attempted, since none of those three
can be blamed on any one id.

Each id's own add/remove route call, once made, is translated through
table (A) with ONE documented deviation (operator-ruled, MCP marketplace
wave Task 3 review): `_skill_route_result` -- used by this tool alone, in
place of `_agent_route_result` -- surfaces a 404 verbatim instead of
folding it into "agent not found" when, and only when, that 404's body is
one of the two skill-scoped "not found" messages the underlying routes
raise AFTER their own visibility/ownership gate has already passed --
`"skill not found: <id>"` (`delete_skill`'s `remove_skill` call, a
`remove` id this agent doesn't carry) or `"skill no longer in the
catalog: <id>"` (`add_skill_from_catalog`'s `add_catalog_skill` call, an
`add` id the catalog stopped serving between this tool's own catalog read
and the route's own). Both fire strictly POST-gate -- the caller already
has standing on this exact agent by the time either can happen -- so
folding them into the generic "agent not found" would carry zero
enumeration value while actively hiding which id failed; every OTHER 404
(the gate's own "not visible") and every 403 still normalize exactly like
every other tool in this module.

Application is deliberately partial-tolerant: entries already applied
before a later one fails stay applied (the routes underneath already
committed them), and the `ToolError` a failing id raises is that id's own
route message (e.g. `"the agent already carries a skill named '<id>'"` on
a 409 collision, or either skill-scoped 404 above) -- always naming the
id that failed, never a generic mid-loop failure.

## `kobo_add_agent_tool`'s source arbitration

Unlike every other tool in this module, `kobo_add_agent_tool` accepts TWO
mutually exclusive ways to name the file it stages: a marketplace `tool_id`
(fetched from the marketplace repo via `marketplace.fetch_tool_py`, Task
1) or an inline `filename` plus its `content`. Exactly one shape is
accepted -- `tool_id` alone, or `filename` together with `content` -- and
this is checked FIRST, before any route or marketplace call, using this
module's own gate (not table (A), since there is no route response yet to
translate): `ToolError(_EXACTLY_ONE_SOURCE)` for neither, for both, or for
only half of the inline pair (a `filename` with no `content`, or vice
versa).

The marketplace path resolves `(filename, content)` via `fetch_tool_py`
before ever touching the agent: a `MarketplaceError` (an invalid `tool_id`
shape, an unconfigured marketplace repo, or no such tool) becomes a
`ToolError` with that message preserved verbatim, and a `ClientError` (the
underlying clone's own transport failure) becomes the same sanitized
`_MARKETPLACE_DID_NOT_ANSWER` wording `kobo_set_agent_skills`'s own
catalog read already uses, imported rather than duplicated.

Both paths, once `(filename, content)` is in hand, call the SAME upload
route (`POST .../tooling`, `main.py:3366`) this module's own docstring
mechanism describes -- the route owns the visibility/ownership/readiness
gates, the tooling validator (size/UTF-8/non-empty/ast/import-allowlist/
entrypoint hook), and the runtime-image re-pin, exactly once, for either
source. The response is translated through the shared `_agent_route_result`
table (A) -- the plain table every other agent-scoped tool here uses, NOT
`_skill_route_result`'s one documented deviation, since the upload route
carries no post-gate not-found case of its own to unfold. The route's own
success body is just `{"sha": ...}`; this tool's own return value adds back
`filename` (the caller's own `tool_id`-derived or given name, never
something the route echoes) alongside it.

`content` -- the raw source of a tool a caller is staging -- joins
`kobo_api.mcp.audit.SENSITIVE_KEYS` the same way `kobo_wire_slack`'s
`bot_token`/`app_token` already do: never stored raw in an audit row.

## `kobo_create_skill`/`kobo_create_tool`'s membership gate (MCP marketplace
## wave, Task 5)

Unlike every other tool in this module, these two call `kobo_api.
marketplace.create_skill`/`create_tool` DIRECTLY -- there is no existing
route for either (the marketplace door has no HTTP surface of its own),
so this module's own reuse mechanism (this docstring's opening section)
does not apply here: the membership gate that stands in for a route's own
visibility/ownership check is written once, right here, as
`_require_team_membership`.

`marketplace.create_skill`/`create_tool` publish by direct commit with NO
review gate (operator ruling 1, `kobo_api.marketplace`'s own module
docstring) -- so the one thing standing between any signed-in caller and
a same-day, org-wide publish under an arbitrary team's name is this
module's own membership check: `_require_team_membership(app, email,
team)` raises before either marketplace function is ever called.
`authz.is_admin` short-circuits it entirely (an operator may publish under
any team, the same "operator may name any configured group" ruling
`kobo_create_agent`'s own group-consent check already holds); otherwise
`clients.groups.groups_for(email)` must contain `team` exactly. A `None`
answer (the resolver could not be reached, or none is configured at all)
refuses -- fail closed, matching `kobo_api.main`'s own group-consent gate
for `kobo_create_agent`'s `group_name` (a resolver that cannot answer
right now grants nothing, never "assume it's fine").

`kobo_create_tool` carries ONE further gate beside that one, in the same
before-any-write position (Task 6 review, operator ruled): a `tool_id`
starting with `marketplace.BASELINE_TOOL_PREFIX` is refused
(`_BASELINE_PREFIX_RESERVED`) unless `authz.is_admin` says the caller is a
room operator. Every other tool in the marketplace is opt-in per agent; a
`baseline-` one is staged into EVERY agent created afterward by
`kobo_api.steps._stage_baseline_tools`, so publishing one raises the whole
room's floor of tooling -- that is an operator's call, not a team member's.
`kobo_create_skill` has no such gate: there is no baseline SKILL pack.

Once membership clears, both tools call straight through with
`author=email` (the commit message's own attribution, `marketplace.
create_skill`/`create_tool`'s own docstrings) and translate the two typed
failures the same way `kobo_add_agent_tool`'s marketplace path already
does: a `MarketplaceError` (bad id shape, a frontmatter owner conflict, a
failed tooling validation, or an "already exists" collision) becomes a
`ToolError` with that message preserved verbatim; a `ClientError` off the
clone itself becomes the same sanitized `_MARKETPLACE_DID_NOT_ANSWER`
wording every other marketplace-transport failure in this arc already
uses.

The two tools' own `note` fields deliberately differ, matching how each
catalog is actually served: `kobo_list_skills` reads a periodically
refreshed bucket index (`kobo_api.search.read_marketplace_catalog`), so
`kobo_create_skill`'s note says the new entry appears after the publish
pipeline and that refresh finish; `kobo_list_tools` reads the marketplace
repo directly on every call (`marketplace.list_tools`'s own docstring), so
`kobo_create_tool`'s note says the new entry is visible there immediately,
no pipeline involved. Both tools carry `destructiveHint=False`: unlike
`kobo_set_agent_skills`/`kobo_add_agent_tool`, neither ever overwrites
anything -- an id already taken is refused (`MarketplaceError`), never
replaced.

`content` -- the skill body or tool source a caller is publishing -- is
already in `kobo_api.mcp.audit.SENSITIVE_KEYS` (added by Task 4 for
`kobo_add_agent_tool`'s own `content`); both tools below reuse that same
kwarg name, so it redacts here for free, no new entry needed.

## `kobo_create_persona` (persona wave, Task 5)

Unlike `kobo_create_skill`/`kobo_create_tool` just above, `kobo_create_persona`
DOES call a real route (`POST /api/personas`, `create_persona_route` in
`kobo_api.main`) -- the persona door has always had an HTTP surface of its
own, so this tool is a plain shim exactly like `kobo_create_agent`: table
(B) (`_create_result`), no existing catalog entry to protect from
enumeration, every non-2xx status (the route's own 403 team-membership
refusal, 409 name-taken, 422 slug-law/render-overflow) surfaces verbatim.
The route's own `_persona_team_gate` -- commons-open for `team="shared"`,
membership-gated for any other name -- is deliberately NOT this module's
`_require_team_membership` (that gate's own docstring cross-references
this one): the persona axis and the marketplace axis are two different
gates on two different resources, and only the route's own gate fires
here, never a second implementation of it.

## `kobo_decide_learning`/`kobo_decide_learnings_bulk` (MCP learnings trio,
## Task 8)

The verdict half of the daily-learnings spec's decide surface (`kobo_
list_learnings`, this pair's read-only sibling, lives in `kobo_api.mcp.
tools.read`). Both ride `kobo_api.main._decide_learning` -- lifted to
module level for this task from a closure that used to live inside
`create_app` -- the SAME shared verdict path `approve_learning`/
`deny_learning`/`bulk_decide_learnings` call, so every gate it already
enforces (`_ready_agent_or_error`'s 404/409, `_decider_or_403`'s
owner-or-operator-or-grant 403, the row's own 404/409) fires unmodified
for an MCP caller too, exactly this module's whole reuse mechanism.

`kobo_decide_learning` (one id) has no existing HTTP route of its own to
look up via `_endpoint`: the room's own two single-verdict routes each
hardcode their own action (`POST .../approve`, `POST .../deny`), neither
taking `action` as a caller-supplied value the way this tool does. So
this tool calls `_decide_learning` directly (imported straight from
`kobo_api.main`, deferred the same way every other main.py-only symbol in
this module already is) rather than through `_endpoint` -- and validates
`action` itself, BEFORE any call, since `_decide_learning`'s own body
only branches on `action == "deny"` and silently treats anything else as
an approve. `kobo_decide_learnings_bulk` (many ids), in contrast, DOES
have a real route (`POST .../learnings/bulk`, `LearningsBulkPayload`
already carries `action: Literal["approve", "deny"]`), so it is a plain
shim exactly like every other route-backed tool here -- `action`'s
vocabulary is the payload model's own job, not a second check.

Status translation differs sharply between the two, and only ONE of them
goes through this module's usual table (A):

`kobo_decide_learning` (single id) DOES translate, through a new
`_learning_route_result` -- table (A) (`_agent_route_result`) with one
documented, operator-ruled deviation mirroring `kobo_set_agent_skills`'s
own: a 404 whose body is exactly "no such learning" surfaces that message
verbatim rather than folding into `_AGENT_NOT_FOUND`, since that 404 fires
strictly POST-gate (the caller already cleared the ready-and-decide gate
on THIS agent by the time the row lookup runs), so folding it would hide
a plain bad `learning_id` behind the generic agent answer for zero
enumeration benefit. Every other 404 (the gate's own not-visible) and
every 403 (the decide gate's own refusal) still fold into `_AGENT_NOT_
FOUND` exactly like every other agent-scoped tool in this module.

`kobo_decide_learnings_bulk` (many ids) does NOT translate at all -- this
is deliberate, per the design brief's own "per-id status codes verbatim."
Its route (`bulk_decide_learnings`) never fails at the top level; it
always answers 200 with a `results` list, one entry per id, and each
entry's `status_code` is whatever `_decide_learning` itself returned for
that one id, untouched. So a per-id 403 (the decide gate's own refusal)
reaches an MCP caller as a bare 403 in `results`, NOT folded into "agent
not found" the way the single-id tool folds it -- the anti-enumeration
fold this module holds everywhere else does not apply per-entry inside a
bulk result, only to the overall response shape (which bulk's route never
varies). See that tool's own docstring for the full status-code table.

Approve rides the teach lane byte-for-byte, same as `kobo_teach`: a
`BackgroundTasks()` instance threads through the call, and `await
background_tasks()` afterward runs whatever the instant lane queued (the
background archive commit) -- nothing here re-implements
`_archive_teach_note`, this just runs it, same as `kobo_teach`/
`kobo_add_agent_tool` already do. For the bulk tool, ONE shared
`BackgroundTasks()` instance threads through every id `_decide_learning`
processes internally, so a single `await background_tasks()` after the
whole batch runs every id's own queued archive commit.

`kobo_decide_learnings_bulk` honors at most 50 ids, matching the room's
own bulk route (`payload.ids[:50]`, unchanged): a caller passing more
gets the first 50 silently decided, never an error naming the excess --
existing route behavior, byte-identical, not a new cap this tool invents.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import partial
from typing import Any

import anyio.to_thread
from fastapi import BackgroundTasks
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from kobo_api.clients import ClientError
from kobo_api.marketplace import (
    BASELINE_TOOL_PREFIX,
    MarketplaceError,
    create_skill,
    create_tool,
    fetch_tool_py,
)
from kobo_api.mcp import authz
from kobo_api.mcp.audit import audited
from kobo_api.mcp.server import get_user_email
from kobo_api.mcp.tools.read import (
    _CATALOG_UNAVAILABLE,
    _MARKETPLACE_DID_NOT_ANSWER,
    _SKILLS_BUCKET_NOT_CONFIGURED,
)
from kobo_api.search import read_marketplace_catalog

# Shared with kobo_api.mcp.tools.read -- see that module's own module
# docstring for why this exact string is the one answer every agent-scoped
# tool gives for "doesn't exist" and "exists but you can't touch it" alike.
_AGENT_NOT_FOUND = "agent not found"

_DID_NOT_ANSWER = "The room did not answer. Try again shortly."

_SLACK_APP_NOT_CONNECTED = (
    "no Slack app is connected for this agent yet. Connect one for this "
    "agent in the room's web UI first, then wire tokens here."
)

# The two stable message prefixes `kobo_api.skills.SkillsError` carries
# through `delete_skill`/`add_skill_from_catalog` for a POST-GATE 404 (see
# this module's own `kobo_set_agent_skills` docstring section for the
# operator-ruled reasoning): both fire only after the shared visibility/
# ownership gate has already passed, so `_skill_route_result` surfaces
# them verbatim rather than folding them into `_AGENT_NOT_FOUND` the way
# every other 404 in this module still does.
_SKILL_NOT_FOUND_PREFIX = "skill not found:"
_SKILL_GONE_FROM_CATALOG_PREFIX = "skill no longer in the catalog:"

# `kobo_add_agent_tool`'s own source-arbitration gate -- see this module's
# own docstring section for exactly which shapes this covers.
_EXACTLY_ONE_SOURCE = "pass exactly one source: tool_id, or filename with content"

# `kobo_api.main._decide_learning`'s own POST-GATE "no such learning" 404
# (an unknown learning_id on an agent the caller already cleared the
# ready-and-decide gate for) -- see this module's own `kobo_decide_learning`/
# `kobo_decide_learnings_bulk` docstring section for the operator-ruled
# reasoning `_learning_route_result` below acts on.
_LEARNING_NOT_FOUND = "no such learning"

# `kobo_decide_learning`'s own action-vocabulary gate -- there is no route
# of its own to translate a bad value through (see this module's own
# docstring section), so this is checked here, before any call.
_INVALID_LEARNING_ACTION = "action must be 'approve' or 'deny'"


async def _off_loop(fn: Callable, /, **kwargs: Any) -> Any:
    """Run one of main.py's sync route functions in a worker thread.

    Every tool below wraps a sync route function; awaited directly from an
    async tool body those run ON the event loop, freezing the whole room --
    healthz included -- for the call's full duration. A chat that fans out
    to a colleague chain holds the loop for minutes, the liveness probe
    times out, and kubelet kills the pod (the 2026-08-27 exit-137). This
    one hop restores exactly what the HTTP layer gives these same
    functions: FastAPI runs sync routes in this same anyio threadpool.
    """
    return await anyio.to_thread.run_sync(partial(fn, **kwargs))

# `kobo_create_tool`'s baseline-pack reservation (Task 6 review, operator
# ruled). Publishing a `baseline-` id is not a team-scoped act like every
# other tool: `kobo_api.steps._stage_baseline_tools` stages whatever carries
# that prefix into EVERY agent created afterward, so it changes the room's
# floor of tooling for everyone. Room operators only.
_BASELINE_PREFIX_RESERVED = (
    f"tool ids starting with {BASELINE_TOOL_PREFIX!r} are reserved for room operators"
)

# `_require_team_membership`'s own fail-closed answer for a `None` resolve
# (the resolver is unreachable right now, or none is configured at all) --
# see this module's own docstring section on `kobo_create_skill`/
# `kobo_create_tool` for why this refuses rather than assumes membership.
_DIRECTORY_LOOKUP_UNAVAILABLE = "directory lookup unavailable; try again"

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


def _skill_team(entry: dict) -> str:
    """`kobo_set_agent_skills`'s own `team` resolution for one marketplace
    catalog entry: the entry's own `owner` when it's a non-blank string,
    else `"shared"` -- an add-tool caller passes bare skill ids, never a
    team, unlike the room's own web UI (which already knows which team
    owns a given catalog listing before it ever calls the route). Mirrors
    `kobo_api.search._entry_team`'s own owner-precedence discipline for a
    malformed (missing, non-string, or blank) `owner`, just with `"shared"`
    as the fallback rather than that function's `""`.
    """
    owner = entry.get("owner")
    if isinstance(owner, str) and owner.strip():
        return owner
    return "shared"


def _bad_request(exc: Exception) -> str:
    """The refusal for a payload model this module built by hand, from the
    field NAMES that failed and nothing else.

    Never `str(exc)`. Pydantic renders the offending input in its own
    message -- `Input should be a valid string [type=string_type,
    input_value=...]` -- and a tool argument here can be a secret value or
    a whole pasted file. `kobo_api.main._validation_error_message` is the
    same wall the HTTP surface put up for the same reason (a Slack token
    echoed back out of a 422), so it is shared rather than written twice.
    """
    from kobo_api.main import _validation_error_message

    if not hasattr(exc, "errors"):
        # A `TypeError` from building the model with the wrong argument
        # shape entirely. Its text is about the CALL, not about a value,
        # but it is not this module's to guess at either, so it gets the
        # same generic sentence a field-less validation failure gets.
        return "that request wasn't quite right. Check the fields and try again"
    return _validation_error_message(exc)


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


def _skill_route_result(response: JSONResponse) -> dict:
    """`kobo_set_agent_skills`'s own status translation -- table (A) with
    the one documented, operator-ruled deviation this module's own
    `kobo_set_agent_skills` docstring section explains: a 404 whose body
    is one of the two skill-scoped "not found" messages
    (`_SKILL_NOT_FOUND_PREFIX` from `delete_skill`, `_SKILL_GONE_FROM_
    CATALOG_PREFIX` from `add_skill_from_catalog`) surfaces that message
    verbatim instead of folding into `_AGENT_NOT_FOUND` -- both fire only
    AFTER the shared visibility/ownership gate has already passed, so they
    carry zero enumeration value. Every other 404 (the gate's own "not
    visible") and every 403 fall through to `_agent_route_result` and
    normalize exactly like every other tool in this module.
    """
    if response.status_code == 404:
        body = _parse(response)
        message = str(body.get("error", ""))
        if message.startswith((_SKILL_NOT_FOUND_PREFIX, _SKILL_GONE_FROM_CATALOG_PREFIX)):
            raise ToolError(message)
    return _agent_route_result(response)


def _learning_route_result(response: JSONResponse) -> dict:
    """`kobo_decide_learning`'s own status translation -- table (A) with
    one documented, operator-ruled deviation mirroring `_skill_route_
    result`'s own: a 404 whose body is exactly `_LEARNING_NOT_FOUND`
    surfaces that message verbatim instead of folding into
    `_AGENT_NOT_FOUND` -- it fires only AFTER the shared ready/decide gate
    has already passed (see this module's own `kobo_decide_learning`/
    `kobo_decide_learnings_bulk` docstring section), so it carries zero
    enumeration value to hide. Every other 404 (the gate's own "not
    visible") and every 403 fall through to `_agent_route_result` and
    normalize exactly like every other tool in this module.
    """
    if response.status_code == 404:
        body = _parse(response)
        if str(body.get("error", "")) == _LEARNING_NOT_FOUND:
            raise ToolError(_LEARNING_NOT_FOUND)
    return _agent_route_result(response)


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


def _require_team_membership(app: Any, email: str, team: str) -> None:
    """`kobo_create_skill`/`kobo_create_tool`'s shared membership gate --
    see this module's own docstring section for the full reasoning.
    Raises `ToolError`, never returns anything, when the caller may not
    publish under `team`; a silent return means they may.

    `authz.is_admin` short-circuits first -- an operator may name any
    team, mirroring `kobo_create_agent`'s own group-consent exemption for
    operators. Otherwise `clients.groups.groups_for(email)` (a `None`
    `clients.groups` -- no resolver configured at all -- treated
    identically to a resolver that answered `None`) must contain `team`
    exactly; anything else -- unresolvable right now, or resolved but not
    a member -- refuses.

    The persona route's `_persona_team_gate` is the deliberate looser
    sibling: commons-open, membership only for named teams.
    """
    if authz.is_admin(app, email):
        return

    clients = getattr(app.state, "clients", None)
    resolver = clients.groups if clients is not None else None
    groups = resolver.groups_for(email) if resolver is not None else None
    if groups is None:
        raise ToolError(_DIRECTORY_LOOKUP_UNAVAILABLE)
    if team not in groups:
        raise ToolError(f"you are not a member of team {team!r}")


def register(mcp: FastMCP, app: Any) -> None:
    """Registers this module's sixteen write tools on `mcp`. Every route this
    module calls is looked up exactly once, here, at registration time --
    never re-resolved per call. `kobo_create_skill`/`kobo_create_tool`
    (Task 5) call `kobo_api.marketplace` directly and have no route of
    their own to look up here; `kobo_create_persona` (persona wave, Task 5)
    is the exception to that exception -- it looks up a real route, same as
    every other tool in this module. `kobo_decide_learning` (MCP learnings
    trio, Task 8) is a second exception alongside it: it calls `kobo_api.
    main._decide_learning` directly (imported inside its own tool body, the
    same deferred-import discipline every other main.py-only symbol in this
    module already follows) rather than through a route lookup here, since
    no single HTTP route takes `action` as a caller-supplied value -- see
    that tool's own docstring section for why. `kobo_decide_learnings_bulk`,
    its batch sibling, DOES have a real route to look up, same as every
    other tool.
    """
    create_agent_endpoint = _endpoint(app, "POST", "/api/agents")
    name_suggestions_endpoint = _endpoint(app, "POST", "/api/agents/name-suggestions")
    teach_endpoint = _endpoint(app, "POST", "/api/agents/{name}/teach")
    chat_endpoint = _endpoint(app, "POST", "/api/agents/{name}/chat")
    create_invite_endpoint = _endpoint(app, "POST", "/api/agents/{name}/invites")
    accept_invite_endpoint = _endpoint(app, "POST", "/api/invites/{invite_id}/accept")
    connect_slack_endpoint = _endpoint(app, "POST", "/api/agents/{name}/slack/connect")
    save_slack_channels_endpoint = _endpoint(app, "PUT", "/api/agents/{name}/slack/channels")
    add_skill_endpoint = _endpoint(app, "POST", "/api/agents/{name}/skills/catalog")
    remove_skill_endpoint = _endpoint(app, "DELETE", "/api/agents/{name}/skills/{skill_id}")
    add_tool_endpoint = _endpoint(app, "POST", "/api/agents/{name}/tooling")
    set_runtime_endpoint = _endpoint(app, "PATCH", "/api/agents/{name}/runtime")
    request_repo_access_endpoint = _endpoint(app, "POST", "/api/agents/{name}/repo-access")
    list_repo_access_endpoint = _endpoint(app, "GET", "/api/repo-access")
    decide_repo_access_endpoint = _endpoint(app, "POST", "/api/repo-access/decide")
    create_persona_endpoint = _endpoint(app, "POST", "/api/personas")
    bulk_decide_learnings_endpoint = _endpoint(app, "POST", "/api/agents/{name}/learnings/bulk")
    set_secrets_endpoint = _endpoint(app, "PUT", "/api/agents/{name}/secrets")
    set_env_endpoint = _endpoint(app, "PUT", "/api/agents/{name}/env")
    restart_endpoint = _endpoint(app, "POST", "/api/agents/{name}/restart")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @audited("kobo_create_agent")
    async def kobo_create_agent(
        ctx: Context,
        who_they_are: str,
        group: str,
        name: str | None = None,
        skills: list[dict] | None = None,
        tags: list[str] | None = None,
        skip_baseline: bool = False,
        archetype: str | None = None,
        voice: str | None = None,
    ) -> dict:
        """Create a new agent in the room. More capable at create time than
        the room's own modal, which adds skills post-create in the Skills
        tab -- `skills` picks here are still honored. `who_they_are` is the
        catalog persona id this agent should be built from (the room's
        persona catalog, e.g. "helpful-generalist"); `group` is the ACL
        group this agent belongs to, and you must already be a member of
        it (an operator may name any configured group). `name` is
        optional -- when omitted, the room's own naming station picks one
        for you. `skills`, when given, is a list of `{"id", "team"}` picks
        from the room's skills catalog; `tags` is a short list of purpose
        tags. Every new agent ships the baseline tooling pack (leakguard
        and probe guard) unless skip_baseline is true. This tool builds on
        the room's one runtime; there is no choice to make here. A team
        that already has an agent in its own repository connects it in the
        room instead, and this tool does not do that.
        archetype="team_guide" deploys a team's
        informational guide; forces persona/tools/tags. `voice` (optional)
        is a style overlay for archetype personas -- ignored unless
        `archetype` is set. Returns the agent's
        name and status; watch the room (or `kobo_get_agent`) for its
        checklist to finish in the background.
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
                        who_they_are=who_they_are,
                        what_they_do=who_they_are,
                        # `who_they_are` IS a catalog persona id on this
                        # tool (its own docstring above), so it rides along
                        # as `persona_id` too -- a resolvable one gets the
                        # route's own real-body-and-tags enrichment
                        # (NameSuggestionsRequest's own docstring,
                        # create-identity Task 3) in place of the bare id
                        # string standing in for both free-text fields. An
                        # unresolvable id just falls back to that same bare
                        # string, silently, by the route's own design --
                        # never a reason to validate it here first.
                        persona_id=who_they_are,
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
                    skip_baseline=skip_baseline,
                    archetype=archetype,
                    voice=voice,
                )
            except (TypeError, ValidationError) as exc:
                raise ToolError(_bad_request(exc)) from exc

            response = await _off_loop(create_agent_endpoint,payload=payload, request=req, session=session)
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
            response = await _off_loop(teach_endpoint,
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
            response = await _off_loop(chat_endpoint,
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
            response = await _off_loop(create_invite_endpoint,
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
            response = await _off_loop(accept_invite_endpoint,invite_id=invite_id, request=req, session=session)
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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @audited("kobo_set_runtime_image")
    async def kobo_set_runtime_image(ctx: Context, agent: str, runtime_image: str) -> dict:
        """Move an existing agent onto a different runtime image (#15).

        Every agent is built on the room's one runtime, so this tool is for
        two jobs: pinning an agent to a specific tag or digest of it, and
        carrying an agent created on a runtime the room has since retired
        back onto the current one. The alternative is destroying the agent
        and building a replacement -- which throws away Slack wiring (whose
        tokens are supplied by hand at Slack's own site), everything the
        agent has learned or been taught, and its channel membership. This
        changes the runtime and leaves all three alone.

        `runtime_image` must live under the room's own registry root; an
        image from anywhere else is refused. There is no read tool listing
        what the room runs, so pass an image you already know and read the
        rejection copy if it is refused. You must own `agent` (or be a room
        operator), and `agent` must be READY.

        NOT DESTRUCTIVE OF DATA, but it does roll the pod: landing the
        change pushes a commit, and the agent repo's CI rebuilds and
        hard-rotates the running container. Expect a brief restart.
        """
        from kobo_api.main import AgentRuntimePatch

        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        session = app.state.session_factory()
        try:
            response = set_runtime_endpoint(
                name=agent,
                payload=AgentRuntimePatch(runtime_image=runtime_image),
                request=req,
                session=session,
            )
            body = _agent_route_result(response)
        finally:
            session.close()

        return {"agent": agent, **body}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    @audited("kobo_set_agent_skills")
    async def kobo_set_agent_skills(
        ctx: Context,
        agent: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> dict:
        """Pull skills from the org marketplace catalog onto `agent`,
        remove skills it already carries, or both in one call -- the same
        catalog pick and removal the room's own Skills tab drives. Each id
        in `add` must name a skill in the marketplace catalog (see
        kobo_list_skills first); each id in `remove` names a skill already
        on `agent`, whether it was picked from the catalog or authored
        locally -- removing one deletes its whole directory from the
        agent's repo, so treat `remove` as destructive. You must own
        `agent` (or be a room operator), and `agent` must be READY (both
        routes answer with a conflict otherwise). Adds are applied first,
        then removals, each in the order given. If one entry fails partway
        through, every entry already applied stays applied and the error
        names the id that failed.
        """
        from kobo_api.main import CatalogSkillPayload

        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        session = app.state.session_factory()
        added: list[dict] = []
        removed: list[str] = []
        try:
            add_ids = list(add or [])
            if add_ids:
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

                for skill_id in add_ids:
                    entry = catalog.get(skill_id)
                    if entry is None:
                        raise ToolError(f"no such skill: {skill_id!r}")

                    response = await _off_loop(add_skill_endpoint,
                        name=agent,
                        payload=CatalogSkillPayload(name=skill_id, team=_skill_team(entry)),
                        request=req,
                        session=session,
                    )
                    body = _skill_route_result(response)
                    added.append({"id": skill_id, "sha": body.get("sha")})

            for skill_id in remove or []:
                response = await _off_loop(remove_skill_endpoint,
                    name=agent, skill_id=skill_id, request=req, session=session
                )
                _skill_route_result(response)
                removed.append(skill_id)
        finally:
            session.close()

        return {"added": added, "removed": removed}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    @audited("kobo_add_agent_tool")
    async def kobo_add_agent_tool(
        ctx: Context,
        agent: str,
        tool_id: str | None = None,
        filename: str | None = None,
        content: str | None = None,
    ) -> dict:
        """Stage a tool file on `agent`'s bundle/tools directory -- the same
        upload the room's own Tooling tab drives. Pass exactly one source:
        `tool_id` alone (pulls that tool's Python source from the org
        marketplace catalog, see kobo_list_tools first) or `filename`
        together with `content` (an inline upload of your own source). A
        file already staged under that same name is silently REPLACED, not
        merged -- treat this as destructive if `agent` already carries a
        live, enforced hook by that name. The file must pass the room's
        tooling checks before it lands: a top-level `pre_task` or
        `post_draft` hook, no import beyond the standard library. You must
        own `agent` (or be a room operator), and `agent` must be READY.
        Returns the staged file's name; either `sha` (the repo's head after
        the route's own commit, or the head already there when the content
        was byte-identical to what this agent already had staged) or, when
        the agent is reachable right now, `mode: "instant"` plus a `message`
        saying the tool arrives on the agent's next restart -- fast
        delivery is never a hot reload.
        """
        from kobo_api.main import ToolingUploadPayload

        inline_given = filename is not None or content is not None
        if tool_id is not None:
            if inline_given:
                raise ToolError(_EXACTLY_ONE_SOURCE)
        elif filename is None or content is None:
            raise ToolError(_EXACTLY_ONE_SOURCE)

        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        session = app.state.session_factory()
        background_tasks = BackgroundTasks()
        try:
            if tool_id is not None:
                clients = app.state.clients
                cfg = app.state.config
                try:
                    filename, content = fetch_tool_py(clients.github, cfg, tool_id)
                except MarketplaceError as exc:
                    raise ToolError(str(exc)) from exc
                except ClientError as exc:
                    raise ToolError(_MARKETPLACE_DID_NOT_ANSWER) from exc

            response = await _off_loop(add_tool_endpoint,
                name=agent,
                payload=ToolingUploadPayload(filename=filename, content=content),
                request=req,
                background_tasks=background_tasks,
                session=session,
            )
            body = _agent_route_result(response)
            # Mirrors kobo_teach above: runs whatever the fast lane's
            # instant success queued (the background archive commit) --
            # nothing here re-implements `_archive_tooling_upload`, this
            # just runs it.
            await background_tasks()
        finally:
            session.close()

        return {
            "filename": filename,
            "sha": body.get("sha"),
            "mode": body.get("mode"),
            "message": body.get("message"),
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @audited("kobo_request_repo_access")
    async def kobo_request_repo_access(ctx: Context, agent: str, repositories: list[str]) -> dict:
        """Request pull-request access to GitHub repositories for `agent` --
        the agent PR broker's request half. Pass MANY repositories at once
        (each "owner/name"); the room's operator set approves or revokes in
        batch. You must own `agent` (or be a room operator). Approved
        grants let the agent's own github.open_pr tool mint tokens scoped
        to exactly those repositories, nothing wider.
        """
        from kobo_api.main import RepoAccessRequest

        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        session = app.state.session_factory()
        try:
            try:
                payload = RepoAccessRequest(repositories=repositories)
            except ValidationError as exc:
                raise ToolError(_bad_request(exc)) from exc
            response = await _off_loop(
                request_repo_access_endpoint,
                name=agent,
                payload=payload,
                request=req,
                session=session,
            )
        finally:
            session.close()
        body = _agent_route_result(response)
        return {"requested": body.get("requested", [])}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_list_repo_access")
    async def kobo_list_repo_access(ctx: Context, status: str | None = None) -> dict:
        """List repo-access grant rows you can see: each agent's requested,
        approved, and revoked repositories, with who asked and who decided.
        Optional status filters to one of requested/approved/revoked. The
        operator review queue is status="requested".
        """
        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        session = app.state.session_factory()
        try:
            response = await _off_loop(
                list_repo_access_endpoint, request=req, status=status, session=session
            )
        finally:
            session.close()
        body = _agent_route_result(response)
        return {"results": body.get("results", [])}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    @audited("kobo_decide_repo_access")
    async def kobo_decide_repo_access(ctx: Context, ids: list[int], decision: str) -> dict:
        """Approve or revoke repo-access requests in BATCH -- the operator
        gate of the agent PR broker. `ids` are row ids from
        kobo_list_repo_access; `decision` is "approved" or "revoked".
        Operator-only; revoking is destructive (the agent's next token mint
        excludes the repository immediately).
        """
        from kobo_api.main import RepoAccessDecision

        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        session = app.state.session_factory()
        try:
            try:
                payload = RepoAccessDecision(ids=ids, decision=decision)
            except ValidationError as exc:
                raise ToolError(_bad_request(exc)) from exc
            response = await _off_loop(
                decide_repo_access_endpoint, payload=payload, request=req, session=session
            )
        finally:
            session.close()
        # NOT table (A): no agent is being resolved here, so a 403 is the
        # operator gate speaking and its message is the honest answer --
        # folding it into "agent not found" would be nonsense copy. 404
        # stays generic (an unknown row id enumerates nothing).
        if response.status_code == 403:
            raise ToolError(str(_parse(response).get("error", "operator only")))
        body = _agent_route_result(response)
        return {"decided": body.get("decided", [])}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @audited("kobo_create_skill")
    async def kobo_create_skill(ctx: Context, skill_id: str, content: str, team: str) -> dict:
        """Publish a brand-new skill to the org marketplace catalog -- a
        direct commit, no review gate. Before creating, run kobo_list_skills
        and check fit first; prefer an existing entry over a near-twin
        rather than publishing a duplicate. `skill_id` becomes the catalog
        id (must not already exist) and `content` is the skill's Markdown
        body, frontmatter and all; `name` and `owner` are always set to
        `skill_id`/`team` for you (overwriting any pasted values), and
        `description`/`version`/`profiles` are filled with lawful defaults
        when absent. `tags` is required in the frontmatter and must be a
        non-empty list; unlawful frontmatter is refused with the field
        named, never pushed. If you paste an
        existing skill's own frontmatter and it already sets an `owner`
        naming a DIFFERENT team than `team`, that is refused as a
        conflict, never silently overwritten. `team` is the marketplace
        team this skill is filed under -- you must already be a member of
        it (a room operator may name any team). The entry lands under YOUR
        team and publishes immediately, org-wide: it shows up for kobo_list_skills
        callers after the publish pipeline and the room's catalog refresh
        finish, a few minutes later, not instantly.
        """
        email = get_user_email(ctx)
        _require_team_membership(app, email, team)
        clients = app.state.clients
        cfg = app.state.config
        try:
            sha = create_skill(
                clients.github, cfg, skill_id=skill_id, content=content, team=team, author=email
            )
        except MarketplaceError as exc:
            raise ToolError(str(exc)) from exc
        except ClientError as exc:
            raise ToolError(_MARKETPLACE_DID_NOT_ANSWER) from exc

        return {
            "commit": sha,
            "note": (
                "appears in the catalog after the publish pipeline and the "
                "room's catalog refresh (minutes)"
            ),
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @audited("kobo_create_tool")
    async def kobo_create_tool(
        ctx: Context,
        tool_id: str,
        content: str,
        team: str,
        description: str,
        tags: list[str] | None = None,
    ) -> dict:
        """Publish a brand-new tool to the org marketplace catalog -- a
        direct commit, no review gate. Before creating, run kobo_list_tools
        and check fit first; prefer an existing entry over a near-twin
        rather than publishing a duplicate. `tool_id` becomes the catalog
        id (must not already exist) and `content` is the tool's Python
        source: it must pass the room's tooling checks, a top-level
        `pre_task` or `post_draft` hook, no import beyond the standard
        library. `team` is the marketplace team this tool is filed under --
        you must already be a member of it (a room operator may name any
        team). `description` and `tags` are catalog metadata shown to
        anyone browsing the marketplace. The entry lands under YOUR team
        and publishes immediately, org-wide: kobo_list_tools reads the
        marketplace repo directly, so it is visible there right after this
        commit, with no pipeline to wait on. Ids starting with "baseline-"
        are reserved for room operators: those ship automatically with
        every new agent, so publishing one changes the whole room.
        """
        email = get_user_email(ctx)
        _require_team_membership(app, email, team)
        # Beside the membership gate, and like it: before any marketplace
        # write. See `_BASELINE_PREFIX_RESERVED` for why the pack is
        # operator-only, and `authz.is_admin` (the same answer
        # `_require_team_membership` short-circuits on) for who that is.
        if tool_id.startswith(BASELINE_TOOL_PREFIX) and not authz.is_admin(app, email):
            raise ToolError(_BASELINE_PREFIX_RESERVED)
        clients = app.state.clients
        cfg = app.state.config
        try:
            sha = create_tool(
                clients.github,
                cfg,
                tool_id=tool_id,
                content=content,
                team=team,
                description=description,
                tags=list(tags or []),
                author=email,
            )
        except MarketplaceError as exc:
            raise ToolError(str(exc)) from exc
        except ClientError as exc:
            raise ToolError(_MARKETPLACE_DID_NOT_ANSWER) from exc

        return {
            "commit": sha,
            "note": (
                "appears in kobo_list_tools immediately after this commit; "
                "the tools listing reads the repo directly, no publish "
                "pipeline in between"
            ),
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @audited("kobo_create_persona")
    async def kobo_create_persona(
        ctx: Context,
        name: str,
        who_they_are: str,
        what_they_do: str,
        rules: list[str] | None = None,
        team: str = "shared",
        purpose_tags: list[str] | None = None,
    ) -> dict:
        """Create a brand-new persona in the room's catalog -- the same
        create the room's own PersonaBuilder drives. Before creating,
        run kobo_list_personas and check fit first; prefer an existing
        persona over a near-twin.

        Name the behavior, not a codename: a good persona name passes the
        glance test (a stranger reading the name plus who_they_are knows
        what picking it gets them). Prefer lowercase hyphenated trait
        words ("direct-factual-condensed-mellow"), an archetype plus a
        qualifier ("stoic-mentor"), or a figure whose mannerisms the
        persona adopts (mannerisms and tone only, never identity claims).

        `name` becomes the catalog id via the room's slug law (must not
        already exist). `who_they_are` is ONE
        line; `what_they_do` is the persona's body; `rules` are optional
        always/never one-liners. The merged persona renders to at most
        200 lines and 48KB. `team` defaults to "shared", the commons
        anyone may file into; naming any other team requires that you
        are a member of it (a room operator may name any team). The
        persona is searchable and pickable by kobo_create_agent
        immediately.
        """
        from kobo_api.main import CreatePersonaRequest

        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        try:
            payload = CreatePersonaRequest(
                name=name,
                who_they_are=who_they_are,
                what_they_do=what_they_do,
                rules=rules or [],
                team=team,
                purpose_tags=purpose_tags or [],
            )
        except (TypeError, ValidationError) as exc:
            raise ToolError(_bad_request(exc)) from exc

        session = app.state.session_factory()
        try:
            response = await _off_loop(create_persona_endpoint,payload=payload, request=req, session=session)
            return _create_result(response)
        finally:
            session.close()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    @audited("kobo_decide_learning")
    async def kobo_decide_learning(
        ctx: Context, agent: str, learning_id: int, action: str, content: str | None = None
    ) -> dict:
        """Approve or deny one pending learning for `agent` -- the same
        verdict the room's own Learnings tab records for a single row.
        `action` is "approve" (rides the teach lane byte-for-byte, the same
        instant-or-archive split kobo_teach uses) or "deny" (marks the row,
        nothing more). You must clear the decide gate -- owner, room
        operator, or a per-user grant on this agent, the identical gate
        kobo_list_learnings' own can_decide reports -- a caller who can
        only see the agent gets "agent not found", never a distinguishable
        forbidden. `content`, approve only: fixed wording to teach instead
        of the proposed sentence, one line, 200 characters or fewer -- bad
        wording raises a `ToolError` and leaves the row untouched. Returns
        the decided row's id and status, plus mode and filename on an
        approve.
        """
        from kobo_api.main import _decide_learning

        if action not in ("approve", "deny"):
            raise ToolError(f"{_INVALID_LEARNING_ACTION}, got {action!r}")

        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        session = app.state.session_factory()
        background_tasks = BackgroundTasks()
        try:
            response = await _off_loop(
                _decide_learning,
                name=agent,
                learning_id=learning_id,
                action=action,
                request=req,
                background_tasks=background_tasks,
                session=session,
                content=content if action == "approve" else None,
            )
            body = _learning_route_result(response)
            # Mirrors kobo_teach above: runs whatever the approve+instant
            # lane queued (the background archive commit) -- nothing here
            # re-implements `_archive_teach_note`, this just runs it.
            await background_tasks()
        finally:
            session.close()

        return body

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    @audited("kobo_decide_learnings_bulk")
    async def kobo_decide_learnings_bulk(
        ctx: Context, agent: str, ids: list[int], action: str
    ) -> dict:
        """Approve or deny MANY pending learnings for `agent` in one call --
        the room's own Learnings tab select-all gesture, the same POST
        .../learnings/bulk route. `action` ("approve" or "deny") applies to
        every id in `ids`; only the first 50 are honored, matching the
        room's own bulk endpoint -- anything past that is silently
        dropped, never decided and never an error. The call itself always
        answers with `results`, one entry per id, never a single verdict
        for the whole batch. Each entry's `status_code` is the room's own
        verdict VERBATIM, unlike every other tool in this module: it is
        NOT translated the way a decide-gate refusal folds into "agent not
        found" elsewhere. 200 means that id was decided; 403 means you may
        not decide for `agent` right now (not its owner, not an operator,
        no per-user grant); 404 means either that learning_id does not
        exist or `agent` itself is unknown or not visible to you; 409
        means that learning was already decided, or `agent` is not ready.
        Inspect each entry rather than assuming the call as a whole
        succeeded or failed.
        """
        from kobo_api.main import LearningsBulkPayload

        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        session = app.state.session_factory()
        background_tasks = BackgroundTasks()
        try:
            try:
                payload = LearningsBulkPayload(ids=ids, action=action)
            except ValidationError as exc:
                raise ToolError(_bad_request(exc)) from exc
            response = await _off_loop(
                bulk_decide_learnings_endpoint,
                name=agent,
                payload=payload,
                request=req,
                background_tasks=background_tasks,
                session=session,
            )
            body = _agent_route_result(response)
            # Mirrors kobo_decide_learning above: ONE shared background_tasks
            # instance threads through every id `_decide_learning` processes
            # internally, so a single run afterward covers every id's own
            # queued archive commit.
            await background_tasks()
        finally:
            session.close()

        return {"results": body.get("results", [])}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    @audited("kobo_set_agent_secrets")
    async def kobo_set_agent_secrets(
        ctx: Context,
        agent: str,
        # `dict[str, Any]`, not `dict[str, str]`, and this is a safety
        # decision rather than a loose one. The SDK validates a tool's
        # arguments against its own schema BEFORE this function runs, and
        # pydantic renders the offending input in that error
        # (`input_value=...`) -- which for this argument is a credential.
        # Declared wide, the room's own `AgentEnvWrite` is the validator,
        # `_bad_request` is the message, and nothing outside this module
        # ever holds the value. The docstring is what tells a client these
        # are strings.
        secret_values: dict[str, Any] | None = None,
        remove: list[str] | None = None,
    ) -> dict:
        """Add, change or remove `agent`'s secrets in one call -- the same
        write the room's own Secrets and environment panel makes.

        `secret_values` is a name-to-value mapping of STRINGS to store,
        `remove` a list of names to take away; a name in both is refused. Names are upper snake
        case (`[A-Z][A-Z0-9_]*`), 64 characters at most, 100 keys at most,
        and the names the room fills in itself are refused with "the room
        sets this one" -- the colleague pass, the Slack values, and the
        two model-access variables.

        A VALUE passed here is stored and never readable again, by you or
        by anyone else in the room: kobo_list_agent_secrets answers names
        only, and there is no reveal. Removing the last secret an agent
        has is refused; destroy the agent to remove them all.

        The first secret an agent ever gets also adds a line to its own
        files so the running agent can read it. `wiring` says what
        happened: "committed" for an agent the room composed,
        "pull_request" (with `pull_request`, a link to merge) for one
        whose repository belongs to its team, "unchanged" when the line
        was already there. Changes reach the agent the next time it
        starts.

        Marked destructive because `remove` takes a credential away from a
        running agent. You must own `agent` or be a room operator, and
        `agent` must be READY.
        """
        from kobo_api.main import AgentEnvWrite

        # The argument is named `secret_values`, not `set`, because
        # `kobo_api.mcp.audit.SENSITIVE_KEYS` redacts a kwarg by the words
        # in its NAME: a mapping called `set` would land in the audit row
        # whole, values and all, which is the one thing this whole feature
        # promises never happens.
        body = await _write_agent_env(
            ctx, set_secrets_endpoint, agent, secret_values, remove, AgentEnvWrite
        )
        return {
            "secrets": body.get("keys", []),
            "delivered_as": body.get("secret_name"),
            "wiring": body.get("wiring"),
            "pull_request": body.get("pull_request"),
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    @audited("kobo_set_agent_env")
    async def kobo_set_agent_env(
        ctx: Context,
        agent: str,
        # Wide for the same reason as the secrets tool above, and kept the
        # same so the two cannot drift: the room's own model is the
        # validator and the room's own sentence is the refusal.
        variables: dict[str, Any] | None = None,
        remove: list[str] | None = None,
    ) -> dict:
        """Add, change or remove `agent`'s plain environment variables --
        the other half of the room's Secrets and environment panel.

        `variables` is a name-to-value mapping of STRINGS and `remove` a
        list of names to take away, the same shape and the same name rules
        as kobo_set_agent_secrets. The difference is where these are kept and
        who can read them: these live in the agent's own files, in the
        open, so use them for values that are not sensitive and use
        kobo_set_agent_secrets for anything else.

        `env` comes back as what the agent's files now hold, and `wiring`
        says how the change got there: "committed", "pull_request" (with a
        link to merge), or "unchanged". Changes reach the agent the next
        time it starts.

        Marked destructive because `remove` takes a variable away from a
        running agent. You must own `agent` or be a room operator, and
        `agent` must be READY.
        """
        from kobo_api.main import AgentEnvWrite

        # Named plainly, unlike the secrets tool's own argument above:
        # these values are the ones that are NOT sensitive, so an audit row
        # that records them is a more useful row rather than a leak.
        body = await _write_agent_env(
            ctx, set_env_endpoint, agent, variables, remove, AgentEnvWrite
        )
        return {
            "env": body.get("env", {}),
            "wiring": body.get("wiring"),
            "pull_request": body.get("pull_request"),
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    @audited("kobo_restart_agent")
    async def kobo_restart_agent(ctx: Context, agent: str) -> dict:
        """Restart `agent` now -- the same rolling restart the room's own
        Secrets panel button asks for.

        A secret or an environment variable reaches a RUNNING agent at its
        next boot and not before, so this is what makes a change you just
        saved take effect. Nothing is rebuilt and nothing in the agent's
        repository is touched: its pods go down and come back on the image
        they were already running.

        Marked destructive because it interrupts a running agent. Anything
        it was working on when the pods go is gone, so do not fire this
        while it is mid-task.

        You must own `agent` or be a room operator, and `agent` must be
        running already -- an agent whose pipeline has not finished has no
        Deployment to roll, and says so.
        """
        req = authz.room_request(app, get_user_email(ctx))
        session = app.state.session_factory()
        try:
            response = await _off_loop(
                restart_endpoint, name=agent, request=req, session=session
            )
        finally:
            session.close()

        body = _agent_route_result(response)
        return {"agent": agent, "restarted": body.get("restarted", False)}

    async def _write_agent_env(
        ctx: Context,
        endpoint: Callable[..., Any],
        agent: str,
        values: dict[str, Any] | None,
        remove: list[str] | None,
        payload_model: Any,
    ) -> dict:
        """The half the two tools above share: build the route's own body
        model, call the route, and translate its status.

        One deviation from table (A), and it is about meaning rather than
        number. A 503 from these two routes is not an outage: it is a room
        with no secrets platform configured, which is a standing fact the
        caller can act on (use environment variables instead) and which
        `_DID_NOT_ANSWER`'s "try again" would misdescribe. So that one
        status surfaces the route's own sentence; every other 5xx still
        gets the house phrasing, and 403/404 still collapse to "agent not
        found".
        """
        req = authz.room_request(app, get_user_email(ctx))
        session = app.state.session_factory()
        try:
            try:
                payload = payload_model(set=values or {}, remove=list(remove or []))
            except ValidationError as exc:
                raise ToolError(_bad_request(exc)) from exc
            response = await _off_loop(
                endpoint, name=agent, payload=payload, request=req, session=session
            )
        finally:
            session.close()

        if response.status_code == 503:
            raise ToolError(
                str(_parse(response).get("error", "that request could not be completed"))
            )
        return _agent_route_result(response)
