<script lang="ts">
  // Who runs the room and who can see it, read from the org keys that say
  // so, plus the real group-request queue. The three lists are edited in the
  // org config (one line says so); the queue is decided here, as it already
  // is from the dashboard.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { when } from "../../lib/time";
  import { toast } from "../../lib/toast.svelte";
  import { adminPeopleQuery, decideGroupRequestMutation, groupRequestsQuery } from "../../lib/queries";

  const queryClient = useQueryClient();
  const people = createQuery(() => (adminPeopleQuery()));
  const requests = createQuery(() => (groupRequestsQuery(true)));
  const decide = createMutation(() => (decideGroupRequestMutation(queryClient)));

  const ROWS: { key: "admin_groups" | "operators" | "reader_groups" | "slack_operators"; label: string; desc: string }[] = [
    { key: "admin_groups", label: "Admins", desc: "Groups whose members run the room: every agent, every setting, this panel." },
    { key: "operators", label: "Operators", desc: "Named people with the same standing, with or without a group. Keep one as break-glass." },
    { key: "reader_groups", label: "Readers", desc: "Groups that see every agent and change none." },
    { key: "slack_operators", label: "Slack operators", desc: "People who may finish an agent's Slack setup." },
  ];

  const pending = $derived((requests.data ?? []).filter((r) => r.status === "pending"));

  function onDecide(id: number, action: "done" | "declined"): void {
    decide.mutate(
      { id, action },
      {
        onSuccess: () =>
          toast.success(action === "done" ? "Group added" : "Request declined", action === "done" ? "The requester can file their agent there now." : undefined),
        onError: () => toast.error("Could not decide that request", "Try again in a moment."),
      },
    );
  }
</script>

<section class="panel page" aria-labelledby="people-heading">
  <div class="page__header">
    <h2 id="people-heading" class="page__title">People</h2>
    <p class="page__lead">Who runs the room, who can see it, and who is asking for a group.</p>
  </div>

  {#if people.isPending}
    <p class="panel__note">Reading who is who…</p>
  {:else if people.isError}
    <p class="panel__note">Could not read the room's people just now.</p>
  {:else}
    <div class="table-card"><table class="panel__table">
      <thead><tr><th>Who</th><th>Can</th><th>Set to</th></tr></thead>
      <tbody>
        {#each ROWS as r (r.key)}
          <tr>
            <td><span class="who">{r.label}</span></td>
            <td class="quiet">{r.desc}</td>
            <td>
              {#each people.data?.[r.key] ?? [] as v (v)}<span class="chip">{v}</span>{:else}<span class="quiet">none</span>{/each}
            </td>
          </tr>
        {/each}
      </tbody>
    </table></div>
    <p class="page__foot">These lists are set in the org config.</p>
  {/if}

  <h3 class="panel__subheading">Group requests</h3>
  {#if requests.isPending}
    <p class="panel__note">Reading the queue…</p>
  {:else if pending.length === 0}
    <p class="panel__empty">No requests waiting.</p>
  {:else}
    <div class="table-card"><table class="panel__table">
      <thead><tr><th>Group</th><th>Requested by</th><th>When</th><th class="panel__cell--right"></th></tr></thead>
      <tbody>
        {#each pending as g (g.id)}
          <tr>
            <td><span class="panel__name">{g.group_name}</span>{#if g.note} <span class="row__desc">{g.note}</span>{/if}</td>
            <td>{g.requested_by ?? "unknown"}</td>
            <td>{when(g.created_at)}</td>
            <td class="panel__cell--right">
              <button type="button" class="btn btn--quiet" disabled={decide.isPending} onclick={() => onDecide(g.id, "done")}>Added</button>
              <button type="button" class="btn btn--quiet" disabled={decide.isPending} onclick={() => onDecide(g.id, "declined")}>Decline</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table></div>
    <p class="panel__note">Press Added once the group exists in the org config; the requester is told.</p>
  {/if}
</section>

<style>
  .who { font-weight: 600; }
  .quiet { color: var(--muted); font-size: 0.85rem; }
</style>
