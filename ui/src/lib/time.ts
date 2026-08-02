// Pure, dependency-free "time ago" formatting for an agent's `created_at`.
// Deliberately coarse (minutes/hours/days) — this is a card glance, not a
// precise clock — and takes `now` as a parameter rather than reading
// `Date.now()` internally, so it's testable without mocking global timers.

const MINUTE_MS = 60 * 1000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/**
 * Format `iso` (an ISO-8601 timestamp) relative to `now` (defaults to the
 * current time) as a coarse "time ago" string:
 *  - under a minute -> "just now"
 *  - under an hour  -> "Nm ago"
 *  - under a day    -> "Nh ago"
 *  - a day or more  -> "Nd ago"
 *
 * A negative difference (a timestamp in the future — clock skew between
 * client and server, most likely) is clamped to "just now" rather than
 * printing a negative duration.
 */
export function relativeTime(iso: string, now: Date = new Date()): string {
  const diff = now.getTime() - new Date(iso).getTime();

  if (diff < MINUTE_MS) {
    return "just now";
  }
  if (diff < HOUR_MS) {
    return `${Math.floor(diff / MINUTE_MS)}m ago`;
  }
  if (diff < DAY_MS) {
    return `${Math.floor(diff / HOUR_MS)}h ago`;
  }
  return `${Math.floor(diff / DAY_MS)}d ago`;
}
