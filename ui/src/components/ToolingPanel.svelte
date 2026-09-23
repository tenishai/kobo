<script lang="ts">
  // The staged door for an agent's LAW-to-be (2026-08-13 addendum): files
  // land in `bundle/tools/*.{py,go}` in the agent's own repo, validated and
  // delivered the same way knowledge/skills are. Two doors write into the
  // same list: a single-file Upload (file picker + filename + content, same
  // form family as MemoryPanel's Teach-from-a-file, just `.py`/`.go` instead
  // of `.md`/`.txt`) and a repo Import (owner/repo + folder path; the server
  // validates every matching file independently and reports per-file
  // results — one bad file never sinks the rest). Remove is a plain
  // files-not-memories delete, and it asks in the room's own Modal with
  // the same one line it always used; it used to ask through the
  // browser's confirm(), which looked like it came from somewhere else.
  //
  // Enforcement banner graduation (Task 4: card probe + banner graduation):
  // the standing notice below is no longer a single fixed string — it's
  // `bannerView`, derived from the list query's `enforcement` envelope
  // (probed server-side from the running pod's card, never guessed
  // kobo-side). Three states, in precedence order: `locked` (the runtime
  // attests to enforcement but is currently serving refusals because a hook
  // failed to load) always wins and names the broken file; otherwise
  // `attested` swaps in the "live law" banner; otherwise (a pre-enforcement
  // runtime, or one kobo simply couldn't reach) today's original notice
  // still renders, unconditionally, exactly as before this addendum.
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
  // visit). Separately from both, every row also carries a badge — "stored"
  // by default, or "enforced" (accent tone) when the server's `status` field
  // says so (attested, not locked, and this exact file delivered) — never
  // guessed client-side, just read straight off the row.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { derived, writable } from "svelte/store";
  import ErrorState from "./ErrorState.svelte";
  import Modal from "./Modal.svelte";
  import Skeleton from "./Skeleton.svelte";
  import { errorMessage, type ToolingFile } from "../lib/api";
  import {
    deleteToolingMutation,
    deliveryQuery,
    toolingQuery,
    uploadToolingMutation,
  } from "../lib/queries";

  let { agentName, frozen = false }: { agentName: string; frozen?: boolean } = $props();

  const queryClient = useQueryClient();
  // `agentName` is read once here rather than tracked reactively: same
  // reasoning as SkillsPanel/MemoryPanel — AgentDetail (the only caller)
  // remounts this component via `{#key route.name}` whenever the routed
  // agent changes, so this prop is fixed for the component's entire
  // lifetime.
  // svelte-ignore state_referenced_locally
  const tooling = createQuery(() => (toolingQuery(agentName)));
  // svelte-ignore state_referenced_locally
  const uploadMutation = createMutation(() => (uploadToolingMutation(queryClient, agentName)));
  // svelte-ignore state_referenced_locally
  // svelte-ignore state_referenced_locally
  const deleteMutation = createMutation(() => (deleteToolingMutation(queryClient, agentName)));

  // --- Enforcement banner (Task 4: card probe + banner graduation) --------
  // (see the module doc comment above for the three-state precedence)

  type BannerTone = "warn" | "accent" | "fail";
  type BannerView = { tone: BannerTone; text: string };

  const DEFAULT_NOTICE =
    "Stored and delivered, not yet enforced. Enforcement arrives with the runtime's next upgrade.";

  const bannerView = $derived.by((): BannerView => {
    const enforcement = tooling.data?.enforcement;
    if (enforcement?.locked) {
      return {
        tone: "fail",
        text: `${enforcement.locked_file} failed to load. The agent is answering with a refusal until it is fixed or removed.`,
      };
    }
    if (enforcement?.attested) {
      return { tone: "accent", text: "Enforced by the runtime. Every reply passes through these." };
    }
    return { tone: "warn", text: DEFAULT_NOTICE };
  });

  // --- Delivery chip (shared by upload, a passing import, and delete) -----
  // (see the module doc comment above for the full store-pair rationale)

  const DELIVERED_CLEAR_MS = 5000;

  let deliverySha = $state<string | null>(null);
  const delivery = createQuery(() => (deliveryQuery(agentName, deliverySha)));

  let clearTimer: ReturnType<typeof setTimeout> | undefined;

  function beginDelivery(sha: string): void {
    if (clearTimer !== undefined) {
      clearTimeout(clearTimer);
      clearTimer = undefined;
    }
    deliverySha = sha;
  }

  $effect(() => {
    if (delivery.data?.state !== "delivered") {
      return;
    }
    clearTimer = setTimeout(() => {
      deliverySha = null;
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
    if (delivery.isError) {
      return {
        tone: "fail",
        text: errorMessage(delivery.error, "Couldn't check on your changes."),
      };
    }
    const data = delivery.data;
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
    if (uploadMutation.isPending) {
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
    uploadMutation.mutate(
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

  // --- Remove: confirmed in the room's own dialog ------------------------
  //
  // This used to be a bare `window.confirm()`. The wording is the same;
  // the box it appears in is now the same Modal every other panel asks
  // through, so a removal here does not suddenly look like it came from
  // the browser rather than the room.

  let removeError = $state<string | null>(null);
  let removeTarget = $state<ToolingFile | null>(null);

  // The "+ Tool" disclosure. Shut on arrival.
  let uploadOpen = $state(false);

  function openRemove(file: ToolingFile): void {
    removeError = null;
    removeTarget = file;
  }

  function closeRemove(): void {
    removeTarget = null;
  }

  function handleRemove(): void {
    const file = removeTarget;
    if (file === null || deleteMutation.isPending) {
      return;
    }
    removeError = null;
    deleteMutation.mutate(file.name, {
      onSuccess: (result) => {
        removeTarget = null;
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

<section class="panel" aria-labelledby="tooling-panel-heading">
  <div class="panel__header">
    <h2 id="tooling-panel-heading" class="panel__heading">Tooling</h2>
    <p class="panel__lead">
      Every agent ships with colleague tools: it can list and consult the room,
      and share a fact with quorum review. Its repo tools stay dormant until an
      operator approves a repository on the Access tab. Tools added here are
      extras on top of that baseline.
    </p>
  </div>

  <div class="tooling-panel__notice tooling-panel__notice--{bannerView.tone}" role="status">
    {bannerView.text}
  </div>

  <!-- Usable while frozen: it reveals a form, it changes nothing. -->
  <div class="panel__toolbar">
    <div class="panel__tools">
      <button
        type="button"
        class="btn"
        aria-expanded={uploadOpen}
        onclick={() => (uploadOpen = !uploadOpen)}
      >
        + Tool
      </button>
    </div>
  </div>

  {#if uploadOpen}
  <form class="panel__drawer tooling-form" onsubmit={handleUpload}>
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
          disabled={frozen}
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
        disabled={frozen}
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
        disabled={frozen}
      ></textarea>
      <div id="tooling-content-status">
        {#if uploadContentTooLarge}
          <p class="tooling-form__status tooling-form__status--error" role="alert">
            That's too much text. Keep it under 512KB (524,288 bytes).
          </p>
        {:else if uploadContent.trim().length === 0}
          <p class="tooling-form__help">Add a bit of code before uploading.</p>
        {/if}
      </div>
    </div>

    {#if uploadError}
      <p class="tooling-form__status tooling-form__status--error" role="alert">{uploadError}</p>
    {/if}

    <button type="submit" class="btn btn--primary" disabled={frozen || !canUpload}>
      {uploadMutation.isPending ? "Uploading…" : "Upload"}
    </button>
  </form>
  {/if}

  {#if tooling.isPending}
    <Skeleton rows={3} />
  {:else if tooling.isError}
    <ErrorState
      message={errorMessage(tooling.error, "Couldn't load this agent's tooling files.")}
      onretry={() => tooling.refetch()}
    />
  {:else}
    {#if removeError && removeTarget === null}
      <p class="panel__note panel__note--fail" role="alert">{removeError}</p>
    {/if}
    <table class="panel__table">
      <thead>
        <tr>
          <th scope="col">Tool</th>
          <th scope="col" class="tooling-panel__col-size">Size</th>
          <th scope="col" class="tooling-panel__col-state">State</th>
          <th scope="col" class="tooling-panel__col-action">
            <span class="panel__sr">Remove</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {#if tooling.data.files.length === 0}
          <tr>
            <td class="panel__empty" colspan="4">
              Nothing here yet. Upload a file to govern this agent.
            </td>
          </tr>
        {/if}
        {#each tooling.data.files as file (file.name)}
          <tr class="tooling-row">
            <td><code class="panel__name">{file.name}</code></td>
            <td><span class="panel__meta">{humanSize(file.content_bytes)}</span></td>
            <td>
              <span class="tooling-row__states">
                <span
                  class="tooling-row__delivered tooling-row__delivered--{file.delivered
                    ? 'yes'
                    : 'no'}"
                >
                  {file.delivered ? "Delivered" : "Not yet delivered"}
                </span>
                <span
                  class="tooling-row__badge tooling-row__badge--{file.status === 'enforced'
                    ? 'enforced'
                    : 'stored'}"
                >
                  {file.status === "enforced" ? "enforced" : "stored"}
                </span>
              </span>
            </td>
            <td class="panel__cell--right">
              <button
                type="button"
                class="btn btn--quiet btn--danger"
                disabled={frozen || deleteMutation.isPending}
                aria-label={`Remove ${file.name}`}
                onclick={() => openRemove(file)}
              >
                Remove
              </button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

  {#if deliverySha !== null}
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

<Modal
  open={removeTarget !== null}
  title={removeTarget ? `Remove ${removeTarget.name}` : undefined}
  onclose={closeRemove}
>
  {#if removeTarget}
    <p class="tooling-modal__confirm-text">
      Remove {removeTarget.name}? This can't be undone.
    </p>
    {#if removeError}
      <p class="panel__note panel__note--fail" role="alert">{removeError}</p>
    {/if}
    <div class="tooling-modal__actions">
      <button type="button" class="btn btn--quiet" onclick={closeRemove}>Cancel</button>
      <button
        type="button"
        class="btn btn--danger"
        disabled={deleteMutation.isPending}
        onclick={handleRemove}
      >
        {deleteMutation.isPending ? "Removing…" : "Remove"}
      </button>
    </div>
  {/if}
</Modal>

<style>
  .tooling-panel__notice {
    padding: var(--space-sm) var(--space-md);
    border: 1px solid var(--warn);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--warn) 10%, transparent);
    color: var(--ink);
    font-size: 0.85rem;
  }

  /* Task 4 (card probe + banner graduation): the notice's border/background
     tone follows the same server-derived enforcement state the copy itself
     does — warn (today's pre-enforcement default) is the base rule above;
     these two swap in once the runtime attests to enforcement. */
  .tooling-panel__notice--accent {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 10%, transparent);
  }

  .tooling-panel__notice--fail {
    border-color: var(--fail);
    background: color-mix(in srgb, var(--fail) 10%, transparent);
  }

  /* The drawer "+ Tool" opens. No frame of its own: it lives inside the
     panel, and one box is enough. */
  .tooling-form {
    gap: var(--space-md);
    width: 100%;
  }

  .tooling-panel__col-size {
    width: 6rem;
  }

  .tooling-panel__col-state {
    width: 16rem;
  }

  .tooling-panel__col-action {
    width: 8rem;
  }

  .tooling-row__states {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2xs);
  }

  .tooling-modal__confirm-text {
    margin: 0 0 var(--space-md);
    color: var(--ink);
  }

  .tooling-modal__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-xs);
    margin-top: var(--space-md);
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

  /* Task 4 (card probe + banner graduation): accent tone, only when the
     server's `status` field on this row is exactly "enforced" — every other
     row keeps the plain "stored" look above. */
  .tooling-row__badge--enforced {
    border-color: var(--accent);
    color: var(--accent);
    background: color-mix(in srgb, var(--accent) 12%, transparent);
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
