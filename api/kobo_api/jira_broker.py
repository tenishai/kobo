"""The agent Jira read broker (2026-09-04, AREQ lane): scoped tracker reads.

The PR broker's custody model applied to reads instead of token minting.
The room holds ONE read-only tracker credential (a service account's API
token, mounted as a file the same way the PR App's key is) and exposes two
GET routes; deployed agents call them with their colleague JWT (verified by
the same `pr_broker.verify_agent_token` walls) and never see the credential.

The scoping difference from the PR broker: GitHub enforces a minted token's
repository pins, but Atlassian's Basic auth has no per-call scoping to
delegate to -- so the pinning lives HERE, in code, and is deliberately not
a parameter. `jira_projects` (config) is the only reach this module has:
issue reads refuse keys outside those projects before any network call, and
search never accepts JQL at all -- the caller hands over bare text, and the
JQL is assembled server-side around it. Free-text query passthrough is the
exact resource-guard theater the external-MCP evaluation rejected; bounded
inputs into a fixed template is what makes this lane honest.

Comment bodies arrive as Atlassian Document Format trees; `_adf_text`
flattens them to plain text because the consumer is an agent reading
evidence (who approved, in which words, when), not a renderer.
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path

import httpx

from kobo_api.clients import ClientError

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_COMMENTS_MAX = 50
_SEARCH_CAP = 25
_TEXT_MAX = 200
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ADF node types that end a block of text; everything else concatenates.
_BLOCK_NODES = {
    "paragraph",
    "heading",
    "listItem",
    "blockquote",
    "codeBlock",
    "tableRow",
}


def _adf_text(node) -> str:
    """Flatten one Atlassian Document Format node to plain text. Unknown
    node types contribute their children's text and nothing else, so a new
    ADF node type degrades to partial text, never to a crash."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type")
    if node_type == "text":
        return node.get("text") or ""
    if node_type == "hardBreak":
        return "\n"
    if node_type in ("mention", "emoji"):
        return (node.get("attrs") or {}).get("text") or ""
    if node_type == "inlineCard":
        return (node.get("attrs") or {}).get("url") or ""
    text = "".join(_adf_text(child) for child in node.get("content") or [])
    if node_type in _BLOCK_NODES:
        text += "\n"
    return text


def _flatten(body) -> str:
    """A comment/description body to plain text: ADF tree or already-plain
    string, whichever this Jira version handed back."""
    if isinstance(body, str):
        return body.strip()
    return _adf_text(body).strip()


def _auth_header(email: str, pat_path: Path) -> dict[str, str]:
    """Basic auth from the mounted credential. Read lazily per call (the
    PR App key's own convention): a rotated secret takes effect on the next
    call, and the value never sits in room memory between calls."""
    pat = pat_path.read_text().strip()
    token = base64.b64encode(f"{email}:{pat}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


# The sentence the routes pass on when the credential itself is the problem.
CREDENTIAL_REJECTED = "the tracker rejected the room's credential"


def _credential_rejected(
    domain: str, email: str, pat_path: Path, http_client: httpx.Client | None = None
) -> bool:
    """Jira Cloud treats a Basic pair it cannot verify as ANONYMOUS, not as
    an error: every issue read is then a 404 and every search comes back
    empty, which is exactly what a missing issue looks like (2026-11-03: a
    rotated token read as "no such issue" for two days). `/myself` tells
    the two apart: a 401, or the X-Seraph-LoginReason header saying
    AUTHENTICATED_FAILED, means the credential is wrong, not the key."""
    try:
        response = _get(domain, email, pat_path, "/rest/api/3/myself", {}, http_client)
    except ClientError:
        return False
    reason = response.headers.get("x-seraph-loginreason") or ""
    return response.status_code == 401 or "AUTHENTICATED_FAILED" in reason


def _person(field) -> str | None:
    if not isinstance(field, dict):
        return None
    return field.get("emailAddress") or field.get("displayName")


def _get(
    domain: str,
    email: str,
    pat_path: Path,
    path: str,
    params: dict,
    http_client: httpx.Client | None = None,
):
    client = http_client or httpx.Client(timeout=_TIMEOUT)
    try:
        response = client.get(
            f"https://{domain}{path}",
            headers=_auth_header(email, pat_path),
            params=params,
        )
    except httpx.HTTPError as exc:
        log.debug("jira request failed: %r", exc)
        raise ClientError("failed to reach the tracker") from exc
    finally:
        if http_client is None:
            client.close()
    return response


def fetch_issue(
    *,
    domain: str,
    email: str,
    pat_path: Path,
    key: str,
    http_client: httpx.Client | None = None,
) -> dict:
    """One issue with its comment history, in the shape the daedalus jira
    sidecar formats. Raises ClientError("no such issue") on a 404 so the
    route can answer honestly, ClientError(generic) on everything else."""
    issue_response = _get(
        domain,
        email,
        pat_path,
        f"/rest/api/3/issue/{key}",
        {
            "fields": "summary,status,assignee,reporter,issuetype,resolution,"
            "resolutiondate,created,updated,labels,description"
        },
        http_client,
    )
    if issue_response.status_code == 404:
        if _credential_rejected(domain, email, pat_path, http_client):
            log.warning("jira issue read: the tracker rejected the room's credential")
            raise ClientError(CREDENTIAL_REJECTED)
        raise ClientError("no such issue")
    if issue_response.status_code != 200:
        log.warning(
            "jira issue read refused: %s %s",
            issue_response.status_code,
            issue_response.text[:200],
        )
        raise ClientError("the tracker refused the read")
    data = issue_response.json()
    fields = data.get("fields") or {}

    comments_response = _get(
        domain,
        email,
        pat_path,
        f"/rest/api/3/issue/{key}/comment",
        {"maxResults": _COMMENTS_MAX, "orderBy": "created"},
        http_client,
    )
    comments: list[dict] = []
    comments_truncated = False
    if comments_response.status_code == 200:
        body = comments_response.json()
        for comment in body.get("comments") or []:
            author = comment.get("author") or {}
            comments.append(
                {
                    "author": author.get("displayName"),
                    "author_email": author.get("emailAddress"),
                    "created": comment.get("created"),
                    "body": _flatten(comment.get("body")),
                }
            )
        comments_truncated = (body.get("total") or 0) > len(comments)
    else:
        # The issue read succeeded; a comment-read refusal must not turn
        # into a silent "comments: none" -- that reads as evidence of
        # absence to an auditing agent. Fail the whole read loudly instead.
        log.warning(
            "jira comment read refused: %s %s",
            comments_response.status_code,
            comments_response.text[:200],
        )
        raise ClientError("the tracker refused the comment read")

    return {
        "issue": {
            "key": data.get("key") or key,
            "url": f"https://{domain}/browse/{data.get('key') or key}",
            "summary": fields.get("summary"),
            "status": (fields.get("status") or {}).get("name"),
            "type": (fields.get("issuetype") or {}).get("name"),
            "assignee": _person(fields.get("assignee")),
            "reporter": _person(fields.get("reporter")),
            "resolution": (fields.get("resolution") or {}).get("name"),
            "resolved_at": fields.get("resolutiondate"),
            "created_at": fields.get("created"),
            "updated_at": fields.get("updated"),
            "labels": fields.get("labels") or [],
            "description": _flatten(fields.get("description")),
        },
        "comments": comments,
        "comments_truncated": comments_truncated,
    }


def search_issues(
    *,
    domain: str,
    email: str,
    pat_path: Path,
    project: str,
    text: str,
    limit: int,
    created_after: str = "",
    created_before: str = "",
    http_client: httpx.Client | None = None,
) -> dict:
    """Text search pinned to `project`, newest first, optionally scoped to
    a created-date window. The JQL is assembled here from bounded inputs
    -- the term is stripped of the two characters that could escape its
    quoting and length-capped, the dates must be bare yyyy-mm-dd, and the
    project comes from config, never the caller."""
    term = text.replace("\\", " ").replace('"', " ").strip()[:_TEXT_MAX]
    if not term:
        raise ClientError("nothing searchable in the given text")
    for label, value in (("created_after", created_after), ("created_before", created_before)):
        if value and not _DATE_RE.fullmatch(value):
            raise ClientError(f"{label} must be a bare date like 2026-07-10")
    count = max(1, min(int(limit), _SEARCH_CAP))
    jql = f'project = "{project}" AND text ~ "{term}"'
    if created_after:
        jql += f' AND created >= "{created_after}"'
    if created_before:
        jql += f' AND created <= "{created_before}"'
    jql += " ORDER BY created DESC"
    response = _get(
        domain,
        email,
        pat_path,
        "/rest/api/3/search/jql",
        {
            "jql": jql,
            "fields": "summary,status,reporter,created",
            "maxResults": count,
        },
        http_client,
    )
    if response.status_code != 200:
        log.warning(
            "jira search refused: %s %s", response.status_code, response.text[:200]
        )
        raise ClientError("the tracker refused the search")
    body = response.json()
    issues = []
    for entry in body.get("issues") or []:
        fields = entry.get("fields") or {}
        issues.append(
            {
                "key": entry.get("key"),
                "summary": fields.get("summary"),
                "status": (fields.get("status") or {}).get("name"),
                "reporter": _person(fields.get("reporter")),
                "created_at": fields.get("created"),
            }
        )
    if not issues and _credential_rejected(domain, email, pat_path, http_client):
        log.warning("jira search: the tracker rejected the room's credential")
        raise ClientError(CREDENTIAL_REJECTED)
    return {
        "issues": issues,
        "truncated": bool(body.get("nextPageToken")) or len(issues) >= count,
    }
