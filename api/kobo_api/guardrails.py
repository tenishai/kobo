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
longer list instead of letting the runtime cut it. A rule's words are at most
RULE_MAX_CHARS and the whole line, with what it names, at most RULE_LINE_MAX_CHARS,
so neither a paragraph nor a long list can eat the rest.

A rule can name what it is about (Kyle, 2026-11-20: "only respond to these people",
then "other things we want rules based off of, like emoji types"): people, emoji,
Slack channels and repositories. Each list rides on the rule's own line, after its
words, in that order and each at most once:

    Only act on these. (These people: Ada <ada@example.com>; Al <al@example.com>)
    (These emoji: :ticket:, :+1:) (These channels: #sre-requests) (These repositories: acme/api)

(one line in the file), so the file stays the one record. The runtime tells the
model who sent each Slack message and where (`(From Name <email>, in #channel.)`,
daedalus's Slack transport) and which emoji woke it, and its rules intro says a
rule's lists are matched against those; repositories are the ones its tools act on.
Like every rule, it guides; it locks nothing.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kobo_api import delivery
from kobo_api.clients import GitHubClient
from kobo_api.models import Agent, AgentLearning, GuardrailRule

GUARDRAILS_MARKER = "guardrails"
GUARDRAILS_FILE = "guardrails.md"
GUARDRAILS_PATH = f"bundle/{GUARDRAILS_FILE}"
RULES_MAX_CHARS = 4000
RULE_MAX_CHARS = 300
# Breaks of one rule before the panel says it wants a check or a permission: the
# same two techne's lesson review uses before it suggests enforcement.
BREAKS_BEFORE_A_CHECK = 2

# Where a rule came from. "repository" is never sent: it is what a line written in
# the repository, with no row in the room, reads as.
SOURCE_PERSON = "person"
SOURCE_REPOSITORY = "repository"
SETTABLE_SOURCES = ("person", "persona", "skill", "correction")

# What a rule can name, in the order its line carries them (`compose_line`), at most
# RULE_MAX_LIST of each.
LIST_KINDS = ("people", "emoji", "channels", "repositories")
RULE_MAX_LIST = 20
RULE_LINE_MAX_CHARS = 1000
_LIST_SUFFIX = re.compile(
    r"\s*\(These (?P<kind>people|emoji|channels|repositories):(?P<items>[^()]*)\)$"
)
# A list left in a rule's words (not `_LIST_MARKER` below, which is a markdown
# bullet at the start of a line).
_NAMED_LIST = re.compile(r"\(These (?:people|emoji|channels|repositories):")
_PERSON = re.compile(r"^(?P<name>[^<>;()]*?)\s*<(?P<email>[^<>\s;()]+)>$")
_EMAIL = re.compile(r"^[^@\s<>;()]+@[^@\s<>;()]+\.[^@\s<>;()]+$")
_NAME_DROP = re.compile(r"[<>;()\r\n]")
# Slack's channel names: lower case letters, digits, hyphens and underscores.
_CHANNEL_NAME = re.compile(r"^[a-z0-9_\-]{1,80}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")

_HEADER = (
    "<!-- This agent's guardrails: one rule per line, followed on every task. "
    "Edit them in the room's Guardrails section. -->\n"
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADING = re.compile(r"^#{1,6}(\s|$)")
_LIST_MARKER = re.compile(r"^(?:[-*+]|\d+[.)])\s+")


class GuardrailsError(Exception):
    """A rules list the room will not save, with the sentence a person reads."""


@dataclass(frozen=True)
class Person:
    """Someone a rule names: what the directory calls them, and their email, which is
    what the runtime's sender line carries too."""

    name: str
    email: str


@dataclass(frozen=True)
class RuleLists:
    """What a rule names, by kind (LIST_KINDS). Empty when it names nothing."""

    people: tuple[Person, ...] = ()
    emoji: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()
    repositories: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.people or self.emoji or self.channels or self.repositories)


def _emoji_name(raw: str) -> str | None:
    # Read here, not at import: the rule is slack_setup's, the names Slack's
    # reaction events carry and the wake emoji field takes.
    from kobo_api.slack_setup import emoji_name

    return emoji_name(raw)


def _channel_name(raw: str) -> str | None:
    """A Slack channel as Slack names it: no #, lower case, a space as a hyphen."""
    name = re.sub(r"\s+", "-", raw.strip().lstrip("#").strip()).lower()
    return name if _CHANNEL_NAME.match(name) else None


def _capped(items: list, kind: str) -> list:
    if len(items) > RULE_MAX_LIST:
        raise GuardrailsError(f"a rule names at most {RULE_MAX_LIST} {kind}")
    return items


def clean_people(people: list[Person]) -> list[Person]:
    """`people` as a rule's line will carry them: names on one line without the
    characters the line's own shape uses, each email once whatever its case. Emails
    keep the case they came with, so a line written in the repository saves back
    unchanged. A GuardrailsError names the first that can't be."""
    cleaned: list[Person] = []
    seen: set[str] = set()
    for person in people:
        email = person.email.strip()
        if not _EMAIL.match(email):
            raise GuardrailsError(f"{email or 'an empty email'} isn't an email address")
        if email.lower() in seen:
            continue
        seen.add(email.lower())
        name = " ".join(_NAME_DROP.sub(" ", person.name).split())[:100]
        cleaned.append(Person(name=name, email=email))
    return _capped(cleaned, "people")


def clean_emoji(names: list[str]) -> list[str]:
    """Emoji as Slack's reaction events name them: no colons, lower case, no skin
    tone, thumbsup as +1, each once."""
    cleaned: list[str] = []
    for raw in names:
        name = _emoji_name(raw)
        if name is None:
            raise GuardrailsError(
                f"{raw.strip() or 'an empty name'} isn't an emoji name; use the name Slack "
                "shows for it, like ticket"
            )
        if name not in cleaned:
            cleaned.append(name)
    return _capped(cleaned, "emoji")


def clean_channels(names: list[str]) -> list[str]:
    """Slack channels by name, without the #, lower case, each once."""
    cleaned: list[str] = []
    for raw in names:
        name = _channel_name(raw)
        if name is None:
            raise GuardrailsError(
                f"{raw.strip() or 'an empty name'} isn't a Slack channel name; use the name "
                "Slack shows, like sre-requests"
            )
        if name not in cleaned:
            cleaned.append(name)
    return _capped(cleaned, "channels")


def clean_repositories(names: list[str]) -> list[str]:
    """Repositories as owner/name, each once whatever its case, keeping the case
    they came with."""
    cleaned: list[str] = []
    for raw in names:
        name = raw.strip()
        if not _REPOSITORY.match(name):
            raise GuardrailsError(f"{name or 'an empty name'} isn't a repository; use owner/name")
        if name.lower() not in {done.lower() for done in cleaned}:
            cleaned.append(name)
    return _capped(cleaned, "repositories")


def clean_lists(lists: RuleLists) -> RuleLists:
    """Every list of a rule, cleaned as its kind's cleaner says."""
    return RuleLists(
        people=tuple(clean_people(list(lists.people))),
        emoji=tuple(clean_emoji(list(lists.emoji))),
        channels=tuple(clean_channels(list(lists.channels))),
        repositories=tuple(clean_repositories(list(lists.repositories))),
    )


def compose_line(text: str, lists: RuleLists) -> str:
    """The rule's line in the file: its words, then each list it names, in
    LIST_KINDS order."""
    parts = [text]
    if lists.people:
        named = "; ".join(f"{p.name} <{p.email}>" if p.name else f"<{p.email}>" for p in lists.people)
        parts.append(f"(These people: {named})")
    if lists.emoji:
        parts.append("(These emoji: " + ", ".join(f":{name}:" for name in lists.emoji) + ")")
    if lists.channels:
        parts.append("(These channels: " + ", ".join(f"#{name}" for name in lists.channels) + ")")
    if lists.repositories:
        parts.append("(These repositories: " + ", ".join(lists.repositories) + ")")
    return " ".join(parts)


def _parse_items(kind: str, raw: str) -> tuple | None:
    """One list's entries as `compose_line` writes them, or None when any entry is
    not one (the line is then words up to there, as written)."""
    entries = [entry.strip() for entry in raw.split(";" if kind == "people" else ",")]
    if not entries or not all(entries):
        return None
    if kind == "people":
        people = []
        for entry in entries:
            person = _PERSON.match(entry)
            if person is None or not _EMAIL.match(person.group("email")):
                return None
            people.append(Person(name=person.group("name").strip(), email=person.group("email")))
        return tuple(people)
    # Read as the runtime names them, so a line written by hand means what it says:
    # :thumbsup: is the +1 the runtime reports, #SRE is #sre.
    if kind == "emoji":
        if not all(len(entry) > 2 and entry[0] == entry[-1] == ":" for entry in entries):
            return None
        emoji = [_emoji_name(entry) for entry in entries]
        return None if None in emoji else tuple(emoji)
    if kind == "channels":
        if not all(entry.startswith("#") for entry in entries):
            return None
        channels = [_channel_name(entry) for entry in entries]
        return None if None in channels else tuple(channels)
    return tuple(entries) if all(_REPOSITORY.match(entry) for entry in entries) else None


def split_line(line: str) -> tuple[str, RuleLists]:
    """A line's words and the lists it names, read from its end. A list out of
    LIST_KINDS order, a kind twice, or an entry that is not one ends the reading:
    from there leftwards the line is words, as written."""
    found: dict[str, tuple] = {}
    rest = line
    bound = len(LIST_KINDS)
    while True:
        match = _LIST_SUFFIX.search(rest)
        if match is None:
            break
        index = LIST_KINDS.index(match.group("kind"))
        if index >= bound:
            break
        items = _parse_items(match.group("kind"), match.group("items"))
        if items is None:
            break
        found[match.group("kind")] = items
        bound = index
        rest = rest[: match.start()]
    if not found:
        return line, RuleLists()
    return rest, RuleLists(**found)


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
        words, lists = split_line(rule)
        if not words.strip():
            raise GuardrailsError("a rule needs words as well as what it names")
        if lists and _NAMED_LIST.search(words):
            # A list left in the words: the model would read both and the panel would
            # show one. A line from the repository whose ending only looks like a
            # list names nothing and passes as written.
            raise GuardrailsError("a rule names each list once")
        for kind in LIST_KINDS:
            _capped(list(getattr(lists, kind)), kind)
        if len(rule) > RULE_LINE_MAX_CHARS:
            raise GuardrailsError(
                f"a rule and what it names are at most {RULE_LINE_MAX_CHARS:,} characters; "
                "name fewer"
            )
        if len(words) > RULE_MAX_CHARS:
            raise GuardrailsError(
                f"a rule is at most {RULE_MAX_CHARS} characters; split a longer one, "
                "or move the steps into a skill"
            )
        if "<!--" in rule or "-->" in rule:
            # The file's comment markers: the runtime would drop what is between them.
            raise GuardrailsError("a rule can't hold '<!--' or '-->'")
        if rule in cleaned:
            raise GuardrailsError(f"that rule is on the list twice: {rule!r}")
        cleaned.append(rule)
    if parse_rules(render_file(cleaned)) != cleaned:
        # A backstop: whatever the file would not give back as written is refused.
        raise GuardrailsError("a rule would not read back as written; reword it")
    if used_chars(cleaned) > RULES_MAX_CHARS:
        raise GuardrailsError(
            f"the list is over its {RULES_MAX_CHARS:,}-character limit; shorten a rule, or "
            "move rules that only matter inside one job into that job's skill"
        )
    return cleaned


def fit_starter_rules(rules: list[str]) -> tuple[list[str], list[str]]:
    """Of a persona's rules, the ones that start an agent's guardrails, in order, and
    the ones left in its persona. A rule the list would refuse (too long, a comment
    marker, a repeat) or one past the limit stays where it was, so nothing a persona
    said is lost on the way in."""
    starter: list[str] = []
    left: list[str] = []
    used = 0
    for rule in rules:
        cost = len(f"- {rule}\n")
        fits = (
            len(rule) <= RULE_MAX_CHARS
            and "<!--" not in rule
            and "-->" not in rule
            and rule not in starter
            and used + cost <= RULES_MAX_CHARS
        )
        if fits:
            starter.append(rule)
            used += cost
        else:
            left.append(rule)
    return starter, left


def persona_with_rules(body: str, rules: list[str]) -> str:
    """`body` with a house `## Rules` section holding `rules` again, or `body` alone
    when there are none: what stays in the persona when not every rule fits."""
    if not rules:
        return body
    return body.rstrip("\n") + "\n\n## Rules\n\n" + "".join(f"- {rule}\n" for rule in rules)


def starter_guardrails(persona_body: str) -> tuple[str, list[str]]:
    """The persona body an agent is born with and its starter guardrails: the
    persona's rules move into the list, minus any the list could not hold."""
    from kobo_api.personas import split_rules

    body, rules = split_rules(persona_body)
    starter, left = fit_starter_rules(rules)
    return persona_with_rules(body, left), starter


def record_starter_rows(session: Session, agent: Agent, rules: list[str]) -> None:
    """The provenance of an agent's starter guardrails, uncommitted: each from the
    persona it was born with. Re-running a create keeps the same rows."""
    items = [RuleIn(text=rule, source_kind="persona", source_ref=agent.persona) for rule in rules]
    sync_rows(session, agent.name, items, agent.created_by or "room")


def render_file(rules: list[str]) -> str:
    """The file the room writes and relays, byte for byte."""
    return _HEADER + "".join(f"- {rule}\n" for rule in rules)


def file_sha(text: str) -> str:
    """The version a person's save names (`base_sha`): the file's own sha256, the
    same bytes the runtime hashes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_guardrails(github: GitHubClient, repo: str) -> str:
    """The repository's guardrails file, or "" when it has none yet. One file read,
    no clone."""
    return github.read_file(repo, GUARDRAILS_PATH) or ""


def save_guardrails_file(
    github: GitHubClient, repo: str, content: str, *, archive_only: bool = False
) -> str:
    """Write `bundle/guardrails.md` and push it; returns the pushed commit's sha.
    `archive_only` as in `kobo_api.knowledge.save_knowledge_file`: after a relay
    the agent already has the list, so the commit only mirrors it (`[skip ci]`);
    without one, the commit is the delivery and keeps CI. The caller holds
    `repo_write_lock(repo)` around its whole save (the version check, the relay
    and this), so two saves of one list can never both pass the check."""
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


def rule_payload(row: GuardrailRule | None, line: str) -> dict:
    """One rule as the panel and the room's assistant tools read it: `text` is its
    words and `people`, `emoji`, `channels` and `repositories` what it names, all
    from `line`, the rule as the file has it."""
    text, lists = split_line(line)
    named = {
        "people": [{"name": p.name, "email": p.email} for p in lists.people],
        "emoji": list(lists.emoji),
        "channels": list(lists.channels),
        "repositories": list(lists.repositories),
    }
    if row is None:
        return {"id": None, "text": text, **named, "line": line,
                "source": {"kind": SOURCE_REPOSITORY, "ref": None},
                "added_by": None, "added_at": None, "edited_by": None, "edited_at": None,
                "breaks": 0, "last_broken_at": None}
    return {
        "id": row.id,
        "text": text,
        **named,
        "line": line,
        "source": {"kind": row.source_kind, "ref": row.source_ref},
        "added_by": row.added_by,
        "added_at": row.added_at.isoformat() if row.added_at else None,
        "edited_by": row.edited_by,
        "edited_at": row.edited_at.isoformat() if row.edited_at else None,
        "breaks": row.breaks or 0,
        "last_broken_at": row.last_broken_at.isoformat() if row.last_broken_at else None,
    }


def annotate(session: Session, agent_name: str, rules: list[str]) -> list[dict]:
    """The file's rules in order, each with its row's provenance when it has one. A
    row goes to one line only: a line repeated in the repository reads as written
    there the second time, so no two rules share an id."""
    rows = session.scalars(
        select(GuardrailRule).where(GuardrailRule.agent_name == agent_name)
    ).all()
    by_text: dict[str, GuardrailRule] = {}
    for row in rows:
        by_text.setdefault(row.text, row)
    out = []
    for text in rules:
        out.append(rule_payload(by_text.pop(text, None), text))
    return out


def checked_source(session: Session, agent: Agent, kind: str, ref: str | None) -> tuple[str, str | None]:
    """The source a saved rule may claim. The persona must be the agent's, a skill
    one it has, a correction one of its own learnings; anything else is recorded as
    the person's, so a label never names a source the rule did not come from."""
    if kind == "persona" and ref == agent.persona:
        return kind, ref
    if kind == "skill" and ref in {entry.get("name") for entry in agent.skills or []}:
        return kind, ref
    if kind == "correction" and ref is not None and ref.isdigit():
        learning = session.get(AgentLearning, int(ref))
        if learning is not None and learning.agent_name == agent.name:
            return kind, ref
    return SOURCE_PERSON, None


def sync_rows(session: Session, agent_name: str, items: list[RuleIn], actor: str) -> None:
    """Make the agent's rows match a saved list, uncommitted. A line sent with its
    row's id is that row, edited if the text changed; a line whose text matches a
    row keeps it; then a line whose words match a row's, with other people, is that
    row edited, so a rule whose people an assistant changed keeps where it came from
    and its breaks; any other line is new. Rows no line kept are deleted."""
    rows = session.scalars(
        select(GuardrailRule).where(GuardrailRule.agent_name == agent_name)
    ).all()
    by_id = {row.id: row for row in rows}
    kept: set[int] = set()
    # Ids first: an edited line names its row, and a line elsewhere in the same
    # save that still says the old text must not take that row from it.
    claimed: list[GuardrailRule | None] = []
    for item in items:
        row = by_id.get(item.id) if item.id is not None else None
        if row is not None and row.id not in kept:
            kept.add(row.id)
            claimed.append(row)
        else:
            claimed.append(None)
    by_text: dict[str, GuardrailRule] = {}
    by_words: dict[str, GuardrailRule] = {}
    for row in rows:
        if row.id not in kept:
            by_text.setdefault(row.text, row)
            by_words.setdefault(split_line(row.text)[0], row)
    exact = {item.text for item in items}
    now = datetime.now(UTC)
    for item, row in zip(items, claimed, strict=True):
        if row is None:
            row = by_text.pop(item.text, None)
        if row is None:
            # Same words, other people: never a row some line in this save still
            # names exactly, and never one already taken.
            candidate = by_words.get(split_line(item.text)[0])
            if candidate is not None and candidate.id not in kept and candidate.text not in exact:
                row = candidate
        if row is not None:
            # Taken now: out of both maps, but only where it is the entry, since two
            # rows may share a line written twice in the repository.
            if by_text.get(row.text) is row:
                del by_text[row.text]
            words = split_line(row.text)[0]
            if by_words.get(words) is row:
                del by_words[words]
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
