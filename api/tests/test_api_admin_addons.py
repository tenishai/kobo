"""The admin panel's Add-ons route (#173 step 2): 404 while the room has the
panel off, 403 for a non-admin, and derived cards for an admin."""

from __future__ import annotations

from kobo_api.admin_addons import addon_cards
from kobo_api.config import Config
from tests.test_api_org_status import (
    ASSERTION_HEADER,
    WHOAMI_AUDIENCE,
    _generate_jwk,
    _iap_client,
    _sign_whoami,
)


def _config(tmp_path, **overrides) -> Config:
    return Config(
        workdir=tmp_path / "work",
        iap_audience=WHOAMI_AUDIENCE,
        operators=["ops@example.com"],
        **overrides,
    )


def test_panel_off_is_404_even_for_an_admin(tmp_path):
    private_key, jwk = _generate_jwk()
    client = _iap_client(_config(tmp_path), tmp_path, jwk=jwk)
    token = _sign_whoami(private_key, email="ops@example.com")

    response = client.get("/api/admin/addons", headers={ASSERTION_HEADER: token})

    assert response.status_code == 404
    assert response.json() == {"error": "the admin panel is off for this room"}


def test_non_admin_is_403(tmp_path):
    private_key, jwk = _generate_jwk()
    client = _iap_client(_config(tmp_path, admin_panel=True), tmp_path, jwk=jwk)
    token = _sign_whoami(private_key, email="someone@example.com")

    response = client.get("/api/admin/addons", headers={ASSERTION_HEADER: token})

    assert response.status_code == 403
    assert response.json() == {"error": "the admin panel is for the room's admins"}


def test_admin_gets_one_card_per_addon_with_derived_state(tmp_path):
    private_key, jwk = _generate_jwk()
    config = _config(tmp_path, admin_panel=True, sero_url="http://sero.internal", noto_url="http://noto")
    client = _iap_client(config, tmp_path, jwk=jwk)
    token = _sign_whoami(private_key, email="ops@example.com")

    response = client.get("/api/admin/addons", headers={ASSERTION_HEADER: token})

    assert response.status_code == 200
    cards = {c["id"]: c for c in response.json()["addons"]}
    assert list(cards) == [
        "deployment", "secrets", "observability", "switchboard",
        "tool-gateway", "memory", "model-gateway",
    ]
    assert cards["secrets"]["state"] == "on"
    assert cards["secrets"]["keys"] == ["sero_url", "sero_audience", "sero_namespace"]
    # noto needs both keys; one set is still off, and the detail says which.
    assert cards["observability"]["state"] == "off"
    assert "noto_project" in cards["observability"]["detail"]
    assert cards["memory"]["state"] == "on"
    # No clients wired in this app: the platform card is honest about it.
    assert cards["deployment"]["state"] in {"error", "off"}


def test_addon_cards_never_raise_without_clients(tmp_path):
    cards = addon_cards(_config(tmp_path, gojira_cluster=""), None)
    assert cards[0] == {
        "id": "deployment",
        "name": "Deployment",
        "summary": "Turns each agent's repository into a running deployment and keeps it there.",
        "state": "off",
        "detail": "No cluster configured; Create cannot run without it.",
        "keys": ["gojira_cluster", "gojira_url", "gojira_token"],
        "owner": "deploy team",
    }
