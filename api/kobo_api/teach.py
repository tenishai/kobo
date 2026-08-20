"""Teach module (Task 1): turn an operator's distilled chat note into the
formatted markdown and filename the existing intake seam
(`kobo_api.intake.upload_intake_file`) needs to push it into
`bundle/intake/*` as a one-shot memory upload.

This module owns exactly two pure, side-effect-free functions -- no git,
no HTTP, no validation of its own. Whether the operator's raw text is
empty/whitespace-only or oversized is the `POST /api/agents/{name}/teach`
route's own concern (main.py), same as every other shape check that route
already inline-checks (see e.g. `chat_with_agent`'s "last message must
have role 'user'" check) -- these two functions must behave sanely on any
input a caller hands them, including the edge cases the route itself
would normally reject before ever calling in here, since a unit test is
free to call them directly.

`format_note` builds the note body; `note_filename` derives the upload's
filename. Both take the SAME `on_date` string (an ISO `YYYY-MM-DD`, the
route's own single UTC "now" computed once per request and passed to
both) -- one canonical date per request, not two independently-formatted
guesses that could drift apart.
"""

from __future__ import annotations

import re

# Every word-worthy character `note_filename`'s slug keeps: lowercase
# letters and digits (mirrors kobo_api.intake.INTAKE_FILENAME_RE's own
# `[a-z0-9-]` filename alphabet). Applied to `text.lower()`, so an
# uppercase letter still contributes a word; punctuation, whitespace,
# emoji, and everything else simply contributes nothing at all rather
# than raising or corrupting the filename.
_SLUG_WORD_RE = re.compile(r"[a-z0-9]+")

# How many of the text's leading words feed the slug, and how long (in
# characters) the joined, hyphenated slug may run before being cut off.
# Chosen so "chat-<8-digit date>-<slug>.md" always stays comfortably
# inside kobo_api.intake.INTAKE_FILENAME_RE's 40-char-before-".md" cap:
# "chat-" (5) + an 8-digit date + "-" (1) = 14 fixed chars, leaving 26
# free; capping the slug itself at 20 keeps a wide margin without ever
# needing to touch this number in lockstep with the intake regex.
_SLUG_MAX_WORDS = 6
_SLUG_MAX_CHARS = 20

# The filename's fallback slug when the text yields no [a-z0-9] word at
# all (empty text, all-whitespace, or all-punctuation/emoji) --
# INTAKE_FILENAME_RE requires at least one character right after
# "chat-<date>-", so an empty slug would otherwise produce an invalid
# intake filename outright.
_FALLBACK_SLUG = "note"


def format_note(text: str, *, taught_by: str | None, on_date: str) -> str:
    """The markdown pushed to `bundle/intake/*` for a "teach from chat"
    upload: a short attribution header (this was taught in chat, on
    `on_date`, by `taught_by` when the room actually knows who was signed
    in), a blank line, then `text` verbatim as the body.

    `taught_by` is the signed-in caller's IAP email when the room has one
    wired up (`request.state.iap_email`, read by the route), or `None`
    otherwise -- the attribution line is simply omitted rather than
    printed as some placeholder like "unknown", the same "omit rather
    than fake it" convention every other optional-detail field in this
    codebase already follows (e.g. `_org_config_check`'s `org.error or
    org.repo or ""`).

    `text` lands in the body byte-for-byte: never stripped, reformatted,
    or otherwise touched, even if it contains an em dash -- this
    codebase's own "no em dashes in user-facing copy" rule binds kobo's
    OWN template copy (the header line below), never a caller's own
    words. These notes are memory content, not UI chrome.
    """
    header = f"Taught in chat on {on_date}."
    if taught_by:
        header += f" Taught by {taught_by}."
    return f"{header}\n\n{text}\n"


def note_filename(text: str, *, on_date: str) -> str:
    """`chat-<YYYYMMDD>-<slug>.md`, matching
    `kobo_api.intake.INTAKE_FILENAME_RE` for any input.

    `on_date` is an ISO `YYYY-MM-DD` string (the same value passed to
    `format_note` for this same upload); the dashes are simply dropped to
    get the filename's compact 8-digit date.

    `slug` comes from up to the first `_SLUG_MAX_WORDS` words of `text`,
    lowercased, hyphen-joined, and length-capped -- any character outside
    `[a-z0-9]` (punctuation, whitespace, emoji, ...) contributes nothing
    to it rather than raising or corrupting the filename. Falls back to
    `_FALLBACK_SLUG` when that yields nothing at all (empty text,
    whitespace-only, or all-punctuation/emoji).

    Collision-tolerant by design, not by construction: the same text
    taught again on the same day produces this exact same filename, and
    that's deliberate -- re-teaching is just a fresh push to the same
    intake path, which the intake seam itself already treats as "same
    name, new content, a fresh commit" (see `upload_intake_file`'s own
    docstring), so `list_intake`'s ancestor-derived status naturally reads
    it as pending again until the seeder catches up. No hash/uniqueness
    suffix is needed here for that to work.
    """
    compact_date = on_date.replace("-", "")
    words = _SLUG_WORD_RE.findall(text.lower())[:_SLUG_MAX_WORDS]
    slug = "-".join(words)[:_SLUG_MAX_CHARS].rstrip("-") or _FALLBACK_SLUG
    return f"chat-{compact_date}-{slug}.md"
