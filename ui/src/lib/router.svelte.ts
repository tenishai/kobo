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
  | { page: "agent"; name: string; tab?: string; section?: string }
  | { page: "getting-started"; section?: string }
  | { page: "admin"; pane?: string; item?: string };

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
 *
 * Segments three and four are the agent page's tab and, when that tab is
 * Harness, the section inside it (harness wave, 2026-09-23):
 * `#/agents/clio/harness/skills`. Both are handed back verbatim -- the
 * router has no opinion about what names a real tab, since that answer
 * depends on the agent, and `tabs.ts` owns it.
 */
export function parseHash(hash: string): Route {
  const path = hash.startsWith("#") ? hash.slice(1) : hash;
  const segments = path.split("/").filter((segment) => segment.length > 0);

  // `#/getting-started/<section>` opens the guide at one section (guide
  // wave, 2026-09-24); the section id is handed back verbatim, and the page
  // falls back to the first section for an id the guide does not have.
  // `#/admin/<pane>/<item>` is the admin panel: the pane and the thing open
  // inside it, both verbatim, so a pane and a row are a link someone can send.
  if (segments[0] === "admin" && segments.length <= 3) {
    const route: Route = { page: "admin" };
    if (segments[1]) {
      route.pane = segments[1];
      if (segments[2]) {
        route.item = segments[2];
      }
    }
    return route;
  }
  if (segments[0] === "getting-started" && segments.length <= 2) {
    const route: Route = { page: "getting-started" };
    if (segments[1]) {
      route.section = segments[1];
    }
    return route;
  }
  if (segments[0] === "agents" && segments.length > 1 && segments[1].length > 0) {
    const raw = segments[1];
    let name: string;
    try {
      name = decodeURIComponent(raw);
    } catch {
      name = raw;
    }
    if (name.length === 0) {
      return DASHBOARD;
    }
    const route: Route = { page: "agent", name };
    if (segments[2]) {
      route.tab = segments[2];
      if (segments[3]) {
        route.section = segments[3];
      }
    }
    return route;
  }
  return DASHBOARD;
}

/** The inverse of parseHash: the hash to set for a given Route. */
export function formatHash(route: Route): string {
  if (route.page === "agent") {
    const base = `#/agents/${encodeURIComponent(route.name)}`;
    // A section with no tab in front of it would land in the tab's own
    // segment on the way back, so it is dropped rather than written.
    if (!route.tab) {
      return base;
    }
    const tail = route.section ? `/${route.section}` : "";
    return `${base}/${route.tab}${tail}`;
  }
  if (route.page === "getting-started") {
    return route.section ? `#/getting-started/${route.section}` : "#/getting-started";
  }
  if (route.page === "admin") {
    if (!route.pane) {
      return "#/admin";
    }
    const tail = route.item ? `/${encodeURIComponent(route.item)}` : "";
    return `#/admin/${route.pane}${tail}`;
  }
  return "#/";
}

function currentHash(): string {
  return typeof location === "undefined" ? "" : location.hash;
}

/** Flat, mutable shape of the live route state (see `route` below). */
type RouteState = {
  page: "dashboard" | "agent" | "getting-started" | "admin";
  name: string | null;
  /** The agent page's tab, and the Harness tab's section (or the guide's
   * section on the getting-started page), as written in the hash -- `null`
   * when the hash names neither. Unvalidated on purpose; the page checks
   * them against the tabs that agent (or sections the guide) actually has. */
  tab: string | null;
  section: string | null;
};

function toState(parsed: Route): RouteState {
  // The admin page reuses the two slots: `tab` is the pane, `section` the
  // item inside it, so nothing downstream grows a third shape.
  if (parsed.page === "admin") {
    return { page: "admin", name: null, tab: parsed.pane ?? null, section: parsed.item ?? null };
  }
  return {
    page: parsed.page,
    name: parsed.page === "agent" ? parsed.name : null,
    tab: parsed.page === "agent" ? parsed.tab ?? null : null,
    section:
      parsed.page === "agent" || parsed.page === "getting-started"
        ? parsed.section ?? null
        : null,
  };
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
  route.tab = next.tab;
  route.section = next.section;
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
