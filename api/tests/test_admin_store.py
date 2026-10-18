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
