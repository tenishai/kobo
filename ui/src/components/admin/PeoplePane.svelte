<script lang="ts">
  // Who runs the room and who can see it, plus the group-request queue.
  import { toast } from "../../lib/toast.svelte";
  import { admin, decideGroupRequest, setPeople } from "../../lib/admin/store.svelte";

  type Kind = "adminGroups" | "operators" | "readerGroups";
  let editing = $state<Kind | null>(null);
  let text = $state("");

  const ROWS: { kind: Kind; label: string; desc: string }[] = [
    { kind: "adminGroups", label: "Admins", desc: "Groups whose members run the room. Every admin permission." },
    { kind: "operators", label: "Operators", desc: "Named people with operator standing, with or without a group." },
    { kind: "readerGroups", label: "Readers", desc: "Groups that see every agent and change none." },
  ];

  function startEdit(kind: Kind): void {
    editing = kind;
    text = admin.people[kind].join(", ");
  }

  function saveEdit(): void {
    if (!editing) return;
    const values = text.split(",").map((s) => s.trim()).filter(Boolean);
    setPeople(editing, values);
    toast.success("People updated", "Pull request opened against the org config.");
    editing = null;
  }

  function decide(id: number, action: "add" | "decline"): void {
    decideGroupRequest(id, action);
    toast.success(action === "add" ? "Group added" : "Request declined", action === "add" ? "Pull request opened; the requester is told when it merges." : undefined);
  }
</script>

<section class="panel" aria-labelledby="people-heading">
  <div class="panel__header">
    <h2 id="people-heading" class="panel__heading">People</h2>
    <p class="panel__lead">Who runs the room and who can see it.</p>
  </div>

  <div class="rows">
    {#each ROWS as r (r.kind)}
      <div class="row">
        <div class="row__text">
          <span class="row__label">{r.label}</span>
          <p class="row__desc">{r.desc}</p>
        </div>
        <div class="row__value">
          {#if editing === r.kind}
            <input id="people-{r.kind}" type="text" bind:value={text} aria-label={r.label} />
            <button type="button" class="btn btn--primary" onclick={saveEdit}>Save</button>
            <button type="button" class="btn btn--quiet" onclick={() => (editing = null)}>Cancel</button>
          {:else}
            <span>{admin.people[r.kind].join(", ") || "none"}</span>
            <button type="button" class="btn btn--quiet" onclick={() => startEdit(r.kind)}>Edit</button>
          {/if}
        </div>
      </div>
    {/each}
  </div>

  <h3 class="panel__subheading">Group requests</h3>
  {#if admin.people.groupRequests.length === 0}
    <p class="panel__empty">No requests waiting.</p>
  {:else}
    <table class="panel__table">
      <thead><tr><th>Group</th><th>Requested by</th><th>When</th><th class="panel__cell--right"></th></tr></thead>
      <tbody>
        {#each admin.people.groupRequests as g (g.id)}
          <tr>
            <td><span class="panel__name">{g.group}</span></td>
            <td>{g.requester}</td>
            <td>{g.when}</td>
            <td class="panel__cell--right">
              <button type="button" class="btn btn--quiet" onclick={() => decide(g.id, "add")}>Add</button>
              <button type="button" class="btn btn--quiet" onclick={() => decide(g.id, "decline")}>Decline</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
  <p class="panel__note">Add writes the group to the org config as a pull request; the requester is told when it merges.</p>
</section>

<style>
  .rows {
    display: flex;
    flex-direction: column;
  }

  .row {
    display: grid;
    grid-template-columns: minmax(10rem, 14rem) 1fr;
    gap: var(--space-md);
    align-items: center;
    padding: var(--space-sm) 0;
    border-bottom: 1px solid var(--border);
  }

  .row__label {
    font-weight: 600;
  }

  .row__desc {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .row__value {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-xs);
    min-width: 0;
    font-size: 0.9rem;
  }

  .row__value input {
    flex: 1;
    min-width: 12rem;
    padding: var(--space-2xs) var(--space-xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: var(--ink);
    font: inherit;
  }

  @media (max-width: 40rem) {
    .row {
      grid-template-columns: 1fr;
    }
  }
</style>
