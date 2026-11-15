<script lang="ts">
  // The Chat tab of an agent that runs its team's own code. The room asks
  // such an agent through the switchboard, which can reach it only once its
  // code answers, so the box to type in is here when the page's own `chat`
  // line reads ok (the same read ConnectedChecks draws in the header).
  // Until then the tab says that line: one sentence on what is missing, in
  // place of a box that could only fail. The shared thread and the
  // colleague log are not here; other agents cannot ask this one yet.
  import { createQuery } from "@tanstack/svelte-query";
  import { agentChecksQuery } from "../lib/queries";
  import ChatPanel from "./ChatPanel.svelte";

  let { agentName, frozen = false }: { agentName: string; frozen?: boolean } = $props();

  const checks = createQuery(() => agentChecksQuery(agentName));
  const chat = $derived(checks.data?.checks?.find((check) => check.id === "chat"));
</script>

{#if chat?.state === "ok"}
  <ChatPanel {agentName} {frozen} teach={false} />
{:else if chat}
  <p class="connected-chat">{chat.line}</p>
{:else if !checks.isPending}
  <p class="connected-chat">The room could not check whether chat reaches this agent just now.</p>
{/if}

<style>
  .connected-chat {
    margin: 0;
    padding: var(--space-md);
    color: var(--ink);
    font-size: 0.9rem;
  }
</style>
