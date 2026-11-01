<script lang="ts">
  // Agent to agent: the Harness section (issue #368) that used to be the
  // Other agents block of the Access tab. Which agents from other teams this
  // one may ask for help, and who has asked to reach it. Agents in the same
  // group are paired by the room on its own and need nothing here.
  //
  // The table holds every agent fact in one place: incoming pending
  // requests (Accept/Decline), same-team pairs (automatic, not revocable)
  // and accepted cross-group pairs (Revoke). A paired row's status says
  // what the pair buys, "Can join shared threads" (issue #289); the chip
  // beside it keeps the distinction between the automatic same-team pair
  // and an accepted request.
  //
  // With the permission service on (issue #310; `GET /api/agents/{name}/
  // access` answers), asking another team's agent is a permission: the
  // picker posts an access request for `a2a_ask` on the target agent, the
  // row waits for Review, and an approved one is a pair the server reports
  // with `via: "grant"`. With it off (the access route is 404) the picker
  // sends the invite it always did.
  //
  // Mounted by HarnessPanel only when the caller may administer the agent
  // (the page's own invites probe answered 200, the same gate the Access
  // tab has); the routes below answer nobody else.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { errorMessage } from "../lib/api";
  import {
    acceptInviteMutation,
    agentAccessQuery,
    createInviteMutation,
    declineInviteMutation,
    invitesQuery,
    pairsQuery,
    requestAgentAccessMutation,
    requestableQuery,
    revokeInviteMutation,
  } from "../lib/queries";
  import ErrorState from "./ErrorState.svelte";
  import Skeleton from "./Skeleton.svelte";

  let { agentName, frozen = false }: { agentName: string; frozen?: boolean } = $props();

  const queryClient = useQueryClient();
  // agentName is read once: AgentDetail remounts every panel via
  // `{#key route.name}` when the routed agent changes.
  // svelte-ignore state_referenced_locally
  const invites = createQuery(() => (invitesQuery(agentName)));
  // svelte-ignore state_referenced_locally
  const requestable = createQuery(() => (requestableQuery(agentName)));
  // svelte-ignore state_referenced_locally
  const acceptMutation = createMutation(() => (acceptInviteMutation(queryClient, agentName)));
  // svelte-ignore state_referenced_locally
  const declineMutation = createMutation(() => (declineInviteMutation(queryClient, agentName)));
  // svelte-ignore state_referenced_locally
  const revokeMutation = createMutation(() => (revokeInviteMutation(queryClient, agentName)));
  const requestAccess = createMutation(() => (createInviteMutation(queryClient)));
  // The permission service's view of this agent; a 404 means it is off for
  // the room and the invite path stands.
  // svelte-ignore state_referenced_locally
  const access = createQuery(() => (agentAccessQuery(agentName)));
  const accessOn = $derived(access.isSuccess);
  // svelte-ignore state_referenced_locally
  const pairs = createQuery(() => ({ ...pairsQuery(agentName), enabled: access.isSuccess }));
  // svelte-ignore state_referenced_locally
  const askRequest = createMutation(() => (requestAgentAccessMutation(queryClient, agentName)));

  const ASK_PERMISSION = "a2a_ask";
  /** Ask requests waiting in Review, one row per target agent. */
  const waitingAsks = $derived(
    (access.data?.pending ?? []).filter((r) => r.permissions.includes(ASK_PERMISSION)),
  );
  /** Grant-backed pairs: this agent's approved asks (its own grants), plus
   * any pair the server reports as a grant from the other side. */
  const grantPairs = $derived.by(() => {
    const names = new Set<string>();
    for (const g of access.data?.grants ?? []) {
      if (g.permission === ASK_PERMISSION) names.add(g.integration);
    }
    for (const p of pairs.data?.pairs ?? []) {
      if (p.via === "grant") names.add(p.name);
    }
    return [...names].sort();
  });

  const incomingPending = $derived(
    (invites.data?.incoming ?? []).filter((invite) => invite.status === "pending"),
  );
  const teamPairs = $derived(invites.data?.team_pairs ?? []);
  const acceptedPairs = $derived(
    [...(invites.data?.incoming ?? []), ...(invites.data?.outgoing ?? [])].filter(
      (invite) => invite.status === "accepted",
    ),
  );
  const hasPairs = $derived(teamPairs.length > 0 || acceptedPairs.length > 0 || grantPairs.length > 0);
  const agentRowCount = $derived(
    incomingPending.length + teamPairs.length + acceptedPairs.length + waitingAsks.length + grantPairs.length,
  );

  // The picker is a disclosure, shut on arrival: a section that greets you
  // with an open form reads as work waiting to be done, and this is not.
  let pickerOpen = $state(false);
  let inviteActionError = $state<string | null>(null);

  function handleAccept(id: number): void {
    if (acceptMutation.isPending) {
      return;
    }
    inviteActionError = null;
    acceptMutation.mutate(id, {
      onError: (err: unknown) => {
        inviteActionError = errorMessage(err, "Couldn't accept that request. Try again.");
      },
    });
  }

  function handleDecline(id: number): void {
    if (declineMutation.isPending) {
      return;
    }
    inviteActionError = null;
    declineMutation.mutate(id, {
      onError: (err: unknown) => {
        inviteActionError = errorMessage(err, "Couldn't decline that request. Try again.");
      },
    });
  }

  function handleRevoke(id: number): void {
    if (revokeMutation.isPending) {
      return;
    }
    inviteActionError = null;
    revokeMutation.mutate(id, {
      onError: (err: unknown) => {
        inviteActionError = errorMessage(err, "Couldn't revoke that. Try again.");
      },
    });
  }

  /** One requestable row's displayed purpose: its capability sentence, else
   * its purpose tags joined into a plain line, else an honest "nothing
   * written yet" -- same three-tier fallback the requestable route's own
   * docstring promises the UI. */
  function purposeText(candidate: { capability: string | null; purpose_tags: string[] }): string {
    if (candidate.capability) {
      return candidate.capability;
    }
    if (candidate.purpose_tags.length > 0) {
      return candidate.purpose_tags.join(", ");
    }
    return "No description yet.";
  }

  function handleRequestAccess(toAgentName: string): void {
    if (requestAccess.isPending || askRequest.isPending) {
      return;
    }
    inviteActionError = null;
    if (accessOn) {
      askRequest.mutate(
        { integration: toAgentName, permissions: [ASK_PERMISSION], note: null },
        {
          onError: (err: unknown) => {
            inviteActionError = errorMessage(err, "Couldn't send that request. Try again.");
          },
        },
      );
      return;
    }
    requestAccess.mutate(
      { fromAgent: agentName, toAgent: toAgentName },
      {
        onError: (err: unknown) => {
          inviteActionError = errorMessage(err, "Couldn't send that request. Try again.");
        },
      },
    );
  }
</script>

<section class="panel" aria-labelledby="a2a-heading">
  <div class="panel__header">
    <h2 id="a2a-heading" class="panel__heading">Agent to agent</h2>
    <p class="panel__lead">
      Which agents from other teams this one may ask for help, and who has asked to reach it.
      Agents in the same group need nothing here.
    </p>
  </div>

  <!-- The disclosure button stays usable on a frozen agent: it reveals a
       form, it never changes anything. Every control inside it is disabled
       while frozen, which is where the freeze actually bites. -->
  <div class="panel__toolbar">
    <div class="panel__tools">
      <button
        type="button"
        class="btn"
        aria-expanded={pickerOpen}
        onclick={() => (pickerOpen = !pickerOpen)}
      >
        Request another team's agent
      </button>
    </div>
  </div>

  {#if pickerOpen}
    <div class="panel__drawer a2a__picker">
      {#if requestable.isPending}
        <Skeleton rows={2} />
      {:else if requestable.isError}
        <ErrorState
          message={errorMessage(requestable.error, "Couldn't load other agents.")}
          onretry={() => requestable.refetch()}
        />
      {:else if (requestable.data?.requestable ?? []).length === 0 && (requestable.data?.already ?? []).length === 0 && (requestable.data?.not_ready ?? []).length === 0}
        <p class="panel__note">
          Nobody to ask: every other agent is already reachable (same group, listed
          below), or there is no other agent yet.
        </p>
      {:else}
        <ul class="a2a__list">
          {#each requestable.data?.requestable ?? [] as candidate (candidate.name)}
            <li class="a2a__candidate">
              <div class="a2a__candidate-info">
                <span class="a2a__row-name">{candidate.name}</span>
                {#if candidate.group}
                  <span class="a2a__chip">{candidate.group}</span>
                {/if}
                <p class="panel__note">{purposeText(candidate)}</p>
              </div>
              <button
                type="button"
                class="btn"
                disabled={frozen || requestAccess.isPending || askRequest.isPending}
                aria-label={`Request access to ${candidate.name}`}
                onclick={() => handleRequestAccess(candidate.name)}
              >
                Request access
              </button>
            </li>
          {/each}
          {#each requestable.data?.already ?? [] as entry (entry.name)}
            <li class="a2a__candidate">
              <span class="a2a__row-name">{entry.name}</span>
              <span class="a2a__chip a2a__chip--state">{entry.status}</span>
            </li>
          {/each}
          {#each requestable.data?.not_ready ?? [] as entry (entry.name)}
            <li class="a2a__candidate a2a__candidate--muted">
              <span class="a2a__row-name">{entry.name}</span>
              {#if entry.group}
                <span class="a2a__chip">{entry.group}</span>
              {/if}
              <span class="a2a__chip a2a__chip--state">
                not ready ({entry.status})
              </span>
            </li>
          {/each}
        </ul>
        {#if (requestable.data?.requestable ?? []).length === 0}
          <p class="panel__note">
            Agents in the same group are already reachable and listed below.
          </p>
        {/if}
      {/if}
    </div>
  {/if}

  {#if invites.isPending}
    <Skeleton rows={2} />
  {:else if invites.isError}
    <ErrorState
      message={errorMessage(invites.error, "Couldn't load colleague requests.")}
      onretry={() => invites.refetch()}
    />
  {:else}
    {#if inviteActionError}
      <p class="panel__note panel__note--fail" role="alert">{inviteActionError}</p>
    {/if}
    <div class="table-card">
      <table class="panel__table">
        <thead>
          <tr>
            <th scope="col">Agent</th>
            <th scope="col" class="a2a__col-role">Status</th>
            <th scope="col" class="a2a__col-action">
              <span class="panel__sr">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {#if agentRowCount === 0}
            <tr>
              <td class="panel__empty" colspan="3">No requests or pairs yet.</td>
            </tr>
          {/if}
          {#each incomingPending as invite (invite.id)}
            <tr class="a2a__row">
              <td><span class="a2a__row-name">{invite.from_agent}</span></td>
              <td><span class="panel__meta">Wants to talk to yours</span></td>
              <td class="panel__cell--right">
                <div class="a2a__actions">
                  <button
                    type="button"
                    class="btn"
                    disabled={frozen || acceptMutation.isPending}
                    aria-label={`Accept ${invite.from_agent}`}
                    onclick={() => handleAccept(invite.id)}
                  >
                    Accept
                  </button>
                  <button
                    type="button"
                    class="btn btn--quiet btn--danger"
                    disabled={frozen || declineMutation.isPending}
                    aria-label={`Decline ${invite.from_agent}`}
                    onclick={() => handleDecline(invite.id)}
                  >
                    Decline
                  </button>
                </div>
              </td>
            </tr>
          {/each}
          <!-- Same-team pairs first, and never revocable: the room pairs them
               on its own, so there is no grant here for anyone to take back. -->
          {#each teamPairs as pair (pair.name)}
            <tr class="a2a__row">
              <td><span class="a2a__row-name">{pair.name}</span></td>
              <td><span class="panel__meta">Can join shared threads</span></td>
              <td class="panel__cell--right">
                <span class="a2a__chip">Same team ({pair.group})</span>
              </td>
            </tr>
          {/each}
          {#each waitingAsks as ask (ask.id)}
            <tr class="a2a__row">
              <td><span class="a2a__row-name">{ask.integration}</span></td>
              <td><span class="panel__meta">Waiting for review</span></td>
              <td class="panel__cell--right">
                <span class="a2a__chip">Requested</span>
              </td>
            </tr>
          {/each}
          {#each grantPairs as name (name)}
            <tr class="a2a__row">
              <td><span class="a2a__row-name">{name}</span></td>
              <td><span class="panel__meta">Can join shared threads</span></td>
              <td class="panel__cell--right">
                <span class="a2a__chip">Approved in Review</span>
              </td>
            </tr>
          {/each}
          {#each acceptedPairs as invite (invite.id)}
            <tr class="a2a__row">
              <td>
                <span class="a2a__row-name">
                  {invite.from_agent} and {invite.to_agent.name}
                </span>
              </td>
              <td><span class="panel__meta">Can join shared threads</span></td>
              <td class="panel__cell--right">
                <div class="a2a__revoke-group">
                  <span class="a2a__chip">Accepted request</span>
                  <button
                    type="button"
                    class="btn btn--quiet btn--danger"
                    disabled={frozen || revokeMutation.isPending}
                    aria-label={`Revoke ${invite.from_agent} and ${invite.to_agent.name}`}
                    onclick={() => handleRevoke(invite.id)}
                  >
                    Revoke
                  </button>
                  <span class="a2a__hint">
                    Revoking takes effect within a few minutes.
                  </span>
                </div>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if hasPairs}
      <p class="panel__note">Pairs are live as soon as they appear here.</p>
    {/if}
  {/if}
</section>

<style>
  .a2a__picker {
    width: 100%;
  }

  .a2a__list {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    width: 100%;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .a2a__candidate {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-xs);
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
  }

  .a2a__candidate-info {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 0;
  }

  .a2a__row-name {
    overflow-wrap: anywhere;
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.85rem;
  }

  .a2a__chip {
    align-self: flex-start;
    padding: 0.1rem var(--space-xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-pill);
    color: var(--muted);
    font-size: 0.7rem;
    white-space: nowrap;
  }

  .a2a__chip--state {
    text-transform: capitalize;
  }

  .a2a__actions,
  .a2a__revoke-group {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: var(--space-xs);
  }

  .a2a__revoke-group {
    flex-wrap: wrap;
  }

  .a2a__hint {
    color: var(--muted);
    font-size: 0.75rem;
  }

  .a2a__col-role {
    width: 12rem;
  }

  .a2a__col-action {
    width: 16rem;
  }
</style>
