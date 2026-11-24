<script lang="ts">
  // The Access tab (access-control arc, Task C2; reshaped by issues #309,
  // #365 and #368): what this agent can reach. One line about visibility,
  // one line about the identity it runs as, then the vendors (PermissionsPanel):
  // what it holds at each, and the picker that asks for more.
  //
  // The two blocks that were here and are not about vendors moved out in
  // issue #368: People is the Owners sheet off the page header, and Other
  // agents is the Harness section Agent to agent.
  //
  // Mounted by AgentDetail only once its own probe of
  // `GET /api/agents/{name}/invites` has already succeeded (see tabs.ts's
  // `primaryTabs` doc comment); this component never re-derives that
  // eligibility itself, same "server decides, UI reflects" discipline every
  // other gated surface in this room follows.
  import type { AgentIdentity } from "../lib/api";
  import PermissionsPanel from "./PermissionsPanel.svelte";

  let {
    agentName,
    groupName,
    frozen = false,
    identity = null,
  }: {
    agentName: string;
    groupName: string | null;
    frozen?: boolean;
    /** The identity the agent runs as, named under the heading; absent on
     * an older server. */
    identity?: AgentIdentity | null;
  } = $props();
</script>

<section class="panel" aria-labelledby="access-heading">
  <div class="panel__header">
    <h2 id="access-heading" class="panel__heading">Access</h2>
    {#if groupName}
      <p class="panel__lead">Everyone in {groupName} can see this agent.</p>
    {:else}
      <p class="panel__lead">
        This agent is not in a group. Only its owner, the room's operators and the people
        added under Owners can see it.
      </p>
    {/if}
    {#if identity?.named_only}
      <p class="access-panel__identity">
        Google Cloud's logs name this agent <code>{identity.gsa_email}</code>.
      </p>
    {:else if identity}
      <p class="access-panel__identity">Runs as <code>{identity.gsa_email}</code>.</p>
    {/if}
  </div>

  <PermissionsPanel {agentName} {frozen} />
</section>

<style>
  .access-panel__identity {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .access-panel__identity code {
    color: var(--ink);
    font-size: 0.8rem;
  }
</style>
