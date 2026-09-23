<script lang="ts">
  // "Deploy your team's guide" (team-guide spec 2026-09-14): a one-station
  // sibling of CreateModal's own Create station, for the one archetype that
  // exists today (`archetype: "team_guide"`). Same controlled-visibility
  // contract as every other modal in this room (`open` + `onclose`, owned
  // by the caller) and the same field styling, just one station instead of
  // three -- there's no persona/naming ceremony here, the server renders
  // the guide's persona itself from the picked group + optional voice.
  //
  // Group reuses the exact same source CreateModal's own Create station
  // reads (`groupsQuery`, sectioned shared/non-engineering/engineering,
  // never a hardcoded list) -- this modal just doesn't carry CreateModal's
  // "my group isn't listed" escape hatch, since deploying a guide is
  // secondary to actually creating an agent in a not-yet-configured group.
  //
  // The existing-guide pre-check reads `agentsQuery` -- the SAME cache key
  // Dashboard/AgentCard/Sidebar already poll, so this never issues a
  // fetch of its own on a warm cache. `GET /api/agents` had never carried
  // `archetype`/`purpose_tags` before this task (only the per-agent detail
  // route did) -- both were added to the list route alongside this file so
  // the pre-check has something to read at all. A guide's own `group_name`
  // is always the commons (the server forces it there), so matching by
  // group_name -- the naive read of "does this team have a guide" -- would
  // never work; the real identity lives in the tag pair
  // (`TEAM_GUIDE_TAG` + the team's own slug), which is what `existingGuide`
  // below actually matches on. `normalizeGroupTag` (validate.ts) mirrors
  // the server's own fold so a picked group like "Data Eng" checks against
  // the same "data-eng" slug the server actually stored.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import Modal from "./Modal.svelte";
  import Skeleton from "./Skeleton.svelte";
  import ErrorState from "./ErrorState.svelte";
  import { ApiError, errorMessage } from "../lib/api";
  import { agentsQuery, createAgentMutation, groupsQuery } from "../lib/queries";
  import { formatHash } from "../lib/router.svelte";
  import { toast } from "../lib/toast.svelte";
  import { normalizeGroupTag, validateName } from "../lib/validate";

  let { open, onclose }: { open: boolean; onclose: () => void } = $props();

  // Mirrors kobo_api/archetypes.py's own constants verbatim -- the server
  // is authoritative for what these strings mean, this just has to agree
  // with it to recognize a guide it didn't just create.
  const ARCHETYPE_TEAM_GUIDE = "team_guide";
  const TEAM_GUIDE_TAG = "team-guide";

  const queryClient = useQueryClient();
  const groups = createQuery(() => (groupsQuery()));
  // Same `["agents"]` cache Dashboard/Sidebar already keep warm/polling --
  // this is a read of that shared entry, never a second poll of its own.
  const agents = createQuery(() => (agentsQuery()));
  const mutation = createMutation(() => (createAgentMutation(queryClient)));

  let groupValue = $state("");
  let name = $state("");
  let voice = $state("");
  let submitError = $state<string | null>(null);
  // Set only from a 409's own body (a race the client-side pre-check below
  // missed -- someone else's guide landed between this modal opening and
  // submit). Independent of `existingGuide`: that one is a live, reactive
  // read of the agents cache; this is a one-shot snapshot of what the
  // server actually said, for the exact same "Open {name}" treatment.
  let existingFromError = $state<string | null>(null);

  const groupOptionsAvailable = $derived((groups.data?.length ?? 0) > 0);
  const sharedGroupOptions = $derived(
    (groups.data ?? []).filter((g) => g.kind === "shared"),
  );
  const engineeringGroupOptions = $derived(
    (groups.data ?? []).filter((g) => g.kind === "engineering"),
  );
  const nonEngineeringGroupOptions = $derived(
    (groups.data ?? []).filter((g) => g.kind === "non-engineering"),
  );
  const groupChosen = $derived(groupValue !== "");

  const nameError = $derived(name.length > 0 ? validateName(name) : null);
  const nameValid = $derived(name.length > 0 && nameError === null);

  /** The picked group, folded the same way the server folds any purpose
   * tag -- `""` until a group is actually chosen. */
  const groupSlug = $derived(groupChosen ? normalizeGroupTag(groupValue) : "");

  /** The picked group's own existing guide, if the agents list already
   * holds one -- `null` while nothing's picked yet, while the list hasn't
   * loaded, or when no row matches. Never filtered by `group_name` (every
   * guide's own is forced to the commons): the tag pair is the only thing
   * that actually identifies which team a guide serves. */
  const existingGuide = $derived(
    groupSlug.length > 0
      ? ((agents.data?.agents ?? []).find(
          (a) =>
            a.archetype === ARCHETYPE_TEAM_GUIDE &&
            a.purpose_tags.includes(TEAM_GUIDE_TAG) &&
            a.purpose_tags.includes(groupSlug),
        ) ?? null)
      : null,
  );

  const canSubmit = $derived(
    groupChosen &&
      nameValid &&
      existingGuide === null &&
      !mutation.isPending,
  );

  // Fresh form every time the modal opens -- same idiom as CreateModal's
  // own reset effect.
  $effect(() => {
    if (open) {
      groupValue = "";
      name = "";
      voice = "";
      submitError = null;
      existingFromError = null;
    }
  });

  /** Called by the form's own submit (Enter in a field) and by the footer's
   * Quick-start button, which sits outside the form element the modal shell
   * puts in its body. */
  function handleSubmit(): void {
    if (!canSubmit) {
      return;
    }
    submitError = null;
    existingFromError = null;
    const createdName = name;
    mutation.mutate(
      {
        name: createdName,
        // Required by the request model; ignored server-side on the
        // archetype path (the route renders the guide's own persona from
        // group + voice instead).
        persona: "helpful-generalist",
        skills: [],
        model: null,
        purpose_tags: [],
        group_name: groupValue,
        archetype: ARCHETYPE_TEAM_GUIDE,
        voice: voice.trim().length > 0 ? voice.trim() : undefined,
      },
      {
        onSuccess: () => {
          toast.success("Guide deployed", `Assembling ${createdName}…`);
          onclose();
        },
        onError: (error: unknown) => {
          if (error instanceof ApiError && error.status === 409) {
            const body = error.body as { existing?: unknown } | undefined;
            existingFromError =
              typeof body?.existing === "string" ? body.existing : null;
          }
          submitError = errorMessage(error, "Something went wrong. Try again.");
        },
      },
    );
  }
</script>


{#snippet guideFooter()}
  <div class="modal__actions">
    <button type="button" class="btn btn--quiet" onclick={onclose}>Cancel</button>
    <button
      type="button"
      class="btn btn--primary"
      disabled={!canSubmit}
      onclick={handleSubmit}
    >
      {mutation.isPending ? "Starting…" : "Quick-start agent"}
    </button>
  </div>
{/snippet}

<Modal {open} title="Quick-start team agent" {onclose} footer={guideFooter}>
  <form
    class="modal__form"
    onsubmit={(event) => {
      event.preventDefault();
      handleSubmit();
    }}
  >
    <div class="field">
      <label class="field__label" for="guide-group">Group</label>
      {#if groups.isPending}
        <Skeleton rows={1} />
      {:else if groups.isError}
        <ErrorState
          message={errorMessage(groups.error, "Couldn't load groups.")}
          onretry={() => groups.refetch()}
        />
      {:else if !groupOptionsAvailable}
        <p class="field__help">No groups are configured yet.</p>
      {:else}
        <select
          id="guide-group"
          class="field__input"
          required
          value={groupValue}
          onchange={(event) =>
            (groupValue = (event.currentTarget as HTMLSelectElement).value)}
        >
          <option value="" disabled>Choose a group</option>
          {#if sharedGroupOptions.length > 0}
            <optgroup label="Shared">
              {#each sharedGroupOptions as group (group.name)}
                <option value={group.name}>{group.name} (everyone in the company)</option>
              {/each}
            </optgroup>
          {/if}
          {#if nonEngineeringGroupOptions.length > 0}
            <optgroup label="Non-engineering groups">
              {#each nonEngineeringGroupOptions as group (group.name)}
                <option value={group.name}>{group.name}</option>
              {/each}
            </optgroup>
          {/if}
          {#if engineeringGroupOptions.length > 0}
            <optgroup label="Engineering groups">
              {#each engineeringGroupOptions as group (group.name)}
                <option value={group.name}>{group.name}</option>
              {/each}
            </optgroup>
          {/if}
        </select>
        <p class="field__help">Which team this guide speaks for.</p>
      {/if}
    </div>

    {#if existingGuide}
      <div class="guide-existing">
        <p class="field__status">{groupValue} already has a team guide.</p>
        <a
          class="btn guide-existing__link"
          href={formatHash({ page: "agent", name: existingGuide.name })}
        >
          Open {existingGuide.name}
        </a>
      </div>
    {:else if groupChosen}
      <div class="field">
        <label class="field__label" for="guide-name">Name</label>
        <input
          id="guide-name"
          class="field__input"
          type="text"
          bind:value={name}
          placeholder="e.g. sre-guide"
          autocomplete="off"
          spellcheck="false"
          aria-invalid={nameError !== null}
        />
        <p class="field__help">
          Lowercase letters, numbers, and hyphens. 2-24 characters.
        </p>
        {#if nameError}
          <p class="field__status field__status--error" role="alert">{nameError}</p>
        {/if}
      </div>

      <div class="field">
        <label class="field__label" for="guide-voice">Voice (optional)</label>
        <input
          id="guide-voice"
          class="field__input"
          type="text"
          bind:value={voice}
          maxlength="120"
          autocomplete="off"
        />
        <p class="field__help">
          name a famous person, artist, or regional character your guide
          should sound like (optional)
        </p>
      </div>

      {#if submitError}
        <p class="field__status field__status--error" role="alert">
          {submitError}
          {#if existingFromError}
            <a href={formatHash({ page: "agent", name: existingFromError })}>
              Open {existingFromError}
            </a>
          {/if}
        </p>
      {/if}
    {:else}
      <p class="modal__hint">Pick a group first.</p>
    {/if}
  </form>
</Modal>

<style>
  .guide-existing {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }

  /* A link wearing the shared button shape, which brings its own
     underline along. */
  .guide-existing__link {
    text-decoration: none;
  }
</style>
