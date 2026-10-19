"""The admin store's two rules: every write leaves an audit event in the same
transaction, and settings keys come from the registry."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api import admin_store
from kobo_api.models import Base


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = factory()
    yield s
    s.close()


def test_put_setting_writes_the_row_and_one_audit_event_together(session):
    admin_store.put_setting(session, "panel.preferences", {"dense": True}, actor="kyle@example.com")
    session.commit()

    assert admin_store.get_setting(session, "panel.preferences") == {"dense": True}
    events = admin_store.audit_events(session)
    assert [e.action for e in events] == ["setting.put"]
    assert events[0].before is None and events[0].after == {"dense": True}
    assert events[0].landed == admin_store.LANDED_APPLIED


def test_a_second_write_keeps_the_history_and_bumps_the_version(session):
    admin_store.put_setting(session, "panel.preferences", {"dense": True}, actor="a@example.com")
    row = admin_store.put_setting(session, "panel.preferences", {"dense": False}, actor="b@example.com")
    session.commit()

    assert row.version == 2 and row.updated_by == "b@example.com"
    events = admin_store.audit_events(session)
    assert len(events) == 2
    assert events[0].before == {"dense": True} and events[0].after == {"dense": False}


def test_an_unregistered_key_is_refused_before_anything_is_written(session):
    with pytest.raises(admin_store.UnknownSettingKey):
        admin_store.put_setting(session, "whatever.i.like", {"x": 1}, actor="a@example.com")
    with pytest.raises(admin_store.UnknownSettingKey):
        admin_store.get_setting(session, "whatever.i.like")
    assert admin_store.audit_events(session) == []


def test_a_projection_reports_back_on_the_event(session):
    event = admin_store.record(
        session, actor="a@example.com", action="grant.create", object_type="grant",
        object_id="sage/github", before=None, after={"access": "read"},
    )
    session.commit()
    assert event.landed == admin_store.LANDED_PENDING

    admin_store.mark_landed(session, event.id, landed=admin_store.LANDED_FAILED, detail="gateway 503")
    session.commit()
    assert admin_store.audit_events(session)[0].landed_detail == "gateway 503"


# The alembic revision for these tables is checked against the models by
# tests/test_migrations.py (compare_metadata), the same as every table.


# --- integrations, roles, grants (step 3) -----------------------------------


def test_connect_an_integration_then_a_grant_no_wider_than_its_base_access(session):
    row = admin_store.put_integration(
        session, vendor="Datadog", name="Datadog", base_access="read",
        spec={"address": "https://mcp.datadoghq.com"}, credential_ref="apps/kobo/integrations/datadog",
        actor="kyle@example.com",
    )
    session.commit()
    assert row.id == "datadog" and row.state == "pending"

    grant = admin_store.put_grant(session, agent_name="sage", integration_id="datadog", access="read", actor="kyle@example.com")
    session.commit()
    assert grant.state == "pending"

    with pytest.raises(admin_store.AdminStoreError, match="read-only"):
        admin_store.put_grant(session, agent_name="sage", integration_id="datadog", access="write", actor="kyle@example.com")

    actions = [e.action for e in admin_store.audit_events(session)]
    assert actions == ["grant.create", "integration.connect"]


def test_projection_reports_land_on_the_row(session):
    admin_store.put_integration(session, vendor="GitHub", name="GitHub App", base_access="write", spec={}, credential_ref=None, actor="a@example.com")
    grant = admin_store.put_grant(session, agent_name="sage", integration_id="github", access="write", actor="a@example.com")
    session.commit()

    admin_store.set_grant_state(session, grant.id, state=admin_store.STATE_APPLIED, detail="grant live at the gateway", spec={"pr": "https://example/pr/9"})
    admin_store.set_integration_state(session, "github", state=admin_store.STATE_APPLIED, detail="served")
    session.commit()

    g = admin_store.list_grants(session, agent_name="sage")[0]
    assert g.state == "applied" and g.spec == {"pr": "https://example/pr/9"}
    assert admin_store.list_integrations(session)[0].state == "applied"


def test_disconnect_takes_the_grants_with_it(session):
    admin_store.put_integration(session, vendor="Jira", name="Jira", base_access="read", spec={}, credential_ref=None, actor="a@example.com")
    admin_store.put_grant(session, agent_name="sage", integration_id="jira", access="read", actor="a@example.com")
    session.commit()

    admin_store.remove_integration(session, "jira", actor="a@example.com")
    session.commit()

    assert admin_store.list_integrations(session) == []
    assert admin_store.list_grants(session) == []
    assert admin_store.audit_events(session)[0].action == "integration.disconnect"


def test_a_role_holds_groups_and_refuses_an_unknown_body_key_or_ceiling(session):
    role = admin_store.put_role(
        session, name="Data engineering", description="Data team's agents", groups=["data", "data"],
        spec={"capabilities": ["create_agent"], "connectors": [{"plugin": "datadog", "ceiling": "read"}]},
        actor="a@example.com",
    )
    session.commit()
    d = admin_store.role_dict(role)
    assert d["id"] == "data-engineering" and d["groups"] == ["data"] and d["admin"] == []

    with pytest.raises(admin_store.AdminStoreError, match="holds only"):
        admin_store.put_role(session, name="x", description="", groups=[], spec={"powers": []}, actor="a@example.com")
    with pytest.raises(admin_store.AdminStoreError, match="ceiling"):
        admin_store.put_role(session, name="y", description="", groups=[], spec={"connectors": [{"plugin": "datadog", "ceiling": "all"}]}, actor="a@example.com")


def test_a_builtin_role_cannot_be_deleted(session):
    admin_store.put_role(session, name="Admin", description="", groups=["platform-admins"], spec={}, actor="a@example.com", builtin=True)
    session.commit()
    with pytest.raises(admin_store.AdminStoreError, match="built-in"):
        admin_store.delete_role(session, "admin", actor="a@example.com")
    admin_store.put_role(session, name="Temp", description="", groups=[], spec={}, actor="a@example.com")
    session.commit()
    admin_store.delete_role(session, "temp", actor="a@example.com")
    session.commit()
    assert [r.id for r in admin_store.list_roles(session)] == ["admin"]
