import pytest

from pnyx_api.catalog import NAME_RE, RESERVED_NAMES, validate_name
from pnyx_api.models import Agent

# --- NAME_RE -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("echo", True),
        ("a1", True),
        ("valid-name", True),
        ("A", False),
        ("1abc", False),
        ("ab", True),
        ("a", False),
        ("has_underscore", False),
        ("Has-Caps", False),
        ("a" * 24, True),
        ("a" * 25, False),
    ],
)
def test_name_re(name, expected):
    assert bool(NAME_RE.fullmatch(name)) == expected


# --- validate_name -------------------------------------------------------------


def test_validate_name_valid_returns_none():
    assert validate_name("echo") is None
    assert validate_name("a" * 24) is None


def test_validate_name_too_long_names_the_max():
    assert validate_name("a" * 25) == "too long (max 24)"
    # A too-long name that would ALSO fail other checks (e.g. a trailing
    # hyphen, or collides with a reserved name once truncated) still reports
    # "too long" first — the length check runs before everything else.
    assert validate_name(("a" * 24) + "-") == "too long (max 24)"


def test_validate_name_malformed_charset_or_shape_is_invalid_name():
    assert validate_name("Not_Valid") == "invalid name"
    assert validate_name("1abc") == "invalid name"
    assert validate_name("a") == "invalid name"


def test_validate_name_trailing_hyphen():
    assert validate_name("agent-") == "must not end with a hyphen"


def test_validate_name_24char_ending_hyphen_reports_hyphen_not_length():
    # Boundary + reason combo: a 24-char name ending in a hyphen is at max
    # length but must report "must not end with a hyphen", not "too long",
    # since the trailing-hyphen check runs after the length check.
    name_24_with_hyphen = "a" * 23 + "-"
    assert len(name_24_with_hyphen) == 24
    assert validate_name(name_24_with_hyphen) == "must not end with a hyphen"


@pytest.mark.parametrize("name", sorted(RESERVED_NAMES))
def test_validate_name_reserved(name):
    assert validate_name(name) == "reserved name"


# --- availability route -------------------------------------------------------


def test_availability_invalid_name(client):
    response = client.get("/api/availability", params={"name": "Not_Valid"})

    assert response.status_code == 200
    assert response.json() == {"available": False, "reason": "invalid name"}


def test_availability_fresh_name(client):
    response = client.get("/api/availability", params={"name": "brand-new"})

    assert response.status_code == 200
    assert response.json() == {"available": True, "reason": None}


def test_availability_name_taken(client, session_factory):
    session = session_factory()
    session.add(
        Agent(
            name="taken-name",
            persona="helpful-generalist",
            model_id="vertex_ai/claude-sonnet-4-5",
        )
    )
    session.commit()
    session.close()

    response = client.get("/api/availability", params={"name": "taken-name"})

    assert response.status_code == 200
    assert response.json() == {"available": False, "reason": "name taken"}


def test_availability_invalid_name_with_trailing_newline(client):
    response = client.get("/api/availability", params={"name": "ab\n"})

    assert response.status_code == 200
    assert response.json() == {"available": False, "reason": "invalid name"}


def test_availability_too_long_name(client):
    response = client.get("/api/availability", params={"name": "a" * 25})

    assert response.status_code == 200
    assert response.json() == {"available": False, "reason": "too long (max 24)"}


def test_availability_trailing_hyphen(client):
    response = client.get("/api/availability", params={"name": "agent-"})

    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "reason": "must not end with a hyphen",
    }


def test_availability_reserved_name(client):
    response = client.get("/api/availability", params={"name": "cortex"})

    assert response.status_code == 200
    assert response.json() == {"available": False, "reason": "reserved name"}
