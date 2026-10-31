<script lang="ts">
  // The Access tab (access-control arc, Task C2, refined by the access-tab
  // refinement wave, reshaped to the shared panel idiom, split in two by
  // issue #309): one line about visibility, then two sections with their
  // own heading, sentence and toolbar. People: who can see or manage this
  // agent besides its owner and the operators, each with a role. Other
  // agents: which agents this one may ask for help.
  // Mounted by AgentDetail only once its own probe of
  // `GET /api/agents/{name}/invites` has already succeeded (see tabs.ts's
  // `visibleTabs` doc comment) — this component never re-derives that
  // eligibility itself, it just renders what the queries below say, same
  // "server decides, UI reflects" discipline every other gated surface in
  // this room follows.
  //
  // Each section's form is a toolbar disclosure that stays shut until asked
  // for:
  //   - Add person — a person by email (directory autocomplete) and a role,
  //     Can see (opens and chats) or Can manage (the owner's edits, short
  //     of destroying the agent; the server's ownership gate honours it)
  //   - Request another team's agent — the browsable outgoing picker
  // The Other agents table holds every agent fact in one place: incoming pending
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
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { errorMessage, type GrantRole, type Person } from "../lib/api";
  import { debounced } from "../lib/debounce";
  import {
    acceptInviteMutation,
    addGrantMutation,
    agentAccessQuery,
    createInviteMutation,
    declineInviteMutation,
    grantsQuery,
    invitesQuery,
    pairsQuery,
    peopleSearchQuery,
    removeGrantMutation,
    requestAgentAccessMutation,
    requestableQuery,
    revokeInviteMutation,
  } from "../lib/queries";
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
  const invites = createQuery(() => (invitesQuery(agentName)));
  // svelte-ignore state_referenced_locally
  const grants = createQuery(() => (grantsQuery(agentName)));
  // svelte-ignore state_referenced_locally
  const requestable = createQuery(() => (requestableQuery(agentName)));
  // svelte-ignore state_referenced_locally
  const acceptMutation = createMutation(() => (acceptInviteMutation(queryClient, agentName)));
  // svelte-ignore state_referenced_locally
  const declineMutation = createMutation(() => (declineInviteMutation(queryClient, agentName)));
  // svelte-ignore state_referenced_locally
  const revokeMutation = createMutation(() => (revokeInviteMutation(queryClient, agentName)));
  // svelte-ignore state_referenced_locally
  const addGrant = createMutation(() => (addGrantMutation(queryClient, agentName)));
  // svelte-ignore state_referenced_locally
  const removeGrant = createMutation(() => (removeGrantMutation(queryClient, agentName)));
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

  // The two toolbar disclosures. Shut on arrival: a panel that greets you
  // with two open forms reads as work waiting to be done, and neither of
  // these is.
  let grantOpen = $state(false);
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

  // --- People: grants -------------------------------------------------------

  let grantEmail = $state("");
  let grantRole = $state<GrantRole>("view");
  let grantError = $state<string | null>(null);

  const ROLE_LABEL: Record<GrantRole, string> = { view: "Can see", manage: "Can manage" };

  const canAddGrant = $derived(!frozen && !addGrant.isPending && grantEmail.trim().length > 0);

  function handleAddGrant(event: SubmitEvent): void {
    event.preventDefault();
    if (!canAddGrant) {
      return;
    }
    const email = grantEmail.trim();
    grantError = null;
    addGrant.mutate(
      { email, role: grantRole },
      {
        onSuccess: () => {
          grantEmail = "";
          grantRole = "view";
        },
        onError: (err: unknown) => {
          grantError = errorMessage(err, "Couldn't add that person. Try again.");
        },
      },
    );
  }

  function handleRemoveGrant(email: string): void {
    if (removeGrant.isPending) {
      return;
    }
    grantError = null;
    removeGrant.mutate(email, {
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
  let peopleSearchTerm = $state("");
  const peopleResults = createQuery(() => (peopleSearchQuery(peopleSearchTerm)));

  const commitPeopleSearch = debounced((value: string) => {
    peopleSearchTerm = value;
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

  const suggestions = $derived(peopleResults.data?.people ?? []);
  const directoryUnavailable = $derived(peopleResults.data?.unavailable === true);
  const showSuggestions = $derived(
    !suggestionsDismissed && grantEmail.trim().length >= 2 && suggestions.length > 0,
  );

  function pickPerson(person: Person): void {
    grantEmail = person.email;
    suggestionsDismissed = true;
  }
</script>

<section class="panel" aria-labelledby="access-heading">
  <div class="panel__header">
    <h2 id="access-heading" class="panel__heading">Access</h2>
    {#if groupName}
      <p class="panel__lead">Everyone in {groupName} can see this agent.</p>
    {:else}
      <p class="panel__lead">
        This agent is not in a group. Only its owner, operators, and the people listed below can see it.
      </p>
    {/if}
  </div>

  <!-- The disclosure buttons stay usable on a frozen agent: they reveal a
       form, they never change anything. Every control inside them is
       disabled while frozen, which is where the freeze actually bites. -->

  <section class="access-panel__section" aria-labelledby="access-people-heading">
    <div class="panel__header access-panel__head">
      <h3 id="access-people-heading" class="panel__subheading">People</h3>
      <p class="panel__lead">
        Who can see and manage this agent besides its owner and the room's operators.
      </p>
      <div class="panel__tools">
          <button
            type="button"
            class="btn"
            aria-expanded={grantOpen}
            onclick={() => (grantOpen = !grantOpen)}
          >
            Add person
          </button>
      </div>
    </div>

    <div class="access-panel__body">

      {#if grantOpen}
        <form class="panel__drawer access-panel__grant" onsubmit={handleAddGrant}>
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
                        disabled={frozen}
                        onclick={() => pickPerson(person)}
                      >
                        {person.name} &lt;{person.email}&gt;
                      </button>
                    </li>
                  {/each}
                </ul>
              {/if}
            </div>
            <label class="panel__sr" for="access-grant-role">Role</label>
            <select
              id="access-grant-role"
              class="access-panel__select"
              bind:value={grantRole}
              disabled={frozen}
            >
              <option value="view">{ROLE_LABEL.view}</option>
              <option value="manage">{ROLE_LABEL.manage}</option>
            </select>
            <button type="submit" class="btn btn--primary" disabled={frozen || !canAddGrant}>
              {addGrant.isPending ? "Adding…" : "Add"}
            </button>
          </div>
          <p class="panel__note"><strong>Can see:</strong> opens the agent and chats with it.</p>
          <p class="panel__note">
            <strong>Can manage:</strong> edits its skills, tooling, Slack, secrets and permission
            requests. Cannot destroy it.
          </p>
          {#if directoryUnavailable}
            <p class="panel__note">
              Directory search is unavailable right now. You can still type an email directly.
            </p>
          {/if}
          {#if grantError}
            <p class="panel__note panel__note--fail" role="alert">{grantError}</p>
          {/if}
        </form>
      {/if}

      {#if grants.isPending}
        <Skeleton rows={2} />
      {:else if grants.isError}
        <ErrorState
          message={errorMessage(grants.error, "Couldn't load who has access.")}
          onretry={() => grants.refetch()}
        />
      {:else}
        <table class="panel__table">
          <thead>
            <tr>
              <th scope="col">Person</th>
              <th scope="col" class="access-panel__col-role">Role</th>
              <th scope="col" class="access-panel__col-action">
                <span class="panel__sr">Remove</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {#if grants.data.length === 0}
              <tr>
                <td class="panel__empty" colspan="3">Nobody has been added yet.</td>
              </tr>
            {/if}
            {#each grants.data as grant (grant.email)}
              <tr class="access-panel__row">
                <td><span class="access-panel__row-name">{grant.email}</span></td>
                <td><span class="access-panel__chip">{ROLE_LABEL[grant.role] ?? grant.role}</span></td>
                <td class="panel__cell--right">
                  <button
                    type="button"
                    class="btn btn--quiet btn--danger"
                    disabled={frozen || removeGrant.isPending}
                    aria-label={`Remove ${grant.email}`}
                    onclick={() => handleRemoveGrant(grant.email)}
                  >
                    Remove
                  </button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
        {#if grantError && !grantOpen}
          <p class="panel__note panel__note--fail" role="alert">{grantError}</p>
        {/if}
      {/if}
    </div>
  </section>

  <section class="access-panel__section" aria-labelledby="access-agents-heading">
    <div class="panel__header access-panel__head">
      <h3 id="access-agents-heading" class="panel__subheading">Other agents</h3>
      <p class="panel__lead">Agents this one may ask for help, across teams.</p>
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

    <div class="access-panel__body">

      {#if pickerOpen}
        <div class="panel__drawer access-panel__picker">
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
            <ul class="access-panel__list">
              {#each requestable.data?.requestable ?? [] as candidate (candidate.name)}
                <li class="access-panel__candidate">
                  <div class="access-panel__candidate-info">
                    <span class="access-panel__row-name">{candidate.name}</span>
                    {#if candidate.group}
                      <span class="access-panel__chip">{candidate.group}</span>
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
                <li class="access-panel__candidate">
                  <span class="access-panel__row-name">{entry.name}</span>
                  <span class="access-panel__chip access-panel__chip--state">{entry.status}</span>
                </li>
              {/each}
              {#each requestable.data?.not_ready ?? [] as entry (entry.name)}
                <li class="access-panel__candidate access-panel__candidate--muted">
                  <span class="access-panel__row-name">{entry.name}</span>
                  {#if entry.group}
                    <span class="access-panel__chip">{entry.group}</span>
                  {/if}
                  <span class="access-panel__chip access-panel__chip--state">
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
        <table class="panel__table">
          <thead>
            <tr>
              <th scope="col">Agent</th>
              <th scope="col" class="access-panel__col-role">Status</th>
              <th scope="col" class="access-panel__col-action">
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
              <tr class="access-panel__row">
                <td><span class="access-panel__row-name">{invite.from_agent}</span></td>
                <td><span class="panel__meta">Wants to talk to yours</span></td>
                <td class="panel__cell--right">
                  <div class="access-panel__actions">
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
              <tr class="access-panel__row">
                <td><span class="access-panel__row-name">{pair.name}</span></td>
                <td><span class="panel__meta">Can join shared threads</span></td>
                <td class="panel__cell--right">
                  <span class="access-panel__chip">Same team ({pair.group})</span>
                </td>
              </tr>
            {/each}
            {#each waitingAsks as ask (ask.id)}
              <tr class="access-panel__row">
                <td><span class="access-panel__row-name">{ask.integration}</span></td>
                <td><span class="panel__meta">Waiting for review</span></td>
                <td class="panel__cell--right">
                  <span class="access-panel__chip">Requested</span>
                </td>
              </tr>
            {/each}
            {#each grantPairs as name (name)}
              <tr class="access-panel__row">
                <td><span class="access-panel__row-name">{name}</span></td>
                <td><span class="panel__meta">Can join shared threads</span></td>
                <td class="panel__cell--right">
                  <span class="access-panel__chip">Approved in Review</span>
                </td>
              </tr>
            {/each}
            {#each acceptedPairs as invite (invite.id)}
              <tr class="access-panel__row">
                <td>
                  <span class="access-panel__row-name">
                    {invite.from_agent} and {invite.to_agent.name}
                  </span>
                </td>
                <td><span class="panel__meta">Can join shared threads</span></td>
                <td class="panel__cell--right">
                  <div class="access-panel__revoke-group">
                    <span class="access-panel__chip">Accepted request</span>
                    <button
                      type="button"
                      class="btn btn--quiet btn--danger"
                      disabled={frozen || revokeMutation.isPending}
                      aria-label={`Revoke ${invite.from_agent} and ${invite.to_agent.name}`}
                      onclick={() => handleRevoke(invite.id)}
                    >
                      Revoke
                    </button>
                    <span class="access-panel__hint">
                      Revoking takes effect within a few minutes.
                    </span>
                  </div>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
        {#if hasPairs}
          <p class="panel__note">Pairs are live as soon as they appear here.</p>
        {/if}
      {/if}
    </div>
  </section>
</section>

<style>
  /* One section per thing the tab governs (issue #309): its own heading,
     sentence, toolbar and table, stacked with the panel's own rhythm. */
  /* Each part of Access is a settings row (2026-10-31): what it is and its
     action on the left, the list itself on the right, a rule between rows. */
  .access-panel__section {
    display: grid;
    grid-template-columns: minmax(0, 15rem) minmax(0, 1fr);
    gap: var(--space-xl);
    width: 100%;
    padding: var(--space-lg) 0 var(--space-sm);
    border-top: 1px solid var(--border);
  }

  .access-panel__head {
    align-content: start;
    gap: var(--space-xs);
  }

  .access-panel__head :global(.panel__subheading) {
    color: var(--ink);
    font-size: 0.92rem;
    font-weight: 600;
    letter-spacing: normal;
    text-transform: none;
  }

  .access-panel__head :global(.panel__lead) {
    font-size: 0.82rem;
    line-height: 1.5;
  }

  .access-panel__head :global(.panel__tools) {
    margin-top: var(--space-xs);
  }

  .access-panel__body {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: var(--space-sm);
  }

  /* The lists sit in a bordered card with a tinted header row. */
  .access-panel__body :global(.panel__table) {
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    border-collapse: separate;
    border-spacing: 0;
  }

  .access-panel__body :global(.panel__table th) {
    height: 2.4rem;
    padding: 0 var(--space-md);
    background: var(--bg);
    vertical-align: middle;
  }

  .access-panel__body :global(.panel__table td) {
    height: 3rem;
    padding: var(--space-2xs) var(--space-md);
  }

  .access-panel__body :global(.panel__table tbody tr:last-child td) {
    border-bottom: none;
  }

  .access-panel__body :global(.panel__empty) {
    padding: var(--space-lg) var(--space-md);
  }

  @media (max-width: 52rem) {
    .access-panel__section {
      grid-template-columns: minmax(0, 1fr);
      gap: var(--space-sm);
    }
  }

  .access-panel__grant,
  .access-panel__picker {
    width: 100%;
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
    width: 100%;
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
  }

  .access-panel__input:focus-visible,
  .access-panel__select:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .access-panel__select {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
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

  .access-panel__suggestion:hover:not(:disabled) {
    background: var(--bg);
  }

  .access-panel__list {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    width: 100%;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .access-panel__candidate {
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

  .access-panel__candidate-info {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 0;
  }

  .access-panel__row-name {
    overflow-wrap: anywhere;
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.85rem;
  }

  .access-panel__chip {
    align-self: flex-start;
    padding: 0.1rem var(--space-xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-pill);
    color: var(--muted);
    font-size: 0.7rem;
    white-space: nowrap;
  }

  .access-panel__chip--state {
    text-transform: capitalize;
  }

  .access-panel__actions,
  .access-panel__revoke-group {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: var(--space-xs);
  }

  .access-panel__revoke-group {
    flex-wrap: wrap;
  }

  .access-panel__hint {
    color: var(--muted);
    font-size: 0.75rem;
  }

  .access-panel__col-role {
    width: 12rem;
  }

  .access-panel__col-action {
    width: 16rem;
  }
</style>
