// Hash-based, two-page router: `#/` (or anything unrecognized) is the
// dashboard, `#/agents/<name>` is a single agent's detail page. There is no
// nested routing and no history API involvement — the browser's own
// back/forward + hashchange handles that for free.
//
// `parseHash` is a pure function (see router.test.ts) so the routing rules
// are unit-tested without touching the DOM. `route` is the live rune store
// components read from; `navigate` and the `hashchange` listener below are
// the only things that write to it.

export type Route =
  | { page: "dashboard" }
  | { page: "agent"; name: string }
  | { page: "getting-started" };

const DASHBOARD: Route = { page: "dashboard" };

/**
 * Parse a `location.hash`-shaped string into a Route. Anything that isn't
 * exactly `#/agents/<nonempty-name>` falls back to the dashboard — an empty
 * string, `#`, `#/`, `#/agents`, `#/agents/` (empty name segment), and any
 * unrelated path. Deliberately just "is there a second, nonempty path
 * segment after agents" rather than a regex mirroring the server's own
 * route matching. The name segment is percent-decoded on a best-effort
 * basis: `decodeURIComponent` throws `URIError` on malformed
 * percent-encoding (e.g. a lone trailing `%`, as in `#/agents/100%`), and
 * since `route` below is initialized at module import time, an uncaught
 * throw here would blank the whole app on page load and (post-load) kill
 * the `hashchange` listener for the rest of the session. So a decode
 * failure falls back to the raw, still-encoded segment instead of
 * propagating.
 */
export function parseHash(hash: string): Route {
  const path = hash.startsWith("#") ? hash.slice(1) : hash;
  const segments = path.split("/").filter((segment) => segment.length > 0);

  if (segments.length === 1 && segments[0] === "getting-started") {
    return { page: "getting-started" };
  }
  if (segments[0] === "agents" && segments.length > 1 && segments[1].length > 0) {
    const raw = segments[1];
    let name: string;
    try {
      name = decodeURIComponent(raw);
    } catch {
      name = raw;
    }
    return name.length > 0 ? { page: "agent", name } : DASHBOARD;
  }
  return DASHBOARD;
}

/** The inverse of parseHash: the hash to set for a given Route. */
export function formatHash(route: Route): string {
  if (route.page === "agent") {
    return `#/agents/${encodeURIComponent(route.name)}`;
  }
  return route.page === "getting-started" ? "#/getting-started" : "#/";
}

function currentHash(): string {
  return typeof location === "undefined" ? "" : location.hash;
}

/** Flat, mutable shape of the live route state (see `route` below). */
type RouteState = { page: "dashboard" | "agent" | "getting-started"; name: string | null };

function toState(parsed: Route): RouteState {
  return { page: parsed.page, name: parsed.page === "agent" ? parsed.name : null };
}

/**
 * Live routing state. Read `route.page` and (when `page === "agent"`)
 * `route.name` from components. Written only by `navigate` and the
 * `hashchange` listener registered below.
 */
export const route: RouteState = $state(toState(parseHash(currentHash())));

function sync(): void {
  const next = toState(parseHash(currentHash()));
  route.page = next.page;
  route.name = next.name;
}

/** Navigate to `to` by updating `location.hash` (which drives `route` via `hashchange`). */
export function navigate(to: Route): void {
  if (typeof location === "undefined") {
    return;
  }
  const hash = formatHash(to);
  if (location.hash === hash) {
    // Setting an unchanged hash doesn't fire `hashchange` — sync directly.
    sync();
    return;
  }
  location.hash = hash;
}

if (typeof window !== "undefined") {
  window.addEventListener("hashchange", sync);
}
