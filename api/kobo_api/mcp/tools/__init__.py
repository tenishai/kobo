"""kobo's MCP tool modules (MCP server arc, Task 7 onward): `read.py`
carries the read-only tools; `write.py` carries the mutating ones -- see
each module's own docstring for its current tool count and roster, never
duplicated here where a later wave would have to remember to update both
places in lockstep. Each module exposes its own `register(mcp, app)` --
`kobo_api.mcp.server.build_mcp_server` calls both in turn. No aggregator
function lives here on purpose: two flat `register` calls in `server.py`
are already the whole wiring story, and a wrapper around them would only
hide which tools came from which module.
"""

from __future__ import annotations
