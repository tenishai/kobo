<script lang="ts">
  // A single agent's detail: header (back link, name, status, model, repo
  // link), a read-only composition summary (persona/skills/model — no
  // editing affordances, that's P2), the step checklist, a retry banner
  // when the agent has failed, and — once it's arrived — the "First words"
  // card. All polling is `agentQuery`'s own adaptive cadence (status.ts):
  // this component only renders whatever state that query is in, same
  // loading/error/populated split Dashboard already uses.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import ChatPanel from "../components/ChatPanel.svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import FirstReply from "../components/FirstReply.svelte";
  import Skeleton from "../components/Skeleton.svelte";
  import StatusBadge from "../components/StatusBadge.svelte";
  import StepChecklist from "../components/StepChecklist.svelte";
  import { errorMessage } from "../lib/api";
  import { agentQuery, retryAgentMutation } from "../lib/queries";
  import { formatHash, route } from "../lib/router.svelte";
  import { toast } from "../lib/toast.svelte";

  // Invariant: App.svelte only mounts this page when `route.page === "agent"`,
  // and the router (router.svelte.ts) only ever sets that page together with
  // a non-null `name` — so this assertion just names that contract rather
  // than re-deriving it.
  const name = route.name as string;

  const queryClient = useQueryClient();
  const agent = createQuery(agentQuery(name));
  const retry = createMutation(retryAgentMutation(queryClient, name));

  function handleRetry(): void {
    $retry.mutate(undefined, {
      onSuccess: () => {
        toast.success("Retrying", `Re-running ${name}'s setup…`);
      },
      onError: (error: unknown) => {
        toast.error("Retry failed", errorMessage(error, "Something went wrong."));
      },
    });
  }
</script>

<div class="agent-detail">
  <a class="agent-detail__back" href={formatHash({ page: "dashboard" })}>
    ← Back to dashboard
  </a>

  {#if $agent.isPending}
    <Skeleton rows={6} />
  {:else if $agent.isError}
    <ErrorState
      message={errorMessage($agent.error, "Couldn't load this agent.")}
      onretry={() => $agent.refetch()}
    />
  {:else if $agent.data}
    {@const data = $agent.data}
    <header class="agent-detail__header">
      <div class="agent-detail__title">
        <h1 class="agent-detail__name">{data.name}</h1>
        <StatusBadge status={data.status} />
      </div>
      <div class="agent-detail__meta">
        <span class="agent-detail__model" title={data.model}>{data.model_label}</span>
        {#if data.repo_full_name}
          <span class="agent-detail__sep" aria-hidden="true">·</span>
          <a
            class="agent-detail__repo"
            href={`https://github.com/${data.repo_full_name}`}
            target="_blank"
            rel="noreferrer"
          >
            source
          </a>
        {/if}
      </div>
    </header>

    <div class="composition">
      <span class="composition__item">
        <span class="composition__label">Persona</span>
        <span class="composition__value">{data.persona}</span>
      </span>
      {#if data.skills.length > 0}
        <div class="composition__skills">
          {#each data.skills as skill (skill.name)}
            <span class="skill-chip" title={skill.sha}>{skill.name}</span>
          {/each}
        </div>
      {/if}
      <span class="composition__item">
        <span class="composition__label">Model</span>
        <span class="composition__value">{data.model_label}</span>
      </span>
    </div>

    {#if data.status === "failed"}
      <div class="failed-banner" role="alert">
        <p class="failed-banner__message">
          {data.error ?? "Something went wrong during setup."}
        </p>
        <button
          type="button"
          class="failed-banner__retry"
          disabled={$retry.isPending}
          onclick={handleRetry}
        >
          {$retry.isPending ? "Retrying…" : "Retry"}
        </button>
      </div>
    {/if}

    <StepChecklist steps={data.steps} />

    {#if data.first_reply}
      <FirstReply reply={data.first_reply} />
    {/if}

    {#if data.status === "ready"}
      <ChatPanel agentName={data.name} />
    {/if}
  {/if}
</div>

<style>
  .agent-detail {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
    max-width: 40rem;
  }

  .agent-detail__back {
    align-self: flex-start;
    color: var(--muted);
    font-size: 0.875rem;
    text-decoration: none;
  }

  .agent-detail__back:hover {
    color: var(--accent);
  }

  .agent-detail__header {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .agent-detail__title {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-sm);
  }

  .agent-detail__name {
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--ink);
    font-size: 1.4rem;
  }

  .agent-detail__meta {
    display: flex;
    align-items: center;
    gap: var(--space-2xs);
    color: var(--muted);
    font-size: 0.875rem;
  }

  .agent-detail__model {
    font-family: var(--font-mono);
    font-size: 0.8rem;
  }

  .agent-detail__repo {
    color: var(--accent);
  }

  .composition {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-md);
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .composition__item {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }

  .composition__label {
    color: var(--muted);
    font-size: 0.75rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .composition__value {
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.85rem;
  }

  .composition__skills {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2xs);
  }

  .skill-chip {
    padding: 0.2rem var(--space-xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.75rem;
  }

  .failed-banner {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-md);
    padding: var(--space-md);
    border: 1px solid var(--fail);
    border-radius: var(--radius);
    background: color-mix(in srgb, var(--fail) 8%, var(--panel));
  }

  .failed-banner__message {
    margin: 0;
    color: var(--fail);
    font-size: 0.9rem;
  }

  .failed-banner__retry {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--fail);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .failed-banner__retry:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .failed-banner__retry:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
