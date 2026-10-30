"""One way to write a timestamp onto the wire (issue #306).

The tables hold naive datetimes that are UTC by construction
(`datetime.now(UTC).replace(tzinfo=None)`), and `datetime.isoformat()` on one
of those prints no offset. A browser reads an offset-less string as LOCAL
time, so every page showed the viewer's UTC offset as an error. Every
timestamp that reaches a response goes through `rfc3339`, which treats a
naive datetime as UTC and always ends in `Z`. Strings the permission service
hands back pass through `stamp_strings`, which gives an offset-less one its
`Z` too. Unix-second fields (`ts`) are numbers and stay numbers.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, overload

_OFFSETLESS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$")
_UTC_OFFSET = re.compile(r"[+-]00:?00$")
_KEY = re.compile(r"(^|_)at$")


@overload
def rfc3339(dt: datetime) -> str: ...
@overload
def rfc3339(dt: None) -> None: ...
def rfc3339(dt: datetime | None) -> str | None:
    """`dt` as RFC 3339 in UTC with a trailing `Z`; a naive value is UTC already."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def rfc3339_str(value: str) -> str:
    """An ISO-8601 string from another service, given its `Z` when it names no
    offset (the permission service writes naive UTC too). Anything else is
    returned as it came."""
    if _OFFSETLESS.match(value):
        return value + "Z"
    if _UTC_OFFSET.search(value) and "T" in value:
        return _UTC_OFFSET.sub("Z", value)
    return value


def stamp_strings(obj: Any) -> Any:
    """Walk a JSON body and fix every `*_at` / `at` string in place."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str) and _KEY.search(key):
                obj[key] = rfc3339_str(value)
            elif isinstance(value, (dict, list)):
                stamp_strings(value)
    elif isinstance(obj, list):
        for item in obj:
            stamp_strings(item)
    return obj
