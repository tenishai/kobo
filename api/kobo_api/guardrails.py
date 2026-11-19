"""Guardrails: an agent's rules list, one rule per line, in front of the model on
every task (issue #524 step 2; the runtime half is daedalus's `daedalus.guardrails`).

The record is `bundle/guardrails.md` in the agent's own repository, the same bundle
of record knowledge and skills live in. The room keeps one `GuardrailRule` row per
rule only for what the file cannot say: where the line came from (the persona, a
skill, a correction, a person) and who added or last edited it. A line in the file
with no row was written in the repository and reads as such; a row whose line left
the file goes on the next save.

The file the room writes is exactly the text it relays. The runtime compares the
bundle's bytes with its live copy's, so an archive commit with different bytes
would read to it as a newer edit made in the repository.

The limit is the runtime's: 4,000 characters of "- rule" lines. The room refuses a
longer list instead of letting the runtime cut it, and one rule is at most
RULE_MAX_CHARS, so a paragraph cannot eat the list.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kobo_api import delivery
from kobo_api.clients import GitHubClient
from kobo_api.models import GuardrailRule

GUARDRAILS_MARKER = "guardrails"
GUARDRAILS_FILE = "guardrails.md"
RULES_MAX_CHARS = 4000
RULE_MAX_CHARS = 300

# Where a rule came from. "repository" is never sent: it is what a line written in
# the repository, with no row in the room, reads as.
SOURCE_PERSON = "person"
SOURCE_REPOSITORY = "repository"
SETTABLE_SOURCES = ("person", "persona", "skill", "correction")

_HEADER = (
    "<!-- This agent's guardrails: one rule per line, followed on every task. "
    "Edit them in the room's Guardrails section. -->\n"
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADING = re.compile(r"^#{1,6}(\s|$)")
_LIST_MARKER = re.compile(r"^(?:[-*+]|\d+[.)])\s+")


class GuardrailsError(Exception):
    """A rules list the room will not save, with the sentence a person reads."""


def parse_rules(text: str) -> list[str]:
    """The rules in a guardrails file, as the runtime reads them: one per non-empty
    line, skipping HTML comments and markdown headings, dropping a list marker."""
    rules: list[str] = []
    for line in _HTML_COMMENT.sub("", text).splitlines():
        line = line.strip()
        if not line or _HEADING.match(line):
            continue
        line = _LIST_MARKER.sub("", line).strip()
        if line:
            rules.append(line)
    return rules


def used_chars(rules: list[str]) -> int:
    """What the list costs against the limit, counted as the runtime counts it."""
    return sum(len(f"- {rule}\n") for rule in rules)


def validate_rules(rules: list[str]) -> list[str]:
    """The rules, trimmed, or GuardrailsError naming the first problem."""
    cleaned: list[str] = []
    for raw in rules:
        rule = raw.strip()
        if not rule:
            raise GuardrailsError("a rule can't be empty")
        if "\n" in rule or "\r" in rule:
            raise GuardrailsError("a rule is one line; put each rule on its own line")
        if len(rule) > RULE_MAX_CHARS:
            raise GuardrailsError(
                f"a rule is at most {RULE_MAX_CHARS} characters; split a longer one, "
                "or move the steps into a skill"
            )
        if rule in cleaned:
            raise GuardrailsError(f"that rule is on the list twice: {rule!r}")
        cleaned.append(rule)
    if parse_rules(render_file(cleaned)) != cleaned:
        # A rule shaped like a heading or a comment would vanish on the way in.
        raise GuardrailsError("a rule can't start with '#' and a space, or hold '<!--'")
    if used_chars(cleaned) > RULES_MAX_CHARS:
        raise GuardrailsError(
            f"the list is over its {RULES_MAX_CHARS:,}-character limit; shorten a rule, or "
            "move rules that only matter inside one job into that job's skill"
        )
    return cleaned


def render_file(rules: list[str]) -> str:
    """The file the room writes and relays, byte for byte."""
    return _HEADER + "".join(f"- {rule}\n" for rule in rules)


def read_guardrails(github: GitHubClient, repo: str) -> str:
    """The repository's guardrails file, or "" when it has none yet."""
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        path = workdir / "bundle" / GUARDRAILS_FILE
        return path.read_text(encoding="utf-8") if path.is_file() else ""


def save_guardrails_file(
    github: GitHubClient, repo: str, content: str, *, archive_only: bool = False
) -> str:
    """Write `bundle/guardrails.md` and push it; returns the pushed commit's sha.
    `archive_only` as in `kobo_api.knowledge.save_knowledge_file`: after a relay
    the agent already has the list, so the commit only mirrors it (`[skip ci]`);
    without one, the commit is the delivery and keeps CI."""
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        bundle_dir = workdir / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / GUARDRAILS_FILE).write_text(content, encoding="utf-8")
        if archive_only:
            message = "kobo: archive guardrails [skip ci]"
        else:
            message = "kobo: update guardrails"
        delivery.push(github, workdir, message, restart=not archive_only)
        return github.local_head(workdir)


@dataclass(frozen=True)
class RuleIn:
    """One line of a save: `id` when the line was already on the list (an edit
    keeps where it came from), `source` when the room proposed it."""

    text: str
    id: int | None = None
    source_kind: str = SOURCE_PERSON
    source_ref: str | None = None


def rule_payload(row: GuardrailRule | None, text: str) -> dict:
    """One rule as the panel and the room's assistant tools read it."""
    if row is None:
        return {"id": None, "text": text, "source": {"kind": SOURCE_REPOSITORY, "ref": None},
                "added_by": None, "added_at": None, "edited_by": None, "edited_at": None}
    return {
        "id": row.id,
        "text": text,
        "source": {"kind": row.source_kind, "ref": row.source_ref},
        "added_by": row.added_by,
        "added_at": row.added_at.isoformat() if row.added_at else None,
        "edited_by": row.edited_by,
        "edited_at": row.edited_at.isoformat() if row.edited_at else None,
    }


def annotate(session: Session, agent_name: str, rules: list[str]) -> list[dict]:
    """The file's rules in order, each with its row's provenance when it has one."""
    rows = session.scalars(
        select(GuardrailRule).where(GuardrailRule.agent_name == agent_name)
    ).all()
    by_text: dict[str, GuardrailRule] = {}
    for row in rows:
        by_text.setdefault(row.text, row)
    return [rule_payload(by_text.get(text), text) for text in rules]


def sync_rows(session: Session, agent_name: str, items: list[RuleIn], actor: str) -> None:
    """Make the agent's rows match a saved list, uncommitted. A line sent with its
    row's id is that row, edited if the text changed; a line whose text matches a
    row keeps it; any other line is new. Rows no line kept are deleted."""
    rows = session.scalars(
        select(GuardrailRule).where(GuardrailRule.agent_name == agent_name)
    ).all()
    by_id = {row.id: row for row in rows}
    by_text = {row.text: row for row in rows}
    kept: set[int] = set()
    now = datetime.now(UTC)
    for item in items:
        row = by_id.get(item.id) if item.id is not None else None
        if row is None or row.id in kept:
            row = by_text.get(item.text)
            if row is not None and row.id in kept:
                row = None
        if row is None:
            kind = item.source_kind if item.source_kind in SETTABLE_SOURCES else SOURCE_PERSON
            row = GuardrailRule(
                agent_name=agent_name,
                text=item.text,
                source_kind=kind,
                source_ref=(item.source_ref or None) if kind != SOURCE_PERSON else None,
                added_by=actor,
                added_at=now,
            )
            session.add(row)
            session.flush()
        elif row.text != item.text:
            row.text = item.text
            row.edited_by = actor
            row.edited_at = now
        kept.add(row.id)
    for row in rows:
        if row.id not in kept:
            session.delete(row)
