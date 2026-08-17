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
  import DangerZone from "../components/DangerZone.svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import FirstReply from "../components/FirstReply.svelte";
  import MemoryPanel from "../components/MemoryPanel.svelte";
  import Skeleton from "../components/Skeleton.svelte";
  import SkillsPanel from "../components/SkillsPanel.svelte";
  import SlackPanel from "../components/SlackPanel.svelte";
  import StatusBadge from "../components/StatusBadge.svelte";
  import StepChecklist from "../components/StepChecklist.svelte";
  import ToolingPanel from "../components/ToolingPanel.svelte";
  import { ApiError, errorMessage } from "../lib/api";
  import { agentQuery, generateAvatarMutation, retryAgentMutation } from "../lib/queries";
  import { formatHash, navigate, route } from "../lib/router.svelte";
  import { isDestroyComplete, isFrozen } from "../lib/status";
  import { TAB_LABELS, visibleTabs, type TabId } from "../lib/tabs";
  import { toast } from "../lib/toast.svelte";

  // Invariant: App.svelte only mounts this page when `route.page === "agent"`,
  // and the router (router.svelte.ts) only ever sets that page together with
  // a non-null `name` — so this assertion just names that contract rather
  // than re-deriving it.
  const name = route.name as string;

  const queryClient = useQueryClient();
  const agent = createQuery(agentQuery(name));
  const retry = createMutation(retryAgentMutation(queryClient, name));
  const generateAvatar = createMutation(generateAvatarMutation(queryClient, name));

  // Which of the ready-agent tabs (tabs.ts) is showing. Every panel stays
  // mounted inside its own tabpanel regardless of this value — see the
  // markup below, `hidden={activeTab !== tab}`, never `{#if}` — so
  // switching tabs never refetches a panel's query or drops its in-progress
  // local state (an open modal, a half-typed form).
  let activeTab = $state<TabId>("memory");

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

  // The most recent status this query actually returned. Kept separately
  // from `$agent.data` because once destroy-forget completes there is no
  // agent row left (kobo_api.destroy's module docstring): `GET
  // /api/agents/{name}` starts 404ing, and `$agent.data` itself goes stale
  // rather than updating, so this is the only place that still remembers
  // what the agent's status was right before that happened.
  let lastKnownStatus = $state<string | null>(null);

  $effect(() => {
    const data = $agent.data;
    if (data) {
      lastKnownStatus = data.status;
    }
  });

  // A 404 for this agent means one of two very different things: an
  // unknown/mistyped name (never loaded anything, `lastKnownStatus` stays
  // null, falls through to the plain ErrorState below), or destruction
  // finished (this page had the agent mid-destroy, then its row vanished
  // with the last step). Only the second case is a plain notice plus a
  // trip back to the dashboard, not an error screen. The decision itself
  // is `isDestroyComplete` (status.ts), a pure, unit-tested function,
  // rather than an inline condition here.
  $effect(() => {
    if (
      isDestroyComplete(
        lastKnownStatus,
        $agent.isError,
        $agent.error instanceof ApiError ? $agent.error.status : undefined,
      )
    ) {
      toast.success("Agent destroyed", `${name} and its records are gone.`);
      navigate({ page: "dashboard" });
    }
  });

  // Cache-bust seam for the `<img>` that reads the avatar back: the GET
  // route serves whatever's cached on the agent row, and a browser will
  // otherwise keep showing the previously-cached image at the same URL
  // after a regenerate. `avatarVersion` starts null (plain URL, relies on
  // normal HTTP caching) and becomes the mutation's returned sha on
  // success, appended as a `?v=` param the route ignores but the browser
  // treats as a new URL — so the swapped-in image is never stale.
  let avatarVersion = $state<string | null>(null);
  let avatarError = $state<string | null>(null);
  let avatarNote = $state<string | null>(null);

  const avatarSrc = $derived(
    avatarVersion === null
      ? `/api/agents/${encodeURIComponent(name)}/avatar`
      : `/api/agents/${encodeURIComponent(name)}/avatar?v=${encodeURIComponent(avatarVersion)}`,
  );

  function handleGenerateAvatar(): void {
    avatarError = null;
    avatarNote = null;
    $generateAvatar.mutate(undefined, {
      onSuccess: (result) => {
        avatarVersion = result.sha;
        avatarNote = "Saved. It'll ride along with the next update.";
      },
      onError: (error: unknown) => {
        avatarError = errorMessage(error, "Couldn't paint a face this time.");
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
      {#if data.has_avatar || avatarVersion !== null}
        <img class="agent-detail__avatar" src={avatarSrc} alt="{data.name}'s picture" />
      {/if}
      <div class="agent-detail__heading">
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

      <div class="composition__avatar-action">
        <button
          type="button"
          class="composition__avatar-button"
          disabled={isFrozen(data.status) || $generateAvatar.isPending}
          onclick={handleGenerateAvatar}
        >
          {#if $generateAvatar.isPending}
            <span class="composition__avatar-dot pulse" aria-hidden="true"></span>
            Painting…
          {:else if data.has_avatar || avatarVersion !== null}
            New face
          {:else}
            Give {data.name} a face
          {/if}
        </button>
        {#if avatarError}
          <p class="composition__avatar-error" role="alert">{avatarError}</p>
        {:else if avatarNote}
          <p class="composition__avatar-note">{avatarNote}</p>
        {/if}
      </div>
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

    {#if data.status === "ready" || isFrozen(data.status)}
      {#if isFrozen(data.status)}
        <p class="agent-detail__destroy-notice" role="status" aria-live="polite">
          This agent is being destroyed.
        </p>
      {/if}

      <ChatPanel agentName={data.name} frozen={isFrozen(data.status)} />

      {@const tabs = visibleTabs(data.slack_visible)}
      <div class="detail-tabs">
        <div class="detail-tabs__bar" role="tablist" aria-label="Agent sections">
          {#each tabs as tab (tab)}
            <button
              type="button"
              role="tab"
              id={`tab-${tab}`}
              class="detail-tabs__tab"
              aria-selected={activeTab === tab}
              aria-controls={`panel-${tab}`}
              onclick={() => (activeTab = tab)}
            >
              {TAB_LABELS[tab]}
            </button>
          {/each}
        </div>

        {#each tabs as tab (tab)}
          <div
            id={`panel-${tab}`}
            role="tabpanel"
            aria-labelledby={`tab-${tab}`}
            hidden={activeTab !== tab}
            tabindex="0"
          >
            {#if tab === "memory"}
              <MemoryPanel agentName={data.name} frozen={isFrozen(data.status)} />
            {:else if tab === "skills"}
              <SkillsPanel agentName={data.name} frozen={isFrozen(data.status)} />
            {:else if tab === "tooling"}
              <ToolingPanel agentName={data.name} frozen={isFrozen(data.status)} />
            {:else}
              <SlackPanel agentName={data.name} slack={data.slack} frozen={isFrozen(data.status)} />
            {/if}
          </div>
        {/each}
      </div>
    {/if}

    {#if data.destroy_visible}
      <DangerZone agentName={data.name} status={data.status} error={data.error} />
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
    align-items: center;
    gap: var(--space-md);
  }

  .agent-detail__avatar {
    width: 4rem;
    height: 4rem;
    flex-shrink: 0;
    border-radius: 50%;
    object-fit: cover;
  }

  .agent-detail__heading {
    display: flex;
    flex-direction: column;
    min-width: 0;
    flex: 1;
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

  .composition__avatar-action {
    display: flex;
    flex-basis: 100%;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-xs);
  }

  .composition__avatar-button {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .composition__avatar-button:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .composition__avatar-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .composition__avatar-dot {
    display: inline-block;
    width: 0.5rem;
    height: 0.5rem;
    margin-right: var(--space-2xs);
    border-radius: 50%;
    background: var(--pulse);
  }

  .composition__avatar-note {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .composition__avatar-error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
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

  .detail-tabs {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
  }

  .detail-tabs__bar {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-md);
    border-bottom: 1px solid var(--border);
  }

  .detail-tabs__tab {
    padding: var(--space-sm) var(--space-xs);
    border: none;
    border-bottom: 2px solid transparent;
    background: none;
    color: var(--muted);
    font: inherit;
    font-size: 0.9rem;
    cursor: pointer;
  }

  .detail-tabs__tab:hover {
    color: var(--ink);
  }

  .detail-tabs__tab[aria-selected="true"] {
    border-bottom-color: var(--accent);
    color: var(--ink);
    font-weight: 600;
  }

  .agent-detail__destroy-notice {
    margin: 0;
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
    color: var(--muted);
    font-size: 0.9rem;
  }
</style>
