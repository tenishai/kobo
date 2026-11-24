<script lang="ts">
  // What this agent's model calls cost, as the room's model gateway metered
  // them (issue #556): the total for the window, then one row per model.
  // The cost is the gateway's estimate, and the panel says so. A room with
  // no gateway meters nothing, and the panel says that instead of zeros.
  import { createQuery } from "@tanstack/svelte-query";
  import { formatCost, formatCount } from "../lib/api";
  import { agentSpendQuery } from "../lib/queries";

  let { agentName }: { agentName: string } = $props();

  let days = $state(30);
  const spend = createQuery(() => (agentSpendQuery(agentName, days)));
</script>

<section class="panel use" aria-labelledby="model-use-heading">
  <div class="use__head">
    <h2 id="model-use-heading" class="panel__heading">Model use</h2>
    <label class="use__pick"><span>Last</span>
      <select bind:value={days}><option value={7}>7 days</option><option value={30}>30 days</option><option value={90}>90 days</option></select>
    </label>
  </div>

  {#if spend.isPending}
    <p class="panel__note">Reading model use…</p>
  {:else if spend.isError}
    <p class="panel__note">Could not read model use just now.</p>
  {:else if !spend.data.measured}
    <p class="panel__note">Not measured. This room does not reach its models through the model gateway, so calls are not counted per agent.</p>
  {:else if spend.data.total.calls === 0}
    <p class="panel__note">No model calls counted in the last {spend.data.days} days.</p>
  {:else}
    {@const t = spend.data.total}
    <dl class="use__totals">
      <div><dt>Estimated cost</dt><dd>{formatCost(t.cost)}</dd></div>
      <div><dt>Calls</dt><dd>{formatCount(t.calls)}</dd></div>
      <div><dt>Tokens in</dt><dd>{formatCount(t.input_tokens)}</dd></div>
      <div><dt>Tokens out</dt><dd>{formatCount(t.output_tokens)}</dd></div>
    </dl>
    <table class="panel__table">
      <thead>
        <tr><th>Model</th><th class="num">Calls</th><th class="num">Tokens in</th><th class="num">Tokens out</th><th class="num">Estimated cost</th></tr>
      </thead>
      <tbody>
        {#each spend.data.models as m (m.model)}
          <tr>
            <td>{m.model}</td>
            <td class="num">{formatCount(m.calls)}</td>
            <td class="num">{formatCount(m.input_tokens)}</td>
            <td class="num">{formatCount(m.output_tokens)}</td>
            <td class="num">{formatCost(m.cost)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
    <p class="panel__note">
      Counted by the room's model gateway from its price list. It is an estimate, not the cloud bill.
      {#if t.failed > 0}{formatCount(t.failed)} of the calls failed.{/if}
    </p>
  {/if}
</section>

<style>
  .use__head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-md); flex-wrap: wrap; }
  .use__pick { display: inline-flex; align-items: center; gap: var(--space-xs); color: var(--muted); font-size: 0.9rem; }
  .use__pick select { font: inherit; }
  .use__totals {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: var(--space-sm);
    margin: 0 0 var(--space-md);
  }
  .use__totals div { padding: var(--space-sm) var(--space-md); border: 1px solid var(--border); border-radius: var(--radius-sm); }
  .use__totals dt { margin: 0; color: var(--muted); font-size: 0.8rem; }
  .use__totals dd { margin: 0; font-size: 1.15rem; font-weight: 600; font-variant-numeric: tabular-nums; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  @media (max-width: 640px) {
    .use__totals { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
</style>
