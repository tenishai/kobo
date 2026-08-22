<script lang="ts">
  // The room's left rail: logo (the Kobo identity -- hammer mark + wordmark,
  // "the room gets its hammer" ruling), nav, and the Create button with its
  // warm-prefetch handlers. Collapses to an icon rail on narrow viewports.
  import { createQuery } from "@tanstack/svelte-query";
  import { formatHash } from "../lib/router.svelte";
  import { agentsQuery, whoamiQuery } from "../lib/queries";

  let { active, oncreate, onprefetch }: {
    active: "dashboard" | "agent" | "getting-started";
    oncreate: () => void;
    onprefetch: () => void;
  } = $props();

  // Access-control Task A1: the room shows who is signed in. `staleTime:
  // Infinity` (whoamiQuery's own option) means this fires once per session,
  // not on every Sidebar mount. Server-verified identity only — the footer
  // just displays what `/api/whoami` already decided, never derives
  // authority itself.
  const whoami = createQuery(whoamiQuery());

  // The Kobo identity ruling ("the pulse"): the hammer mark pulses while
  // any agent is under construction. Same `["agents"]` query key Dashboard
  // already polls (`agentsQuery`'s own adaptive `refetchInterval`, status.ts)
  // — this just subscribes to that one shared cache entry, it doesn't start
  // a second poll of its own. "Building" is deliberately narrower than
  // status.ts's `isTerminal`/`anyAgentActive` (which also count a
  // destroy-in-flight as non-terminal): only the two statuses that actually
  // mean "under construction" count here.
  const agents = createQuery(agentsQuery());
  const building = $derived(
    ($agents.data?.agents ?? []).some(
      (agent) => agent.status === "pending" || agent.status === "provisioning",
    ),
  );

  /** Svelte's `SVGAttributes` typing has no `title` entry (unlike
   * `HTMLAttributes`), so the hammer's explanatory title -- an attribute,
   * not the `<title>` child element SVG normally wants, since that's what a
   * plain `getAttribute("title")` reads -- is set imperatively through this
   * action instead of a typed attribute binding. */
  function hammerTitle(node: SVGElement, value: string | undefined): { update(value: string | undefined): void } {
    function apply(v: string | undefined): void {
      if (v) {
        node.setAttribute("title", v);
      } else {
        node.removeAttribute("title");
      }
    }
    apply(value);
    return { update: apply };
  }
</script>

<nav class="sidebar" aria-label="Main">
  <a class="sidebar__logo" href={formatHash({ page: "dashboard" })} aria-label="Kobo home">
    <span class="sidebar__logo-row">
      <svg
        class="sidebar__hammer"
        class:sidebar__hammer--building={building}
        viewBox="0 0 24 24"
        width="26"
        height="26"
        aria-hidden="true"
        use:hammerTitle={building ? "Kobo is building an agent." : undefined}
      >
        <circle class="sidebar__hammer-ring" cx="12" cy="12" r="10.5" />
        <!-- Forge-hammer redraw: a cross-peen head (flat rectangular face on
             the right, tapered wedge peen on the left, drawn before the
             45deg strike rotation below) fused to a stout handle. The path
             traces tip -> face's near-top corner -> far-top corner -> far
             bottom corner -> face's near-bottom corner -> Z (back to the
             tip), so the left edge tapers to a point and the right edge
             stays a flat vertical face. Same bounding box the old
             symmetric head rect used (x 6.5-17.2, y 3.5-7.7), so the
             rotate(45) below still lands the same way. -->
        <g class="sidebar__hammer-glyph" transform="rotate(45 12 12)">
          <path d="M6.5 5.6 L13 3.5 L17.2 3.5 L17.2 7.7 L13 7.7 Z" />
          <rect x="10.6" y="7.4" width="2.8" height="9.6" rx="1.2" />
        </g>
      </svg>
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

  {#if $whoami.data?.email}
    <p class="sidebar__whoami" title={$whoami.data.email}>{$whoami.data.email}</p>
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

  /* The hammer mark: a simple ring (amber accent) framing a forge
     cross-peen hammer glyph (ink) -- flat rectangular face and tapered
     wedge peen, head angled up-right at rest -- the "ink/amber palette
     tokens" the ruling asks for. */
  .sidebar__hammer {
    flex-shrink: 0;
  }

  .sidebar__hammer-ring {
    fill: none;
    stroke: var(--accent);
    stroke-width: 1.5;
  }

  .sidebar__hammer-glyph {
    fill: var(--ink);
  }

  /* The pulse: while an agent is pending/provisioning, the hammer breathes
     green — the same "Ready" green (--ok) StatusBadge's own dot uses, not
     the amber --pulse token (that one means "still working" on a status
     dot; here the room itself is doing the work, so it reads as progress,
     not a warning). The glow (a drop-shadow tied to the same --ok token)
     breathes its radius in step with the existing opacity/scale beats,
     rather than running as a separate animation, so the two never drift
     out of sync. */
  .sidebar__hammer--building .sidebar__hammer-glyph {
    fill: var(--ok);
  }

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

    .sidebar__whoami {
      display: none;
    }
  }
</style>
