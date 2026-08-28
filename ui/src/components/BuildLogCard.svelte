<script lang="ts">
  // Build-log viewer (2026-08-27 feedback: kodama parity). A collapsed
  // card on the Overview tab; expanding it fetches the tail of the agent
  // repo's most recent workflow-run log -- failed steps only when the run
  // failed, whole log otherwise (the server decides). Lazy on purpose:
  // the fetch costs a GitHub round trip, so nothing loads until someone
  // actually asks to see it. Owner-or-operator on the server; a 403 here
  // renders as the polite line below rather than an error wall.
  import { createQuery } from "@tanstack/svelte-query";
  import { ApiError, errorMessage, fetchBuildLog } from "../lib/api";

  let { agentName }: { agentName: string } = $props();

  let open = $state(false);

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

<section class="build-log">
  <button
    type="button"
    class="build-log__toggle"
    aria-expanded={open}
    onclick={() => (open = !open)}
  >
    {open ? "Hide build log" : "View build log"}
  </button>

  {#if open}
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
  {/if}
</section>

<style>
  .build-log {
    margin-top: 1rem;
  }

  .build-log__toggle {
    background: none;
    border: 1px solid var(--border, #d0c8bc);
    border-radius: 6px;
    padding: 0.35rem 0.75rem;
    cursor: pointer;
    font: inherit;
    font-size: 0.85rem;
  }

  .build-log__note {
    font-size: 0.85rem;
    opacity: 0.75;
    margin: 0.5rem 0 0;
  }

  .build-log__run {
    font-size: 0.85rem;
    margin: 0.5rem 0 0.25rem;
  }

  .build-log__sha {
    opacity: 0.6;
  }

  .build-log__text {
    max-height: 24rem;
    overflow: auto;
    font-size: 0.75rem;
    line-height: 1.4;
    background: var(--surface-sunken, #f4efe8);
    border-radius: 6px;
    padding: 0.75rem;
    white-space: pre-wrap;
    word-break: break-all;
  }
</style>
