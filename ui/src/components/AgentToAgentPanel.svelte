<script lang="ts">
  // Agent to agent: the Harness section (issue #368) that used to be the
  // Other agents block of the Access tab. Which agents from other teams this
  // one may ask for help, and who has asked to reach it. Agents in the same
  // group are paired by the room on its own and need nothing here.
  //
  // The picker is grouped by team (owner feedback, 2026-11-01): one row per
  // team with a count and a chevron; open a team and its agents are compact
  // rows with a checkbox, the name and one line of description. Tick one or
  // many across teams, then one Request access sends a request per picked
  // agent. An agent already asked, or not ready, shows its state in place
  // of the checkbox so nobody asks twice. The viewer's own team never
  // appears: the server names it as `paired_group`.
  //
  // The table holds every agent fact in one place: incoming pending
  // requests (Accept/Decline), same-team pairs (automatic, not revocable)
  // and accepted cross-group pairs (Revoke), each with the agent's team,
  // its status and the date. A paired row's status says what the pair
  // buys, "Can join shared threads" (issue #289); the quiet word beside
  // it keeps the distinction between the automatic same-team pair and an
  // accepted or approved request.
  //
  // With the permission service on (issue #310; `GET /api/agents/{name}/
  // access` answers), asking another team's agent is a permission: the
  // picker posts an access request for `a2a_ask` on the target agent, the
  // row waits for Review, and an approved one is a pair the server reports
  // with `via: "grant"`. With it off (the access route is 404) the picker
  // sends the invite it always did.
  //
  // Mounted by HarnessPanel only when the caller may administer the agent
  // (the page's own invites probe answered 200, the same gate the Access
  // tab has); the routes below answer nobody else.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { errorMessage } from "../lib/api";
  import {
    acceptInviteMutation,
    agentAccessQuery,
    createInviteMutation,
    declineInviteMutation,
    invitesQuery,
    pairsQuery,
    requestAgentAccessMutation,
    requestableQuery,
    revokeInviteMutation,
  } from "../lib/queries";
  import { whenDay } from "../lib/time";
  import { toast } from "../lib/toast.svelte";
  import ErrorState from "./ErrorState.svelte";
  import Skeleton from "./Skeleton.svelte";

  let { agentName, frozen = false }: { agentName: string; frozen?: boolean } = $props();

  const queryClient = useQueryClient();
  // agentName is read once: AgentDetail remounts every panel via
  // `{#key route.name}` when the routed agent changes.
  // svelte-ignore state_referenced_locally
  const invites = createQuery(() => (invitesQuery(agentName)));
  // svelte-ignore state_referenced_locally
  const requestable = createQuery(() => (requestableQuery(agentName)));
  // svelte-ignore state_referenced_locally
  const acceptMutation = createMutation(() => (acceptInviteMutation(queryClient, agentName)));
  // svelte-ignore state_referenced_locally
  const declineMutation = createMutation(() => (declineInviteMutation(queryClient, agentName)));
  // svelte-ignore state_referenced_locally
  const revokeMutation = createMutation(() => (revokeInviteMutation(queryClient, agentName)));
  const requestAccess = createMutation(() => (createInviteMutation(queryClient)));
  // The permission service's view of this agent; a 404 means it is off for
  // the room and the invite path stands.
  // svelte-ignore state_referenced_locally
  const access = createQuery(() => (agentAccessQuery(agentName)));
  const accessOn = $derived(access.isSuccess);
  // svelte-ignore state_referenced_locally
  const pairs = createQuery(() => ({ ...pairsQuery(agentName), enabled: access.isSuccess }));
  // svelte-ignore state_referenced_locally
  const askRequest = createMutation(() => (requestAgentAccessMutation(queryClient, agentName)));

  const ASK_PERMISSION = "a2a_ask";
  /** Ask requests waiting in Review, one row per target agent. */
  const waitingAsks = $derived(
    (access.data?.pending ?? []).filter((r) => r.permissions.includes(ASK_PERMISSION)),
  );
  /** Grant-backed pairs: this agent's approved asks (its own grants, dated),
   * plus any pair the server reports as a grant from the other side. */
  const grantPairs = $derived.by(() => {
    const dates = new Map<string, string | null>();
    for (const g of access.data?.grants ?? []) {
      if (g.permission === ASK_PERMISSION) dates.set(g.integration, g.created_at);
    }
    for (const p of pairs.data?.pairs ?? []) {
      if (p.via === "grant" && !dates.has(p.name)) dates.set(p.name, null);
    }
    return [...dates.entries()].sort(([a], [b]) => a.localeCompare(b));
  });

  const incomingPending = $derived(
    (invites.data?.incoming ?? []).filter((invite) => invite.status === "pending"),
  );
  const teamPairs = $derived(invites.data?.team_pairs ?? []);
  const acceptedPairs = $derived(
    [...(invites.data?.incoming ?? []), ...(invites.data?.outgoing ?? [])].filter(
      (invite) => invite.status === "accepted",
    ),
  );

  /** Every agent's group this panel has heard of, by name, so a table row
   * can print the team of an agent whose own payload carries only its name
   * (an incoming invite's asker, a pending ask's target). */
  const groupOf = $derived.by(() => {
    const map = new Map<string, string | null>();
    const data = requestable.data;
    for (const a of data?.requestable ?? []) map.set(a.name, a.group);
    for (const a of data?.already ?? []) map.set(a.name, a.group);
    for (const a of data?.not_ready ?? []) map.set(a.name, a.group);
    for (const p of pairs.data?.pairs ?? []) map.set(p.name, p.group);
    for (const p of teamPairs) map.set(p.name, p.group);
    for (const invite of [...(invites.data?.incoming ?? []), ...(invites.data?.outgoing ?? [])]) {
      map.set(invite.to_agent.name, invite.to_agent.group_name);
    }
    return map;
  });

  type RowAction = { kind: "decide"; id: number } | { kind: "revoke"; id: number } | null;
  type Row = {
    key: string;
    name: string;
    group: string | null;
    status: string;
    /** The quiet word after the status: how this pair came to be. */
    how: string | null;
    date: string | null;
    action: RowAction;
  };

  /** The table, in one order: who is asking, then the pairs the room made,
   * then what is waiting in Review, then the approved and accepted pairs. */
  const rows = $derived.by((): Row[] => {
    const out: Row[] = [];
    for (const invite of incomingPending) {
      out.push({
        key: `in-${invite.id}`,
        name: invite.from_agent,
        group: groupOf.get(invite.from_agent) ?? null,
        status: "Wants to talk to yours",
        how: null,
        date: invite.created_at,
        action: { kind: "decide", id: invite.id },
      });
    }
    for (const pair of teamPairs) {
      out.push({
        key: `team-${pair.name}`,
        name: pair.name,
        group: pair.group,
        status: "Can join shared threads",
        how: "Same team",
        date: null,
        action: null,
      });
    }
    for (const ask of waitingAsks) {
      out.push({
        key: `ask-${ask.id}`,
        name: ask.integration,
        group: groupOf.get(ask.integration) ?? null,
        status: "Waiting for review",
        how: null,
        date: ask.created_at,
        action: null,
      });
    }
    for (const [name, date] of grantPairs) {
      out.push({
        key: `grant-${name}`,
        name,
        group: groupOf.get(name) ?? null,
        status: "Can join shared threads",
        how: "Approved in Review",
        date,
        action: null,
      });
    }
    for (const invite of acceptedPairs) {
      const other = invite.from_agent === agentName ? invite.to_agent.name : invite.from_agent;
      out.push({
        key: `inv-${invite.id}`,
        name: other,
        group:
          invite.from_agent === agentName
            ? invite.to_agent.group_name
            : (groupOf.get(invite.from_agent) ?? null),
        status: "Can join shared threads",
        how: "Accepted request",
        date: invite.decided_at ?? invite.created_at,
        action: { kind: "revoke", id: invite.id },
      });
    }
    return out;
  });
  const hasPairs = $derived(rows.some((row) => row.status === "Can join shared threads"));

  // --- the picker ---------------------------------------------------------

  type PickState = "open" | "waiting" | "paired" | "not_ready";
  type Candidate = { name: string; description: string | null; state: PickState; stateLabel: string };
  type Team = { key: string; label: string; agents: Candidate[] };

  const NO_TEAM = "No team";

  /** One requestable row's displayed purpose: its capability sentence, else
   * its purpose tags joined into a plain line, else an honest "nothing
   * written yet", the same three-tier fallback the requestable route's own
   * docstring promises the UI. */
  function purposeText(candidate: { capability: string | null; purpose_tags: string[] }): string {
    if (candidate.capability) {
      return candidate.capability;
    }
    if (candidate.purpose_tags.length > 0) {
      return candidate.purpose_tags.join(", ");
    }
    return "No description yet.";
  }

  function alreadyLabel(status: string): [PickState, string] {
    if (status === "accepted") return ["paired", "Paired"];
    if (status === "pending") return ["waiting", accessOn ? "Waiting for review" : "Waiting for its owner"];
    return ["waiting", status];
  }

  /** The other teams and their agents, sorted by name; the viewer's own
   * team (the server's `paired_group`) is left out, since its agents are
   * paired by the room already. */
  const teams = $derived.by((): Team[] => {
    const data = requestable.data;
    if (!data) return [];
    const own = data.paired_group ?? null;
    const byGroup = new Map<string, Candidate[]>();
    const add = (group: string | null, candidate: Candidate) => {
      if (own !== null && group === own) return;
      const key = group ?? "";
      const list = byGroup.get(key) ?? [];
      list.push(candidate);
      byGroup.set(key, list);
    };
    for (const a of data.requestable) {
      add(a.group, { name: a.name, description: purposeText(a), state: "open", stateLabel: "" });
    }
    for (const a of data.already) {
      const [state, stateLabel] = alreadyLabel(a.status);
      add(a.group, { name: a.name, description: null, state, stateLabel });
    }
    for (const a of data.not_ready ?? []) {
      add(a.group, { name: a.name, description: null, state: "not_ready", stateLabel: "Not ready" });
    }
    return [...byGroup.entries()]
      .map(([key, agents]) => ({
        key,
        label: key === "" ? NO_TEAM : key,
        agents: agents.sort((x, y) => x.name.localeCompare(y.name)),
      }))
      .sort((x, y) => {
        if (x.key === "") return 1;
        if (y.key === "") return -1;
        return x.label.localeCompare(y.label);
      });
  });

  // The picker is a disclosure, shut on arrival: a section that greets you
  // with an open form reads as work waiting to be done, and this is not.
  let pickerOpen = $state(false);
  let openTeams = $state<Record<string, boolean>>({});
  let picked = $state<string[]>([]);
  let sending = $state(false);
  let sendError = $state<string | null>(null);
  let inviteActionError = $state<string | null>(null);

  function togglePicker(): void {
    pickerOpen = !pickerOpen;
    if (!pickerOpen) {
      sendError = null;
    }
  }

  function toggleTeam(key: string): void {
    openTeams = { ...openTeams, [key]: !openTeams[key] };
  }

  function togglePick(name: string): void {
    picked = picked.includes(name) ? picked.filter((n) => n !== name) : [...picked, name];
  }

  function countLabel(n: number, noun: string): string {
    return `${n} ${n === 1 ? noun : `${noun}s`}`;
  }

  function handleAccept(id: number): void {
    if (acceptMutation.isPending) {
      return;
    }
    inviteActionError = null;
    acceptMutation.mutate(id, {
      onError: (err: unknown) => {
        inviteActionError = errorMessage(err, "Couldn't accept that request. Try again.");
      },
    });
  }

  function handleDecline(id: number): void {
    if (declineMutation.isPending) {
      return;
    }
    inviteActionError = null;
    declineMutation.mutate(id, {
      onError: (err: unknown) => {
        inviteActionError = errorMessage(err, "Couldn't decline that request. Try again.");
      },
    });
  }

  function handleRevoke(id: number): void {
    if (revokeMutation.isPending) {
      return;
    }
    inviteActionError = null;
    revokeMutation.mutate(id, {
      onError: (err: unknown) => {
        inviteActionError = errorMessage(err, "Couldn't revoke that. Try again.");
      },
    });
  }

  /** One request per picked agent, in sequence (the existing mutation, so
   * each success refreshes the lists on its own). A toast says how many
   * went out; the picker shuts when every one did, and stays open with the
   * failed ones still ticked otherwise. */
  async function sendPicked(): Promise<void> {
    if (sending || picked.length === 0) {
      return;
    }
    sending = true;
    sendError = null;
    let sent = 0;
    let lastError: unknown = null;
    for (const name of [...picked]) {
      try {
        if (accessOn) {
          await askRequest.mutateAsync({ integration: name, permissions: [ASK_PERMISSION], note: null });
        } else {
          await requestAccess.mutateAsync({ fromAgent: agentName, toAgent: name });
        }
        sent += 1;
        picked = picked.filter((n) => n !== name);
      } catch (err) {
        lastError = err;
      }
    }
    sending = false;
    if (sent > 0) {
      toast.success(
        `${countLabel(sent, "request")} sent`,
        accessOn ? "Each one waits in Admin, Review." : "Each one goes to that agent's owner.",
      );
    }
    if (picked.length > 0) {
      sendError = errorMessage(
        lastError,
        `Couldn't send ${countLabel(picked.length, "request")}. Try again.`,
      );
      return;
    }
    pickerOpen = false;
  }
</script>

<section class="panel" aria-labelledby="a2a-heading">
  <div class="panel__header">
    <h2 id="a2a-heading" class="panel__heading">Agent to agent</h2>
    <p class="panel__lead">
      Which agents from other teams this one may ask for help, and who has asked to reach it.
      Agents in the same group need nothing here.
    </p>
  </div>

  <!-- The disclosure button stays usable on a frozen agent: it reveals a
       picker, it never changes anything. Every control inside it is
       disabled while frozen, which is where the freeze actually bites. -->
  <div class="panel__toolbar">
    <div class="panel__tools">
      <button
        type="button"
        class="btn"
        aria-expanded={pickerOpen}
        aria-controls="a2a-picker"
        onclick={togglePicker}
      >
        Request another team's agent
      </button>
    </div>
  </div>

  {#if pickerOpen}
    <div id="a2a-picker" class="a2a__picker">
      {#if requestable.isPending}
        <div class="a2a__pad"><Skeleton rows={2} /></div>
      {:else if requestable.isError}
        <div class="a2a__pad">
          <ErrorState
            message={errorMessage(requestable.error, "Couldn't load other agents.")}
            onretry={() => requestable.refetch()}
          />
        </div>
      {:else if teams.length === 0}
        <p class="panel__note a2a__pad">
          Nobody to ask: every other agent is already paired with this one, or there is no
          other agent yet.
        </p>
      {:else}
        <p class="panel__note a2a__pad">
          Open a team, tick the agents to ask, then press Request access.
        </p>
        <ul class="a2a__teams">
          {#each teams as team (team.key)}
            <li class="a2a__team">
              <button
                type="button"
                class="a2a__team-row"
                aria-expanded={openTeams[team.key] === true}
                onclick={() => toggleTeam(team.key)}
              >
                <span class="a2a__team-name">{team.label}</span>
                <span class="a2a__count">{countLabel(team.agents.length, "agent")}</span>
                <span
                  class="a2a__chevron"
                  class:a2a__chevron--open={openTeams[team.key] === true}
                  aria-hidden="true"
                >
                  ›
                </span>
              </button>
              {#if openTeams[team.key]}
                <ul class="a2a__agents">
                  {#each team.agents as agent (agent.name)}
                    <li>
                      {#if agent.state === "open"}
                        <label class="a2a__agent">
                          <input
                            type="checkbox"
                            checked={picked.includes(agent.name)}
                            disabled={frozen || sending}
                            aria-label={`Pick ${agent.name}`}
                            onchange={() => togglePick(agent.name)}
                          />
                          <span class="a2a__name">{agent.name}</span>
                          <span class="a2a__desc" title={agent.description}>{agent.description}</span>
                        </label>
                      {:else}
                        <div class="a2a__agent a2a__agent--state">
                          <span aria-hidden="true"></span>
                          <span class="a2a__name">{agent.name}</span>
                          <span class="a2a__desc"></span>
                          <span class="a2a__state">{agent.stateLabel}</span>
                        </div>
                      {/if}
                    </li>
                  {/each}
                </ul>
              {/if}
            </li>
          {/each}
        </ul>
        <div class="a2a__footer">
          <span class="a2a__count">{countLabel(picked.length, "agent")} picked</span>
          {#if sendError}
            <span class="panel__note panel__note--fail" role="alert">{sendError}</span>
          {/if}
          <div class="a2a__footer-actions">
            <button
              type="button"
              class="btn btn--primary"
              disabled={frozen || sending || picked.length === 0}
              onclick={sendPicked}
            >
              Request access
            </button>
          </div>
        </div>
      {/if}
    </div>
  {/if}

  {#if invites.isPending}
    <Skeleton rows={2} />
  {:else if invites.isError}
    <ErrorState
      message={errorMessage(invites.error, "Couldn't load colleague requests.")}
      onretry={() => invites.refetch()}
    />
  {:else}
    {#if inviteActionError}
      <p class="panel__note panel__note--fail" role="alert">{inviteActionError}</p>
    {/if}
    <div class="table-card">
      <table class="panel__table">
        <thead>
          <tr>
            <th scope="col">Agent</th>
            <th scope="col">Team</th>
            <th scope="col">Status</th>
            <th scope="col" class="a2a__col-date">Date</th>
            <th scope="col" class="a2a__col-action">
              <span class="panel__sr">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {#if rows.length === 0}
            <tr>
              <td class="panel__empty" colspan="5">
                No agent is paired with this one yet, and nobody has asked to reach it.
              </td>
            </tr>
          {/if}
          {#each rows as row (row.key)}
            <tr class="a2a__row">
              <td><span class="a2a__name">{row.name}</span></td>
              <td><span class="a2a__team-cell">{row.group ?? ""}</span></td>
              <td>
                <span class="a2a__status">{row.status}</span>
                {#if row.how}
                  <span class="a2a__how">{row.how}</span>
                {/if}
              </td>
              <td><span class="a2a__date">{whenDay(row.date)}</span></td>
              <td class="panel__cell--right">
                {#if row.action?.kind === "decide"}
                  {@const id = row.action.id}
                  <div class="a2a__actions">
                    <button
                      type="button"
                      class="btn"
                      disabled={frozen || acceptMutation.isPending}
                      aria-label={`Accept ${row.name}`}
                      onclick={() => handleAccept(id)}
                    >
                      Accept
                    </button>
                    <button
                      type="button"
                      class="btn btn--quiet btn--danger"
                      disabled={frozen || declineMutation.isPending}
                      aria-label={`Decline ${row.name}`}
                      onclick={() => handleDecline(id)}
                    >
                      Decline
                    </button>
                  </div>
                {:else if row.action?.kind === "revoke"}
                  {@const id = row.action.id}
                  <div class="a2a__actions">
                    <!-- A revoked pass rides a few more minutes on any token
                         already minted for it; the hint sits beside the
                         button, not as a standing caption. -->
                    <span class="a2a__hint">Revoking takes effect within a few minutes.</span>
                    <button
                      type="button"
                      class="btn btn--quiet btn--danger"
                      disabled={frozen || revokeMutation.isPending}
                      aria-label={`Revoke ${row.name}`}
                      onclick={() => handleRevoke(id)}
                    >
                      Revoke
                    </button>
                  </div>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if hasPairs}
      <p class="panel__note">Pairs are live as soon as they appear here.</p>
    {/if}
  {/if}
</section>

<style>
  /* The picker: one bordered box that scrolls, teams divided by hairlines,
     the footer stuck to its bottom edge. */
  .a2a__picker {
    display: flex;
    flex-direction: column;
    width: 100%;
    max-height: 26rem;
    overflow: auto;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
  }

  .a2a__pad {
    padding: var(--space-xs) var(--space-sm);
  }

  .a2a__teams {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .a2a__team {
    border-top: 1px solid var(--border);
  }

  .a2a__team-row {
    display: flex;
    width: 100%;
    align-items: center;
    gap: var(--space-xs);
    padding: var(--space-xs) var(--space-sm);
    border: none;
    background: none;
    color: var(--ink);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  .a2a__team-row:hover {
    background: var(--accent-soft);
  }

  .a2a__team-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    font-size: 0.9rem;
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .a2a__count {
    color: var(--muted);
    font-size: 0.8rem;
    white-space: nowrap;
  }

  .a2a__chevron {
    color: var(--muted);
    font-size: 1.1rem;
    line-height: 1;
    transition: transform 0.15s ease;
  }

  .a2a__chevron--open {
    transform: rotate(90deg);
  }

  .a2a__agents {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    margin: 0;
    padding: 0 var(--space-sm) var(--space-xs) calc(var(--space-sm) + var(--space-xs));
    list-style: none;
  }

  /* One agent: the checkbox (or its empty place), the name, one line of
     description that gives way, and the state when there is one. */
  .a2a__agent {
    display: grid;
    grid-template-columns: 1rem auto minmax(0, 1fr) auto;
    align-items: center;
    column-gap: var(--space-xs);
    min-height: 1.5rem;
  }

  label.a2a__agent {
    cursor: pointer;
  }

  .a2a__agent input {
    margin: 0;
  }

  .a2a__name {
    color: var(--ink);
    font-size: 0.9rem;
    font-weight: 600;
    overflow-wrap: anywhere;
  }

  .a2a__agent .a2a__name {
    white-space: nowrap;
  }

  .a2a__desc {
    min-width: 0;
    overflow: hidden;
    color: var(--ink);
    font-size: 0.85rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .a2a__state {
    color: var(--muted);
    font-size: 0.8rem;
    white-space: nowrap;
  }

  .a2a__footer {
    position: sticky;
    bottom: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-xs);
    padding: var(--space-xs) var(--space-sm);
    border-top: 1px solid var(--border);
    background: var(--panel);
  }

  .a2a__footer-actions {
    margin-left: auto;
  }

  /* The table. */
  .a2a__team-cell,
  .a2a__status {
    color: var(--ink);
    font-size: 0.85rem;
  }

  .a2a__how,
  .a2a__date,
  .a2a__hint {
    color: var(--muted);
    font-size: 0.8rem;
  }

  .a2a__how {
    margin-left: var(--space-2xs);
  }

  .a2a__how::before {
    content: "· ";
  }

  .a2a__date {
    white-space: nowrap;
  }

  .a2a__actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: var(--space-xs);
  }

  .a2a__col-date {
    width: 6rem;
  }

  .a2a__col-action {
    width: 16rem;
  }

  @media (max-width: 48rem) {
    .a2a__agent {
      grid-template-columns: 1rem minmax(0, 1fr) auto;
    }

    .a2a__desc {
      grid-column: 2 / span 2;
    }

    .a2a__agent--state .a2a__desc {
      display: none;
    }
  }
</style>
