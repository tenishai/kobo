<script lang="ts">
  // The Access tab (access-control arc, Task C2): who can SEE this agent
  // (People: the group fact line, plus per-user grants) and which other
  // agents may talk to it across a group boundary (Agents: incoming
  // requests, outgoing requests, and the active accepted pairs). Mounted by
  // AgentDetail only once its own probe of `GET /api/agents/{name}/invites`
  // has already succeeded (see tabs.ts's `visibleTabs` doc comment) — this
  // component never re-derives that eligibility itself, it just renders
  // what the two queries below say, same "server decides, UI reflects"
  // discipline every other gated surface in this room follows.
  //
  // Three invite groupings, matching the design spec's own three bullets
  // for the Access tab (not a single flat list): incoming PENDING invites
  // (Accept/Decline, the only state where an action makes sense), outgoing
  // invites of any status (a state chip only — cancelling a pending
  // outgoing request isn't wired here, matching the spec's own split
  // between "outgoing requests (state)" and "current allow-pairs, revoke"),
  // and accepted pairs from EITHER direction (Revoke, since either side may
  // walk away from an arrangement it already has standing on, mirroring
  // the server's own `DELETE /api/invites/{id}` rule).
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { errorMessage } from "../lib/api";
  import {
    acceptInviteMutation,
    addGrantMutation,
    declineInviteMutation,
    grantsQuery,
    invitesQuery,
    removeGrantMutation,
    revokeInviteMutation,
  } from "../lib/queries";
  import EmptyState from "./EmptyState.svelte";
  import ErrorState from "./ErrorState.svelte";
  import Skeleton from "./Skeleton.svelte";

  let {
    agentName,
    groupName,
    frozen = false,
  }: {
    agentName: string;
    groupName: string | null;
    frozen?: boolean;
  } = $props();

  const queryClient = useQueryClient();
  // agentName is read once here rather than tracked reactively: AgentDetail
  // (the only caller) remounts every panel via `{#key route.name}` whenever
  // the routed agent changes, same reasoning as every other panel in this
  // room (ChatPanel/SlackPanel/ToolingPanel).
  // svelte-ignore state_referenced_locally
  const invites = createQuery(invitesQuery(agentName));
  // svelte-ignore state_referenced_locally
  const grants = createQuery(grantsQuery(agentName));
  // svelte-ignore state_referenced_locally
  const acceptMutation = createMutation(acceptInviteMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const declineMutation = createMutation(declineInviteMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const revokeMutation = createMutation(revokeInviteMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const addGrant = createMutation(addGrantMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const removeGrant = createMutation(removeGrantMutation(queryClient, agentName));

  const incomingPending = $derived(
    ($invites.data?.incoming ?? []).filter((invite) => invite.status === "pending"),
  );
  const outgoingInvites = $derived($invites.data?.outgoing ?? []);
  const acceptedPairs = $derived(
    [...($invites.data?.incoming ?? []), ...($invites.data?.outgoing ?? [])].filter(
      (invite) => invite.status === "accepted",
    ),
  );

  let inviteActionError = $state<string | null>(null);

  function handleAccept(id: number): void {
    if ($acceptMutation.isPending) {
      return;
    }
    inviteActionError = null;
    $acceptMutation.mutate(id, {
      onError: (err: unknown) => {
        inviteActionError = errorMessage(err, "Couldn't accept that request. Try again.");
      },
    });
  }

  function handleDecline(id: number): void {
    if ($declineMutation.isPending) {
      return;
    }
    inviteActionError = null;
    $declineMutation.mutate(id, {
      onError: (err: unknown) => {
        inviteActionError = errorMessage(err, "Couldn't decline that request. Try again.");
      },
    });
  }

  function handleRevoke(id: number): void {
    if ($revokeMutation.isPending) {
      return;
    }
    inviteActionError = null;
    $revokeMutation.mutate(id, {
      onError: (err: unknown) => {
        inviteActionError = errorMessage(err, "Couldn't revoke that. Try again.");
      },
    });
  }

  // --- People: grants -----------------------------------------------------

  let grantEmail = $state("");
  let grantError = $state<string | null>(null);

  const canAddGrant = $derived(!frozen && !$addGrant.isPending && grantEmail.trim().length > 0);

  function handleAddGrant(event: SubmitEvent): void {
    event.preventDefault();
    if (!canAddGrant) {
      return;
    }
    const email = grantEmail.trim();
    grantError = null;
    $addGrant.mutate(email, {
      onSuccess: () => {
        grantEmail = "";
      },
      onError: (err: unknown) => {
        grantError = errorMessage(err, "Couldn't add that person. Try again.");
      },
    });
  }

  function handleRemoveGrant(email: string): void {
    if ($removeGrant.isPending) {
      return;
    }
    grantError = null;
    $removeGrant.mutate(email, {
      onError: (err: unknown) => {
        grantError = errorMessage(err, "Couldn't remove that person. Try again.");
      },
    });
  }
</script>

<div class="access-panel">
  <section class="access-panel__section" aria-labelledby="access-people-heading">
    <h2 id="access-people-heading" class="access-panel__heading">People</h2>

    {#if groupName}
      <p class="access-panel__fact">Everyone in {groupName} can see this agent.</p>
    {:else}
      <p class="access-panel__fact">
        This agent is not in a group. Only its owner, operators, and anyone granted access below can see it.
      </p>
    {/if}

    <form class="access-panel__grant-form" onsubmit={handleAddGrant}>
      <label class="access-panel__label" for="access-grant-email">Add someone by email</label>
      <div class="access-panel__grant-row">
        <input
          id="access-grant-email"
          class="access-panel__input"
          type="email"
          autocomplete="off"
          bind:value={grantEmail}
          disabled={frozen}
        />
        <button type="submit" class="access-panel__grant-submit" disabled={frozen || !canAddGrant}>
          {$addGrant.isPending ? "Adding…" : "Add"}
        </button>
      </div>
      {#if grantError}
        <p class="access-panel__error" role="alert">{grantError}</p>
      {/if}
    </form>

    {#if $grants.isPending}
      <Skeleton rows={2} />
    {:else if $grants.isError}
      <ErrorState
        message={errorMessage($grants.error, "Couldn't load who has access.")}
        onretry={() => $grants.refetch()}
      />
    {:else if $grants.data.length === 0}
      <EmptyState message="Nobody has been individually granted access yet." />
    {:else}
      <ul class="access-panel__list">
        {#each $grants.data as grant (grant.email)}
          <li class="access-panel__row">
            <span class="access-panel__row-name">{grant.email}</span>
            <button
              type="button"
              class="access-panel__row-remove"
              disabled={frozen || $removeGrant.isPending}
              aria-label={`Remove ${grant.email}`}
              onclick={() => handleRemoveGrant(grant.email)}
            >
              Remove
            </button>
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  <section class="access-panel__section" aria-labelledby="access-agents-heading">
    <h2 id="access-agents-heading" class="access-panel__heading">Agents</h2>

    {#if $invites.isPending}
      <Skeleton rows={2} />
    {:else if $invites.isError}
      <ErrorState
        message={errorMessage($invites.error, "Couldn't load colleague requests.")}
        onretry={() => $invites.refetch()}
      />
    {:else}
      {#if inviteActionError}
        <p class="access-panel__error" role="alert">{inviteActionError}</p>
      {/if}

      <div class="access-panel__subsection">
        <h3 class="access-panel__subheading">Incoming requests</h3>
        {#if incomingPending.length === 0}
          <EmptyState message="No pending requests right now." />
        {:else}
          <ul class="access-panel__list">
            {#each incomingPending as invite (invite.id)}
              <li class="access-panel__row">
                <span class="access-panel__row-name">{invite.from_agent}</span>
                <div class="access-panel__row-actions">
                  <button
                    type="button"
                    class="access-panel__accept"
                    disabled={frozen || $acceptMutation.isPending}
                    aria-label={`Accept ${invite.from_agent}`}
                    onclick={() => handleAccept(invite.id)}
                  >
                    Accept
                  </button>
                  <button
                    type="button"
                    class="access-panel__decline"
                    disabled={frozen || $declineMutation.isPending}
                    aria-label={`Decline ${invite.from_agent}`}
                    onclick={() => handleDecline(invite.id)}
                  >
                    Decline
                  </button>
                </div>
              </li>
            {/each}
          </ul>
        {/if}
      </div>

      <div class="access-panel__subsection">
        <h3 class="access-panel__subheading">Outgoing requests</h3>
        {#if outgoingInvites.length === 0}
          <EmptyState message="No outgoing requests yet." />
        {:else}
          <ul class="access-panel__list">
            {#each outgoingInvites as invite (invite.id)}
              <li class="access-panel__row">
                <span class="access-panel__row-name">{invite.to_agent.name}</span>
                <span class="access-panel__state-chip">{invite.status}</span>
              </li>
            {/each}
          </ul>
        {/if}
      </div>

      <div class="access-panel__subsection">
        <h3 class="access-panel__subheading">Active pairs</h3>
        {#if acceptedPairs.length === 0}
          <EmptyState message="No cross-group pairs yet." />
        {:else}
          <p class="access-panel__fact">Takes effect within a few minutes.</p>
          <ul class="access-panel__list">
            {#each acceptedPairs as invite (invite.id)}
              <li class="access-panel__row">
                <span class="access-panel__row-name">
                  {invite.from_agent} and {invite.to_agent.name}
                </span>
                <button
                  type="button"
                  class="access-panel__revoke"
                  disabled={frozen || $revokeMutation.isPending}
                  aria-label={`Revoke ${invite.from_agent} and ${invite.to_agent.name}`}
                  onclick={() => handleRevoke(invite.id)}
                >
                  Revoke
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    {/if}
  </section>
</div>

<style>
  .access-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
  }

  .access-panel__section {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .access-panel__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .access-panel__fact {
    margin: 0;
    color: var(--muted);
    font-size: 0.9rem;
  }

  .access-panel__grant-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .access-panel__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.85rem;
  }

  .access-panel__grant-row {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
  }

  .access-panel__input {
    flex: 1;
    min-width: 0;
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .access-panel__input:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .access-panel__grant-submit {
    flex-shrink: 0;
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .access-panel__grant-submit:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .access-panel__grant-submit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .access-panel__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .access-panel__subsection {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .access-panel__subheading {
    margin: 0;
    color: var(--muted);
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .access-panel__list {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .access-panel__row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
  }

  .access-panel__row-name {
    overflow-wrap: anywhere;
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.9rem;
  }

  .access-panel__row-actions {
    display: flex;
    gap: var(--space-xs);
  }

  .access-panel__row-remove,
  .access-panel__decline {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--muted);
    cursor: pointer;
    white-space: nowrap;
  }

  .access-panel__row-remove:hover:not(:disabled),
  .access-panel__decline:hover:not(:disabled) {
    border-color: var(--fail);
    color: var(--fail);
  }

  .access-panel__accept {
    padding: var(--space-2xs) var(--space-sm);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .access-panel__accept:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .access-panel__revoke {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--fail);
    cursor: pointer;
    white-space: nowrap;
  }

  .access-panel__revoke:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .access-panel__row-remove:disabled,
  .access-panel__decline:disabled,
  .access-panel__accept:disabled,
  .access-panel__revoke:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .access-panel__state-chip {
    padding: 0.15rem var(--space-xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.75rem;
    text-transform: capitalize;
    white-space: nowrap;
  }
</style>
