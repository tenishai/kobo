"""Models come from the clouds the room connects (kobo#390): the providers,
the gateway filler, the catalogue and the per-agent key.

A provider is part of a cloud connection: a Google Cloud project row whose
spec carries `models = {provider: "vertex_ai", location}` serves Vertex AI.
The org config's `models:` list stays the price-named catalogue the room
offers (labels, families, tiers); each entry whose id starts with a
provider's prefix (`vertex_ai/...`) is served by every connected provider of
that kind. On the room's model gateway (a LiteLLM proxy the install deploys,
`Config.models_gateway_url`) each pair is one deployment: `model_name` is
the entry's label as a slug (`claude-affordable-sonnet`, what agents ask
for; two providers serving the same entry share the name, and the gateway
balances between them) and `model_info.id` is `<provider row id>:<entry
id>`, so a provider's removal deletes exactly its models.

The filler (`sync_gateway`) is idempotent: it diffs the gateway's `GET
/model/info` against the desired set and adds and deletes the difference,
at connect, at disconnect and at boot. A gateway that fails a write leaves
the provider connected, with the row's `state_detail` saying the gateway did
not take its models.

With the gateway off (`models_gateway_url` empty) nothing in this module is
reached: the catalogue route answers from `models:` as it always has.
"""

from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from kobo_api import admin_store
from kobo_api.clients import (
    ClientError,
    Clients,
    GitHubClient,
    GojiraClient,
    ModelsGatewayClient,
)
from kobo_api.composer import (
    MODELS_GATEWAY_KEY_ENV,
    gateway_model_block,
    update_agent_yaml_model,
)
from kobo_api.config import Config, ModelChoice
from kobo_api.models import AdminIntegration

log = logging.getLogger(__name__)

# The provider kinds the room knows, keyed by the prefix of a catalogue
# entry's id, and what the picker calls the cloud behind each.
PROVIDER_LABELS: dict[str, str] = {"vertex_ai": "Google Cloud"}

# What the picker says under a model nobody serves, and under every model
# when the gateway itself does not answer.
REASON_NOT_CONNECTED = "Ask a kobo admin to connect {cloud}"
REASON_GATEWAY_DOWN = "The model gateway is not reachable"
# New agent's one sentence when no model is available; the create route
# refuses with it too.
NO_MODEL_AVAILABLE = "Connect a cloud with models under Admin, Integrations, before creating an agent."

# The per-agent key: the Secret it arrives in, the key alias it is filed
# under on the gateway, and the secrets-lane purpose (`apps/<agent>/models`).
MODELS_SECRET_PURPOSE = "models"


def models_secret_name(agent_name: str) -> str:
    return f"{agent_name}-{MODELS_SECRET_PURPOSE}"


def key_alias(agent_name: str) -> str:
    return f"agent:{agent_name}"


def gateway_on(config: Config) -> bool:
    return bool(config.models_gateway_url)


@dataclass(frozen=True)
class Provider:
    """One connected provider: the integration row that carries it."""

    row_id: str
    name: str
    provider: str
    project: str
    location: str


def providers_from_rows(rows: list[AdminIntegration]) -> list[Provider]:
    """The providers among the room's integration rows: a connected
    (`applied`) row whose spec carries `models`. A failed row serves
    nothing, which is what its state already says."""
    out: list[Provider] = []
    for row in rows:
        if row.state != admin_store.STATE_APPLIED:
            continue
        spec = row.spec or {}
        models = spec.get("models") or {}
        provider = models.get("provider")
        if provider not in PROVIDER_LABELS:
            continue
        out.append(Provider(
            row_id=row.id, name=row.name, provider=provider,
            project=str(spec.get("project") or ""), location=str(models.get("location") or ""),
        ))
    return out


def provider_of(entry_id: str) -> str:
    """The provider prefix of a catalogue entry id (`vertex_ai/x` -> `vertex_ai`)."""
    return entry_id.split("/", 1)[0] if "/" in entry_id else ""


def gateway_model_name(entry: ModelChoice) -> str:
    """The name agents ask the gateway for: the entry's label as a slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", entry.label.lower()).strip("-")
    return slug or re.sub(r"[^a-z0-9]+", "-", entry.id.lower()).strip("-")


def gateway_model_for(config: Config, model_id: str) -> str:
    """The gateway name of the catalogue entry `model_id`. An id no longer
    in the list (an agent created under an older catalogue) is named from
    the id itself, so a compose never fails on a stale row."""
    for entry in config.models:
        if entry.id == model_id:
            return gateway_model_name(entry)
    return gateway_model_name(ModelChoice(id=model_id, label=model_id.split("/", 1)[-1]))


@dataclass(frozen=True)
class Desired:
    """One deployment the gateway should hold."""

    gateway_id: str
    model_name: str
    litellm_params: dict[str, Any]
    entry: ModelChoice
    provider: Provider


def desired_models(config: Config, providers: list[Provider]) -> list[Desired]:
    out: list[Desired] = []
    for provider in providers:
        for entry in config.models:
            if provider_of(entry.id) != provider.provider:
                continue
            params: dict[str, Any] = {"model": entry.id}
            if provider.provider == "vertex_ai":
                params["vertex_project"] = provider.project
                # A per-entry location (a preview served only on `global`)
                # wins over the provider's default, as it does for the
                # per-agent env today (`config.vertex_location_for`).
                params["vertex_location"] = entry.location or provider.location
            out.append(Desired(
                gateway_id=f"{provider.row_id}:{entry.id}", model_name=gateway_model_name(entry),
                litellm_params=params, entry=entry, provider=provider,
            ))
    return out


def _ours(model_id: str) -> bool:
    """Whether a gateway deployment id is one the room wrote (`<row>:<entry>`).
    The gateway's own ids are uuids with no colon, and a deployment someone
    added by hand is never the room's to delete."""
    return ":" in model_id


@dataclass
class SyncResult:
    added: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    kept: int = 0
    # The gateway's refusal, when one write (or the listing) failed. The rest
    # of the diff still ran.
    failure: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None

    def sentence(self) -> str:
        parts = [f"{len(self.added)} added", f"{len(self.deleted)} removed", f"{self.kept} kept"]
        text = ", ".join(parts)
        return f"{text}; {self.failure}" if self.failure else text


def sync_gateway(gateway: ModelsGatewayClient, config: Config, rows: list[AdminIntegration]) -> SyncResult:
    """Make the gateway hold exactly the desired set: add what is missing,
    delete what the room wrote and no longer wants, leave the rest. One
    failed call is recorded and the others still run, so a gateway that
    refuses one model does not keep the others off."""
    result = SyncResult()
    desired = {d.gateway_id: d for d in desired_models(config, providers_from_rows(rows))}
    try:
        current = gateway.list_models()
    except ClientError as exc:
        result.failure = f"the model gateway did not answer: {exc}"
        return result
    current_ids = {str((m.get("model_info") or {}).get("id") or "") for m in current}
    for gateway_id, want in desired.items():
        if gateway_id in current_ids:
            result.kept += 1
            continue
        try:
            gateway.add_model(
                model_name=want.model_name, model_id=gateway_id, litellm_params=want.litellm_params,
                model_info={"provider": want.provider.provider, "integration": want.provider.row_id, "label": want.entry.label},
            )
            result.added.append(gateway_id)
        except ClientError as exc:
            result.failure = f"the model gateway did not take its models: {exc}"
    for gateway_id in sorted(current_ids):
        if not gateway_id or gateway_id in desired or not _ours(gateway_id):
            continue
        try:
            gateway.delete_model(gateway_id)
            result.deleted.append(gateway_id)
        except ClientError as exc:
            result.failure = f"the model gateway kept a model the room removed: {exc}"
    return result


def sync_from_session(session: Session, clients: Clients | None, config: Config) -> SyncResult | None:
    """The filler as the routes and boot call it: reads the rows, writes the
    gateway, logs one line. None when the gateway is off or the room holds
    no client for it. Never raises."""
    gateway = getattr(clients, "models_gateway", None) if clients is not None else None
    if not gateway_on(config) or gateway is None:
        return None
    session.flush()
    rows = admin_store.list_integrations(session)
    try:
        result = sync_gateway(gateway, config, rows)
    except Exception as exc:  # noqa: BLE001 - the filler must never fail a connect or a boot
        log.warning("model gateway: the sync did not run: %s", exc)
        return SyncResult(failure=f"the model gateway sync did not run: {exc}")
    (log.warning if result.failure else log.info)("model gateway: %s", result.sentence())
    return result


# --- the catalogue -----------------------------------------------------------


def _reason(entry: ModelChoice, providers: list[Provider], ready: bool) -> str | None:
    kind = provider_of(entry.id)
    if not any(p.provider == kind for p in providers):
        return REASON_NOT_CONNECTED.format(cloud=PROVIDER_LABELS.get(kind, kind or "a cloud"))
    if not ready:
        return REASON_GATEWAY_DOWN
    return None


def catalogue(config: Config, rows: list[AdminIntegration], gateway: ModelsGatewayClient | None) -> dict[str, Any]:
    """`GET /api/catalog/models` with the gateway on: every entry of the
    org's list, each saying which cloud serves it, what the gateway calls
    it, and whether it can be picked right now (and why not)."""
    providers = providers_from_rows(rows)
    ready = bool(gateway is not None and gateway.ready())
    models = []
    for entry in config.models:
        kind = provider_of(entry.id)
        reason = _reason(entry, providers, ready)
        models.append({
            "id": entry.id, "label": entry.label, "family": entry.family, "tier": entry.tier,
            "provider": kind, "provider_label": PROVIDER_LABELS.get(kind, kind),
            "gateway_model": gateway_model_name(entry),
            "available": reason is None, "reason": reason,
        })
    default = next((m["id"] for m in models if m["available"]), None)
    return {"models": models, "default": default, "gateway": True}


def availability(config: Config, rows: list[AdminIntegration], gateway: ModelsGatewayClient | None, model_id: str) -> tuple[bool, str]:
    """Whether `model_id` may be picked right now, and the sentence for a
    refusal: the no-model sentence when nothing is available at all, else
    the model's own reason."""
    view = catalogue(config, rows, gateway)
    entry = next((m for m in view["models"] if m["id"] == model_id), None)
    if entry is None:
        return False, f"there is no model {model_id!r} in the room's list"
    if entry["available"]:
        return True, ""
    # Nothing connected serves anything: the sentence names the page to go
    # to. A gateway that does not answer, or one model whose cloud is not
    # connected while others are, is the model's own reason.
    if view["default"] is None and entry["reason"] != REASON_GATEWAY_DOWN:
        return False, NO_MODEL_AVAILABLE
    return False, f"{entry['label']} is not available right now: {entry['reason']}"


def available_gateway_models(config: Config, rows: list[AdminIntegration]) -> list[str]:
    """The gateway names a new key may call: every entry some connected
    provider serves. The readiness of the gateway is not asked here; a key
    is minted for what the room offers, not for what answers this second."""
    providers = providers_from_rows(rows)
    names: list[str] = []
    for want in desired_models(config, providers):
        if want.model_name not in names:
            names.append(want.model_name)
    return names


@dataclass(frozen=True)
class GatewayView:
    """What the Services row says: providers connected, models available,
    the default's label, and whether the gateway answers."""

    ready: bool
    providers: int
    models: int
    default_label: str | None


def gateway_view(session: Session, config: Config, clients: Clients | None) -> GatewayView:
    gateway = getattr(clients, "models_gateway", None) if clients is not None else None
    rows = admin_store.list_integrations(session)
    view = catalogue(config, rows, gateway)
    default = next((m for m in view["models"] if m["id"] == view["default"]), None)
    return GatewayView(
        ready=bool(gateway is not None and gateway.ready()),
        providers=len(providers_from_rows(rows)),
        models=sum(1 for m in view["models"] if m["available"]),
        default_label=default["label"] if default else None,
    )


# --- the agent's key ---------------------------------------------------------


def deliver_models_key(clients: Clients, config: Config, agent, *, models: list[str], acting_email: str | None) -> None:
    """Mint the agent's gateway key and deliver it into its namespace as the
    Secret `<agent>-models` under `MODELS_GATEWAY_KEY`, through the same lane
    the Slack values use: the secrets add-on when the room has one, else the
    direct Secret write. The earlier key under the same alias is revoked
    first, so a re-wire never leaves two live keys. The key itself is never
    logged and never appears in a raised message."""
    gateway = clients.models_gateway
    if gateway is None:
        raise ClientError("the room holds no client for the model gateway")
    alias = key_alias(agent.name)
    gateway.revoke_key(alias)
    key = gateway.mint_key(agent.name, models, alias=alias)
    secret = models_secret_name(agent.name)
    if getattr(clients, "sero", None) is not None:
        # Local import: kobo_api.sero imports ClientError from clients, and
        # this module is imported by the steps; same shape as deliver_pass.
        from kobo_api.sero import deliver_secret

        deliver_secret(
            clients.sero, acting_email=acting_email or agent.created_by, agent_name=agent.name,
            purpose=MODELS_SECRET_PURPOSE, values={MODELS_GATEWAY_KEY_ENV: key}, secret_name=secret,
        )
        return
    clients.kubectl.apply_secret(agent.name, secret, {MODELS_GATEWAY_KEY_ENV: key})


def revoke_models_key(clients: Clients | None, agent_name: str) -> bool:
    """Destroy's half: take the agent's key back. False when the gateway
    held none (an agent born before the gateway, or a resume)."""
    gateway = getattr(clients, "models_gateway", None) if clients is not None else None
    if gateway is None:
        return False
    return gateway.revoke_key(key_alias(agent_name))


def append_secrets_from(data: dict, entry: str) -> bool:
    """Append `entry` to gojira.yaml's `secrets_from` list once. The same
    rule `kobo_api.steps._append_secrets_from` keeps; stated here because
    the steps import this module."""
    secrets_from = data.get("secrets_from", [])
    if entry in secrets_from:
        return False
    secrets_from.append(entry)
    data["secrets_from"] = secrets_from
    return True


def commit_gateway_model(
    github: GitHubClient, gojira: GojiraClient | None, *, repo: str, gojira_app_id: int | None,
    agent_name: str, config: Config, gateway_model: str,
) -> str:
    """Change model with the gateway on, for a composed agent: one commit
    that rewrites `bundle/agent.yaml`'s model stanza to the gateway block,
    wires the `<agent>-models` Secret into gojira.yaml's `secrets_from` and
    drops an `AGENT_MODEL` left by an earlier Vertex change; then one
    reprovision so the deploy contract regenerates. Returns how it went:
    `committed` or `unchanged`. Raises `ClientError` on any refusal."""
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        changed = update_agent_yaml_model(
            workdir / "bundle", block=gateway_model_block(config.models_gateway_url, gateway_model)
        )
        gojira_yaml = workdir / "gojira.yaml"
        if gojira_yaml.exists():
            data = yaml.safe_load(gojira_yaml.read_text()) or {}
            touched = append_secrets_from(data, models_secret_name(agent_name))
            env = data.get("env")
            if isinstance(env, dict) and "AGENT_MODEL" in env:
                env.pop("AGENT_MODEL")
                touched = True
            if touched:
                gojira_yaml.write_text(yaml.safe_dump(data, sort_keys=False))
                changed = True
        pushed = github.commit_push(workdir, f"kobo: model is now {gateway_model} through the room's model gateway") if changed else False
    if gojira is not None and gojira_app_id is not None:
        gojira.reprovision(gojira_app_id)
    return "committed" if pushed else "unchanged"
