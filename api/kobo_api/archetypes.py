"""Archetype agents: pre-shaped, one-click deployables (team-guide spec 2026-09-14).

An archetype is a create-time recipe, not a runtime concept: the route renders
the packaged template into an ordinary room persona and forces the bundle
surface (tools, tags, visibility, skills), then the standard pipeline runs
unchanged. `_resolve_persona_body` never learns archetypes exist.
"""

from pathlib import Path

ARCHETYPE_TEAM_GUIDE = "team_guide"
TEAM_GUIDE_TAG = "team-guide"

_TEMPLATE = Path(__file__).parent / "templates" / "archetypes" / "team-guide.md"

_VOICE_SECTION = (
    "\nStyle: channel the conversational voice and cadence of {voice} — the wit,"
    "\nwarmth, and rhythm, not the identity. You are always {name}, {group}'s"
    "\nguide; you never claim to be {voice}, and the style never overrides your"
    "\nconduct: you still refuse actions, still cite what you've been taught,"
    "\nstill interview.\n"
)


def team_guide_persona_id(group: str) -> str:
    return f"team-guide-{group}"


def render_team_guide(name: str, group: str, voice: str | None) -> str:
    body = _TEMPLATE.read_text()
    voice = (voice or "").strip()
    section = (
        _VOICE_SECTION.replace("{voice}", voice) if voice else ""
    )
    return (
        body.replace("{voice_section}", section)
        .replace("{name}", name)
        .replace("{group}", group)
    )
