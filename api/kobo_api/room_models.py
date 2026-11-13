"""Which models the room offers (kobo#405): the `room_models` table, its seed,
model discovery through the permission service, and the Models pane's view.

An admin chooses what New agent shows. Each connected provider's models are
discovered (Vertex AI and Bedrock through the permission service's
`GET /integrations/{id}/models`, a self-hosted row from its own probe) and
listed on the Models pane; a tick makes a model offered, and only offered
models reach the catalogue, the gateway filler and a new agent's key
(`kobo_api.model_providers` takes the enabled set as `entries`).

The table starts empty. At boot, when it is empty, the org config's
`models:` list seeds it enabled with its price names, families and tiers,
and so do the answering models of any self-hosted row already connected,
so nothing disappears from New agent on the roll that brings the table.
After that the org list is only a hint the pane shows, and the table is
the truth: every tick is a row, every change an audit row (`models.offered`,
`models.withdrawn`), and the gateway filler runs after each change.

A cloud's listing is the slow part (two to eight seconds on a real room,
all of it the cloud's own answer), so it is kept per provider in this
process: fresh for ten minutes, and after that still held, so the pane can
open at once on what the room last read (`LISTING_HELD`) while it asks for
each provider's listing on its own. Providers are listed side by side, never
one after another. A provider the service cannot list shows its sentence on
the pane with the listing the room last held, or its table rows alone.
Azure has no discovery yet: the pane shows it as not connected.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from kobo_api import admin_store, audit, self_hosted
from kobo_api.clients import ClientError, Clients
from kobo_api.config import Config, ModelChoice
from kobo_api.model_providers import (
    PROVIDER_BEDROCK,
    PROVIDER_TAGS,
    Provider,
    gateway_on,
    provider_of,
    providers_from_rows,
    sync_from_session,
)
from kobo_api.models import AdminIntegration, RoomModel

log = logging.getLogger(__name__)

DISCOVERY_TTL_S = 600.0
# How hard a read asks a cloud for its listing (`GET /api/admin/models?listing=`).
LISTING_LIVE = "live"  # ask when none is held or the held one is older than DISCOVERY_TTL_S
LISTING_ANY = "any"  # ask only when none is held: a tick never waits on a listing the room has
LISTING_HELD = "held"  # never ask: what this process holds, at once
# What a caller of the route may name; a tick uses LISTING_ANY itself.
LISTINGS = (LISTING_LIVE, LISTING_HELD)
# Listings asked for side by side; a room holds a handful of providers.
DISCOVERY_WORKERS = 8
PROVIDER_VERTEX = "vertex_ai"
DISCOVERED_PROVIDERS = frozenset({PROVIDER_VERTEX, PROVIDER_BEDROCK})

ACTION_OFFERED = "models.offered"
ACTION_WITHDRAWN = "models.withdrawn"
TARGET_KIND = "model"
# The pane's one sentence when no provider is connected.
EMPTY_STATE = "Connect a cloud or a self-hosted endpoint under Integrations; its models appear here."

_lock = threading.Lock()
# (row id, location or region) -> (fresh until, models, fetched_at)
_discovery_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]], str | None]] = {}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# --- ids ----------------------------------------------------------------------


def catalogue_id(provider: str, model: str, source: str | None = None) -> str:
    """The id an agent's `model_id` carries: `vertex_ai/<model>`,
    `bedrock/<id>`, `self-hosted/<row>/<name>`."""
    if provider == self_hosted.VENDOR:
        return self_hosted.catalogue_id(source or "", model)
    return f"{provider}/{model}"


def split_id(model_id: str) -> tuple[str, str | None, str]:
    """(provider, source row or None, the provider's own model id)."""
    own = self_hosted.split_catalogue_id(model_id)
    if own is not None:
        return self_hosted.VENDOR, own[0], own[1]
    provider, _, model = model_id.partition("/")
    return (provider, None, model) if model else ("", None, model_id)


# --- the table ---------------------------------------------------------------


def as_entry(row: RoomModel) -> ModelChoice:
    return ModelChoice(id=row.id, label=row.label, family=row.family, tier=row.tier, location=row.location)


def list_rows(session: Session) -> list[RoomModel]:
    return list(session.query(RoomModel).order_by(RoomModel.added_at, RoomModel.id).all())


def all_entries(session: Session) -> list[ModelChoice]:
    """Every row, enabled or not: what `gateway_model_for` names an agent's
    model from, so a hidden model keeps its gateway name."""
    return [as_entry(r) for r in list_rows(session)]


def known_entries(session: Session, config: Config) -> list[ModelChoice]:
    """Every model the room has ever known, enabled or not, for naming: an
    agent card keeps its model's label after an untick. The org list only
    when the table is still empty (a room before its first read)."""
    return all_entries(session) or list(config.models)


def enabled_entries(session: Session, config: Config, rows: list[AdminIntegration] | None = None) -> list[ModelChoice]:
    """The offered set, seeded first when the table is empty."""
    seed(session, config, rows)
    return [as_entry(r) for r in list_rows(session) if r.enabled]


def seed(session: Session, config: Config, rows: list[AdminIntegration] | None = None, *, actor: str = audit.ROOM_ACTOR) -> int:
    """When the table is empty: the org list enabled, and the answering
    models of every connected self-hosted row enabled. Returns how many
    rows were added; 0 when the table already had any."""
    if session.query(RoomModel.id).first() is not None:
        return 0
    # Rows list in `added_at` order, so the seed stamps them a microsecond
    # apart: the org list's order (its first entry is New agent's default)
    # holds, and the endpoints' models follow it.
    start = _now()
    added = 0
    seen: set[str] = set()

    def stamp() -> datetime:
        return start + timedelta(microseconds=added)

    for entry in config.models:
        provider = provider_of(entry.id) or "other"
        if entry.id in seen:
            continue
        seen.add(entry.id)
        session.add(RoomModel(
            id=entry.id, provider=provider, source=None, model=split_id(entry.id)[2], label=entry.label,
            location=entry.location, enabled=True, tier=entry.tier, family=entry.family,
            added_by=actor, added_at=stamp(), updated_at=stamp(),
        ))
        added += 1
    if rows is None:
        rows = admin_store.list_integrations(session)
    for provider in providers_from_rows(rows):
        if not provider.self_hosted:
            continue
        for model in provider.answering:
            name = str(model["name"])
            model_id = self_hosted.catalogue_id(provider.row_id, name)
            if model_id in seen:
                continue
            seen.add(model_id)
            session.add(RoomModel(
                id=model_id, provider=self_hosted.VENDOR, source=provider.row_id, model=name,
                label=str(model.get("label") or name), enabled=True, added_by=actor, added_at=stamp(), updated_at=stamp(),
            ))
            added += 1
    session.flush()
    log.info("room models: seeded %d model(s) from the org list and the connected endpoints", added)
    return added


# --- discovery ---------------------------------------------------------------


def clear_discovery_cache() -> None:
    with _lock:
        _discovery_cache.clear()


def _cloud_items(provider: Provider, models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in models:
        model = str(m.get("id") or "")
        if not model:
            continue
        out.append({
            "id": catalogue_id(provider.provider, model), "model": model, "label": str(m.get("label") or model),
            "publisher": m.get("publisher"), "family": m.get("family"), "stage": m.get("stage"), "via": m.get("via"),
        })
    return out


def discover(bab, provider: Provider, actor: str, *, listing: str = LISTING_LIVE,
             ) -> tuple[list[dict[str, Any]], str | None, str | None, bool]:
    """The provider's models as the cloud lists them: (models, fetched_at,
    error, pending). A self-hosted row answers from its probe; a cloud
    through the permission service, kept per (row, place): fresh for
    `DISCOVERY_TTL_S`, held after that. `listing` says when the cloud is
    asked (`LISTING_LIVE`, `LISTING_ANY`, `LISTING_HELD`); `pending` is true
    when the answer is not a fresh listing and nothing was asked, so the
    caller knows one is owed. An error is the service's sentence and is not
    kept: it comes with the listing the room last held, when it holds one."""
    if provider.self_hosted:
        items = [{
            "id": self_hosted.catalogue_id(provider.row_id, str(m["name"])), "model": str(m["name"]),
            "label": str(m.get("label") or m["name"]), "publisher": None, "family": None, "stage": None, "via": None,
        } for m in provider.answering]
        return items, None, None, False
    if provider.provider not in DISCOVERED_PROVIDERS:
        return [], None, None, False
    if bab is None:
        return [], None, "The permission service is off for this room, so this cloud's models cannot be listed.", False
    place = provider.location if provider.provider == PROVIDER_VERTEX else provider.region
    key = (provider.row_id, place)
    now = time.monotonic()
    with _lock:
        held = _discovery_cache.get(key)
    if held is not None and held[0] > now:
        return held[1], held[2], None, False
    if listing == LISTING_HELD or (listing == LISTING_ANY and held is not None):
        return (held[1], held[2], None, True) if held is not None else ([], None, None, True)
    try:
        if provider.provider == PROVIDER_VERTEX:
            body = bab.models(provider.row_id, actor, location=place or None)
        else:
            body = bab.models(provider.row_id, actor, region=place or None)
    except ClientError as exc:
        return (held[1], held[2], str(exc), False) if held is not None else ([], None, str(exc), False)
    items = _cloud_items(provider, list(body.get("models") or []))
    fetched_at = body.get("fetched_at")
    with _lock:
        _discovery_cache[key] = (now + DISCOVERY_TTL_S, items, fetched_at)
    return items, fetched_at, None, False


def discover_all(bab, providers: list[Provider], actor: str, *, listing: str = LISTING_LIVE,
                 ) -> list[tuple[list[dict[str, Any]], str | None, str | None, bool]]:
    """`discover` for each provider, in their order, the clouds asked side by
    side: the wait is the slowest cloud's, not the sum of them."""
    if listing == LISTING_HELD or len(providers) < 2:
        return [discover(bab, p, actor, listing=listing) for p in providers]
    with ThreadPoolExecutor(max_workers=min(DISCOVERY_WORKERS, len(providers)), thread_name_prefix="model-listing") as pool:
        return list(pool.map(lambda p: discover(bab, p, actor, listing=listing), providers))


# --- the pane's view ---------------------------------------------------------


def _belongs(row: RoomModel, provider: Provider) -> bool:
    if provider.self_hosted:
        return row.provider == self_hosted.VENDOR and split_id(row.id)[1] == provider.row_id
    return row.provider == provider.provider


def _place(provider: Provider) -> str:
    if provider.self_hosted:
        return self_hosted.host_of(provider.base_url)
    return provider.location or provider.region or ""


def read_table(session: Session, config: Config, rows: list[AdminIntegration]) -> dict[str, RoomModel]:
    """The room's ticks by id, seeded first when the table is empty."""
    enabled_entries(session, config, rows)
    return {r.id: r for r in list_rows(session)}


def build_view(config: Config, rows: list[AdminIntegration], table: dict[str, RoomModel], bab, actor: str, *,
               listing: str = LISTING_LIVE, source: str | None = None) -> dict[str, Any]:
    """The Models pane's view from the ticks already read: one section per
    connected provider (only `source`'s when one is named) with the models
    the cloud lists merged with the table (`enabled`), plus the table rows of
    that kind the cloud does not list now (`discovered: false`), so a seeded
    or retired model can still be unticked. `name` is the cloud's own name
    for a model and `label` the room's. A section is `pending` when its
    listing was not asked for and is owed (see `discover`). Touches no
    database session, so a route can hand its session back before the
    clouds are asked."""
    providers = providers_from_rows(rows)
    shown = [p for p in providers if source is None or p.row_id == source]
    sections = []
    for provider, (items, fetched_at, error, pending) in zip(
            shown, discover_all(bab, shown, actor, listing=listing), strict=True):
        seen: set[str] = set()
        discovered: list[dict[str, Any]] = []
        for item in items:
            row = table.get(item["id"])
            seen.add(item["id"])
            discovered.append({
                **item, "source": provider.row_id, "discovered": True, "name": item["label"],
                "enabled": bool(row is not None and row.enabled),
                "label": row.label if row is not None else item["label"],
                "tier": row.tier if row is not None else None,
                "family": row.family if row is not None and row.family else item.get("family"),
            })
        for row in table.values():
            if row.id in seen or not _belongs(row, provider):
                continue
            discovered.append({
                "id": row.id, "model": row.model, "label": row.label, "name": row.label,
                "publisher": None, "via": None, "stage": None,
                "family": row.family, "tier": row.tier, "source": provider.row_id,
                "enabled": row.enabled, "discovered": False,
            })
        sections.append({
            "provider": provider.provider, "tag": PROVIDER_TAGS.get(provider.provider, provider.provider),
            "source": provider.row_id, "name": provider.name, "region_or_location": _place(provider),
            "discovered": discovered, "offered": [m["id"] for m in discovered if m["enabled"]],
            "error": error, "fetched_at": fetched_at, "pending": pending,
        })
    connected = {p.provider for p in providers}
    return {
        "gateway": gateway_on(config),
        "providers": sections,
        "not_connected": [{"provider": k, "tag": t} for k, t in PROVIDER_TAGS.items() if k not in connected],
        "org_list": [{"id": m.id, "label": m.label, "tier": m.tier, "family": m.family} for m in config.models],
        "empty_state": EMPTY_STATE,
    }


def admin_view(session: Session, config: Config, rows: list[AdminIntegration], bab, actor: str, *,
               listing: str = LISTING_LIVE, source: str | None = None) -> dict[str, Any]:
    """`GET /api/admin/models` in one call: the ticks read, then the view built."""
    return build_view(config, rows, read_table(session, config, rows), bab, actor, listing=listing, source=source)


# --- changes -----------------------------------------------------------------


class UnknownModel(ValueError):
    """An id that names no provider the room knows."""


def apply_changes(session: Session, config: Config, clients: Clients | None, changes: list[dict[str, Any]], *,
                  bab, actor: str, request: Request | None = None) -> dict[str, Any]:
    """`PUT /api/admin/models`: upsert each `{id, enabled, label?}`, one
    audit row per change that changed something, then the gateway filler
    (enabled rows only). Answers the refreshed view with `changed` and the
    filler's sentence. Raises `UnknownModel` for an id of no known kind."""
    rows = admin_store.list_integrations(session)
    # A tick names a model the pane already listed, so a listing the room
    # holds is enough at any age; a cloud is asked only when none is held.
    view = admin_view(session, config, rows, bab, actor, listing=LISTING_ANY)
    found = {m["id"]: m for section in view["providers"] for m in section["discovered"]}
    who = audit.who(request) if request is not None else actor
    now = _now()
    changed: list[str] = []
    audit_rows = []
    for change in changes:
        model_id = str(change.get("id") or "").strip()
        provider, source, model = split_id(model_id)
        if not model_id or provider not in PROVIDER_TAGS:
            raise UnknownModel(f"{model_id!r} is not a model id the room knows")
        enabled = bool(change.get("enabled"))
        label = (change.get("label") or "").strip() or None
        info = found.get(model_id, {})
        row = session.get(RoomModel, model_id)
        if row is None:
            if not enabled and label is None:
                continue  # hiding a model the room never offered changes nothing
            row = RoomModel(
                id=model_id, provider=provider, source=info.get("source") or source, model=model,
                label=label or str(info.get("label") or model), enabled=enabled,
                added_by=actor, added_at=now, updated_at=now,
            )
            session.add(row)
        else:
            if row.enabled == enabled and (label is None or label == row.label):
                continue
            if label is not None:
                row.label = label
            row.enabled = enabled
            row.source = row.source or info.get("source") or source
            row.updated_at = now
        tag = PROVIDER_TAGS[provider]
        action = ACTION_OFFERED if enabled else ACTION_WITHDRAWN
        verb = "offered" if enabled else "hid"
        where = "in New agent" if enabled else "from New agent"
        audit_rows.append(audit.record(
            session, request=request, actor=actor if request is None else None, action=action,
            target_kind=TARGET_KIND, target_id=model_id,
            summary=f"{who} {verb} {row.label} ({tag}) {where}",
            detail={"provider": provider, "model": model, "source": row.source, "enabled": enabled, "label": row.label},
            landed=audit.LANDED_PENDING,
        ))
        changed.append(model_id)
    session.flush()
    result = sync_from_session(session, clients, config) if changed else None
    sentence = result.sentence() if result is not None else None
    for entry in audit_rows:
        if result is None:
            entry.landed = audit.LANDED_APPLIED
            entry.landed_detail = "stored; the model gateway is off"
        else:
            entry.landed = audit.LANDED_APPLIED if result.ok else audit.LANDED_FAILED
            entry.landed_detail = sentence
    # The cloud's listing has not changed with the ticks; the cache stays.
    fresh = admin_view(session, config, rows, bab, actor, listing=LISTING_ANY)
    return {**fresh, "changed": changed, "gateway_result": sentence}
