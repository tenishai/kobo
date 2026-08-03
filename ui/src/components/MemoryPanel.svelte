<script lang="ts">
  // What a deployed agent knows from the start (`bundle/knowledge/*.md` in
  // its repo, P2b) — rendered only once the agent is `ready` (same gate as
  // ChatPanel, in AgentDetail). Every save/delete here is delivered through
  // a rebuild the user never sees or names: copy speaks only of saving
  // changes and the agent picking them up, never "commit"/"deploy"/"build"/
  // "repo"/"sha"/"pipeline".
  //
  // One shared `Modal` instance drives edit, add, and delete — which of the
  // three (and which file, for edit/delete) lives in `target`; the textarea/
  // filename fields are separate top-level state so "Back to editing" can
  // return to them without losing what was typed, and so the add-mode name
  // field survives the review round trip.
  //
  // Delivery-chip reactivity: `deliveryQuery(name, sha)` needs a *new* query
  // (not just new data on the same one) every time `sha` changes, so a save
  // that lands while an earlier one is still "delivering" doesn't keep
  // polling the stale target. The installed @tanstack/svelte-query adapter
  // (5.90.2, see node_modules/@tanstack/svelte-query/dist/createBaseQuery.js)
  // only re-derives a query's options from an actual Svelte store — it
  // checks `isSvelteStore(options)` and otherwise freezes whatever was
  // passed at mount time, so a `$derived`-computed options object (a plain
  // value, not a store) would never refresh after the first sha. A classic
  // `writable`/`derived` pair is the version-correct equivalent of "local
  // reactive state driving the query" here: `deliverySha` is still owned by
  // this component and still read with the same `$name` syntax as a rune
  // would be in the markup below, it just comes from svelte/store instead
  // of `$state` so `createQuery` actually notices when it changes.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { derived, writable } from "svelte/store";
  import EmptyState from "./EmptyState.svelte";
  import ErrorState from "./ErrorState.svelte";
  import Modal from "./Modal.svelte";
  import Skeleton from "./Skeleton.svelte";
  import { errorMessage, type KnowledgeFile } from "../lib/api";
  import { lineDiff, type DiffLine } from "../lib/diff";
  import {
    deleteKnowledgeMutation,
    deliveryQuery,
    knowledgeQuery,
    saveKnowledgeMutation,
  } from "../lib/queries";

  let { agentName }: { agentName: string } = $props();

  const queryClient = useQueryClient();
  // `agentName` is read once here rather than tracked reactively: same
  // reasoning as ChatPanel's `chatMutation(agentName)` — AgentDetail (the
  // only caller) remounts this component via `{#key route.name}` whenever
  // the routed agent changes, so this prop is fixed for the component's
  // entire lifetime.
  // svelte-ignore state_referenced_locally
  const knowledge = createQuery(knowledgeQuery(agentName));
  // svelte-ignore state_referenced_locally
  const saveMutation = createMutation(saveKnowledgeMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const deleteMutation = createMutation(deleteKnowledgeMutation(queryClient, agentName));

  // --- Delivery chip -----------------------------------------------------

  // How long the "Changes are in place" chip stays up before it clears on
  // its own — see the module doc comment above for why this is a plain
  // store rather than `$state`.
  const DELIVERED_CLEAR_MS = 5000;

  const deliverySha = writable<string | null>(null);
  const deliveryOptions = derived(deliverySha, ($sha) => deliveryQuery(agentName, $sha));
  const delivery = createQuery(deliveryOptions);

  // Handle of the pending "clear the chip" timeout, if any. Not reactive
  // state on purpose — it's bookkeeping for `beginDelivery`/the effect
  // below, not something the template renders.
  let clearTimer: ReturnType<typeof setTimeout> | undefined;

  /** Start tracking a new save/delete's delivery, replacing whatever was
   * being tracked before. Clears any pending "fade the chip" timer first so
   * a save that lands while an earlier one's chip is winding down can't have
   * its own fresh chip yanked away by a timer that belonged to the old sha. */
  function beginDelivery(sha: string): void {
    if (clearTimer !== undefined) {
      clearTimeout(clearTimer);
      clearTimer = undefined;
    }
    deliverySha.set(sha);
  }

  $effect(() => {
    if ($delivery.data?.state !== "delivered") {
      return;
    }
    clearTimer = setTimeout(() => {
      deliverySha.set(null);
      clearTimer = undefined;
    }, DELIVERED_CLEAR_MS);
    return () => {
      if (clearTimer !== undefined) {
        clearTimeout(clearTimer);
        clearTimer = undefined;
      }
    };
  });

  type DeliveryView = { tone: "pending" | "ok" | "fail"; text: string };

  const deliveryView = $derived.by((): DeliveryView => {
    if ($delivery.isError) {
      return {
        tone: "fail",
        text: errorMessage($delivery.error, "Couldn't check on your changes."),
      };
    }
    const data = $delivery.data;
    if (data === undefined) {
      return { tone: "pending", text: "Delivering changes…" };
    }
    if (data.state === "delivered") {
      return { tone: "ok", text: "Changes are in place" };
    }
    if (data.state === "failed") {
      return {
        tone: "fail",
        text: errorMessage(
          data.detail !== null ? new Error(data.detail) : undefined,
          "Something went wrong. Try saving again.",
        ),
      };
    }
    // "building" | "rolling", or any state this code doesn't recognize yet —
    // treated the same as still-in-progress, never claiming success or
    // failure it can't back up (mirrors status.ts's agentTone/stepTone
    // fallback convention).
    return { tone: "pending", text: "Delivering changes…" };
  });

  // --- Modal: edit / add / delete ----------------------------------------

  type Target =
    | { kind: "edit"; file: KnowledgeFile }
    | { kind: "add" }
    | { kind: "delete"; file: KnowledgeFile };

  let target = $state<Target | null>(null);
  let content = $state("");
  let filename = $state("");
  let reviewing = $state(false);
  let modalError = $state<string | null>(null);

  // Above this many (before-lines × after-lines) cells, the LCS table in
  // diff.ts's O(n·m) algorithm gets expensive enough that it's not worth
  // paying for a preview the user is about to skim past anyway.
  const DIFF_LINE_PRODUCT_LIMIT = 1_000_000;

  /** Line count under diff.ts's own convention: an empty string is zero
   * lines, not one (see diff.ts's `splitLines`). This only estimates the
   * cost of a diff for the guard below — it doesn't compute one. */
  function lineCount(text: string): number {
    return text === "" ? 0 : text.split("\n").length;
  }

  function displayName(fullName: string): string {
    return fullName.endsWith(".md") ? fullName.slice(0, -3) : fullName;
  }

  function firstLine(fileContent: string): string {
    const newlineIndex = fileContent.indexOf("\n");
    const line = newlineIndex === -1 ? fileContent : fileContent.slice(0, newlineIndex);
    return line.length > 0 ? line : "No preview yet.";
  }

  function diffMarker(kind: DiffLine["kind"]): string {
    switch (kind) {
      case "added":
        return "+ ";
      case "removed":
        return "− ";
      default:
        return "  ";
    }
  }

  function openEdit(file: KnowledgeFile): void {
    target = { kind: "edit", file };
    content = file.content;
    filename = "";
    reviewing = false;
    modalError = null;
  }

  function openAdd(): void {
    target = { kind: "add" };
    content = "";
    filename = "";
    reviewing = false;
    modalError = null;
  }

  function openDelete(file: KnowledgeFile): void {
    target = { kind: "delete", file };
    modalError = null;
  }

  function closeModal(): void {
    target = null;
    content = "";
    filename = "";
    reviewing = false;
    modalError = null;
  }

  function backToEditing(): void {
    reviewing = false;
    modalError = null;
  }

  const originalContent = $derived(target?.kind === "edit" ? target.file.content : "");
  const contentUnchanged = $derived(content === originalContent);

  const filenameError = $derived.by((): string | null => {
    if (target?.kind !== "add") {
      return null;
    }
    if (filename.length === 0) {
      return null; // nothing to say about an empty field yet
    }
    if (filename.length > 40) {
      return "Use 1-40 characters.";
    }
    if (!/^[a-z0-9-]+$/.test(filename)) {
      return "Use lowercase letters, numbers, and hyphens only.";
    }
    if (!/^[a-z]/.test(filename)) {
      return "Must start with a letter.";
    }
    if ($knowledge.data?.some((file) => file.name === `${filename}.md`)) {
      return "There's already a memory file with this name.";
    }
    return null;
  });

  const canReview = $derived.by((): boolean => {
    if (target === null || target.kind === "delete") {
      return false;
    }
    if (content.trim().length === 0 || contentUnchanged) {
      return false;
    }
    if (target.kind === "add" && (filename.length === 0 || filenameError !== null)) {
      return false;
    }
    return true;
  });

  const diffTooLarge = $derived(
    lineCount(originalContent) * lineCount(content) > DIFF_LINE_PRODUCT_LIMIT,
  );

  const diffLines = $derived.by((): DiffLine[] => {
    if (!reviewing || diffTooLarge) {
      return [];
    }
    return lineDiff(originalContent, content);
  });

  const modalTitle = $derived.by((): string | undefined => {
    if (target === null) {
      return undefined;
    }
    if (target.kind === "add") {
      return "Add memory";
    }
    if (target.kind === "delete") {
      return `Remove ${displayName(target.file.name)}`;
    }
    return `Edit ${displayName(target.file.name)}`;
  });

  function handleSave(): void {
    if (target === null || target.kind === "delete") {
      return;
    }
    const targetFilename = target.kind === "edit" ? target.file.name : `${filename}.md`;
    modalError = null;
    $saveMutation.mutate(
      { filename: targetFilename, content },
      {
        onSuccess: (result) => {
          beginDelivery(result.sha);
          closeModal();
        },
        onError: (err: unknown) => {
          modalError = errorMessage(err, "Couldn't save. Try again.");
        },
      },
    );
  }

  function handleDelete(): void {
    if (target?.kind !== "delete") {
      return;
    }
    const targetFilename = target.file.name;
    modalError = null;
    $deleteMutation.mutate(targetFilename, {
      onSuccess: (result) => {
        beginDelivery(result.sha);
        closeModal();
      },
      onError: (err: unknown) => {
        modalError = errorMessage(err, "Couldn't remove it. Try again.");
      },
    });
  }
</script>

<section class="memory-panel" aria-labelledby="memory-panel-heading">
  <div class="memory-panel__header">
    <div>
      <h2 id="memory-panel-heading" class="memory-panel__heading">Memory</h2>
      <p class="memory-panel__subtitle">What your agent knows from the start.</p>
    </div>
    <button type="button" class="memory-panel__add" onclick={openAdd}>Add memory</button>
  </div>

  {#if $knowledge.isPending}
    <Skeleton rows={3} />
  {:else if $knowledge.isError}
    <ErrorState
      message={errorMessage($knowledge.error, "Couldn't load what this agent knows.")}
      onretry={() => $knowledge.refetch()}
    />
  {:else if $knowledge.data.length === 0}
    <EmptyState message="Nothing here yet — add the first thing your agent should know." />
  {:else}
    <ul class="memory-panel__list">
      {#each $knowledge.data as file (file.name)}
        <li class="memory-row">
          <button type="button" class="memory-row__open" onclick={() => openEdit(file)}>
            <span class="memory-panel__sr-only">Edit </span>
            <span class="memory-row__name">{displayName(file.name)}</span>
            <span class="memory-row__preview">{firstLine(file.content)}</span>
          </button>
          <button
            type="button"
            class="memory-row__remove"
            aria-label={`Remove ${displayName(file.name)}`}
            onclick={() => openDelete(file)}
          >
            Remove
          </button>
        </li>
      {/each}
    </ul>
  {/if}

  {#if $deliverySha !== null}
    <div
      class="memory-panel__delivery memory-panel__delivery--{deliveryView.tone}"
      role="status"
      aria-live="polite"
    >
      {#if deliveryView.tone === "pending"}
        <span class="memory-panel__delivery-dot pulse" aria-hidden="true"></span>
      {/if}
      <span>{deliveryView.text}</span>
    </div>
  {/if}
</section>

<Modal open={target !== null} title={modalTitle} onclose={closeModal}>
  {#if target}
    {@const t = target}
    {#if t.kind === "delete"}
      <p class="memory-modal__confirm-text">
        This removes it from what {agentName} knows. Remove?
      </p>
      {#if modalError}
        <p class="memory-modal__error" role="alert">{modalError}</p>
      {/if}
      <div class="memory-modal__actions">
        <button type="button" class="memory-modal__cancel" onclick={closeModal}>Cancel</button>
        <button
          type="button"
          class="memory-modal__danger"
          disabled={$deleteMutation.isPending}
          onclick={handleDelete}
        >
          {$deleteMutation.isPending ? "Removing…" : "Remove"}
        </button>
      </div>
    {:else if reviewing}
      {#if diffTooLarge}
        <p class="memory-modal__diff-notice">
          Too much changed to preview line by line — review your text, then save.
        </p>
      {:else}
        <div class="memory-modal__diff">
          {#each diffLines as line, i (i)}
            <p class="memory-diff-line memory-diff-line--{line.kind}">{diffMarker(
                line.kind,
              )}{line.text}</p>
          {/each}
        </div>
      {/if}
      {#if modalError}
        <p class="memory-modal__error" role="alert">{modalError}</p>
      {/if}
      <div class="memory-modal__actions">
        <button type="button" class="memory-modal__cancel" onclick={backToEditing}>
          Back to editing
        </button>
        <button
          type="button"
          class="memory-modal__primary"
          disabled={$saveMutation.isPending}
          onclick={handleSave}
        >
          {$saveMutation.isPending ? "Saving…" : "Save"}
        </button>
      </div>
    {:else}
      <form
        class="memory-form"
        onsubmit={(event) => {
          event.preventDefault();
          if (canReview) {
            reviewing = true;
          }
        }}
      >
        {#if t.kind === "add"}
          <div class="memory-form__field">
            <label class="memory-form__label" for="memory-filename">Name</label>
            <input
              id="memory-filename"
              class="memory-form__input"
              type="text"
              bind:value={filename}
              autocomplete="off"
              spellcheck="false"
              aria-describedby="memory-filename-status"
              aria-invalid={filenameError !== null}
            />
            <p class="memory-form__help">
              Lowercase letters, numbers, and hyphens. 1-40 characters.
            </p>
            <div id="memory-filename-status">
              {#if filenameError}
                <p class="memory-form__status memory-form__status--error" role="alert">
                  {filenameError}
                </p>
              {/if}
            </div>
          </div>
        {:else}
          <p class="memory-form__filename">{displayName(t.file.name)}</p>
        {/if}

        <div class="memory-form__field">
          <label class="memory-form__label" for="memory-content">What it knows</label>
          <textarea
            id="memory-content"
            class="memory-form__textarea"
            bind:value={content}
            rows="10"
          ></textarea>
          {#if content.trim().length === 0}
            <p class="memory-form__help">Add a bit of text before saving.</p>
          {/if}
        </div>

        <div class="memory-modal__actions">
          <button type="button" class="memory-modal__cancel" onclick={closeModal}>Cancel</button>
          <button type="submit" class="memory-modal__primary" disabled={!canReview}>
            {contentUnchanged ? "No changes yet" : "Review changes"}
          </button>
        </div>
      </form>
    {/if}
  {/if}
</Modal>

<style>
  .memory-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .memory-panel__sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  .memory-panel__header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-md);
  }

  .memory-panel__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .memory-panel__subtitle {
    margin: var(--space-2xs) 0 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .memory-panel__add {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .memory-panel__add:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .memory-panel__list {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .memory-row {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
  }

  .memory-row__open {
    display: flex;
    flex: 1;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.15rem;
    min-width: 0;
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  .memory-row__open:hover {
    border-color: var(--accent);
  }

  .memory-row__open:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .memory-row__name {
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .memory-row__preview {
    overflow: hidden;
    max-width: 100%;
    color: var(--muted);
    font-size: 0.8rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .memory-row__remove {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--muted);
    cursor: pointer;
    white-space: nowrap;
  }

  .memory-row__remove:hover {
    border-color: var(--fail);
    color: var(--fail);
  }

  .memory-panel__delivery {
    display: inline-flex;
    align-self: flex-start;
    align-items: center;
    gap: var(--space-xs);
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .memory-panel__delivery--ok {
    border-color: var(--ok);
    color: var(--ok);
  }

  .memory-panel__delivery--fail {
    border-color: var(--fail);
    color: var(--fail);
  }

  .memory-panel__delivery-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--pulse);
  }

  .memory-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
  }

  .memory-form__field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .memory-form__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.9rem;
  }

  .memory-form__filename {
    margin: 0;
    color: var(--ink);
    font-family: var(--font-mono);
    font-weight: 600;
    font-size: 0.95rem;
  }

  .memory-form__input,
  .memory-form__textarea {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .memory-form__textarea {
    font-family: var(--font-mono);
    font-size: 0.85rem;
    resize: vertical;
  }

  .memory-form__input:focus-visible,
  .memory-form__textarea:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .memory-form__help {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .memory-form__status {
    margin: 0;
    font-size: 0.8rem;
  }

  .memory-form__status--error {
    color: var(--fail);
  }

  .memory-modal__confirm-text {
    margin: 0;
    color: var(--ink);
  }

  .memory-modal__diff {
    display: flex;
    flex-direction: column;
    max-height: 24rem;
    overflow-y: auto;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }

  .memory-diff-line {
    margin: 0;
    padding: 0.1rem var(--space-xs);
    overflow-wrap: anywhere;
    font-family: var(--font-mono);
    font-size: 0.85rem;
    white-space: pre-wrap;
  }

  .memory-diff-line--added {
    background: color-mix(in srgb, var(--ok) 12%, transparent);
    color: var(--ok);
  }

  .memory-diff-line--removed {
    background: color-mix(in srgb, var(--fail) 12%, transparent);
    color: var(--fail);
  }

  .memory-diff-line--same {
    color: var(--muted);
  }

  .memory-modal__diff-notice {
    margin: 0;
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: var(--muted);
    font-size: 0.85rem;
  }

  .memory-modal__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .memory-modal__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-sm);
  }

  .memory-modal__cancel {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    cursor: pointer;
  }

  .memory-modal__cancel:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .memory-modal__cancel:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .memory-modal__primary {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .memory-modal__primary:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .memory-modal__primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .memory-modal__danger {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--fail);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .memory-modal__danger:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .memory-modal__danger:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
