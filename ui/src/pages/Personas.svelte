<script lang="ts">
  // The persona catalog (hardening wave): the naming law made visible. Lists
  // active personas via the existing unfiltered search (`personasSearchQuery`,
  // the same query CreateModal warms) as cards showing the DISPLAY
  // HIERARCHY the naming law is about -- the persona's NAME is the primary
  // line, `who_they_are`/`description` the grey secondary line directly
  // beneath, team and tags as chips, a builtin badge where true.
  //
  // Archived personas are already excluded from that search server-side
  // (the catalog ingest drops them -- see api.ts's own doc comment on
  // `searchPersonas`), so the active list needs no client-side filtering at
  // all. A "Show archived" toggle reveals a second section fed by
  // `archivedPersonasQuery`, fetched lazily behind that toggle -- the same
  // "don't fetch until a human actually asks to see it" idiom
  // `GroupRequestsCard`'s own expand/collapse toggle and `slackManifestQuery`'s
  // `opened` flag both already use in this room. Its cards carry Unarchive
  // instead of Archive.
  //
  // The archive/unarchive gate (builtin or team "shared": operator only; any
  // other team: a member of that team, or an operator) is enforced entirely
  // server-side -- every card shows both kinds of controls to everyone; a
  // refusal's exact `{error}` copy (naming the actual requirement) surfaces
  // as an error toast, the house idiom (toast.svelte.ts) AgentDetail's own
  // retry/avatar failures already use.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { derived, writable } from "svelte/store";
  import EmptyState from "../components/EmptyState.svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import Skeleton from "../components/Skeleton.svelte";
  import { errorMessage, type ArchivedPersona, type CatalogEntry } from "../lib/api";
  import {
    archivePersonaMutation,
    archivedPersonasQuery,
    personasSearchQuery,
    unarchivePersonaMutation,
  } from "../lib/queries";
  import { toast } from "../lib/toast.svelte";

  const active = createQuery(() => (personasSearchQuery("", null)));

  let showArchived = $state(false);
  const archived = createQuery(() => (archivedPersonasQuery(showArchived)));

  function toggleArchived(): void {
    showArchived.update((value) => !value);
  }

  const queryClient = useQueryClient();
  const archiveMutation = createMutation(() => (archivePersonaMutation(queryClient)));
  const unarchiveMutation = createMutation(() => (unarchivePersonaMutation(queryClient)));

  function handleArchive(id: string): void {
    archiveMutation.mutate(id, {
      onError: (error: unknown) => {
        toast.error(
          "Couldn't archive that persona",
          errorMessage(error, "Something went wrong. Try again."),
        );
      },
    });
  }

  function handleUnarchive(id: string): void {
    unarchiveMutation.mutate(id, {
      onError: (error: unknown) => {
        toast.error(
          "Couldn't unarchive that persona",
          errorMessage(error, "Something went wrong. Try again."),
        );
      },
    });
  }
</script>

<div class="personas-page">
  <div class="personas-page__header">
    <h1 class="personas-page__title">Personas</h1>
    <button
      type="button"
      class="personas-page__toggle"
      onclick={toggleArchived}
      aria-expanded={showArchived}
    >
      {showArchived ? "Hide archived" : "Show archived"}
    </button>
  </div>

  {#if active.isPending}
    <Skeleton rows={3} />
  {:else if active.isError}
    <ErrorState
      message={errorMessage(active.error, "Couldn't load personas.")}
      onretry={() => active.refetch()}
    />
  {:else if active.data.length === 0}
    <EmptyState message="No personas yet." />
  {:else}
    <ul class="persona-list">
      {#each active.data as persona (persona.id)}
        {@render personaCard(persona, "archive")}
      {/each}
    </ul>
  {/if}

  {#if showArchived}
    <div class="personas-page__archived">
      <h2 class="personas-page__archived-title">Archived</h2>
      {#if archived.isPending}
        <Skeleton rows={2} />
      {:else if archived.isError}
        <ErrorState
          message={errorMessage(archived.error, "Couldn't load archived personas.")}
          onretry={() => archived.refetch()}
        />
      {:else if archived.data.length === 0}
        <EmptyState message="No archived personas." />
      {:else}
        <ul class="persona-list">
          {#each archived.data as persona (persona.id)}
            {@render personaCard(persona, "unarchive")}
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</div>

{#snippet personaCard(persona: CatalogEntry | ArchivedPersona, mode: "archive" | "unarchive")}
  <li class="persona-card">
    <div class="persona-card__body">
      <div class="persona-card__head">
        <span class="persona-card__name">{persona.name}</span>
        {#if persona.builtin}
          <span class="persona-card__badge persona-card__badge--builtin">builtin</span>
        {/if}
      </div>
      <p class="persona-card__description">{persona.description}</p>
      <div class="persona-card__chips">
        <span class="persona-card__chip">{persona.team}</span>
        {#each persona.tags as tag (tag)}
          <span class="persona-card__chip">{tag}</span>
        {/each}
      </div>
    </div>
    {#if mode === "archive"}
      <button
        type="button"
        class="persona-card__action"
        disabled={archiveMutation.isPending}
        onclick={() => handleArchive(persona.id)}
      >
        Archive
      </button>
    {:else}
      <button
        type="button"
        class="persona-card__action"
        disabled={unarchiveMutation.isPending}
        onclick={() => handleUnarchive(persona.id)}
      >
        Unarchive
      </button>
    {/if}
  </li>
{/snippet}

<style>
  .personas-page {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
  }

  .personas-page__header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
  }

  .personas-page__title {
    margin: 0;
    color: var(--ink);
  }

  .personas-page__toggle {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-size: 0.85rem;
    cursor: pointer;
  }

  .personas-page__toggle:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .personas-page__archived {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    padding-top: var(--space-md);
    border-top: 1px solid var(--border);
  }

  .personas-page__archived-title {
    margin: 0;
    color: var(--muted);
    font-size: 0.9rem;
  }

  .persona-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .persona-card {
    display: flex;
    align-items: flex-start;
    gap: var(--space-sm);
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .persona-card__body {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: 0.2rem;
    min-width: 0;
  }

  .persona-card__head {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
  }

  /* The naming law's display hierarchy: the NAME is the primary line here
     (ink-toned, bold) -- `.persona-card__description` right beneath it is
     the grey secondary line, never a co-equal title. */
  .persona-card__name {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.95rem;
  }

  .persona-card__description {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .persona-card__chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2xs);
    margin-top: var(--space-2xs);
  }

  .persona-card__chip,
  .persona-card__badge {
    display: inline-flex;
    padding: 0.1rem var(--space-2xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.7rem;
    white-space: nowrap;
  }

  .persona-card__badge--builtin {
    border-color: var(--accent);
    color: var(--accent);
  }

  .persona-card__action {
    flex-shrink: 0;
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .persona-card__action:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .persona-card__action:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
