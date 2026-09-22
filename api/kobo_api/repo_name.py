"""What a repository name may look like, said once.

Two unrelated readers ask it -- the connect scan, before it reads anything
through the GitHub API, and `ConnectStep`, before the pipeline builds paths
and URLs out of the value the route recorded -- and a shape rule that
disagreed between them would be no rule at all: the scan would pass
something the step then refuses, halfway through a provision.

The charset is GitHub's own for an owner and a repository name (letters,
digits, dot, dash, underscore). It is enforced rather than assumed because
this string is not just stored: it is interpolated into `gh api` paths and
into URLs, where a `?`, a `#`, a space or a `..` would address something
other than the repository the caller named.
"""

from __future__ import annotations

import re

# One segment of `owner/name`. Length-capped at GitHub's own 100, which is
# also what keeps a pasted essay out of every path built from this.
REPO_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")

# A segment that is only dots addresses a directory rather than a name.
_DOT_SEGMENTS = {".", ".."}


def split_repo(repo: str) -> tuple[str, str] | None:
    """`("owner", "name")` for a well-formed `owner/name`, else None.

    Exactly one slash: `owner/a/b` would pass an owner check and then
    travel into every path the room builds from this value.
    """
    parts = repo.strip().split("/")
    if len(parts) != 2:
        return None
    owner, name = parts
    for part in (owner, name):
        if part in _DOT_SEGMENTS or not REPO_SEGMENT_RE.fullmatch(part):
            return None
    return owner, name


def is_in_org(repo: str, org: str) -> bool:
    """Whether `repo` is a well-formed name inside `org`.

    Case-insensitive on the owner, because GitHub is: the room holds one
    App installation, and refusing `Example-Org/thing` while accepting
    `example-org/thing` would be refusing the same repository on its
    spelling. The room still records the spelling GitHub itself answers
    with (`RepoAccessInfo.full_name`), not either of these.
    """
    parts = split_repo(repo)
    return parts is not None and parts[0].lower() == org.strip().lower()
