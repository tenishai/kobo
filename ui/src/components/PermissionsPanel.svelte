<script lang="ts">
  // What this agent may do at each connected vendor, and how to ask for more.
  // The tree is the vendor's own shape (groups, a tier column); a permission
  // the connection cannot honour is greyed with the reason, never hidden.
  // Approved permissions are grants the agent borrows a credential against
  // for an hour at a time; the decisions are the reviewers', in Admin > Review.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { errorMessage, type AccessGrant, type AccessIntegration, type AccessPermission } from "../lib/api";
  import { agentAccessQuery, requestAgentAccessMutation, revokeAgentAccessMutation } from "../lib/queries";
  import { toast } from "../lib/toast.svelte";

  let { agentName }: { agentName: string } = $props();

  const queryClient = useQueryClient();
  const access = createQuery(() => (agentAccessQuery(agentName)));
  const ask = createMutation(() => (requestAgentAccessMutation(queryClient, agentName)));
  const revoke = createMutation(() => (revokeAgentAccessMutation(queryClient, agentName)));

  // What the owner has ticked, per integration, that is not already held or waiting.
  let picked = $state<Record<string, string[]>>({});
  let note = $state("");

  // Each list defaults to empty: a room answering with a partial shape (or a
  // test's stub) must never take the whole section down.
  const grants = $derived(access.data?.grants ?? []);
  const pending = $derived(access.data?.pending ?? []);
  const integrations = $derived(access.data?.integrations ?? []);
  const held = $derived(new Set(grants.filter((g) => g.state !== "expired").map((g) => `${g.integration}:${g.permission}`)));
  const waiting = $derived(new Set(pending.flatMap((r) => (r.permissions ?? []).map((p) => `${r.integration}:${p}`))));

  function rowState(i: AccessIntegration, p: AccessPermission): "held" | "waiting" | "above" | "open" {
    const key = `${i.id}:${p.name}`;
    if (held.has(key)) return "held";
    if (waiting.has(key)) return "waiting";
    if (p.above_ceiling) return "above";
    return "open";
  }

  function toggle(i: AccessIntegration, p: AccessPermission): void {
    const list = picked[i.id] ?? [];
    picked = { ...picked, [i.id]: list.includes(p.name) ? list.filter((n) => n !== p.name) : [...list, p.name] };
  }

  function counts(i: AccessIntegration): string {
    const list = picked[i.id] ?? [];
    if (list.length === 0) return "";
    const byTier: Record<string, number> = {};
    for (const g of i.groups) for (const p of g.permissions) if (list.includes(p.name)) byTier[p.tier] = (byTier[p.tier] ?? 0) + 1;
    return `${list.length} picked (${Object.entries(byTier).map(([t, n]) => `${n} ${t}`).join(", ")})`;
  }

  function submit(i: AccessIntegration): void {
    const permissions = picked[i.id] ?? [];
    if (permissions.length === 0) return;
    ask.mutate(
      { integration: i.id, permissions, note: note.trim() || null },
      {
        onSuccess: () => {
          toast.success("Requested", `${permissions.length} permission${permissions.length === 1 ? "" : "s"} on ${i.id} wait for a reviewer.`);
          picked = { ...picked, [i.id]: [] };
          note = "";
        },
        onError: (err: unknown) => toast.error("Could not request", errorMessage(err, "Try again.")),
      },
    );
  }

  function handleRevoke(g: AccessGrant): void {
    revoke.mutate(g.id, {
      onSuccess: () => toast.success("Revoked", `${agentName} no longer holds ${g.permission} on ${g.integration}; open sessions end within the hour.`),
      onError: (err: unknown) => toast.error("Could not revoke", errorMessage(err, "Try again.")),
    });
  }

  function expires(g: AccessGrant): string {
    if (!g.expires_at) return "standing";
    const ms = new Date(g.expires_at).getTime() - Date.now();
    if (ms <= 0) return "expired";
    const days = Math.ceil(ms / 86_400_000);
    return days <= 1 ? "today" : `in ${days} days`;
  }

  function reason(p: AccessPermission): string {
    return p.above_ceiling ? "the connection does not allow this" : "";
  }
</script>

<section class="panel" aria-labelledby="permissions-heading">
  <h2 id="permissions-heading" class="panel__heading">Permissions</h2>
  <p class="panel__lead">
    What this agent may do at each connected vendor. It holds no standing credential: when a task
    needs one, it borrows an hour of access scoped to exactly these permissions, and the vendor's
    audit trail names the agent.
  </p>

  {#if access.isPending}
    <p class="panel__note">Reading the permissions…</p>
  {:else if access.isError}
    <p class="panel__note">{errorMessage(access.error, "Access is off for this room.")}</p>
  {:else if access.data}
    <h3 class="panel__subheading">Held</h3>
    {#if grants.length === 0}
      <p class="panel__empty">Nothing yet. Pick permissions below and ask.</p>
    {:else}
      <div class="table-card"><table class="panel__table">
        <thead><tr><th>Permission</th><th>Account</th><th>Tier</th><th>Expires</th><th>Granted by</th><th class="panel__cell--right"></th></tr></thead>
        <tbody>
          {#each grants as g (g.id)}
            <tr class:quiet={g.state === "expired"}>
              <td><span class="perm">{g.permission}</span></td>
              <td>{g.integration}</td>
              <td><span class="tier tier--{g.tier}">{g.tier}</span></td>
              <td>{expires(g)}</td>
              <td class="quiet">{g.granted_by}</td>
              <td class="panel__cell--right">
                {#if access.data.can_decide}
                  <button type="button" class="btn btn--quiet btn--danger" disabled={revoke.isPending} onclick={() => handleRevoke(g)}>Revoke now</button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table></div>
    {/if}

    {#if pending.length > 0}
      <h3 class="panel__subheading">Waiting for review</h3>
      <ul class="pending">
        {#each pending as r (r.id)}
          <li><span class="perm">{r.permissions.join(", ")}</span> on {r.integration}, asked by {r.requested_by}{#if r.note}: <span class="quiet">{r.note}</span>{/if}</li>
        {/each}
      </ul>
    {/if}

    <h3 class="panel__subheading">Ask for more</h3>
    {#if integrations.length === 0}
      <p class="panel__empty">No vendor is connected to the room yet.</p>
    {:else if !access.data.can_request}
      <p class="panel__note">The agent's owner or a room operator asks; you can see what it holds.</p>
    {:else}
      {#each integrations as i (i.id)}
        <details class="integration" open={integrations.length === 1}>
          <summary><span class="perm">{i.id}</span> <span class="quiet">{i.vendor}</span> <span class="counts">{counts(i)}</span></summary>
          {#each i.groups ?? [] as g (g.name)}
            <p class="group">{g.name}</p>
            <ul class="perms">
              {#each g.permissions as p (p.name)}
                {@const st = rowState(i, p)}
                <li class="perms__row perms__row--{st}">
                  <label>
                    <input type="checkbox" disabled={st !== "open"} checked={st === "held" || (picked[i.id] ?? []).includes(p.name)} onchange={() => toggle(i, p)} />
                    <span class="perm">{p.name}</span>
                    {#if p.description}<span class="quiet">{p.description}</span>{/if}
                  </label>
                  <span class="tier tier--{p.tier}">{p.tier}</span>
                  <span class="quiet why">{st === "held" ? "held" : st === "waiting" ? "waiting for review" : reason(p)}</span>
                </li>
              {/each}
            </ul>
          {/each}
          <div class="askrow">
            <input class="field__input" type="text" placeholder="Why (optional), for the reviewer" bind:value={note} />
            <button type="button" class="btn btn--primary" disabled={ask.isPending || (picked[i.id] ?? []).length === 0} onclick={() => submit(i)}>Ask</button>
          </div>
        </details>
      {/each}
      <p class="panel__note">Requests go to the room's reviewers (Admin › Review). Write-tier permissions expire and need asking again; read permissions stand until revoked.</p>
    {/if}
  {/if}
</section>

<style>
  .perm { font-family: var(--font-mono); font-size: 0.85rem; font-weight: 600; }
  .quiet { color: var(--muted); font-size: 0.85rem; }
  .tier { display: inline-block; padding: 0.05rem 0.45rem; border: 1px solid var(--border); border-radius: 999px; font-size: 0.75rem; }
  .tier--mutate { border-color: var(--warn); }
  .tier--destroy { border-color: var(--danger, var(--warn)); }
  .pending { margin: 0 0 var(--space-md); padding-left: 1.2rem; }
  .integration { margin: 0 0 var(--space-md); padding: var(--space-sm) var(--space-md); border: 1px solid var(--border); border-radius: var(--radius-sm); }
  .integration summary { cursor: pointer; }
  .counts { margin-left: var(--space-sm); color: var(--muted); font-size: 0.85rem; }
  .group { margin: var(--space-sm) 0 var(--space-2xs); color: var(--muted); font-size: 0.75rem; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; }
  .perms { margin: 0; padding: 0; list-style: none; }
  .perms__row { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(8rem, auto); gap: var(--space-sm); align-items: center; padding: var(--space-2xs) 0; }
  .perms__row label { display: flex; gap: var(--space-xs); align-items: baseline; min-width: 0; }
  .perms__row--above label, .perms__row--above .perm { color: var(--muted); }
  .why { text-align: right; }
  .askrow { display: flex; gap: var(--space-sm); margin-top: var(--space-sm); }
  .askrow .field__input { flex: 1; }
  .btn--danger { color: var(--danger, inherit); }
</style>
