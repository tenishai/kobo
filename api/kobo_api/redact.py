"""Scrub secret-shaped strings out of text the room shows people (issue #200).

GitHub Actions masks only the secrets a workflow registered; a token echoed by
a build step reaches whoever can read the log. The build-log route runs its
output through `redact_secrets` before answering. The patterns are the
well-known credential shapes plus any value that follows a secret-ish key, so
a miss is possible (a bare high-entropy string with no marker) but the common
leaks are caught. Over-matching only costs a redacted word in a log.
"""

from __future__ import annotations

import re

REDACTED = "[redacted]"

_PATTERNS = [
    # Vendor token shapes.
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # a JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    # Anything handed as a bearer, or assigned to a secret-ish name.
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"(?i)((?:token|secret|password|passwd|api[_-]?key|private[_-]?key|credential)s?"
        r"[A-Za-z0-9_]*\s*[=:]\s*[\"']?)([^\s\"']{8,})"
    ),
]


def redact_secrets(text: str) -> str:
    """`text` with every secret-shaped run replaced by `[redacted]`. Keeps
    the marker that preceded a value (`TOKEN=`, `Bearer `) so the line still
    says what was there."""
    out = text
    for pattern in _PATTERNS:
        if pattern.groups == 2 or pattern.groups == 1:
            out = pattern.sub(lambda m: m.group(1) + REDACTED, out)
        else:
            out = pattern.sub(REDACTED, out)
    return out
