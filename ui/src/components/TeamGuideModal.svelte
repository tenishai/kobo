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

  function handleSubmit(event: SubmitEvent): void {
    event.preventDefault();
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

<Modal {open} title="Deploy your team's guide" {onclose}>
  <form class="guide-form" onsubmit={handleSubmit}>
    <div class="guide-form__field">
      <label class="guide-form__label" for="guide-group">Group</label>
      {#if groups.isPending}
        <Skeleton rows={1} />
      {:else if groups.isError}
        <ErrorState
          message={errorMessage(groups.error, "Couldn't load groups.")}
          onretry={() => groups.refetch()}
        />
      {:else if !groupOptionsAvailable}
        <p class="guide-form__help">No groups are configured yet.</p>
      {:else}
        <select
          id="guide-group"
          class="guide-form__input"
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
        <p class="guide-form__help">Which team this guide speaks for.</p>
      {/if}
    </div>

    {#if existingGuide}
      <div class="guide-existing">
        <p class="guide-form__status">{groupValue} already has a team guide.</p>
        <a
          class="guide-existing__link"
          href={formatHash({ page: "agent", name: existingGuide.name })}
        >
          Open {existingGuide.name}
        </a>
      </div>
    {:else if groupChosen}
      <div class="guide-form__field">
        <label class="guide-form__label" for="guide-name">Name</label>
        <input
          id="guide-name"
          class="guide-form__input"
          type="text"
          bind:value={name}
          placeholder="e.g. sre-guide"
          autocomplete="off"
          spellcheck="false"
          aria-invalid={nameError !== null}
        />
        <p class="guide-form__help">
          Lowercase letters, numbers, and hyphens. 2-24 characters.
        </p>
        {#if nameError}
          <p class="guide-form__status guide-form__status--error" role="alert">
            {nameError}
          </p>
        {/if}
      </div>

      <div class="guide-form__field">
        <label class="guide-form__label" for="guide-voice">Voice (optional)</label>
        <input
          id="guide-voice"
          class="guide-form__input"
          type="text"
          bind:value={voice}
          maxlength="120"
          autocomplete="off"
        />
        <p class="guide-form__help">
          name a famous person, artist, or regional character your guide
          should sound like (optional)
        </p>
      </div>

      {#if submitError}
        <p class="guide-form__status guide-form__status--error" role="alert">
          {submitError}
          {#if existingFromError}
            <a href={formatHash({ page: "agent", name: existingFromError })}>
              Open {existingFromError}
            </a>
          {/if}
        </p>
      {/if}

      <div class="guide-form__actions">
        <button type="button" class="guide-form__cancel" onclick={onclose}>
          Cancel
        </button>
        <button type="submit" class="guide-form__submit" disabled={!canSubmit}>
          {mutation.isPending ? "Deploying…" : "Deploy guide"}
        </button>
      </div>
    {:else}
      <p class="guide-form__hint">Pick a group first.</p>
    {/if}
  </form>
</Modal>

<style>
  /* Same visual language as CreateModal's own Create station -- a sibling,
     not a new design system -- just without that station's woodstamp
     numbering (there's only one station here). */
  .guide-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
    background-color: var(--bg);
    background-repeat: no-repeat, no-repeat, repeat;
    background-image:
      radial-gradient(
        140% 100% at 8% 0%,
        color-mix(in srgb, var(--accent) 6%, transparent) 0%,
        transparent 55%
      ),
      radial-gradient(
        120% 120% at 100% 100%,
        color-mix(in srgb, var(--accent) 4%, transparent) 0%,
        transparent 60%
      ),
      url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.05'/%3E%3C/svg%3E");
  }

  .guide-form__field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .guide-form__label {
    padding: 0;
    color: var(--ink);
    font-weight: 600;
    font-size: 0.9rem;
  }

  .guide-form__input {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .guide-form__input:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .guide-form__help {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .guide-form__hint {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .guide-form__status {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .guide-form__status--error {
    color: var(--fail);
  }

  .guide-form__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-sm);
  }

  .guide-form__cancel {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    cursor: pointer;
  }

  .guide-form__cancel:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .guide-form__submit {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .guide-form__submit:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .guide-form__submit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .guide-existing {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
  }

  .guide-existing__link {
    align-self: flex-start;
    padding: var(--space-2xs) var(--space-sm);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    text-decoration: none;
  }

  .guide-existing__link:hover {
    filter: brightness(1.05);
  }
</style>
