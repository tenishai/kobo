<script lang="ts">
  // The room's left rail: logo (wordmark placeholder until the operator
  // picks a mark), nav, and the Create button with its warm-prefetch
  // handlers. Collapses to an icon rail on narrow viewports.
  import { createQuery } from "@tanstack/svelte-query";
  import { formatHash } from "../lib/router.svelte";
  import { whoamiQuery } from "../lib/queries";

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
</script>

<nav class="sidebar" aria-label="Main">
  <a class="sidebar__logo" href={formatHash({ page: "dashboard" })} aria-label="kobo home">
    <span class="sidebar__wordmark">kobo</span>
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

  .sidebar__wordmark {
    font-size: 1.3rem;
    font-weight: 600;
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
