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
  import { ArrowUpRight } from "phosphor-svelte";
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
  // Issue #142: the nightly learnings pass failing night after night.
  let nightlyFailing = $derived((agent.system_tasks_failing ?? []).includes("learnings"));
  // Issue #147: a ready agent with no purpose tags publishes a directory
  // card that says nothing about what it is for. Say so here instead.
  let untagged = $derived(
    agent.status === "ready" && !connected && (agent.purpose_tags ?? []).length === 0,
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
      {:else}
        <!-- No portrait yet: the initial, so every card has a face. -->
        <span class="agent-card__avatar agent-card__avatar--initial" aria-hidden="true">
          {agent.name.slice(0, 1).toUpperCase()}
        </span>
      {/if}
      <span class="agent-card__name" title={agent.name}>{agent.name}</span>
    </span>
    <StatusBadge status={agent.status} health={agent.health} />
  </div>

  <!-- Two lines, always: a card that says "No description yet." is still
       the same height as the one beside it. The whole sentence rides on the
       title for anyone who wants the rest of it. -->
  <p
    class="agent-card__desc"
    class:agent-card__desc--empty={description === NO_DESCRIPTION}
    title={description}
  >
    {description}
  </p>

  {#if agent.group_name || connected || nightlyFailing || untagged}
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
      {#if nightlyFailing}
        <span class="agent-card__tag agent-card__warn" title="The nightly learnings pass keeps failing. Open the Learnings tab for the error.">learnings failing</span>
      {/if}
      {#if untagged}
        <span class="agent-card__tag agent-card__warn" title="Colleagues and the directory see no purpose for this agent. Add purpose tags with Edit details, in the menu beside its name.">no purpose tags</span>
      {/if}
    </div>
  {/if}

  {#snippet facts()}
    <p class="agent-card__meta">
      <span>{relativeTime(agent.created_at)}</span>
      <span class="agent-card__sep" aria-hidden="true">·</span>
      <!-- Agents made before the room recorded who made them have no
           author; say so rather than leave a hole in the row. -->
      <span class="agent-card__owner" title={agent.created_by ?? undefined}>Author: {agent.created_by ?? "Unknown"}</span>
    </p>
  {/snippet}

  <div class="agent-card__footer">
    <div class="agent-card__facts">
    {#if failed}
      <!-- What happened on the first line, where a ready card shows its
           model; the same real facts as every card under it (age, author).
           The list route carries no error text: the agent's page has it. -->
      <p class="agent-card__status-line agent-card__status-line--fail agent-card__error">Something went wrong</p>
      {@render facts()}
    {:else if waiting}
      <p class="agent-card__status-line agent-card__waiting">Waiting on a pull request</p>
      {@render facts()}
    {:else}
      <!-- The model gets a line of its own (2026-10-01): sharing one line
           with the age, the byline and the Observability link cut every
           label short. Two rows, always, so every card is the same height. -->
      <p class="agent-card__model" title={agent.model}>{agent.model_label}</p>
      {@render facts()}
    {/if}

    </div>

  <!-- A way out to another site, not a fact about the agent: a button in
       a fixed place at the right of the facts tile, beside the two lines,
       so a long run of tags never moves it. -->
    {#if dashboardUrl}
      <a
        class="agent-card__observability"
        href={dashboardUrl}
        target="_blank"
        rel="noreferrer"
        title={`Open ${agent.name}'s health dashboard in a new tab`}
      >
        <span class="agent-card__observability-label">Health</span>
        <ArrowUpRight class="agent-card__icon" size={12} weight="bold" aria-hidden="true" />
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
    gap: var(--space-sm);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
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

  .agent-card__avatar--initial {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: var(--accent-soft);
    color: var(--ink);
    font-family: var(--font-display-mono);
    font-size: 0.8rem;
    font-weight: 600;
  }

  /* The agent's name is the one identifier on the card: mono, and heavier
     than anything under it. */
  .agent-card__name {
    overflow: hidden;
    color: var(--ink);
    font-family: var(--font-display-mono);
    font-size: 1.05rem;
    font-weight: 600;
    letter-spacing: -0.01em;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Two lines, always: a short description still reserves the second
     line, so every card in a row lines its tags and facts up. Anything
     longer is clipped with an ellipsis and rides on the title. */
  .agent-card__desc {
    display: -webkit-box;
    min-height: calc(2 * 1.45em);
    margin: 0;
    overflow: hidden;
    color: var(--muted);
    font-size: 0.82rem;
    line-height: 1.45;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
  }

  .agent-card__desc--empty {
    color: var(--muted);
  }

  .agent-card__tags {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2xs);
  }

  /* Quieter than the name above them: facts about the agent, not part of
     its identity. */
  .agent-card__tag {
    display: inline-flex;
    flex-shrink: 0;
    padding: 0.15rem var(--space-xs);
    border: 1px solid transparent;
    border-radius: var(--radius-pill);
    background: var(--accent-soft);
    color: var(--ink);
    font-size: 0.72rem;
    font-weight: 500;
    white-space: nowrap;
  }

  .agent-card__warn {
    color: var(--warn);
  }

  /* Pushed to the bottom, so two cards with different amounts to say still
     agree on where the quiet line sits. */
  /* Two rows: the first line (model, or what went wrong) with the Health
     button beside it, then the age and author across the full width, so
     the author's email is not cut short by the button. */
  .agent-card__footer {
    display: grid;
    min-width: 0;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--space-2xs) var(--space-xs);
    margin-top: auto;
    padding: var(--space-sm) var(--space-md);
    border-radius: 0;
    background: var(--accent-soft);
  }

  .agent-card__facts {
    display: contents;
  }

  .agent-card__footer > .agent-card__facts > :global(:first-child) {
    grid-column: 1;
    grid-row: 1;
  }

  .agent-card__footer :global(.agent-card__meta) {
    grid-column: 1 / -1;
    grid-row: 2;
  }

  .agent-card__meta {
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

  /* A failed or waiting card's one sentence: a full line of its own, in the
     place a ready card shows its model. */
  .agent-card__status-line {
    flex: 1 0 100%;
    margin: 0;
    overflow: hidden;
    color: var(--ink);
    font-size: 0.8rem;
    font-weight: 500;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .agent-card__status-line--fail {
    color: var(--fail);
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

  /* Positioned (not static) so it paints above the stretched link that
     precedes it in the DOM (see the module doc comment above) while staying
     in normal flow. A labelled link, not a bare icon: the operator could
     not tell what the icon was for, and a link nobody recognises is not a
     link. Quiet by default, link-coloured on hover so it still defers to
     the card's one primary click. */
  /* Positioned (not static) so it paints above the stretched link that
     precedes it in the DOM while staying in normal flow. An outline pill
     at the right end of the tag row: it leaves the room, so it looks like
     a button and carries an arrow out, not a status colour. */
  /* Positioned (not static) so it paints above the stretched link that
     precedes it in the DOM. A quiet secondary button: white on the grey
     facts tile with a thin border, so it is findable without pulling the
     eye away from the agent's name and status. */
  .agent-card__observability {
    position: relative;
    display: inline-flex;
    flex-shrink: 0;
    align-items: center;
    grid-column: 2;
    grid-row: 1;
    gap: 0.3rem;
    padding: 0.2rem var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-pill);
    background: var(--panel);
    color: var(--ink);
    font-size: 0.75rem;
    font-weight: 500;
    letter-spacing: var(--label-tracking);
    text-transform: var(--label-case);
    text-decoration: none;
    white-space: nowrap;
    transition: background 0.15s ease, border-color 0.15s ease;
  }

  .agent-card__observability:hover,
  .agent-card__observability:focus-visible {
    border-color: var(--ring);
    background: var(--accent-soft);
  }

  .agent-card__observability :global(.agent-card__icon) {
    flex-shrink: 0;
  }


</style>
