// The one place a server timestamp becomes a Date, and the shared formatters
// over it. The API writes RFC 3339 with a Z (issue #306); an older server, or
// a service behind it, may still hand out a naive string with no offset, and
// the browser would read that as LOCAL time. `serverDate` treats an
// offset-less string as UTC so every page shows the viewer's local time.
// Unix-second fields (`ts`) are numbers and never come through here.

const MINUTE_MS = 60 * 1000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

const OFFSETLESS = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/;

/** Parse a timestamp the server sent. An ISO string with no offset is UTC. */
export function serverDate(iso: string): Date {
  return new Date(OFFSETLESS.test(iso) ? `${iso}Z` : iso);
}

/** "Oct 29, 05:07 PM" in the viewer's locale and zone; "" for nothing. */
export function when(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = serverDate(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/** "Oct 29" in the viewer's locale and zone; "" for nothing. */
export function whenDay(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = serverDate(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/**
 * Format `iso` relative to `now` (defaults to the current time) as a coarse
 * "time ago" string: "just now", "Nm ago", "Nh ago", "Nd ago". A timestamp
 * in the future (clock skew) is clamped to "just now".
 */
export function relativeTime(iso: string, now: Date = new Date()): string {
  const diff = now.getTime() - serverDate(iso).getTime();

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
