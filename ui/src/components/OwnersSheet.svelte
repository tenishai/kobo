<script lang="ts">
  // Owners (issue #368): who can see and manage this agent besides its
  // owner and the room's operators, each with a role. This was the People
  // block of the Access tab; it is a sheet off the page header now, opened
  // by the Owners button there, and the Access tab is about vendors. Same
  // routes, same roles, same directory-backed add-person autocomplete.
  //
  //   - Can see: opens the agent and chats with it.
  //   - Can manage: the owner's edits, short of destroying the agent; the
  //     server's ownership gate honours it.
  //
  // Fully controlled by the page (`open` + `onclose`), like every dialog in
  // this room. Mounted only once the page's own probe of
  // `GET /api/agents/{name}/invites` has succeeded: the grants route answers
  // the same callers, and this component never re-derives that eligibility.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { errorMessage, type GrantRole, type Person } from "../lib/api";
  import { debounced } from "../lib/debounce";
  import {
    addGrantMutation,
    grantsQuery,
    peopleSearchQuery,
    removeGrantMutation,
  } from "../lib/queries";
  import ErrorState from "./ErrorState.svelte";
  import Modal from "./Modal.svelte";
  import Skeleton from "./Skeleton.svelte";

  let {
    agentName,
    open,
    onclose,
    frozen = false,
  }: {
    agentName: string;
    open: boolean;
    onclose: () => void;
    frozen?: boolean;
  } = $props();

  const queryClient = useQueryClient();
  // agentName is read once: AgentDetail remounts this component via
  // `{#key route.name}` when the routed agent changes.
  // svelte-ignore state_referenced_locally
  const grants = createQuery(() => (grantsQuery(agentName)));
  // svelte-ignore state_referenced_locally
  const addGrant = createMutation(() => (addGrantMutation(queryClient, agentName)));
  // svelte-ignore state_referenced_locally
  const removeGrant = createMutation(() => (removeGrantMutation(queryClient, agentName)));

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

  // --- Directory-backed autocomplete (access-tab refinement wave) ----------
  //
  // Reuses `grantEmail` itself as the search term: the same input both
  // searches the directory and holds the value Add ultimately submits, so
  // picking a suggestion just overwrites it with that person's email. The
  // debounced write lands in its own `$state`, which is what the query
  // options read.
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
  // dropdown a quarter-second later. Reopened by any REAL keystroke:
  // `oninput` only fires on genuine typing, never on the programmatic
  // `grantEmail = person.email` assignment a pick makes.
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

{#snippet ownersFooter()}
  <div class="modal__actions">
    <button type="button" class="btn btn--quiet" onclick={onclose}>Done</button>
  </div>
{/snippet}

<Modal {open} title="Owners" {onclose} footer={ownersFooter}>
  <p class="owners__lead">
    Who can see and manage this agent besides its owner and the room's operators.
  </p>

  <form class="owners__grant" onsubmit={handleAddGrant}>
    <label class="owners__label" for="owners-grant-email">Add someone by email</label>
    <div class="owners__grant-row">
      <div class="owners__autocomplete">
        <input
          id="owners-grant-email"
          class="field__input"
          type="text"
          autocomplete="off"
          bind:value={grantEmail}
          oninput={handleGrantEmailTyped}
          disabled={frozen}
        />
        {#if showSuggestions}
          <ul class="owners__suggestions" role="listbox">
            {#each suggestions as person (person.email)}
              <li role="option" aria-selected="false">
                <button
                  type="button"
                  class="owners__suggestion"
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
      <label class="panel__sr" for="owners-grant-role">Role</label>
      <select
        id="owners-grant-role"
        class="field__input owners__select"
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

  {#if grants.isPending}
    <Skeleton rows={2} />
  {:else if grants.isError}
    <ErrorState
      message={errorMessage(grants.error, "Couldn't load who has access.")}
      onretry={() => grants.refetch()}
    />
  {:else}
    <div class="table-card">
      <table class="panel__table">
        <thead>
          <tr>
            <th scope="col">Person</th>
            <th scope="col" class="owners__col-role">Role</th>
            <th scope="col" class="owners__col-action">
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
            <tr class="owners__row">
              <td><span class="owners__row-name">{grant.email}</span></td>
              <td><span class="owners__chip">{ROLE_LABEL[grant.role] ?? grant.role}</span></td>
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
    </div>
  {/if}
</Modal>

<style>
  .owners__lead {
    margin: 0 0 var(--space-md);
    color: var(--ink);
    font-size: 0.9rem;
  }

  .owners__grant {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    width: 100%;
    margin-bottom: var(--space-md);
  }

  .owners__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.85rem;
  }

  .owners__grant-row {
    display: flex;
    align-items: flex-start;
    gap: var(--space-xs);
    width: 100%;
  }

  .owners__autocomplete {
    position: relative;
    flex: 1;
    min-width: 0;
  }

  .owners__autocomplete .field__input {
    width: 100%;
  }

  .owners__select {
    width: auto;
  }

  .owners__suggestions {
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

  .owners__suggestion {
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

  .owners__suggestion:hover:not(:disabled) {
    background: var(--bg);
  }

  .owners__row-name {
    overflow-wrap: anywhere;
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.85rem;
  }

  .owners__chip {
    padding: 0.1rem var(--space-xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-pill);
    color: var(--muted);
    font-size: 0.7rem;
    white-space: nowrap;
  }

  .owners__col-role {
    width: 8rem;
  }

  .owners__col-action {
    width: 7rem;
  }
</style>
