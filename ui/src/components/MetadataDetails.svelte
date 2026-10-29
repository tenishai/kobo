<script lang="ts">
  // The directory layer's edit surface (persona-metadata plan, 2026-08-18):
  // team, purpose tags, and the earned capability line, all editable by any
  // signed-in user through one modal off the Overview composition card.
  // Self-contained the same way DangerZone/SlackPanel are — this component
  // owns its own trigger button, its own Modal, and its own mutations;
  // AgentDetail just hands it the agent's current values plus `frozen`.
  //
  // Team/purpose tags/capability are seeded into local draft state the
  // moment the modal OPENS (`openEditor`), not kept in sync with the props
  // continuously — same "read once, don't clobber an in-progress edit"
  // discipline SlackPanel's channel rows document. A successful Save
  // re-seeds the draft from the server's own echoed-back values (the one
  // moment the draft *should* move), same as SlackPanel's channels save.
  //
  // The team-options query (`teamOptionsQuery`, `/api/teams`) is fetched
  // lazily, gated on this component's own `open` state via a writable/
  // derived pair — same "only fetch once actually needed" idiom
  // `slackManifestQuery` uses for the Slack manifest reveal, and for the
  // same version-quirk reason (the installed @tanstack/svelte-query adapter
  // only re-derives a query's options from an actual Svelte store, not a
  // plain object recomputed on every render — see MemoryPanel/SlackPanel's
  // own doc comments). Most agent-detail visits never open this modal, so
  // there's no reason to warm it on every AgentDetail mount; this modal is
  // now the query's only consumer (the create form stopped asking teams).
  //
  // The delivery chip is the exact same writable/derived-sha pattern
  // MemoryPanel/SlackPanel use, shared by both Save and Regenerate — each
  // just calls `beginDelivery(result.sha)` when it resolves. Modal idiom
  // wave (2026-09-23): it rides in the shell's pinned footer, on the left
  // of Cancel and Save, so the answer to "did that land?" is beside the
  // button that asked. Regenerate stays with the field it rewrites, and
  // stays quiet -- Save is the one filled button here.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { derived, writable } from "svelte/store";
  import Modal from "./Modal.svelte";
  import { errorMessage } from "../lib/api";
  import {
    deliveryQuery,
    patchMetadataMutation,
    regenerateMetadataMutation,
    teamOptionsQuery,
  } from "../lib/queries";

  let {
    agentName,
    team,
    purposeTags,
    capability,
    frozen = false,
    showTrigger = true,
  }: {
    agentName: string;
    team: string;
    purposeTags: string[];
    capability: string | null;
    frozen?: boolean;
    /** Whether to draw this component's own "Edit details" button.
     * AgentDetail turns it off (page-shell wave, 2026-09-23): the trigger
     * there is an item in the header's overflow menu, and it calls
     * `openEditor` below through a `bind:this`. The modal cannot live
     * inside that menu -- closing the menu would take the modal with it --
     * so the trigger and the modal are in two places, and this is the
     * seam between them. Defaults to on, so every other caller (and this
     * component's own tests) is unchanged. */
    showTrigger?: boolean;
  } = $props();

  const queryClient = useQueryClient();
  // agentName is read once here rather than tracked reactively: AgentDetail
  // (the only caller) remounts this component via `{#key route.name}`
  // whenever the routed agent changes, same reasoning as every other
  // panel's own mutation setup.
  // svelte-ignore state_referenced_locally
  const patchMutation = createMutation(() => (patchMetadataMutation(queryClient, agentName)));
  // svelte-ignore state_referenced_locally
  const regenerateMutation = createMutation(() => (regenerateMetadataMutation(queryClient, agentName)));

  let open = $state(false);

  // --- Team-options query, gated on `open` (lazy) --------------------------

  let teamOptionsOpen = $state(false);
  const teamOptions = createQuery(() => (({
    ...teamOptionsQuery(),
    enabled: teamOptionsOpen,
  })));

  // Always includes the agent's CURRENT team, even before the query
  // resolves (or if that team isn't among the current skill-owner pool —
  // an already-recorded custom team must still show up selected, same
  // reasoning the create station's picker documents for its own escape).
  const teamPickerOptions = $derived.by((): string[] => {
    const data = teamOptions.data;
    const pool = new Set<string>(data ? [data.default, ...data.teams] : ["shared"]);
    pool.add(team);
    return [...pool];
  });

  // --- Draft state (seeded on open) -----------------------------------------

  const OTHER = "__other__";
  let teamMode = $state<"select" | "other">("select");
  let teamValue = $state("shared");
  let teamOther = $state("");

  let tagsDraft = $state<string[]>([]);
  let tagInput = $state("");
  const TAGS_CAP = 8;

  const CAPABILITY_MAX = 160;
  let capabilityDraft = $state("");

  let saveError = $state<string | null>(null);
  let regenerateError = $state<string | null>(null);

  /** Opens the editor. Exported so a caller that draws its own trigger
   * (AgentDetail's overflow menu) can reach it through `bind:this`. */
  export function openEditor(): void {
    if (frozen) {
      return;
    }
    teamOptionsOpen = true;
    teamMode = "select";
    teamValue = team;
    teamOther = "";
    tagsDraft = [...purposeTags];
    tagInput = "";
    capabilityDraft = capability ?? "";
    saveError = null;
    regenerateError = null;
    open = true;
  }

  function closeEditor(): void {
    open = false;
  }

  function handleTeamSelectChange(value: string): void {
    if (value === OTHER) {
      teamMode = "other";
      teamOther = "";
    } else {
      teamMode = "select";
      teamValue = value;
    }
  }

  const effectiveTeam = $derived(
    teamMode === "other" ? teamOther.trim().toLowerCase() : teamValue,
  );

  function addTag(): void {
    const value = tagInput.trim().toLowerCase();
    if (value.length === 0) {
      return;
    }
    if (tagsDraft.includes(value) || tagsDraft.length >= TAGS_CAP) {
      tagInput = "";
      return;
    }
    tagsDraft = [...tagsDraft, value];
    tagInput = "";
  }

  function removeTag(value: string): void {
    tagsDraft = tagsDraft.filter((t) => t !== value);
  }

  const capabilityCount = $derived(capabilityDraft.length);
  const capabilityOverCap = $derived(capabilityCount > CAPABILITY_MAX);

  const canSave = $derived(
    !frozen &&
      !patchMutation.isPending &&
      effectiveTeam.length > 0 &&
      !capabilityOverCap,
  );

  // --- Delivery chip (copied pattern from MemoryPanel/SlackPanel) ----------

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
    return { tone: "pending", text: "Delivering changes…" };
  });

  // --- Save / Regenerate -----------------------------------------------------

  function handleSave(): void {
    if (!canSave) {
      return;
    }
    saveError = null;
    const body: { team?: string; purpose_tags?: string[]; capability?: string } = {
      team: effectiveTeam,
      purpose_tags: tagsDraft,
    };
    // The server has no way to distinguish "field omitted" from "field
    // explicitly cleared" (both arrive as `None` server-side) — an empty
    // capability is never sent, so it's treated as "leave unchanged" rather
    // than a rejected empty string.
    const trimmedCapability = capabilityDraft.trim();
    if (trimmedCapability.length > 0) {
      body.capability = trimmedCapability;
    }
    patchMutation.mutate(body, {
      onSuccess: (result) => {
        teamMode = "select";
        teamValue = result.team;
        tagsDraft = [...result.purpose_tags];
        capabilityDraft = result.capability ?? "";
        beginDelivery(result.sha);
      },
      onError: (err: unknown) => {
        saveError = errorMessage(err, "Couldn't save. Try again.");
      },
    });
  }

  function handleRegenerate(): void {
    if (frozen || regenerateMutation.isPending) {
      return;
    }
    regenerateError = null;
    regenerateMutation.mutate(undefined, {
      onSuccess: (result) => {
        capabilityDraft = result.capability ?? "";
        beginDelivery(result.sha);
      },
      onError: (err: unknown) => {
        regenerateError = errorMessage(err, "Couldn't regenerate this. Try again.");
      },
    });
  }
</script>


{#if showTrigger}
  <button
    type="button"
    class="btn metadata-details__trigger"
    disabled={frozen}
    onclick={openEditor}
  >
    Edit details
  </button>
{/if}

{#snippet detailsFooter()}
  {#if deliverySha !== null}
    <div
      class="metadata-form__delivery metadata-form__delivery--{deliveryView.tone}"
      role="status"
      aria-live="polite"
    >
      {#if deliveryView.tone === "pending"}
        <span class="metadata-form__delivery-dot pulse" aria-hidden="true"></span>
      {/if}
      <span>{deliveryView.text}</span>
    </div>
  {/if}
  <div class="modal__actions">
    <button
      type="button"
      class="btn btn--quiet"
      disabled={frozen}
      onclick={closeEditor}
    >
      Cancel
    </button>
    <button
      type="button"
      class="btn btn--primary"
      disabled={!canSave}
      onclick={handleSave}
    >
      {patchMutation.isPending ? "Saving…" : "Save"}
    </button>
  </div>
{/snippet}

<Modal {open} title="Edit details" onclose={closeEditor} footer={detailsFooter}>
  <form
    class="modal__form"
    onsubmit={(event) => {
      event.preventDefault();
      handleSave();
    }}
  >
    <div class="field">
      <label class="field__label" for="metadata-team">Persona team</label>
      <select
        id="metadata-team"
        class="field__input"
        value={teamMode === "other" ? OTHER : teamValue}
        disabled={frozen}
        onchange={(event) =>
          handleTeamSelectChange((event.currentTarget as HTMLSelectElement).value)}
      >
        {#each teamPickerOptions as t (t)}
          <option value={t}>{t}</option>
        {/each}
        <option value={OTHER}>Other...</option>
      </select>
      <p class="field__help">
        Which team's personas and skills it draws from. Not who can see it: that is its group.
      </p>
      {#if teamMode === "other"}
        <label class="field__sr" for="metadata-team-other">Team name</label>
        <input
          id="metadata-team-other"
          class="field__input"
          type="text"
          bind:value={teamOther}
          placeholder="team-name"
          autocomplete="off"
          spellcheck="false"
          disabled={frozen}
        />
        <p class="field__help">lowercase-with-dashes</p>
      {/if}
    </div>

    <div class="field">
      <label class="field__label" for="metadata-tag-input">Purpose tags</label>
      {#if tagsDraft.length > 0}
        <div class="chip-row">
          {#each tagsDraft as tag (tag)}
            <span class="chip">
              {tag}
              <button
                type="button"
                class="chip__remove"
                aria-label={`Remove ${tag}`}
                disabled={frozen}
                onclick={() => removeTag(tag)}
              >
                &times;
              </button>
            </span>
          {/each}
        </div>
      {/if}
      <div class="field__row">
        <input
          id="metadata-tag-input"
          class="field__input"
          type="text"
          bind:value={tagInput}
          placeholder="Add a tag"
          autocomplete="off"
          disabled={frozen}
          onkeydown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              addTag();
            }
          }}
        />
        <button
          type="button"
          class="btn"
          disabled={frozen || tagInput.trim().length === 0 || tagsDraft.length >= TAGS_CAP}
          onclick={addTag}
        >
          Add
        </button>
      </div>
      <p class="field__help">Up to 8 tags.</p>
    </div>

    <div class="field">
      <label class="field__label" for="metadata-capability">Capability</label>
      <textarea
        id="metadata-capability"
        class="field__input"
        bind:value={capabilityDraft}
        rows="3"
        disabled={frozen}
      ></textarea>
      <div class="metadata-form__capability-row">
        <p class="field__help" class:field__help--error={capabilityOverCap}>
          {capabilityCount}/{CAPABILITY_MAX}
        </p>
        <button
          type="button"
          class="btn"
          disabled={frozen || regenerateMutation.isPending}
          onclick={handleRegenerate}
        >
          {regenerateMutation.isPending ? "Regenerating…" : "Regenerate"}
        </button>
      </div>
      {#if regenerateError}
        <p class="field__status field__status--error" role="alert">
          {regenerateError}
        </p>
      {/if}
    </div>

    {#if saveError}
      <p class="field__status field__status--error" role="alert">{saveError}</p>
    {/if}
  </form>
</Modal>

<style>
  .metadata-details__trigger {
    align-self: flex-start;
  }

  .metadata-form__capability-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
  }

  /* The delivery chip, in the footer beside the buttons that cause it. */
  .metadata-form__delivery {
    display: inline-flex;
    align-items: center;
    gap: var(--space-xs);
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .metadata-form__delivery--ok {
    border-color: var(--ok);
    color: var(--ok);
  }

  .metadata-form__delivery--fail {
    border-color: var(--fail);
    color: var(--fail);
  }

  .metadata-form__delivery-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--pulse);
  }
</style>
