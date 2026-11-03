"""Models come from the clouds the room connects (kobo#390): the providers,
the gateway filler, the catalogue and the per-agent key.

A provider is part of a cloud connection: a Google Cloud row (an organization
or a project) whose spec carries `models = {provider: "vertex_ai", project,
location}` serves Vertex AI from that project (kobo#405; an older project row
without `project` in `models` serves its own).
What the room offers is the enabled rows of `room_models`
(`kobo_api.room_models`, kobo#405): an admin ticks them on the Models pane,
and the org config's `models:` list seeds the first ticks with its price
names, families and tiers. Every function here that needs the offered set
takes it as `entries` (a list of `ModelChoice`, the same shape the org list
has); each entry whose id starts with a provider's prefix (`vertex_ai/...`)
is served by every connected provider of that kind, and a self-hosted
entry (`self-hosted/<row>/<name>`) by its own row. On the room's model gateway (a LiteLLM proxy the install deploys,
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

A Self-hosted models row (kobo#390, `kobo_api.self_hosted`) is the one
provider that is only models: its spec carries `base_url`, `options` and
`models = [{name, label, ok, reason}]` as the connect probe left them, and
it contributes one deployment per answering model, `model_name` `<row
slug>-<model slug>`, `model_info.id` `<row id>:<model name>`, LiteLLM's
`openai/` route at the endpoint with the row's key (read back from the
room's Secret) and its options as `extra_body`. The catalogue lists them
under the row's name; the org's `models:` list is untouched.

An AWS account row (kobo#390) serves Bedrock the same way Vertex rides a
project: its spec carries `models = {provider: "bedrock", region, role_arn}`
and it serves the org's `bedrock/...` entries. The gateway runs as the
room's identity and assumes the row's role itself (LiteLLM's
`aws_role_name` with a web identity token it fetches from the Google
metadata server on each use, `BEDROCK_TOKEN_REF`), so no credential is
stored anywhere in the room and nothing runs beside the gateway.

Every model the catalogue serves carries a `provider_tag` (GCP, AWS, Azure,
Self-hosted), the short word the picker shows after the name (kobo#405).

With the gateway off (`models_gateway_url` empty) nothing in this module is
reached: the catalogue route answers from `models:` as it always has.
"""

from __future__ import annotations

import logging
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from kobo_api import admin_store, delivery, self_hosted
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
PROVIDER_LABELS: dict[str, str] = {"vertex_ai": "Google Cloud", "bedrock": "AWS", self_hosted.VENDOR: self_hosted.LABEL}
PROVIDER_BEDROCK = "bedrock"
PROVIDER_AZURE = "azure"
# The short word after a model's name in the pickers (kobo#405): the cloud
# it comes from. Azure has no discovery yet; the pane shows it as not connected.
PROVIDER_TAGS: dict[str, str] = {"vertex_ai": "GCP", PROVIDER_BEDROCK: "AWS", PROVIDER_AZURE: "Azure", self_hosted.VENDOR: "Self-hosted"}
# How the gateway gets the Google identity token it trades for the Bedrock
# role's session: LiteLLM's `oidc/google/<audience>` reference makes it ask
# the metadata server itself, as the gateway's own identity, with audience
# `sts.amazonaws.com` (the `oaud` the role's trust policy pins). No file,
# no sidecar. The session name is what AWS logs the call under.
BEDROCK_TOKEN_REF = "oidc/google/sts.amazonaws.com"
BEDROCK_SESSION_NAME = "kobo-gateway"

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
    """One connected provider: the integration row that carries it. A Google
    Cloud provider has a `project` and `location`; an AWS one the `region`
    and the `role_arn` the gateway assumes; a self-hosted one has the
    endpoint's `base_url`, its `options`, the `models` the probe left on
    the row and, when the endpoint takes a key, the `credential_ref` the
    room reads it back from."""

    row_id: str
    name: str
    provider: str
    project: str = ""
    location: str = ""
    region: str = ""
    role_arn: str = ""
    base_url: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    models: tuple[dict[str, Any], ...] = ()
    credential_ref: str | None = None

    @property
    def self_hosted(self) -> bool:
        return self.provider == self_hosted.VENDOR

    @property
    def answering(self) -> list[dict[str, Any]]:
        """The self-hosted models the probe found answering; the failed ones
        stay on the row and never reach the gateway."""
        return [m for m in self.models if m.get("ok") and m.get("name")]


def providers_from_rows(rows: list[AdminIntegration]) -> list[Provider]:
    """The providers among the room's integration rows: a connected
    (`applied`) row whose spec carries `models`, as a cloud's `{provider,
    project, location}` or a self-hosted row's list. The Vertex AI project
    is `models.project` first (an organization row names it), then the row's
    own `project`. A failed row serves nothing, which is what its state
    already says."""
    out: list[Provider] = []
    for row in rows:
        if row.state != admin_store.STATE_APPLIED:
            continue
        spec = row.spec or {}
        models = spec.get("models")
        if spec.get("auth") == self_hosted.AUTH and isinstance(models, list):
            out.append(Provider(
                row_id=row.id, name=row.name, provider=self_hosted.VENDOR,
                base_url=str(spec.get("base_url") or ""), options=dict(spec.get("options") or {}),
                models=tuple(m for m in models if isinstance(m, dict)), credential_ref=row.credential_ref,
            ))
            continue
        if not isinstance(models, dict):
            continue
        provider = models.get("provider")
        if provider not in PROVIDER_LABELS or provider == self_hosted.VENDOR:
            continue
        out.append(Provider(
            row_id=row.id, name=row.name, provider=provider,
            project=str(models.get("project") or spec.get("project") or (spec.get("identity") or {}).get("project") or ""),
            location=str(models.get("location") or ""),
            region=str(models.get("region") or ""),
            role_arn=str(models.get("role_arn") or (spec.get("identity") or {}).get("role_arn") or ""),
        ))
    return out


def provider_of(entry_id: str) -> str:
    """The provider prefix of a catalogue entry id (`vertex_ai/x` -> `vertex_ai`)."""
    return entry_id.split("/", 1)[0] if "/" in entry_id else ""


def gateway_model_name(entry: ModelChoice) -> str:
    """The name agents ask the gateway for: the entry's label as a slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", entry.label.lower()).strip("-")
    return slug or re.sub(r"[^a-z0-9]+", "-", entry.id.lower()).strip("-")


def gateway_model_for(entries: list[ModelChoice], model_id: str) -> str:
    """The gateway name of the catalogue entry `model_id` among `entries`
    (every row the room knows, enabled or not: `room_models.all_entries`,
    so an agent on a model an admin has since hidden keeps its name). A
    self-hosted id (`self-hosted/<row>/<name>`) names its gateway model by
    itself. An id in no row (an agent created under an older catalogue) is
    named from the id itself, so a compose never fails on a stale row."""
    own = self_hosted.split_catalogue_id(model_id)
    if own is not None:
        return self_hosted.gateway_model_name(*own)
    for entry in entries:
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


def desired_models(entries: list[ModelChoice], providers: list[Provider], keys: dict[str, str | None] | None = None) -> list[Desired]:
    """One `Desired` per (provider, model). A cloud provider serves the
    enabled entries with its prefix; a self-hosted provider serves the
    models its probe found answering that are enabled too, with its key
    from `keys` (by row id; absent means the endpoint takes none)."""
    out: list[Desired] = []
    by_id = {entry.id: entry for entry in entries}
    for provider in providers:
        if provider.self_hosted:
            key = (keys or {}).get(provider.row_id)
            for model in provider.answering:
                name = str(model["name"])
                entry = by_id.get(self_hosted.catalogue_id(provider.row_id, name))
                if entry is None:
                    continue
                out.append(Desired(
                    gateway_id=f"{provider.row_id}:{name}", model_name=self_hosted.gateway_model_name(provider.row_id, name),
                    litellm_params=self_hosted.litellm_params(
                        model_name=name, base_url=provider.base_url, api_key=key,
                        options={} if model.get("without_options") else provider.options),
                    entry=entry, provider=provider,
                ))
            continue
        for entry in entries:
            if provider_of(entry.id) != provider.provider:
                continue
            params: dict[str, Any] = {"model": entry.id}
            if provider.provider == "vertex_ai":
                params["vertex_project"] = provider.project
                # A per-entry location (a preview served only on `global`)
                # wins over the provider's default, as it does for the
                # per-agent env today (`config.vertex_location_for`).
                params["vertex_location"] = entry.location or provider.location
            elif provider.provider == PROVIDER_BEDROCK:
                params.update(bedrock_params(provider))
            out.append(Desired(
                gateway_id=f"{provider.row_id}:{entry.id}", model_name=gateway_model_name(entry),
                litellm_params=params, entry=entry, provider=provider,
            ))
    return out


def bedrock_params(provider: Provider) -> dict[str, Any]:
    """How the gateway reaches an AWS account's Bedrock: it assumes the row's
    role with the identity token the metadata server hands it (`BEDROCK_TOKEN_REF`),
    in the row's region. No key, nothing stored."""
    return {
        "aws_region_name": provider.region,
        "aws_role_name": provider.role_arn,
        "aws_web_identity_token": BEDROCK_TOKEN_REF,
        "aws_session_name": BEDROCK_SESSION_NAME,
    }


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


KeyReader = Callable[[Provider], str | None]


def sync_gateway(gateway: ModelsGatewayClient, entries: list[ModelChoice], rows: list[AdminIntegration],
                 *, key_reader: KeyReader | None = None) -> SyncResult:
    """Make the gateway hold exactly the desired set (the enabled `entries`
    every connected provider serves): add what is missing,
    delete what the room wrote and no longer wants, leave the rest. One
    failed call is recorded and the others still run, so a gateway that
    refuses one model does not keep the others off. A self-hosted row's key
    comes through `key_reader`; a row whose key cannot be read is left as
    it is on the gateway (nothing added, nothing deleted) with the failure
    recorded, never registered with the wrong key."""
    result = SyncResult()
    providers = providers_from_rows(rows)
    keys: dict[str, str | None] = {}
    skipped: set[str] = set()
    for provider in providers:
        if not provider.self_hosted or not provider.credential_ref:
            continue
        try:
            keys[provider.row_id] = key_reader(provider) if key_reader is not None else None
            if keys[provider.row_id] is None:
                raise ClientError("this room has no reader for its Secrets")
        except ClientError as exc:
            skipped.add(provider.row_id)
            result.failure = f"the key for {provider.name} could not be read: {exc}"
    desired = {d.gateway_id: d for d in desired_models(entries, [p for p in providers if p.row_id not in skipped], keys)}
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
        if gateway_id.split(":", 1)[0] in skipped:
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
    # Local import: room_models reads this module's providers; the table is
    # read here, where the session is.
    from kobo_api import room_models

    session.flush()
    rows = admin_store.list_integrations(session)
    entries = room_models.enabled_entries(session, config, rows)
    kubectl = getattr(clients, "kubectl", None) if clients is not None else None

    def read_key(provider: Provider) -> str | None:
        return self_hosted.read_key(kubectl, provider.credential_ref)

    try:
        result = sync_gateway(gateway, entries, rows, key_reader=read_key)
    except Exception as exc:  # noqa: BLE001 - the filler must never fail a connect or a boot
        log.warning("model gateway: the sync did not run: %s", exc)
        return SyncResult(failure=f"the model gateway sync did not run: {exc}")
    (log.warning if result.failure else log.info)("model gateway: %s", result.sentence())
    return result


# --- the catalogue -----------------------------------------------------------


def _reason(entry: ModelChoice, providers: list[Provider], ready: bool) -> str | None:
    kind = provider_of(entry.id)
    if not any(p.provider == kind for p in providers):
        cloud = PROVIDER_LABELS.get(kind, kind or "a cloud")
        return REASON_NOT_CONNECTED.format(cloud=f"{cloud} with Bedrock" if kind == PROVIDER_BEDROCK else cloud)
    if not ready:
        return REASON_GATEWAY_DOWN
    return None


def _provider_label(kind: str, providers: list[Provider]) -> str:
    """What the picker calls the cloud behind an entry. Bedrock is per
    account: with one AWS account serving it the label is the cloud's, with
    several it names them, so a person sees which accounts answer."""
    label = PROVIDER_LABELS.get(kind, kind)
    if kind == PROVIDER_BEDROCK:
        names = [p.name for p in providers if p.provider == kind]
        if len(names) > 1:
            return f"{label} ({', '.join(names)})"
    return label


def catalogue(entries: list[ModelChoice], rows: list[AdminIntegration], gateway: ModelsGatewayClient | None) -> dict[str, Any]:
    """`GET /api/catalog/models` with the gateway on: every enabled entry
    (`room_models.enabled_entries`), each saying which cloud serves it
    (`provider_label`, and the short `provider_tag` the picker shows), what
    the gateway calls it, and whether it can be picked right now (and why
    not). A self-hosted entry is offered only while its row is connected and
    its probe found the model answering; a model the probe marked failed
    stays on the Integrations row."""
    providers = providers_from_rows(rows)
    ready = bool(gateway is not None and gateway.ready())
    answering: dict[str, Provider] = {}
    for provider in providers:
        if provider.self_hosted:
            for model in provider.answering:
                answering[self_hosted.catalogue_id(provider.row_id, str(model["name"]))] = provider
    models = []
    for entry in entries:
        kind = provider_of(entry.id)
        if kind == self_hosted.VENDOR:
            provider = answering.get(entry.id)
            if provider is None:
                continue
            own = self_hosted.split_catalogue_id(entry.id)
            models.append({
                "id": entry.id, "label": entry.label, "family": None, "tier": None,
                "provider": self_hosted.VENDOR, "provider_label": provider.name, "provider_tag": PROVIDER_TAGS[self_hosted.VENDOR],
                "gateway_model": self_hosted.gateway_model_name(*own) if own else gateway_model_name(entry),
                "available": ready, "reason": None if ready else REASON_GATEWAY_DOWN,
            })
            continue
        reason = _reason(entry, providers, ready)
        models.append({
            "id": entry.id, "label": entry.label, "family": entry.family, "tier": entry.tier,
            "provider": kind, "provider_label": _provider_label(kind, providers), "provider_tag": PROVIDER_TAGS.get(kind, kind),
            "gateway_model": gateway_model_name(entry),
            "available": reason is None, "reason": reason,
        })
    default = next((m["id"] for m in models if m["available"]), None)
    return {"models": models, "default": default, "gateway": True}


def availability_in(view: dict[str, Any], model_id: str) -> tuple[bool, str]:
    """`availability` over a catalogue already in hand."""
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


def availability(entries: list[ModelChoice], rows: list[AdminIntegration], gateway: ModelsGatewayClient | None, model_id: str) -> tuple[bool, str]:
    """Whether `model_id` may be picked right now, and the sentence for a
    refusal: the no-model sentence when nothing is available at all, else
    the model's own reason."""
    return availability_in(catalogue(entries, rows, gateway), model_id)


def available_gateway_models(entries: list[ModelChoice], rows: list[AdminIntegration]) -> list[str]:
    """The gateway names a new key may call: every enabled entry some
    connected provider serves. The readiness of the gateway is not asked
    here; a key is minted for what the room offers, not for what answers
    this second."""
    providers = providers_from_rows(rows)
    names: list[str] = []
    for want in desired_models(entries, providers):
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
    from kobo_api import room_models

    gateway = getattr(clients, "models_gateway", None) if clients is not None else None
    rows = admin_store.list_integrations(session)
    view = catalogue(room_models.enabled_entries(session, config, rows), rows, gateway)
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
    drops an `AGENT_MODEL`, `VERTEXAI_PROJECT` or `VERTEXAI_LOCATION` left by
    the direct-Vertex path (a gateway agent carries none of the three); then
    one reprovision so the deploy contract regenerates. Returns how it went:
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
            if isinstance(env, dict):
                for key in ("AGENT_MODEL", "VERTEXAI_PROJECT", "VERTEXAI_LOCATION"):
                    if key in env:
                        env.pop(key)
                        touched = True
            if touched:
                gojira_yaml.write_text(yaml.safe_dump(data, sort_keys=False))
                changed = True
        pushed = delivery.push(github, workdir, f"kobo: model is now {gateway_model} through the room's model gateway") if changed else False
    if gojira is not None and gojira_app_id is not None:
        gojira.reprovision(gojira_app_id)
    return "committed" if pushed else "unchanged"
