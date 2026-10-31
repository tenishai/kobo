# Adding a vendor

What it takes for an agent to use a vendor through the room, in the order the
pieces have to exist. Every item here was missed at least once while Datadog
went in (2026-10-30), and each miss looked like "the agent cannot reach the
vendor" with a different cause. Walk the list top to bottom; the smoke test at
the end tells you which gate is still closed.

The model: the **permission service** decides and mints (a grant becomes a
one-hour lease), the **tool gateway** attaches the leased credential to the
call and forwards it, and the **room** is where people connect the vendor and
approve agents. An agent holds no standing credential and never sees one.

## Once per room (not per vendor)

| # | Gate | Where | How you know it is closed |
|---|------|-------|---------------------------|
| 0a | The org config names the gateway's data plane: `liminal_url`. Without it the composer writes no gateway block into any agent, whatever its permissions say. | org config (`liminal_url`, next to `liminal_admin_url`, `cabal_url`) | Agents answer "I have no tool to do that"; `[tools] offering:` in an agent's log lists only `skills.load` and `colleagues.*`. |
| 0b | The gateway's admin plane is reachable by the room (`liminal_admin_url`) so a grant and a live target apply at once. | org config | Integrations rows read Pending with a pull request instead of Connected; Approve says "the gateway admin plane is not wired". |
| 0c | The permission service is on (`bab_url` + `KOBO_BAB_TOKEN`) and the gateway may lease for agents (`BAB_GATEWAY_SERVICE_ACCOUNT` on the service, the projected token on the gateway). | org config; the deploy of each | Permissions section absent; or the gateway logs "permission-service lease failed". |
| 0d | Agents made before 0a was set were wired once by hand (the gateway block in `bundle/agent.yaml`). New agents get it at create. | the agent's repository | Same symptom as 0a for one agent only. |

## Per vendor, in order

| # | Gate | Where | Notes |
|---|------|-------|-------|
| 1 | **Catalogue**: the vendor's permissions, tier `read` or `mutate`, with the vendor's own scope names in `extra` for the minter. | permission service `catalogues/room.yaml` | One row per thing an agent may be granted. Read permissions become the connection's ceiling. |
| 2 | **Minter**: how a grant becomes a per-agent credential (scoped key, STS session, installation token). No per-agent credential at the vendor means a static-token lane through the gateway instead, held once by the room. | permission service `service/minters.py` | Add every scope the vendor's MCP server needs on top of the resource scopes (Datadog: `mcp_read`, `mcp_write`). |
| 3 | **Vendor side, done by a person**: the service account or role the room mints from, the management credential it needs (`service_account_write` and the right to look users up, for Datadog), the vendor's MCP access switch (Datadog: Organization Settings > MCP Access), and the per-agent permissions the role must hold (`mcp_read`). | the vendor's console | The connect form's note must say exactly this; nothing in the room can do it. |
| 4 | **Connect form**: the auth kind, its fields, its checks, the probe. | `api/kobo_api/admin_integrations.py` (`AUTH_KINDS`, `check_auth`, a `_connect_<vendor>`), `ui/src/lib/admin/catalog.ts` | The probe must mint and delete a real credential before anything is saved. |
| 5 | **Gateway target or handler**: the vendor's hosted MCP server as a live target (`PUT /admin/targets/<vendor>-mcp` through the admin plane, `auth_method = "service-headers"` or `"service-token"`, credentials from the lease), or a handler in the gateway when the vendor has no usable server. | `admin_integrations.live_target_spec` / `<vendor>_target_spec`; gateway `credentials.py` must know the integration a target leases (`<integration>-mcp` leases `<integration>`) | Verify the vendor's hosted server takes a key or token for a non-interactive caller before choosing it (Datadog: `DD_API_KEY`/`DD_APPLICATION_KEY` headers; Atlassian: service-account key as Bearer). |
| 6 | **The lane**: what an agent granted this vendor needs on the gateway (scope, handlers) **and in its own bundle** (allow patterns). Approving the first permission applies both; the second half restarts the agent once. | `api/kobo_api/gateway_lane.py` `GATEWAY_LANES` | The runtime offers a gateway tool only when the bundle's allow list matches it, however the gateway is configured. Forgetting `allow` is the quietest failure: the agent lists the tools at the gateway and offers none to its model. |
| 7 | **Skills**: any skill that mentions the vendor must tell an agent to use the gateway tools by name, and must not ask for a shell, keys or curl. | the skills catalogue repository | A skill written for a person's terminal makes the agent say "I have no shell". Keep the terminal recipe under its own heading for people. |
| 8 | **Guide**: the Integrations paragraph and the Permissions paragraph name the vendor and what the person must do at the vendor. | `api/kobo_api/templates/guide/07-access-and-groups.md`, `08-the-harness.md` | Same pull request as the form. |
| 9 | **Wire test, both layers, green**: the gateway-level script against the vendor with one agent's real lease, then the agent-level script through the switchboard. Run before anyone is asked to try the vendor in chat, and again after any change to the gateway, the permission service or the runtime. Red means not done. | the deployment config repository, `scripts/wire-test/` (README there) | Chat is not the test harness: a person asking in Slack finds one failure per turn and cannot tell which layer it is. The scripts find all of them in one run and name the layer. |

## What GitHub taught (vendor 1, 2026-10-31)

- **The vendor's MCP server may need a credential for the catalog fetch too, and it must be one nobody rotates.** GitHub's refuses an unauthenticated `initialize` and wants the credential on `Authorization`, which liminal's `catalog_headers_env` may not set (liminal owns that header). The first cut reached for a long-lived personal token in the gateway's env; a person has to make that and rotate it yearly, and the room would break silently when it lapsed. The target names `catalog_token_provider = "github-app"` instead (liminal 0.9.2): the gateway registers a provider under liminal's `liminal.catalog_tokens` entry-point group that mints its own App installation token before every catalog fetch. The rule for the next vendor: if the catalog needs a credential, the gateway mints it from something it already holds, or the vendor's named-header credential goes through `catalog_headers_env`; never a token a person made.
- **Per-request switches are target `headers`, not session state.** GitHub honours `X-MCP-Readonly` only on the request that carries it, so the spec sends it on every request. Test the header's scope with two requests before relying on it.
- **The app or account the room mints from must hold the union of the catalogue rows.** A GitHub App without `issues: read` refuses any token that asks for it (HTTP 422), so gate 3 lists the exact permissions and gate 1's rows must stay inside them. Read the live app's permissions (`GET /app/installations/{id}` with the app's JWT) before writing the rows.
- **Reuse what the room already holds.** The room's own GitHub App (Integrations > GitHub) is the natural minter; the connect form takes blanks to mean it and only asks for a second app when someone pastes one.
- **Release mechanics differ per repository.** The kobo release script edits `api/pyproject.toml` and `ui/`; bab and the others bump `pyproject.toml` by hand. The release workflow refuses a tag whose version the project does not declare, so a mismatch costs a version number, not a bad image.

## Smoke test, in order

1. **Connect** on Admin > Integrations: the row reads Connected at once. The gateway's health page lists the target as ready (its catalog fetched with the shared credential).
2. **Request and approve** one read permission for one agent under Review. The audit row's sentence names the lane and, the first time, the allow patterns; the agent restarts once and its header reads Updating, not Down.
3. **Ask the agent** in chat to do one read. The gateway log shows the lease request and the admitted call; the permission service's ledger shows the lease; the vendor's own audit trail shows the per-agent credential name.
4. **Revoke** and ask again: the refusal names Permissions.

Steps 3 and 4 are what the agent-level wire test (gate 9) runs as fixed-shape turns, plus three calls in one turn and a later turn after a pause; run it instead of asking in chat.

If step 3 fails with "no tool", check the agent's log for `[tools] offering:`; the vendor's tools missing there is gate 6 (allow) or gate 0a/0d (no gateway block). If the tools are offered and the call is refused, it is the gateway grant (gate 6, first half) or the lease (gates 1 to 3).
