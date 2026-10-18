"""The team-guide archetype: template rendering and the voice overlay's bounds."""

from kobo_api.archetypes import (
    render_team_guide,
    team_guide_persona_id,
)


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


def test_voice_containing_placeholder_token_is_not_reinterpolated():
    # voice="{group} Cowboy" must land literally in the voice slots; it must
    # never be re-scanned and have {group} resolved to "sre".
    body = render_team_guide("atlas", "sre", "{group} Cowboy")
    assert "cadence of {group} Cowboy" in body
    assert "never claim to be {group} Cowboy" in body
    assert "cadence of sre Cowboy" not in body


def test_name_containing_placeholder_token_stays_literal_in_name_slot():
    body = render_team_guide("{group}", "sre", None)
    assert "You are {group}, the informational guide for the sre team." in body


def test_group_containing_placeholder_token_stays_literal_in_group_positions():
    body = render_team_guide("atlas", "{name}", None)
    assert "the informational guide for the {name} team." in body
    assert "other teams wondering what {name}\ndoes" in body
