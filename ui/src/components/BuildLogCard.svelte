<script lang="ts">
  // Build-log viewer (2026-08-27 feedback: parity with the org's PaaS). A
  // sheet off the page header's Build log button (issue #368; a card in the
  // Settings tab before that): opening it fetches the tail of the agent
  // repo's most recent workflow-run log -- failed steps only when the run
  // failed, whole log otherwise (the server decides). Lazy on purpose: the
  // fetch costs a GitHub round trip, so nothing loads until someone
  // actually asks to see it. Owner-or-operator on the server; a 403 here
  // renders as the polite line below rather than an error wall. Fully
  // controlled by the page (`open` + `onclose`), like every dialog here.
  import { createQuery } from "@tanstack/svelte-query";
  import { ApiError, errorMessage, fetchBuildLog } from "../lib/api";
  import Modal from "./Modal.svelte";

  let {
    agentName,
    open,
    onclose,
  }: { agentName: string; open: boolean; onclose: () => void } = $props();

  const buildLog = createQuery(() => ({
    queryKey: ["agents", agentName, "build-log"],
    queryFn: () => fetchBuildLog(agentName),
    enabled: open,
    staleTime: 15_000,
  }));

  const notAllowed = $derived(
    buildLog.isError && buildLog.error instanceof ApiError && buildLog.error.status === 403,
  );
  const noBuilds = $derived(
    buildLog.isError && buildLog.error instanceof ApiError && buildLog.error.status === 404,
  );
</script>

<Modal {open} title="Build log" size="wide" {onclose}>
  <p class="build-log__lead">The last build of this agent's code, as the build itself told it.</p>
  {#if buildLog.isPending}
    <p class="build-log__note">Fetching the latest build…</p>
  {:else if notAllowed}
    <p class="build-log__note">Only this agent's owner (or a room operator) can read its build log.</p>
  {:else if noBuilds}
    <p class="build-log__note">This agent's repository has no builds yet.</p>
  {:else if buildLog.isError}
    <p class="build-log__note">{errorMessage(buildLog.error, "The build log could not be loaded.")}</p>
  {:else if buildLog.data}
    <p class="build-log__run">
      Latest build: <strong>{buildLog.data.run.conclusion ?? buildLog.data.run.status}</strong>
      <span class="build-log__sha">({buildLog.data.run.head_sha.slice(0, 7)})</span>
    </p>
    <pre class="build-log__text">{buildLog.data.log}</pre>
  {/if}
</Modal>

<style>
  .build-log__lead {
    margin: 0 0 var(--space-sm);
    color: var(--ink);
    font-size: 0.9rem;
  }

  .build-log__note {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .build-log__run {
    margin: 0 0 var(--space-xs);
    color: var(--ink);
    font-size: 0.85rem;
  }

  .build-log__sha {
    color: var(--muted);
    font-family: var(--font-mono);
  }

  /* Code on a dark surface, whichever theme the page is in. */
  .build-log__text {
    max-height: 60vh;
    margin: 0;
    padding: var(--space-sm) var(--space-md);
    overflow: auto;
    border-radius: var(--radius-sm);
    background: oklch(0.2 0.005 52);
    color: oklch(0.93 0.003 90);
    font-size: 0.75rem;
    line-height: 1.45;
    white-space: pre-wrap;
    word-break: break-all;
  }
</style>
