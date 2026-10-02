"""GitHub App installation token minting (deployability plan, Task 3).

Deployed, kobo authenticates to GitHub as an installed App rather than
borrowing the operator's ambient `gh auth` session (`GhCLI`'s default,
dev-only mode). This module is the seam that mints the short-lived
credential `GhCLI` hands to every `gh`/`git` invocation via
`token_provider` (`kobo_api.clients`): a per-App RS256 JWT exchanged for a
per-installation access token, cached until shortly before it expires.

Same iat/exp convention as the family's other GitHub App clients
(`gojira/gojira/github.py`, `bao_secrets/clients/github_app.py`): `iat`
back-dated 60s to tolerate clock skew, `exp` 9 minutes out — comfortably
inside GitHub's 10-minute App-JWT ceiling.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx
import jwt

from kobo_api.clients import ClientError

log = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_ACCEPT = "application/vnd.github+json"

# App JWT lifetime: iat 60s in the past (clock-skew tolerance), exp 9
# minutes out — see module docstring.
_JWT_BACKDATE_S = 60
_JWT_LIFETIME_S = 9 * 60

# Refresh the cached installation token once it's within 5 minutes of its
# reported expiry, rather than waiting to observe an actual 401 — an
# installation token is only good for ~1h, and a request that starts just
# before expiry could otherwise fail mid-flight.
_REFRESH_MARGIN_S = 5 * 60


class InstallationTokenProvider:
    """Mints and caches a GitHub App installation access token.

    `.token()` is the seam `GhCLI` (kobo_api.clients) calls before every
    `gh`/`git` invocation — cheap when the cached token is still fresh,
    and transparently refreshes it otherwise. The App's private key is
    read from `private_key_path` lazily, on first use (not at
    construction), and its contents are never logged.

    `http_client` and `now` are test seams only (a real `httpx.Client` and
    `time.time` by default) — no other caller needs to pass them.
    """

    def __init__(
        self,
        app_id: str,
        installation_id: str,
        private_key_path: Path,
        http_client: httpx.Client | None = None,
        now: Callable[[], float] | None = None,
    ):
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key_path = private_key_path
        self._http_client = http_client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._now = now or time.time
        self._private_key: str | None = None
        self._token: str | None = None
        self._expires_at: float | None = None
        # The permissions GitHub granted the installation, as returned with
        # every minted token ({"contents": "write", ...}). Kept because the
        # `permissions` object on a repository read is a USER's standing and
        # says nothing true about an App (see `can_write_contents`).
        self._permissions: dict[str, str] = {}

    def _load_private_key(self) -> str:
        if self._private_key is None:
            try:
                self._private_key = self._private_key_path.read_text()
            except OSError as exc:
                # Never the key material — just the (non-secret) path.
                raise ClientError(
                    f"github app private key not found: {self._private_key_path}"
                ) from exc
        return self._private_key

    def _app_jwt(self) -> str:
        now = int(self._now())
        payload = {
            "iat": now - _JWT_BACKDATE_S,
            "exp": now + _JWT_LIFETIME_S,
            "iss": self._app_id,
        }
        return jwt.encode(payload, self._load_private_key(), algorithm="RS256")

    def token(self) -> str:
        """The current installation access token, refreshing first if the
        cached one is missing or within 5 minutes of expiry."""
        if (
            self._token is not None
            and self._expires_at is not None
            and self._expires_at - self._now() > _REFRESH_MARGIN_S
        ):
            return self._token
        self._refresh()
        assert self._token is not None  # _refresh always sets it or raises
        return self._token

    def _refresh(self) -> None:
        try:
            resp = self._http_client.post(
                f"{_GITHUB_API}/app/installations/{self._installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {self._app_jwt()}", "Accept": _ACCEPT},
            )
        except httpx.HTTPError as exc:
            log.debug("installation token request failed: %r", exc)
            raise ClientError("failed to reach GitHub to mint an installation token") from exc

        if resp.status_code != 201:
            # GitHub's error body can echo request context (e.g. the
            # installation id) — logged server-side only, never in the
            # message a caller (and, from there, an HTTP response) might
            # surface. Status code alone is safe and still actionable.
            log.debug(
                "installation token mint failed (%s): %s",
                resp.status_code, resp.text[:500],
            )
            raise ClientError(
                f"installation token mint failed ({resp.status_code})"
            )

        data = resp.json()
        self._token = data["token"]
        self._expires_at = datetime.fromisoformat(data["expires_at"]).timestamp()
        self._permissions = {
            str(k): str(v) for k, v in (data.get("permissions") or {}).items()
        }

    def can_write_contents(self) -> bool:
        """Whether this installation may change repository contents.

        The App's `contents` permission is granted per installation, not per
        repository, and GitHub states it in the body of every minted token.
        That is the only honest source: `GET repos/{repo}` answers an App
        with `permissions` all False, `pull` included, even on a repository
        the App created and pushes to daily (a client org, 2026-09-25: nine such
        repositories, all `push: false`). Reading `push` there made every
        connect scan refuse every repository. Mints a token first if none
        is cached, so the answer is never a stale default.
        """
        self.token()
        return self._permissions.get("contents") in {"write", "admin"}
