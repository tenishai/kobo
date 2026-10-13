<script lang="ts">
  // One tile in the dashboard grid. Presentation only, over the one row
  // `agentsQuery` already returns for this agent, plus the room-wide
  // observability catalog (shared across every mounted card, one query key —
  // see `observabilityQuery`'s own doc comment).
  //
  // Four rows, in this order, so a column of cards reads as a column and
  // not as a pile: who it is (avatar, name, status), what it is for (one
  // clipped line), what it belongs to (tags), and the quiet facts
  // underneath (model, age, who made it) with the way out to its dashboard
  // at the right. The footer is pushed to the bottom, so two cards with
  // different amounts to say still line their footers up.
  //
  // The root is a `<div>`, not an `<a>`: nesting the Observability link
  // (Task 6 of the observability-catalog addendum) inside a whole-tile
  // anchor would be invalid HTML — a browser's parser closes an outer `<a>`
  // as soon as it meets an inner one, which would silently detach the inner
  // link from the card's layout. Instead, `.agent-card__stretched-link` is
  // an absolutely positioned anchor covering the whole tile (built from the
  // router's own `formatHash`, so its destination hash can never drift from
  // what `parseHash` on the other end expects) — that's what makes the
  // WHOLE card clickable/keyboard-focusable/"open in new tab"-able, same as
  // before. The Observability link, appearing later in the DOM, paints
  // above it (see its own `position: relative` below) so it stays
  // independently clickable rather than swallowed by the stretched anchor
  // underneath.
  import StatusBadge from "./StatusBadge.svelte";
  import { createQuery } from "@tanstack/svelte-query";
  import { KIND_CONNECTED } from "../lib/api";
  import { NO_DESCRIPTION, agentDescription } from "../lib/agents";
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

  let description = $derived(agentDescription(agent));
  let connected = $derived(agent.kind === KIND_CONNECTED);
  let failed = $derived(agentTone(agent.status) === "fail");
  // Only one thing parks an agent: its setup pull request waiting on a
  // person (`StepWaiting`). A bare "waiting" chip didn't say on what
  // (issue #123).
  let waiting = $derived(agent.status === "waiting");
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
    </span>
    <StatusBadge status={agent.status} health={agent.health} />
  </div>

  <!-- One line, always: a card that says "No description yet." is still the
       same height as the one beside it. The whole sentence rides on the
       title for anyone who wants the rest of it. -->
  <p
    class="agent-card__desc"
    class:agent-card__desc--empty={description === NO_DESCRIPTION}
    title={description}
  >
    {description}
  </p>

  {#if agent.group_name || connected}
    <div class="agent-card__tags">
      {#if agent.group_name}
        <span class="agent-card__tag">{agent.group_name}</span>
      {/if}
      <!-- Connected-agents design 2026-09-21: a small marker so nobody
           wonders why this one has no persona. Compared against the named
           kind constant, never truth-tested, so a later kind wears no
           marker until it asks for one. -->
      {#if connected}
        <span class="agent-card__tag agent-card__kind">connected</span>
      {/if}
    </div>
  {/if}

  <div class="agent-card__footer">
    {#if failed}
      <!-- The list route carries no error text, so the card says the one
           true thing it knows and points at the page that has the rest. -->
      <p class="agent-card__error">
        <span class="agent-card__error-line">Something went wrong.</span>
        <span class="agent-card__error-tail">Open for details.</span>
      </p>
    {:else if waiting}
      <p class="agent-card__waiting">
        <span class="agent-card__error-line">Waiting on a pull request.</span>
        <span class="agent-card__error-tail">Open for details.</span>
      </p>
    {:else}
      <!-- The model gets a line of its own (2026-10-01): sharing one line
           with the age, the byline and the Observability link cut every
           label short. Two rows, always, so every card is the same height. -->
      <p class="agent-card__model" title={agent.model}>{agent.model_label}</p>
      <p class="agent-card__meta">
        <span>{relativeTime(agent.created_at)}</span>
        {#if agent.created_by}
          <span class="agent-card__sep" aria-hidden="true">·</span>
          <span class="agent-card__owner">Made by {agent.created_by}</span>
        {/if}
      </p>
    {/if}

    {#if dashboardUrl}
      <a
        class="agent-card__observability"
        href={dashboardUrl}
        target="_blank"
        rel="noreferrer"
        title="Observability"
      >
        <svg
          class="agent-card__icon"
          viewBox="0 0 16 16"
          aria-hidden="true"
          focusable="false"
        >
          <path
            d="M2 13V9m4 4V4m4 9V7m4 6V3"
            fill="none"
            stroke="currentColor"
            stroke-width="1.6"
            stroke-linecap="round"
          />
        </svg>
        <span class="agent-card__observability-label">Observability</span>
      </a>
    {/if}
  </div>
</div>

<style>
  .agent-card {
    position: relative;
    display: flex;
    height: 100%;
    flex-direction: column;
    gap: var(--space-xs);
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
    color: inherit;
    transition:
      border-color 0.15s ease,
      box-shadow 0.15s ease,
      transform 0.15s ease;
  }

  .agent-card:hover {
    border-color: color-mix(in srgb, var(--ink) 20%, transparent);
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.08);
    transform: translateY(-2px);
  }

  @media (prefers-reduced-motion: reduce) {
    .agent-card:hover {
      transform: none;
    }
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

  /* The agent's name is the one identifier on the card: mono, and heavier
     than anything under it. */
  .agent-card__name {
    overflow: hidden;
    color: var(--ink);
    font-family: var(--font-mono);
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .agent-card__desc {
    margin: 0;
    overflow: hidden;
    color: var(--ink);
    font-size: 0.85rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .agent-card__desc--empty {
    color: var(--muted);
  }

  .agent-card__tags {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2xs);
  }

  /* Quieter than the name above them: facts about the agent, not part of
     its identity. */
  .agent-card__tag {
    display: inline-flex;
    flex-shrink: 0;
    padding: 0.1rem var(--space-xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.7rem;
    white-space: nowrap;
  }

  /* Pushed to the bottom, so two cards with different amounts to say still
     agree on where the quiet line sits. */
  .agent-card__footer {
    display: flex;
    min-width: 0;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2xs) var(--space-xs);
    margin-top: auto;
    padding-top: var(--space-2xs);
  }

  .agent-card__meta,
  .agent-card__error,
  .agent-card__waiting {
    display: flex;
    min-width: 0;
    flex: 1;
    align-items: baseline;
    gap: var(--space-2xs);
    margin: 0;
    overflow: hidden;
    color: var(--muted);
    font-size: 0.8rem;
    white-space: nowrap;
  }

  /* The byline is the one fact that gives way: it is the longest and the
     least urgent, so it takes all the shrinking and the model and the age
     stay whole. A model label long enough to need clipping is capped
     rather than allowed to push them off. */
  .agent-card__model {
    flex: 1 0 100%;
    margin: 0;
    overflow: hidden;
    color: var(--ink);
    font-size: 0.8rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .agent-card__owner {
    min-width: 3rem;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .agent-card__sep {
    flex-shrink: 0;
  }

  .agent-card__error {
    color: var(--fail);
  }

  .agent-card__error-line {
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Not a link, and not in the failure colour: it is the way out, not part
     of what went wrong. */
  .agent-card__error-tail {
    flex-shrink: 0;
    color: var(--muted);
  }

  /* Positioned (not static) so it paints above the stretched link that
     precedes it in the DOM (see the module doc comment above) while staying
     in normal flow. A labelled link, not a bare icon: the operator could
     not tell what the icon was for, and a link nobody recognises is not a
     link. Quiet by default, link-coloured on hover so it still defers to
     the card's one primary click. */
  .agent-card__observability {
    position: relative;
    display: inline-flex;
    flex-shrink: 0;
    align-items: center;
    gap: 0.3rem;
    margin-left: auto;
    padding: var(--space-2xs) var(--space-xs);
    border-radius: var(--radius-sm);
    color: var(--link);
    font-size: 0.8rem;
    text-decoration: none;
    white-space: nowrap;
  }

  .agent-card__observability:hover,
  .agent-card__observability:focus-visible {
    background: var(--accent-soft);
    text-decoration: underline;
  }

  .agent-card__icon {
    width: 1rem;
    height: 1rem;
  }

</style>
