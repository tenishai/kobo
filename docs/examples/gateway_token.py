"""A connected agent's side of the room's gateway contract, standard library only.

The room gives an agent that runs its team's own code three things in its
environment (guide section "Running your own code"):

    CABAL_COLLEAGUE_TOKEN   the agent's pass: standing identity, a secret
    KOBO_SWITCHBOARD_URL    where the pass is traded for a gateway token
    KOBO_GATEWAY_URL        the tool gateway, an MCP server

The pass never goes to the gateway. `GatewayToken` trades it for the short
token the gateway does take and keeps that one fresh; hand `headers()` to
whichever MCP client the agent already uses:

    token = GatewayToken()
    async with streamablehttp_client(os.environ["KOBO_GATEWAY_URL"], headers=token.headers()) as ...

and call `headers()` again for each new connection, or after a 401.
Copy this file; it is yours to change.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime

# Trade again this long before the token runs out: one that dies in the
# middle of a call helps nobody.
SLACK_SECONDS = 60.0


class GatewayToken:
    def __init__(self, switchboard_url: str | None = None, pass_env: str = "CABAL_COLLEAGUE_TOKEN"):
        self._url = (switchboard_url or os.environ["KOBO_SWITCHBOARD_URL"]).rstrip("/")
        self._pass_env = pass_env
        self._token = ""
        self._expires = 0.0

    def get(self) -> str:
        """The current gateway token, traded for again when it is about to run out."""
        if time.time() >= self._expires - SLACK_SECONDS:
            self._trade()
        return self._token

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get()}"}

    def forget(self) -> None:
        """Call on a 401 from the gateway: the next `get` trades again."""
        self._expires = 0.0

    def _trade(self) -> None:
        agent_pass = os.environ.get(self._pass_env, "")
        if not agent_pass:
            raise RuntimeError(f"${self._pass_env} is empty: the room's wiring has not reached this agent yet")
        request = urllib.request.Request(
            f"{self._url}/auth/liminal-token", method="POST", headers={"Authorization": f"Bearer {agent_pass}"}
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            answer = json.load(response)
        self._token = answer["access_token"]
        try:
            self._expires = datetime.fromisoformat(answer["expires_at"]).timestamp()
        except (KeyError, ValueError, TypeError):
            # An expiry that cannot be read is treated as already past, so the
            # next use trades again instead of trusting a lifetime nobody stated.
            self._expires = 0.0
