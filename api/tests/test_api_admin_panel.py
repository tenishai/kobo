"""The admin panel's People and Audit routes (#173 step 3 opening): the shared
gate, the real lists, and the store's events read back."""

from __future__ import annotations

from kobo_api import admin_store
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
        admin_panel=True,
        **overrides,
    )


def test_people_reads_the_org_keys(tmp_path):
    private_key, jwk = _generate_jwk()
    cfg = _config(tmp_path, reader_groups=["sre-leads"], slack_operators=["s@example.com"])
    client = _iap_client(cfg, tmp_path, jwk=jwk)
    token = _sign_whoami(private_key, email="ops@example.com")

    response = client.get("/api/admin/people", headers={ASSERTION_HEADER: token})

    assert response.status_code == 200
    assert response.json() == {
        "admin_groups": [],
        "operators": ["ops@example.com"],
        "reader_groups": ["sre-leads"],
        "slack_operators": ["s@example.com"],
    }


def test_people_and_audit_share_the_gate(tmp_path):
    private_key, jwk = _generate_jwk()
    client = _iap_client(_config(tmp_path), tmp_path, jwk=jwk)
    token = _sign_whoami(private_key, email="someone@example.com")

    for path in ("/api/admin/people", "/api/admin/audit"):
        response = client.get(path, headers={ASSERTION_HEADER: token})
        assert response.status_code == 403, path


def test_audit_reads_the_store_newest_first(tmp_path):
    private_key, jwk = _generate_jwk()
    client = _iap_client(_config(tmp_path), tmp_path, jwk=jwk)
    token = _sign_whoami(private_key, email="ops@example.com")
    with client.app.state.session_factory() as session:
        admin_store.put_setting(session, "panel.preferences", {"dense": True}, actor="a@example.com")
        admin_store.record(
            session, actor="b@example.com", action="grant.create", object_type="grant",
            object_id="sage/github", before=None, after={"access": "read"},
        )
        session.commit()

    response = client.get("/api/admin/audit", headers={ASSERTION_HEADER: token})

    assert response.status_code == 200
    events = response.json()["events"]
    assert [e["action"] for e in events] == ["grant.create", "setting.put"]
    assert events[0]["landed"] == "pending" and events[1]["landed"] == "applied"
    assert set(events[0]) == {"id", "at", "actor", "action", "object_type", "object_id", "landed", "landed_detail"}


def test_admin_room_names_the_org_repo_runtime_and_cluster(tmp_path):
    """The panel's rail shows real facts about this room, not a demo's."""
    from kobo_api.config import Config
    from tests.test_api_org_status import (
        ASSERTION_HEADER,
        WHOAMI_AUDIENCE,
        _generate_jwk,
        _iap_client,
        _sign_whoami,
    )

    private_key, jwk = _generate_jwk()
    cfg = Config(
        workdir=tmp_path / "work", iap_audience=WHOAMI_AUDIENCE, operators=["ops@example.com"], admin_panel=True,
        org_repo="example/kobo-config", runtime_image="registry/daedalus-runtime@sha256:abc", gojira_cluster="agents-dev",
    )
    client = _iap_client(cfg, tmp_path, jwk=jwk)
    headers = {ASSERTION_HEADER: _sign_whoami(private_key, email="ops@example.com")}
    body = client.get("/api/admin/room", headers=headers).json()
    assert body == {
        "org_repo": "example/kobo-config",
        "runtime_image": "registry/daedalus-runtime@sha256:abc",
        "cluster": "agents-dev",
        "delivery_backend": "deployed",
    }
