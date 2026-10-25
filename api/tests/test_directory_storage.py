"""The directory-backed StorageClient (issue #208): the same contract the GCS
client and the test fake honour, on a folder, plus the catalog paths that
ergon needs working with no object store at all."""

from __future__ import annotations

import json

import pytest

from kobo_api.clients import ClientError, DirectoryStorageClient, StorageNotFound
from kobo_api.config import Config
from kobo_api.personas import create_persona, read_index, seed_default_personas
from tests.test_personas import _fields


def test_read_write_metadata_and_list_follow_the_contract(tmp_path):
    store = DirectoryStorageClient(tmp_path)
    with pytest.raises(StorageNotFound):
        store.list_keys("personas", "")
    assert store.ensure_bucket("personas") == "created"
    assert store.ensure_bucket("personas") == "exists"
    assert store.list_keys("personas", "") == []
    with pytest.raises(StorageNotFound):
        store.read_bytes("personas", "aria/persona.md")

    store.write_bytes("personas", "aria/persona.md", b"# Aria", metadata={"team": "cs", "creator": "room"}, content_type="text/markdown")
    store.write_bytes("personas", "index.json", b"{}", content_type="application/json")
    assert store.read_bytes("personas", "aria/persona.md") == b"# Aria"
    meta = store.object_metadata("personas", "aria/persona.md")
    assert meta["metadata"] == {"team": "cs", "creator": "room"}
    assert meta["updated"].endswith("Z") and meta["timeCreated"] == meta["updated"]
    # Sidecars and temp files never show up as objects; prefixes narrow.
    assert store.list_keys("personas", "") == ["aria/persona.md", "index.json"]
    assert store.list_keys("personas", "aria/") == ["aria/persona.md"]
    # A rewrite keeps the creation instant and moves `updated`.
    first = store.object_metadata("personas", "aria/persona.md")["timeCreated"]
    store.write_bytes("personas", "aria/persona.md", b"# Aria v2", metadata={"team": "cs"})
    again = store.object_metadata("personas", "aria/persona.md")
    assert again["timeCreated"] == first and again["metadata"] == {"team": "cs"}
    assert store.ensure_versioning("personas") is False
    with pytest.raises(StorageNotFound):
        store.ensure_versioning("nope")


@pytest.mark.parametrize("key", ["", "/etc/passwd", "../x", "a/../../x", "a/./b", "a//b", "trailing/", "..", "."])
def test_keys_that_leave_the_bucket_are_refused_before_any_io(tmp_path, key):
    store = DirectoryStorageClient(tmp_path)
    store.ensure_bucket("b")
    with pytest.raises(ClientError):
        store.read_bytes("b", key)
    with pytest.raises(ClientError):
        store.write_bytes("b", key, b"x")
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize("bucket", ["", "a/b", "..", ".hidden"])
def test_bucket_names_are_plain_directory_names(tmp_path, bucket):
    with pytest.raises(ClientError):
        DirectoryStorageClient(tmp_path).ensure_bucket(bucket)


def test_the_persona_catalog_seeds_lists_and_creates_on_a_directory(tmp_path):
    """Issue #208's "done when": built-ins seed, the index lists them, create adds one."""
    store = DirectoryStorageClient(tmp_path)
    store.ensure_bucket("personas")
    counts = seed_default_personas(store, bucket="personas")
    assert counts["seeded"] > 0
    assert seed_default_personas(store, bucket="personas")["seeded"] == 0

    ids = {e["id"] for e in read_index(store, "personas", "")}
    assert len(ids) == counts["seeded"]
    entry = create_persona(store, bucket="personas", fields=_fields(), team="cs-team")
    assert entry.id in {e["id"] for e in read_index(store, "personas", "")}
    body = (tmp_path / "personas" / entry.id / "persona.md").read_bytes()
    assert body.startswith(b"#") or b"persona" in body.lower() or len(body) > 0
    sidecar = json.loads((tmp_path / "personas" / entry.id / "persona.md.meta.json").read_text())
    assert sidecar["metadata"]["team"] == "cs-team"


def test_config_picks_the_directory_backend_only_with_a_root():
    with pytest.raises(ValueError, match="storage_root"):
        Config(storage_backend="directory")
    with pytest.raises(ValueError, match="gcs"):
        Config(storage_backend="s3")
    cfg = Config(storage_backend="directory", storage_root="/tmp/kobo-store", personas_bucket="personas")
    assert cfg.storage_backend == "directory"
