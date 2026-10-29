<script lang="ts">
  // Permission requests waiting on a reviewer: who asked for what, for which
  // agent, by tier. Tick rows, Approve or Deny (a reason is required). bab
  // decides what the signed-in person may approve (the admin team, or the
  // security team for the destroy tier) and says so per permission.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import Modal from "../Modal.svelte";
  import { errorMessage, type AccessRequest } from "../../lib/api";
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
  function when(iso: string): string {
    return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function left(iso: string): string {
    const min = Math.max(0, Math.round((new Date(iso).getTime() - Date.now()) / 60_000));
    return min >= 60 ? "about an hour" : `${min} min`;
  }
</script>

<section class="panel page" aria-labelledby="review-heading">
  <div class="page__header">
    <h2 id="review-heading" class="page__title">Review</h2>
    <p class="page__lead">Permissions agents' owners asked for. Approve and the agent may borrow a credential for them; deny with a reason.</p>
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
    <div class="toolbar">
      <label class="pick"><span>Show</span>
        <select bind:value={scope}><option value="pending">waiting</option><option value="all">everything</option></select>
      </label>
      <span class="count">{pendingIds.length} waiting</span>
      <button type="button" class="btn btn--primary" disabled={ticked.length === 0 || decide.isPending} onclick={approve}>Approve {ticked.length || ""}</button>
      <button type="button" class="btn" disabled={ticked.length === 0 || decide.isPending} onclick={() => (denying = true)}>Deny…</button>
      <button type="button" class="btn btn--quiet" disabled={sync.isPending} onclick={handleSync} title="Tell the permission service which agents exist now">Sync agents</button>
    </div>
    {#if rows.length === 0}
      <p class="panel__empty">{scope === "pending" ? "Nothing waiting." : "No requests yet."}</p>
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
                <details>
                  <summary>{r.permissions.length} ({tiers(r)})</summary>
                  <ul class="perms">
                    {#each r.permissions as p (p)}
                      <li><span class="perm">{p}</span>{#if r.outcome[p]}<span class="quiet"> · {r.outcome[p]}</span>{/if}</li>
                    {/each}
                  </ul>
                  {#if r.note}<p class="quiet">“{r.note}”</p>{/if}
                </details>
              </td>
              <td class="quiet nowrap">{when(r.created_at)}</td>
              <td>
                <span class="status status--{r.status === 'pending' ? 'warn' : r.status === 'approved' ? 'on' : 'fail'}"><span class="status__dot" aria-hidden="true"></span>{r.status}</span>
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
  .toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-sm); margin: 0 0 var(--space-sm); }
  .pick { display: inline-flex; align-items: center; gap: var(--space-xs); color: var(--muted); font-size: 0.9rem; }
  .pick select { font: inherit; }
  .count { margin-right: auto; color: var(--muted); font-size: 0.9rem; }
  .perm { font-family: var(--font-mono); font-size: 0.85rem; }
  .perms { margin: var(--space-2xs) 0 0; padding-left: 1rem; }
  .quiet { color: var(--muted); font-size: 0.85rem; }
  .nowrap { white-space: nowrap; }
  .detail { display: block; font-size: 0.78rem; }
</style>
