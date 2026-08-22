<script lang="ts">
  // The "Request colleague access" affordance (access-control arc, Task
  // C2): shown on ANOTHER agent's detail page when it's visible to the
  // caller but not theirs to administer (AgentDetail.svelte's own probe of
  // `GET /api/agents/{name}/invites` 403ing is what decides that, never
  // anything this component re-derives). Lets the caller pick one of THEIR
  // OWN agents (created_by matches their own verified email, the same
  // convention the server's `is_owned` uses) and request cross-group
  // colleague access from it to `toAgentName`.
  //
  // The picker deliberately reads the same `agentsQuery` the dashboard
  // uses -- that query is already server-filtered to what the caller may
  // SEE (access-control Task B2), and ownership is a strict subset of
  // visibility, so no separate endpoint is needed just to list "my
  // agents." Filtering to owned-only happens here, client-side, purely for
  // display -- the server's own owner-or-operator gate on `POST
  // /api/agents/{name}/invites` is the real enforcement, same "UI reflects,
  // server decides" discipline as everywhere else in this room.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { errorMessage } from "../lib/api";
  import { agentsQuery, createInviteMutation, whoamiQuery } from "../lib/queries";

  let { toAgentName }: { toAgentName: string } = $props();

  const agents = createQuery(agentsQuery());
  const whoami = createQuery(whoamiQuery());

  const myAgents = $derived.by(() => {
    const email = $whoami.data?.email;
    if (!email) {
      return [];
    }
    const lower = email.toLowerCase();
    return ($agents.data?.agents ?? []).filter(
      (agent) => agent.created_by !== null && agent.created_by.toLowerCase() === lower,
    );
  });

  let selectedFromAgent = $state<string | null>(null);

  // Keeps the picker's selection pointed at a real option: seeds it once
  // the owned-agents list arrives, and re-seeds it if a stale pick (e.g.
  // the list changed underneath it) no longer names one of them.
  $effect(() => {
    if (myAgents.length === 0) {
      selectedFromAgent = null;
      return;
    }
    if (selectedFromAgent === null || !myAgents.some((a) => a.name === selectedFromAgent)) {
      selectedFromAgent = myAgents[0].name;
    }
  });

  const queryClient = useQueryClient();
  const requestAccess = createMutation(createInviteMutation(queryClient));

  let requestError = $state<string | null>(null);
  let requestSent = $state(false);

  function handleSubmit(event: SubmitEvent): void {
    event.preventDefault();
    if (selectedFromAgent === null || $requestAccess.isPending) {
      return;
    }
    requestError = null;
    requestSent = false;
    $requestAccess.mutate(
      { fromAgent: selectedFromAgent, toAgent: toAgentName },
      {
        onSuccess: () => {
          requestSent = true;
        },
        onError: (err: unknown) => {
          requestError = errorMessage(err, "Couldn't send that request. Try again.");
        },
      },
    );
  }
</script>

<div class="request-access" aria-labelledby="request-access-heading">
  <h2 id="request-access-heading" class="request-access__heading">Request colleague access</h2>

  {#if $whoami.isPending || $agents.isPending}
    <p class="request-access__status">Loading your agents…</p>
  {:else if myAgents.length === 0}
    <p class="request-access__copy">
      You need an agent of your own before you can request access. Create one, then come back
      here to ask this agent's owner for a colleague introduction.
    </p>
  {:else}
    <p class="request-access__copy">
      Ask this agent's owner to let one of your agents talk to it.
    </p>
    <form class="request-access__form" onsubmit={handleSubmit}>
      <label class="request-access__label" for="request-access-picker">From your agent</label>
      <select
        id="request-access-picker"
        class="request-access__picker"
        bind:value={selectedFromAgent}
      >
        {#each myAgents as agent (agent.name)}
          <option value={agent.name}>{agent.name}</option>
        {/each}
      </select>

      {#if requestError}
        <p class="request-access__error" role="alert">{requestError}</p>
      {:else if requestSent}
        <p class="request-access__note">Request sent. Its owner will see it on their Access tab.</p>
      {/if}

      <button type="submit" class="request-access__submit">
        Request colleague access
      </button>
    </form>
  {/if}
</div>

<style>
  .request-access {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .request-access__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .request-access__copy,
  .request-access__status {
    margin: 0;
    color: var(--muted);
    font-size: 0.9rem;
  }

  .request-access__form {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
  }

  .request-access__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.85rem;
  }

  .request-access__picker {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .request-access__submit {
    align-self: flex-start;
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .request-access__submit:hover {
    filter: brightness(1.05);
  }

  .request-access__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .request-access__note {
    margin: 0;
    color: var(--ok);
    font-size: 0.85rem;
  }
</style>
