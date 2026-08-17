<script lang="ts">
  // What a ready agent can do, on top of what it knows (MemoryPanel's
  // `bundle/knowledge/*.md`). Two provenance classes share one list: a
  // catalog pick (read-only content, lives in the org's skills catalog —
  // Remove only) and a local, guardrail-authored skill (edited here —
  // Edit + Remove). Same rebuild-the-user-never-names convention as
  // MemoryPanel: copy speaks only of saving/adding/removing, never
  // "commit"/"deploy"/"repo"/"pipeline".
  //
  // Three affordances, three different shapes:
  //   - "Add from catalog": an inline, toggleable search section (fresh
  //     markup adapted from CreateModal's debounced skill search — CreateModal
  //     itself is untouched) that Adds a pick immediately per row (single-select,
  //     not a staged multi-pick + submit).
  //   - "New guardrail skill": opens the shared editor Modal (copied from
  //     MemoryPanel: filename + textarea, review-with-diff, then save).
  //   - Edit / Remove on an existing row: Edit re-opens that same Modal in
  //     edit mode (after fetching the skill's current content — unlike
  //     MemoryPanel's knowledge list, `agentSkillsQuery` doesn't carry
  //     content, only provenance, so edit mode has its own tiny load state
  //     ahead of the familiar review/diff/save flow); Remove opens a
  //     confirm step, same Modal again.
  //
  // Delivery-chip reactivity: copied verbatim from MemoryPanel/SlackPanel —
  // see MemoryPanel's doc comment for why `deliverySha` is a plain
  // `writable`/`derived` pair rather than `$state`/`$derived` (the installed
  // @tanstack/svelte-query adapter only re-derives a query's options from an
  // actual Svelte store). `deliveryQuery` itself is shared as-is: it checks
  // git ancestry vs. the live image for a given sha, a question that has
  // nothing knowledge-specific about it despite living under a
  // "knowledge/delivery" path — any sha this room pushes (a knowledge save,
  // a skill add/edit/remove) is delivered and checked the same way.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { derived, writable } from "svelte/store";
  import EmptyState from "./EmptyState.svelte";
  import ErrorState from "./ErrorState.svelte";
  import Modal from "./Modal.svelte";
  import Skeleton from "./Skeleton.svelte";
  import { errorMessage, readLocalSkill, type AgentSkill, type CatalogEntry } from "../lib/api";
  import { debounced } from "../lib/debounce";
  import { lineDiff, type DiffLine } from "../lib/diff";
  import {
    addCatalogSkillMutation,
    agentSkillsQuery,
    deleteSkillMutation,
    deliveryQuery,
    saveLocalSkillMutation,
    skillsSearchQuery,
    teamsQuery,
  } from "../lib/queries";

  let { agentName, frozen = false }: { agentName: string; frozen?: boolean } = $props();

  const queryClient = useQueryClient();
  // `agentName` is read once here rather than tracked reactively: same
  // reasoning as MemoryPanel/ChatPanel — AgentDetail (the only caller)
  // remounts this component via `{#key route.name}` whenever the routed
  // agent changes, so this prop is fixed for the component's entire lifetime.
  // svelte-ignore state_referenced_locally
  const skills = createQuery(agentSkillsQuery(agentName));
  // svelte-ignore state_referenced_locally
  const teams = createQuery(teamsQuery());
  // svelte-ignore state_referenced_locally
  const addCatalog = createMutation(addCatalogSkillMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const saveMutation = createMutation(saveLocalSkillMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const deleteMutation = createMutation(deleteSkillMutation(queryClient, agentName));

  // --- Delivery chip (copied pattern from MemoryPanel; see its doc comment
  // and the module comment above for the full rationale) -------------------

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

  // --- Add from catalog: debounced search, single-select, adds on click ---
  // (fresh inline markup adapted from CreateModal's skill search — see its
  // own doc comment for why the debounced term lives in a plain `writable`
  // rather than `$derived`; same @tanstack/svelte-query version constraint.)

  let catalogOpen = $state(false);
  let catalogQ = $state("");
  // "" means "All teams" — a <select>'s DOM value is always a string;
  // converted to `null` only where the search params are committed, below.
  let catalogTeamFilter = $state("");
  let catalogError = $state<string | null>(null);
  let pendingCatalogAdd = $state<string | null>(null);

  const catalogSearchParams = writable<{ q: string; team: string | null }>({
    q: "",
    team: null,
  });
  const catalogSearchOptions = derived(catalogSearchParams, ($params) =>
    skillsSearchQuery($params.q, $params.team),
  );
  const catalogResults = createQuery(catalogSearchOptions);

  const commitCatalogSearch = debounced((value: { q: string; team: string | null }) => {
    catalogSearchParams.set(value);
  }, 250);

  $effect(() => {
    const value = { q: catalogQ, team: catalogTeamFilter === "" ? null : catalogTeamFilter };
    if (!catalogOpen) {
      return;
    }
    commitCatalogSearch(value);
    return () => {
      commitCatalogSearch.cancel();
    };
  });

  /** Toggles the catalog search section, resetting it fresh every time it
   * opens — same "clean slate on open" idiom as CreateModal's own reset
   * effect, just scoped to this toggle instead of a whole-modal `open`. */
  function toggleCatalog(): void {
    catalogOpen = !catalogOpen;
    if (catalogOpen) {
      catalogQ = "";
      catalogTeamFilter = "";
      catalogError = null;
      catalogSearchParams.set({ q: "", team: null });
    }
  }

  function alreadyCarried(skillName: string): boolean {
    return $skills.data?.some((s) => s.name === skillName) ?? false;
  }

  function handleAddFromCatalog(entry: CatalogEntry): void {
    if (alreadyCarried(entry.name) || pendingCatalogAdd !== null) {
      return;
    }
    catalogError = null;
    pendingCatalogAdd = entry.name;
    $addCatalog.mutate(
      { skillName: entry.name, team: entry.team },
      {
        onSuccess: (result) => {
          pendingCatalogAdd = null;
          beginDelivery(result.sha);
        },
        onError: (err: unknown) => {
          pendingCatalogAdd = null;
          catalogError = errorMessage(err, "Couldn't add that skill. Try again.");
        },
      },
    );
  }

  // --- Modal: new/edit local skill, or delete (either class) --------------

  type Target =
    | { kind: "edit"; skill: AgentSkill }
    | { kind: "add" }
    | { kind: "delete"; skill: AgentSkill };

  type EditLoad = { status: "loading" } | { status: "error"; message: string } | { status: "ready" };

  let target = $state<Target | null>(null);
  let content = $state("");
  let originalContent = $state("");
  let filename = $state("");
  let reviewing = $state(false);
  let modalError = $state<string | null>(null);
  let editLoad = $state<EditLoad>({ status: "ready" });

  // Above this many (before-lines × after-lines) cells, the LCS table in
  // diff.ts's O(n·m) algorithm gets expensive enough that it's not worth
  // paying for a preview the user is about to skim past anyway.
  const DIFF_LINE_PRODUCT_LIMIT = 1_000_000;

  /** Line count under diff.ts's own convention: an empty string is zero
   * lines, not one (see diff.ts's `splitLines`). This only estimates the
   * cost of a diff for the guard below — it doesn't compute one. */
  function lineCount(text: string): number {
    return text === "" ? 0 : text.split("\n").length;
  }

  function diffMarker(kind: DiffLine["kind"]): string {
    switch (kind) {
      case "added":
        return "+ ";
      case "removed":
        return "− ";
      default:
        return "  ";
    }
  }

  function shortSha(sha: string): string {
    return sha.slice(0, 12);
  }

  /** The provenance line under a row's name: `team · sha12` for a catalog
   * pick, `local · editable` for a guardrail file. */
  function provenanceLine(skill: AgentSkill): string {
    if (skill.source === "local") {
      return "local · editable";
    }
    const team = skill.team ?? "unknown team";
    const sha = skill.sha !== null ? shortSha(skill.sha) : "no pin yet";
    return `${team} · ${sha}`;
  }

  function openAdd(): void {
    target = { kind: "add" };
    content = "";
    originalContent = "";
    filename = "";
    reviewing = false;
    modalError = null;
    editLoad = { status: "ready" };
  }

  /** Opens the editor on an existing local skill, fetching its current
   * content first (unlike MemoryPanel's knowledge files, `AgentSkill` from
   * the list query carries provenance only, not content). Guards against a
   * stale response landing after the user has already closed the modal or
   * moved on to a different skill — same idiom as CreateModal's debounced
   * availability check guarding on `value !== name`. */
  async function openEdit(skill: AgentSkill): Promise<void> {
    const requestedName = skill.name;
    target = { kind: "edit", skill };
    content = "";
    originalContent = "";
    filename = "";
    reviewing = false;
    modalError = null;
    editLoad = { status: "loading" };
    try {
      const result = await readLocalSkill(agentName, requestedName);
      if (target?.kind !== "edit" || target.skill.name !== requestedName) {
        return;
      }
      content = result.content;
      originalContent = result.content;
      editLoad = { status: "ready" };
    } catch (err) {
      if (target?.kind !== "edit" || target.skill.name !== requestedName) {
        return;
      }
      editLoad = { status: "error", message: errorMessage(err, "Couldn't load this skill.") };
    }
  }

  function openDelete(skill: AgentSkill): void {
    target = { kind: "delete", skill };
    modalError = null;
  }

  function closeModal(): void {
    target = null;
    content = "";
    originalContent = "";
    filename = "";
    reviewing = false;
    modalError = null;
    editLoad = { status: "ready" };
  }

  function backToEditing(): void {
    reviewing = false;
    modalError = null;
  }

  const contentUnchanged = $derived(content === originalContent);

  const filenameError = $derived.by((): string | null => {
    if (target?.kind !== "add") {
      return null;
    }
    if (filename.length === 0) {
      return null; // nothing to say about an empty field yet
    }
    if (filename.length > 40) {
      return "Use 1-40 characters.";
    }
    if (!/^[a-z0-9-]+$/.test(filename)) {
      return "Use lowercase letters, numbers, and hyphens only.";
    }
    if (!/^[a-z]/.test(filename)) {
      return "Must start with a letter.";
    }
    if (alreadyCarried(filename)) {
      return "There's already a skill with this name.";
    }
    return null;
  });

  const canReview = $derived.by((): boolean => {
    if (target === null || target.kind === "delete") {
      return false;
    }
    if (target.kind === "edit" && editLoad.status !== "ready") {
      return false;
    }
    if (content.trim().length === 0 || contentUnchanged) {
      return false;
    }
    if (target.kind === "add" && (filename.length === 0 || filenameError !== null)) {
      return false;
    }
    return true;
  });

  const diffTooLarge = $derived(
    lineCount(originalContent) * lineCount(content) > DIFF_LINE_PRODUCT_LIMIT,
  );

  const diffLines = $derived.by((): DiffLine[] => {
    if (!reviewing || diffTooLarge) {
      return [];
    }
    return lineDiff(originalContent, content);
  });

  const modalTitle = $derived.by((): string | undefined => {
    if (target === null) {
      return undefined;
    }
    if (target.kind === "add") {
      return "New guardrail skill";
    }
    if (target.kind === "delete") {
      return `Remove ${target.skill.name}`;
    }
    return `Edit ${target.skill.name}`;
  });

  function handleSave(): void {
    if (target === null || target.kind === "delete") {
      return;
    }
    const skillId = target.kind === "edit" ? target.skill.name : filename;
    modalError = null;
    $saveMutation.mutate(
      { skillId, content },
      {
        onSuccess: (result) => {
          beginDelivery(result.sha);
          closeModal();
        },
        onError: (err: unknown) => {
          modalError = errorMessage(err, "Couldn't save. Try again.");
        },
      },
    );
  }

  function handleDelete(): void {
    if (target?.kind !== "delete") {
      return;
    }
    const skillId = target.skill.name;
    modalError = null;
    $deleteMutation.mutate(skillId, {
      onSuccess: (result) => {
        beginDelivery(result.sha);
        closeModal();
      },
      onError: (err: unknown) => {
        modalError = errorMessage(err, "Couldn't remove it. Try again.");
      },
    });
  }
</script>

<section class="skills-panel" aria-labelledby="skills-panel-heading">
  <div class="skills-panel__header">
    <div>
      <h2 id="skills-panel-heading" class="skills-panel__heading">Skills</h2>
      <p class="skills-panel__subtitle">What your agent can do, on top of what it knows.</p>
    </div>
    <div class="skills-panel__header-actions">
      <button type="button" class="skills-panel__secondary" disabled={frozen} onclick={toggleCatalog}>
        {catalogOpen ? "Hide catalog" : "Add from catalog"}
      </button>
      <button type="button" class="skills-panel__add" disabled={frozen} onclick={openAdd}>
        New guardrail skill
      </button>
    </div>
  </div>

  {#if $skills.isPending}
    <Skeleton rows={3} />
  {:else if $skills.isError}
    <ErrorState
      message={errorMessage($skills.error, "Couldn't load this agent's skills.")}
      onretry={() => $skills.refetch()}
    />
  {:else if $skills.data.length === 0}
    <EmptyState message="Nothing here yet. Add a skill from the catalog or write a guardrail." />
  {:else}
    <ul class="skills-panel__list">
      {#each $skills.data as skill (skill.name)}
        <li class="skill-row">
          <span class="skill-row__body">
            <span class="skill-row__name">{skill.name}</span>
            <span class="skill-row__provenance">{provenanceLine(skill)}</span>
          </span>
          <span class="skill-row__actions">
            {#if skill.editable}
              <button
                type="button"
                class="skill-row__edit"
                disabled={frozen}
                onclick={() => openEdit(skill)}
              >
                Edit
              </button>
            {/if}
            <button
              type="button"
              class="skill-row__remove"
              disabled={frozen}
              aria-label={`Remove ${skill.name}`}
              onclick={() => openDelete(skill)}
            >
              Remove
            </button>
          </span>
        </li>
      {/each}
    </ul>
  {/if}

  {#if catalogOpen}
    <div class="skills-panel__catalog">
      <div class="skills-panel__catalog-row">
        <label class="skills-panel__sr-only" for="skills-catalog-search">
          Search skills catalog
        </label>
        <input
          id="skills-catalog-search"
          class="skills-panel__catalog-input"
          type="text"
          bind:value={catalogQ}
          placeholder="Search skills"
          autocomplete="off"
          disabled={frozen}
        />
        <label class="skills-panel__sr-only" for="skills-catalog-team">Filter by team</label>
        <select
          id="skills-catalog-team"
          class="skills-panel__catalog-input"
          value={catalogTeamFilter}
          disabled={frozen}
          onchange={(event) =>
            (catalogTeamFilter = (event.currentTarget as HTMLSelectElement).value)}
        >
          <option value="">All teams</option>
          {#if $teams.data}
            {#each $teams.data as t (t)}
              <option value={t}>{t}</option>
            {/each}
          {/if}
        </select>
      </div>

      {#if catalogError}
        <p class="skills-panel__catalog-error" role="alert">{catalogError}</p>
      {/if}

      {#if $catalogResults.isPending}
        <Skeleton rows={2} />
      {:else if $catalogResults.isError}
        <ErrorState
          message={errorMessage($catalogResults.error, "Couldn't load the skills catalog.")}
          onretry={() => $catalogResults.refetch()}
        />
      {:else if $catalogResults.data.length === 0}
        <p class="skills-panel__catalog-help">
          {catalogQ.length > 0 ? "Nothing matches your search." : "No skills in the catalog yet."}
        </p>
      {:else}
        <ul class="skills-panel__catalog-list">
          {#each $catalogResults.data as entry (entry.id)}
            {@const already = alreadyCarried(entry.name)}
            {@const pending = pendingCatalogAdd === entry.name}
            <li class="catalog-row">
              <span class="catalog-row__body">
                <span class="catalog-row__head">
                  <span class="catalog-row__name">{entry.name}</span>
                  <span class="skills-panel__badge">{entry.team}</span>
                </span>
                <span class="catalog-row__description">{entry.description}</span>
              </span>
              <button
                type="button"
                class="catalog-row__add"
                disabled={frozen || already || pending}
                onclick={() => handleAddFromCatalog(entry)}
              >
                {#if pending}
                  Adding…
                {:else if already}
                  Added
                {:else}
                  Add
                {/if}
              </button>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}

  {#if $deliverySha !== null}
    <div
      class="skills-panel__delivery skills-panel__delivery--{deliveryView.tone}"
      role="status"
      aria-live="polite"
    >
      {#if deliveryView.tone === "pending"}
        <span class="skills-panel__delivery-dot pulse" aria-hidden="true"></span>
      {/if}
      <span>{deliveryView.text}</span>
    </div>
  {/if}
</section>

<Modal open={target !== null} title={modalTitle} onclose={closeModal}>
  {#if target}
    {@const t = target}
    {#if t.kind === "delete"}
      <p class="skills-modal__confirm-text">
        This removes it from what {agentName} can do. Remove?
      </p>
      {#if modalError}
        <p class="skills-modal__error" role="alert">{modalError}</p>
      {/if}
      <div class="skills-modal__actions">
        <button type="button" class="skills-modal__cancel" disabled={frozen} onclick={closeModal}>Cancel</button>
        <button
          type="button"
          class="skills-modal__danger"
          disabled={frozen || $deleteMutation.isPending}
          onclick={handleDelete}
        >
          {$deleteMutation.isPending ? "Removing…" : "Remove"}
        </button>
      </div>
    {:else if t.kind === "edit" && editLoad.status !== "ready"}
      {@const load = editLoad}
      {#if load.status === "loading"}
        <Skeleton rows={4} />
      {:else if load.status === "error"}
        <ErrorState message={load.message} onretry={() => openEdit(t.skill)} />
      {/if}
    {:else if reviewing}
      {#if diffTooLarge}
        <p class="skills-modal__diff-notice">
          Too much changed to preview line by line. Review your text, then save.
        </p>
      {:else}
        <div class="skills-modal__diff">
          {#each diffLines as line, i (i)}
            <p class="skills-diff-line skills-diff-line--{line.kind}">{diffMarker(
                line.kind,
              )}{line.text}</p>
          {/each}
        </div>
      {/if}
      {#if modalError}
        <p class="skills-modal__error" role="alert">{modalError}</p>
      {/if}
      <div class="skills-modal__actions">
        <button type="button" class="skills-modal__cancel" disabled={frozen} onclick={backToEditing}>
          Back to editing
        </button>
        <button
          type="button"
          class="skills-modal__primary"
          disabled={frozen || $saveMutation.isPending}
          onclick={handleSave}
        >
          {$saveMutation.isPending ? "Saving…" : "Save"}
        </button>
      </div>
    {:else}
      <form
        class="skills-form"
        onsubmit={(event) => {
          event.preventDefault();
          if (canReview) {
            reviewing = true;
          }
        }}
      >
        {#if t.kind === "add"}
          <div class="skills-form__field">
            <label class="skills-form__label" for="skills-filename">Name</label>
            <input
              id="skills-filename"
              class="skills-form__input"
              type="text"
              bind:value={filename}
              autocomplete="off"
              spellcheck="false"
              aria-describedby="skills-filename-status"
              aria-invalid={filenameError !== null}
              disabled={frozen}
            />
            <p class="skills-form__help">
              Lowercase letters, numbers, and hyphens. 1-40 characters.
            </p>
            <div id="skills-filename-status">
              {#if filenameError}
                <p class="skills-form__status skills-form__status--error" role="alert">
                  {filenameError}
                </p>
              {/if}
            </div>
          </div>
        {:else}
          <p class="skills-form__filename">{t.skill.name}</p>
        {/if}

        <div class="skills-form__field">
          <label class="skills-form__label" for="skills-content">Guardrail text</label>
          <textarea
            id="skills-content"
            class="skills-form__textarea"
            bind:value={content}
            rows="10"
            disabled={frozen}
          ></textarea>
          <p class="skills-form__help">
            Guardrail skills need frontmatter with name: and description: lines.
          </p>
          {#if content.trim().length === 0}
            <p class="skills-form__help">Add a bit of text before saving.</p>
          {/if}
        </div>

        {#if modalError}
          <p class="skills-modal__error" role="alert">{modalError}</p>
        {/if}

        <div class="skills-modal__actions">
          <button type="button" class="skills-modal__cancel" disabled={frozen} onclick={closeModal}>Cancel</button>
          <button type="submit" class="skills-modal__primary" disabled={frozen || !canReview}>
            {contentUnchanged ? "No changes yet" : "Review changes"}
          </button>
        </div>
      </form>
    {/if}
  {/if}
</Modal>

<style>
  .skills-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .skills-panel__sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  .skills-panel__header {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-md);
  }

  .skills-panel__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .skills-panel__subtitle {
    margin: var(--space-2xs) 0 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .skills-panel__header-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-xs);
  }

  .skills-panel__secondary,
  .skills-panel__add {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .skills-panel__secondary:hover,
  .skills-panel__add:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .skills-panel__list {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .skill-row {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
  }

  .skill-row__body {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 0;
  }

  .skill-row__name {
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .skill-row__provenance {
    color: var(--muted);
    font-size: 0.8rem;
  }

  .skill-row__actions {
    display: flex;
    gap: var(--space-xs);
  }

  .skill-row__edit,
  .skill-row__remove {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--muted);
    cursor: pointer;
    white-space: nowrap;
  }

  .skill-row__edit:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .skill-row__remove:hover {
    border-color: var(--fail);
    color: var(--fail);
  }

  .skills-panel__catalog {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
  }

  .skills-panel__catalog-row {
    display: flex;
    gap: var(--space-xs);
  }

  .skills-panel__catalog-input {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .skills-panel__catalog-row .skills-panel__catalog-input:first-child {
    flex: 1;
  }

  .skills-panel__catalog-input:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .skills-panel__catalog-help {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .skills-panel__catalog-error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .skills-panel__catalog-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .catalog-row {
    display: flex;
    align-items: flex-start;
    gap: var(--space-xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
  }

  .catalog-row__body {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 0;
  }

  .catalog-row__head {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
  }

  .catalog-row__name {
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.85rem;
    font-weight: 600;
  }

  .catalog-row__description {
    color: var(--muted);
    font-size: 0.8rem;
  }

  .skills-panel__badge {
    display: inline-flex;
    padding: 0.1rem var(--space-2xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.7rem;
    white-space: nowrap;
  }

  .catalog-row__add {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .catalog-row__add:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .catalog-row__add:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .skills-panel__delivery {
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

  .skills-panel__delivery--ok {
    border-color: var(--ok);
    color: var(--ok);
  }

  .skills-panel__delivery--fail {
    border-color: var(--fail);
    color: var(--fail);
  }

  .skills-panel__delivery-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--pulse);
  }

  .skills-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
  }

  .skills-form__field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .skills-form__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.9rem;
  }

  .skills-form__filename {
    margin: 0;
    color: var(--ink);
    font-family: var(--font-mono);
    font-weight: 600;
    font-size: 0.95rem;
  }

  .skills-form__input,
  .skills-form__textarea {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .skills-form__textarea {
    font-family: var(--font-mono);
    font-size: 0.85rem;
    resize: vertical;
  }

  .skills-form__input:focus-visible,
  .skills-form__textarea:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .skills-form__help {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .skills-form__status {
    margin: 0;
    font-size: 0.8rem;
  }

  .skills-form__status--error {
    color: var(--fail);
  }

  .skills-modal__confirm-text {
    margin: 0;
    color: var(--ink);
  }

  .skills-modal__diff {
    display: flex;
    flex-direction: column;
    max-height: 24rem;
    overflow-y: auto;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }

  .skills-diff-line {
    margin: 0;
    padding: 0.1rem var(--space-xs);
    overflow-wrap: anywhere;
    font-family: var(--font-mono);
    font-size: 0.85rem;
    white-space: pre-wrap;
  }

  .skills-diff-line--added {
    background: color-mix(in srgb, var(--ok) 12%, transparent);
    color: var(--ok);
  }

  .skills-diff-line--removed {
    background: color-mix(in srgb, var(--fail) 12%, transparent);
    color: var(--fail);
  }

  .skills-diff-line--same {
    color: var(--muted);
  }

  .skills-modal__diff-notice {
    margin: 0;
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: var(--muted);
    font-size: 0.85rem;
  }

  .skills-modal__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .skills-modal__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-sm);
  }

  .skills-modal__cancel {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    cursor: pointer;
  }

  .skills-modal__cancel:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .skills-modal__cancel:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .skills-modal__primary {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .skills-modal__primary:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .skills-modal__primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .skills-modal__danger {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--fail);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .skills-modal__danger:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .skills-modal__danger:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
