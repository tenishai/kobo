"""Roles from the admin panel (#173 step 3): built-ins seeded from the org
keys on first read, custom roles from the same lists, built-ins undeletable."""

from __future__ import annotations

from kobo_api.config import Config
from tests.test_api_org_status import (
    ASSERTION_HEADER,
    WHOAMI_AUDIENCE,
    _generate_jwk,
    _iap_client,
    _sign_whoami,
)


def _client(tmp_path, **overrides):
    values = {
        "workdir": tmp_path / "work", "iap_audience": WHOAMI_AUDIENCE, "operators": ["ops@example.com"],
        "admin_panel": True, "admin_groups": [], "reader_groups": ["sre-leads"],
    }
    values.update(overrides)
    private_key, jwk = _generate_jwk()
    client = _iap_client(Config(**values), tmp_path, jwk=jwk)
    return client, {ASSERTION_HEADER: _sign_whoami(private_key, email="ops@example.com")}


def test_first_read_seeds_the_four_builtins_from_the_org_keys(tmp_path):
    client, headers = _client(tmp_path)

    body = client.get("/api/admin/roles", headers=headers).json()

    roles = {r["id"]: r for r in body["roles"]}
    assert list(roles) == ["admin", "operator", "member", "reader"]
    assert all(roles[r]["builtin"] for r in roles)
    assert roles["reader"]["groups"] == ["sre-leads"] and roles["reader"]["capabilities"] == []
    assert roles["admin"]["capabilities"] == [c["id"] for c in body["catalogue"]["capabilities"]]
    assert "sre-leads" in body["catalogue"]["groups"]
    # A second read seeds nothing more and keeps the same four.
    again = client.get("/api/admin/roles", headers=headers).json()
    assert [r["id"] for r in again["roles"]] == ["admin", "operator", "member", "reader"]


def test_a_custom_role_is_saved_from_the_same_lists_and_can_be_deleted(tmp_path):
    client, headers = _client(tmp_path)
    client.get("/api/admin/roles", headers=headers)
    body = {
        "name": "Data engineering", "description": "The data team's agents", "groups": ["data"],
        "capabilities": ["create_agents", "chat_teach"], "admin": [],
        "connectors": [{"plugin": "datadog", "ceiling": "read"}], "models": [],
    }

    created = client.put("/api/admin/roles/data-engineering", json=body, headers=headers)
    assert created.status_code == 201
    assert created.json()["groups"] == ["data"] and created.json()["builtin"] is False

    updated = client.put("/api/admin/roles/data-engineering", json={**body, "groups": ["data", "growth"]}, headers=headers)
    assert updated.status_code == 200 and updated.json()["groups"] == ["data", "growth"]

    bad = client.put("/api/admin/roles/x", json={**body, "connectors": [{"plugin": "datadog", "ceiling": "all"}]}, headers=headers)
    assert bad.status_code == 422

    assert client.delete("/api/admin/roles/data-engineering", headers=headers).status_code == 200
    assert client.delete("/api/admin/roles/data-engineering", headers=headers).status_code == 404
    audit = [e["action"] for e in client.get("/api/admin/audit", headers=headers).json()["events"]]
    assert audit[:3] == ["role.delete", "role.update", "role.create"]


def test_a_builtin_cannot_be_deleted_but_can_be_edited(tmp_path):
    client, headers = _client(tmp_path)
    client.get("/api/admin/roles", headers=headers)

    refused = client.delete("/api/admin/roles/admin", headers=headers)
    assert refused.status_code == 409 and "built-in" in refused.json()["error"]

    edited = client.put(
        "/api/admin/roles/admin",
        json={"name": "Admin", "description": "Runs the room.", "groups": ["platform-admins"],
              "capabilities": [], "admin": ["view_audit"], "connectors": [], "models": []},
        headers=headers,
    )
    assert edited.status_code == 200
    assert edited.json()["builtin"] is True and edited.json()["groups"] == ["platform-admins"]
