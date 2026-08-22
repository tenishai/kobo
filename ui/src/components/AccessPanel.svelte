<script lang="ts">
  // The Access tab (access-control arc, Task C2, refined by the access-tab
  // refinement wave): who can SEE this agent (People: the group fact line,
  // plus per-user grants via a directory-backed autocomplete) and which
  // other agents may talk to it across a group boundary (Agents: incoming
  // requests, a browsable outgoing picker, and the active pairs). Mounted by
  // AgentDetail only once its own probe of `GET /api/agents/{name}/invites`
  // has already succeeded (see tabs.ts's `visibleTabs` doc comment) — this
  // component never re-derives that eligibility itself, it just renders
  // what the two queries below say, same "server decides, UI reflects"
  // discipline every other gated surface in this room follows.
  //
  // Agents section, three groupings: incoming PENDING invites ("Agent
  // requests to your agent" — Accept/Decline, the only state where an
  // action makes sense), a browsable outgoing picker ("Request access to
  // another team's agent" — every other requestable agent plus a Request
  // access button, and anything already asked rendered as a status chip
  // instead), and current pairs ("Active pairs" — auto-paired same-group
  // colleagues first, non-revocable, then accepted cross-group invites with
  // Revoke).
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { derived, writable } from "svelte/store";
  import { errorMessage, type Person } from "../lib/api";
  import { debounced } from "../lib/debounce";
  import {
    acceptInviteMutation,
    addGrantMutation,
    createInviteMutation,
    declineInviteMutation,
    grantsQuery,
    invitesQuery,
    peopleSearchQuery,
    removeGrantMutation,
    requestableQuery,
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
  const requestable = createQuery(requestableQuery(agentName));
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
  const requestAccess = createMutation(createInviteMutation(queryClient));

  const incomingPending = $derived(
    ($invites.data?.incoming ?? []).filter((invite) => invite.status === "pending"),
  );
  const teamPairs = $derived($invites.data?.team_pairs ?? []);
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
    if ($requestAccess.isPending) {
      return;
    }
    inviteActionError = null;
    $requestAccess.mutate(
      { fromAgent: agentName, toAgent: toAgentName },
      {
        onError: (err: unknown) => {
          inviteActionError = errorMessage(err, "Couldn't send that request. Try again.");
        },
      },
    );
  }

  // --- People: grants -------------------------------------------------------

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

  // --- People: directory-backed autocomplete (access-tab refinement wave) --
  //
  // Reuses `grantEmail` itself as the search term: the same input both
  // searches the directory and holds the value Add ultimately submits, so
  // picking a suggestion just overwrites it with that person's email — Add
  // grants exactly as before. Same debounced-into-a-writable-store idiom
  // CreateModal's own persona/skill search use (`personaSearchTerm`,
  // `nameSuggestionParams`): the installed svelte-query adapter only
  // re-derives a query's options from an actual Svelte store, never a
  // `$derived` value it would freeze at first render.
  const peopleSearchTerm = writable("");
  const peopleSearchOptions = derived(peopleSearchTerm, ($q) => peopleSearchQuery($q));
  const peopleResults = createQuery(peopleSearchOptions);

  const commitPeopleSearch = debounced((value: string) => {
    peopleSearchTerm.set(value);
  }, 250);

  $effect(() => {
    const value = grantEmail;
    commitPeopleSearch(value);
    return () => {
      commitPeopleSearch.cancel();
    };
  });

  // Dismissed right after a pick, so the just-picked email's own debounced
  // search (which will very likely match itself) doesn't reopen the
  // dropdown a quarter-second later. Reopened by any REAL keystroke —
  // `oninput` only fires on genuine typing, never on the programmatic
  // `grantEmail = person.email` assignment a pick makes (a plain state
  // write dispatches no DOM event).
  let suggestionsDismissed = $state(false);

  function handleGrantEmailTyped(): void {
    suggestionsDismissed = false;
  }

  const suggestions = $derived($peopleResults.data?.people ?? []);
  const directoryUnavailable = $derived($peopleResults.data?.unavailable === true);
  const showSuggestions = $derived(
    !suggestionsDismissed && grantEmail.trim().length >= 2 && suggestions.length > 0,
  );

  function pickPerson(person: Person): void {
    grantEmail = person.email;
    suggestionsDismissed = true;
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
        <div class="access-panel__autocomplete">
          <input
            id="access-grant-email"
            class="access-panel__input"
            type="text"
            autocomplete="off"
            bind:value={grantEmail}
            oninput={handleGrantEmailTyped}
            disabled={frozen}
          />
          {#if showSuggestions}
            <ul class="access-panel__suggestions" role="listbox">
              {#each suggestions as person (person.email)}
                <li role="option" aria-selected="false">
                  <button
                    type="button"
                    class="access-panel__suggestion"
                    onclick={() => pickPerson(person)}
                  >
                    {person.name} &lt;{person.email}&gt;
                  </button>
                </li>
              {/each}
            </ul>
          {/if}
        </div>
        <button type="submit" class="access-panel__grant-submit" disabled={frozen || !canAddGrant}>
          {$addGrant.isPending ? "Adding…" : "Add"}
        </button>
      </div>
      {#if directoryUnavailable}
        <p class="access-panel__note">
          Directory search is unavailable right now. You can still type an email directly.
        </p>
      {/if}
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
        <h3 class="access-panel__subheading">Agent requests to your agent</h3>
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
        <h3 class="access-panel__subheading">Request access to another team's agent</h3>
        {#if $requestable.isPending}
          <Skeleton rows={2} />
        {:else if $requestable.isError}
          <ErrorState
            message={errorMessage($requestable.error, "Couldn't load other agents.")}
            onretry={() => $requestable.refetch()}
          />
        {:else if ($requestable.data?.requestable ?? []).length === 0 && ($requestable.data?.already ?? []).length === 0}
          <EmptyState message="No other agents to request access from yet." />
        {:else}
          <ul class="access-panel__list">
            {#each $requestable.data?.requestable ?? [] as candidate (candidate.name)}
              <li class="access-panel__row">
                <div class="access-panel__requestable-info">
                  <span class="access-panel__row-name">{candidate.name}</span>
                  {#if candidate.group}
                    <span class="access-panel__group-chip">{candidate.group}</span>
                  {/if}
                  <p class="access-panel__purpose">{purposeText(candidate)}</p>
                </div>
                <button
                  type="button"
                  class="access-panel__request-button"
                  disabled={frozen || $requestAccess.isPending}
                  aria-label={`Request access to ${candidate.name}`}
                  onclick={() => handleRequestAccess(candidate.name)}
                >
                  Request access
                </button>
              </li>
            {/each}
            {#each $requestable.data?.already ?? [] as entry (entry.name)}
              <li class="access-panel__row">
                <span class="access-panel__row-name">{entry.name}</span>
                <span class="access-panel__state-chip">{entry.status}</span>
              </li>
            {/each}
          </ul>
        {/if}
      </div>

      <div class="access-panel__subsection">
        <h3 class="access-panel__subheading">Active pairs</h3>
        {#if teamPairs.length === 0 && acceptedPairs.length === 0}
          <EmptyState message="No cross-group pairs yet." />
        {:else}
          <p class="access-panel__fact">Pairs are live as soon as they appear here.</p>
          <ul class="access-panel__list">
            {#each teamPairs as pair (pair.name)}
              <li class="access-panel__row">
                <span class="access-panel__row-name">{pair.name} - same team ({pair.group})</span>
                <span class="access-panel__auto-chip">auto</span>
              </li>
            {/each}
            {#each acceptedPairs as invite (invite.id)}
              <li class="access-panel__row">
                <span class="access-panel__row-name">
                  {invite.from_agent} and {invite.to_agent.name}
                </span>
                <div class="access-panel__revoke-group">
                  <button
                    type="button"
                    class="access-panel__revoke"
                    disabled={frozen || $revokeMutation.isPending}
                    aria-label={`Revoke ${invite.from_agent} and ${invite.to_agent.name}`}
                    onclick={() => handleRevoke(invite.id)}
                  >
                    Revoke
                  </button>
                  <span class="access-panel__revoke-hint">
                    Revoking takes effect within a few minutes.
                  </span>
                </div>
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
    align-items: flex-start;
    gap: var(--space-xs);
  }

  .access-panel__autocomplete {
    position: relative;
    flex: 1;
    min-width: 0;
  }

  .access-panel__input {
    width: 100%;
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
    box-sizing: border-box;
  }

  .access-panel__input:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .access-panel__suggestions {
    position: absolute;
    z-index: 1;
    top: calc(100% + var(--space-2xs));
    left: 0;
    right: 0;
    display: flex;
    flex-direction: column;
    margin: 0;
    padding: var(--space-2xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    box-shadow: 0 4px 12px color-mix(in srgb, black 15%, transparent);
    list-style: none;
  }

  .access-panel__suggestion {
    width: 100%;
    padding: var(--space-2xs) var(--space-xs);
    border: none;
    border-radius: var(--radius-sm);
    background: none;
    color: var(--ink);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  .access-panel__suggestion:hover {
    background: var(--bg);
  }

  .access-panel__note {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
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

  .access-panel__requestable-info {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 0;
  }

  .access-panel__group-chip {
    align-self: flex-start;
    padding: 0.1rem var(--space-2xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.7rem;
    white-space: nowrap;
  }

  .access-panel__purpose {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .access-panel__request-button {
    flex-shrink: 0;
    padding: var(--space-2xs) var(--space-sm);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .access-panel__request-button:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .access-panel__request-button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
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

  .access-panel__revoke-group {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: var(--space-2xs);
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

  .access-panel__revoke-hint {
    margin: 0;
    color: var(--muted);
    font-size: 0.75rem;
    text-align: right;
  }

  .access-panel__row-remove:disabled,
  .access-panel__decline:disabled,
  .access-panel__accept:disabled,
  .access-panel__revoke:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .access-panel__state-chip,
  .access-panel__auto-chip {
    padding: 0.15rem var(--space-xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.75rem;
    text-transform: capitalize;
    white-space: nowrap;
  }
</style>
