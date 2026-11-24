<script lang="ts">
  // The room's model use, one row per agent, most expensive first, as the
  // model gateway metered it (issue #556). Read-only. The cost is the
  // gateway's estimate from its price list, and the page says so; the cloud
  // bill is the bill.
  import { createQuery } from "@tanstack/svelte-query";
  import { formatCost, formatCount } from "../../lib/api";
  import { formatHash } from "../../lib/router.svelte";
  import { roomSpendQuery } from "../../lib/queries";

  let days = $state(30);
  const spend = createQuery(() => (roomSpendQuery(days)));
</script>

<section class="panel page" aria-labelledby="usage-heading">
  <div class="page__toolbar">
    <div>
      <h2 id="usage-heading" class="page__title">Usage</h2>
      <p class="page__lead">What each agent's model calls cost, counted by the room's model gateway.</p>
    </div>
    <div class="page__actions">
      <label class="pick"><span>Last</span>
        <select bind:value={days}><option value={7}>7 days</option><option value={30}>30 days</option><option value={90}>90 days</option></select>
      </label>
    </div>
  </div>

  {#if spend.isPending}
    <p class="page__foot">Reading model use…</p>
  {:else if spend.isError}
    <p class="page__foot">Could not read model use just now.</p>
  {:else if !spend.data.measured}
    <p class="page__foot">Not measured. This room does not reach its models through the model gateway, so calls are not counted per agent.</p>
  {:else if spend.data.agents.length === 0 && spend.data.other.calls === 0}
    <p class="page__foot">No model calls counted in the last {spend.data.days} days.</p>
  {:else}
    {@const t = spend.data.total}
    {@const other = spend.data.other}
    <div class="table-card">
      <table class="panel__table">
        <thead>
          <tr><th>Agent</th><th class="num">Calls</th><th class="num">Failed</th><th class="num">Tokens in</th><th class="num">Tokens out</th><th class="num">Estimated cost</th></tr>
        </thead>
        <tbody>
          {#each spend.data.agents as a (a.agent)}
            <tr>
              <td>
                {#if a.removed}
                  <span class="name">{a.agent}</span> <span class="quiet">removed</span>
                {:else}
                  <a class="name" href={formatHash({ page: "agent", name: a.agent, tab: "harness" })}>{a.agent}</a>
                {/if}
              </td>
              <td class="num">{formatCount(a.calls)}</td>
              <td class="num">{formatCount(a.failed)}</td>
              <td class="num">{formatCount(a.input_tokens)}</td>
              <td class="num">{formatCount(a.output_tokens)}</td>
              <td class="num">{formatCost(a.cost)}</td>
            </tr>
          {/each}
          {#if other.calls > 0}
            <tr>
              <td><span class="name">Other keys</span> <span class="quiet">not an agent's</span></td>
              <td class="num">{formatCount(other.calls)}</td>
              <td class="num">{formatCount(other.failed)}</td>
              <td class="num">{formatCount(other.input_tokens)}</td>
              <td class="num">{formatCount(other.output_tokens)}</td>
              <td class="num">{formatCost(other.cost)}</td>
            </tr>
          {/if}
        </tbody>
        <tfoot>
          <tr>
            <td>Total</td>
            <td class="num">{formatCount(t.calls)}</td>
            <td class="num">{formatCount(t.failed)}</td>
            <td class="num">{formatCount(t.input_tokens)}</td>
            <td class="num">{formatCount(t.output_tokens)}</td>
            <td class="num">{formatCost(t.cost)}</td>
          </tr>
        </tfoot>
      </table>
    </div>
    <p class="page__foot">
      The cost is the gateway's estimate from its price list, not the cloud bill. An agent with no row made no calls through the gateway in this window.
    </p>
  {/if}
</section>

<style>
  .name { font-weight: 600; }
  a.name { color: inherit; }
  .page__actions { display: flex; gap: var(--space-xs); }
  .pick { display: inline-flex; align-items: center; gap: var(--space-xs); color: var(--muted); font-size: 0.9rem; }
  .pick select { font: inherit; }
  .quiet { color: var(--muted); font-size: 0.85rem; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  tfoot td { font-weight: 600; }
</style>
