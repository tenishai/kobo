<script lang="ts">
  // Where a connected agent stands, in the order things have to come true:
  // it is running, it is set up for the tool gateway, it takes messages
  // from Slack. The room builds every part of an agent it composes, so that
  // agent works or its checklist says why not; one that runs its team's own
  // code can be deployed and still be half in the room, and this is where
  // the page says which half. One sentence each, written by the server
  // (`kobo_api.connected_checks`) so a person's assistant reads the same
  // words. Nothing here is a control: what a line asks for is done where it
  // has always been done (a pull request, the Access tab).
  import { createQuery } from "@tanstack/svelte-query";
  import { agentChecksQuery } from "../lib/queries";
  import { relativeTime } from "../lib/time";

  let { agentName }: { agentName: string } = $props();

  const checks = createQuery(() => agentChecksQuery(agentName));

  const LABELS: Record<string, string> = {
    running: "Deployed",
    gateway: "Tools",
    slack: "Slack",
  };
</script>

<!-- A read that failed draws nothing: the header never claims a standing it
     did not see, and the banner below already says when the agent is down. -->
{#if checks.data?.checks?.length}
  <ul class="checks" aria-label="Where this agent stands">
    {#each checks.data.checks as check (check.id)}
      <li class="checks__row">
        <span class={`checks__dot checks__dot--${check.state}`} aria-hidden="true"></span>
        <span class="checks__label">{LABELS[check.id] ?? check.id}</span>
        <span class="checks__line">
          {check.line}
          {#if check.pull_request}
            <a href={check.pull_request} target="_blank" rel="noreferrer">Open the pull request</a>
          {/if}
          {#if check.at}
            <span class="checks__at">{relativeTime(check.at)}</span>
          {/if}
        </span>
      </li>
    {/each}
  </ul>
{/if}

<style>
  .checks {
    display: grid;
    gap: var(--space-2xs);
    margin: var(--space-xs) 0 0;
    padding: 0;
    list-style: none;
    font-size: 0.88rem;
  }

  .checks__row {
    display: grid;
    grid-template-columns: 0.5rem 4.5rem 1fr;
    align-items: baseline;
    gap: var(--space-xs);
    color: var(--ink);
  }

  .checks__dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    align-self: center;
    background: var(--muted);
  }

  .checks__dot--ok {
    background: var(--ok);
  }

  .checks__dot--waiting {
    background: var(--warn);
  }

  .checks__dot--missing {
    background: var(--fail);
  }

  /* Nothing to report either way: a ring, not a fill. */
  .checks__dot--idle {
    background: transparent;
    box-shadow: inset 0 0 0 1.5px var(--muted);
  }

  .checks__label {
    font-weight: 600;
  }

  /* When it was last seen is metadata, the one grey thing on the row. */
  .checks__at {
    margin-left: var(--space-2xs);
    color: var(--muted);
    white-space: nowrap;
  }
</style>
