<script lang="ts">
  import { ArrowsClockwise, SealCheck } from "phosphor-svelte";
  // Permission requests waiting on a reviewer: who asked for what, for which
  // agent, by tier. Tick rows, Approve or Deny (a reason is required). bab
  // decides what the signed-in person may approve (the admin team, or the
  // security team for the destroy tier) and says so per permission.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import Modal from "../Modal.svelte";
  import { errorMessage, type AccessRequest } from "../../lib/api";
  import { serverDate, when } from "../../lib/time";
  import { toast } from "../../lib/toast.svelte";
  import { accessLeasesQuery, accessRequestsQuery, decideAccessMutation, syncAccessMutation } from "../../lib/queries";

  const queryClient = useQueryClient();
  let scope = $state<"pending" | "all">("pending");
  const requests = createQuery(() => (accessRequestsQuery(scope)));
  // Credentials out in the room right now, refreshed often: an hour is short.
  const leases = createQuery(() => ({ ...accessLeasesQuery(), refetchInterval: 30_000 }));
  const decide = createMutation(() => (decideAccessMutation(queryClient)));
  const sync = createMutation(() => (syncAccessMutation(queryClient)));

  let ticked = $state<number[]>([]);
  let denying = $state(false);
  let reason = $state("");

  const rows = $derived(requests.data ?? []);
  const pendingIds = $derived(rows.filter((r) => r.status === "pending").map((r) => r.id));

  function toggle(id: number): void {
    ticked = ticked.includes(id) ? ticked.filter((x) => x !== id) : [...ticked, id];
  }
  function toggleAll(): void {
    ticked = ticked.length === pendingIds.length ? [] : [...pendingIds];
  }

  function approve(): void {
    const ids = [...ticked];
    decide.mutate(
      { ids, verdict: "approve" },
      {
        onSuccess: (results) => {
          const granted = results.filter((r) => r.status === "approved").length;
          const refused = results.flatMap((r) => Object.values(r.outcome ?? {})).filter((o) => o.startsWith("refused")).length;
          toast.success(`${granted} approved`, refused ? `${refused} permission${refused === 1 ? "" : "s"} refused by the ceiling; open a row to see which.` : "The agents may borrow credentials for these now.");
          ticked = [];
        },
        onError: (err: unknown) => toast.error("Could not decide", errorMessage(err, "Try again.")),
      },
    );
  }

  function deny(event: SubmitEvent): void {
    event.preventDefault();
    const ids = [...ticked];
    decide.mutate(
      { ids, verdict: "deny", reason: reason.trim() },
      {
        onSuccess: () => {
          toast.success(`${ids.length} denied`, "The owners see the reason on the agent's Permissions section.");
          ticked = [];
          denying = false;
          reason = "";
        },
        onError: (err: unknown) => toast.error("Could not deny", errorMessage(err, "Try again.")),
      },
    );
  }

  function handleSync(): void {
    sync.mutate(undefined, {
      onSuccess: (r) => toast.success("Agents synced", `${r.agents} agent${r.agents === 1 ? "" : "s"} known to the permission service.`),
      onError: (err: unknown) => toast.error("Could not sync", errorMessage(err, "Try again.")),
    });
  }

  function tiers(r: AccessRequest): string {
    return Object.entries(r.tiers).map(([t, n]) => `${n} ${t}`).join(", ");
  }
  /** A request to ask another agent (issue #310) names the target agent as its
   * account and one permission, shown as the word it means. */
  function permLabel(p: string): string {
    return p === "a2a_ask" ? "ask" : p;
  }
  function left(iso: string): string {
    const min = Math.max(0, Math.round((serverDate(iso).getTime() - Date.now()) / 60_000));
    return min >= 60 ? "about an hour" : `${min} min`;
  }
</script>

<section class="panel page" aria-labelledby="review-heading">
  <div class="review__head">
    <div class="page__header">
      <h2 id="review-heading" class="page__title">Review</h2>
      <p class="page__lead">Permissions and repositories agents' owners asked for. Approve and the agent may borrow a credential for them; deny with a reason.</p>
    </div>
    <!-- A page-level action, so it sits with the title, not with the list. -->
    <button type="button" class="btn review__sync" disabled={sync.isPending} onclick={handleSync} title="Tell the permission service which agents exist now">
      <ArrowsClockwise size={14} aria-hidden="true" />
      Sync agents
    </button>
  </div>

  {#if leases.data && leases.data.length > 0}
    <h3 class="panel__subheading">Borrowed now</h3>
    <div class="table-card"><table class="panel__table">
      <thead><tr><th>Agent</th><th>Account</th><th>Permissions</th><th>Task</th><th>Issued</th><th>Ends in</th></tr></thead>
      <tbody>
        {#each leases.data as l (l.id)}
          <tr>
            <td><span class="panel__name">{l.agent}</span></td>
            <td>{l.integration}</td>
            <td class="quiet">{l.permissions.join(", ")}</td>
            <td class="quiet">{l.task ?? ""}</td>
            <td class="quiet nowrap">{when(l.issued_at)}</td>
            <td class="nowrap">{left(l.expires_at)}</td>
          </tr>
        {/each}
      </tbody>
    </table></div>
    <h3 class="panel__subheading">Waiting for a decision</h3>
  {/if}

  {#if requests.isPending}
    <p class="panel__note">Reading the requests…</p>
  {:else if requests.isError}
    <p class="panel__note">{errorMessage(requests.error, "Access is off for this room.")}</p>
  {:else}
    <!-- Which requests to show, as two tabs; and the decision buttons only
         once something is ticked, so an empty queue is not a row of dead
         controls. -->
    <div class="review__bar">
      <div class="review__tabs" role="tablist" aria-label="Show">
        <button type="button" role="tab" class="review__tab" aria-selected={scope === "pending"} onclick={() => (scope = "pending")}>
          Waiting <span class="review__count">{pendingIds.length}</span>
        </button>
        <button type="button" role="tab" class="review__tab" aria-selected={scope === "all"} onclick={() => (scope = "all")}>
          Everything
        </button>
      </div>
      {#if ticked.length > 0}
        <div class="review__selection" role="group" aria-label="Decide the selected requests">
          <span class="review__selected">{ticked.length} selected</span>
          <button type="button" class="btn btn--danger" disabled={decide.isPending} onclick={() => (denying = true)}>Deny</button>
          <button type="button" class="btn btn--primary" disabled={decide.isPending} onclick={approve}>Approve</button>
        </div>
      {/if}
    </div>
    {#if rows.length === 0}
      <div class="review__empty">
        <SealCheck size={28} aria-hidden="true" />
        <p class="review__empty-title">{scope === "pending" ? "Nothing waiting" : "No requests yet"}</p>
        <p class="review__empty-text">When an agent's owner asks for a permission, it shows up here for a decision.</p>
      </div>
    {:else}
      <div class="table-card"><table class="panel__table">
        <thead><tr>
          <th><input type="checkbox" aria-label="Select all waiting" checked={pendingIds.length > 0 && ticked.length === pendingIds.length} disabled={pendingIds.length === 0} onchange={toggleAll} /></th>
          <th>Agent</th><th>Owner</th><th>Account</th><th>Permissions</th><th>Asked</th><th>Status</th>
        </tr></thead>
        <tbody>
          {#each rows as r (r.id)}
            <tr>
              <td><input type="checkbox" aria-label={`Select request ${r.id}`} disabled={r.status !== "pending"} checked={ticked.includes(r.id)} onchange={() => toggle(r.id)} /></td>
              <td><span class="panel__name">{r.agent}</span>{#if r.team}<span class="quiet"> · {r.team}</span>{/if}</td>
              <td class="quiet">{r.owner ?? r.requested_by}</td>
              <td>{r.integration}</td>
              <td>
                {#if r.kind === "repositories"}
                  <!-- An owner's pick on a set the agent already holds a level on: what it
                       would add (and take away), and the levels the new ones would be reached with. -->
                  {@const adds = r.proposal?.added ?? []}
                  {@const drops = r.proposal?.removed ?? []}
                  <details open>
                    {#if r.proposal?.all}
                      <!-- Every repository the room's GitHub app can see, for Read alone. -->
                      <summary>Every repository, Read only{#if r.outcome?.["every repository"]}<span class="quiet"> · {r.outcome["every repository"]}</span>{/if}</summary>
                    {:else}
                    <summary>Add {adds.length} repositor{adds.length === 1 ? "y" : "ies"}{drops.length > 0 ? `, remove ${drops.length}` : ""}</summary>
                    {/if}
                    <ul class="perms">
                      {#each adds as repo (repo)}<li><code>{repo}</code>{#if r.outcome?.[repo]}<span class="quiet"> · {r.outcome[repo]}</span>{/if}</li>{/each}
                      {#each drops as repo (repo)}<li><code>{repo}</code><span class="quiet"> · {r.outcome?.[repo] ?? "to remove"}</span></li>{/each}
                    </ul>
                    <p class="reach">Would be reached with {r.permissions.map(permLabel).join(", ")} ({tiers(r)})</p>
                  </details>
                {:else}
                  <details>
                    <summary>{r.permissions.length} ({tiers(r)})</summary>
                    <ul class="perms">
                      {#each r.permissions as p (p)}
                        <li><span class="perm">{permLabel(p)}</span>{#if r.outcome[p]}<span class="quiet"> · {r.outcome[p]}</span>{/if}</li>
                      {/each}
                    </ul>
                    {#if r.note}<p class="quiet">“{r.note}”</p>{/if}
                  </details>
                {/if}
                {#if r.all_repositories}
                  <p class="reach">On every repository the room's GitHub app can see, Read only</p>
                {:else if r.repositories && r.repositories.length > 0}
                  <!-- A GitHub request (issue #365) is for a repository set: the reviewer sees which. -->
                  <details class="repos">
                    <summary>{r.repositories.length} repositor{r.repositories.length === 1 ? "y" : "ies"}{r.kind === "repositories" ? " now" : ""}</summary>
                    <ul class="perms">
                      {#each r.repositories as repo (repo)}<li><code>{repo}</code></li>{/each}
                    </ul>
                  </details>
                {/if}
              </td>
              <td class="quiet nowrap">{when(r.created_at)}</td>
              <td>
                <span class="status status--{r.status === 'pending' ? 'warn' : r.status === 'approved' ? 'on' : r.status === 'withdrawn' ? 'off' : 'fail'}"><span class="status__dot" aria-hidden="true"></span>{r.status}</span>
                {#if r.reason}<span class="quiet detail">{r.reason}</span>{/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table></div>
    {/if}
  {/if}
</section>

<Modal open={denying} title={`Deny ${ticked.length} request${ticked.length === 1 ? "" : "s"}`} onclose={() => (denying = false)}>
  <form id="deny-form" class="form" onsubmit={deny}>
    <label class="field">
      <span>Reason (the owner reads this)</span>
      <textarea class="field__input" rows="3" bind:value={reason} required></textarea>
    </label>
  </form>
  {#snippet footer()}
    <button type="button" class="btn" onclick={() => (denying = false)}>Cancel</button>
    <button type="submit" form="deny-form" class="btn btn--primary" disabled={decide.isPending || reason.trim() === ""}>Deny</button>
  {/snippet}
</Modal>

<style>
  .review__head {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-md);
  }

  .review__head > :global(.page__header) {
    flex: 1 1 24rem;
    min-width: 0;
  }

  .review__sync {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }

  .review__bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
    margin-top: var(--space-sm);
    padding-bottom: var(--space-sm);
    border-bottom: 1px solid var(--border);
  }

  .review__tabs {
    display: inline-flex;
    gap: 2px;
    padding: 3px;
    border-radius: calc(var(--radius-sm) + 3px);
    background: var(--accent-soft);
  }

  .review__tab {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    height: 2rem;
    padding: 0 var(--space-sm);
    border: none;
    border-radius: var(--radius-sm);
    background: none;
    color: var(--muted);
    font: inherit;
    font-size: 0.85rem;
    font-weight: 500;
    cursor: pointer;
  }

  .review__tab[aria-selected="true"] {
    background: var(--panel);
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06), 0 0 0 1px var(--border);
    color: var(--ink);
  }

  .review__count {
    display: inline-flex;
    min-width: 1.15rem;
    height: 1.15rem;
    align-items: center;
    justify-content: center;
    padding: 0 0.3rem;
    border-radius: 999px;
    background: var(--border);
    color: var(--ink);
    font-family: var(--font-display-mono);
    font-size: 0.65rem;
  }

  .review__selection {
    display: inline-flex;
    align-items: center;
    gap: var(--space-xs);
  }

  .review__selected {
    margin-right: var(--space-xs);
    color: var(--muted);
    font-size: 0.85rem;
  }

  .review__empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-2xs);
    padding: var(--space-2xl) var(--space-lg);
    color: var(--muted);
    text-align: center;
  }

  .review__empty-title {
    margin: var(--space-xs) 0 0;
    color: var(--ink);
    font-size: 0.95rem;
    font-weight: 600;
  }

  .review__empty-text {
    max-width: 26rem;
    margin: 0;
    font-size: 0.85rem;
  }

  .perm { font-family: var(--font-mono); font-size: 0.85rem; }
  .perms { margin: var(--space-2xs) 0 0; padding-left: 1rem; }
  .reach { margin: var(--space-2xs) 0 0; color: var(--ink); font-size: 0.85rem; }
  .quiet { color: var(--muted); font-size: 0.85rem; }
  .nowrap { white-space: nowrap; }
  .detail { display: block; font-size: 0.78rem; }
</style>
