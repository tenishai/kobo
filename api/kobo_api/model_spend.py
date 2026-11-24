"""What each agent's model calls cost, read from the model gateway (issue #556).

Every agent calls the gateway with its own key, filed under the alias
`agent:<name>` (`model_providers.key_alias`). The gateway meters each call
and keeps the numbers by day under that alias, also after the key is rotated
or the agent is gone. This module turns those rows into what the room shows:
one agent's use by day and by model, and the room's use by agent.

The cost is the gateway's own estimate from its price list. It is not the
cloud bill, and the room says so wherever it prints one.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from kobo_api.clients import ModelsGatewayClient

WINDOWS = (7, 30, 90)
DEFAULT_WINDOW = 30
_AGENT_PREFIX = "agent:"


def window(days: int, today: date) -> tuple[str, str]:
    """The first and last day of a window that ends today, both included."""
    return (today - timedelta(days=days - 1)).isoformat(), today.isoformat()


def _empty() -> dict[str, Any]:
    return {"cost": 0.0, "calls": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}


def _add(total: dict[str, Any], metrics: dict | None) -> None:
    m = metrics or {}
    total["cost"] += float(m.get("spend") or 0)
    total["calls"] += int(m.get("api_requests") or 0)
    total["failed"] += int(m.get("failed_requests") or 0)
    total["input_tokens"] += int(m.get("prompt_tokens") or 0)
    total["output_tokens"] += int(m.get("completion_tokens") or 0)
    total["cached_tokens"] += int(m.get("cache_read_input_tokens") or 0)


def _rounded(total: dict[str, Any]) -> dict[str, Any]:
    return {**total, "cost": round(total["cost"], 4)}


def _alias(entry: dict | None) -> str:
    return str(((entry or {}).get("metadata") or {}).get("key_alias") or "")


def read_rows(gateway: ModelsGatewayClient, days: int, today: date) -> list[dict]:
    start, end = window(days, today)
    return gateway.daily_activity(start, end)


def agent_spend(rows: list[dict], agent_name: str) -> dict[str, Any]:
    """One agent's use across `rows`: the total, each day it made a call,
    and each model it called, most expensive first."""
    alias = f"{_AGENT_PREFIX}{agent_name}"
    total = _empty()
    daily: list[dict[str, Any]] = []
    models: dict[str, dict[str, Any]] = {}
    for row in rows:
        breakdown = row.get("breakdown") or {}
        day = _empty()
        for entry in (breakdown.get("api_keys") or {}).values():
            if _alias(entry) == alias:
                _add(day, entry.get("metrics"))
        if day["calls"] or day["cost"]:
            daily.append({"date": row.get("date"), **_rounded(day)})
            for key in total:
                total[key] += day[key]
        for model_name, model in (breakdown.get("models") or {}).items():
            for entry in ((model or {}).get("api_key_breakdown") or {}).values():
                if _alias(entry) == alias:
                    _add(models.setdefault(model_name, _empty()), entry.get("metrics"))
    return {
        "total": _rounded(total),
        "daily": sorted(daily, key=lambda d: d["date"] or ""),
        "models": sorted(
            ({"model": name, **_rounded(m)} for name, m in models.items()),
            key=lambda m: (-m["cost"], -m["calls"], m["model"]),
        ),
    }


def room_spend(rows: list[dict], agent_names: set[str]) -> dict[str, Any]:
    """The room's use across `rows`, one line per agent that made a call,
    most expensive first. An agent the room no longer has keeps its line,
    marked `removed`. Calls made with a key the room did not mint for an
    agent are summed under `other`."""
    total = _empty()
    other = _empty()
    agents: dict[str, dict[str, Any]] = {}
    for row in rows:
        for entry in ((row.get("breakdown") or {}).get("api_keys") or {}).values():
            alias = _alias(entry)
            _add(total, entry.get("metrics"))
            if alias.startswith(_AGENT_PREFIX):
                _add(agents.setdefault(alias[len(_AGENT_PREFIX):], _empty()), entry.get("metrics"))
            else:
                _add(other, entry.get("metrics"))
    return {
        "total": _rounded(total),
        "other": _rounded(other),
        "agents": sorted(
            ({"agent": name, "removed": name not in agent_names, **_rounded(a)} for name, a in agents.items()),
            key=lambda a: (-a["cost"], -a["calls"], a["agent"]),
        ),
    }
