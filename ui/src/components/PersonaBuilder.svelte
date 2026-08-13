<script lang="ts">
  // Inline "create a new persona" form: swapped in for CreateModal's persona
  // search list when the user picks its "Build your own" card. Persona
  // builder v2 (addendum): four fields — name, who they are, what they do,
  // and an optional rules list — down from the original seven. Tags/tone/
  // role/purpose/boundaries are gone: Rules absorbs boundaries' job, and
  // tags are now derived server-side (`kobo_api.personas._derive_tags`) —
  // this form sends none at all.
  //
  // This component owns only its own four form fields plus the mutation
  // that posts them — it does NOT own the "which persona is selected" or
  // "am I showing the list or the builder" state, both of which stay in
  // CreateModal (this is the thing that swaps in and out, not the thing
  // deciding when to swap). `team` is passed in from CreateModal's own team
  // field rather than being a field here: the brief's fixed-field list for
  // this form never includes one, so there's nothing to re-enter —
  // whatever the create form's team currently says is what the new persona
  // is tagged with.
  import { createMutation, useQueryClient } from "@tanstack/svelte-query";
  import { ApiError, errorMessage, type PersonaForm } from "../lib/api";
  import { createPersonaMutation } from "../lib/queries";
  import { splitLines } from "../lib/validate";

  let {
    team,
    teamValid,
    oncancel,
    oncreated,
  }: {
    team: string;
    teamValid: boolean;
    oncancel: () => void;
    oncreated: (id: string) => void;
  } = $props();

  const queryClient = useQueryClient();
  const mutation = createMutation(createPersonaMutation(queryClient));

  let name = $state("");
  let whoTheyAre = $state("");
  let whatTheyDo = $state("");
  let rulesInput = $state("");
  let submitError = $state<string | null>(null);

  const rules = $derived(splitLines(rulesInput));

  // Shown only once there's something to say about it — an untouched
  // textarea isn't "invalid", it's just empty, same convention v1 used for
  // its Boundaries field.
  const rulesError = $derived(
    rules.length > 8 ? "Use up to 8, one per line." : null,
  );

  const canCreate = $derived(
    teamValid &&
      name.trim().length > 0 &&
      whoTheyAre.trim().length > 0 &&
      whatTheyDo.trim().length > 0 &&
      rules.length <= 8 &&
      !$mutation.isPending,
  );

  function handleSubmit(event: SubmitEvent): void {
    event.preventDefault();
    if (!canCreate) {
      return;
    }
    submitError = null;
    const body: PersonaForm = {
      name,
      who_they_are: whoTheyAre,
      what_they_do: whatTheyDo,
      rules,
      team,
    };
    $mutation.mutate(body, {
      onSuccess: (created) => {
        oncreated(created.id);
      },
      onError: (error: unknown) => {
        if (error instanceof ApiError && error.status === 409) {
          // The server may already have this persona from an earlier
          // attempt that landed but whose response never made it back here
          // (a dropped connection, a refresh mid-request, ...) — pointing
          // that out beats a bare "already exists" that leaves the user
          // wondering whether to go look for it.
          submitError =
            "A persona with that name already exists — it may already be in the list.";
        } else {
          submitError = errorMessage(error, "Something went wrong. Try again.");
        }
      },
    });
  }
</script>

<form class="persona-builder" onsubmit={handleSubmit}>
  <div class="persona-builder__field">
    <label class="persona-builder__label" for="persona-builder-name">Name</label>
    <input
      id="persona-builder-name"
      class="persona-builder__input"
      type="text"
      bind:value={name}
      autocomplete="off"
    />
  </div>

  <div class="persona-builder__field">
    <label class="persona-builder__label" for="persona-builder-who-they-are">
      Who they are
    </label>
    <input
      id="persona-builder-who-they-are"
      class="persona-builder__input"
      type="text"
      bind:value={whoTheyAre}
      placeholder="an unflappable network detective who's seen every outage twice"
      autocomplete="off"
    />
    <p class="persona-builder__help">One vivid line — who is this agent?</p>
  </div>

  <div class="persona-builder__field">
    <label class="persona-builder__label" for="persona-builder-what-they-do">
      What they do
    </label>
    <textarea
      id="persona-builder-what-they-do"
      class="persona-builder__textarea"
      bind:value={whatTheyDo}
      rows="3"
    ></textarea>
    <p class="persona-builder__help">2-4 sentences, plain prose.</p>
  </div>

  <div class="persona-builder__field">
    <label class="persona-builder__label" for="persona-builder-rules">
      Rules (optional)
    </label>
    <textarea
      id="persona-builder-rules"
      class="persona-builder__textarea"
      bind:value={rulesInput}
      rows="3"
      placeholder="One per line — things they always or never do."
      aria-describedby="persona-builder-rules-status"
      aria-invalid={rulesError !== null}
    ></textarea>
    <p class="persona-builder__help">Up to 8, one per line.</p>
    <div id="persona-builder-rules-status">
      {#if rulesError}
        <p class="persona-builder__status persona-builder__status--error" role="alert">
          {rulesError}
        </p>
      {/if}
    </div>
  </div>

  {#if submitError}
    <p class="persona-builder__status persona-builder__status--error" role="alert">
      {submitError}
    </p>
  {/if}

  {#if !teamValid}
    <p class="persona-builder__hint">Pick your team above first.</p>
  {/if}

  <div class="persona-builder__actions">
    <button type="button" class="persona-builder__cancel" onclick={oncancel}>
      Cancel
    </button>
    <button type="submit" class="persona-builder__submit" disabled={!canCreate}>
      {$mutation.isPending ? "Creating…" : "Create persona"}
    </button>
  </div>
</form>

<style>
  .persona-builder {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
  }

  .persona-builder__field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .persona-builder__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.9rem;
  }

  .persona-builder__input,
  .persona-builder__textarea {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .persona-builder__textarea {
    resize: vertical;
  }

  .persona-builder__input:focus-visible,
  .persona-builder__textarea:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .persona-builder__help {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .persona-builder__status {
    margin: 0;
    font-size: 0.8rem;
  }

  .persona-builder__status--error {
    color: var(--fail);
  }

  .persona-builder__hint {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .persona-builder__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-sm);
  }

  .persona-builder__cancel {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    cursor: pointer;
  }

  .persona-builder__cancel:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .persona-builder__submit {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .persona-builder__submit:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .persona-builder__submit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
