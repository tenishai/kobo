<script lang="ts">
  // The one-click moment: name it, pick a persona/skills/model, submit.
  // Fully controlled by the caller (`open` + `onclose`), same contract as
  // Modal itself — this component owns the form's own state (name,
  // selections, the availability check, submit errors) but not whether it's
  // visible.
  //
  // Name availability is the one field with a network round trip attached:
  // every keystroke gets instant client-side validation (validate.ts) and
  // only once that passes does a debounced (350ms) `checkAvailability` call
  // go out, so a name that's obviously wrong never costs a request. The
  // Create button only lights up once both checks — and a persona and a
  // model — are in place.
  //
  // Persona/skills/model come from `catalogsQuery()`, which App.svelte
  // already prefetches on Create-button hover/focus — `createQuery` here
  // just consumes whatever's in cache (warm, most of the time) or fetches
  // it fresh if the modal was opened some other way.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import ErrorState from "./ErrorState.svelte";
  import Modal from "./Modal.svelte";
  import Skeleton from "./Skeleton.svelte";
  import { ApiError, checkAvailability } from "../lib/api";
  import { debounced } from "../lib/debounce";
  import { catalogsQuery, createAgentMutation } from "../lib/queries";
  import { toast } from "../lib/toast.svelte";
  import { validateName } from "../lib/validate";

  let { open, onclose }: { open: boolean; onclose: () => void } = $props();

  const queryClient = useQueryClient();
  const catalogs = createQuery(catalogsQuery());
  const mutation = createMutation(createAgentMutation(queryClient));

  let name = $state("");
  let selectedSkills = $state<Set<string>>(new Set());
  // `null` means "no explicit choice yet" — the effective value falls back
  // to the catalog's first persona / defaultModel, below.
  let personaOverride = $state<string | null>(null);
  let modelOverride = $state<string | null>(null);
  let submitError = $state<string | null>(null);

  const selectedPersona = $derived(
    personaOverride ?? $catalogs.data?.personas[0]?.id ?? "",
  );
  const selectedModel = $derived(
    modelOverride ?? $catalogs.data?.defaultModel ?? "",
  );

  // Client-side check, only rendered once there's something to say about it
  // (an empty field isn't "invalid", it's just empty).
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

  // Fresh form every time the modal opens.
  $effect(() => {
    if (open) {
      name = "";
      selectedSkills = new Set();
      personaOverride = null;
      modelOverride = null;
      submitError = null;
    }
  });

  const canSubmit = $derived(
    $catalogs.data !== undefined &&
      nameLocallyValid &&
      availability.status === "available" &&
      selectedPersona !== "" &&
      selectedModel !== "" &&
      !$mutation.isPending,
  );

  function toggleSkill(id: string): void {
    const next = new Set(selectedSkills);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    selectedSkills = next;
  }

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
        skills: Array.from(selectedSkills),
        model: selectedModel,
      },
      {
        onSuccess: () => {
          toast.success("Agent created", `Assembling ${createdName}…`);
          onclose();
        },
        onError: (error: unknown) => {
          submitError =
            error instanceof ApiError
              ? error.message
              : "Something went wrong. Try again.";
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
      />
      <p class="create-form__help">
        Lowercase letters, numbers, and hyphens. 2-30 characters.
      </p>
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

    {#if $catalogs.isPending}
      <Skeleton rows={4} />
    {:else if $catalogs.isError}
      <ErrorState
        message="Couldn't load personas, skills, or models."
        onretry={() => $catalogs.refetch()}
      />
    {:else if $catalogs.data}
      <fieldset class="create-form__field create-form__fieldset">
        <legend class="create-form__label">Persona</legend>
        <div class="persona-list">
          {#each $catalogs.data.personas as persona (persona.id)}
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
                <span class="persona-card__name">{persona.id}</span>
                <span class="persona-card__description">{persona.description}</span>
              </span>
            </label>
          {/each}
        </div>
      </fieldset>

      <fieldset class="create-form__field create-form__fieldset">
        <legend class="create-form__label">Skills</legend>
        {#if $catalogs.data.skills.length === 0}
          <p class="create-form__help">No skills in the catalog yet</p>
        {:else}
          <div class="skill-list">
            {#each $catalogs.data.skills as skill (skill.id)}
              <label class="skill-item">
                <input
                  type="checkbox"
                  checked={selectedSkills.has(skill.id)}
                  onchange={() => toggleSkill(skill.id)}
                />
                <span class="skill-item__body">
                  <span class="skill-item__name">{skill.id}</span>
                  <span class="skill-item__description">{skill.description}</span>
                </span>
              </label>
            {/each}
          </div>
        {/if}
      </fieldset>

      <div class="create-form__field">
        <label class="create-form__label" for="create-model">Model</label>
        <select
          id="create-model"
          class="create-form__input"
          value={selectedModel}
          onchange={(event) =>
            (modelOverride = (event.currentTarget as HTMLSelectElement).value)}
        >
          {#each $catalogs.data.models as model (model.id)}
            <option value={model.id}>{model.label}</option>
          {/each}
        </select>
      </div>
    {/if}

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

  .create-form__status--ok {
    color: var(--ok);
  }

  .create-form__status--error {
    color: var(--fail);
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
    flex-direction: column;
    gap: 0.15rem;
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
