<script lang="ts">
  // Repo access (agent PR broker, 2026-08-27 spec): the Access tab's
  // GitHub section. The owner requests repositories in batch (one
  // owner/name per line); the operator set approves or revokes -- also in
  // batch, per the ruling -- and approved rows are exactly what the
  // broker pins this agent's tokens to. The decide buttons render for
  // everyone and let the SERVER's operator gate answer: the room's
  // enforcement lives in the route, never in what the UI hides.
  //
  // Shaped like every other panel now (shared .panel/.btn classes in
  // app.css): one card, a heading, one table, and the request form behind
  // a toolbar button instead of standing open under the list. It sits
  // beside the access panel rather than inside it because AgentDetail
  // mounts the two as siblings.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import {
    decideRepoAccess,
    errorMessage,
    listRepoAccess,
    requestRepoAccess,
  } from "../lib/api";
  import { ApiError } from "../lib/api";
  import ErrorState from "./ErrorState.svelte";
  import Skeleton from "./Skeleton.svelte";

  let { agentName, frozen = false }: { agentName: string; frozen?: boolean } = $props();

  const queryClient = useQueryClient();
  const REPO_SHAPE = /^[\w.-]+\/[\w.-]+$/;

  const rows = createQuery(() => ({
    queryKey: ["repo-access", agentName],
    queryFn: async () => {
      const all = await listRepoAccess();
      return all.results.filter((row) => row.agent === agentName);
    },
  }));

  // 503 from the list means the room has no pull request app configured
  // (#270): a state of the room, not a failure of this page, so the card
  // says so in one line and offers nothing to retry.
  const brokerOff = $derived(
    rows.isError && rows.error instanceof ApiError && rows.error.status === 503,
  );

  let draft = $state("");
  let feedback = $state<string | null>(null);
  // Shut on arrival: asking for a repository is an occasional errand, not
  // the reason this panel is on screen.
  let requestOpen = $state(false);

  const parsed = $derived(
    draft
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean),
  );
  const invalid = $derived(parsed.filter((repo) => !REPO_SHAPE.test(repo)));

  const request = createMutation(() => ({
    mutationFn: () => requestRepoAccess(agentName, parsed),
    onSuccess: () => {
      draft = "";
      feedback = "Requested. A room operator decides from here.";
      queryClient.invalidateQueries({ queryKey: ["repo-access", agentName] });
    },
    onError: (error: unknown) => {
      feedback = errorMessage(error, "That did not go through. Try again.");
    },
  }));

  const decide = createMutation(() => ({
    mutationFn: ({ id, decision }: { id: number; decision: "approved" | "revoked" }) =>
      decideRepoAccess([id], decision),
    onSuccess: () => {
      feedback = null;
      queryClient.invalidateQueries({ queryKey: ["repo-access", agentName] });
    },
    onError: (error: unknown) => {
      feedback = errorMessage(error, "That did not go through. Try again.");
    },
  }));
</script>

<section class="panel" aria-labelledby="repo-access-heading">
  <div class="panel__header">
    <h2 id="repo-access-heading" class="panel__heading">Repositories</h2>
    <p class="panel__lead">
      Approved repositories are the only places this agent's github.open_pr
      tool can open pull requests. Requests are decided by a room operator.
    </p>
  </div>

  <!-- Usable while frozen: it opens a form, it changes nothing. The
       textarea and the submit inside it are the ones that go dead. -->
  {#if !brokerOff}
    <div class="panel__toolbar">
      <div class="panel__tools">
        <button
          type="button"
          class="btn"
          aria-expanded={requestOpen}
          onclick={() => (requestOpen = !requestOpen)}
        >
          Request repositories
        </button>
      </div>
    </div>
  {/if}

  {#if requestOpen}
    <div class="panel__drawer repo-access__drawer">
      <label class="repo-access__label" for="repo-access-draft">
        One owner/name per line
      </label>
      <textarea
        id="repo-access-draft"
        class="repo-access__draft"
        rows="3"
        placeholder="your-org/example-repo"
        bind:value={draft}
        disabled={frozen}
      ></textarea>
      {#if invalid.length > 0}
        <p class="panel__note panel__note--fail">
          Not owner/name shaped: {invalid.join(", ")}
        </p>
      {/if}
      <button
        type="button"
        class="btn btn--primary"
        disabled={frozen || parsed.length === 0 || invalid.length > 0 || request.isPending}
        onclick={() => request.mutate()}
      >
        {request.isPending ? "Requesting…" : "Request access"}
      </button>
    </div>
  {/if}

  {#if rows.isPending}
    <Skeleton rows={2} />
  {:else if brokerOff}
    <p class="panel__note" role="status">
      This room has no GitHub app for pull requests, so agents can't open them. A room operator
      adds it under <a href="#/admin/integrations">Admin &gt; Integrations</a>.
    </p>
  {:else if rows.isError}
    <ErrorState
      message={errorMessage(rows.error, "Repo access could not be loaded.")}
      onretry={() => rows.refetch()}
    />
  {:else}
    <table class="panel__table">
      <thead>
        <tr>
          <th scope="col">Repository</th>
          <th scope="col" class="repo-access__col-state">State</th>
          <th scope="col" class="repo-access__col-action">
            <span class="panel__sr">Decide</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {#if (rows.data ?? []).length === 0}
          <tr>
            <td class="panel__empty" colspan="3">No repositories requested yet.</td>
          </tr>
        {/if}
        {#each rows.data ?? [] as row (row.id)}
          <tr class="repo-access__row">
            <td><code class="panel__name">{row.repository}</code></td>
            <td>
              <span class="repo-access__state repo-access__state--{row.status}">
                {row.status}
              </span>
            </td>
            <td class="panel__cell--right">
              <div class="repo-access__actions">
                {#if row.status === "requested"}
                  <button
                    type="button"
                    class="btn"
                    disabled={frozen || decide.isPending}
                    aria-label={`Approve ${row.repository}`}
                    onclick={() => decide.mutate({ id: row.id, decision: "approved" })}
                  >
                    Approve
                  </button>
                {/if}
                {#if row.status !== "revoked"}
                  <button
                    type="button"
                    class="btn btn--quiet btn--danger"
                    disabled={frozen || decide.isPending}
                    aria-label={`Revoke ${row.repository}`}
                    onclick={() => decide.mutate({ id: row.id, decision: "revoked" })}
                  >
                    Revoke
                  </button>
                {/if}
              </div>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

  {#if feedback}
    <p class="panel__note" role="status">{feedback}</p>
  {/if}
</section>

<style>
  .repo-access__drawer {
    width: 100%;
  }

  .repo-access__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.85rem;
  }

  .repo-access__draft {
    display: block;
    width: 100%;
    padding: var(--space-2xs) var(--space-xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.8rem;
    resize: vertical;
  }

  .repo-access__state {
    padding: 0.1rem var(--space-xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.7rem;
    text-transform: capitalize;
    white-space: nowrap;
  }

  .repo-access__state--approved {
    border-color: var(--ok);
    color: var(--ok);
  }

  .repo-access__state--revoked {
    opacity: 0.6;
  }

  .repo-access__actions {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: var(--space-xs);
  }

  .repo-access__col-state {
    width: 8rem;
  }

  .repo-access__col-action {
    width: 12rem;
  }
</style>
