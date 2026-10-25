<script lang="ts">
  // Every agent is a row, every lane a chip, so "no lane" is visible before
  // it fails a reply. A lane is a grant on the tool gateway: applied at once
  // through the gateway's admin plane when the room holds its token, and
  // always written to the gateway's grants file as a pull request.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import Modal from "../Modal.svelte";
  import { errorMessage, type AdminLane } from "../../lib/api";
  import { toast } from "../../lib/toast.svelte";
  import { adminConnectionsQuery, putAdminLaneMutation, revokeAdminLaneMutation } from "../../lib/queries";

  let { item = null }: { item?: string | null } = $props();

  const queryClient = useQueryClient();
  const conns = createQuery(() => (adminConnectionsQuery()));
  const grant = createMutation(() => (putAdminLaneMutation(queryClient)));
  const revoke = createMutation(() => (revokeAdminLaneMutation(queryClient)));

  const integrations = $derived(conns.data?.integrations ?? []);
  let granting = $state<string | null>(null);
  let pick = $state("");
  let access = $state<"read" | "write">("read");

  // A deep link `#/admin/connections/<agent>` opens that agent's grant form.
  $effect(() => {
    if (item && !granting && conns.data?.agents.some((a) => a.agent === item)) {
      openGrant(item);
    }
  });

  function openGrant(agent: string): void {
    granting = agent;
    pick = integrations[0]?.id ?? "";
    access = "read";
  }

  const pickedBase = $derived(integrations.find((i) => i.id === pick)?.base_access ?? "read");

  function submitGrant(event: SubmitEvent): void {
    event.preventDefault();
    if (!granting || !pick) return;
    const agent = granting;
    grant.mutate(
      { agent, integration: pick, access },
      {
        onSuccess: (r) => {
          toast.success(`${agent} may ${access} ${pick}`, `${r.outcome.live}; ${r.outcome.file}.`);
          granting = null;
        },
        onError: (err: unknown) => toast.error("Could not grant the lane", errorMessage(err, "Try again.")),
      },
    );
  }

  function handleRevoke(l: AdminLane): void {
    revoke.mutate(
      { agent: l.agent, integration: l.integration },
      {
        onSuccess: (r) => toast.success(`${l.agent} no longer reaches ${l.integration}`, `${r.outcome.live}.`),
        onError: (err: unknown) => toast.error("Could not revoke the lane", errorMessage(err, "Try again.")),
      },
    );
  }

  const STATE: Record<string, string> = { pending: "Pending", applied: "Live", failed: "Failed", revoked: "Revoked" };
</script>

<section class="panel page" aria-labelledby="connections-heading">
  <div class="page__header">
    <h2 id="connections-heading" class="page__title">Connections</h2>
    <p class="page__lead">Which vendors each agent may reach. A lane is one agent's access to one integration.</p>
  </div>

  {#if conns.isPending}
    <p class="panel__note">Reading the lanes…</p>
  {:else if conns.isError}
    <p class="panel__note">Could not read the lanes just now.</p>
  {:else if (conns.data?.agents ?? []).length === 0}
    <p class="panel__empty">No agents yet.</p>
  {:else}
    <div class="table-card"><table class="panel__table">
      <thead><tr><th>Agent</th><th>Group</th><th>Lanes</th><th class="panel__cell--right"></th></tr></thead>
      <tbody>
        {#each conns.data?.agents ?? [] as a (a.agent)}
          <tr>
            <td><span class="panel__name">{a.agent}</span></td>
            <td>{a.group ?? "—"}</td>
            <td>
              {#if a.lanes.filter((l) => l.state !== "revoked").length === 0}
                <span class="lane lane--none">no lane</span>
              {:else}
                <div class="lanes">
                  {#each a.lanes.filter((l) => l.state !== "revoked") as l (l.id)}
                    <span class="lane lane--{l.state}" title={l.state_detail ?? ""}>
                      {l.integration} · {l.access} · {STATE[l.state] ?? l.state}
                      {#if l.spec.pull_request}<a class="lane__act" href={l.spec.pull_request} target="_blank" rel="noreferrer">PR</a>{/if}
                      <button type="button" class="lane__act lane__act--danger" disabled={revoke.isPending} onclick={() => handleRevoke(l)}>Revoke</button>
                    </span>
                  {/each}
                </div>
              {/if}
            </td>
            <td class="panel__cell--right">
              <button type="button" class="btn btn--quiet" disabled={integrations.length === 0} onclick={() => openGrant(a.agent)}>Grant a lane</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table></div>
    {#if integrations.length === 0}
      <p class="panel__note">Connect an integration first; a lane needs something to reach.</p>
    {/if}
  {/if}
</section>

<Modal open={granting !== null} title={granting ? `Grant ${granting} a lane` : ""} onclose={() => (granting = null)}>
  {#if granting}
    <form id="grant-form" class="form" onsubmit={submitGrant}>
      <label class="field">
        <span>Integration</span>
        <select id="grant-integration" bind:value={pick}>
          {#each integrations as i (i.id)}
            <option value={i.id}>{i.name} ({i.base_access === "write" ? "read and write" : "read-only"})</option>
          {/each}
        </select>
      </label>
      <fieldset class="field">
        <legend>Access</legend>
        <label><input type="radio" name="access" value="read" bind:group={access} /> read</label>
        <label><input type="radio" name="access" value="write" bind:group={access} disabled={pickedBase !== "write"} /> write{pickedBase !== "write" ? " (the room holds this one read-only)" : ""}</label>
      </fieldset>
      <p class="panel__note">The lane is written to the gateway now when its admin plane is wired, and to its grants file as a pull request either way.</p>
    </form>
  {/if}
  {#snippet footer()}
    <button type="button" class="btn" onclick={() => (granting = null)}>Cancel</button>
    <button type="submit" form="grant-form" class="btn btn--primary" disabled={grant.isPending || !pick}>Grant</button>
  {/snippet}
</Modal>

<style>
  .lanes {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2xs);
  }

  .lane {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2xs);
    padding: 0.1rem 0.5rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    font-size: 0.8rem;
  }

  .lane--none {
    color: var(--muted);
    border-style: dashed;
  }

  .lane--applied {
    border-color: var(--ok);
  }

  .lane--pending {
    border-color: var(--warn);
  }

  .lane--failed {
    border-color: var(--fail);
    color: var(--fail);
  }

  .lane__act {
    padding: 0;
    border: none;
    background: none;
    color: var(--accent);
    font: inherit;
    font-size: 0.75rem;
    cursor: pointer;
    text-decoration: none;
  }

  .lane__act--danger {
    color: var(--fail);
  }

  .form {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
  }

  .field select {
    padding: var(--space-2xs) var(--space-xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: var(--ink);
    font: inherit;
  }

  fieldset.field {
    display: flex;
    gap: var(--space-sm);
    padding: 0;
    border: none;
  }
</style>
