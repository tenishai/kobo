"""Refresh-token families for kobo's MCP server (MCP server arc, Task 3,
the design spec's "Token mechanics" and `POST /oauth/token`'s
`grant_type=refresh_token` step): rotate-on-use, replay revokes the whole
family.

Only refresh tokens are persisted -- state/authorization_code/
access_token are stateless JWTs verified on their own claims
(`kobo_api.mcp.jwt`). A refresh token's `jti` claim doubles as the primary
key of `kobo_api.models.MCPRefreshToken`; the three functions below are
the entire lifecycle a `POST /oauth/token` route (a later task) needs:
mint a fresh family for a brand-new grant, rotate an existing family's
live jti on every refresh, and revoke a family outright (directly, or
because `consume` below caught a replay).

Session/commit boundary: every function here takes an already-open
`session` and never calls `session.commit()` itself -- the same house
convention `kobo_api.pairs`/`kobo_api.directory` follow. The caller (a
route handler) owns the transaction boundary, so a single request that
touches more than one table commits everything together or not at all.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kobo_api.models import MCPRedeemedCode, MCPRefreshToken, MCPRevocation

log = logging.getLogger(__name__)

# The one revoked_reason value this module ever writes on its own
# initiative (a direct revoke_family call from elsewhere names its own
# reason) -- named here so consume()'s replay branch and any test asserting
# on it share the exact same spelling.
REPLAY_REASON = "replay_detected"
CODE_REPLAY_REASON = "authorization_code_replayed"
EXPIRED_REASON = "family_expired"
REVOKED_PERSON_REASON = "person_revoked"

# How long one OAuth grant may keep refreshing (issue #197). Each refresh
# rotates the token, but without this bound a family never ends while the
# client keeps coming back; a person removed from the Workspace, or a stolen
# laptop, kept MCP access indefinitely. After this long the next refresh
# fails and the person signs in again, which re-runs the Google and domain
# checks that only the first grant performs.
FAMILY_MAX_AGE = timedelta(days=30)


def create_family(session: Session, *, jti: str, email: str) -> str:
    """Start a brand-new refresh-token family: inserts the grant's first
    row under a freshly-minted `family_id` (a uuid4 hex, the same id shape
    every other id in this arc uses), and returns that family_id so the
    caller can stamp it into the refresh token JWT's own `family_id`
    extra claim (`kobo_api.mcp.jwt.mint`'s `**extra`). `jti` is the
    caller's own already-minted jti (`kobo_api.mcp.jwt.mint`'s `jti`
    claim), never generated here -- this function only ever records the
    first row of a family, it doesn't mint tokens.
    """
    family_id = uuid.uuid4().hex
    session.add(
        MCPRefreshToken(
            jti=jti,
            family_id=family_id,
            email=email,
            issued_at=datetime.now(UTC),
        )
    )
    return family_id


def consume(session: Session, *, jti: str, email: str) -> str | None:
    """Exchange `jti` for a new jti in the same family, or refuse.

    Checked in this order:

    - No row at all for `jti`: return `None`, no mutation. An unknown jti
      carries no family to touch.
    - The row exists but its `email` doesn't match the caller's: return
      `None`, no mutation, and log a warning. Kept as its own branch
      (rather than folded into "unknown") on purpose: an email mismatch
      must NEVER revoke the family. An attacker who can only guess jtis
      (never possess the real signed token) for someone else's email must
      not be able to kill that person's session just by trying -- that
      would turn this endpoint into a denial-of-service oracle. A
      genuinely unknown jti has no family to kill in the first place, so
      the two branches end up at the same `None` by different reasoning,
      not because they're the same event.
    - The row's family is already revoked (`revoked_at` set, whether by a
      direct `revoke_family` call or a prior replay here): return `None`,
      no further mutation. The family is already dead; this is just a
      fresh confirmation of that fact.
    - The row is already rotated (`rotated_at` set): REPLAY. The jti being
      presented a second time means whoever holds it now is not the party
      that received the newest rotation. The entire family (every row
      sharing `family_id`, including the newest live jti) is revoked via
      `revoke_family`, and this call returns `None`.
    - Otherwise: atomic rotation. The mutation is a single rowcount-
      guarded `UPDATE ... WHERE jti=:jti AND rotated_at IS NULL AND
      revoked_at IS NULL` -- the sqlite-safe equivalent of the reference
      pattern's `SELECT ... FOR UPDATE` (sqlite has no real row locks;
      `FOR UPDATE` is a silent no-op there, so it buys nothing). Checking
      `rowcount == 1` closes the same race a row lock would have: if a
      concurrent call already rotated or revoked this exact jti between
      this function's own lookup above and this UPDATE, rowcount comes
      back 0 -- the loser treats that exactly like a replay (revokes the
      family), since at that point a genuine replay and a lost race are
      indistinguishable, and failing safe is correct either way. On
      success, a new jti is inserted into the same family and returned.
    """
    row = session.get(MCPRefreshToken, jti)
    if row is None:
        return None

    if row.email != email:
        # Truncated to 8 chars: the jti alone isn't a credential (the
        # token's signature is still required to use it), but a full id
        # doesn't belong verbatim in a log line either.
        log.warning(
            "MCP refresh token jti=%s presented for email=%s but belongs "
            "to a different email; treating as not-found",
            jti[:8],
            email,
        )
        return None

    if row.revoked_at is not None:
        return None

    if row.rotated_at is not None:
        revoke_family(session, family_id=row.family_id, reason=REPLAY_REASON)
        return None

    now = datetime.now(UTC)
    started = family_started_at(session, family_id=row.family_id)
    if started is not None and now.replace(tzinfo=None) - started > FAMILY_MAX_AGE:
        revoke_family(session, family_id=row.family_id, reason=EXPIRED_REASON)
        return None
    result = session.execute(
        update(MCPRefreshToken)
        .where(MCPRefreshToken.jti == jti)
        .where(MCPRefreshToken.rotated_at.is_(None))
        .where(MCPRefreshToken.revoked_at.is_(None))
        .values(rotated_at=now)
    )
    if result.rowcount != 1:
        # Lost a race: another call already rotated or revoked this jti
        # between the lookup above and this UPDATE. Fail safe, same as a
        # genuine replay.
        revoke_family(session, family_id=row.family_id, reason=REPLAY_REASON)
        return None

    new_jti = uuid.uuid4().hex
    session.add(
        MCPRefreshToken(
            jti=new_jti,
            family_id=row.family_id,
            email=email,
            issued_at=now,
        )
    )
    return new_jti


def revoke_family(session: Session, *, family_id: str, reason: str) -> int:
    """Revoke every not-yet-revoked row in `family_id`, stamping
    `revoked_at`/`revoked_reason` together on each. Returns the number of
    rows this call actually revoked -- an already-revoked row is left
    untouched, so its original `revoked_at`/`revoked_reason` stays the
    true record of when and why the family died, never overwritten by a
    later caller re-revoking an already-dead family.
    """
    result = session.execute(
        update(MCPRefreshToken)
        .where(MCPRefreshToken.family_id == family_id)
        .where(MCPRefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC), revoked_reason=reason)
    )
    return result.rowcount


def family_started_at(session: Session, *, family_id: str) -> datetime | None:
    """When the family's first token was issued: the grant's own instant, the
    one `FAMILY_MAX_AGE` counts from. Naive UTC, like every DateTime here."""
    started = session.execute(
        select(func.min(MCPRefreshToken.issued_at)).where(
            MCPRefreshToken.family_id == family_id
        )
    ).scalar_one_or_none()
    if started is not None and started.tzinfo is not None:
        started = started.astimezone(UTC).replace(tzinfo=None)
    return started


def redeem_code(
    session: Session, *, jti: str, email: str, family_id: str
) -> str | None:
    """Spend authorization code `jti` once (issue #196). Returns None when
    this call is the first redemption. Returns the family the FIRST
    redemption produced when the code was already spent, after revoking that
    family: whoever presents a code twice is not two honest clients, and the
    tokens the first exchange handed out can no longer be trusted. The
    primary key does the deciding, so two concurrent redemptions resolve the
    same way as two sequential ones: exactly one insert lands.

    Unlike its siblings this function manages its own transaction boundary,
    because the duplicate shows up as an IntegrityError at flush and the
    session has to be rolled back before anything else can be written."""
    session.add(
        MCPRedeemedCode(
            jti=jti, email=email, family_id=family_id, redeemed_at=datetime.now(UTC)
        )
    )
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        first = session.get(MCPRedeemedCode, jti)
        first_family = first.family_id if first is not None else family_id
        log.warning(
            "MCP authorization code jti=%s redeemed again for email=%s; revoking family %s",
            jti[:8],
            email,
            first_family[:8],
        )
        revoke_family(session, family_id=first_family, reason=CODE_REPLAY_REASON)
        session.commit()
        return first_family
    return None


def revoke_email(
    session: Session, *, email: str, actor: str, reason: str | None
) -> int:
    """Cut off `email`'s MCP access now (issue #197): every live family they
    hold is revoked, and the revocation instant is recorded so access tokens
    issued before it are refused by the verifier until they expire on their
    own. Returns how many refresh-token rows were revoked. Idempotent: a
    second call moves the instant forward and revokes nothing new."""
    now = datetime.now(UTC)
    row = session.get(MCPRevocation, email)
    if row is None:
        session.add(
            MCPRevocation(email=email, revoked_at=now, revoked_by=actor, reason=reason)
        )
    else:
        row.revoked_at = now
        row.revoked_by = actor
        row.reason = reason
    result = session.execute(
        update(MCPRefreshToken)
        .where(MCPRefreshToken.email == email)
        .where(MCPRefreshToken.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=REVOKED_PERSON_REASON)
    )
    return int(result.rowcount or 0)


def email_revoked_at(session: Session, *, email: str) -> datetime | None:
    """The instant `email` was last cut off, or None. Naive UTC."""
    row = session.get(MCPRevocation, email)
    if row is None:
        return None
    moment = row.revoked_at
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment
