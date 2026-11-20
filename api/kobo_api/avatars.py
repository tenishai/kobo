"""Avatar module (P2d): a deterministic prompt template for generating an
agent's face, plus the clone/write/commit idiom that pushes the generated PNG
to `bundle/avatar.png` in the agent's repo (the bundle of record) — the same
disposable-clone pattern `kobo_api.knowledge` uses, just writing bytes
instead of text this time.

The generated PNG itself is produced by `clients.image` (`kobo_api.clients.
ImageClient`) and stored by the caller (`kobo_api.main`'s avatar route),
which also caches a base64 copy on the `Agent` row for fast list/detail
rendering — this module only owns the prompt text and the repo push, mirroring
how `kobo_api.knowledge` owns the file write/push but never the route's
guards or response shape.

Two things changed on 2026-11-20 (Kyle: the dashboard's pictures "load in
slowly every single time", and an agent named after a famous character got
no picture at all):

- Every picture is kept with a version (a short digest of its bytes) and a
  small copy (`PORTRAIT_SMALL_PX` square, WebP). The page asks for the small
  copy by version, so a browser keeps it until the picture changes, instead
  of downloading a 1 MB, 1024px PNG for a 28px face on every load.
- An agent always gets a picture (`paint_portrait`): the persona prompt with
  the agent's name, then the same prompt without it (a famous
  character's name invites a refusal), then a placeholder drawn here, its
  initials in pixel letters on a color its name picks.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from kobo_api import delivery
from kobo_api.clients import GitHubClient

log = logging.getLogger(__name__)

# The small copy's side: the cards show a face at 28px and the page header at
# 56px, so 160px stays sharp on a dense screen.
PORTRAIT_SMALL_PX = 160
# A picture larger than this is not decoded for a small copy: a PNG of a few
# megabytes can claim a size that would take gigabytes to open.
_MAX_SOURCE_PIXELS = 4096 * 4096


def avatar_prompt(agent_name: str, persona_description: str, purpose_tags: list[str] | None = None) -> str:
    """A deterministic prompt for `clients.image.generate` — same inputs
    always produce the exact same string, so a route can be tested against
    it byte-for-byte without depending on the image backend at all.

    Named person-first ("a character named {name}") and enriched with persona
    description and optional purpose tags. Closes on an explicit no-text
    clause: the model this backs (Vertex's `gemini-2.5-flash-image`, see
    `VertexImageClient`) tends to render stray lettering into "portrait"
    prompts unless told not to, and an avatar is a small square rendered at
    icon size -- any text in it reads as noise, never as a legible label.

    When purpose_tags is empty or None, the themes clause is omitted entirely.
    """
    parts = [
        (f"friendly square avatar portrait of a character named {agent_name}; "
        f"persona: {persona_description};")
    ]

    if purpose_tags:
        tags_str = ", ".join(purpose_tags)
        parts.append(f" themes: {tags_str};")

    parts.append(
        f" the face and styling should feel like a \"{agent_name}\" -- let the name "
        f"and persona set the character's vibe; flat, minimal, centered, plain "
        f"background; no text in image."
    )

    return "".join(parts)


def set_avatar(github: GitHubClient, *, repo: str, png: bytes) -> str:
    """Write (or overwrite) `bundle/avatar.png` with `png` and push it.

    Exactly `kobo_api.knowledge.save_knowledge_file`'s clone/write/commit/
    local_head idiom, with two differences: this always writes the same
    fixed filename (there's only ever one avatar per agent, no user-supplied
    name to validate), and it writes bytes (`Path.write_bytes`) rather than
    text, since a PNG isn't UTF-8. Regenerating just overwrites the same
    path — the commit history is the only place an old avatar survives.

    Returns the pushed commit's local sha (`github.local_head`), a fresh
    clone's own HEAD right after the push — see `save_knowledge_file`'s
    docstring for why that's preferred over a `head_sha(repo)` API read.
    """
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        bundle_dir = workdir / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "avatar.png").write_bytes(png)
        delivery.push(github, workdir, "kobo: set avatar")
        return github.local_head(workdir)


def avatar_prompt_without_name(persona_description: str, purpose_tags: list[str] | None = None) -> str:
    """The second try, when the first prompt was refused or failed: the persona
    and its themes, and no name, since a famous character's name is what an
    image model most often refuses."""
    parts = [
        (
            "friendly square avatar portrait of an original character; "
            f"persona: {persona_description};"
        )
    ]
    if purpose_tags:
        parts.append(f" themes: {', '.join(purpose_tags)};")
    parts.append(
        " an original design, not based on any existing character or person; flat, "
        "minimal, centered, plain background; no text in image."
    )
    return "".join(parts)


@dataclass(frozen=True)
class Painted:
    """A picture for an agent and how it came to be: `painted`,
    `painted-without-name` or `placeholder`."""

    png: bytes
    how: str


PAINTED = "painted"
PAINTED_WITHOUT_NAME = "painted-without-name"
PLACEHOLDER = "placeholder"


def paint_portrait(
    image, *, name: str, description: str, tags: list[str], max_bytes: int
) -> Painted:
    """Always a picture. With an image backend: the usual prompt, then one
    without the agent's name; a refusal, an error or a picture over
    `max_bytes` moves on to the next. Without one, or when both fail, the
    placeholder. Failures are logged here, never raised."""
    if image is not None:
        nameless = description if name.lower() not in description.lower() else "an AI assistant"
        tries = (
            (PAINTED, avatar_prompt(name, description, tags)),
            (PAINTED_WITHOUT_NAME, avatar_prompt_without_name(nameless, tags)),
        )
        for how, prompt in tries:
            try:
                png = image.generate(prompt)
            except Exception as exc:  # noqa: BLE001 -- a picture is never a reason to fail
                log.warning("portrait for %s (%s) failed: %s", name, how, exc)
                continue
            if len(png) > max_bytes:
                log.warning(
                    "portrait for %s (%s) came out at %d bytes, over %d", name, how, len(png), max_bytes
                )
                continue
            return Painted(png=png, how=how)
    return Painted(png=placeholder_portrait(name), how=PLACEHOLDER)


def portrait_version(png: bytes) -> str:
    """A short digest of the picture's bytes: what the page asks for, so a
    browser can keep a picture until it changes."""
    return hashlib.sha256(png).hexdigest()[:12]


def small_portrait(png: bytes) -> bytes | None:
    """The picture at `PORTRAIT_SMALL_PX` square as WebP, for the cards and the
    page header, or None when it can't be read (the full picture is served
    instead)."""
    try:
        with Image.open(io.BytesIO(png)) as source:
            width, height = source.size
            if width * height > _MAX_SOURCE_PIXELS:
                return None
            source.load()
            has_alpha = source.mode in ("RGBA", "LA") or "transparency" in source.info
            frame = source.convert("RGBA" if has_alpha else "RGB")
        frame.thumbnail((PORTRAIT_SMALL_PX, PORTRAIT_SMALL_PX), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        frame.save(out, format="WEBP", quality=82, method=6)
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001 -- the full picture still serves
        log.warning("no small copy of a portrait: %s", exc)
        return None


def cache_portrait(agent, png: bytes) -> str:
    """Keep `png` as `agent`'s picture on its row: the picture, its version and
    its small copy, all written together. Returns the version."""
    agent.avatar_png = base64.b64encode(png).decode("ascii")
    agent.avatar_version = portrait_version(png)
    small = small_portrait(png)
    agent.avatar_small = base64.b64encode(small).decode("ascii") if small else None
    return agent.avatar_version


# --- The placeholder: initials in pixel letters ------------------------------
#
# Drawn on a 32 by 32 grid and scaled up without smoothing, in the same blocky
# letters as the room's wordmark. The color comes from the name, so an agent
# keeps the same placeholder and two agents rarely share one.

_GRID = 32
_SCALE = 16  # 512px square
# 5 by 7 letters, one string per row, "#" is ink.
_GLYPHS = {
    "A": [".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "B": ["####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."],
    "C": [".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."],
    "D": ["####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."],
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
    "F": ["#####", "#....", "#....", "####.", "#....", "#....", "#...."],
    "G": [".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".####"],
    "H": ["#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "I": [".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "J": ["..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."],
    "K": ["#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"],
    "L": ["#....", "#....", "#....", "#....", "#....", "#....", "#####"],
    "M": ["#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"],
    "N": ["#...#", "#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#"],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "P": ["####.", "#...#", "#...#", "####.", "#....", "#....", "#...."],
    "Q": [".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"],
    "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
    "S": [".####", "#....", "#....", ".###.", "....#", "....#", "####."],
    "T": ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."],
    "U": ["#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "V": ["#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."],
    "W": ["#...#", "#...#", "#...#", "#.#.#", "#.#.#", "#.#.#", ".#.#."],
    "X": ["#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"],
    "Y": ["#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."],
    "Z": ["#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"],
    "0": [".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."],
    "1": ["..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "2": [".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"],
    "3": ["####.", "....#", "....#", ".###.", "....#", "....#", "####."],
    "4": ["...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."],
    "5": ["#####", "#....", "####.", "....#", "....#", "#...#", ".###."],
    "6": [".###.", "#....", "#....", "####.", "#...#", "#...#", ".###."],
    "7": ["#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."],
    "8": [".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."],
    "9": [".###.", "#...#", "#...#", ".####", "....#", "....#", ".###."],
    "?": [".###.", "#...#", "....#", "...#.", "..#..", ".....", "..#.."],
}
# Background and ink pairs, soft enough to sit beside painted portraits.
_PALETTE = (
    ((219, 234, 254), (30, 64, 175)),
    ((220, 252, 231), (22, 101, 52)),
    ((254, 243, 199), (146, 64, 14)),
    ((252, 231, 243), (157, 23, 77)),
    ((237, 233, 254), (91, 33, 182)),
    ((204, 251, 241), (17, 94, 89)),
    ((255, 237, 213), (154, 52, 18)),
    ((241, 245, 249), (51, 65, 85)),
)


def _initials(name: str) -> str:
    """Up to two letters: the first of each of the first two words of the name."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name) if w]
    letters = "".join(w[0] for w in words[:2]).upper()
    return letters or "?"


def placeholder_portrait(name: str) -> bytes:
    """A 512px PNG: the agent's initials in pixel letters on a color its name
    picks. The same name always draws the same picture."""
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    background, ink = _PALETTE[digest[0] % len(_PALETTE)]
    grid = Image.new("RGB", (_GRID, _GRID), background)
    letters = _initials(name)
    glyph_w, glyph_h, scale, gap = 5, 7, 2, 2
    width = len(letters) * glyph_w * scale + (len(letters) - 1) * gap
    left = (_GRID - width) // 2
    top = (_GRID - glyph_h * scale) // 2
    for index, letter in enumerate(letters):
        rows = _GLYPHS.get(letter, _GLYPHS["?"])
        x0 = left + index * (glyph_w * scale + gap)
        for row, line in enumerate(rows):
            for col, cell in enumerate(line):
                if cell != "#":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        grid.putpixel((x0 + col * scale + dx, top + row * scale + dy), ink)
    portrait = grid.resize((_GRID * _SCALE, _GRID * _SCALE), Image.Resampling.NEAREST)
    out = io.BytesIO()
    portrait.save(out, format="PNG", optimize=True)
    return out.getvalue()
