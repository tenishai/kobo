<script lang="ts">
  // Each agent's lanes through the gateway to the vendors the room holds a
  // credential for. Every agent is a row, so "no lane" is visible before it
  // fails a reply. A grant travels as a pull request against the gateway's
  // grants file by default; an admin may apply it now through the gateway's
  // admin plane instead.
  import Modal from "../Modal.svelte";
  import { toast } from "../../lib/toast.svelte";
  import {
    admin,
    approveLane,
    connection,
    grantLane,
    markLanded,
    pluginName,
    revokeLane,
    vendorsWithLanes,
    type Access,
    type Connection,
    type Lane,
  } from "../../lib/admin/store.svelte";

  let { item = null }: { item?: string | null } = $props();

  const vendors = $derived(vendorsWithLanes());
  const gateway = $derived(admin.plugins.find((p) => p.id === "gateway"));

  let granting = $state<Connection | null>(null);
  let pick = $state("");
  let access = $state<Access>("read");
  let mode = $state<"pr" | "now">("pr");

  $effect(() => {
    if (item && connection(item) && granting?.agent !== item) {
      openGrant(connection(item)!);
    }
  });

  function openGrant(c: Connection): void {
    granting = c;
    pick = vendors.find((v) => !c.lanes.some((l) => l.plugin === v.id))?.id ?? vendors[0]?.id ?? "";
    access = "read";
    mode = "pr";
  }

  function submitGrant(event: SubmitEvent): void {
    event.preventDefault();
    if (!granting || !pick) return;
    grantLane(granting.agent, pick, access, mode);
    toast.success(
      `${granting.agent} granted ${pluginName(pick)} (${access})`,
      mode === "pr" ? "Pull request opened against the grants file." : "Applied through the gateway now.",
    );
    granting = null;
  }

  function approve(c: Connection, l: Lane, how: "pr" | "now"): void {
    approveLane(c.agent, l.plugin, how);
    toast.success(`${c.agent}: ${pluginName(l.plugin)} approved`, how === "pr" ? "Pull request opened." : "Applied now.");
  }

  function revoke(c: Connection, l: Lane): void {
    revokeLane(c.agent, l.plugin);
    toast.success(`${c.agent}: ${pluginName(l.plugin)} revoked`, "Pull request opened.");
  }

  function stateLabel(l: Lane): string {
    return l.state === "granted" ? "granted" : l.state === "pr_open" ? `waiting on ${l.pr}` : l.state === "requested" ? "requested" : "none";
  }
</script>

<section class="panel" aria-labelledby="connections-heading">
  <div class="panel__header">
    <h2 id="connections-heading" class="panel__heading">Connections</h2>
    <p class="panel__lead">
      Each agent's lanes to the vendors this room holds a credential for, through the gateway.
      {#if gateway}
        Grants file <code>{gateway.fields.find((f) => f.key === "grants_repo")?.value}</code>.
      {/if}
    </p>
  </div>

  <table class="panel__table">
    <thead>
      <tr>
        <th>Agent</th>
        <th>Group</th>
        <th>Lanes</th>
        <th class="panel__cell--right">Actions</th>
      </tr>
    </thead>
    <tbody>
      {#each admin.connections as c (c.agent)}
        <tr class:row--open={item === c.agent}>
          <td>
            <span class="panel__name">{c.agent}</span>
            <span class="panel__meta">{c.owner}</span>
          </td>
          <td>{c.group}</td>
          <td>
            {#if c.lanes.length === 0}
              <span class="lane lane--none">no lane</span>
            {:else}
              <div class="lanes">
                {#each c.lanes as l (l.plugin)}
                  <span class="lane lane--{l.state}">
                    <span class="lane__dot" aria-hidden="true"></span>
                    {pluginName(l.plugin)} · {l.access} · {stateLabel(l)}
                    {#if l.state === "requested"}
                      <button type="button" class="lane__act" onclick={() => approve(c, l, "pr")}>Approve</button>
                      <button type="button" class="lane__act" onclick={() => approve(c, l, "now")}>Apply now</button>
                    {:else if l.state === "pr_open"}
                      <button type="button" class="lane__act" onclick={() => markLanded(c.agent, l.plugin)}>Mark merged</button>
                    {:else if l.state === "granted"}
                      <button type="button" class="lane__act lane__act--danger" onclick={() => revoke(c, l)}>Revoke</button>
                    {/if}
                  </span>
                {/each}
              </div>
            {/if}
          </td>
          <td class="panel__cell--right">
            <button type="button" class="btn btn--quiet" onclick={() => openGrant(c)}>Grant a lane</button>
          </td>
        </tr>
      {/each}
    </tbody>
  </table>
  <p class="panel__note">
    A pull request is the durable lane: reviewed, and seeded by the gateway at its next restart.
    Apply now writes the grant through the gateway's admin plane and is lost if its policy volume is.
  </p>
</section>

<Modal open={granting !== null} title={granting ? `Grant ${granting.agent} a lane` : ""} onclose={() => (granting = null)}>
  {#if granting}
    <form id="grant-form" class="form" onsubmit={submitGrant}>
      <label class="field">
        <span>Vendor</span>
        <select id="grant-vendor" bind:value={pick}>
          {#each vendors as v (v.id)}
            <option value={v.id}>{v.name} (credential: {v.credential})</option>
          {/each}
        </select>
      </label>
      <fieldset class="field field--group">
        <legend>Access</legend>
        <label><input id="grant-read" type="radio" name="access" value="read" bind:group={access} /> read</label>
        <label>
          <input id="grant-write" type="radio" name="access" value="write" bind:group={access}
            disabled={vendors.find((v) => v.id === pick)?.credential === "read"} />
          write
          {#if vendors.find((v) => v.id === pick)?.credential === "read"}
            <span class="panel__note">(the base credential is read only)</span>
          {/if}
        </label>
      </fieldset>
      <fieldset class="field field--group">
        <legend>How</legend>
        <label><input id="grant-pr" type="radio" name="mode" value="pr" bind:group={mode} /> Open a pull request against the grants file</label>
        <label><input id="grant-now" type="radio" name="mode" value="now" bind:group={mode} /> Apply now through the gateway</label>
      </fieldset>
    </form>
  {/if}
  {#snippet footer()}
    <button type="button" class="btn" onclick={() => (granting = null)}>Cancel</button>
    <button type="submit" form="grant-form" class="btn btn--primary">Grant</button>
  {/snippet}
</Modal>

<style>
  .row--open td {
    background: var(--accent-soft);
  }

  .lanes {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .lane {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2xs);
    font-size: 0.85rem;
  }

  .lane--none {
    color: var(--muted);
  }

  .lane__dot {
    width: 8px;
    height: 8px;
    border: 1px solid var(--muted);
    border-radius: 50%;
  }

  .lane--granted .lane__dot {
    border-color: var(--ok);
    background: var(--ok);
  }

  .lane--requested .lane__dot,
  .lane--pr_open .lane__dot {
    border-color: var(--warn);
    background: var(--warn);
  }

  .lane__act {
    margin-left: var(--space-2xs);
    padding: 0;
    border: none;
    background: none;
    color: var(--link);
    font: inherit;
    font-size: 0.8rem;
    cursor: pointer;
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

  fieldset {
    margin: 0;
    padding: 0;
    border: none;
  }

  legend {
    padding: 0;
    margin-bottom: var(--space-2xs);
  }

  .field--group label {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
  }
</style>
