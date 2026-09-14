"""The team-guide archetype: template rendering and the voice overlay's bounds."""

from kobo_api.archetypes import render_team_guide, team_guide_persona_id


def test_render_interpolates_name_and_group():
    body = render_team_guide("atlas", "sre", None)
    assert "You are atlas, the informational guide for the sre team." in body
    assert "{name}" not in body and "{group}" not in body


def test_no_voice_leaves_no_residue():
    body = render_team_guide("atlas", "sre", None)
    assert "{voice_section}" not in body
    assert "Style:" not in body
    assert not body.rstrip().endswith("}")


def test_voice_overlay_is_subordinate_style():
    body = render_team_guide("atlas", "sre", "Dolly Parton")
    assert "Style: channel the conversational voice and cadence of Dolly Parton" in body
    assert "never claim to be Dolly Parton" in body
    # constitution precedes voice: Never act appears before Style
    assert body.index("Never act.") < body.index("Style:")


def test_persona_id_is_per_group():
    assert team_guide_persona_id("sre") == "team-guide-sre"
