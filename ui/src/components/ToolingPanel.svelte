<script lang="ts">
  // The staged door for an agent's LAW-to-be (2026-08-13 addendum): files
  // land in `bundle/tools/*.{py,go}` in the agent's own repo, validated and
  // delivered the same way knowledge/skills are — but there is no
  // enforcement yet, and this panel says so plainly (the standing notice
  // below, always visible, never conditional on any query state). Two doors
  // write into the same list: a single-file Upload (file picker + filename
  // + content, same form family as MemoryPanel's Teach-from-a-file, just
  // `.py`/`.go` instead of `.md`/`.txt`) and a repo Import (owner/repo +
  // folder path; the server validates every matching file independently and
  // reports per-file results — one bad file never sinks the rest). Remove
  // is a plain files-not-memories delete: a browser confirm(), no bespoke
  // copy or Modal round trip (contrast MemoryPanel's intake Remove, which
  // gates behind a Modal because its copy has to explain that already-
  // ingested memories survive the file's removal — tooling carries no such
  // caveat to make).
  //
  // Delivery-chip reactivity: copied verbatim pattern from
  // SkillsPanel/MemoryPanel — see MemoryPanel's doc comment for the full
  // rationale on why `deliverySha` is a plain `writable`/`derived` pair
  // rather than `$state`/`$derived` (the installed @tanstack/svelte-query
  // adapter only re-derives a query's options from an actual Svelte store).
  // `deliveryQuery` itself is shared as-is, unchanged from those panels: any
  // sha this room pushes (an upload, an import that landed at least one
  // file, a delete) is delivered and checked the same way. An import that
  // landed nothing (`sha: null`) has nothing to track — its per-file results
  // are shown without a chip, per the addendum.
  //
  // Each row's own "Delivered"/"Not yet delivered" pill (from the list
  // query's `delivered` field) is a different thing from the shared chip
  // below: the pill is the server's ancestor-derived, persisted answer for
  // THAT file as of the last list fetch; the chip is ephemeral, live
  // feedback for whichever mutation most recently ran. The two can disagree
  // briefly (the chip still says "Delivering…" while a row's pill hasn't
  // caught up yet) — that's expected, not a bug: the row corrects itself
  // once the list query refetches (on invalidation, or the user's next
  // visit). Separately from both, every row also carries a fixed "stored"
  // badge that never changes with data — the addendum's explicit
  // honesty guarantee that there is no "active"/"enforced" state to claim.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { derived, writable } from "svelte/store";
  import EmptyState from "./EmptyState.svelte";
  import ErrorState from "./ErrorState.svelte";
  import Skeleton from "./Skeleton.svelte";
  import { errorMessage, type ToolingFile, type ToolingImportResult } from "../lib/api";
  import {
    deleteToolingMutation,
    deliveryQuery,
    importToolingMutation,
    toolingQuery,
    uploadToolingMutation,
  } from "../lib/queries";

  let { agentName }: { agentName: string } = $props();

  const queryClient = useQueryClient();
  // `agentName` is read once here rather than tracked reactively: same
  // reasoning as SkillsPanel/MemoryPanel — AgentDetail (the only caller)
  // remounts this component via `{#key route.name}` whenever the routed
  // agent changes, so this prop is fixed for the component's entire
  // lifetime.
  // svelte-ignore state_referenced_locally
  const tooling = createQuery(toolingQuery(agentName));
  // svelte-ignore state_referenced_locally
  const uploadMutation = createMutation(uploadToolingMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const importMutation = createMutation(importToolingMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const deleteMutation = createMutation(deleteToolingMutation(queryClient, agentName));

  // --- Delivery chip (shared by upload, a passing import, and delete) -----
  // (see the module doc comment above for the full store-pair rationale)

  const DELIVERED_CLEAR_MS = 5000;

  const deliverySha = writable<string | null>(null);
  const deliveryOptions = derived(deliverySha, ($sha) => deliveryQuery(agentName, $sha));
  const delivery = createQuery(deliveryOptions);

  let clearTimer: ReturnType<typeof setTimeout> | undefined;

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
    // failure it can't back up.
    return { tone: "pending", text: "Delivering changes…" };
  });

  // --- Upload: file picker + editable filename + content -------------------

  // Mirrors the server's TOOLING_FILENAME_RE (kobo_api.tooling): a lowercase
  // letter, then lowercase/digit/underscore/hyphen (1-64 chars total), ending
  // in .py or .go.
  const TOOLING_FILENAME_RE = /^[a-z][a-z0-9_-]{0,63}\.(py|go)$/;

  // Matches the server's TOOLING_MAX_BYTES exactly — a client-side guard so
  // an oversized paste is caught before the round trip, not a separate rule
  // of its own.
  const TOOLING_MAX_BYTES = 524_288;

  let uploadFilename = $state("");
  let uploadContent = $state("");
  let uploadError = $state<string | null>(null);
  let fileReadError = $state<string | null>(null);

  const uploadContentBytes = $derived(new TextEncoder().encode(uploadContent).length);
  const uploadContentTooLarge = $derived(uploadContentBytes > TOOLING_MAX_BYTES);

  const uploadFilenameError = $derived.by((): string | null => {
    if (uploadFilename.length === 0) {
      return null; // nothing to say about an empty field yet
    }
    if (!TOOLING_FILENAME_RE.test(uploadFilename)) {
      return "Use lowercase letters, numbers, underscores, and hyphens, ending in .py or .go.";
    }
    return null;
  });

  const canUpload = $derived.by((): boolean => {
    if ($uploadMutation.isPending) {
      return false;
    }
    if (uploadFilename.length === 0 || uploadFilenameError !== null) {
      return false;
    }
    if (uploadContent.trim().length === 0 || uploadContentTooLarge) {
      return false;
    }
    return true;
  });

  function handleFileSelect(event: Event): void {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) {
      return;
    }
    fileReadError = null;
    const reader = new FileReader();
    reader.onload = (e) => {
      try {
        const text = e.target?.result as string;
        uploadContent = text;
        // Extract lowercased basename without extension, then append the
        // original extension back — same convention as MemoryPanel's own
        // handleFileSelect.
        const dotIndex = file.name.lastIndexOf(".");
        const withoutExt = dotIndex > 0 ? file.name.slice(0, dotIndex) : file.name;
        const ext = file.name.slice(withoutExt.length);
        uploadFilename = (withoutExt + ext).toLowerCase();
      } catch {
        fileReadError = "Couldn't read this file. Try another.";
      }
    };
    reader.onerror = () => {
      fileReadError = "Couldn't read this file. Try another.";
    };
    reader.readAsText(file);
  }

  function handleUpload(event: SubmitEvent): void {
    event.preventDefault();
    if (!canUpload) {
      return;
    }
    uploadError = null;
    $uploadMutation.mutate(
      { filename: uploadFilename, content: uploadContent },
      {
        onSuccess: (result) => {
          beginDelivery(result.sha);
          uploadFilename = "";
          uploadContent = "";
        },
        onError: (err: unknown) => {
          // The server's 422 body (including syntax-error-with-line-number
          // messages from ast.parse) is written in plain language for a
          // human — surfaced as-is, not paraphrased over.
          uploadError = errorMessage(err, "Couldn't upload this file. Try again.");
        },
      },
    );
  }

  // --- Import: owner/repo + folder path, per-file results -------------------

  let importRepo = $state("");
  let importPath = $state("");
  let importError = $state<string | null>(null);
  let importResult = $state<ToolingImportResult | null>(null);

  const canImport = $derived.by((): boolean => {
    if ($importMutation.isPending) {
      return false;
    }
    return importRepo.trim().length > 0 && importPath.trim().length > 0;
  });

  function handleImport(event: SubmitEvent): void {
    event.preventDefault();
    if (!canImport) {
      return;
    }
    importError = null;
    importResult = null;
    $importMutation.mutate(
      { repo: importRepo.trim(), path: importPath.trim() },
      {
        onSuccess: (result) => {
          importResult = result;
          // sha is null when nothing passed validation — nothing was
          // pushed, so there's nothing for the shared chip to track.
          if (result.sha !== null) {
            beginDelivery(result.sha);
          }
          importRepo = "";
          importPath = "";
        },
        onError: (err: unknown) => {
          // Includes the server's verbatim 404 when the path doesn't exist
          // in the source repo, alongside its plain 422s.
          importError = errorMessage(err, "Couldn't import from that repo. Try again.");
        },
      },
    );
  }

  // --- Remove: files, not memories — a plain confirm, no bespoke copy -----

  let removeError = $state<string | null>(null);

  function handleRemove(file: ToolingFile): void {
    if ($deleteMutation.isPending) {
      return;
    }
    if (!window.confirm(`Remove ${file.name}? This can't be undone.`)) {
      return;
    }
    removeError = null;
    $deleteMutation.mutate(file.name, {
      onSuccess: (result) => {
        beginDelivery(result.sha);
      },
      onError: (err: unknown) => {
        removeError = errorMessage(err, "Couldn't remove it. Try again.");
      },
    });
  }

  /** Human-readable size for a row's `content_bytes` — same "1.2 KB"/"48 B"
   * style as MemoryPanel's intake list, for the same plain-tone reasons. */
  function humanSize(bytes: number): string {
    if (bytes < 1024) {
      return `${bytes} B`;
    }
    const kb = bytes / 1024;
    if (kb < 1024) {
      return `${kb.toFixed(kb < 10 ? 1 : 0)} KB`;
    }
    return `${(kb / 1024).toFixed(1)} MB`;
  }
</script>

<section class="tooling-panel" aria-labelledby="tooling-panel-heading">
  <div class="tooling-panel__header">
    <h2 id="tooling-panel-heading" class="tooling-panel__heading">Tooling</h2>
    <p class="tooling-panel__subtitle">The rules this agent will be held to.</p>
  </div>

  <div class="tooling-panel__notice" role="status">
    Stored and delivered, not yet enforced — enforcement arrives with the runtime's next
    upgrade.
  </div>

  <form class="tooling-form" onsubmit={handleUpload}>
    <h3 class="tooling-form__heading">Upload</h3>

    <div class="tooling-form__field">
      <label class="tooling-form__label" for="tooling-file">Choose file</label>
      <div class="tooling-form__file-row">
        <input
          id="tooling-file"
          class="tooling-form__file-input"
          type="file"
          accept=".py,.go"
          onchange={handleFileSelect}
          aria-describedby="tooling-file-status"
        />
        <div id="tooling-file-status">
          {#if fileReadError}
            <p class="tooling-form__status tooling-form__status--error" role="alert">
              {fileReadError}
            </p>
          {/if}
        </div>
      </div>
    </div>

    <div class="tooling-form__field">
      <label class="tooling-form__label" for="tooling-filename">File name</label>
      <input
        id="tooling-filename"
        class="tooling-form__input"
        type="text"
        bind:value={uploadFilename}
        autocomplete="off"
        spellcheck="false"
        aria-describedby="tooling-filename-status"
        aria-invalid={uploadFilenameError !== null}
      />
      <p class="tooling-form__help">lowercase_with_underscores-or-dashes, .py or .go</p>
      <div id="tooling-filename-status">
        {#if uploadFilenameError}
          <p class="tooling-form__status tooling-form__status--error" role="alert">
            {uploadFilenameError}
          </p>
        {/if}
      </div>
    </div>

    <div class="tooling-form__field">
      <label class="tooling-form__label" for="tooling-content">Content</label>
      <textarea
        id="tooling-content"
        class="tooling-form__textarea"
        bind:value={uploadContent}
        rows="8"
        aria-describedby="tooling-content-status"
      ></textarea>
      <div id="tooling-content-status">
        {#if uploadContentTooLarge}
          <p class="tooling-form__status tooling-form__status--error" role="alert">
            That's too much text — keep it under 512KB (524,288 bytes).
          </p>
        {:else if uploadContent.trim().length === 0}
          <p class="tooling-form__help">Add a bit of code before uploading.</p>
        {/if}
      </div>
    </div>

    {#if uploadError}
      <p class="tooling-form__status tooling-form__status--error" role="alert">{uploadError}</p>
    {/if}

    <button type="submit" class="tooling-form__submit" disabled={!canUpload}>
      {$uploadMutation.isPending ? "Uploading…" : "Upload"}
    </button>
  </form>

  <form class="tooling-form" onsubmit={handleImport}>
    <h3 class="tooling-form__heading">Import from a repo</h3>

    <div class="tooling-form__field">
      <label class="tooling-form__label" for="tooling-import-repo">Repository</label>
      <input
        id="tooling-import-repo"
        class="tooling-form__input"
        type="text"
        bind:value={importRepo}
        autocomplete="off"
        spellcheck="false"
        placeholder="owner/repo"
      />
    </div>

    <div class="tooling-form__field">
      <label class="tooling-form__label" for="tooling-import-path">Folder path</label>
      <input
        id="tooling-import-path"
        class="tooling-form__input"
        type="text"
        bind:value={importPath}
        autocomplete="off"
        spellcheck="false"
        placeholder="bundle/tools"
      />
    </div>

    {#if importError}
      <p class="tooling-form__status tooling-form__status--error" role="alert">{importError}</p>
    {/if}

    <button type="submit" class="tooling-form__submit" disabled={!canImport}>
      {$importMutation.isPending ? "Importing…" : "Import"}
    </button>

    {#if importResult}
      <div class="tooling-import__results">
        {#if importResult.imported.length > 0}
          <div class="tooling-import__group">
            <p class="tooling-import__group-label tooling-import__group-label--ok">Imported</p>
            <ul class="tooling-import__list">
              {#each importResult.imported as name (name)}
                <li class="tooling-import__list-item">{name}</li>
              {/each}
            </ul>
          </div>
        {/if}
        {#if importResult.rejected.length > 0}
          <div class="tooling-import__group">
            <p class="tooling-import__group-label tooling-import__group-label--fail">Rejected</p>
            <ul class="tooling-import__list">
              {#each importResult.rejected as entry (entry.name)}
                <li class="tooling-import__list-item">
                  <span class="tooling-import__rejected-name">{entry.name}</span>
                  <span class="tooling-import__rejected-reason">{entry.reason}</span>
                </li>
              {/each}
            </ul>
          </div>
        {/if}
        {#if importResult.imported.length === 0 && importResult.rejected.length === 0}
          <p class="tooling-import__empty">No .py or .go files found there.</p>
        {/if}
      </div>
    {/if}
  </form>

  {#if $tooling.isPending}
    <Skeleton rows={3} />
  {:else if $tooling.isError}
    <ErrorState
      message={errorMessage($tooling.error, "Couldn't load this agent's tooling files.")}
      onretry={() => $tooling.refetch()}
    />
  {:else if $tooling.data.length === 0}
    <EmptyState message="Nothing here yet — upload a file or import from a repo." />
  {:else}
    {#if removeError}
      <p class="tooling-panel__remove-error" role="alert">{removeError}</p>
    {/if}
    <ul class="tooling-panel__list">
      {#each $tooling.data as file (file.name)}
        <li class="tooling-row">
          <span class="tooling-row__body">
            <span class="tooling-row__name">{file.name}</span>
            <span class="tooling-row__meta">{humanSize(file.content_bytes)}</span>
          </span>
          <span
            class="tooling-row__delivered tooling-row__delivered--{file.delivered
              ? 'yes'
              : 'no'}"
          >
            {file.delivered ? "Delivered" : "Not yet delivered"}
          </span>
          <span class="tooling-row__badge">stored</span>
          <button
            type="button"
            class="tooling-row__remove"
            disabled={$deleteMutation.isPending}
            aria-label={`Remove ${file.name}`}
            onclick={() => handleRemove(file)}
          >
            Remove
          </button>
        </li>
      {/each}
    </ul>
  {/if}

  {#if $deliverySha !== null}
    <div
      class="tooling-panel__delivery tooling-panel__delivery--{deliveryView.tone}"
      role="status"
      aria-live="polite"
    >
      {#if deliveryView.tone === "pending"}
        <span class="tooling-panel__delivery-dot pulse" aria-hidden="true"></span>
      {/if}
      <span>{deliveryView.text}</span>
    </div>
  {/if}
</section>

<style>
  .tooling-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .tooling-panel__header {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .tooling-panel__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .tooling-panel__subtitle {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .tooling-panel__notice {
    padding: var(--space-sm) var(--space-md);
    border: 1px solid var(--warn);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--warn) 10%, transparent);
    color: var(--ink);
    font-size: 0.85rem;
  }

  .tooling-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
  }

  .tooling-form__heading {
    margin: 0;
    color: var(--ink);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .tooling-form__field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .tooling-form__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.9rem;
  }

  .tooling-form__input,
  .tooling-form__textarea {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .tooling-form__textarea {
    font-family: var(--font-mono);
    font-size: 0.85rem;
    resize: vertical;
  }

  .tooling-form__input:focus-visible,
  .tooling-form__textarea:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .tooling-form__help {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .tooling-form__status {
    margin: 0;
    font-size: 0.8rem;
  }

  .tooling-form__status--error {
    color: var(--fail);
  }

  .tooling-form__file-row {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .tooling-form__file-input {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-weight: 600;
    font-size: 0.9rem;
    cursor: pointer;
  }

  .tooling-form__file-input:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .tooling-form__submit {
    align-self: flex-start;
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .tooling-form__submit:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .tooling-form__submit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .tooling-import__results {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
  }

  .tooling-import__group {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .tooling-import__group-label {
    margin: 0;
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .tooling-import__group-label--ok {
    color: var(--ok);
  }

  .tooling-import__group-label--fail {
    color: var(--fail);
  }

  .tooling-import__list {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .tooling-import__list-item {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-xs);
    overflow-wrap: anywhere;
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.85rem;
  }

  .tooling-import__rejected-name {
    color: var(--ink);
    font-weight: 600;
  }

  .tooling-import__rejected-reason {
    color: var(--muted);
    font-family: var(--font-sans);
  }

  .tooling-import__empty {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .tooling-panel__remove-error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .tooling-panel__list {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .tooling-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
  }

  .tooling-row__body {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 0;
  }

  .tooling-row__name {
    overflow-wrap: anywhere;
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .tooling-row__meta {
    color: var(--muted);
    font-size: 0.8rem;
  }

  .tooling-row__delivered {
    display: inline-flex;
    flex-shrink: 0;
    padding: 0.15rem var(--space-xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    font-size: 0.75rem;
    font-weight: 600;
    white-space: nowrap;
  }

  .tooling-row__delivered--yes {
    border-color: var(--ok);
    color: var(--ok);
    background: color-mix(in srgb, var(--ok) 12%, transparent);
  }

  .tooling-row__delivered--no {
    border-color: var(--muted);
    color: var(--muted);
    background: transparent;
  }

  .tooling-row__badge {
    display: inline-flex;
    flex-shrink: 0;
    padding: 0.15rem var(--space-xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.75rem;
    white-space: nowrap;
  }

  .tooling-row__remove {
    flex-shrink: 0;
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--muted);
    cursor: pointer;
    white-space: nowrap;
  }

  .tooling-row__remove:hover:not(:disabled) {
    border-color: var(--fail);
    color: var(--fail);
  }

  .tooling-row__remove:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .tooling-panel__delivery {
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

  .tooling-panel__delivery--ok {
    border-color: var(--ok);
    color: var(--ok);
  }

  .tooling-panel__delivery--fail {
    border-color: var(--fail);
    color: var(--fail);
  }

  .tooling-panel__delivery-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--pulse);
  }
</style>
