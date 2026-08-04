<script lang="ts">
  // Inline "create a new persona" form: swapped in for CreateModal's persona
  // search list when the user picks its "Build your own" card. This
  // component owns only its own six form fields (name, role, purpose, tone,
  // tone note, boundaries) plus tags, and the mutation that posts them — it
  // does NOT own the "which persona is selected" or "am I showing the list
  // or the builder" state, both of which stay in CreateModal (this is the
  // thing that swaps in and out, not the thing deciding when to swap).
  // `team` is passed in from CreateModal's own team field rather than being
  // a field here: the brief's fixed-field list for this form never
  // includes one, so there's nothing to re-enter — whatever the create
  // form's team currently says is what the new persona is tagged with.
  import { createMutation, useQueryClient } from "@tanstack/svelte-query";
  import { ApiError, errorMessage, type PersonaForm } from "../lib/api";
  import { createPersonaMutation } from "../lib/queries";
  import { splitLines, splitTags, validateSlug } from "../lib/validate";

  let {
    team,
    oncancel,
    oncreated,
  }: {
    team: string;
    oncancel: () => void;
    oncreated: (id: string) => void;
  } = $props();

  const queryClient = useQueryClient();
  const mutation = createMutation(createPersonaMutation(queryClient));

  type Tone = PersonaForm["tone"];

  let name = $state("");
  let role = $state("");
  let purpose = $state("");
  let tone = $state<Tone>("warm");
  let toneNote = $state("");
  let boundariesInput = $state("");
  let tagsInput = $state("");
  let submitError = $state<string | null>(null);

  const boundaries = $derived(splitLines(boundariesInput));
  const tags = $derived(splitTags(tagsInput));

  // Shown only once there's something to say about it — an untouched
  // textarea isn't "invalid", it's just empty, same convention as
  // CreateModal's nameError.
  const boundariesError = $derived(
    boundaries.length > 8 ? "Use up to 8, one per line." : null,
  );

  const tagsError = $derived.by((): string | null => {
    if (tagsInput.trim().length === 0) {
      return null;
    }
    if (tags.length === 0) {
      return "Add at least one tag.";
    }
    if (tags.length > 6) {
      return "Use up to 6 tags.";
    }
    if (tags.some((tag) => validateSlug(tag) !== null)) {
      return "Tags must be lowercase letters, numbers, and hyphens.";
    }
    return null;
  });

  const canCreate = $derived(
    name.trim().length > 0 &&
      role.trim().length > 0 &&
      purpose.trim().length > 0 &&
      tags.length >= 1 &&
      tags.length <= 6 &&
      tags.every((tag) => validateSlug(tag) === null) &&
      boundaries.length <= 8 &&
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
      role,
      purpose,
      tone,
      tone_note: toneNote,
      boundaries,
      tags,
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
    <label class="persona-builder__label" for="persona-builder-role">Role</label>
    <input
      id="persona-builder-role"
      class="persona-builder__input"
      type="text"
      bind:value={role}
      autocomplete="off"
    />
  </div>

  <div class="persona-builder__field">
    <label class="persona-builder__label" for="persona-builder-purpose">Purpose</label>
    <textarea
      id="persona-builder-purpose"
      class="persona-builder__textarea"
      bind:value={purpose}
      rows="3"
    ></textarea>
  </div>

  <div class="persona-builder__field">
    <label class="persona-builder__label" for="persona-builder-tone">Tone</label>
    <select
      id="persona-builder-tone"
      class="persona-builder__input"
      value={tone}
      onchange={(event) =>
        (tone = (event.currentTarget as HTMLSelectElement).value as Tone)}
    >
      <option value="warm">Warm</option>
      <option value="direct">Direct</option>
      <option value="playful">Playful</option>
      <option value="formal">Formal</option>
    </select>
  </div>

  <div class="persona-builder__field">
    <label class="persona-builder__label" for="persona-builder-tone-note">
      Tone note (optional)
    </label>
    <input
      id="persona-builder-tone-note"
      class="persona-builder__input"
      type="text"
      bind:value={toneNote}
      autocomplete="off"
    />
  </div>

  <div class="persona-builder__field">
    <label class="persona-builder__label" for="persona-builder-boundaries">
      Boundaries (optional)
    </label>
    <textarea
      id="persona-builder-boundaries"
      class="persona-builder__textarea"
      bind:value={boundariesInput}
      rows="3"
      placeholder="One per line"
      aria-describedby="persona-builder-boundaries-status"
      aria-invalid={boundariesError !== null}
    ></textarea>
    <p class="persona-builder__help">One per line, up to 8.</p>
    <div id="persona-builder-boundaries-status">
      {#if boundariesError}
        <p class="persona-builder__status persona-builder__status--error" role="alert">
          {boundariesError}
        </p>
      {/if}
    </div>
  </div>

  <div class="persona-builder__field">
    <label class="persona-builder__label" for="persona-builder-tags">Tags</label>
    <input
      id="persona-builder-tags"
      class="persona-builder__input"
      type="text"
      bind:value={tagsInput}
      placeholder="e.g. support, docs"
      autocomplete="off"
      aria-describedby="persona-builder-tags-status"
      aria-invalid={tagsError !== null}
    />
    <p class="persona-builder__help">
      Comma or space separated. 1-6 tags, lowercase letters, numbers, and hyphens.
    </p>
    <div id="persona-builder-tags-status">
      {#if tagsError}
        <p class="persona-builder__status persona-builder__status--error" role="alert">
          {tagsError}
        </p>
      {/if}
    </div>
  </div>

  {#if submitError}
    <p class="persona-builder__status persona-builder__status--error" role="alert">
      {submitError}
    </p>
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
