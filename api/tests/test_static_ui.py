"""Route-level tests for serving the built UI when `config.ui_dist` is set
(Task 1). Builds the app the same way every other route-level test module
does (`create_app` + the shared `session_factory`/`config` fixtures from
conftest.py), just with `ui_dist` pointed at a tmp_path fixture dist
directory instead of left unset.
"""

import pytest
from fastapi.testclient import TestClient

from kobo_api.main import create_app

INDEX_HTML = "<html><body>SPA-INDEX-MARKER</body></html>"
APP_JS = "console.log('app');"


def make_dist(tmp_path, *, with_index=True):
    """A minimal Vite-shaped `dist/` dir: `index.html` + `assets/app.js`."""
    dist_dir = tmp_path / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)
    if with_index:
        (dist_dir / "index.html").write_text(INDEX_HTML)
    (assets_dir / "app.js").write_text(APP_JS)
    return dist_dir


def test_root_returns_index_html(session_factory, config, tmp_path):
    dist_dir = make_dist(tmp_path)
    ui_config = config.model_copy(update={"ui_dist": dist_dir})
    client = TestClient(create_app(session_factory, config=ui_config))

    response = client.get("/")

    assert response.status_code == 200
    assert response.text == INDEX_HTML


def test_spa_route_returns_index_html(session_factory, config, tmp_path):
    dist_dir = make_dist(tmp_path)
    ui_config = config.model_copy(update={"ui_dist": dist_dir})
    client = TestClient(create_app(session_factory, config=ui_config))

    response = client.get("/agents/whatever")

    assert response.status_code == 200
    assert response.text == INDEX_HTML


def test_assets_are_served(session_factory, config, tmp_path):
    dist_dir = make_dist(tmp_path)
    ui_config = config.model_copy(update={"ui_dist": dist_dir})
    client = TestClient(create_app(session_factory, config=ui_config))

    response = client.get("/assets/app.js")

    assert response.status_code == 200
    assert response.text == APP_JS


def test_api_routes_unaffected_by_ui_mount(session_factory, config, tmp_path):
    dist_dir = make_dist(tmp_path)
    ui_config = config.model_copy(update={"ui_dist": dist_dir})
    client = TestClient(create_app(session_factory, config=ui_config))

    healthz = client.get("/api/healthz")
    agents = client.get("/api/agents")

    assert healthz.status_code == 200
    assert healthz.json() == {"ok": True}
    assert agents.status_code == 200
    assert agents.json() == {"agents": [], "groups_unavailable": False}


def test_unknown_api_path_still_404s_not_swallowed_by_spa_fallback(
    session_factory, config, tmp_path
):
    dist_dir = make_dist(tmp_path)
    ui_config = config.model_copy(update={"ui_dist": dist_dir})
    client = TestClient(create_app(session_factory, config=ui_config))

    response = client.get("/api/no-such-route")

    assert response.status_code == 404
    assert response.text != INDEX_HTML


def test_ui_dist_unset_root_404s_exactly_as_today(client):
    # `client` (conftest.py) never sets ui_dist — this proves dev parity:
    # no UI mounted means "/" is just an unregistered path, same 404 as
    # before this task existed.
    response = client.get("/")

    assert response.status_code == 404


def test_missing_index_html_raises_at_create_app(session_factory, config, tmp_path):
    dist_dir = make_dist(tmp_path, with_index=False)
    ui_config = config.model_copy(update={"ui_dist": dist_dir})

    with pytest.raises(RuntimeError):
        create_app(session_factory, config=ui_config)
