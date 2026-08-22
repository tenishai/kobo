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
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<circle cx="12" cy="12" r="10" /></svg>'
)


def make_dist(tmp_path, *, with_index=True, with_favicon=False):
    """A minimal Vite-shaped `dist/` dir: `index.html` + `assets/app.js`."""
    dist_dir = tmp_path / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)
    if with_index:
        (dist_dir / "index.html").write_text(INDEX_HTML)
    (assets_dir / "app.js").write_text(APP_JS)
    if with_favicon:
        (dist_dir / "favicon.svg").write_text(FAVICON_SVG)
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


def test_favicon_svg_served_with_svg_content_type(session_factory, config, tmp_path):
    # HIGH bug this pins the fix for: /favicon.svg used to fall into the SPA
    # catch-all and come back as index.html (text/html), which is wrong for
    # a file a browser tab requests expecting actual SVG bytes.
    dist_dir = make_dist(tmp_path, with_favicon=True)
    ui_config = config.model_copy(update={"ui_dist": dist_dir})
    client = TestClient(create_app(session_factory, config=ui_config))

    response = client.get("/favicon.svg")

    assert response.status_code == 200
    assert response.text == FAVICON_SVG
    assert response.text != INDEX_HTML
    assert response.headers["content-type"].startswith("image/svg+xml")


def test_spa_route_still_wins_over_favicon_handling(session_factory, config, tmp_path):
    # A real SPA route (not a root static file) must be unaffected by the
    # new root-static-file check.
    dist_dir = make_dist(tmp_path, with_favicon=True)
    ui_config = config.model_copy(update={"ui_dist": dist_dir})
    client = TestClient(create_app(session_factory, config=ui_config))

    response = client.get("/agents/whatever")

    assert response.status_code == 200
    assert response.text == INDEX_HTML


def test_missing_root_static_file_falls_back_to_spa_index(
    session_factory, config, tmp_path
):
    # Pinned semantic: a plausible-looking but absent root static file (e.g.
    # a stale favicon reference, a typo) is treated like any other unknown
    # client-side route -- it returns the SPA shell (200, index.html), not a
    # 404. Only unmatched /api/... paths 404 (see the test below).
    dist_dir = make_dist(tmp_path, with_favicon=False)
    ui_config = config.model_copy(update={"ui_dist": dist_dir})
    client = TestClient(create_app(session_factory, config=ui_config))

    response = client.get("/nonexistent.svg")

    assert response.status_code == 200
    assert response.text == INDEX_HTML


def test_nested_svg_path_is_not_treated_as_root_static_file(
    session_factory, config, tmp_path
):
    # Name-only check, no subdirs beyond what /assets already covers: a
    # path containing "/" must never be served straight off disk here, even
    # if a same-named file happens to exist at that nested path -- that
    # would blur the line into arbitrary-file-read territory. It falls
    # through to the ordinary SPA fallback instead.
    dist_dir = make_dist(tmp_path, with_favicon=True)
    nested_dir = dist_dir / "nested"
    nested_dir.mkdir()
    (nested_dir / "favicon.svg").write_text(FAVICON_SVG)
    ui_config = config.model_copy(update={"ui_dist": dist_dir})
    client = TestClient(create_app(session_factory, config=ui_config))

    response = client.get("/nested/favicon.svg")

    assert response.status_code == 200
    assert response.text == INDEX_HTML


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
