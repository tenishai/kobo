<script lang="ts">
  // Repo access (agent PR broker, 2026-08-27 spec): the Access tab's
  // GitHub section. The owner requests repositories in batch (one
  // owner/name per line); the operator set approves or revokes -- also in
  // batch, per the ruling -- and approved rows are exactly what the
  // broker pins this agent's tokens to. The decide buttons render for
  // everyone and let the SERVER's operator gate answer: the room's
  // enforcement lives in the route, never in what the UI hides.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import {
    decideRepoAccess,
    errorMessage,
    listRepoAccess,
    requestRepoAccess,
  } from "../lib/api";

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

  let draft = $state("");
  let feedback = $state<string | null>(null);

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

<section class="repo-access">
  <h3 class="repo-access__title">GitHub repositories</h3>
  <p class="repo-access__hint">
    Approved repositories are the only places this agent's github.open_pr
    tool can open pull requests. Requests are decided by a room operator.
  </p>

  {#if rows.isPending}
    <p class="repo-access__note">Loading…</p>
  {:else if rows.isError}
    <p class="repo-access__note">{errorMessage(rows.error, "Repo access could not be loaded.")}</p>
  {:else if rows.data}
    {#if rows.data.length === 0}
      <p class="repo-access__note">No repositories requested yet.</p>
    {:else}
      <ul class="repo-access__list">
        {#each rows.data as row (row.id)}
          <li class="repo-access__row">
            <code>{row.repository}</code>
            <span class="repo-access__status repo-access__status--{row.status}">{row.status}</span>
            {#if row.status === "requested"}
              <button
                type="button"
                disabled={decide.isPending}
                onclick={() => decide.mutate({ id: row.id, decision: "approved" })}
              >Approve</button>
            {/if}
            {#if row.status !== "revoked"}
              <button
                type="button"
                disabled={decide.isPending}
                onclick={() => decide.mutate({ id: row.id, decision: "revoked" })}
              >Revoke</button>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  {/if}

  {#if !frozen}
    <label class="repo-access__label" for="repo-access-draft">
      Request repositories (one owner/name per line)
    </label>
    <textarea
      id="repo-access-draft"
      class="repo-access__draft"
      rows="3"
      placeholder="your-org/example-repo"
      bind:value={draft}
    ></textarea>
    {#if invalid.length > 0}
      <p class="repo-access__note">Not owner/name shaped: {invalid.join(", ")}</p>
    {/if}
    <button
      type="button"
      class="repo-access__submit"
      disabled={parsed.length === 0 || invalid.length > 0 || request.isPending}
      onclick={() => request.mutate()}
    >
      {request.isPending ? "Requesting…" : "Request access"}
    </button>
  {/if}

  {#if feedback}
    <p class="repo-access__note" role="status">{feedback}</p>
  {/if}
</section>

<style>
  .repo-access {
    margin-top: 1.5rem;
  }

  .repo-access__title {
    font-size: 1rem;
    margin: 0 0 0.25rem;
  }

  .repo-access__hint,
  .repo-access__note {
    font-size: 0.85rem;
    opacity: 0.75;
    margin: 0.25rem 0;
  }

  .repo-access__list {
    list-style: none;
    padding: 0;
    margin: 0.5rem 0;
  }

  .repo-access__row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.25rem 0;
    font-size: 0.9rem;
  }

  .repo-access__row button {
    font: inherit;
    font-size: 0.75rem;
    background: none;
    border: 1px solid var(--border, #d0c8bc);
    border-radius: 5px;
    padding: 0.15rem 0.5rem;
    cursor: pointer;
  }

  .repo-access__status {
    font-size: 0.75rem;
    border-radius: 999px;
    padding: 0.1rem 0.5rem;
    background: var(--surface-sunken, #f4efe8);
  }

  .repo-access__status--approved {
    background: #dcefdc;
  }

  .repo-access__status--revoked {
    opacity: 0.6;
  }

  .repo-access__draft {
    display: block;
    width: 100%;
    font: inherit;
    font-size: 0.85rem;
    margin-top: 0.25rem;
  }

  .repo-access__submit {
    margin-top: 0.5rem;
    font: inherit;
    font-size: 0.85rem;
    border: 1px solid var(--border, #d0c8bc);
    border-radius: 6px;
    padding: 0.35rem 0.75rem;
    cursor: pointer;
    background: none;
  }
</style>
