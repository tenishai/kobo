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
  // The teach box, shut on arrival.
  let teachOpen = $state(false);
  let errorText = $state("");
  let openId = $state<number | null>(null);      // row whose quote is showing
  let editingId = $state<number | null>(null);   // row being reworded
  let draft = $state("");
  let confirmId = $state<number | null>(null);   // flagged row asking for a second yes
  let teachText = $state("");
  let teachNote = $state("");
  let confirming = $state<"approve" | "deny" | null>(null);

  // The server's bulk route silently truncates at BULK_MAX ids -- decideAll
  // must send no more than that, and the confirm prompt must say so rather
  // than promise a count it can't deliver.
  const BULK_MAX = 50;

  const all = $derived(learnings.data?.learnings ?? []);
  // Issue #142: a nightly pass that keeps failing (an old runtime refusing
  // the task, say) would otherwise look like a quiet week.
  const nightly = $derived(
    (learnings.data?.system_tasks ?? []).find((t) => t.task === "learnings" && t.failing),
  );
  const pending = $derived(all.filter((l: Learning) => l.status === "pending"));
  const bulkIds = $derived(pending.slice(0, BULK_MAX).map((l: Learning) => l.id));
  const history = $derived(all.filter((l: Learning) => l.status !== "pending"));
  const remembered = $derived(history.filter((l: Learning) => l.status === "approved").length);
  // Only a denied row is "Skipped" -- an expired one is counted and shown
  // separately, never folded into skipped just because it isn't remembered.
  const skipped = $derived(history.filter((l: Learning) => l.status === "denied").length);
  const expiredCount = $derived(history.filter((l: Learning) => l.status === "expired").length);

  // A decide/bulk-decide success (or a plain refetch) can empty `pending`
  // out from under an open confirm prompt -- reset it so a stale "Remember
  // all 2?" never lingers over a list that's no longer there to act on.
  $effect(() => {
    if (pending.length === 0) confirming = null;
  });

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

  // Issue #143: a row nobody can be traced to, or one about access, is
  // remembered only on a second, explicit yes. Bulk "Remember all" leaves
  // such rows here for one-by-one review (the room refuses them in bulk).
  const CONCERN_LABELS: Record<string, string> = {
    unattributed: "Nobody to attribute this to",
    access_change: "About access",
  };

  function concernsOf(l: Learning): string[] {
    return l.concerns ?? [];
  }

  const flaggedCount = $derived(
    pending.slice(0, BULK_MAX).filter((l: Learning) => concernsOf(l).length > 0).length,
  );

  function startEdit(l: Learning): void {
    editingId = l.id;
    draft = l.content;
  }

  function remember(l: Learning): void {
    errorText = "";
    const flagged = concernsOf(l).length > 0;
    if (flagged && confirmId !== l.id) {
      confirmId = l.id;
      return;
    }
    const edited = editingId === l.id && draft.trim() !== l.content;
    decide.mutate(
      {
        id: l.id,
        action: "approve",
        ...(edited ? { content: draft.trim() } : {}),
        ...(flagged ? { confirm: true } : {}),
      },
      {
        // Guarded: deciding row B while row A is mid-edit must not clear
        // row A's own edit state out from under it.
        onSuccess: () => {
          if (editingId === l.id) editingId = null;
          if (confirmId === l.id) confirmId = null;
        },
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
      { ids: bulkIds, action },
      { onError: (e: unknown) => (errorText = errorMessage(e, "Couldn't finish that. Try again.")) },
    );
  }

  function bulkConfirmText(action: "approve" | "deny"): string {
    const verb = action === "approve" ? "Remember" : "Skip";
    const question =
      pending.length <= BULK_MAX
        ? `${verb} all ${pending.length}?`
        : `${verb} the first ${BULK_MAX} of ${pending.length}?`;
    if (action !== "approve" || flaggedCount === 0) return question;
    return `${question} ${flaggedCount} flagged ${flaggedCount === 1 ? "stays" : "stay"} for you to check one by one.`;
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

<section class="panel" aria-labelledby="learnings-panel-heading">
  <div class="panel__header">
    <h2 id="learnings-panel-heading" class="panel__heading">Learnings</h2>
  </div>

  {#if canDecide}
    <!-- The teach box used to stand open above everything. It is a
         disclosure now: the panel is here to show what the agent picked
         up, and typing something new is the occasional errand. -->
    <div class="panel__toolbar">
      <div class="panel__tools">
        <button
          type="button"
          class="btn"
          aria-expanded={teachOpen}
          onclick={() => (teachOpen = !teachOpen)}
        >
          Teach something
        </button>
      </div>
    </div>

    {#if teachOpen}
      <form class="panel__drawer learnings-teach" onsubmit={submitTeach}>
        <div class="learnings-teach__row">
          <input
            class="learnings-teach__input"
            type="text"
            bind:value={teachText}
            placeholder={`Teach ${agentName} something...`}
            aria-label={`Teach ${agentName} something`}
            maxlength="400"
            disabled={frozen || teach.isPending}
          />
          <button
            class="btn btn--primary"
            type="submit"
            disabled={frozen || teach.isPending || !teachText.trim()}
          >
            Add
          </button>
        </div>
        {#if teachNote}<p class="panel__note" role="status">{teachNote}</p>{/if}
      </form>
    {/if}
  {/if}

  {#if errorText}
    <p class="panel__note panel__note--fail" role="alert">{errorText}</p>
  {/if}

  {#if learnings.isPending}
    <p class="panel__note">Loading...</p>
  {:else if learnings.isError}
    <ErrorState
      message={errorMessage(learnings.error, "Couldn't load this agent's learnings.")}
      onretry={() => learnings.refetch()}
    />
  {:else}
    {#if nightly}
      <p class="panel__note panel__note--fail" role="status">
        The nightly pass has failed {nightly.consecutive_failures} times in a row, so
        {agentName} hasn't proposed anything new.{nightly.last_error
          ? ` Last error: ${nightly.last_error}`
          : ""}
      </p>
    {/if}
    <table class="panel__table">
      <thead>
        <tr>
          <th scope="col">What it picked up</th>
          <th scope="col" class="learnings-panel__col-byline">Who said it</th>
          <th scope="col" class="learnings-panel__col-action">
            <span class="panel__sr">Decide</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {#if pending.length === 0}
          <tr>
            <td class="panel__empty" colspan="3">
              Nothing new. When someone tells {agentName} something worth keeping, it shows up here.
            </td>
          </tr>
        {/if}
        {#each pending as l (l.id)}
          <tr class="learning-row">
            <td>
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
            </td>
            <td>
              <span class="learning-row__byline">{byline(l)}</span>
              {#each concernsOf(l) as concern (concern)}
                <span class="learning-row__concern">{CONCERN_LABELS[concern] ?? concern}</span>
              {/each}
            </td>
            <td class="panel__cell--right">
              {#if canDecide}
                <span class="learning-row__actions">
                  {#if editingId === l.id}
                    <button class="btn btn--quiet" type="button"
                      onclick={() => (editingId = null)}>Cancel</button>
                  {:else}
                    <button class="btn btn--quiet" type="button"
                      disabled={frozen || deciding} aria-label="Fix the wording" title="Fix the wording"
                      onclick={() => startEdit(l)}>✎</button>
                    <button class="btn btn--quiet" type="button"
                      disabled={frozen || deciding} onclick={() => skip(l.id)}>Skip</button>
                  {/if}
                  <button class="btn learnings-panel__emphasis" type="button"
                    disabled={frozen || deciding || (editingId === l.id && !draft.trim())}
                    onclick={() => remember(l)}
                  >{confirmId === l.id ? "Yes, remember it" : "Remember"}</button>
                </span>
              {/if}
            </td>
          </tr>
          {#if openId === l.id && l.evidence}
            <tr>
              <td class="learning-row__quote-cell" colspan="3">
                <blockquote class="learning-row__quote">{l.evidence}</blockquote>
              </td>
            </tr>
          {/if}
        {/each}
      </tbody>
    </table>

    {#if pending.length > 0}
      <div class="panel__footer">
        <p class="panel__status">
          {agentName} picked up {pending.length} {pending.length === 1 ? "thing" : "things"}
        </p>
        {#if canDecide}
          <div class="panel__actions">
            {#if confirming}
              <span class="panel__status" role="status">
                {bulkConfirmText(confirming)}
              </span>
              <button class="btn btn--quiet" type="button"
                disabled={frozen || deciding} onclick={() => (confirming = null)}>Cancel</button>
              <button class="btn learnings-panel__emphasis" type="button"
                disabled={frozen || deciding}
                onclick={() => { const action = confirming; confirming = null; if (action) decideAll(action); }}
              >{confirming === "approve" ? "Yes, remember" : "Yes, skip"}</button>
            {:else}
              <button class="btn btn--quiet" type="button"
                disabled={frozen || deciding} onclick={() => (confirming = "deny")}>Skip all</button>
              <button class="btn learnings-panel__emphasis" type="button"
                disabled={frozen || deciding} onclick={() => (confirming = "approve")}>Remember all</button>
            {/if}
          </div>
        {/if}
      </div>
    {/if}
  {/if}

  {#if history.length > 0}
    <button class="learnings-panel__history-toggle" type="button"
      aria-expanded={showHistory} onclick={() => (showHistory = !showHistory)}>
      Remembered {remembered} · Skipped {skipped}{expiredCount > 0 ? ` · Expired ${expiredCount}` : ""}
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
  .learnings-panel__emphasis {
    border-color: var(--accent);
    color: var(--accent);
  }

  .learnings-panel__col-byline {
    width: 14rem;
  }

  .learnings-panel__col-action {
    width: 16rem;
  }

  .learnings-teach {
    width: 100%;
  }

  .learnings-teach__row {
    display: flex;
    gap: var(--space-xs);
    width: 100%;
  }

  .learnings-teach__input,
  .learning-row__edit {
    flex: 1;
    min-width: 0;
    width: 100%;
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: var(--ink);
    font: inherit;
  }

  .learnings-teach__input:focus,
  .learning-row__edit:focus { outline: none; border-color: var(--accent); }

  .learnings-list { display: flex; flex-direction: column; margin: 0; padding: 0; list-style: none; }
  .learning-row--decided {
    display: flex; flex-direction: column; gap: var(--space-2xs);
    padding: var(--space-sm) 0; border-top: 1px solid var(--border);
    opacity: 0.75;
  }
  .learning-row--decided:first-child { border-top: 0; }
  .learning-row__statement {
    display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical;
    overflow: hidden; width: 100%; margin: 0; padding: 0; border: 0; background: none;
    color: var(--ink); font: inherit; font-size: 0.85rem; line-height: 1.4;
    text-align: left; cursor: pointer;
  }
  .learning-row__statement--open { display: block; overflow: visible; white-space: pre-wrap; }
  .learning-row__statement-text { margin: 0; color: var(--ink); line-height: 1.4; }
  .learning-row__byline { color: var(--muted); font-size: 0.8rem; }
  .learning-row__concern {
    display: inline-block;
    margin-left: var(--space-2xs);
    color: var(--warn);
    font-size: 0.75rem;
  }
  .learning-row__actions { display: flex; align-items: center; justify-content: flex-end; gap: var(--space-2xs); }
  .learning-row__quote-cell { padding-bottom: var(--space-xs); }
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
