<script lang="ts">
  // Secrets and environment, per agent (design 2026-09-22) -- the section
  // the sibling deploy platform has had all along, and the reason nobody
  // has to make a Kubernetes Secret by hand to give an agent an API key.
  //
  // Two lists, one panel, and the difference between them is what you can
  // read back. A SECRET's value goes up once and never comes down: the
  // table below holds key names and a date, and there is deliberately no
  // reveal, no mask, and no "show" control to add one to. An ENVIRONMENT
  // variable's value does come back and is edited in place, because that
  // list is for values that are not sensitive and a list you cannot read
  // is not one you can edit.
  //
  // Both halves render for a composed agent and a connected one alike. The
  // difference shows up only in what a save reports: the room commits to a
  // repository it owns, and proposes a pull request against one it does
  // not. `wiring` says which, and the notice below prints the link when
  // there is one, because a change waiting in a pull request has not
  // reached the agent and saying otherwise would be a lie.
  //
  // A room with no secrets platform configured replaces the Secrets half
  // with the server's own sentence (`platform: false`). The environment
  // half still works, which is the point of checking rather than hiding
  // the whole panel.
  //
  // There is no "Restart agent" button. The room has no route that rolls
  // an agent's Deployment -- the one internal helper that does it
  // (`kobo_api.main._request_tool_restart`) is fired by the tooling fast
  // lane and exposed nowhere -- so the panel promises what is actually
  // true and says changes reach the agent the next time it starts.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import EmptyState from "./EmptyState.svelte";
  import ErrorState from "./ErrorState.svelte";
  import Skeleton from "./Skeleton.svelte";
  import { errorMessage, type EnvWiring } from "../lib/api";
  import { keyRefusal, MAX_KEYS, parseEnvPaste } from "../lib/dotenv";
  import {
    agentEnvQuery,
    agentSecretsQuery,
    saveAgentEnvMutation,
    saveAgentSecretsMutation,
  } from "../lib/queries";

  let {
    agentName,
    frozen = false,
    active = true,
  }: {
    agentName: string;
    frozen?: boolean;
    /** Whether the Secrets tab is the selected one. Both queries below are
     * gated on it: every tabpanel on the agent page stays MOUNTED when
     * another tab is picked (they are hidden, not destroyed), so without
     * this, opening any agent page at all would ask the secrets platform
     * about an agent nobody was looking at and put a row in its audit
     * trail saying somebody did. */
    active?: boolean;
  } = $props();

  const queryClient = useQueryClient();
  // Read once, like every other panel here: AgentDetail remounts this
  // component via `{#key route.name}` when the routed agent changes, so
  // this prop is fixed for the component's whole lifetime.
  // `active` rides into the query factories the way `manifestOpened` does
  // in SlackPanel: a closure the adapter re-reads, so flipping to this tab
  // starts the read and nothing before it does.
  const secrets = createQuery(() => agentSecretsQuery(agentName, active));
  const env = createQuery(() => agentEnvQuery(agentName, active));
  // svelte-ignore state_referenced_locally
  const saveSecrets = createMutation(() => saveAgentSecretsMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const saveEnv = createMutation(() => saveAgentEnvMutation(queryClient, agentName));

  // --- The one line above both lists ----------------------------------------

  const LEAD =
    "Secrets are stored in the secrets platform and reach the agent as " +
    "environment variables. Environment variables are for values that are " +
    "not sensitive. Changes reach the agent the next time it starts.";

  // --- Where a save landed --------------------------------------------------

  // Shared by both halves, because both report the same three outcomes and
  // a person reading this panel cares about the answer, not about which
  // form produced it. Cleared when either form starts a new save.
  let landing = $state<{ wiring: EnvWiring; pullRequest?: string } | null>(null);

  const landingText = $derived.by(() => {
    if (landing === null) {
      return null;
    }
    if (landing.wiring === "pull_request") {
      return "Saved. The agent's files belong to its team, so the change is waiting in a pull request.";
    }
    if (landing.wiring === "unchanged") {
      return "Saved. The agent's files already said this.";
    }
    return "Saved. The agent picks this up the next time it starts.";
  });

  // --- Secrets: add one -----------------------------------------------------

  let newKey = $state("");
  let newValue = $state("");
  let secretsError = $state<string | null>(null);

  const newKeyRefusal = $derived(newKey.length === 0 ? null : keyRefusal(newKey));
  const canAddSecret = $derived(
    !frozen &&
      !saveSecrets.isPending &&
      newKey.length > 0 &&
      newValue.length > 0 &&
      newKeyRefusal === null,
  );

  function handleAddSecret(event: SubmitEvent): void {
    event.preventDefault();
    if (!canAddSecret) {
      return;
    }
    writeSecrets({ [newKey]: newValue }, [], () => {
      newKey = "";
      newValue = "";
    });
  }

  function handleRemoveSecret(key: string): void {
    if (frozen || saveSecrets.isPending) {
      return;
    }
    writeSecrets({}, [key]);
  }

  // --- Secrets: paste a .env ------------------------------------------------

  let paste = $state("");
  let pasteProblems = $state<string[]>([]);

  function handlePaste(event: SubmitEvent): void {
    event.preventDefault();
    if (frozen || saveSecrets.isPending) {
      return;
    }
    const parsed = parseEnvPaste(paste);
    pasteProblems = parsed.problems;
    if (parsed.problems.length > 0) {
      // Nothing is written until every line is fixed: half a pasted file
      // is a worse outcome than none of it, because which half landed is
      // not something the list can show.
      return;
    }
    const pasted = Object.keys(parsed.values);
    if (pasted.length === 0) {
      pasteProblems = ["There is nothing to add here yet."];
      return;
    }
    // The same cap the server holds, checked before the round trip: a
    // pasted file is exactly where somebody goes past it, and answering
    // here says how far over they are rather than just refusing.
    const held = secrets.data?.keys ?? [];
    const after = new Set([...held, ...pasted]).size;
    if (after > MAX_KEYS) {
      pasteProblems = [
        `That would leave ${after} secrets, and ${MAX_KEYS} is the most a list can hold.`,
      ];
      return;
    }
    writeSecrets(parsed.values, [], () => {
      paste = "";
    });
  }

  function writeSecrets(
    set: Record<string, string>,
    remove: string[],
    done?: () => void,
  ): void {
    secretsError = null;
    landing = null;
    saveSecrets.mutate(
      { set, remove },
      {
        onSuccess: (result) => {
          landing = { wiring: result.wiring, pullRequest: result.pull_request };
          done?.();
        },
        onError: (err: unknown) => {
          secretsError = errorMessage(err, "Couldn't save that. Try again.");
        },
      },
    );
  }

  // --- Environment variables ------------------------------------------------

  // A local draft seeded from the server's answer the first time it lands,
  // and re-seeded after every save from what the save itself reports the
  // file now holds. Never re-seeded from a background refetch: this is
  // text somebody is editing, and a list that rewrote itself underneath
  // them would lose it.
  // Each row carries an `id` that never changes, and the `{#each}` below
  // is keyed on it. Keying on the index meant deleting a row re-keyed
  // every row after it: Svelte reused the wrong DOM nodes, so focus and
  // an in-progress edit landed on the neighbour of the row somebody
  // actually deleted.
  let rows = $state<{ id: number; key: string; value: string }[]>([]);
  let nextRowId = 0;
  let seeded = $state(false);
  let envError = $state<string | null>(null);

  $effect(() => {
    const data = env.data;
    if (!seeded && data) {
      rows = toRows(data.env);
      seeded = true;
    }
  });

  /** One `{key, value}` row per entry, tolerant of a body that has no `env`
   * mapping at all. A read is not the place to raise over a shape the
   * server should not have sent: an empty list is the honest render, and
   * the save below is what would fix the file. */
  function toRows(env: Record<string, string> | undefined) {
    return Object.entries(env ?? {}).map(([key, value]) => ({
      id: nextRowId++,
      key,
      value,
    }));
  }

  const rowRefusals = $derived(
    rows.map((row) => (row.key.length === 0 ? null : keyRefusal(row.key))),
  );
  const canSaveEnv = $derived(
    !frozen && !saveEnv.isPending && rowRefusals.every((refusal) => refusal === null),
  );

  function handleAddRow(): void {
    rows = [...rows, { id: nextRowId++, key: "", value: "" }];
  }

  function handleRemoveRow(id: number): void {
    rows = rows.filter((row) => row.id !== id);
  }

  function handleSaveEnv(event: SubmitEvent): void {
    event.preventDefault();
    if (!canSaveEnv) {
      return;
    }
    const before = env.data?.env ?? {};
    const set: Record<string, string> = {};
    for (const row of rows) {
      if (row.key.length > 0) {
        set[row.key] = row.value;
      }
    }
    // Whatever the file held and the draft no longer does. Worked out here
    // rather than by the server, because only this side knows which rows a
    // person deleted.
    const remove = Object.keys(before).filter((key) => !(key in set));

    envError = null;
    landing = null;
    saveEnv.mutate(
      { set, remove },
      {
        onSuccess: (result) => {
          landing = { wiring: result.wiring, pullRequest: result.pull_request };
          rows = toRows(result.env);
        },
        onError: (err: unknown) => {
          envError = errorMessage(err, "Couldn't save that. Try again.");
        },
      },
    );
  }

  function whenChanged(updatedAt: string | null): string | null {
    if (!updatedAt) {
      return null;
    }
    const at = new Date(updatedAt);
    return Number.isNaN(at.getTime()) ? null : at.toLocaleDateString();
  }
</script>

<section class="secrets-panel" aria-labelledby="secrets-panel-heading">
  <div class="secrets-panel__header">
    <h2 id="secrets-panel-heading" class="secrets-panel__heading">
      Secrets and environment
    </h2>
    <p class="secrets-panel__lead">{LEAD}</p>
  </div>

  {#if landingText}
    <div class="secrets-panel__notice" role="status">
      <span>{landingText}</span>
      {#if landing?.pullRequest}
        <a
          class="secrets-panel__link"
          href={landing.pullRequest}
          target="_blank"
          rel="noreferrer"
        >
          Open the pull request
        </a>
      {/if}
    </div>
  {/if}

  <!-- Secrets -->
  <div class="secrets-group">
    <h3 class="secrets-group__heading">Secrets</h3>

    {#if secrets.isPending}
      <Skeleton />
    {:else if secrets.isError}
      <ErrorState
        message={errorMessage(secrets.error, "Couldn't load the secrets.")}
        onretry={() => secrets.refetch()}
      />
    {:else if secrets.data.platform !== true}
      <p class="secrets-group__unavailable">
        This room has no secrets platform configured, so it cannot keep secrets for
        an agent. Environment variables still work.
      </p>
    {:else}
      <p class="secrets-group__help">
        Values are sent once and are never shown again, here or anywhere else in
        the room. They reach the agent as
        <code>{secrets.data.secret_name}</code>.
        {#if whenChanged(secrets.data.updated_at)}
          Last changed {whenChanged(secrets.data.updated_at)}.
        {/if}
      </p>

      {#if (secrets.data.keys ?? []).length === 0}
        <EmptyState message="No secrets yet." />
      {:else}
        <ul class="secrets-list">
          {#each secrets.data.keys ?? [] as key (key)}
            <li class="secrets-list__row">
              <code class="secrets-list__key">{key}</code>
              <button
                type="button"
                class="secrets-list__remove"
                aria-label={`Remove ${key}`}
                disabled={frozen || saveSecrets.isPending}
                onclick={() => handleRemoveSecret(key)}
              >
                Remove
              </button>
            </li>
          {/each}
        </ul>
      {/if}

      <form class="secrets-form" onsubmit={handleAddSecret}>
        <h4 class="secrets-form__heading">Add secret</h4>
        <div class="secrets-form__field">
          <label class="secrets-form__label" for="secret-key">Name</label>
          <input
            id="secret-key"
            class="secrets-form__input"
            type="text"
            bind:value={newKey}
            autocomplete="off"
            spellcheck="false"
            aria-invalid={newKeyRefusal !== null}
            disabled={frozen}
          />
          <p class="secrets-form__help">
            Capital letters, digits and underscores, starting with a letter.
          </p>
          {#if newKeyRefusal}
            <p class="secrets-form__status" role="alert">{newKeyRefusal}</p>
          {/if}
        </div>
        <div class="secrets-form__field">
          <label class="secrets-form__label" for="secret-value">Value</label>
          <input
            id="secret-value"
            class="secrets-form__input"
            type="password"
            bind:value={newValue}
            autocomplete="off"
            spellcheck="false"
            disabled={frozen}
          />
        </div>
        <button type="submit" class="secrets-form__submit" disabled={!canAddSecret}>
          {saveSecrets.isPending ? "Saving…" : "Add secret"}
        </button>
      </form>

      <form class="secrets-form" onsubmit={handlePaste}>
        <h4 class="secrets-form__heading">Paste .env</h4>
        <div class="secrets-form__field">
          <label class="secrets-form__label" for="secret-paste">
            One KEY=value per line
          </label>
          <textarea
            id="secret-paste"
            class="secrets-form__textarea"
            rows="4"
            bind:value={paste}
            spellcheck="false"
            disabled={frozen}
          ></textarea>
        </div>
        {#if pasteProblems.length > 0}
          <ul class="secrets-form__problems" role="alert">
            {#each pasteProblems as problem (problem)}
              <li>{problem}</li>
            {/each}
          </ul>
        {/if}
        <button
          type="submit"
          class="secrets-form__submit"
          disabled={frozen || saveSecrets.isPending || paste.trim().length === 0}
        >
          Add these
        </button>
      </form>

      {#if secretsError}
        <p class="secrets-form__status" role="alert">{secretsError}</p>
      {/if}
    {/if}
  </div>

  <!-- Environment variables -->
  <div class="secrets-group">
    <h3 class="secrets-group__heading">Environment variables</h3>

    {#if env.isPending}
      <Skeleton />
    {:else if env.isError}
      <ErrorState
        message={errorMessage(env.error, "Couldn't load the environment variables.")}
        onretry={() => env.refetch()}
      />
    {:else}
      <p class="secrets-group__help">
        For values that are not sensitive. These are kept in the agent's own files,
        where anyone who can read them can see them.
      </p>
      <form class="secrets-form" onsubmit={handleSaveEnv}>
        {#if rows.length === 0}
          <EmptyState message="No environment variables yet." />
        {/if}
        {#each rows as row, index (row.id)}
          <div class="env-row">
            <div class="secrets-form__field">
              <label class="secrets-form__label" for={`env-key-${index}`}>
                Name {index + 1}
              </label>
              <input
                id={`env-key-${index}`}
                class="secrets-form__input"
                type="text"
                bind:value={row.key}
                autocomplete="off"
                spellcheck="false"
                aria-invalid={rowRefusals[index] !== null}
                disabled={frozen}
              />
            </div>
            <div class="secrets-form__field">
              <label class="secrets-form__label" for={`env-value-${index}`}>
                Value {index + 1}
              </label>
              <input
                id={`env-value-${index}`}
                class="secrets-form__input"
                type="text"
                bind:value={row.value}
                autocomplete="off"
                spellcheck="false"
                disabled={frozen}
              />
            </div>
            <button
              type="button"
              class="secrets-list__remove"
              aria-label={`Remove variable ${index + 1}`}
              disabled={frozen}
              onclick={() => handleRemoveRow(row.id)}
            >
              Remove
            </button>
            {#if rowRefusals[index]}
              <p class="secrets-form__status" role="alert">{rowRefusals[index]}</p>
            {/if}
          </div>
        {/each}

        <div class="secrets-form__actions">
          <button
            type="button"
            class="secrets-form__secondary"
            disabled={frozen}
            onclick={handleAddRow}
          >
            Add another variable
          </button>
          <button type="submit" class="secrets-form__submit" disabled={!canSaveEnv}>
            {saveEnv.isPending ? "Saving…" : "Save variables"}
          </button>
        </div>
      </form>

      {#if envError}
        <p class="secrets-form__status" role="alert">{envError}</p>
      {/if}
    {/if}
  </div>
</section>

<style>
  .secrets-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .secrets-panel__header {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .secrets-panel__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .secrets-panel__lead {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .secrets-panel__notice {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-xs);
    padding: var(--space-sm) var(--space-md);
    border: 1px solid var(--accent);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--accent) 10%, transparent);
    color: var(--ink);
    font-size: 0.85rem;
  }

  .secrets-panel__link {
    color: var(--accent);
    font-weight: 600;
  }

  .secrets-group {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
  }

  .secrets-group__heading {
    margin: 0;
    color: var(--ink);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .secrets-group__help,
  .secrets-group__unavailable {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .secrets-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .secrets-list__row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
  }

  .secrets-list__key {
    font-family: var(--font-mono);
    font-size: 0.85rem;
  }

  .secrets-list__remove {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--muted);
    font: inherit;
    font-size: 0.8rem;
    cursor: pointer;
  }

  .secrets-list__remove:hover:not(:disabled) {
    border-color: var(--fail);
    color: var(--fail);
  }

  .secrets-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
  }

  .secrets-form__heading {
    margin: 0;
    color: var(--ink);
    font-size: 0.85rem;
    font-weight: 600;
  }

  .secrets-form__field {
    display: flex;
    flex: 1 1 10rem;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .secrets-form__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.85rem;
  }

  .secrets-form__input,
  .secrets-form__textarea {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .secrets-form__textarea {
    font-family: var(--font-mono);
    font-size: 0.85rem;
    resize: vertical;
  }

  .secrets-form__input:focus-visible,
  .secrets-form__textarea:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .secrets-form__help {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .secrets-form__status {
    margin: 0;
    color: var(--fail);
    font-size: 0.8rem;
  }

  .secrets-form__problems {
    margin: 0;
    padding-left: var(--space-md);
    color: var(--fail);
    font-size: 0.8rem;
  }

  .secrets-form__actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-sm);
  }

  .secrets-form__submit {
    align-self: flex-start;
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font: inherit;
    font-weight: 600;
    cursor: pointer;
  }

  .secrets-form__submit:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .secrets-form__submit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .secrets-form__secondary {
    align-self: flex-start;
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--ink);
    font: inherit;
    font-weight: 600;
    cursor: pointer;
  }

  .env-row {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--space-sm);
  }
</style>
