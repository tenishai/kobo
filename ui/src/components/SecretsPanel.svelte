<script lang="ts">
  // Secrets and environment, per agent (design 2026-09-22, compacted
  // 2026-09-22 after the operator saw the first cut live) -- the section
  // the sibling deploy platform has had all along, and the reason nobody
  // has to make a Kubernetes Secret by hand to give an agent an API key.
  //
  // ONE table, two kinds of row, one Save. The first cut gave each kind its
  // own card, its own always-open form and its own Save button, which read
  // as three panels stacked in a fourth. This is the same two lists and the
  // same two calls, laid out the way the deploy platform's settings page
  // lays them out: a toolbar, a table, a Save.
  //
  // The difference between the two kinds is what you can read back. A
  // SECRET's value goes up once and never comes down: a stored secret's row
  // has no field at all, only dots, and there is deliberately no reveal, no
  // mask and no "show" control to add one to. A pending secret has a
  // password field until Save sends it, and after that the row is a stored
  // one like any other. A VARIABLE's value does come back and is edited in
  // place, because that list is for values that are not sensitive and a
  // list you cannot read is not one you can edit.
  //
  // Edits are PENDING until Save. Nothing here writes on a keystroke or a
  // click of the remove button: Save sends the secrets first and then the
  // variables, both through the same two mutations the first cut used, and
  // Discard throws the whole draft away. Saving secrets first is not
  // arbitrary -- a variable save rewrites the agent's files, and a person
  // who has queued both should have the credential stored before the file
  // naming it is proposed.
  //
  // Both halves render for a composed agent and a connected one alike. The
  // difference shows up only in what a save reports: the room commits to a
  // repository it owns, and proposes a pull request against one it does
  // not. `wiring` says which, and the line below prints the link when there
  // is one, because a change waiting in a pull request has not reached the
  // agent and saying otherwise would be a lie.
  //
  // A room with no secrets platform configured (`platform: false`), or one
  // with nobody to read this agent's secrets as (`unreadable`), keeps the
  // table and loses only the two controls that write secrets. The variables
  // still work, which is the point of checking rather than hiding the whole
  // panel.
  //
  // There is no "Restart agent" button. The room has no route that rolls
  // an agent's Deployment -- the one internal helper that does it
  // (`kobo_api.main._request_tool_restart`) is fired by the tooling fast
  // lane and exposed nowhere -- so the panel promises what is actually
  // true and says changes reach the agent the next time it starts.
  import { untrack } from "svelte";
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import ErrorState from "./ErrorState.svelte";
  import Skeleton from "./Skeleton.svelte";
  import { errorMessage, type AgentSecrets, type EnvWiring } from "../lib/api";
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

  const LEAD =
    "Secrets are stored in the secrets platform and reach the agent as " +
    "environment variables. Variables are for values that are not " +
    "sensitive. Changes reach the agent the next time it starts.";

  // --- The draft ------------------------------------------------------------

  type Kind = "secret" | "variable";

  /** One row of the table. `fresh` means it is not on the server yet: a
   * fresh secret has a value to send, a stored one has only a name.
   * Each row carries an `id` that never changes and the `{#each}` below is
   * keyed on it. Keying on the index meant deleting a row re-keyed every
   * row after it: Svelte reused the wrong DOM nodes, so focus and an
   * in-progress edit landed on the neighbour of the row somebody actually
   * deleted. */
  type Row = { id: number; kind: Kind; name: string; value: string; fresh: boolean };

  let rows = $state<Row[]>([]);
  let nextRowId = 0;

  // What the server last told us, held separately from the queries' own
  // cache so that every pending change is a diff against a fixed point: a
  // background refetch landing mid-edit cannot move the baseline out from
  // under somebody's draft.
  let baseKeys = $state<string[]>([]);
  let baseEnv = $state<Record<string, string>>({});
  let baseSignature = $state<string | null>(null);

  /** The newest row, so the name field of a row somebody just added takes
   * focus without them reaching for the mouse. */
  let focusId = $state<number | null>(null);

  let saveError = $state<string | null>(null);

  const secretsReadable = $derived(
    !secrets.isPending &&
      !secrets.isError &&
      secrets.data?.platform === true &&
      secrets.data.unreadable !== true,
  );

  function serverKeys(data: AgentSecrets | undefined): string[] {
    if (!data || data.platform !== true || data.unreadable === true) {
      return [];
    }
    return data.keys ?? [];
  }

  function seedRows(keys: string[], values: Record<string, string>): Row[] {
    return [
      ...keys.map((name) => ({
        id: nextRowId++,
        kind: "secret" as Kind,
        name,
        value: "",
        fresh: false,
      })),
      ...Object.entries(values).map(([name, value]) => ({
        id: nextRowId++,
        kind: "variable" as Kind,
        name,
        value,
        fresh: false,
      })),
    ];
  }

  // Seed from the reads, and re-seed whenever the server's answer actually
  // changes -- but never over a draft. `hasPending` is read untracked: this
  // effect writes the very state it would otherwise depend on.
  $effect(() => {
    if (secrets.isPending || env.isPending || env.isError || !env.data) {
      return;
    }
    const keys = serverKeys(secrets.data);
    const values = env.data.env ?? {};
    const signature = JSON.stringify([keys, values]);
    untrack(() => {
      if (signature === baseSignature) {
        return;
      }
      if (baseSignature !== null && hasPending) {
        return;
      }
      baseKeys = keys;
      baseEnv = values;
      baseSignature = signature;
      rows = seedRows(keys, values);
    });
  });

  // --- What a save would send ----------------------------------------------

  /** Every fresh secret row with a name, which is exactly the set of values
   * that has not left the browser yet. */
  const secretSet = $derived.by(() => {
    const set: Record<string, string> = {};
    for (const row of rows) {
      if (row.kind === "secret" && row.fresh && row.name.length > 0) {
        set[row.name] = row.value;
      }
    }
    return set;
  });

  /** A stored key with no row left carrying its name. A fresh row with the
   * same name counts as carrying it: that is an overwrite, not a removal,
   * and sending both would ask the server to undo the write it just made. */
  const secretRemove = $derived(
    baseKeys.filter((key) => !rows.some((row) => row.kind === "secret" && row.name === key)),
  );

  const envSet = $derived.by(() => {
    const set: Record<string, string> = {};
    for (const row of rows) {
      if (row.kind === "variable" && row.name.length > 0) {
        set[row.name] = row.value;
      }
    }
    return set;
  });

  /** Whatever the file held and the draft no longer does. Worked out here
   * rather than by the server, because only this side knows which rows a
   * person deleted. */
  const envRemove = $derived(Object.keys(baseEnv).filter((key) => !(key in envSet)));

  const secretsPending = $derived(
    Object.keys(secretSet).length > 0 || secretRemove.length > 0,
  );
  const envPending = $derived(
    envRemove.length > 0 ||
      Object.entries(envSet).some(([key, value]) => baseEnv[key] !== value),
  );
  const hasPending = $derived(secretsPending || envPending);

  /** The key rule, inline, for the names somebody can actually type. A
   * stored secret's name came from the server and is not editable, so
   * there is nothing to refuse. */
  function rowRefusal(row: Row): string | null {
    if (row.name.length === 0 || (row.kind === "secret" && !row.fresh)) {
      return null;
    }
    return keyRefusal(row.name);
  }

  /** A named secret with no value would store an empty credential, which is
   * never what somebody meant. Not shown as a refusal (it would nag at the
   * first letter of the name); it just keeps Save out of reach until the
   * field beside it is filled. */
  function rowIncomplete(row: Row): boolean {
    return row.kind === "secret" && row.fresh && row.name.length > 0 && row.value.length === 0;
  }

  const refusals = $derived(rows.map(rowRefusal));
  const saving = $derived(saveSecrets.isPending || saveEnv.isPending);
  const canSave = $derived(
    !frozen &&
      !saving &&
      hasPending &&
      refusals.every((refusal) => refusal === null) &&
      !rows.some(rowIncomplete),
  );

  // --- Where a save landed --------------------------------------------------

  type Landing = {
    wiring: EnvWiring;
    pullRequest?: string;
    pendingUnwire?: boolean;
    emptied?: boolean;
  };

  let landing = $state<Landing | null>(null);

  const landingText = $derived.by(() => {
    if (landing === null) {
      return null;
    }
    if (landing.pendingUnwire) {
      // The one outcome where the save is NOT the whole story: the
      // agent's files still name the secret, so it has to stay until
      // somebody merges. Saying "removed" here would be a lie somebody
      // only finds out about when they look at the platform.
      return "The secret stays until that pull request merges. Save again after it merges to finish removing it.";
    }
    if (landing.emptied) {
      return "Removed. That was this agent's last secret, so the line naming it is gone too.";
    }
    if (landing.wiring === "pull_request") {
      return "Saved. The agent's files belong to its team, so the change is waiting in a pull request.";
    }
    if (landing.wiring === "unchanged") {
      return "Saved. The agent's files already said this.";
    }
    return "Saved. The agent picks this up the next time it starts.";
  });

  // --- The controls ---------------------------------------------------------

  function addRow(kind: Kind): void {
    if (frozen) {
      return;
    }
    const row: Row = { id: nextRowId++, kind, name: "", value: "", fresh: true };
    // At the top: a row added below a long list is a row nobody sees.
    rows = [row, ...rows];
    focusId = row.id;
  }

  function removeRow(id: number): void {
    if (frozen) {
      return;
    }
    rows = rows.filter((row) => row.id !== id);
  }

  function discard(): void {
    rows = seedRows(baseKeys, baseEnv);
    paste = "";
    pasteProblems = [];
    pasteOpen = false;
    saveError = null;
    landing = null;
  }

  async function handleSave(): Promise<void> {
    if (!canSave) {
      return;
    }
    saveError = null;
    landing = null;
    let keys = baseKeys;
    let values = baseEnv;
    let landed: Landing | null = null;
    try {
      if (secretsPending) {
        const result = await saveSecrets.mutateAsync({
          set: secretSet,
          remove: secretRemove,
        });
        keys = result.keys ?? [];
        landed = {
          wiring: result.wiring,
          pullRequest: result.pull_request,
          pendingUnwire: result.pending_unwire,
          emptied: result.emptied,
        };
      }
      if (envPending) {
        const result = await saveEnv.mutateAsync({ set: envSet, remove: envRemove });
        values = result.env ?? {};
        // The variables' own outcome, unless the secrets half reported one
        // of the two outcomes that carry more than "saved": those two say
        // something about the secret that nothing else here would.
        if (!landed?.pendingUnwire && !landed?.emptied) {
          landed = { wiring: result.wiring, pullRequest: result.pull_request };
        }
      }
    } catch (err: unknown) {
      // The secrets half is sent first and on its own, so a failure there
      // leaves the variables exactly as they were and the draft intact.
      saveError = errorMessage(err, "Couldn't save that. Try again.");
      return;
    }
    landing = landed;
    baseKeys = keys;
    baseEnv = values;
    baseSignature = JSON.stringify([keys, values]);
    rows = seedRows(keys, values);
    paste = "";
    pasteProblems = [];
    pasteOpen = false;
  }

  // --- Paste a .env ---------------------------------------------------------

  let pasteOpen = $state(false);
  let paste = $state("");
  let pasteProblems = $state<string[]>([]);

  function handlePasteAdd(): void {
    if (frozen) {
      return;
    }
    const parsed = parseEnvPaste(paste);
    pasteProblems = parsed.problems;
    if (parsed.problems.length > 0) {
      // Nothing is added until every line is fixed: half a pasted file is
      // a worse outcome than none of it, because which half landed is not
      // something the table can show.
      return;
    }
    const pasted = Object.keys(parsed.values);
    if (pasted.length === 0) {
      pasteProblems = ["There is nothing to add here yet."];
      return;
    }
    // The same cap the server holds, checked before anything is queued: a
    // pasted file is exactly where somebody goes past it, and answering
    // here says how far over they are rather than just refusing.
    const held = rows.filter((row) => row.kind === "secret").map((row) => row.name);
    const after = new Set([...baseKeys, ...held, ...pasted]).size;
    if (after > MAX_KEYS) {
      pasteProblems = [
        `That would leave ${after} secrets, and ${MAX_KEYS} is the most a list can hold.`,
      ];
      return;
    }
    const added: Row[] = pasted.map((name) => ({
      id: nextRowId++,
      kind: "secret" as Kind,
      name,
      value: parsed.values[name],
      fresh: true,
    }));
    const names = new Set(pasted);
    rows = [
      ...added,
      ...rows.filter((row) => !(row.kind === "secret" && names.has(row.name))),
    ];
    paste = "";
    pasteProblems = [];
    pasteOpen = false;
  }

  // --- Small helpers --------------------------------------------------------

  function whenChanged(updatedAt: string | null | undefined): string | null {
    if (!updatedAt) {
      return null;
    }
    const at = new Date(updatedAt);
    return Number.isNaN(at.getTime()) ? null : at.toLocaleDateString();
  }

  /** Focus the name field of a row somebody just added. Runs at mount, so
   * only the new row gets it and an older row never steals it back. */
  function focusOnMount(node: HTMLInputElement, wanted: boolean) {
    if (wanted) {
      node.focus();
    }
  }
</script>

<section class="secrets" aria-labelledby="secrets-heading">
  <div class="secrets__header">
    <h2 id="secrets-heading" class="secrets__heading">Secrets and environment</h2>
    <p class="secrets__lead">{LEAD}</p>
    {#if secrets.isError}
      <p class="secrets__note secrets__note--fail">
        {errorMessage(secrets.error, "Couldn't load the secrets.")}
        <button type="button" class="secrets__linkish" onclick={() => secrets.refetch()}>
          Try again
        </button>
      </p>
    {:else if !secrets.isPending && secrets.data?.platform !== true}
      <p class="secrets__note">
        This room has no secrets platform configured, so it cannot keep secrets for an
        agent. Environment variables still work.
      </p>
    {:else if !secrets.isPending && secrets.data?.unreadable}
      <p class="secrets__note">
        The room has nobody to read this agent's secrets as, so it cannot show them. An
        operator can open them in the secrets platform.
      </p>
    {:else if secretsReadable && secrets.data?.secret_name}
      <p class="secrets__note">
        Secret values are sent once and never shown again. They reach the agent as
        <code>{secrets.data.secret_name}</code>.
        {#if whenChanged(secrets.data.updated_at)}
          Last changed {whenChanged(secrets.data.updated_at)}.
        {/if}
      </p>
    {/if}
  </div>

  {#if env.isPending || secrets.isPending}
    <Skeleton />
  {:else if env.isError}
    <ErrorState
      message={errorMessage(env.error, "Couldn't load the environment variables.")}
      onretry={() => env.refetch()}
    />
  {:else}
    <div class="secrets__toolbar">
      <div class="secrets__tools">
        {#if secretsReadable}
          <button
            type="button"
            class="secrets__button"
            disabled={frozen}
            onclick={() => addRow("secret")}
          >
            + Secret
          </button>
        {/if}
        <button
          type="button"
          class="secrets__button"
          disabled={frozen}
          onclick={() => addRow("variable")}
        >
          + Variable
        </button>
      </div>
      {#if secretsReadable}
        <button
          type="button"
          class="secrets__button secrets__button--quiet"
          aria-expanded={pasteOpen}
          disabled={frozen}
          onclick={() => (pasteOpen = !pasteOpen)}
        >
          Paste .env
        </button>
      {/if}
    </div>

    {#if pasteOpen && secretsReadable}
      <div class="secrets__paste">
        <label class="secrets__paste-label" for="secrets-paste">
          One KEY=value per line
        </label>
        <textarea
          id="secrets-paste"
          class="secrets__textarea"
          rows="3"
          bind:value={paste}
          spellcheck="false"
          disabled={frozen}
        ></textarea>
        {#if pasteProblems.length > 0}
          <ul class="secrets__problems" role="alert">
            {#each pasteProblems as problem (problem)}
              <li>{problem}</li>
            {/each}
          </ul>
        {/if}
        <button
          type="button"
          class="secrets__button"
          disabled={frozen || paste.trim().length === 0}
          onclick={handlePasteAdd}
        >
          Add
        </button>
      </div>
    {/if}

    <table class="secrets__table">
      <thead>
        <tr>
          <th scope="col" class="secrets__col-name">Name</th>
          <th scope="col" class="secrets__col-kind">Kind</th>
          <th scope="col" class="secrets__col-value">Value</th>
          <th scope="col" class="secrets__col-remove">
            <span class="secrets__sr">Remove</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {#if rows.length === 0}
          <tr>
            <td class="secrets__empty" colspan="4">No secrets or variables yet.</td>
          </tr>
        {/if}
        {#each rows as row, index (row.id)}
          <tr class="secrets__row">
            <td class="secrets__cell">
              {#if row.kind === "secret" && !row.fresh}
                <code class="secrets__name">{row.name}</code>
              {:else}
                <input
                  class="secrets__input"
                  type="text"
                  aria-label={`Name ${index + 1}`}
                  aria-invalid={refusals[index] !== null}
                  autocomplete="off"
                  spellcheck="false"
                  bind:value={row.name}
                  disabled={frozen}
                  use:focusOnMount={row.id === focusId}
                />
              {/if}
            </td>
            <td class="secrets__cell">
              <span class="secrets__kind">
                {row.kind === "secret" ? "Secret" : "Variable"}
              </span>
            </td>
            <td class="secrets__cell">
              {#if row.kind === "secret" && !row.fresh}
                <span class="secrets__dots" aria-hidden="true">••••••••</span>
                <span class="secrets__sr">Value hidden</span>
              {:else}
                <input
                  class="secrets__input"
                  type={row.kind === "secret" ? "password" : "text"}
                  aria-label={`Value ${index + 1}`}
                  autocomplete="off"
                  spellcheck="false"
                  bind:value={row.value}
                  disabled={frozen}
                />
              {/if}
            </td>
            <td class="secrets__cell secrets__cell--remove">
              <button
                type="button"
                class="secrets__remove"
                aria-label={row.name.length > 0
                  ? `Remove ${row.name}`
                  : `Remove row ${index + 1}`}
                disabled={frozen}
                onclick={() => removeRow(row.id)}
              >
                <span aria-hidden="true">✕</span>
              </button>
            </td>
          </tr>
          {#if refusals[index]}
            <tr>
              <td class="secrets__refusal" colspan="4">
                <span role="alert">{refusals[index]}</span>
              </td>
            </tr>
          {/if}
        {/each}
      </tbody>
    </table>

    <div class="secrets__footer">
      {#if saveError}
        <p class="secrets__status secrets__fail" role="alert">{saveError}</p>
      {:else if landingText}
        <p class="secrets__status">
          <span role="status">{landingText}</span>
          {#if landing?.pullRequest}
            <a
              class="secrets__link"
              href={landing.pullRequest}
              target="_blank"
              rel="noreferrer"
            >
              Open the pull request
            </a>
          {/if}
        </p>
      {/if}
      <div class="secrets__actions">
        {#if hasPending}
          <button
            type="button"
            class="secrets__button secrets__button--quiet"
            disabled={saving}
            onclick={discard}
          >
            Discard
          </button>
        {/if}
        <button
          type="button"
          class="secrets__button secrets__button--primary"
          disabled={!canSave}
          onclick={handleSave}
        >
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
  {/if}
</section>

<style>
  .secrets {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .secrets__header {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .secrets__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .secrets__lead,
  .secrets__note {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .secrets__note--fail {
    color: var(--fail);
  }

  .secrets__note code {
    font-size: 0.8rem;
  }

  .secrets__toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-xs);
  }

  .secrets__tools {
    display: flex;
    gap: var(--space-xs);
  }

  .secrets__button {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--ink);
    font: inherit;
    font-size: 0.85rem;
    font-weight: 600;
    line-height: 1.4;
    cursor: pointer;
  }

  .secrets__button:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .secrets__button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .secrets__button--quiet {
    border-color: transparent;
    color: var(--muted);
  }

  .secrets__button--primary {
    border-color: var(--accent);
    background: var(--accent);
    color: var(--accent-ink);
  }

  .secrets__button--primary:hover:not(:disabled) {
    color: var(--accent-ink);
    filter: brightness(1.05);
  }

  .secrets__linkish {
    padding: 0;
    border: none;
    background: none;
    color: var(--accent);
    font: inherit;
    font-size: 0.85rem;
    text-decoration: underline;
    cursor: pointer;
  }

  .secrets__paste {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-2xs);
  }

  .secrets__paste-label {
    color: var(--muted);
    font-size: 0.8rem;
  }

  .secrets__textarea {
    width: 100%;
    padding: var(--space-2xs) var(--space-xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.8rem;
    resize: vertical;
  }

  .secrets__problems {
    margin: 0;
    padding-left: var(--space-md);
    color: var(--fail);
    font-size: 0.8rem;
  }

  .secrets__table {
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
  }

  .secrets__table th {
    padding: 0 var(--space-xs) var(--space-2xs);
    border-bottom: 1px solid var(--border);
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-align: left;
    text-transform: uppercase;
  }

  .secrets__col-kind {
    width: 6rem;
  }

  .secrets__col-remove {
    width: 2.25rem;
  }

  .secrets__row {
    height: 36px;
  }

  .secrets__cell {
    padding: 2px var(--space-xs);
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
  }

  .secrets__cell--remove {
    text-align: right;
  }

  .secrets__name {
    display: block;
    overflow: hidden;
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.8rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .secrets__kind {
    color: var(--muted);
    font-size: 0.7rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .secrets__dots {
    color: var(--muted);
    letter-spacing: 0.1em;
  }

  .secrets__input {
    width: 100%;
    padding: var(--space-2xs) var(--space-xs);
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.8rem;
  }

  .secrets__input:hover:not(:disabled) {
    border-color: var(--border);
  }

  .secrets__input:focus-visible {
    border-color: transparent;
    background: var(--bg);
    outline: 2px solid var(--accent);
    outline-offset: -1px;
  }

  .secrets__input[aria-invalid="true"] {
    border-color: var(--fail);
  }

  .secrets__remove {
    padding: 0 var(--space-2xs);
    border: none;
    background: transparent;
    color: var(--muted);
    font: inherit;
    font-size: 0.85rem;
    line-height: 1;
    cursor: pointer;
  }

  .secrets__remove:hover:not(:disabled) {
    color: var(--fail);
  }

  .secrets__remove:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .secrets__empty {
    padding: var(--space-md);
    border-bottom: 1px solid var(--border);
    color: var(--muted);
    font-size: 0.85rem;
    text-align: center;
  }

  .secrets__refusal {
    padding: 0 var(--space-xs) var(--space-2xs);
    border-bottom: 1px solid var(--border);
    color: var(--fail);
    font-size: 0.8rem;
  }

  .secrets__footer {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-xs);
  }

  .secrets__status {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-xs);
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .secrets__fail {
    color: var(--fail);
  }

  .secrets__link {
    color: var(--accent);
    font-weight: 600;
  }

  .secrets__actions {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    margin-left: auto;
  }

  .secrets__sr {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
</style>
