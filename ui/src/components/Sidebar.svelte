<script lang="ts">
  // The room's left rail: logo (wordmark placeholder until the operator
  // picks a mark), nav, and the Create button with its warm-prefetch
  // handlers. Collapses to an icon rail on narrow viewports.
  import { formatHash } from "../lib/router.svelte";

  let { active, oncreate, onprefetch }: {
    active: "dashboard" | "agent" | "getting-started";
    oncreate: () => void;
    onprefetch: () => void;
  } = $props();
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
  >
    Create agent
  </button>
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

  @media (max-width: 48rem) {
    .sidebar {
      width: auto;
    }

    .sidebar__label,
    .sidebar__tagline,
    .sidebar__create {
      display: none;
    }
  }
</style>
