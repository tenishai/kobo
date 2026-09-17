<script lang="ts">
  // Learnings rethink (2026-09-16): a learning is one sentence a person
  // actually said, with their name on it. One list, Remember / Skip, inline
  // fix, and a teach box -- adding a memory and accepting one live in the
  // same place. Mirrors MemoryPanel's own conventions: thunk-wrapped
  // createQuery/createMutation, BEM-scoped CSS below.
  //
  // Every panel mounts unconditionally inside its own tabpanel regardless of
  // which tab is active (AgentDetail's own tab invariant), so this query
  // fires as soon as the agent's detail page loads, not just when the
  // Learnings tab is selected. `canDecide` is the server's own
  // `can_decide_learnings` gate (AgentDetail passes it straight through) --
  // this panel hides the verdict buttons entirely when it's false, never
  // just disables them, since the route is the real enforcement and a
  // visible-but-dead button would only invite a 403 for nothing. `frozen`
  // (destroying/destroy_failed) disables the buttons instead, same as every
  // other panel's own `frozen` prop.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import ErrorState from "./ErrorState.svelte";
  import { errorMessage, teachAgent } from "../lib/api";
  import {
    decideLearningMutation,
    decideLearningsBulkMutation,
    learningsQuery,
  } from "../lib/queries";
  import type { Learning } from "../lib/queries";

  let {
    agentName,
    canDecide = false,
    frozen = false,
  }: { agentName: string; canDecide?: boolean; frozen?: boolean } = $props();

  const queryClient = useQueryClient();
  // `agentName` is read once here rather than tracked reactively: same
  // reasoning as MemoryPanel's own `agentName` -- AgentDetail (the only
  // caller) remounts this component via `{#key route.name}` whenever the
  // routed agent changes, so this prop is fixed for the component's entire
  // lifetime.
  // svelte-ignore state_referenced_locally
  const learnings = createQuery(() => (learningsQuery(agentName)));
  // svelte-ignore state_referenced_locally
  const decide = createMutation(() => (decideLearningMutation(queryClient, agentName)));
  // svelte-ignore state_referenced_locally
  const bulkDecide = createMutation(() => (
    decideLearningsBulkMutation(queryClient, agentName)
  ));

  let showHistory = $state(false);
  let errorText = $state("");
  let openId = $state<number | null>(null);      // row whose quote is showing
  let editingId = $state<number | null>(null);   // row being reworded
  let draft = $state("");
  let teachText = $state("");
  let teachNote = $state("");
  let confirming = $state<"approve" | "deny" | null>(null);

  const all = $derived(learnings.data?.learnings ?? []);
  const pending = $derived(all.filter((l: Learning) => l.status === "pending"));
  const history = $derived(all.filter((l: Learning) => l.status !== "pending"));
  const remembered = $derived(history.filter((l: Learning) => l.status === "approved").length);
  const skipped = $derived(history.length - remembered);

  // svelte-ignore state_referenced_locally
  const teach = createMutation(() => ({
    mutationFn: (text: string) => teachAgent(agentName, text),
    onSuccess: () => {
      teachText = "";
      teachNote = `${agentName} will remember that.`;
      queryClient.invalidateQueries({ queryKey: ["agents", agentName] });
    },
  }));

  // Verdict controls (row + bulk) lock while a decide/bulk-decide request is
  // in flight; the teach box has its own, separate lock below (`frozen ||
  // teach.isPending`) so an in-flight verdict never freezes the unrelated
  // "teach this" input -- see the review follow-up this split fixes.
  const deciding = $derived(decide.isPending || bulkDecide.isPending);

  function byline(l: Learning): string {
    return [l.said_by, l.source].filter(Boolean).join(" · ");
  }

  function startEdit(l: Learning): void {
    editingId = l.id;
    draft = l.content;
  }

  function remember(l: Learning): void {
    errorText = "";
    const edited = editingId === l.id && draft.trim() !== l.content;
    decide.mutate(
      { id: l.id, action: "approve", ...(edited ? { content: draft.trim() } : {}) },
      {
        onSuccess: () => (editingId = null),
        onError: (e: unknown) =>
          (errorText = errorMessage(e, "Couldn't save that. Try again.")),
      },
    );
  }

  function skip(id: number): void {
    errorText = "";
    decide.mutate(
      { id, action: "deny" },
      { onError: (e: unknown) => (errorText = errorMessage(e, "Couldn't skip that. Try again.")) },
    );
  }

  function decideAll(action: "approve" | "deny"): void {
    errorText = "";
    bulkDecide.mutate(
      { ids: pending.map((l: Learning) => l.id), action },
      { onError: (e: unknown) => (errorText = errorMessage(e, "Couldn't finish that. Try again.")) },
    );
  }

  function submitTeach(event: SubmitEvent): void {
    event.preventDefault();
    const text = teachText.trim();
    if (!text) return;
    errorText = "";
    teachNote = "";
    teach.mutate(text, {
      onError: (e: unknown) => (errorText = errorMessage(e, "Couldn't teach that. Try again.")),
    });
  }
</script>

<section class="learnings-panel" aria-labelledby="learnings-panel-heading">
  <h2 id="learnings-panel-heading" class="learnings-panel__heading">Learnings</h2>

  {#if canDecide}
    <form class="learnings-teach" onsubmit={submitTeach}>
      <input
        class="learnings-teach__input"
        type="text"
        bind:value={teachText}
        placeholder={`Teach ${agentName} something...`}
        aria-label={`Teach ${agentName} something`}
        maxlength="400"
        disabled={frozen || teach.isPending}
      />
      <button class="learnings-panel__button" type="submit" disabled={frozen || teach.isPending || !teachText.trim()}>
        Add
      </button>
    </form>
    {#if teachNote}<p class="learnings-panel__note" role="status">{teachNote}</p>{/if}
  {/if}

  {#if errorText}
    <p class="learnings-panel__error" role="alert">{errorText}</p>
  {/if}

  {#if learnings.isPending}
    <p class="learnings-panel__empty">Loading...</p>
  {:else if learnings.isError}
    <ErrorState
      message={errorMessage(learnings.error, "Couldn't load this agent's learnings.")}
      onretry={() => learnings.refetch()}
    />
  {:else if pending.length === 0}
    <p class="learnings-panel__empty">
      Nothing new. When someone tells {agentName} something worth keeping, it shows up here.
    </p>
  {:else}
    <div class="learnings-panel__summary">
      <p class="learnings-panel__count">
        {agentName} picked up {pending.length} {pending.length === 1 ? "thing" : "things"}
      </p>
      {#if canDecide}
        <div class="learnings-panel__bulk">
          {#if confirming}
            <span class="learnings-panel__confirm">
              {confirming === "approve" ? "Remember all" : "Skip all"} {pending.length}?
            </span>
            <button class="learnings-panel__button learnings-panel__button--quiet" type="button"
              disabled={frozen || deciding} onclick={() => (confirming = null)}>Cancel</button>
            <button class="learnings-panel__button learnings-panel__button--primary" type="button"
              disabled={frozen || deciding}
              onclick={() => { const action = confirming; confirming = null; if (action) decideAll(action); }}
            >{confirming === "approve" ? "Yes, remember" : "Yes, skip"}</button>
          {:else}
            <button class="learnings-panel__button learnings-panel__button--quiet" type="button"
              disabled={frozen || deciding} onclick={() => (confirming = "deny")}>Skip all</button>
            <button class="learnings-panel__button learnings-panel__button--primary" type="button"
              disabled={frozen || deciding} onclick={() => (confirming = "approve")}>Remember all</button>
          {/if}
        </div>
      {/if}
    </div>

    <ul class="learnings-list">
      {#each pending as l (l.id)}
        <li class="learning-row">
          {#if editingId === l.id}
            <input
              class="learning-row__edit"
              type="text"
              bind:value={draft}
              maxlength="200"
              aria-label="Fix the wording"
              onkeydown={(e) => { if (e.key === "Escape") editingId = null; }}
            />
          {:else}
            <button
              class="learning-row__statement"
              class:learning-row__statement--open={openId === l.id}
              type="button"
              aria-expanded={openId === l.id}
              onclick={() => (openId = openId === l.id ? null : l.id)}
            >{l.content}</button>
          {/if}

          <div class="learning-row__foot">
            <span class="learning-row__byline">{byline(l)}</span>
            {#if canDecide}
              <span class="learning-row__actions">
                {#if editingId === l.id}
                  <button class="learnings-panel__button learnings-panel__button--quiet" type="button"
                    onclick={() => (editingId = null)}>Cancel</button>
                {:else}
                  <button class="learnings-panel__button learnings-panel__button--quiet" type="button"
                    disabled={frozen || deciding} aria-label="Fix the wording" title="Fix the wording"
                    onclick={() => startEdit(l)}>✎</button>
                  <button class="learnings-panel__button learnings-panel__button--quiet" type="button"
                    disabled={frozen || deciding} onclick={() => skip(l.id)}>Skip</button>
                {/if}
                <button class="learnings-panel__button learnings-panel__button--primary" type="button"
                  disabled={frozen || deciding || (editingId === l.id && !draft.trim())}
                  onclick={() => remember(l)}>Remember</button>
              </span>
            {/if}
          </div>

          {#if openId === l.id && l.evidence}
            <blockquote class="learning-row__quote">{l.evidence}</blockquote>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}

  {#if history.length > 0}
    <button class="learnings-panel__history-toggle" type="button"
      aria-expanded={showHistory} onclick={() => (showHistory = !showHistory)}>
      Remembered {remembered} · Skipped {skipped}
      <span aria-hidden="true">{showHistory ? "▾" : "▸"}</span>
    </button>
    {#if showHistory}
      <ul class="learnings-list learnings-list--history">
        {#each history as l (l.id)}
          <li class="learning-row learning-row--decided">
            <p class="learning-row__statement-text">{l.content}</p>
            <span class="learning-row__byline">
              {l.status === "approved" ? "Remembered" : l.status === "denied" ? "Skipped" : "Expired"}{l.decided_by ? ` by ${l.decided_by}` : ""}
            </span>
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</section>

<style>
  .learnings-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .learnings-panel__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .learnings-panel__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .learnings-panel__empty {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .learnings-panel__button {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .learnings-panel__button:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .learnings-panel__button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .learnings-teach { display: flex; gap: var(--space-xs); }
  .learnings-teach__input,
  .learning-row__edit {
    flex: 1;
    min-width: 0;
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: var(--ink);
    font: inherit;
  }
  .learnings-teach__input:focus,
  .learning-row__edit:focus { outline: none; border-color: var(--accent); }
  .learnings-panel__note { margin: 0; color: var(--muted); font-size: 0.85rem; }
  .learnings-panel__summary {
    display: flex; flex-wrap: wrap; align-items: center;
    justify-content: space-between; gap: var(--space-sm);
  }
  .learnings-panel__count { margin: 0; color: var(--ink); font-size: 0.95rem; font-weight: 600; }
  .learnings-panel__bulk { display: flex; align-items: center; gap: var(--space-xs); }
  .learnings-panel__confirm { color: var(--muted); font-size: 0.85rem; }
  .learnings-panel__button--primary { border-color: var(--accent); color: var(--accent); }
  .learnings-panel__button--quiet { border-color: transparent; background: transparent; color: var(--muted); }
  .learnings-list { display: flex; flex-direction: column; margin: 0; padding: 0; list-style: none; }
  .learning-row {
    display: flex; flex-direction: column; gap: var(--space-2xs);
    padding: var(--space-sm) 0; border-top: 1px solid var(--border);
  }
  .learning-row:first-child { border-top: 0; }
  .learning-row--decided { opacity: 0.75; }
  .learning-row__statement {
    display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical;
    overflow: hidden; margin: 0; padding: 0; border: 0; background: none;
    color: var(--ink); font: inherit; line-height: 1.4; text-align: left; cursor: pointer;
  }
  .learning-row__statement--open { display: block; overflow: visible; }
  .learning-row__statement-text { margin: 0; color: var(--ink); line-height: 1.4; }
  .learning-row__foot {
    display: flex; flex-wrap: wrap; align-items: center;
    justify-content: space-between; gap: var(--space-xs);
  }
  .learning-row__byline { color: var(--muted); font-size: 0.8rem; }
  .learning-row__actions { display: flex; gap: var(--space-2xs); margin-left: auto; }
  .learning-row__quote {
    margin: 0; padding-left: var(--space-sm); border-left: 2px solid var(--border);
    color: var(--muted); font-size: 0.85rem;
  }
  .learnings-panel__history-toggle {
    align-self: flex-start; padding: 0; border: 0; background: none;
    color: var(--muted); font: inherit; font-size: 0.85rem; cursor: pointer;
  }
  .learnings-panel__history-toggle:hover { color: var(--accent); }
</style>
