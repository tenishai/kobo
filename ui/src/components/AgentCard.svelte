<script lang="ts">
  // One tile in the dashboard grid. Presentation only, over the one row
  // `agentsQuery` already returns for this agent, plus the room-wide
  // observability catalog (shared across every mounted card, one query key —
  // see `observabilityQuery`'s own doc comment). The root is a `<div>`, not
  // an `<a>`: nesting the Observability link (Task 6 of the
  // observability-catalog addendum) inside a whole-tile anchor would be
  // invalid HTML — a browser's parser closes an outer `<a>` as soon as it
  // meets an inner one, which would silently detach the inner link from the
  // card's layout. Instead, `.agent-card__stretched-link` is an absolutely
  // positioned anchor covering the whole tile (built from the router's own
  // `formatHash`, so its destination hash can never drift from what
  // `parseHash` on the other end expects) — that's what makes the WHOLE
  // card clickable/keyboard-focusable/"open in new tab"-able, same as
  // before. The Observability link, appearing later in the DOM, paints
  // above it (see its own `position: relative` below) so it stays
  // independently clickable rather than swallowed by the stretched anchor
  // underneath.
  import StatusBadge from "./StatusBadge.svelte";
  import { createQuery } from "@tanstack/svelte-query";
  import { KIND_CONNECTED } from "../lib/api";
  import { observabilityQuery } from "../lib/queries";
  import { formatHash } from "../lib/router.svelte";
  import { agentTone } from "../lib/status";
  import { relativeTime } from "../lib/time";
  import type { AgentSummary } from "../lib/queries";

  let { agent }: { agent: AgentSummary } = $props();

  // Task 6 of the observability-catalog addendum: rendered only when the
  // room config carries BOTH the dashboard base URL and the project, per
  // the addendum's fixed URL shape.
  const observability = createQuery(() => observabilityQuery());
  let dashboardUrl = $derived(
    observability.data?.url && observability.data?.project
      ? `${observability.data.url}/projects/${observability.data.project}/dashboards/${agent.name}-agent-health`
      : null,
  );
</script>

<div class="agent-card">
  <a
    class="agent-card__stretched-link"
    href={formatHash({ page: "agent", name: agent.name })}
    aria-label={`Open ${agent.name}`}
    title={agent.name}
  ></a>
  <div class="agent-card__top">
    <span class="agent-card__identity">
      {#if agent.has_avatar}
        <img
          class="agent-card__avatar"
          src={`/api/agents/${encodeURIComponent(agent.name)}/avatar`}
          alt=""
        />
      {/if}
      <span class="agent-card__name" title={agent.name}>{agent.name}</span>
      <!-- Connected-agents design 2026-09-21: a small marker so nobody
           wonders why this one has no persona. Compared against the named
           kind constant, never truth-tested, so a later kind wears no
           marker until it asks for one. -->
      {#if agent.kind === KIND_CONNECTED}
        <span class="agent-card__kind">connected</span>
      {/if}
    </span>
    <StatusBadge status={agent.status} />
  </div>
  <div class="agent-card__meta">
    <span class="agent-card__model" title={agent.model}>{agent.model_label}</span>
    <span class="agent-card__sep" aria-hidden="true">·</span>
    <span class="agent-card__time">{relativeTime(agent.created_at)}</span>
  </div>
  {#if agent.created_by}
    <p class="agent-card__owner">Made by {agent.created_by}</p>
  {/if}
  {#if agentTone(agent.status) === "fail"}
    <p class="agent-card__error">Something went wrong. Open for details.</p>
  {/if}
  {#if dashboardUrl}
    <a
      class="agent-card__observability"
      href={dashboardUrl}
      target="_blank"
      rel="noreferrer"
    >
      Observability
    </a>
  {/if}
</div>

<style>
  .agent-card {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
    color: inherit;
    transition:
      border-color 0.15s ease,
      box-shadow 0.15s ease;
  }

  .agent-card:hover {
    border-color: var(--accent);
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
  }

  .agent-card__stretched-link {
    position: absolute;
    inset: 0;
    color: inherit;
    text-decoration: none;
  }

  .agent-card__stretched-link:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
    border-radius: var(--radius);
  }

  .agent-card__top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
  }

  .agent-card__identity {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: var(--space-xs);
    overflow: hidden;
  }

  .agent-card__avatar {
    width: 1.75rem;
    height: 1.75rem;
    flex-shrink: 0;
    border-radius: 50%;
    object-fit: cover;
  }

  .agent-card__name {
    overflow: hidden;
    color: var(--ink);
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Quieter than the name it sits beside: a fact about the agent, not part
     of its identity. */
  .agent-card__kind {
    display: inline-flex;
    flex-shrink: 0;
    padding: 0.1rem var(--space-2xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.7rem;
    white-space: nowrap;
  }

  .agent-card__meta {
    display: flex;
    align-items: center;
    gap: var(--space-2xs);
    color: var(--muted);
    font-size: 0.875rem;
  }

  .agent-card__model {
    overflow: hidden;
    max-width: 12rem;
    font-family: var(--font-mono);
    font-size: 0.8rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .agent-card__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.875rem;
  }

  .agent-card__owner {
    margin: 0;
    overflow: hidden;
    color: var(--muted);
    font-size: 0.75rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Positioned (not static) so it paints above the stretched link that
     precedes it in the DOM — see the module doc comment above — while
     staying in normal flow (no offsets applied). */
  .agent-card__observability {
    position: relative;
    align-self: flex-start;
    color: var(--accent);
    font-size: 0.8rem;
    text-decoration: none;
  }

  .agent-card__observability:hover,
  .agent-card__observability:focus-visible {
    text-decoration: underline;
  }
</style>
