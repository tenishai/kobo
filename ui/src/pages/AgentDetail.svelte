<script lang="ts">
  // A single agent's detail (UI-simplification wave, 2026-09-02: overview
  // tab retired -- its identity content lives in the always-visible header
  // now).
  //
  // A connected agent (design 2026-09-21) shows a narrower version of this
  // same page rather than a page of its own: status, checklist, the
  // repository, owner and group, the build log, the observability links and
  // destroy. Everything the room composed -- persona, capability, skills,
  // and every tab that reads the bundle -- is ABSENT, not disabled, because
  // there is no bundle behind it and there never will be. What stays is
  // whatever is true of any workload the room deploys. The tab side of that
  // decision lives in tabs.ts, pure and unit-tested; this file only hides
  // the header lines that have no source.
  //
  // Header: back link, name, status, model, repo link, persona line,
  // team/purpose chips, capability, owner, avatar actions, and the "Edit
  // details" trigger. Below that: a failed-status retry banner, the step
  // checklist (pre-ready only), and the build log card, all above a tab bar
  // split into always-visible primary tabs plus a collapsible Advanced
  // group. The Danger zone renders below the tabs once destroy is visible.
  // All polling is `agentQuery`'s own adaptive cadence (status.ts): this
  // component only renders whatever state that query is in, same
  // loading/error/populated split Dashboard already uses.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { derived, writable } from "svelte/store";
  import AccessPanel from "../components/AccessPanel.svelte";
  import BuildLogCard from "../components/BuildLogCard.svelte";
  import RepoAccessCard from "../components/RepoAccessCard.svelte";
  import ChatPanel from "../components/ChatPanel.svelte";
  import ColleagueConversations from "../components/ColleagueConversations.svelte";
  import DangerZone from "../components/DangerZone.svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import FirstReply from "../components/FirstReply.svelte";
  import LearningsPanel from "../components/LearningsPanel.svelte";
  import MemoryPanel from "../components/MemoryPanel.svelte";
  import MetadataDetails from "../components/MetadataDetails.svelte";
  import RequestAccessCard from "../components/RequestAccessCard.svelte";
  import SecretsPanel from "../components/SecretsPanel.svelte";
  import Skeleton from "../components/Skeleton.svelte";
  import SkillsPanel from "../components/SkillsPanel.svelte";
  import SlackPanel from "../components/SlackPanel.svelte";
  import StatusBadge from "../components/StatusBadge.svelte";
  import StepChecklist from "../components/StepChecklist.svelte";
  import ThreadPanel from "../components/ThreadPanel.svelte";
  import ToolingPanel from "../components/ToolingPanel.svelte";
  import { ApiError, errorMessage, KIND_CONNECTED, RUN_MODE_HTTP } from "../lib/api";
  import {
    agentQuery,
    convertToConnectedMutation,
    generateAvatarMutation,
    invitesQuery,
    observabilityQuery,
    personasSearchQuery,
    retryAgentMutation,
    uploadAvatarMutation,
  } from "../lib/queries";
  import { formatHash, navigate, route } from "../lib/router.svelte";
  import { isDestroyComplete, isFrozen } from "../lib/status";
  import { advancedTabs, defaultTab, primaryTabs, TAB_LABELS, type TabId } from "../lib/tabs";
  import { toast } from "../lib/toast.svelte";

  // Invariant: App.svelte only mounts this page when `route.page === "agent"`,
  // and the router (router.svelte.ts) only ever sets that page together with
  // a non-null `name` — so this assertion just names that contract rather
  // than re-deriving it.
  const name = route.name as string;

  const queryClient = useQueryClient();
  const agent = createQuery(() => (agentQuery(name)));

  // The Access tab's own visibility probe (access-control Task C2): fired
  // independently of which tab is selected, and independently of the Access
  // tab even existing yet, since that's exactly what this decides. A 200
  // means the caller is this agent's owner or a room operator (the Access
  // tab renders, see `primary` below); a 403 means the agent is visible but
  // not theirs to administer (the "Request colleague access" card renders
  // instead, right below); anything else (a 404, in practice unreachable
  // here since a 404 on the AGENT itself would already have kept this page
  // from ever mounting `agent.data`) renders neither. Deliberately never
  // keyed on `whoami.operator` -- see `Whoami`'s own WATCH ITEM doc comment
  // in api.ts and `primaryTabs`'s doc comment in tabs.ts -- this tab is
  // exactly as real as the server's own gate on this route, nothing a
  // client-side flag could fake or fall out of sync with.
  const invites = createQuery(() => (invitesQuery(name)));

  // The org's observability catalog (Task 6, observability-catalog
  // addendum): a dashboard base URL + project, both `null` unless the room
  // config carries both -- see `observabilityQuery`'s own doc comment.
  // Shares its cache entry with every AgentCard mounted on the dashboard
  // grid, so this costs no extra network request when the list was visited
  // first.
  const observability = createQuery(() => observabilityQuery());
  let dashboardUrl = $derived(
    observability.data?.url && observability.data?.project
      ? `${observability.data.url}/projects/${observability.data.project}/dashboards/${name}-agent-health`
      : null,
  );

  // Soul-line (persona-naming law, hardening wave): the composition card's
  // Persona line used to print the bare persona id (`data.persona`)
  // plainly. Now it looks the id up in a fresh persona catalog search and
  // renders `{name} -- {who_they_are}` instead -- the naming law's display
  // hierarchy made visible on an agent's own page, where the AGENT's name
  // stays the one real name (the `<h1>` below) and the persona is a
  // secondary grey line, never a co-equal title. Falls back to the bare id
  // when the search comes back with no matching entry (an archived or
  // deleted-from-catalog persona) -- see `personaLine` in the markup below.
  //
  // The search term (the agent's own persona id) isn't known until
  // `agent.data` first resolves, so this uses the same writable/derived
  // store idiom SlackPanel's `manifestOpened`/`manifestOptions` documents (a
  // plain `$derived` options object wouldn't re-fire `createQuery` under the
  // installed @tanstack/svelte-query adapter) -- `enabled` is overridden
  // locally here rather than added to `personasSearchQuery` itself, since
  // every other caller of that shared factory always has a real (possibly
  // empty) search string in hand already.
  let personaSearchTerm = $state<string | null>(null);
  $effect(() => {
    const data = agent.data;
    if (data) {
      personaSearchTerm = data.persona;
    }
  });
  const personaSoul = createQuery(() => (({
    ...personasSearchQuery(personaSearchTerm ?? "", null),
    enabled: personaSearchTerm !== null,
  })));

  // "Convert to connected" (connected-agents design 2026-09-21): the one
  // operator action for an agent the room composed whose repository has
  // stopped running the room's runtime. Lives here rather than inside
  // DangerZone because it is not a destruction: nothing is torn down, and
  // folding it into the destroy card would put a one-way change behind a
  // heading people already read as "do not touch".
  //
  // Whether to offer it at all is read off `destroy_visible`, the operator
  // flag the detail already carries: the server's convert gate is the same
  // operator gate that flag is computed from (`_convert_operator_block`
  // beside `_operator_block`, main.py, with a test pinning the two
  // together). The route stays the real enforcement boundary either way,
  // exactly as the Danger zone's own comment says of itself.
  const convert = createMutation(() => (convertToConnectedMutation(queryClient, name)));
  let convertConfirming = $state(false);
  let convertError = $state<string | null>(null);

  // There is no run mode to choose. An agent the room composed serves web
  // traffic by construction, and the route refuses anything else, so asking
  // would only offer an answer the room is about to turn down.
  function handleConvert(): void {
    if (convert.isPending) {
      return;
    }
    convertError = null;
    convert.mutate(RUN_MODE_HTTP, {
      onSuccess: () => {
        convertConfirming = false;
        toast.success("Converted", `The room no longer edits ${name}.`);
      },
      onError: (error: unknown) => {
        convertError = errorMessage(error, "Couldn't convert this agent. Try again.");
      },
    });
  }

  const retry = createMutation(() => (retryAgentMutation(queryClient, name)));
  const generateAvatar = createMutation(() => (generateAvatarMutation(queryClient, name)));
  const uploadAvatar = createMutation(() => (uploadAvatarMutation(queryClient, name)));

  // Which tab (tabs.ts) is showing. Every panel stays mounted inside its own
  // tabpanel regardless of this value — see the markup below,
  // `hidden={activeTab !== tab}`, never `{#if}` — so switching tabs never
  // refetches a panel's query or drops its in-progress local state (an open
  // modal, a half-typed form).
  //
  // Starts `null` (no data yet, and "no tab" is also a real, permanent state
  // now for any non-ready status — see `defaultTab`, tabs.ts) so the effect
  // below can tell "never chosen" apart from "chosen" — the first real
  // `agent.data` picks the smart default exactly once, and never again even
  // as later polls bring fresher statuses.
  let activeTab = $state<TabId | null>(null);

  // Whether the Advanced tab group (tabs.ts: learnings/memory/skills/
  // tooling) is expanded. Collapsed by default for EVERYONE (operator
  // ruling, 2026-09-02 — an earlier cut seeded it open for deciders): the
  // pending-learnings badge on the toggle carries the signal, and the
  // primary row stays short.
  let advancedOpen = $state(false);

  // Also re-picks when the selected tab stops existing. A convert is the
  // case that made this necessary: the poll after it brings back a kind
  // with no Chat, and the page was left with "chat" still selected and an
  // empty panel area under a tab bar that no longer offers it. Tabs can
  // only vanish, never appear, without a data change, so this stays the
  // same one-shot pick for every other poll.
  $effect(() => {
    const data = agent.data;
    if (!data) {
      return;
    }
    const visible = [
      ...primaryTabs(invites.isSuccess, data.kind),
      ...advancedTabs(data.kind),
    ];
    if (activeTab === null || !visible.includes(activeTab)) {
      activeTab = defaultTab(data.status, data.kind);
    }
  });

  function handleRetry(): void {
    retry.mutate(undefined, {
      onSuccess: () => {
        toast.success("Retrying", `Re-running ${name}'s setup…`);
      },
      onError: (error: unknown) => {
        toast.error("Retry failed", errorMessage(error, "Something went wrong."));
      },
    });
  }

  // The most recent status this query actually returned. Kept separately
  // from `agent.data` because once destroy-forget completes there is no
  // agent row left (kobo_api.destroy's module docstring): `GET
  // /api/agents/{name}` starts 404ing, and `agent.data` itself goes stale
  // rather than updating, so this is the only place that still remembers
  // what the agent's status was right before that happened.
  let lastKnownStatus = $state<string | null>(null);

  $effect(() => {
    const data = agent.data;
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
        agent.isError,
        agent.error instanceof ApiError ? agent.error.status : undefined,
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
    generateAvatar.mutate(undefined, {
      onSuccess: (result) => {
        avatarVersion = result.sha;
        avatarNote = "Saved. It'll ride along with the next update.";
      },
      onError: (error: unknown) => {
        avatarError = errorMessage(error, "Couldn't paint a face this time.");
      },
    });
  }

  // Mirrors the server's own upload cap (kobo_api.main.AVATAR_UPLOAD_MAX_BYTES)
  // and its exact copy, checked here BEFORE anything is read off disk or
  // posted, so an oversized file never even makes the round trip. A file
  // that clears this check can still come back a 422 from the server (wrong
  // content type, or bytes that aren't actually PNG-shaped) — that error
  // passes through verbatim via `errorMessage` below, same as every other
  // server-side rejection this page surfaces.
  const AVATAR_UPLOAD_MAX_BYTES = 2 * 1024 * 1024;
  const AVATAR_TOO_LARGE_MESSAGE = "That image is too large. Two megabytes at most.";

  /** Reads the chosen file's bytes (FileReader, same idiom MemoryPanel/
   * ToolingPanel use for their own file inputs, `readAsArrayBuffer` rather
   * than `readAsText` since a PNG isn't text) and PUTs them once read. The
   * input's own value is cleared right away so picking the exact same file
   * again (e.g. after shrinking it) still fires a fresh `change` event. */
  function handleAvatarFileSelect(event: Event): void {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    input.value = "";
    if (!file) {
      return;
    }

    avatarError = null;
    avatarNote = null;

    if (file.size > AVATAR_UPLOAD_MAX_BYTES) {
      avatarError = AVATAR_TOO_LARGE_MESSAGE;
      return;
    }

    const reader = new FileReader();
    reader.onload = () => {
      const bytes = reader.result as ArrayBuffer;
      uploadAvatar.mutate(bytes, {
        onSuccess: (result) => {
          avatarVersion = result.sha;
          avatarNote = "Saved. It'll ride along with the next update.";
        },
        onError: (error: unknown) => {
          avatarError = errorMessage(error, "Couldn't save that picture. Try again.");
        },
      });
    };
    reader.onerror = () => {
      avatarError = "Couldn't read this file. Try another.";
    };
    reader.readAsArrayBuffer(file);
  }
</script>

<div class="agent-detail">
  <a class="agent-detail__back" href={formatHash({ page: "dashboard" })}>
    ← Back to dashboard
  </a>

  {#if agent.isPending}
    <Skeleton rows={6} />
  {:else if agent.isError}
    <ErrorState
      message={errorMessage(agent.error, "Couldn't load this agent.")}
      onretry={() => agent.refetch()}
    />
  {:else if agent.data}
    {@const data = agent.data}
    {@const connected = data.kind === KIND_CONNECTED}
    {@const personaMatch = personaSoul.data?.find((p) => p.id === data.persona) ?? null}
    {@const personaLine = personaMatch
      ? `${personaMatch.name} -- ${personaMatch.description}`
      : data.persona}
    <header class="agent-detail__header">
      {#if data.has_avatar || avatarVersion !== null}
        <img class="agent-detail__avatar" src={avatarSrc} alt="{data.name}'s picture" />
      {/if}
      <div class="agent-detail__heading">
        <div class="agent-detail__title">
          <h1 class="agent-detail__name">{data.name}</h1>
          <StatusBadge status={data.status} />
          <!-- The same marker the dashboard card carries, in the same
               words, so the two pages agree about what this agent is. -->
          {#if connected}
            <span class="agent-detail__chip">connected</span>
          {/if}
        </div>
        <div class="agent-detail__meta">
          <span class="agent-detail__model" title={data.model}>{data.model_label}</span>
          {#if data.repo_full_name}
            <span class="agent-detail__sep" aria-hidden="true">·</span>
            <!-- A connected agent's page names the repository rather than
                 saying "source": it is somebody else's repository, and
                 which one it is is the main fact about this agent. -->
            <a
              class="agent-detail__repo"
              href={`https://github.com/${data.repo_full_name}`}
              target="_blank"
              rel="noreferrer"
            >
              {connected ? data.repo_full_name : "source"}
            </a>
          {/if}
        </div>
        {#if !connected}
          <p class="agent-detail__persona">
            {personaLine}
            {#if data.team && data.team !== "shared"}
              <span class="agent-detail__chip">{data.team}</span>
            {/if}
          </p>
        {/if}
        <!-- Purpose chips are drafted from the persona at create time and
             edited through Edit details, neither of which a connected agent
             has. A converted agent (Task 6) keeps whatever tags its row
             already carried, so this guard is what keeps them off a page
             that offers no way to change them. -->
        {#if data.purpose_tags.length > 0 && !connected}
          <p class="agent-detail__tags">
            {#each data.purpose_tags as tag (tag)}
              <span class="agent-detail__chip">{tag}</span>
            {/each}
          </p>
        {/if}
        <!-- Skill chips returned to the header (operator ruling, 2026-09-02):
             with Skills living behind the Advanced group, a visitor browsing
             someone else's agent would otherwise see nothing about what it
             can do. Chips only; managing skills stays in the Skills tab. -->
        {#if data.skills.length > 0 && !connected}
          <p class="agent-detail__skills">
            {#each data.skills as skill (skill.name)}
              <span class="agent-detail__chip agent-detail__chip--skill">{skill.name}</span>
            {/each}
          </p>
        {/if}
        <!-- The capability sentence is earned at first contact, and the
             room never makes first contact with a connected agent, so for
             one it would be a promise that never arrives. -->
        {#if !connected}
          <p class="agent-detail__capability">
            {data.capability ?? "Not written yet. It arrives after first contact."}
          </p>
        {/if}
        {#if dashboardUrl}
          <p class="agent-detail__observability">
            Observability:
            <a
              class="agent-detail__repo"
              href={dashboardUrl}
              target="_blank"
              rel="noreferrer"
            >
              {name}-agent-health
            </a>
          </p>
        {/if}
        <!-- Who owns this agent, in one line: the person who made it and
             the group it belongs to. The group used to be visible only
             inside the Access tab, which a connected agent's owner may not
             have at all, and it is the same ownership fact for both kinds,
             so it is shown for both rather than only where it was missed. -->
        {#if data.created_by || data.group_name}
          <p class="agent-detail__owner">
            {#if data.created_by}
              <span>Made by {data.created_by}</span>
            {/if}
            {#if data.group_name}
              <span class="agent-detail__chip">{data.group_name}</span>
            {/if}
          </p>
        {/if}
      </div>
    </header>

    <!-- Portrait actions live BELOW the header row, not beside the avatar:
         inside the header's flex row their three-button run refused to
         shrink and squeezed the identity column to a sliver (2026-09-02
         layout fix, from the first live look at the rolled page). -->
    <div class="agent-detail__portrait-actions">
      <div class="composition__avatar-buttons">
        <button
          type="button"
          class="composition__avatar-button"
          disabled={isFrozen(data.status) || generateAvatar.isPending}
          onclick={handleGenerateAvatar}
        >
          {#if generateAvatar.isPending}
            <span class="composition__avatar-dot pulse" aria-hidden="true"></span>
            Painting…
          {:else if data.has_avatar || avatarVersion !== null}
            New face
          {:else}
            Give {data.name} a face
          {/if}
        </button>

        <label
          class="composition__avatar-upload"
          class:composition__avatar-upload--disabled={isFrozen(data.status) ||
            uploadAvatar.isPending}
        >
          {uploadAvatar.isPending ? "Uploading…" : "Upload a portrait"}
          <input
            class="composition__avatar-upload-input"
            type="file"
            accept=".png,image/png"
            disabled={isFrozen(data.status) || uploadAvatar.isPending}
            onchange={handleAvatarFileSelect}
          />
        </label>

        {#if data.has_avatar || avatarVersion !== null}
          <a
            class="composition__avatar-download"
            href={avatarSrc}
            download={`${data.name}-portrait.png`}
          >
            Download the portrait
          </a>
        {/if}
      </div>
      {#if avatarError}
        <p class="composition__avatar-error" role="alert">{avatarError}</p>
      {:else if avatarNote}
        <p class="composition__avatar-note">{avatarNote}</p>
      {/if}
    </div>

    <!-- Edit details writes the three fields above (team, purpose chips,
         capability), all of which describe a bundle the room composed. A
         connected agent has none of them on its page, so there is nothing
         here for it to edit. -->
    {#if !connected}
      <div class="composition__edit-action">
        <MetadataDetails
          agentName={data.name}
          team={data.team}
          purposeTags={data.purpose_tags}
          capability={data.capability}
          frozen={isFrozen(data.status)}
        />
      </div>
    {/if}

    {#if isFrozen(data.status)}
      <p class="agent-detail__destroy-notice" role="status" aria-live="polite">
        This agent is being destroyed.
      </p>
    {/if}

    {#if invites.isError && invites.error instanceof ApiError && invites.error.status === 403}
      <RequestAccessCard toAgentName={data.name} />
    {/if}

    {#if data.status === "failed"}
      <div class="failed-banner" role="alert">
        <p class="failed-banner__message">
          {data.error ?? "Something went wrong during setup."}
        </p>
        <button
          type="button"
          class="failed-banner__retry"
          disabled={retry.isPending}
          onclick={handleRetry}
        >
          {retry.isPending ? "Retrying…" : "Retry"}
        </button>
      </div>
    {/if}

    {#if data.status !== "ready" && !isFrozen(data.status)}
      <StepChecklist steps={data.steps} />
    {/if}

    <BuildLogCard agentName={data.name} />

    {@const primary = primaryTabs(invites.isSuccess, data.kind)}
    {@const advanced = advancedTabs(data.kind)}
    {@const tabDisabled = data.status !== "ready" && !isFrozen(data.status)}
    <!-- A connected agent whose caller cannot administer it has no tabs at
         all, and an empty tab bar with an Advanced toggle beside it would
         read as a row that failed to load. -->
    {#if primary.length > 0 || advanced.length > 0}
    <div class="detail-tabs">
      <div class="detail-tabs__bar">
        <div class="detail-tabs__list" role="tablist" aria-label="Agent sections">
          <!-- Every panel mounts regardless of status or the Advanced group's
               collapse (see the tabpanel loop below), but its query still
               409s server-side until the agent is `ready` — so the trigger
               itself is disabled rather than left clickable into a dead end,
               for every status that is neither ready nor frozen (pending/
               provisioning/failed). Frozen statuses (destroying/
               destroy_failed) are the one exception: destruction makes
               read-only content reachable again, so only mutation CONTROLS
               freeze there (each panel's own `frozen` prop below) — the tabs
               themselves stay fully clickable. -->
          {#each primary as tab (tab)}
            <button
              type="button"
              role="tab"
              id={`tab-${tab}`}
              class="detail-tabs__tab"
              aria-selected={activeTab === tab}
              aria-controls={`panel-${tab}`}
              disabled={tabDisabled}
              onclick={() => {
                if (!tabDisabled) {
                  activeTab = tab;
                }
              }}
            >
              {TAB_LABELS[tab]}
            </button>
          {/each}
          {#if advancedOpen}
            {#each advanced as tab (tab)}
              <button
                type="button"
                role="tab"
                id={`tab-${tab}`}
                class="detail-tabs__tab"
                aria-selected={activeTab === tab}
                aria-controls={`panel-${tab}`}
                disabled={tabDisabled}
                onclick={() => {
                  if (!tabDisabled) {
                    activeTab = tab;
                  }
                }}
              >
                {TAB_LABELS[tab]}
                {#if tab === "learnings" && data.pending_learnings > 0}
                  <span class="detail-tabs__badge">{data.pending_learnings}</span>
                {/if}
              </button>
            {/each}
          {/if}
        </div>
        <!-- No toggle when the group behind it is empty (a connected
             agent): it would open onto nothing. The pending-learnings
             count rides on this toggle, and a connected agent has no
             learnings either, so it goes with it. -->
        {#if advanced.length > 0}
          <button
            type="button"
            class="detail-tabs__advanced-toggle"
            aria-expanded={advancedOpen}
            onclick={() => {
              advancedOpen = !advancedOpen;
              // `advanced`, not the ADVANCED_TABS constant: the group's
              // membership now depends on the kind of agent (a connected
              // one keeps Secrets and nothing else), and collapsing the
              // group has to clear whichever of THOSE is selected.
              if (!advancedOpen && activeTab !== null
                  && (advanced as readonly string[]).includes(activeTab)) {
                activeTab = defaultTab(data.status, data.kind);
              }
            }}
          >
            Advanced {advancedOpen ? "▾" : "▸"}
            {#if !advancedOpen && data.pending_learnings > 0}
              <span class="detail-tabs__badge">{data.pending_learnings}</span>
            {/if}
          </button>
        {/if}
      </div>

      {#each [...primary, ...advanced] as tab (tab)}
        <div
          id={`panel-${tab}`}
          class="detail-tabs__panel"
          role="tabpanel"
          aria-labelledby={`tab-${tab}`}
          hidden={activeTab !== tab}
          tabindex="0"
        >
          {#if tab === "chat"}
            {#if data.first_reply}
              <FirstReply reply={data.first_reply} />
            {/if}
            <ChatPanel agentName={data.name} frozen={isFrozen(data.status)} />
            <ThreadPanel agentName={data.name} frozen={isFrozen(data.status)} />
            <ColleagueConversations agentName={data.name} />
          {:else if tab === "memory"}
            <MemoryPanel agentName={data.name} frozen={isFrozen(data.status)} />
          {:else if tab === "learnings"}
            <LearningsPanel
              agentName={data.name}
              canDecide={data.can_decide_learnings}
              frozen={isFrozen(data.status)}
            />
          {:else if tab === "skills"}
            <SkillsPanel
              agentName={data.name}
              frozen={isFrozen(data.status)}
              recommendWho={data.capability ?? ""}
              recommendWhat={data.purpose_tags.join(" ")}
            />
          {:else if tab === "tooling"}
            <ToolingPanel agentName={data.name} frozen={isFrozen(data.status)} />
          {:else if tab === "secrets"}
            <!-- `active`: every tabpanel here stays mounted when another
                 tab is picked, and this panel's two reads reach the
                 secrets platform and the agent's repository. Without it,
                 opening any agent page would ask about secrets nobody was
                 looking at, and the platform's audit would say somebody
                 did. -->
            <SecretsPanel
              agentName={data.name}
              frozen={isFrozen(data.status)}
              active={activeTab === "secrets"}
              deployed={data.status === "ready"}
            />
          {:else if tab === "slack"}
            <SlackPanel
              agentName={data.name}
              slack={data.slack}
              frozen={isFrozen(data.status)}
            />
          {:else}
            <AccessPanel
              agentName={data.name}
              groupName={data.group_name}
              frozen={isFrozen(data.status)}
            />
            <RepoAccessCard agentName={data.name} frozen={isFrozen(data.status)} />
          {/if}
        </div>
      {/each}
    </div>
    {/if}

    <!-- Operator-only, and only for an agent the room composed that has
         settled: a pending or provisioning agent is still being built, and
         a destroying one is on its way out. -->
    {#if !connected && data.destroy_visible && (data.status === "ready" || data.status === "failed")}
      <section class="convert-card" aria-labelledby="convert-heading">
        <h2 id="convert-heading" class="convert-card__heading">Convert to connected</h2>
        <p class="convert-card__lead">
          For an agent whose repository no longer runs the room's runtime.
        </p>
        {#if !convertConfirming}
          <button
            type="button"
            class="convert-card__open"
            onclick={() => {
              convertError = null;
              convertConfirming = true;
            }}
          >
            Convert to connected
          </button>
        {:else}
          <p class="convert-card__copy">
            The room stops editing this agent: no chat, memory, learnings, skills, tooling or Slack from here, and other agents can no longer reach it. Its repository, deployment and cloud identity stay as they are. This cannot be undone from the room.
          </p>
          <div class="convert-card__actions">
            <button
              type="button"
              class="convert-card__confirm"
              disabled={convert.isPending}
              onclick={handleConvert}
            >
              {convert.isPending ? "Converting…" : "Convert this agent"}
            </button>
            <button
              type="button"
              class="convert-card__cancel"
              disabled={convert.isPending}
              onclick={() => {
                convertConfirming = false;
                convertError = null;
              }}
            >
              Cancel
            </button>
          </div>
        {/if}
        {#if convertError}
          <p class="convert-card__error" role="alert">{convertError}</p>
        {/if}
      </section>
    {/if}

    {#if data.destroy_visible}
      <DangerZone
        agentName={data.name}
        status={data.status}
        error={data.error}
        kind={data.kind}
      />
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
    align-items: flex-start;
    gap: var(--space-md);
  }

  .agent-detail__portrait-actions {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    margin-top: var(--space-sm);
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

  /* Soul-line (persona-naming law): the Persona line reads as a secondary
     grey line (name + who-they-are), not the ink-toned page title -- the
     agent's own name (the page's `<h1>`) stays the one real title. */
  .agent-detail__persona {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2xs);
    margin: 0;
    color: var(--muted);
    font-size: 0.9rem;
  }

  .agent-detail__tags,
  .agent-detail__skills {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2xs);
    margin: 0;
  }

  .agent-detail__chip--skill {
    color: var(--ink);
  }

  .agent-detail__chip {
    display: inline-flex;
    padding: 0.1rem var(--space-2xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.7rem;
    white-space: nowrap;
  }

  .agent-detail__capability {
    margin: 0;
    color: var(--ink);
    font-size: 0.9rem;
  }

  .agent-detail__owner {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2xs);
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .agent-detail__observability {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .composition__edit-action {
    display: flex;
  }

  .composition__avatar-buttons {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
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

  .composition__avatar-upload {
    display: inline-flex;
    align-items: center;
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-weight: 600;
    cursor: pointer;
  }

  .composition__avatar-upload:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .composition__avatar-upload--disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .composition__avatar-upload-input {
    /* Visually hidden, not `display: none` — a `display: none` input never
       fires focus/keyboard events, which would make the label-wrapped
       control unreachable by keyboard. */
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    opacity: 0;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  .composition__avatar-download {
    display: inline-flex;
    align-items: center;
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-weight: 600;
    text-decoration: none;
    cursor: pointer;
  }

  .composition__avatar-download:hover {
    border-color: var(--accent);
    color: var(--accent);
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

  /* The row WRAPS rather than scrolls: overflow-x:auto hid the Advanced
     toggle entirely at common window widths (macOS shows no scrollbar),
     which read as the control not existing (live finding, 2026-09-02). */
  .detail-tabs__bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-md);
    border-bottom: 1px solid var(--border);
  }

  .detail-tabs__list {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-md);
  }

  .detail-tabs__tab {
    flex-shrink: 0;
    padding: var(--space-sm) var(--space-md);
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

  .detail-tabs__tab:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  /* A quieter sibling of `.detail-tabs__tab` -- no underline-on-select
     styling of its own (it isn't a `role="tab"`, just the group's show/hide
     switch), and a divider margin sets it apart from the primary run of
     tabs to its left. */
  .detail-tabs__advanced-toggle {
    flex-shrink: 0;
    align-self: center;
    margin-left: var(--space-sm);
    padding: 0.2rem var(--space-md);
    border: 1px solid var(--border);
    border-radius: 999px;
    background: none;
    color: var(--ink);
    font: inherit;
    font-size: 0.85rem;
    cursor: pointer;
  }

  .detail-tabs__advanced-toggle:hover {
    color: var(--ink);
  }

  .detail-tabs__badge {
    display: inline-block;
    min-width: 1.2em;
    margin-left: 0.35em;
    padding: 0 0.35em;
    border-radius: 999px;
    background: var(--accent);
    color: var(--accent-ink);
    font-size: 0.72rem;
    line-height: 1.5;
    text-align: center;
  }

  .detail-tabs__panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
  }

  /* An author-level `display` beats the UA stylesheet's `[hidden]` rule,
     so without this the inactive panels render stacked and tab switches
     only move the underline. */
  .detail-tabs__panel[hidden] {
    display: none;
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

  /* The same card idiom as the Danger zone below it, without the `--fail`
     heading: this changes what the room will do next, it destroys
     nothing. */
  .convert-card {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .convert-card__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .convert-card__lead,
  .convert-card__copy {
    margin: 0;
    color: var(--muted);
    font-size: 0.9rem;
  }

  .convert-card__actions {
    display: flex;
    gap: var(--space-sm);
  }

  .convert-card__open,
  .convert-card__confirm {
    align-self: flex-start;
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .convert-card__open:hover:not(:disabled),
  .convert-card__confirm:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .convert-card__open:disabled,
  .convert-card__confirm:disabled,
  .convert-card__cancel:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .convert-card__cancel {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--ink);
    font-weight: 600;
    cursor: pointer;
  }

  .convert-card__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }
</style>
