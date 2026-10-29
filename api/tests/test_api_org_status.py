"""Route tests for GET /api/org/status (Phase A Task 8): the room's
read-only wiring visibility — six checks over org config, both catalog
buckets, both catalog indexes, and the deploy platform.

Same full TestClient/create_app harness as test_api_search.py/
test_api_agents.py. `fake_storage`/`config` are conftest.py's shared
fixtures (seeded with both catalogs: the one packaged persona plus one
sample skill and both `index.json`s) — this module only adds its own
`clients`/`app` wiring (a bare Clients with fakes for every client the
route never touches) and an `org` knob per test.

Every test asserts the *whole* six-check array in order (Task 8 controller
decision #1: org-config, skills-bucket, personas-bucket, skills-index,
personas-seeded, deploy-platform) — this both pins the exact ids/labels/
details the brief specifies verbatim and proves a degraded case flips
exactly its own check, nothing else.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.main import create_app
from kobo_api.models import Base
from kobo_api.org import OrgState
from kobo_api.personas import _PACKAGE_PERSONAS_DIR
from tests.fakes import (
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeStorage,
)

# The seeded count is however many built-ins ship in the package.
_PACKAGED_PERSONA_COUNT = len(list(_PACKAGE_PERSONAS_DIR.glob("*.md")))

ORG_REPO = "acme-corp/kobo-config"
GITHUB_ORG = "acme-corp"


def _clients(*, gojira=None, storage=None) -> Clients:
    """A Clients with every non-storage/gojira field a bare fake — this
    route never touches github/cloud/agent/kubectl, but the Clients
    dataclass requires them (no defaults)."""
    return Clients(
        github=FakeGitHub(),
        gojira=gojira if gojira is not None else FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
        storage=storage,
    )


def _client(config: Config, clients: Clients, tmp_path, *, org: OrgState | None = None) -> TestClient:
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return TestClient(create_app(session_factory, config=config, clients=clients, org=org))


@pytest.fixture
def wired_org():
    return OrgState(True, ORG_REPO)


@pytest.fixture
def wired_config(config: Config) -> Config:
    """conftest.py's `config` fixture, plus a `github_org` — the route
    reports this only when `org.wired` is True (controller decision #2)."""
    return config.model_copy(update={"github_org": GITHUB_ORG})


def test_all_green_against_seeded_fakes(wired_config, fake_storage, tmp_path, wired_org):
    client = _client(
        wired_config, _clients(storage=fake_storage), tmp_path, org=wired_org
    )

    response = client.get("/api/org/status")

    assert response.status_code == 200
    body = response.json()
    assert body["org"] == GITHUB_ORG
    assert body["wired"] is True
    assert body["checks"] == [
        {
            "id": "org-config",
            "label": "Organization settings",
            "ok": True,
            "detail": ORG_REPO,
        },
        {
            "id": "skills-bucket",
            "label": "Skills catalog storage",
            "ok": True,
            "detail": "connected",
        },
        {
            "id": "personas-bucket",
            "label": "Personas storage",
            "ok": True,
            "detail": "connected",
        },
        {
            "id": "skills-index",
            "label": "Skills index",
            "ok": True,
            "detail": "updated 2026-08-03T00:00:00Z",
        },
        {
            "id": "personas-seeded",
            "label": "Personas",
            "ok": True,
            "detail": f"{_PACKAGED_PERSONA_COUNT} personas",
        },
        {
            "id": "deploy-platform",
            "label": "Deployment",
            "ok": True,
            "detail": f"connected to {wired_config.gojira_cluster}",
        },
    ]


def _check(body: dict, check_id: str) -> dict:
    return next(c for c in body["checks"] if c["id"] == check_id)


def _other_checks_ok(body: dict, *except_ids: str) -> bool:
    return all(c["ok"] for c in body["checks"] if c["id"] not in except_ids)


# --- org-config -----------------------------------------------------------


def test_unwired_org_flips_only_org_config_check(wired_config, fake_storage, tmp_path):
    unwired = OrgState(False, None, "no org configured")
    client = _client(wired_config, _clients(storage=fake_storage), tmp_path, org=unwired)

    response = client.get("/api/org/status")

    assert response.status_code == 200
    body = response.json()
    assert body["org"] is None
    assert body["wired"] is False
    assert _check(body, "org-config") == {
        "id": "org-config",
        "label": "Organization settings",
        "ok": False,
        "detail": "no org configured",
    }
    assert _other_checks_ok(body, "org-config")


def test_unwired_org_with_clone_error_detail_is_the_error(wired_config, fake_storage, tmp_path):
    unwired = OrgState(False, ORG_REPO, f"couldn't clone {ORG_REPO}: boom")
    client = _client(wired_config, _clients(storage=fake_storage), tmp_path, org=unwired)

    body = client.get("/api/org/status").json()

    assert _check(body, "org-config")["ok"] is False
    assert _check(body, "org-config")["detail"] == f"couldn't clone {ORG_REPO}: boom"


# --- skills-bucket / personas-bucket ---------------------------------------


def test_empty_skills_bucket_name_flips_only_that_check(fake_storage, tmp_path, wired_org):
    config = Config(workdir=tmp_path / "work", skills_bucket="", personas_bucket="personas-bucket")
    client = _client(config, _clients(storage=fake_storage), tmp_path, org=wired_org)

    body = client.get("/api/org/status").json()

    assert _check(body, "skills-bucket") == {
        "id": "skills-bucket",
        "label": "Skills catalog storage",
        "ok": False,
        "detail": "not configured",
    }
    assert _other_checks_ok(body, "skills-bucket", "skills-index")
    # skills-index also depends on the skills bucket being configured.
    assert _check(body, "skills-index")["ok"] is False


def test_empty_personas_bucket_name_flips_only_that_check(fake_storage, tmp_path, wired_org):
    config = Config(workdir=tmp_path / "work", skills_bucket="skills-bucket", personas_bucket="")
    client = _client(config, _clients(storage=fake_storage), tmp_path, org=wired_org)

    body = client.get("/api/org/status").json()

    assert _check(body, "personas-bucket") == {
        "id": "personas-bucket",
        "label": "Personas storage",
        "ok": False,
        "detail": "not configured",
    }
    assert _check(body, "personas-seeded")["ok"] is False
    assert _other_checks_ok(body, "personas-bucket", "personas-seeded")


def test_bucket_list_keys_client_error_flips_only_that_check(wired_config, tmp_path, wired_org):
    storage = FakeStorage(
        buckets={
            wired_config.skills_bucket: {"index.json": (b'{"entries": []}', {"updated": "x"})},
            wired_config.personas_bucket: {"index.json": (b'{"entries": [{"id": "a"}]}', {})},
        },
        list_error="storage backend unavailable",
    )
    client = _client(wired_config, _clients(storage=storage), tmp_path, org=wired_org)

    body = client.get("/api/org/status").json()

    assert _check(body, "skills-bucket")["ok"] is False
    assert _check(body, "personas-bucket")["ok"] is False
    # the two bucket checks are the only ones that call list_keys.
    assert _check(body, "org-config")["ok"] is True
    assert _check(body, "deploy-platform")["ok"] is True


def test_storage_none_flips_bucket_and_index_checks(wired_config, tmp_path, wired_org):
    client = _client(wired_config, _clients(storage=None), tmp_path, org=wired_org)

    body = client.get("/api/org/status").json()

    assert _check(body, "skills-bucket")["ok"] is False
    assert _check(body, "personas-bucket")["ok"] is False
    assert _check(body, "skills-index")["ok"] is False
    assert _check(body, "personas-seeded")["ok"] is False
    assert _check(body, "org-config")["ok"] is True
    assert _check(body, "deploy-platform")["ok"] is True


# --- skills-index -----------------------------------------------------------


def test_missing_skills_index_flips_only_that_check(wired_config, tmp_path, wired_org):
    storage = FakeStorage(
        buckets={
            wired_config.skills_bucket: {},
            wired_config.personas_bucket: {"index.json": (b'{"entries": [{"id": "a"}]}', {})},
        }
    )
    client = _client(wired_config, _clients(storage=storage), tmp_path, org=wired_org)

    body = client.get("/api/org/status").json()

    assert _check(body, "skills-index") == {
        "id": "skills-index",
        "label": "Skills index",
        "ok": False,
        "detail": "no skills published yet",
    }
    # skills-bucket itself is still reachable (list_keys succeeds against an
    # empty-but-present bucket) — only the index object is missing.
    assert _check(body, "skills-bucket")["ok"] is True
    assert _other_checks_ok(body, "skills-index")


def test_skills_index_healthy_via_catalog_json_when_index_json_absent(
    wired_config, tmp_path, wired_org
):
    # Fix round 2 (whole-arc review): the marketplace bucket's real catalog
    # path is `catalog/catalog.json`, not `index.json` — a bucket carrying
    # ONLY the former (the real post-flip marketplace layout) must still
    # report the skills-index check healthy, not "no skills published yet".
    storage = FakeStorage(
        buckets={
            wired_config.skills_bucket: {
                "catalog/catalog.json": (b'{"skills": {"a": {"name": "a", "description": "d"}}, "count": 1}', {}),
            },
            wired_config.personas_bucket: {"index.json": (b'{"entries": [{"id": "a"}]}', {})},
        }
    )
    client = _client(wired_config, _clients(storage=storage), tmp_path, org=wired_org)

    body = client.get("/api/org/status").json()

    check = _check(body, "skills-index")
    assert check["ok"] is True
    # Path-agnostic: the healthy detail never names which filename resolved.
    assert "index.json" not in check["detail"]
    assert "catalog.json" not in check["detail"]
    assert _other_checks_ok(body, "skills-index")


# --- personas-seeded ---------------------------------------------------------


def test_missing_personas_index_flips_only_that_check(wired_config, tmp_path, wired_org):
    storage = FakeStorage(
        buckets={
            wired_config.skills_bucket: {"index.json": (b'{"entries": [{"id": "a"}]}', {"updated": "x"})},
            wired_config.personas_bucket: {},
        }
    )
    client = _client(wired_config, _clients(storage=storage), tmp_path, org=wired_org)

    body = client.get("/api/org/status").json()

    assert _check(body, "personas-seeded")["ok"] is False
    assert _check(body, "personas-bucket")["ok"] is True
    assert _other_checks_ok(body, "personas-seeded")


def test_empty_personas_entries_flips_only_that_check(wired_config, tmp_path, wired_org):
    storage = FakeStorage(
        buckets={
            wired_config.skills_bucket: {"index.json": (b'{"entries": [{"id": "a"}]}', {"updated": "x"})},
            wired_config.personas_bucket: {"index.json": (b'{"entries": []}', {})},
        }
    )
    client = _client(wired_config, _clients(storage=storage), tmp_path, org=wired_org)

    body = client.get("/api/org/status").json()

    assert _check(body, "personas-seeded") == {
        "id": "personas-seeded",
        "label": "Personas",
        "ok": False,
        "detail": "0 personas",
    }
    assert _other_checks_ok(body, "personas-seeded")


def test_malformed_personas_index_json_never_500s(wired_config, tmp_path, wired_org):
    storage = FakeStorage(
        buckets={
            wired_config.skills_bucket: {"index.json": (b'{"entries": [{"id": "a"}]}', {"updated": "x"})},
            wired_config.personas_bucket: {"index.json": (b"not json", {})},
        }
    )
    client = _client(wired_config, _clients(storage=storage), tmp_path, org=wired_org)

    response = client.get("/api/org/status")

    assert response.status_code == 200
    body = response.json()
    check = _check(body, "personas-seeded")
    assert check["ok"] is False
    assert isinstance(check["detail"], str) and check["detail"]
    assert _other_checks_ok(body, "personas-seeded")


# --- deploy-platform ---------------------------------------------------------


def test_ping_false_flips_only_deploy_platform_check(wired_config, fake_storage, tmp_path, wired_org):
    client = _client(
        wired_config,
        _clients(gojira=FakeGojira(ping_result=False), storage=fake_storage),
        tmp_path,
        org=wired_org,
    )

    body = client.get("/api/org/status").json()

    assert _check(body, "deploy-platform") == {
        "id": "deploy-platform",
        "label": "Deployment",
        "ok": False,
        "detail": "can't reach the deploy platform",
    }
    assert _other_checks_ok(body, "deploy-platform")


def test_empty_cluster_flips_only_deploy_platform_check(fake_storage, tmp_path, wired_org):
    config = Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        gojira_cluster="",
    )
    client = _client(config, _clients(storage=fake_storage), tmp_path, org=wired_org)

    body = client.get("/api/org/status").json()

    assert _check(body, "deploy-platform") == {
        "id": "deploy-platform",
        "label": "Deployment",
        "ok": False,
        "detail": "no cluster configured",
    }
    assert _other_checks_ok(body, "deploy-platform")


def test_route_always_200_even_fully_unwired(tmp_path):
    """A completely bare app (no clients at all, default unwired org) must
    still answer 200 with every check reporting honestly unwired/unreachable
    — never a 500."""
    config = Config(workdir=tmp_path / "work")  # skills_bucket/personas_bucket default ""
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    client = TestClient(create_app(session_factory, config=config))

    response = client.get("/api/org/status")

    assert response.status_code == 200
    body = response.json()
    assert body["org"] is None
    assert body["wired"] is False
    assert all(c["ok"] is False for c in body["checks"])


def test_check_raising_arbitrary_exception_is_isolated(wired_config, fake_storage, tmp_path, wired_org):
    """When any check function raises an arbitrary exception (not a
    ClientError the check expected to handle), the blanket isolation wraps it
    — response is still 200, exactly that check ok=False with the backstop
    detail, and all other checks are unaffected."""
    storage = FakeStorage(
        buckets={
            wired_config.skills_bucket: {"index.json": (b'{"entries": [{"id": "a"}]}', {"updated": "x"})},
            wired_config.personas_bucket: {"index.json": (b'{"entries": [{"id": "a"}]}', {})},
        }
    )
    # Create the app and client normally
    client_obj = _client(
        wired_config, _clients(storage=storage), tmp_path, org=wired_org
    )

    # Monkeypatch FakeStorage.object_metadata to raise RuntimeError instead
    # of returning normally — simulates a check-level catastrophe.
    def raising_object_metadata(bucket: str, key: str):
        raise RuntimeError("storage layer exploded")
    storage.object_metadata = raising_object_metadata

    response = client_obj.get("/api/org/status")

    assert response.status_code == 200
    body = response.json()

    # skills-index check calls object_metadata and should now be backstopped
    skills_index_check = _check(body, "skills-index")
    assert skills_index_check["ok"] is False
    assert skills_index_check["detail"] == "something went wrong running this check"

    # All other checks should still be ok, unaffected by the exception
    assert _check(body, "org-config")["ok"] is True
    assert _check(body, "skills-bucket")["ok"] is True
    assert _check(body, "personas-bucket")["ok"] is True
    assert _check(body, "personas-seeded")["ok"] is True
    assert _check(body, "deploy-platform")["ok"] is True


# --- whoami (access-control Task A1): the room shows who is signed in -------
#
# Same real-JWT idiom test_web_teach.py/test_web_destroy.py use for their own
# IAP-attribution slices: a locally generated ES256 keypair signs the
# assertion, verified through the actual `IapMiddleware`/`IapVerifier`, not a
# mock. The "IAP off" case needs no middleware at all -- the plain `client`
# fixture (conftest.py's default, no `iap_audience`) already is that state.

WHOAMI_AUDIENCE = "test-audience"
WHOAMI_KID = "test-key-1"


def _generate_jwk(kid: str = WHOAMI_KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign_whoami(private_key, *, email: str | None, kid: str = WHOAMI_KID) -> str:
    now = int(time.time())
    claims = {
        "aud": WHOAMI_AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
    }
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


def _fetcher(jwk):
    def fetch():
        return {"keys": [jwk]}

    return fetch


def _iap_client(config: Config, tmp_path, *, jwk) -> TestClient:
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return TestClient(
        create_app(
            session_factory,
            config=config,
            iap_jwks_fetcher=_fetcher(jwk),
        )
    )


def test_whoami_without_iap_is_null_email_and_not_operator(client):
    response = client.get("/api/whoami")

    assert response.status_code == 200
    assert response.json() == {
        "email": None,
        "operator": False,
        "groups": [],
        "groups_unavailable": False,
        "admin_panel": False,
        "logout_url": None,
        "access_on": False,
    }


def test_whoami_with_iap_operator_email_reports_email_and_operator_true(tmp_path):
    private_key, jwk = _generate_jwk()
    config = Config(
        workdir=tmp_path / "work",
        iap_audience=WHOAMI_AUDIENCE,
        operators=["ops@example.com"],
    )
    client = _iap_client(config, tmp_path, jwk=jwk)

    token = _sign_whoami(private_key, email="ops@example.com")
    response = client.get("/api/whoami", headers={ASSERTION_HEADER: token})

    assert response.status_code == 200
    assert response.json() == {
        "email": "ops@example.com",
        "operator": True,
        "groups": [],
        "groups_unavailable": False,
        "admin_panel": False,
        "logout_url": None,
        "access_on": False,
        # shared-group + group-requests wave: an admin's whoami also carries
        # the pending-queue badge count (kobo_api.main.whoami) -- zero here
        # since this test seeds no GroupRequest rows.
        "pending_group_requests": 0,
    }


def test_whoami_with_iap_non_operator_email_reports_email_and_operator_false(tmp_path):
    private_key, jwk = _generate_jwk()
    config = Config(
        workdir=tmp_path / "work",
        iap_audience=WHOAMI_AUDIENCE,
        operators=["ops@example.com"],
    )
    client = _iap_client(config, tmp_path, jwk=jwk)

    token = _sign_whoami(private_key, email="someone-else@example.com")
    response = client.get("/api/whoami", headers={ASSERTION_HEADER: token})

    assert response.status_code == 200
    assert response.json() == {
        "email": "someone-else@example.com",
        "operator": False,
        "groups": [],
        "groups_unavailable": False,
        "admin_panel": False,
        "logout_url": None,
        "access_on": False,
    }


def test_whoami_operator_match_is_case_insensitive(tmp_path):
    private_key, jwk = _generate_jwk()
    config = Config(
        workdir=tmp_path / "work",
        iap_audience=WHOAMI_AUDIENCE,
        operators=["OPS@EXAMPLE.COM"],
    )
    client = _iap_client(config, tmp_path, jwk=jwk)

    token = _sign_whoami(private_key, email="ops@example.com")
    response = client.get("/api/whoami", headers={ASSERTION_HEADER: token})

    assert response.json()["operator"] is True
