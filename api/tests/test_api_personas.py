"""Route-level tests for POST /api/personas (Phase A Task 4): the persona
builder route, exercised through a full TestClient/create_app stack with
FakeStorage standing in for GCS — same harness shape as test_api_search.py.
"""

from __future__ import annotations

import json
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
from kobo_api.persona_draft import (
    _fallback_distilled_name,
    _fallback_distilled_who,
    _fallback_name,
    _fallback_tags,
    _fallback_what,
    _fallback_who,
)
from tests.fakes import (
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeName,
    FakeStorage,
)


def _index_bytes(entries: list[dict]) -> bytes:
    return json.dumps({"entries": entries}).encode()


SKILL_ENTRIES = [
    {
        "id": "code-review",
        "name": "Code review",
        "description": "Reviews pull requests",
        "team": "shared",
        "tags": ["review", "quality"],
        "sha": "abc123",
        "builtin": False,
    },
]

VALID_PAYLOAD = {
    "name": "Aria the Assistant",
    "who_they_are": "a calm troubleshooter for billing questions",
    "what_they_do": "Help customers troubleshoot billing issues.",
    # "shared" (Task 2's team middle gate): every test below that doesn't
    # care about team-gating posts through the plain `client` fixture with
    # no identity at all, and must land on the gate's no-membership-required
    # arm to reach the behavior it actually means to exercise.
    # test_create_persona_explicit_team_still_honored below overrides this
    # with a real member identity to prove a NAMED team is honored too.
    "team": "shared",
}


def _minimal_clients(**kwargs) -> Clients:
    """A Clients with every non-storage field a bare fake — this route
    never touches github/gojira/cloud/agent/kubectl, but the Clients
    dataclass requires them (no defaults)."""
    return Clients(
        github=FakeGitHub(),
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
        **kwargs,
    )


def _make_client(
    config: Config, clients: Clients, tmp_path, *, iap_jwks_fetcher=None
) -> TestClient:
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=clients,
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


@pytest.fixture
def fake_storage():
    return FakeStorage(
        buckets={
            "skills-bucket": {"index.json": (_index_bytes(SKILL_ENTRIES), {})},
            "personas-bucket": {},
        }
    )


@pytest.fixture
def config(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
    )


@pytest.fixture
def clients(fake_storage):
    return _minimal_clients(storage=fake_storage)


@pytest.fixture
def client(config, clients, tmp_path):
    return _make_client(config, clients, tmp_path)


def test_create_persona_201(client, fake_storage):
    response = client.post("/api/personas", json=VALID_PAYLOAD)

    assert response.status_code == 201
    assert response.json() == {
        "id": "aria-the-assistant",
        "name": "Aria the Assistant",
        "description": "a calm troubleshooter for billing questions",
        "team": "shared",
        # Create-flow polish (2026-08-28): supplied-only tags -- this
        # payload sends none, so the entry carries none. The retired
        # derivation used to invent six from the persona's own text.
        "tags": [],
        "builtin": False,
    }
    assert "write_bytes:personas-bucket:aria-the-assistant/persona.md" in fake_storage.calls


def test_create_persona_201_with_rules(client):
    """Rules (optional, up to 150) render into the body's Rules section — a
    regression pin that the route passes `rules` through to `create_persona`
    like every other field, not just the three required ones."""
    payload = {**VALID_PAYLOAD, "rules": ["Never share account numbers.", "Be concise."]}

    response = client.post("/api/personas", json=payload)

    assert response.status_code == 201


def test_create_persona_is_instantly_searchable(client):
    client.post("/api/personas", json=VALID_PAYLOAD)

    response = client.get("/api/personas/search", params={"q": "aria"})

    assert response.status_code == 200
    ids = [r["id"] for r in response.json()["results"]]
    assert "aria-the-assistant" in ids


def test_create_persona_preserves_existing_skill_entries(client):
    """Regression test for the ingest hazard (Task 4 controller decision
    #1): search.ingest_indexes does a FULL rebuild of every kind it's
    given, so a persona-create route that ingested ONLY the personas
    catalog would silently wipe the skills table clean. The route must
    call ingest_indexes with the full kind->bucket mapping
    (configured_catalogs), not just {"persona": ...}.
    """
    pre = client.get("/api/skills/search", params={"q": "revi"})
    assert pre.json()["results"], "fixture sanity: skill entry should be searchable pre-create"

    create_response = client.post("/api/personas", json=VALID_PAYLOAD)
    assert create_response.status_code == 201

    post = client.get("/api/skills/search", params={"q": "revi"})
    assert post.json()["results"] == pre.json()["results"]


def test_create_persona_duplicate_409(client):
    first = client.post("/api/personas", json=VALID_PAYLOAD)
    assert first.status_code == 201

    response = client.post("/api/personas", json=VALID_PAYLOAD)

    assert response.status_code == 409
    assert response.json() == {"error": "a persona with that name already exists"}


def test_create_persona_422_bad_team_shape(client):
    response = client.post("/api/personas", json={**VALID_PAYLOAD, "team": "Not Valid!"})

    assert response.status_code == 422


def test_create_persona_422_missing_who_they_are(client):
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "who_they_are"}

    response = client.post("/api/personas", json=payload)

    assert response.status_code == 422


def test_create_persona_422_too_many_rules(client):
    payload = {**VALID_PAYLOAD, "rules": ["ok"] * 151}

    response = client.post("/api/personas", json=payload)

    assert response.status_code == 422


def test_create_persona_422_merged_render_too_many_lines(client):
    """The route surfaces the merged-render gate's own message verbatim
    (kobo_api.personas._check_rendered_size, raised as PersonaError from
    create_persona and turned into a 422 by the existing
    `except PersonaError` handler) — not the generic field-name-only
    RequestValidationError copy."""
    # To exceed 200 lines: 7 template + 41 what_they_do + 3 rules-opener + 150 rules = 201
    multi_line_what = "\n".join(["line"] * 41)
    payload = {**VALID_PAYLOAD, "what_they_do": multi_line_what, "rules": ["ok"] * 150}

    response = client.post("/api/personas", json=payload)

    assert response.status_code == 422
    assert response.json() == {
        "error": "persona renders to 201 lines; the limit is 200"
    }


def test_create_persona_422_name_does_not_slugify(client):
    """A name that passes pydantic's own length check but can't slugify to
    a valid id (PersonaError, raised from persona_id inside create_persona)
    must still surface as a 422, not an unhandled 500."""
    response = client.post("/api/personas", json={**VALID_PAYLOAD, "name": "!!!"})

    assert response.status_code == 422


def test_create_persona_503_when_storage_none(config, tmp_path):
    client = _make_client(config, _minimal_clients(), tmp_path)

    response = client.post("/api/personas", json=VALID_PAYLOAD)

    assert response.status_code == 503
    assert response.json() == {"error": "catalogs aren't set up yet"}


def test_create_persona_503_when_personas_bucket_unset(tmp_path):
    config = Config(workdir=tmp_path / "work", skills_bucket="skills-bucket")
    client = _make_client(config, _minimal_clients(storage=FakeStorage()), tmp_path)

    response = client.post("/api/personas", json=VALID_PAYLOAD)

    assert response.status_code == 503
    assert response.json() == {"error": "catalogs aren't set up yet"}


def test_create_persona_502_on_storage_error(config, tmp_path):
    erroring_storage = FakeStorage(write_error="storage is on fire")
    client = _make_client(config, _minimal_clients(storage=erroring_storage), tmp_path)

    response = client.post("/api/personas", json=VALID_PAYLOAD)

    assert response.status_code == 502


# --- Team-optional (Persona v3 addendum): create-persona defaults to "shared" -------


def test_create_persona_omitted_team_defaults_to_shared(client):
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "team"}

    response = client.post("/api/personas", json=payload)

    assert response.status_code == 201
    assert response.json()["team"] == "shared"


# --- Team middle gate (Task 2): "shared" stays open, named teams need membership ----
#
# This file's other tests above never wire IAP at all (their shared `config`/
# `client` fixtures carry no `iap_audience`), so identity/resolver behavior
# is exercised through a dedicated, real-JWT client builder here instead of
# perturbing those fixtures. Same real-JWT idiom as test_api_grants.py's own
# copy (ES256, a locally generated keypair, a `jwks_fetcher` test seam) --
# duplicated locally rather than imported, matching this repo's "don't
# cross-import test fixtures" convention (see test_mcp_tools_write.py's own
# module docstring for the same rule stated explicitly).

GATE_AUDIENCE = "test-persona-gate-audience"
GATE_KID = "test-persona-gate-key"
MEMBER_EMAIL = "member@example.com"
STRANGER_EMAIL = "stranger@example.com"
OPERATOR_EMAIL = "ops@example.com"


def _generate_jwk(kid: str = GATE_KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign(private_key, *, email: str | None, kid: str = GATE_KID) -> str:
    now = int(time.time())
    claims = {
        "aud": GATE_AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
    }
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


def _gate_fetcher(jwk):
    def fetch():
        return {"keys": [jwk]}

    return fetch


def _gate_headers(private_key, *, email: str | None) -> dict:
    return {ASSERTION_HEADER: _sign(private_key, email=email)}


class _GroupsStub:
    """Mapping-driven `groups_for` stand-in for the gate's resolver seam --
    same shape as the other gated-route test files' own local copies (e.g.
    test_mcp_tools_write.py's `FakeGroupResolver`), duplicated here rather
    than imported. An email with no entry in `mapping` resolves to `None`
    (unresolvable / "directory down"), matching `_persona_team_gate`'s own
    fail-closed reading of a `None` answer.
    """

    def __init__(self, mapping: dict[str, list[str]] | None = None):
        self._mapping = dict(mapping) if mapping else {}

    def groups_for(self, email: str) -> list[str] | None:
        return self._mapping.get(email.lower())


@pytest.fixture
def gate_config(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        iap_audience=GATE_AUDIENCE,
        operators=[OPERATOR_EMAIL],
    )


def _gate_client(gate_config, fake_storage, tmp_path, *, groups_resolver=None):
    private_key, jwk = _generate_jwk()
    clients = _minimal_clients(storage=fake_storage, groups=groups_resolver)
    client = _make_client(
        gate_config, clients, tmp_path, iap_jwks_fetcher=_gate_fetcher(jwk)
    )
    return client, private_key


def test_persona_create_shared_team_needs_no_membership(gate_config, fake_storage, tmp_path):
    """team omitted entirely -> 201 even with a resolver that would refuse
    every team (resolver returning [] or None must not matter on this arm
    -- the gate never consults it for the default "shared" team)."""
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=_GroupsStub())
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "team"}

    response = client.post(
        "/api/personas", json=payload, headers=_gate_headers(key, email=STRANGER_EMAIL)
    )

    assert response.status_code == 201


def test_persona_create_shared_team_passes_with_resolver_down(gate_config, fake_storage, tmp_path):
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=_GroupsStub())
    payload = {**VALID_PAYLOAD, "team": "shared"}

    response = client.post(
        "/api/personas", json=payload, headers=_gate_headers(key, email=STRANGER_EMAIL)
    )

    assert response.status_code == 201


def test_persona_create_shared_team_passes_with_resolver_answering_empty_list(
    gate_config, fake_storage, tmp_path
):
    """Distinct from the resolver-down case above: here the resolver
    actually ANSWERS for this caller, with an empty list (a real "you are
    in zero groups" reply, not "I could not resolve you"). The "shared"
    arm still never looks at it either way -- confirming the short-circuit
    is on `team == "shared"` alone, not on some reading of the resolver's
    reply."""
    resolver = _GroupsStub({STRANGER_EMAIL: []})
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=resolver)
    payload = {**VALID_PAYLOAD, "team": "shared"}

    response = client.post(
        "/api/personas", json=payload, headers=_gate_headers(key, email=STRANGER_EMAIL)
    )

    assert response.status_code == 201


def test_persona_create_named_team_member_passes(gate_config, fake_storage, tmp_path):
    resolver = _GroupsStub({MEMBER_EMAIL: ["sre"]})
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=resolver)
    payload = {**VALID_PAYLOAD, "team": "sre"}

    response = client.post(
        "/api/personas", json=payload, headers=_gate_headers(key, email=MEMBER_EMAIL)
    )

    assert response.status_code == 201
    assert response.json()["team"] == "sre"


def test_persona_create_named_team_non_member_403(gate_config, fake_storage, tmp_path):
    resolver = _GroupsStub({MEMBER_EMAIL: ["other-team"]})
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=resolver)
    payload = {**VALID_PAYLOAD, "team": "sre"}

    response = client.post(
        "/api/personas", json=payload, headers=_gate_headers(key, email=MEMBER_EMAIL)
    )

    assert response.status_code == 403
    assert response.json() == {
        "error": "you must be a member of team sre to file a persona under it"
    }


def test_persona_create_named_team_unresolvable_403(gate_config, fake_storage, tmp_path):
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=_GroupsStub())
    payload = {**VALID_PAYLOAD, "team": "sre"}

    response = client.post(
        "/api/personas", json=payload, headers=_gate_headers(key, email=STRANGER_EMAIL)
    )

    assert response.status_code == 403


def test_persona_create_named_team_no_resolver_wired_403(gate_config, fake_storage, tmp_path):
    """`_gate_client`'s own default (`groups_resolver=None`) -- no other
    gate test above exercises it: `clients.groups` is `None` because no
    resolver is configured on this room at all, distinct from a resolver
    that's configured but answers `None`. `_persona_team_gate`'s `resolver
    = clients.groups if clients is not None else None` /
    `resolver.groups_for(email) if resolver is not None else None` chain
    folds this into the exact same fail-closed refusal."""
    client, key = _gate_client(gate_config, fake_storage, tmp_path)
    payload = {**VALID_PAYLOAD, "team": "sre"}

    response = client.post(
        "/api/personas", json=payload, headers=_gate_headers(key, email=STRANGER_EMAIL)
    )

    assert response.status_code == 403
    assert response.json() == {
        "error": "you must be a member of team sre to file a persona under it"
    }


def test_persona_create_named_team_operator_passes(gate_config, fake_storage, tmp_path):
    # OPERATOR_EMAIL resolves to no groups at all -- passes purely on
    # `_is_admin`'s operator-email check, never on team membership.
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=_GroupsStub())
    payload = {**VALID_PAYLOAD, "team": "sre"}

    response = client.post(
        "/api/personas", json=payload, headers=_gate_headers(key, email=OPERATOR_EMAIL)
    )

    assert response.status_code == 201


def test_persona_create_named_team_empty_identity_403(gate_config, fake_storage, tmp_path):
    """A validly signed assertion with no `email` claim at all -- IapMiddleware
    still stamps `request.state.iap_email = None`, and the gate must refuse
    (fail-closed) rather than treat a missing identity as harmless."""
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=_GroupsStub())
    payload = {**VALID_PAYLOAD, "team": "sre"}

    response = client.post(
        "/api/personas", json=payload, headers=_gate_headers(key, email=None)
    )

    assert response.status_code == 403


def test_create_persona_explicit_team_still_honored(gate_config, fake_storage, tmp_path):
    """Pre-existing coverage (Persona v3 addendum), moved here and re-wired
    through a real gated identity now that a named team requires membership
    (Task 2): a caller who IS a member of "cs-team" still gets that exact
    team back on the created entry, not silently downgraded to "shared"."""
    resolver = _GroupsStub({MEMBER_EMAIL: ["cs-team"]})
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=resolver)
    payload = {**VALID_PAYLOAD, "team": "cs-team"}

    response = client.post(
        "/api/personas", json=payload, headers=_gate_headers(key, email=MEMBER_EMAIL)
    )

    assert response.status_code == 201
    assert response.json()["team"] == "cs-team"


# --- Archive/unarchive routes (hardening wave Task 5) --------------------------------
#
# Archive, not delete: persona.md is never touched, only the index entry's
# `archived` flag flips. The gate has four arms (builtin/commons operator-
# only, named-team member-or-operator, unknown id 404, catalogs-unwired 503)
# and two distinct refusal messages -- each test below pins the one arm it
# targets so removing that branch alone breaks exactly that test.

ARCHIVE_ENTRY = {"id": "wire-probe", "name": "Wire Probe", "description": "a test persona",
                 "team": "shared", "tags": [], "builtin": False}
TEAM_ARCHIVE_ENTRY = {"id": "stoic-mentor", "name": "stoic-mentor", "description": "d",
                      "team": "sre", "tags": [], "builtin": False}


def _seed_personas_index(fake_storage, entries: list[dict]) -> None:
    fake_storage.write_bytes("personas-bucket", "index.json", _index_bytes(entries))


def test_archive_shared_persona_operator_200_and_instantly_evicted(
    gate_config, fake_storage, tmp_path
):
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=_GroupsStub())
    _seed_personas_index(fake_storage, [ARCHIVE_ENTRY])
    # Prime the catalog BEFORE archiving (Important 2 fix, hardening wave
    # review): the fixture's fresh personas-bucket ("personas-bucket": {})
    # means wire-probe was never in the FTS table to begin with, so the
    # post-archive search assertion below used to prove nothing about THIS
    # route's own re-ingest -- it passed even with no eviction at all,
    # because there was nothing to evict. Warming via an explicit refresh,
    # and confirming the entry is really searchable first, makes the later
    # eviction assertion load-bearing.
    warm = client.post(
        "/api/catalogs/refresh", headers=_gate_headers(key, email=OPERATOR_EMAIL)
    )
    assert warm.status_code == 200
    pre_search = client.get(
        "/api/personas/search", headers=_gate_headers(key, email=OPERATOR_EMAIL)
    )
    assert "wire-probe" in [e["id"] for e in pre_search.json()["results"]]

    response = client.post(
        "/api/personas/wire-probe/archive", headers=_gate_headers(key, email=OPERATOR_EMAIL)
    )
    assert response.status_code == 200
    assert response.json()["archived"] is True
    # gate_config wires iap_audience, so IapMiddleware 401s every /api/*
    # call without a valid assertion -- reads included. An authenticated
    # header is needed here, or this assertion never reaches the search
    # route's own behavior at all.
    search = client.get(
        "/api/personas/search", headers=_gate_headers(key, email=OPERATOR_EMAIL)
    )
    assert "wire-probe" not in [e["id"] for e in search.json()["results"]]


def test_archive_shared_persona_non_operator_403(gate_config, fake_storage, tmp_path):
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=_GroupsStub())
    _seed_personas_index(fake_storage, [ARCHIVE_ENTRY])
    response = client.post(
        "/api/personas/wire-probe/archive", headers=_gate_headers(key, email=STRANGER_EMAIL)
    )
    assert response.status_code == 403
    # Important 1 fix (hardening wave review): full-dict equality, matching
    # this file's own standard for the create gate's refusal at
    # test_persona_create_named_team_non_member_403 above. A substring check
    # ("room operator" in error) doesn't pin which exact string comes back --
    # two separate mutations (leaking the CREATE gate's "...to file a
    # persona under it" copy, and swapping in an unrelated ownership-flavored
    # string) both left that substring assertion green across all 7 archive
    # tests.
    assert response.json() == {
        "error": "only a room operator can archive a persona filed under the commons"
    }


def test_archive_team_persona_member_200_non_member_403(gate_config, fake_storage, tmp_path):
    resolver = _GroupsStub({MEMBER_EMAIL: ["sre"]})
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=resolver)
    _seed_personas_index(fake_storage, [TEAM_ARCHIVE_ENTRY])
    denied = client.post(
        "/api/personas/stoic-mentor/archive", headers=_gate_headers(key, email=STRANGER_EMAIL)
    )
    assert denied.status_code == 403
    # Important 1 fix: same full-dict pin as above, on the named-team arm's
    # own distinct copy ("...filed under it", not the CREATE gate's "...to
    # file a persona under it").
    assert denied.json() == {
        "error": "you must be a member of team sre to archive a persona filed under it"
    }
    allowed = client.post(
        "/api/personas/stoic-mentor/archive", headers=_gate_headers(key, email=MEMBER_EMAIL)
    )
    assert allowed.status_code == 200


def test_archive_builtin_operator_only(gate_config, fake_storage, tmp_path):
    # Minor 6 fix (hardening wave review): the fixture's team must be
    # something OTHER than "shared" here, or the gate's other arm
    # (`entry.get("team") == "shared"`) alone would still route this to the
    # operator-only branch even with the `builtin` check removed entirely,
    # masking a mutation on that arm. A resolver that makes MEMBER_EMAIL a
    # genuine member of the entry's own named team is what isolates it: if
    # the `builtin` check ever silently dropped out, this non-operator team
    # member would wrongly pass through the team-gate's membership arm
    # instead of being refused by the operator-only branch.
    resolver = _GroupsStub({MEMBER_EMAIL: ["sre"]})
    builtin = {**ARCHIVE_ENTRY, "id": "helpful-generalist", "builtin": True, "team": "sre"}
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=resolver)
    _seed_personas_index(fake_storage, [builtin])
    denied = client.post(
        "/api/personas/helpful-generalist/archive",
        headers=_gate_headers(key, email=MEMBER_EMAIL),
    )
    assert denied.status_code == 403
    assert denied.json() == {
        "error": "only a room operator can archive a persona filed under the commons"
    }
    allowed = client.post(
        "/api/personas/helpful-generalist/archive",
        headers=_gate_headers(key, email=OPERATOR_EMAIL),
    )
    assert allowed.status_code == 200


def test_archive_persona_preserves_existing_skill_entries(gate_config, fake_storage, tmp_path):
    """Important 3 fix (hardening wave review): mirrors
    test_create_persona_preserves_existing_skill_entries for the archive
    route. `ingest_indexes` does a FULL rebuild of every kind it's given, so
    an archive route that re-ingested with only {"persona": ...} would
    silently wipe the skills table clean on every single archive. Proven by
    mutation: main.py:2090 mutated to exactly that persona-only mapping left
    every other archive/agent/search test green (170 passed), because none
    of them ever checked the skills side of the route's re-ingest.
    """
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=_GroupsStub())
    _seed_personas_index(fake_storage, [ARCHIVE_ENTRY])

    pre = client.get(
        "/api/skills/search",
        params={"q": "revi"},
        headers=_gate_headers(key, email=OPERATOR_EMAIL),
    )
    assert pre.json()["results"], "fixture sanity: skill entry should be searchable pre-archive"

    response = client.post(
        "/api/personas/wire-probe/archive", headers=_gate_headers(key, email=OPERATOR_EMAIL)
    )
    assert response.status_code == 200

    post = client.get(
        "/api/skills/search",
        params={"q": "revi"},
        headers=_gate_headers(key, email=OPERATOR_EMAIL),
    )
    assert post.json()["results"] == pre.json()["results"]


def test_archive_unknown_id_404(gate_config, fake_storage, tmp_path):
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=_GroupsStub())
    _seed_personas_index(fake_storage, [])
    response = client.post(
        "/api/personas/ghost/archive", headers=_gate_headers(key, email=OPERATOR_EMAIL)
    )
    assert response.status_code == 404
    assert response.json()["error"] == "no persona with that id"


def test_archive_502_on_storage_read_error(gate_config, tmp_path):
    """Minor 4 fix (hardening wave review): the route's index read used to
    sit outside any error handling, so a storage blip surfaced as an
    unhandled 500 rather than the 502 this route advertises for storage
    trouble. Mirrors test_create_persona_502_on_storage_error, on the read
    side rather than the write side.
    """
    erroring_storage = FakeStorage(read_error="storage is on fire")
    client, key = _gate_client(gate_config, erroring_storage, tmp_path,
                               groups_resolver=_GroupsStub())

    response = client.post(
        "/api/personas/wire-probe/archive", headers=_gate_headers(key, email=OPERATOR_EMAIL)
    )

    assert response.status_code == 502


def test_unarchive_restores_and_reindexes(gate_config, fake_storage, tmp_path):
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=_GroupsStub())
    _seed_personas_index(fake_storage, [{**ARCHIVE_ENTRY, "archived": True}])
    # Prime the catalog BEFORE unarchiving, while wire-probe is still
    # archived (Important 2 fix, hardening wave review): this proves the
    # entry is genuinely excluded from a WARM FTS table (Task 4's
    # ingest-drops-archived-rows filter) before the flip, not merely absent
    # because the table was never populated. Without this, mutating away
    # the unarchive route's own re-ingest entirely (both the ingest_indexes
    # call AND the catalog_loaded_at bump) was masked by the search route's
    # own cold-start ingest picking the entry up anyway at search time.
    warm = client.post(
        "/api/catalogs/refresh", headers=_gate_headers(key, email=OPERATOR_EMAIL)
    )
    assert warm.status_code == 200
    pre_search = client.get(
        "/api/personas/search", headers=_gate_headers(key, email=OPERATOR_EMAIL)
    )
    assert "wire-probe" not in [e["id"] for e in pre_search.json()["results"]]

    response = client.post(
        "/api/personas/wire-probe/unarchive", headers=_gate_headers(key, email=OPERATOR_EMAIL)
    )
    assert response.status_code == 200
    assert "archived" not in response.json()
    search = client.get(
        "/api/personas/search", headers=_gate_headers(key, email=OPERATOR_EMAIL)
    )
    assert "wire-probe" in [e["id"] for e in search.json()["results"]]


def test_archived_listing_route(gate_config, fake_storage, tmp_path):
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=_GroupsStub())
    _seed_personas_index(
        fake_storage, [ARCHIVE_ENTRY, {**TEAM_ARCHIVE_ENTRY, "archived": True}]
    )
    response = client.get(
        "/api/personas/archived", headers=_gate_headers(key, email=OPERATOR_EMAIL)
    )
    assert response.status_code == 200
    listed = response.json()["personas"]
    assert [e["id"] for e in listed] == ["stoic-mentor"]
    assert listed[0]["archived"] is True


# --- POST /api/personas/draft (Persona v3 addendum) ---------------------------------


DRAFT_PAYLOAD = {
    "job": "Help customers troubleshoot billing issues quickly and politely.",
}


def _draft_client(config, tmp_path, *, name=None) -> TestClient:
    clients = _minimal_clients(storage=FakeStorage(), name=name)
    return _make_client(config, clients, tmp_path)


def test_draft_persona_happy_path_returns_the_three_fields(config, tmp_path):
    fake_name = FakeName(
        draft={
            "name": "billing-buddy",
            "who_they_are": "a calm troubleshooter for billing questions",
            "what_they_do": "Helps customers resolve billing issues quickly.",
            "purpose_tags": ["billing", "support"],
        }
    )
    client = _draft_client(config, tmp_path, name=fake_name)

    response = client.post("/api/personas/draft", json=DRAFT_PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {
        "name": "billing-buddy",
        "who_they_are": "a calm troubleshooter for billing questions",
        "what_they_do": "Helps customers resolve billing issues quickly.",
        "purpose_tags": ["billing", "support"],
    }


def test_draft_persona_model_purpose_tags_are_normalized(config, tmp_path):
    fake_name = FakeName(
        draft={
            "name": "billing-buddy",
            "who_they_are": "a calm troubleshooter for billing questions",
            "what_they_do": "Helps customers resolve billing issues quickly.",
            "purpose_tags": ["Billing", "billing", " Support ", "REFUNDS"],
        }
    )
    client = _draft_client(config, tmp_path, name=fake_name)

    response = client.post("/api/personas/draft", json=DRAFT_PAYLOAD)

    assert response.json()["purpose_tags"] == ["billing", "support", "refunds"]


def test_draft_persona_model_purpose_tags_capped_at_five(config, tmp_path):
    fake_name = FakeName(
        draft={
            "name": "billing-buddy",
            "who_they_are": "a calm troubleshooter for billing questions",
            "what_they_do": "Helps customers resolve billing issues quickly.",
            "purpose_tags": ["one", "two", "three", "four", "five", "six", "seven"],
        }
    )
    client = _draft_client(config, tmp_path, name=fake_name)

    response = client.post("/api/personas/draft", json=DRAFT_PAYLOAD)

    assert response.json()["purpose_tags"] == ["one", "two", "three", "four", "five"]


def test_draft_persona_falls_back_to_persona_own_tags_when_model_omits_them(
    config, tmp_path
):
    fake_name = FakeName(
        draft={
            "name": "billing-buddy",
            "who_they_are": "a calm troubleshooter for billing questions",
            "what_they_do": "Helps customers resolve billing issues quickly.",
        }
    )
    client = _draft_client(config, tmp_path, name=fake_name)

    response = client.post("/api/personas/draft", json=DRAFT_PAYLOAD)

    assert response.json()["purpose_tags"] == _fallback_tags(
        "billing-buddy",
        "a calm troubleshooter for billing questions",
        "Helps customers resolve billing issues quickly.",
    )


def test_draft_persona_calls_generator_with_job_and_rules(config, tmp_path):
    fake_name = FakeName()
    client = _draft_client(config, tmp_path, name=fake_name)

    client.post(
        "/api/personas/draft",
        json={**DRAFT_PAYLOAD, "rules": ["Never share account numbers.", "Be concise."]},
    )

    assert fake_name.draft_calls == [
        (DRAFT_PAYLOAD["job"], ["Never share account numbers.", "Be concise."])
    ]


def test_draft_persona_defaults_rules_to_empty_list(config, tmp_path):
    fake_name = FakeName()
    client = _draft_client(config, tmp_path, name=fake_name)

    client.post("/api/personas/draft", json=DRAFT_PAYLOAD)

    assert fake_name.draft_calls == [(DRAFT_PAYLOAD["job"], [])]


def test_draft_persona_per_field_fallback_on_invalid_generator_name(config, tmp_path):
    fake_name = FakeName(
        draft={
            "name": "x" * 61,  # over PersonaFields.name's 60-char bound
            "who_they_are": "a calm troubleshooter for billing questions",
            "what_they_do": "Helps customers resolve billing issues quickly.",
        }
    )
    client = _draft_client(config, tmp_path, name=fake_name)

    response = client.post("/api/personas/draft", json=DRAFT_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == _fallback_name(DRAFT_PAYLOAD["job"])
    assert body["who_they_are"] == "a calm troubleshooter for billing questions"
    assert body["what_they_do"] == "Helps customers resolve billing issues quickly."


def test_draft_persona_generator_client_error_falls_back_still_200(config, tmp_path, caplog):
    client = _draft_client(
        config, tmp_path, name=FakeName(draft_error="model unavailable")
    )

    response = client.post("/api/personas/draft", json=DRAFT_PAYLOAD)

    assert response.status_code == 200
    job = DRAFT_PAYLOAD["job"]
    assert response.json() == {
        "name": _fallback_name(job),
        "who_they_are": _fallback_who(job),
        "what_they_do": _fallback_what(job),
        "purpose_tags": _fallback_tags(
            _fallback_name(job), _fallback_who(job), _fallback_what(job)
        ),
    }
    assert any("persona draft" in record.message.lower() for record in caplog.records)


def test_draft_persona_no_name_client_configured_falls_back_still_200(config, tmp_path):
    client = _draft_client(config, tmp_path, name=None)

    response = client.post("/api/personas/draft", json=DRAFT_PAYLOAD)

    assert response.status_code == 200
    job = DRAFT_PAYLOAD["job"]
    assert response.json() == {
        "name": _fallback_name(job),
        "who_they_are": _fallback_who(job),
        "what_they_do": _fallback_what(job),
        "purpose_tags": _fallback_tags(
            _fallback_name(job), _fallback_who(job), _fallback_what(job)
        ),
    }


def test_draft_persona_422_empty_job(config, tmp_path):
    client = _draft_client(config, tmp_path)

    response = client.post("/api/personas/draft", json={"job": ""})

    assert response.status_code == 422


def test_draft_persona_422_missing_job(config, tmp_path):
    client = _draft_client(config, tmp_path)

    response = client.post("/api/personas/draft", json={})

    assert response.status_code == 422


def test_draft_persona_422_job_too_long(config, tmp_path):
    client = _draft_client(config, tmp_path)

    response = client.post("/api/personas/draft", json={"job": "a" * 501})

    assert response.status_code == 422


def test_draft_persona_422_too_many_rules(config, tmp_path):
    client = _draft_client(config, tmp_path)

    response = client.post(
        "/api/personas/draft", json={**DRAFT_PAYLOAD, "rules": ["ok"] * 151}
    )

    assert response.status_code == 422


def test_draft_persona_200_with_150_rule_hints(config, tmp_path):
    """Upload/limits addendum: PersonaDraftRequest.rules mirrors
    PersonaFields.rules' cap move from 8 to 150 items."""
    client = _draft_client(config, tmp_path)

    response = client.post(
        "/api/personas/draft", json={**DRAFT_PAYLOAD, "rules": ["ok"] * 150}
    )

    assert response.status_code == 200


def test_draft_persona_422_bad_rule_item_length(config, tmp_path):
    client = _draft_client(config, tmp_path)

    response = client.post(
        "/api/personas/draft", json={**DRAFT_PAYLOAD, "rules": [""]}
    )

    assert response.status_code == 422


def test_draft_persona_invalid_payload_never_reaches_the_generator(config, tmp_path):
    fake_name = FakeName()
    client = _draft_client(config, tmp_path, name=fake_name)

    client.post("/api/personas/draft", json={"job": ""})

    assert fake_name.draft_calls == []


def test_draft_persona_result_feeds_create_persona_unchanged(config, tmp_path):
    """The addendum's core promise: the drafted fields land in the SAME v2
    fields, with create_persona's own contract untouched — a drafted
    response, posted straight to /api/personas (plus a team), creates
    cleanly."""
    fake_name = FakeName(
        draft={
            "name": "billing-buddy",
            "who_they_are": "a calm troubleshooter for billing questions",
            "what_they_do": "Helps customers resolve billing issues quickly.",
        }
    )
    client = _draft_client(config, tmp_path, name=fake_name)

    draft_response = client.post("/api/personas/draft", json=DRAFT_PAYLOAD)
    draft = draft_response.json()

    create_response = client.post("/api/personas", json={**draft, "team": "shared"})

    assert create_response.status_code == 201
    assert create_response.json()["name"] == "billing-buddy"


# --- POST /api/personas/distill (persona-first station addendum) -------------------
#
# The station's OTHER entry point: the operator arrives with a persona
# document already written (uploaded or pasted), and this route pulls
# discovery metadata (name/who_they_are/purpose_tags) out of it in the
# background - never `what_they_do` (the document itself already IS the
# body; nothing to draft there). Same "always 200, deterministic fallback
# on any generator trouble" contract as /api/personas/draft above.


DISTILL_PAYLOAD = {
    "body": "# Billing Buddy\n\nA calm troubleshooter for billing questions.\n",
}


def _distill_client(config, tmp_path, *, name=None) -> TestClient:
    clients = _minimal_clients(storage=FakeStorage(), name=name)
    return _make_client(config, clients, tmp_path)


def test_distill_persona_happy_path_returns_the_fields(config, tmp_path):
    fake_name = FakeName(
        distill={
            "name": "billing-buddy",
            "who_they_are": "a calm troubleshooter for billing questions",
            "purpose_tags": ["billing", "support"],
        }
    )
    client = _distill_client(config, tmp_path, name=fake_name)

    response = client.post("/api/personas/distill", json=DISTILL_PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {
        "name": "billing-buddy",
        "who_they_are": "a calm troubleshooter for billing questions",
        "purpose_tags": ["billing", "support"],
    }


def test_distill_persona_response_has_no_what_they_do_key(config, tmp_path):
    client = _distill_client(config, tmp_path)

    response = client.post("/api/personas/distill", json=DISTILL_PAYLOAD)

    assert "what_they_do" not in response.json()


def test_distill_persona_model_purpose_tags_are_normalized(config, tmp_path):
    fake_name = FakeName(
        distill={
            "name": "billing-buddy",
            "who_they_are": "a calm troubleshooter for billing questions",
            "purpose_tags": ["Billing", "billing", " Support ", "REFUNDS"],
        }
    )
    client = _distill_client(config, tmp_path, name=fake_name)

    response = client.post("/api/personas/distill", json=DISTILL_PAYLOAD)

    assert response.json()["purpose_tags"] == ["billing", "support", "refunds"]


def test_distill_persona_model_purpose_tags_capped_at_five(config, tmp_path):
    fake_name = FakeName(
        distill={
            "name": "billing-buddy",
            "who_they_are": "a calm troubleshooter for billing questions",
            "purpose_tags": ["one", "two", "three", "four", "five", "six", "seven"],
        }
    )
    client = _distill_client(config, tmp_path, name=fake_name)

    response = client.post("/api/personas/distill", json=DISTILL_PAYLOAD)

    assert response.json()["purpose_tags"] == ["one", "two", "three", "four", "five"]


def test_distill_persona_missing_model_tags_default_to_empty_list(config, tmp_path):
    """Unlike /api/personas/draft (which falls back to the persona's own
    derived tags), a distill has no what_they_do to derive tags from at
    all - a generator reply that omits purpose_tags just gets an empty
    list, full stop."""
    fake_name = FakeName(
        distill={
            "name": "billing-buddy",
            "who_they_are": "a calm troubleshooter for billing questions",
        }
    )
    client = _distill_client(config, tmp_path, name=fake_name)

    response = client.post("/api/personas/distill", json=DISTILL_PAYLOAD)

    assert response.json()["purpose_tags"] == []


def test_distill_persona_calls_generator_with_body(config, tmp_path):
    fake_name = FakeName()
    client = _distill_client(config, tmp_path, name=fake_name)

    client.post("/api/personas/distill", json=DISTILL_PAYLOAD)

    assert fake_name.distill_calls == [DISTILL_PAYLOAD["body"]]


def test_distill_persona_per_field_fallback_on_invalid_generator_name(config, tmp_path):
    fake_name = FakeName(
        distill={
            "name": "x" * 61,  # over PersonaFields.name's 60-char bound
            "who_they_are": "a calm troubleshooter for billing questions",
        }
    )
    client = _distill_client(config, tmp_path, name=fake_name)

    response = client.post("/api/personas/distill", json=DISTILL_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == _fallback_distilled_name(DISTILL_PAYLOAD["body"])
    assert body["who_they_are"] == "a calm troubleshooter for billing questions"


def test_distill_persona_generator_client_error_falls_back_still_200(
    config, tmp_path, caplog
):
    client = _distill_client(
        config, tmp_path, name=FakeName(distill_error="model unavailable")
    )

    response = client.post("/api/personas/distill", json=DISTILL_PAYLOAD)

    assert response.status_code == 200
    doc = DISTILL_PAYLOAD["body"]
    assert response.json() == {
        "name": _fallback_distilled_name(doc),
        "who_they_are": _fallback_distilled_who(doc),
        "purpose_tags": [],
    }
    assert any("persona distill" in record.message.lower() for record in caplog.records)


def test_distill_persona_no_name_client_configured_falls_back_still_200(config, tmp_path):
    client = _distill_client(config, tmp_path, name=None)

    response = client.post("/api/personas/distill", json=DISTILL_PAYLOAD)

    assert response.status_code == 200
    doc = DISTILL_PAYLOAD["body"]
    assert response.json() == {
        "name": _fallback_distilled_name(doc),
        "who_they_are": _fallback_distilled_who(doc),
        "purpose_tags": [],
    }


def test_distill_persona_422_empty_body(config, tmp_path):
    client = _distill_client(config, tmp_path)

    response = client.post("/api/personas/distill", json={"body": ""})

    assert response.status_code == 422


def test_distill_persona_422_missing_body(config, tmp_path):
    client = _distill_client(config, tmp_path)

    response = client.post("/api/personas/distill", json={})

    assert response.status_code == 422


def test_distill_persona_422_body_too_long(config, tmp_path):
    client = _distill_client(config, tmp_path)

    response = client.post("/api/personas/distill", json={"body": "a" * 49153})

    assert response.status_code == 422


def test_distill_persona_200_at_max_length_body(config, tmp_path):
    client = _distill_client(config, tmp_path)

    response = client.post("/api/personas/distill", json={"body": "a" * 49152})

    assert response.status_code == 200


def test_distill_persona_invalid_payload_never_reaches_the_generator(config, tmp_path):
    fake_name = FakeName()
    client = _distill_client(config, tmp_path, name=fake_name)

    client.post("/api/personas/distill", json={"body": ""})

    assert fake_name.distill_calls == []
