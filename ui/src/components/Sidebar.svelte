<script lang="ts">
  // The room's left rail: logo (the Kobo identity -- hammer mark + wordmark,
  // "the room gets its hammer" ruling), nav, and the Create button with its
  // warm-prefetch handlers. Collapses to an icon rail on narrow viewports.
  import { createQuery } from "@tanstack/svelte-query";
  import { formatHash } from "../lib/router.svelte";
  import { agentsQuery, whoamiQuery } from "../lib/queries";

  let { active, oncreate, onopenguide, onopenconnect, onprefetch }: {
    active: "dashboard" | "agent" | "getting-started" | "personas";
    oncreate: () => void;
    onopenguide: () => void;
    onopenconnect: () => void;
    onprefetch: () => void;
  } = $props();

  // Access-control Task A1: the room shows who is signed in. `staleTime:
  // Infinity` (whoamiQuery's own option) means this fires once per session,
  // not on every Sidebar mount. Server-verified identity only — the footer
  // just displays what `/api/whoami` already decided, never derives
  // authority itself.
  const whoami = createQuery(() => (whoamiQuery()));

  // The Kobo identity ruling ("the pulse"): the hammer mark pulses while
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
    <span class="sidebar__logo-row">
      <img
        class="sidebar__hammer"
        class:sidebar__hammer--building={building}
        src="/hammer.png"
        alt=""
        width="26"
        height="26"
        aria-hidden="true"
        title={building ? "Kobo is building an agent." : undefined}
      />
      <span class="sidebar__wordmark">Kobo</span>
    </span>
    <span class="sidebar__tagline">assembly room</span>
  </a>

  <ul class="sidebar__nav">
    <li>
      <a
        class="sidebar__link"
        href={formatHash({ page: "dashboard" })}
        aria-current={active === "dashboard" || active === "agent" ? "page" : undefined}
        aria-label="Agents"
        title="Agents"
      >
        <span class="sidebar__icon" aria-hidden="true">◫</span>
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
        <span class="sidebar__icon" aria-hidden="true">✦</span>
        <span class="sidebar__label">Getting started</span>
      </a>
    </li>
    <li>
      <a
        class="sidebar__link"
        href={formatHash({ page: "personas" })}
        aria-current={active === "personas" ? "page" : undefined}
        aria-label="Personas"
        title="Personas"
      >
        <span class="sidebar__icon" aria-hidden="true">◐</span>
        <span class="sidebar__label">Personas</span>
      </a>
    </li>
  </ul>

  <button
    type="button"
    class="sidebar__create"
    onmouseenter={onprefetch}
    onfocus={onprefetch}
    onclick={oncreate}
    aria-label="Create agent"
    title="Create agent"
  >
    <span class="sidebar__create-label">Create agent</span>
    <span class="sidebar__create-glyph" aria-hidden="true">+</span>
  </button>

  <!-- Team-guide spec 2026-09-14: a secondary action under Create agent,
       never competing with it for primary-accent styling. Shares the same
       warm-prefetch handlers (groups is one of the catalogs this button's
       own modal reads). -->
  <button
    type="button"
    class="sidebar__guide"
    onmouseenter={onprefetch}
    onfocus={onprefetch}
    onclick={onopenguide}
    aria-label="Quick-start team agent"
    title="Quick-start team agent"
  >
    <span class="sidebar__guide-label">Quick-start team agent</span>
    <span class="sidebar__guide-glyph" aria-hidden="true">◎</span>
  </button>

  <!-- Connected-agents design 2026-09-21: a third sibling in the same
       secondary style, for a repository a team already owns. Shares the
       warm-prefetch handlers for the same reason the guide button does
       (groups is one of the catalogs its modal reads). -->
  <button
    type="button"
    class="sidebar__guide"
    onmouseenter={onprefetch}
    onfocus={onprefetch}
    onclick={onopenconnect}
    aria-label="Connect your own agent"
    title="Connect your own agent"
  >
    <span class="sidebar__guide-label">Connect your own agent</span>
    <span class="sidebar__guide-glyph" aria-hidden="true">◈</span>
  </button>

  {#if whoami.data?.email}
    <p class="sidebar__whoami" title={whoami.data.email}>{whoami.data.email}</p>
  {/if}
</nav>

<style>
  .sidebar {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
    width: 13rem;
    padding: var(--space-lg) var(--space-md);
    border-right: 1px solid var(--border);
  }

  .sidebar__logo {
    display: flex;
    flex-direction: column;
    color: var(--ink);
    text-decoration: none;
  }

  .sidebar__logo-row {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
  }

  .sidebar__wordmark {
    font-size: 1.3rem;
    font-weight: 600;
  }

  /* The hammer mark: the operator's own PNG asset (a dark disc with the
     hammer knocked out as transparent negative space, baked into the
     pixels rather than drawn here). A raster image has no fill to recolor,
     so unlike the old inline-SVG mark, the mark itself doesn't swap tone
     while building — the glow below is the whole building signal. */
  .sidebar__hammer {
    flex-shrink: 0;
  }

  /* The pulse: while an agent is pending/provisioning, the hammer breathes
     a green glow — the same "Ready" green (--ok) StatusBadge's own dot
     uses, not the amber --pulse token (that one means "still working" on a
     status dot; here the room itself is doing the work, so it reads as
     progress, not a warning). The glow (a drop-shadow tied to the same
     --ok token) follows the PNG's alpha channel same as it did the SVG's,
     and breathes its radius in step with the existing opacity/scale beats,
     rather than running as a separate animation, so the two never drift
     out of sync. */
  .sidebar__hammer--building {
    animation: hammer-pulse 1.6s ease-in-out infinite;
  }

  @keyframes hammer-pulse {
    0%,
    100% {
      opacity: 1;
      transform: scale(1);
      filter: drop-shadow(0 0 1px var(--ok));
    }
    50% {
      opacity: 0.75;
      transform: scale(1.12);
      filter: drop-shadow(0 0 4px var(--ok));
    }
  }

  /* Reduced motion drops the animation, but a state signal still needs to
     be visible without it moving — a fixed-radius glow stands in for the
     breath. */
  @media (prefers-reduced-motion: reduce) {
    .sidebar__hammer--building {
      animation: none;
      filter: drop-shadow(0 0 3px var(--ok));
    }
  }

  .sidebar__tagline {
    color: var(--muted);
    font-size: 0.75rem;
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
    gap: var(--space-xs);
    padding: var(--space-xs) var(--space-sm);
    border-radius: var(--radius-sm);
    color: var(--ink);
    text-decoration: none;
  }

  .sidebar__link:hover {
    background: var(--panel);
  }

  .sidebar__link[aria-current="page"] {
    background: var(--panel);
    font-weight: 600;
  }

  .sidebar__create {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .sidebar__create:hover {
    filter: brightness(1.05);
  }

  .sidebar__create:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  .sidebar__create-glyph {
    display: none;
  }

  /* Secondary style (team-guide spec 2026-09-14): an outline, not the
     filled accent Create agent uses -- a sibling action, not a competing
     primary one. */
  .sidebar__guide {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-weight: 600;
    cursor: pointer;
  }

  .sidebar__guide:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .sidebar__guide:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  .sidebar__guide-glyph {
    display: none;
  }

  .sidebar__whoami {
    margin: auto 0 0 0;
    padding-top: var(--space-sm);
    border-top: 1px solid var(--border);
    color: var(--muted);
    font-size: 0.75rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  @media (max-width: 48rem) {
    .sidebar {
      width: auto;
    }

    .sidebar__label,
    .sidebar__tagline {
      display: none;
    }

    .sidebar__create-label {
      display: none;
    }

    .sidebar__create-glyph {
      display: inline;
    }

    .sidebar__guide-label {
      display: none;
    }

    .sidebar__guide-glyph {
      display: inline;
    }

    .sidebar__whoami {
      display: none;
    }
  }
</style>
