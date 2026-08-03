<script lang="ts">
  // One tile in the dashboard grid. Presentation only, over the one row
  // `agentsQuery` already returns for this agent — no per-card fetch. The
  // whole tile is a real link (an `<a>`, not a click handler on a `<div>`)
  // built from the router's own `formatHash`, so its destination hash can
  // never drift from what `parseHash` on the other end expects, and it gets
  // keyboard/focus/"open in new tab" behavior for free.
  import StatusBadge from "./StatusBadge.svelte";
  import { formatHash } from "../lib/router.svelte";
  import { agentTone } from "../lib/status";
  import { relativeTime } from "../lib/time";
  import type { AgentSummary } from "../lib/queries";

  let { agent }: { agent: AgentSummary } = $props();
</script>

<a class="agent-card" href={formatHash({ page: "agent", name: agent.name })}>
  <div class="agent-card__top">
    <span class="agent-card__name" title={agent.name}>{agent.name}</span>
    <StatusBadge status={agent.status} />
  </div>
  <div class="agent-card__meta">
    <span class="agent-card__model" title={agent.model}>{agent.model_label}</span>
    <span class="agent-card__sep" aria-hidden="true">·</span>
    <span class="agent-card__time">{relativeTime(agent.created_at)}</span>
  </div>
  {#if agentTone(agent.status) === "fail"}
    <p class="agent-card__error">Something went wrong — open for details.</p>
  {/if}
</a>

<style>
  .agent-card {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
    color: inherit;
    text-decoration: none;
    transition:
      border-color 0.15s ease,
      box-shadow 0.15s ease;
  }

  .agent-card:hover {
    border-color: var(--accent);
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
  }

  .agent-card:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  .agent-card__top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
  }

  .agent-card__name {
    overflow: hidden;
    color: var(--ink);
    font-weight: 600;
    text-overflow: ellipsis;
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
</style>
