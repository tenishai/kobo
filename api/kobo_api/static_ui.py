"""Serves the built UI (a Vite `dist/` directory) alongside the API when
`config.ui_dist` is set (Task 1) — one origin, no CORS, no separate dev
server. `create_app` (kobo_api.main) calls `mount_ui` LAST, after every
`/api/*` route has already been registered.

Design choice, disclosed per the task brief: a bare
`StaticFiles(directory=dist_dir, html=True)` mount at "/" would already lose
to every `/api/*` route — Starlette matches routes/mounts in registration
order, and those routes are registered first — so it would never actually
shadow anything. This module uses an explicit catch-all GET route for the
SPA fallback instead, because that makes the `/api` precedence a readable,
testable line of code (a path starting with "api" falls through to a real
404) rather than a fact that's true only as an accidental consequence of
mount order. It also means an unmatched `/api/...` path (a typo, a removed
route) still 404s honestly instead of being swallowed into `index.html`.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def mount_ui(app: FastAPI, dist_dir: Path) -> None:
    """Mount `dist_dir`'s assets and register the SPA fallback on `app`.

    `dist_dir/assets` is served as-is (normal static headers; Starlette's
    `StaticFiles` has no directory-listing feature at all, so none is
    exposed). Every other GET whose path does not start with `api` returns
    `dist_dir/index.html`, letting the client-side router handle it.

    Fails loud at boot, not per-request: a missing `index.html` raises
    `RuntimeError` immediately (as does a missing `assets` directory, via
    `StaticFiles`'s own startup check) rather than letting every UI request
    404 or 500 later, one at a time, at request time.
    """
    index_path = dist_dir / "index.html"
    if not index_path.is_file():
        raise RuntimeError(f"ui_dist is set but {index_path} does not exist")

    app.mount(
        "/assets", StaticFiles(directory=dist_dir / "assets"), name="ui-assets"
    )

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> FileResponse:
        # Belt-and-suspenders, not the actual precedence mechanism: every
        # `/api/*` route above already wins on registration order alone.
        # This just keeps an *unmatched* `/api/...` path (no such route) a
        # real 404 instead of a misleading 200 of index.html.
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        return FileResponse(index_path)
