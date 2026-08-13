<script lang="ts">
  // The one-click moment: name it, pick a team/persona/skills/model, submit.
  // Fully controlled by the caller (`open` + `onclose`), same contract as
  // Modal itself — this component owns the form's own state (name, team,
  // selections, the availability check, submit errors) but not whether
  // it's visible.
  //
  // Name availability is the one field with a network round trip attached:
  // every keystroke gets instant client-side validation (validate.ts) and
  // only once that passes does a debounced (350ms) `checkAvailability` call
  // go out, so a name that's obviously wrong never costs a request.
  //
  // Persona and skills are catalog *searches* now (Task 10), not a whole
  // catalog fetched once: `personasSearchQuery`/`skillsSearchQuery` re-run
  // as the user types, each debounced 250ms via the same `debounced` helper
  // the name field uses. Because @tanstack/svelte-query's installed adapter
  // (5.90.2) only re-derives a query's options from an actual Svelte store
  // (see MemoryPanel/SlackPanel's delivery-chip doc comments for the same
  // constraint), the debounced search term for each section lives in a
  // plain `writable` store, fed by an `$effect` off the `$state` the input
  // is bound to — not a `$derived` object, which the adapter would freeze
  // at first render and never look at again.
  //
  // Models and teams come from `modelsQuery()`/`teamsQuery()`, both
  // prefetched by App.svelte the same "warm modal" way the old whole-catalog
  // fetch was. Org wiring status (`orgStatusQuery()`) decides whether the
  // persona/skills sections show real pickers or an honest "not set up yet"
  // empty state — see `personasUnwired`/`skillsUnwired` below.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { derived, writable } from "svelte/store";
  import EmptyState from "./EmptyState.svelte";
  import ErrorState from "./ErrorState.svelte";
  import Modal from "./Modal.svelte";
  import PersonaBuilder from "./PersonaBuilder.svelte";
  import Skeleton from "./Skeleton.svelte";
  import { ApiError, checkAvailability, errorMessage, type CatalogEntry } from "../lib/api";
  import { debounced } from "../lib/debounce";
  import {
    createAgentMutation,
    modelsQuery,
    orgStatusQuery,
    personasSearchQuery,
    skillsSearchQuery,
    teamsQuery,
  } from "../lib/queries";
  import { toast } from "../lib/toast.svelte";
  import { validateName, validateSlug } from "../lib/validate";

  let { open, onclose }: { open: boolean; onclose: () => void } = $props();

  const queryClient = useQueryClient();
  const models = createQuery(modelsQuery());
  const teams = createQuery(teamsQuery());
  const orgStatus = createQuery(orgStatusQuery());
  const mutation = createMutation(createAgentMutation(queryClient));

  let name = $state("");
  let team = $state("");
  // `null` means "no explicit choice yet" — the effective value falls back
  // to the catalog's default model, below (persona has no such fallback
  // any more: a search-driven list has no stable "first" item to default
  // to, so a persona must be explicitly picked).
  let personaOverride = $state<string | null>(null);
  let modelOverride = $state<string | null>(null);
  let submitError = $state<string | null>(null);

  const selectedPersona = $derived(personaOverride ?? "");
  const selectedModel = $derived(modelOverride ?? $models.data?.default ?? "");

  // --- Name: client-side check, only rendered once there's something to
  // say about it (an empty field isn't "invalid", it's just empty) --------

  const nameError = $derived(name.length > 0 ? validateName(name) : null);
  const nameLocallyValid = $derived(name.length > 0 && nameError === null);

  type Availability =
    | { status: "idle" }
    | { status: "checking" }
    | { status: "available" }
    | { status: "unavailable"; reason: string };

  let availability = $state<Availability>({ status: "idle" });

  const checkName = debounced((value: string) => {
    checkAvailability(value)
      .then((result) => {
        if (value !== name) {
          return; // stale response — the field has moved on since
        }
        availability = result.available
          ? { status: "available" }
          : { status: "unavailable", reason: result.reason ?? "Not available." };
      })
      .catch(() => {
        if (value !== name) {
          return;
        }
        availability = {
          status: "unavailable",
          reason: "Couldn't check availability. Try again.",
        };
      });
  }, 350);

  // Drives the availability check off `name`, gated by local validity and
  // by the modal being open. The cleanup cancels the in-flight debounce
  // whenever this reruns — on every keystroke (harmless: `debounced`
  // already collapses those) and, more importantly, when the modal closes,
  // so a check started just before close can never resolve into a late
  // "available" flash on a field the user has already walked away from.
  $effect(() => {
    const value = name;
    if (!open || !nameLocallyValid) {
      availability = { status: "idle" };
      return;
    }
    availability = { status: "checking" };
    checkName(value);
    return () => {
      checkName.cancel();
    };
  });

  // --- Team: same slug-hint idiom as name, picked from a datalist or
  // typed fresh (honor-system until identity lands) -----------------------

  const teamError = $derived(team.length > 0 ? validateSlug(team) : null);
  const teamValid = $derived(team.length > 0 && teamError === null);

  // --- Persona search / builder -------------------------------------------

  let personaQ = $state("");
  const personaSearchTerm = writable("");
  const personaSearchOptions = derived(personaSearchTerm, ($q) =>
    personasSearchQuery($q, null),
  );
  const personas = createQuery(personaSearchOptions);

  const commitPersonaSearch = debounced((value: string) => {
    personaSearchTerm.set(value);
  }, 250);

  $effect(() => {
    const value = personaQ;
    if (!open) {
      return;
    }
    commitPersonaSearch(value);
    return () => {
      commitPersonaSearch.cancel();
    };
  });

  let builderOpen = $state(false);

  function handlePersonaCreated(id: string): void {
    personaOverride = id;
    builderOpen = false;
    personaQ = "";
    personaSearchTerm.set("");
    toast.success("Persona ready");
  }

  // --- Skills search -------------------------------------------------------

  let skillQ = $state("");
  // "" means "All teams" — a plain string (not `string | null`) because a
  // `<select>`'s DOM value is always a string; converted to `null` only at
  // the point the search params are committed, below.
  let skillTeamFilter = $state("");

  const skillSearchParams = writable<{ q: string; team: string | null }>({
    q: "",
    team: null,
  });
  const skillsSearchOptions = derived(skillSearchParams, ($params) =>
    skillsSearchQuery($params.q, $params.team),
  );
  const skills = createQuery(skillsSearchOptions);

  const commitSkillSearch = debounced(
    (value: { q: string; team: string | null }) => {
      skillSearchParams.set(value);
    },
    250,
  );

  $effect(() => {
    const value = { q: skillQ, team: skillTeamFilter === "" ? null : skillTeamFilter };
    if (!open) {
      return;
    }
    commitSkillSearch(value);
    return () => {
      commitSkillSearch.cancel();
    };
  });

  type SelectedSkill = { id: string; team: string; name: string };

  /** Keyed `team/id` (a skill id is only unique within its own team) so a
   * pick survives the visible results changing underneath it as the user
   * keeps searching. */
  let selectedSkills = $state<Map<string, SelectedSkill>>(new Map());

  function skillKey(id: string, skillTeam: string): string {
    return `${skillTeam}/${id}`;
  }

  function toggleSkill(entry: CatalogEntry): void {
    const key = skillKey(entry.id, entry.team);
    const next = new Map(selectedSkills);
    if (next.has(key)) {
      next.delete(key);
    } else {
      next.set(key, { id: entry.id, team: entry.team, name: entry.name });
    }
    selectedSkills = next;
  }

  function removeSkill(key: string): void {
    const next = new Map(selectedSkills);
    next.delete(key);
    selectedSkills = next;
  }

  // --- Unwired / empty catalogs --------------------------------------------

  function checkFailed(checkId: string): boolean {
    return $orgStatus.data?.checks.some((c) => c.id === checkId && !c.ok) ?? false;
  }

  // `undefined` (still loading, or the query hasn't resolved) never counts
  // as unwired on its own — that would flash the "not set up yet" empty
  // state on every open before org status has even loaded once. Only a
  // confirmed `wired === false`, or a confirmed failing check, does.
  const personasUnwired = $derived(
    $orgStatus.data !== undefined &&
      ($orgStatus.data.wired === false || checkFailed("personas-seeded")),
  );
  const skillsUnwired = $derived(
    $orgStatus.data !== undefined &&
      ($orgStatus.data.wired === false || checkFailed("skills-index")),
  );

  const UNWIRED_MESSAGE = "Catalogs aren't set up yet — ask whoever runs your room.";

  // Fresh form every time the modal opens.
  $effect(() => {
    if (open) {
      name = "";
      team = "";
      personaOverride = null;
      modelOverride = null;
      submitError = null;
      personaQ = "";
      personaSearchTerm.set("");
      builderOpen = false;
      skillQ = "";
      skillTeamFilter = "";
      skillSearchParams.set({ q: "", team: null });
      selectedSkills = new Map();
    }
  });

  const canSubmit = $derived(
    $models.data !== undefined &&
      nameLocallyValid &&
      availability.status === "available" &&
      teamValid &&
      !personasUnwired &&
      !skillsUnwired &&
      selectedPersona !== "" &&
      selectedModel !== "" &&
      !$mutation.isPending,
  );

  function handleSubmit(event: SubmitEvent): void {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }
    submitError = null;
    const createdName = name;
    $mutation.mutate(
      {
        name: createdName,
        persona: selectedPersona,
        team: team.trim(),
        skills: [...selectedSkills.values()].map(({ id, team: skillTeam }) => ({
          id,
          team: skillTeam,
        })),
        model: selectedModel,
      },
      {
        onSuccess: () => {
          toast.success("Agent created", `Assembling ${createdName}…`);
          onclose();
        },
        onError: (error: unknown) => {
          submitError = errorMessage(error, "Something went wrong. Try again.");
          if (error instanceof ApiError && error.status === 409) {
            // The debounced availability check said this name was free, but
            // the submit lost the race — someone else took it between the
            // check and now. Flip availability to match reality so the ✓
            // next to the field can't keep claiming "available" right above
            // an error that says otherwise.
            availability = { status: "unavailable", reason: "Name already taken." };
          }
        },
      },
    );
  }
</script>

<Modal {open} title="Create agent" {onclose}>
  <form class="create-form" onsubmit={handleSubmit}>
    <div class="create-form__field">
      <label class="create-form__label" for="create-name">Name</label>
      <input
        id="create-name"
        class="create-form__input"
        type="text"
        bind:value={name}
        placeholder="e.g. weekly-digest"
        autocomplete="off"
        spellcheck="false"
        aria-describedby="create-name-status"
        aria-invalid={nameError !== null ||
          availability.status === "unavailable"}
      />
      <p class="create-form__help">
        Lowercase letters, numbers, and hyphens. 2-24 characters.
      </p>
      <div
        id="create-name-status"
        role={nameError || availability.status === "unavailable"
          ? "alert"
          : availability.status === "checking" || availability.status === "available"
            ? "status"
            : undefined}
        aria-live={availability.status === "checking" ||
        availability.status === "available"
          ? "polite"
          : undefined}
      >
        {#if nameError}
          <p class="create-form__status create-form__status--error">{nameError}</p>
        {:else if availability.status === "checking"}
          <p class="create-form__status">Checking…</p>
        {:else if availability.status === "available"}
          <p class="create-form__status create-form__status--ok">✓ Available</p>
        {:else if availability.status === "unavailable"}
          <p class="create-form__status create-form__status--error">
            {availability.reason}
          </p>
        {/if}
      </div>
    </div>

    <div class="create-form__field">
      <label class="create-form__label" for="create-team">Team</label>
      <input
        id="create-team"
        class="create-form__input"
        type="text"
        list="create-team-options"
        bind:value={team}
        placeholder="e.g. growth"
        autocomplete="off"
        spellcheck="false"
        aria-describedby="create-team-status"
        aria-invalid={teamError !== null}
      />
      <datalist id="create-team-options">
        {#if $teams.data}
          {#each $teams.data as t (t)}
            <option value={t}></option>
          {/each}
        {/if}
      </datalist>
      <p class="create-form__help">
        Lowercase letters, numbers, and hyphens. 1-24 characters.
      </p>
      <div id="create-team-status">
        {#if teamError}
          <p class="create-form__status create-form__status--error" role="alert">
            {teamError}
          </p>
        {/if}
      </div>
    </div>

    <fieldset class="create-form__field create-form__fieldset">
      <legend class="create-form__label">Persona</legend>
      {#if personasUnwired}
        <EmptyState message={UNWIRED_MESSAGE} />
      {:else if builderOpen}
        <PersonaBuilder
          {team}
          {teamValid}
          oncancel={() => (builderOpen = false)}
          oncreated={handlePersonaCreated}
        />
      {:else}
        <button
          type="button"
          class="persona-build-cta"
          onclick={() => (builderOpen = true)}
        >
          Build your own persona
        </button>

        <div class="create-form__field">
          <label class="create-form__label" for="persona-search">Search personas</label>
          <input
            id="persona-search"
            class="create-form__input"
            type="text"
            bind:value={personaQ}
            placeholder="Search personas"
            autocomplete="off"
          />
        </div>

        {#if $personas.isPending}
          <Skeleton rows={2} />
        {:else if $personas.isError}
          <ErrorState
            message={errorMessage($personas.error, "Couldn't load personas.")}
            onretry={() => $personas.refetch()}
          />
        {:else}
          <div class="persona-list">
            {#if $personas.data.length === 0 && personaQ.length > 0}
              <p class="create-form__help">Nothing matches your search.</p>
            {/if}
            {#each $personas.data as persona (persona.id)}
              <label
                class="persona-card"
                class:persona-card--selected={persona.id === selectedPersona}
              >
                <input
                  type="radio"
                  name="persona"
                  value={persona.id}
                  checked={persona.id === selectedPersona}
                  onchange={() => (personaOverride = persona.id)}
                />
                <span class="persona-card__body">
                  <span class="persona-card__head">
                    <span class="persona-card__name">{persona.name}</span>
                    <span
                      class="catalog-badge"
                      class:catalog-badge--builtin={persona.builtin}
                    >
                      {persona.builtin ? "Built-in" : persona.team}
                    </span>
                  </span>
                  <span class="persona-card__description">{persona.description}</span>
                </span>
              </label>
            {/each}
          </div>
        {/if}
      {/if}
    </fieldset>

    <fieldset class="create-form__field create-form__fieldset">
      <legend class="create-form__label">Skills</legend>
      {#if skillsUnwired}
        <EmptyState message={UNWIRED_MESSAGE} />
      {:else}
        {#if selectedSkills.size > 0}
          <div class="chip-row">
            {#each [...selectedSkills.entries()] as [key, skill] (key)}
              <span class="chip">
                {skill.name}
                <button
                  type="button"
                  class="chip__remove"
                  aria-label={`Remove ${skill.name}`}
                  onclick={() => removeSkill(key)}
                >
                  &times;
                </button>
              </span>
            {/each}
          </div>
        {/if}

        <div class="create-form__field">
          <label class="create-form__label" for="skill-search">Search skills</label>
          <div class="catalog-search__row">
            <input
              id="skill-search"
              class="create-form__input"
              type="text"
              bind:value={skillQ}
              placeholder="Search skills"
              autocomplete="off"
            />
            <label class="sr-only" for="skill-team-filter">Filter skills by team</label>
            <select
              id="skill-team-filter"
              class="create-form__input"
              value={skillTeamFilter}
              onchange={(event) =>
                (skillTeamFilter = (event.currentTarget as HTMLSelectElement).value)}
            >
              <option value="">All teams</option>
              {#if $teams.data}
                {#each $teams.data as t (t)}
                  <option value={t}>{t}</option>
                {/each}
              {/if}
            </select>
          </div>
        </div>

        {#if $skills.isPending}
          <Skeleton rows={2} />
        {:else if $skills.isError}
          <ErrorState
            message={errorMessage($skills.error, "Couldn't load skills.")}
            onretry={() => $skills.refetch()}
          />
        {:else if $skills.data.length === 0}
          <p class="create-form__help">
            {skillQ.length > 0 ? "Nothing matches your search." : "No skills in the catalog yet"}
          </p>
        {:else}
          <div class="skill-list">
            {#each $skills.data as skill (skillKey(skill.id, skill.team))}
              <label class="skill-item">
                <input
                  type="checkbox"
                  checked={selectedSkills.has(skillKey(skill.id, skill.team))}
                  onchange={() => toggleSkill(skill)}
                />
                <span class="skill-item__body">
                  <span class="skill-item__head">
                    <span class="skill-item__name">{skill.name}</span>
                    <span class="catalog-badge">{skill.team}</span>
                  </span>
                  <span class="skill-item__description">{skill.description}</span>
                </span>
              </label>
            {/each}
          </div>
        {/if}
      {/if}
    </fieldset>

    <div class="create-form__field">
      <label class="create-form__label" for="create-model">Model</label>
      {#if $models.isPending}
        <Skeleton rows={1} />
      {:else if $models.isError}
        <ErrorState
          message={errorMessage($models.error, "Couldn't load models.")}
          onretry={() => $models.refetch()}
        />
      {:else}
        <select
          id="create-model"
          class="create-form__input"
          value={selectedModel}
          onchange={(event) =>
            (modelOverride = (event.currentTarget as HTMLSelectElement).value)}
        >
          {#each $models.data.models as model (model.id)}
            <option value={model.id}>{model.label}</option>
          {/each}
        </select>
      {/if}
    </div>

    {#if submitError}
      <p class="create-form__status create-form__status--error" role="alert">
        {submitError}
      </p>
    {/if}

    <div class="create-form__actions">
      <button type="button" class="create-form__cancel" onclick={onclose}>
        Cancel
      </button>
      <button type="submit" class="create-form__submit" disabled={!canSubmit}>
        {$mutation.isPending ? "Creating…" : "Create agent"}
      </button>
    </div>
  </form>
</Modal>

<style>
  .create-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
  }

  .create-form__field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .create-form__fieldset {
    margin: 0;
    padding: 0;
    border: none;
  }

  .create-form__label {
    padding: 0;
    color: var(--ink);
    font-weight: 600;
    font-size: 0.9rem;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  .create-form__input {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .create-form__input:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .create-form__help {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .create-form__status {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  /* Fixed floor so the idle state (no message rendered at all) reserves the
     same space a one-line status/error message would take — otherwise the
     field sits flush against the help text (a stray gap sliver) and then
     jumps down the moment a message appears. */
  #create-name-status,
  #create-team-status {
    min-height: 1.25rem;
  }

  .create-form__status--ok {
    color: var(--ok);
  }

  .create-form__status--error {
    color: var(--fail);
  }

  .catalog-search__row {
    display: flex;
    gap: var(--space-xs);
  }

  .catalog-search__row .create-form__input:first-child {
    flex: 1;
  }

  .chip-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2xs);
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2xs);
    padding: 0.2rem var(--space-xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--ink);
    font-size: 0.8rem;
  }

  .chip__remove {
    padding: 0;
    border: none;
    background: none;
    color: var(--muted);
    font-size: 0.9rem;
    line-height: 1;
    cursor: pointer;
  }

  .chip__remove:hover {
    color: var(--fail);
  }

  .catalog-badge {
    display: inline-flex;
    padding: 0.1rem var(--space-2xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.7rem;
    white-space: nowrap;
  }

  .catalog-badge--builtin {
    border-color: var(--accent);
    color: var(--accent);
  }

  .persona-list,
  .skill-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .persona-card,
  .skill-item {
    display: flex;
    align-items: flex-start;
    gap: var(--space-xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  /* Persona builder v2 (addendum): "Build your own persona" is a primary-
     styled action above the search results, not a text afterthought below
     them — same accent treatment as create-form__submit, but full-width
     since it now stands alone above the list rather than sitting inline
     next to a cancel button. */
  .persona-build-cta {
    width: 100%;
    padding: var(--space-sm) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .persona-build-cta:hover {
    filter: brightness(1.05);
  }

  .persona-card:hover,
  .skill-item:hover {
    border-color: var(--accent);
  }

  .persona-card--selected {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 8%, var(--panel));
  }

  .persona-card input,
  .skill-item input {
    margin-top: 0.2rem;
  }

  .persona-card__body,
  .skill-item__body {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: 0.15rem;
  }

  .persona-card__head,
  .skill-item__head {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
  }

  .persona-card__name,
  .skill-item__name {
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.85rem;
    font-weight: 600;
  }

  .persona-card__description,
  .skill-item__description {
    color: var(--muted);
    font-size: 0.8rem;
  }

  .create-form__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-sm);
  }

  .create-form__cancel {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    cursor: pointer;
  }

  .create-form__cancel:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .create-form__submit {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .create-form__submit:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .create-form__submit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
