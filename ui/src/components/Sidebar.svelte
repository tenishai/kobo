<script lang="ts">
  // The room's left rail: logo (the dot-matrix KOBO wordmark; the hammer
  // lives on as the favicon), nav, and the New agent button with
  // its warm-prefetch handlers. Collapses to an icon rail on narrow
  // viewports.
  //
  // New-agent entry wave (2026-09-23): the rail used to stack three buttons
  // here, one per station, and they read as three competing primaries. Now
  // there is one, and the question of which station a person wants is asked
  // once, inside the chooser it opens.
  import { createQuery } from "@tanstack/svelte-query";
  import { BookOpen, Plus, SquaresFour } from "phosphor-svelte";
  import { formatHash } from "../lib/router.svelte";
  import AccountMenu from "./AccountMenu.svelte";
  import Wordmark from "./Wordmark.svelte";
  import { agentsQuery, whoamiQuery } from "../lib/queries";

  let { active, onnewagent, onprefetch }: {
    active: "dashboard" | "agent" | "getting-started" | "admin";
    onnewagent: () => void;
    onprefetch: () => void;
  } = $props();

  // Access-control Task A1: the room shows who is signed in. `staleTime:
  // Infinity` (whoamiQuery's own option) means this fires once per session,
  // not on every Sidebar mount. Server-verified identity only — the footer
  // just displays what `/api/whoami` already decided, never derives
  // authority itself.
  const whoami = createQuery(() => (whoamiQuery()));

  // The Kobo identity ruling ("the pulse"): the wordmark pulses while
  // any agent is under construction. Same `["agents"]` query key Dashboard
  // already polls (`agentsQuery`'s own adaptive `refetchInterval`, status.ts)
  // — this just subscribes to that one shared cache entry, it doesn't start
  // a second poll of its own. "Building" is deliberately narrower than
  // status.ts's `isTerminal`/`anyAgentActive` (which also count a
  // destroy-in-flight as non-terminal): only the two statuses that actually
  // mean "under construction" count here.
  const agents = createQuery(() => (agentsQuery()));
  const building = $derived(
    (agents.data?.agents ?? []).some(
      (agent) => agent.status === "pending" || agent.status === "provisioning",
    ),
  );
</script>

<nav class="sidebar" aria-label="Main">
  <a class="sidebar__logo" href={formatHash({ page: "dashboard" })} aria-label="Kobo home">
    <span class="sidebar__brand">
      <span
        class="sidebar__wordmark"
        class:sidebar__wordmark--building={building}
        title={building ? "Kobo is building an agent." : undefined}
      ><Wordmark height={27} /></span>
      <span class="sidebar__tagline">
        <span>Assembly Room</span>
        <span>For Building Agents</span>
      </span>
    </span>
  </a>

  <button
    type="button"
    class="sidebar__create"
    onmouseenter={onprefetch}
    onfocus={onprefetch}
    onclick={onnewagent}
    aria-label="New agent"
    title="New agent"
  >
    <Plus size={16} weight="bold" aria-hidden="true" />
    <span class="sidebar__create-label">New agent</span>
  </button>

  <div class="sidebar__section">
    <span class="sidebar__section-label">Workspace</span>
    <ul class="sidebar__nav">
      <li>
        <a
          class="sidebar__link"
          href={formatHash({ page: "dashboard" })}
          aria-current={active === "dashboard" || active === "agent" ? "page" : undefined}
          aria-label="Agents"
          title="Agents"
        >
          <SquaresFour class="sidebar__icon" size={18} aria-hidden="true" />
          <span class="sidebar__label">Agents</span>
        </a>
      </li>
      <li>
        <a
          class="sidebar__link"
          href={formatHash({ page: "getting-started" })}
          aria-current={active === "getting-started" ? "page" : undefined}
          aria-label="Getting started"
          title="Getting started"
        >
          <BookOpen class="sidebar__icon" size={18} aria-hidden="true" />
          <span class="sidebar__label">Getting started</span>
        </a>
      </li>
    </ul>
  </div>

  {#if whoami.data?.email}
    <AccountMenu whoami={whoami.data} {active} />
  {/if}
</nav>

<style>
  /* The dark rail. It re-points the room's own tokens at the rail palette,
     so everything inside it (links, the New agent button, the account
     menu) reads the rail's colours without a second set of rules. One face
     (the body sans) and one icon set (Phosphor) throughout, so nothing in
     the rail comes from a different font. */
  .sidebar {
    --ink: var(--rail-ink);
    --muted: var(--rail-muted);
    --panel: var(--rail-raised);
    --border: var(--rail-border);
    --accent: var(--rail-ink);
    --accent-ink: var(--rail);
    --accent-soft: var(--rail-raised);
    --bg: var(--rail-raised);

    position: sticky;
    top: var(--space-sm);
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
    width: 15rem;
    flex-shrink: 0;
    height: calc(100vh - 2 * var(--space-sm));
    padding: var(--space-md) var(--space-sm);
    border-radius: var(--radius);
    background: var(--rail);
    color: var(--ink);
    font-family: var(--font-sans);
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.12);
  }

  .sidebar__logo {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
    /* A full-width rule under the subtitle, the same one the account row
       sits under, closes the header off from the actions below. */
    /* One inset on every side of the mark: the rail's own padding plus
       these match its left edge, and the bottom allows for the subtitle's
       line height, so the space reads even all the way round. */
    padding: var(--space-2xs) var(--space-xs) var(--space-md);
    border-bottom: 1px solid var(--border);
    color: var(--ink);
    text-decoration: none;
  }

  .sidebar__brand {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 0.9rem;
  }

  .sidebar__wordmark {
    display: block;
    color: var(--ink);
  }

  /* Geist Mono, the wordmark's own register: small, airy, and set apart
     from the Inter used for everything a person clicks. */
  .sidebar__tagline {
    display: flex;
    flex-direction: column;
    color: var(--muted);
    font-family: var(--font-display-mono);
    font-size: 0.62rem;
    font-weight: 500;
    line-height: 1.6;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  /* The pulse: while an agent is pending/provisioning, the wordmark's
     pixels breathe a green glow (--ok, the Ready colour: the room is
     making progress). */
  .sidebar__wordmark--building {
    animation: wordmark-pulse 1.6s ease-in-out infinite;
  }

  @keyframes wordmark-pulse {
    0%,
    100% {
      opacity: 1;
      filter: drop-shadow(0 0 1px var(--ok));
    }
    50% {
      opacity: 0.7;
      filter: drop-shadow(0 0 4px var(--ok));
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .sidebar__wordmark--building {
      animation: none;
      filter: drop-shadow(0 0 3px var(--ok));
    }
  }

  .sidebar__create {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-xs);
    height: 2.25rem;
    padding: 0 var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--rail-ink);
    color: var(--rail);
    font: inherit;
    font-size: 0.85rem;
    font-weight: 600;
    cursor: pointer;
    transition: opacity 0.15s ease;
  }

  .sidebar__create:hover {
    opacity: 0.9;
  }

  .sidebar__create:focus-visible {
    outline: 2px solid var(--rail-ink);
    outline-offset: 2px;
  }

  .sidebar__section {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .sidebar__section-label {
    padding: 0 var(--space-sm);
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 500;
    letter-spacing: 0.04em;
    opacity: 0.8;
  }

  .sidebar__nav {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .sidebar__link {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
    height: 2.6rem;
    padding: 0 var(--space-sm);
    border-radius: var(--radius-sm);
    color: var(--muted);
    font-size: 0.875rem;
    font-weight: 500;
    text-decoration: none;
    transition: background 0.15s ease, color 0.15s ease;
  }

  .sidebar__link :global(.sidebar__icon) {
    flex-shrink: 0;
    opacity: 0.85;
  }

  .sidebar__link:hover {
    background: color-mix(in oklab, var(--rail-raised) 60%, transparent);
    color: var(--ink);
  }

  .sidebar__link[aria-current="page"] {
    background: var(--rail-raised);
    color: var(--ink);
  }

  .sidebar__link[aria-current="page"] :global(.sidebar__icon) {
    opacity: 1;
  }

  @media (max-width: 48rem) {
    .sidebar {
      width: auto;
    }

    .sidebar__brand,
    .sidebar__label,
    .sidebar__section-label,
    .sidebar__create-label {
      display: none;
    }
  }
</style>
