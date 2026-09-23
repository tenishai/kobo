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
  // Tabs (harness wave, 2026-09-23): five on one row, no menu at the end of
  // it. Chat, Slack, Access and Settings are what they were; Harness is the
  // new one, and it holds the five sections that used to be tabs hidden
  // behind More -- persona, skills, tooling, memory, learnings -- plus the
  // model and the runtime image the agent actually runs on. Secrets is a
  // section of Settings now, above the build log. Which tab is showing, and
  // which Harness section, both live in the hash:
  // `#/agents/<name>/harness/skills`.
  //
  // Header (page-shell wave, 2026-09-23): two lines, not eight. Line one is
  // the avatar, the name, the status, the connected marker, and on the far
  // right one overflow menu holding every action the header used to spell
  // out in filled buttons. Line two is a single muted run -- model, repo,
  // group, who made it, persona, observability -- with the capability
  // sentence as the one ink line under it and at most three chips below
  // that. The rest of an agent's identity lives in Edit details, which is
  // where it is edited. Below the header: a failed-status retry banner and
  // the step checklist (pre-ready only), above the tab bar. The Danger zone
  // and Convert both moved into the Settings tab.
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
  import ErrorState from "../components/ErrorState.svelte";
  import FirstReply from "../components/FirstReply.svelte";
  import HarnessPanel from "../components/HarnessPanel.svelte";
  import MetadataDetails from "../components/MetadataDetails.svelte";
  import RequestAccessCard from "../components/RequestAccessCard.svelte";
  import SecretsPanel from "../components/SecretsPanel.svelte";
  import Skeleton from "../components/Skeleton.svelte";
  import SettingsPanel from "../components/SettingsPanel.svelte";
  import SlackPanel from "../components/SlackPanel.svelte";
  import StatusBadge from "../components/StatusBadge.svelte";
  import StepChecklist from "../components/StepChecklist.svelte";
  import ThreadPanel from "../components/ThreadPanel.svelte";
  import { ApiError, errorMessage, KIND_CONNECTED } from "../lib/api";
  import {
    agentQuery,
    generateAvatarMutation,
    invitesQuery,
    observabilityQuery,
    personasSearchQuery,
    retryAgentMutation,
    uploadAvatarMutation,
  } from "../lib/queries";
  import { formatHash, navigate, route } from "../lib/router.svelte";
  import { isDestroyComplete, isFrozen } from "../lib/status";
  import {
    DEFAULT_HARNESS_SECTION,
    defaultTab,
    harnessSections,
    isHarnessSection,
    isTabId,
    primaryTabs,
    TAB_LABELS,
    type HarnessSectionId,
    type TabId,
  } from "../lib/tabs";
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

  // The header's one overflow menu (page-shell wave, 2026-09-23). Every
  // action that used to sit in the header as its own control -- three
  // portrait buttons, one of them the page's only filled accent button, and
  // a standalone Edit details row -- is an item in here now, so the header
  // is two lines of fact and one button that does not compete with them.
  //
  // The menu deliberately does NOT close when a portrait action is picked:
  // "Painting…" and "Uploading…" are the labels of the items themselves,
  // and closing the menu the moment somebody presses one would take the
  // only progress they have away from them. It closes on its own button, on
  // Escape, on a click outside it, and on Edit details (which opens a modal
  // of its own over the page).
  let menuOpen = $state(false);
  let menuEl = $state<HTMLElement | undefined>();

  // Edit details is a menu ITEM, so the modal behind it cannot live inside
  // the menu: closing the menu would unmount the modal with it. The
  // component is mounted beside the header with its own trigger turned off,
  // and the menu item calls into it (`openEditor`, an instance export).
  let metadata = $state<ReturnType<typeof MetadataDetails> | undefined>();

  $effect(() => {
    if (!menuOpen) {
      return;
    }
    const onPointerDown = (event: MouseEvent) => {
      if (menuEl && !menuEl.contains(event.target as Node)) {
        menuOpen = false;
      }
    };
    const onKeydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        menuOpen = false;
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeydown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeydown);
    };
  });

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

  /** Which section of the Harness tab is showing (tabs.ts). Bound into
   * HarnessPanel, which moves it on a click; kept up here because the hash
   * carries it and the hash belongs to the page. */
  let harnessSection = $state<HarnessSectionId>(DEFAULT_HARNESS_SECTION);

  // Whether a tab can be picked at all: the same condition the checklist
  // has always had -- not ready, and not on its way out. Named here as well
  // as in the markup because the hash is only honoured when tabs are live;
  // a bookmarked `#/agents/x/harness` on an agent that is still building
  // would otherwise light up a panel whose every query 409s.
  const tabDisabled = $derived(
    agent.data ? agent.data.status !== "ready" && !isFrozen(agent.data.status) : true,
  );

  // The hash decides, when it names a tab this agent actually has; otherwise
  // the smart default does, exactly as before. Also re-picks when the
  // selected tab stops existing: a convert is the case that made that
  // necessary -- the poll after it brings back a kind with no Chat, and the
  // page was left with "chat" still selected and an empty panel area under a
  // tab bar that no longer offers it.
  $effect(() => {
    const data = agent.data;
    if (!data) {
      return;
    }
    const visible = primaryTabs(invites.isSuccess, data.kind);
    const fromHash =
      !tabDisabled && isTabId(route.tab) && visible.includes(route.tab) ? route.tab : null;
    if (fromHash !== null) {
      activeTab = fromHash;
      if (
        fromHash === "harness" &&
        isHarnessSection(route.section) &&
        harnessSections(data.kind).includes(route.section)
      ) {
        harnessSection = route.section;
      }
      return;
    }
    if (activeTab === null || !visible.includes(activeTab)) {
      activeTab = defaultTab(data.status, data.kind);
    }
  });

  // ...and the hash follows whatever is showing, so the tab and the section
  // are as linkable as the agent itself. Writing a hash the router then
  // parses back into the same tab is a no-op for the effect above, so the
  // two settle rather than chase each other.
  $effect(() => {
    if (activeTab === null) {
      return;
    }
    navigate({
      page: "agent",
      name,
      tab: activeTab,
      section: activeTab === "harness" ? harnessSection : undefined,
    });
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
    {@const personaName = personaMatch ? personaMatch.name : data.persona}
    <!-- Purpose and skills read as one run of chips, because to a visitor
         they are one fact: what this agent is for. Three of them, then a
         count -- the full set is in Edit details, which is where it is
         changed. -->
    {@const chips = connected
      ? []
      : [
          ...data.purpose_tags.map((tag) => ({ text: tag, skill: false })),
          ...data.skills.map((skill) => ({ text: skill.name, skill: true })),
        ]}
    {@const shownChips = chips.slice(0, 3)}
    {@const hiddenChips = chips.length - shownChips.length}
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
          <div class="agent-detail__menu" bind:this={menuEl}>
            <button
              type="button"
              class="agent-detail__menu-button"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              aria-label="More actions"
              onclick={() => (menuOpen = !menuOpen)}
            >
              <span aria-hidden="true">⋯</span>
            </button>
            {#if menuOpen}
              <div class="agent-detail__menu-list">
                <button
                  type="button"
                  class="agent-detail__menu-item"
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
                  class="agent-detail__menu-item"
                  class:agent-detail__menu-item--disabled={isFrozen(data.status) ||
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
                    class="agent-detail__menu-item"
                    href={avatarSrc}
                    download={`${data.name}-portrait.png`}
                    onclick={() => (menuOpen = false)}
                  >
                    Download the portrait
                  </a>
                {/if}

                {#if !connected}
                  <hr class="agent-detail__menu-rule" />
                  <!-- The editor lives in the Harness tab's Persona section
                       now, so this brings the page there before opening it:
                       a modal inside a hidden panel is a modal nobody can
                       see. -->
                  <button
                    type="button"
                    class="agent-detail__menu-item"
                    disabled={isFrozen(data.status)}
                    onclick={() => {
                      menuOpen = false;
                      activeTab = "harness";
                      harnessSection = "persona";
                      metadata?.openEditor();
                    }}
                  >
                    Edit details
                  </button>
                {/if}
              </div>
            {/if}
          </div>
        </div>
        <!-- One run of facts, one separator, in the order somebody reads
             them: what it runs on, where its code is, who it belongs to,
             who made it, who it is, and where to watch it. -->
        <div class="agent-detail__meta">
          <span class="agent-detail__model" title={data.model}>{data.model_label}</span>
          {#if data.repo_full_name}
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
          {#if data.group_name}
            <span>{data.group_name}</span>
          {/if}
          {#if data.created_by}
            <span>Made by {data.created_by}</span>
          {/if}
          {#if !connected}
            <!-- Soul-line (persona-naming law): the persona's NAME, with
                 who-they-are on the hover. The whole line was its own grey
                 row until this wave; in a one-line run it would crowd out
                 every other fact beside it. -->
            <span class="agent-detail__persona" title={personaLine}>{personaName}</span>
          {/if}
          {#if dashboardUrl}
            <a
              class="agent-detail__repo"
              href={dashboardUrl}
              target="_blank"
              rel="noreferrer"
              title={`${name}-agent-health`}
            >
              Observability
            </a>
          {/if}
        </div>
        <!-- The capability sentence is earned at first contact, and the
             room never makes first contact with a connected agent, so for
             one it would be a promise that never arrives. -->
        {#if !connected}
          <p class="agent-detail__capability">
            {data.capability ?? "Not written yet. It arrives after first contact."}
          </p>
        {/if}
        {#if !connected && (chips.length > 0 || (data.team && data.team !== "shared"))}
          <p class="agent-detail__tags">
            {#if data.team && data.team !== "shared"}
              <span class="agent-detail__chip">{data.team}</span>
            {/if}
            {#each shownChips as chip (chip.text)}
              <span
                class="agent-detail__chip"
                class:agent-detail__chip--skill={chip.skill}
              >
                {chip.text}
              </span>
            {/each}
            {#if hiddenChips > 0}
              <span class="agent-detail__chip">+{hiddenChips}</span>
            {/if}
          </p>
        {/if}
      </div>
    </header>

    <!-- Whatever the last portrait action said, on one line under the
         header. A toast would slide away while somebody was still reading
         it, and this is the only place the answer lives. -->
    {#if avatarError}
      <p class="agent-detail__note agent-detail__note--fail" role="alert">{avatarError}</p>
    {:else if avatarNote}
      <p class="agent-detail__note">{avatarNote}</p>
    {/if}

    <!-- The Harness tab's Persona section: the soul the agent was built
         from, the facts Edit details writes, and the editor itself. Edit
         details writes the fields the header shows (team, purpose chips,
         capability), all of which describe a bundle the room composed, so a
         connected agent never reaches this -- `harnessSections` leaves the
         Persona section out for one entirely.
         MetadataDetails keeps its own trigger off: the header menu item is
         the trigger, and two would be one too many. -->
    {#snippet personaSection()}
      <section class="panel">
        <h2 class="panel__heading">Persona</h2>
        <dl class="agent-detail__facts">
          <dt>Soul</dt>
          <dd>{personaLine}</dd>
          <dt>Team</dt>
          <dd>{data.team}</dd>
          <dt>Purpose</dt>
          <dd>{data.purpose_tags.length > 0 ? data.purpose_tags.join(", ") : "Not set."}</dd>
          <dt>Capability</dt>
          <dd>{data.capability ?? "Not written yet. It arrives after first contact."}</dd>
        </dl>
        <p class="panel__lead">Edit details, in the menu beside the name, writes these.</p>
        <MetadataDetails
          bind:this={metadata}
          agentName={data.name}
          team={data.team}
          purposeTags={data.purpose_tags}
          capability={data.capability}
          frozen={isFrozen(data.status)}
          showTrigger={false}
        />
      </section>
    {/snippet}

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

    <!-- `tabDisabled` (worked out in the script above, where the hash rules
         also read it) is the same condition the checklist has always had:
         not ready, and not on its way out. -->
    {#if tabDisabled}
      <StepChecklist steps={data.steps} />
      <!-- While the tabs are still dead the build log stays beside the
           checklist, because that is exactly when somebody needs it and
           the Settings tab it otherwise lives in cannot be opened. -->
      <BuildLogCard agentName={data.name} />
    {/if}

    {@const primary = primaryTabs(invites.isSuccess, data.kind)}
    <div class="detail-tabs">
      <div class="detail-tabs__bar">
        <div class="detail-tabs__list" role="tablist" aria-label="Agent sections">
          <!-- Every panel mounts regardless of status (see the tabpanel loop
               below), but its query still 409s server-side until the agent
               is `ready` — so the trigger itself is disabled rather than
               left clickable into a dead end, for every status that is
               neither ready nor frozen (pending/provisioning/failed). Frozen
               statuses (destroying/destroy_failed) are the one exception:
               destruction makes read-only content reachable again, so only
               mutation CONTROLS freeze there (each panel's own `frozen` prop
               below) — the tabs themselves stay fully clickable. -->
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
              <!-- The pending-learnings count rides on Harness, which is
                   where Learnings lives now. -->
              {#if tab === "harness" && data.pending_learnings > 0}
                <span class="detail-tabs__badge">{data.pending_learnings}</span>
              {/if}
            </button>
          {/each}
        </div>
      </div>

      {#each primary as tab (tab)}
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
          {:else if tab === "harness"}
            <!-- What runs the agent. The five panels that used to be tabs
                 behind the More menu are sections in here, unchanged, plus
                 the model and the image it runs on. -->
            <HarnessPanel
              agentName={data.name}
              kind={data.kind}
              frozen={isFrozen(data.status)}
              model={data.model}
              modelLabel={data.model_label}
              runtimeImage={data.runtime_image}
              roomRuntimeImage={data.room_runtime_image}
              canUpdateRuntime={data.destroy_visible}
              pendingLearnings={data.pending_learnings}
              canDecideLearnings={data.can_decide_learnings}
              capability={data.capability}
              purposeTags={data.purpose_tags}
              bind:section={harnessSection}
              persona={connected ? undefined : personaSection}
            />
          {:else if tab === "settings"}
            <!-- The page's back office: secrets and environment, the build
                 log every agent has, and the two changes there is no way
                 back from.
                 `active`: every tabpanel here stays mounted when another tab
                 is picked, and SecretsPanel's two reads reach the secrets
                 platform and the agent's repository. Without it, opening any
                 agent page would ask about secrets nobody was looking at,
                 and the platform's audit would say somebody did. -->
            <SecretsPanel
              agentName={data.name}
              frozen={isFrozen(data.status)}
              active={activeTab === "settings"}
              deployed={data.status === "ready"}
            />
            <SettingsPanel
              agentName={data.name}
              status={data.status}
              error={data.error}
              kind={data.kind}
              canConvert={!connected &&
                data.destroy_visible &&
                (data.status === "ready" || data.status === "failed")}
              canDestroy={data.destroy_visible}
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
</div>

<style>
  .agent-detail {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
    /* Wider since the page-shell wave: one tab row that does not wrap
       needs the room, and the panels under it were always cramped at
       40rem. */
    max-width: 56rem;
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

  /* One run of facts at one weight. The separator is drawn by CSS rather
     than written into the markup, so a fact that is absent takes its
     separator with it and nothing has to know which one is first. */
  .agent-detail__meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2xs);
    color: var(--muted);
    font-size: 0.8rem;
  }

  .agent-detail__meta > * + *::before {
    content: "·";
    margin-right: var(--space-2xs);
    color: var(--border);
  }

  .agent-detail__model {
    font-family: var(--font-mono);
  }

  .agent-detail__repo {
    color: var(--accent);
  }

  /* Soul-line (persona-naming law): the persona is one grey fact among the
     others -- never a co-equal title beside the agent's own name. */
  .agent-detail__persona {
    color: var(--muted);
  }

  .agent-detail__tags {
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

  /* The Persona section's read-only pairs, same two-column shape the
     Harness tab's Model and runtime section uses. */
  .agent-detail__facts {
    display: grid;
    grid-template-columns: 7rem minmax(0, 1fr);
    gap: var(--space-2xs) var(--space-sm);
    margin: 0;
  }

  .agent-detail__facts dt {
    color: var(--muted);
    font-size: 0.8rem;
  }

  .agent-detail__facts dd {
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--ink);
    font-size: 0.85rem;
  }

  /* The one button in the header, and it is quiet on purpose: the header
     is for reading, and every action it used to spell out is behind this. */
  .agent-detail__menu {
    position: relative;
    margin-left: auto;
  }

  .agent-detail__menu-button {
    padding: 0 var(--space-xs);
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    background: none;
    color: var(--muted);
    font: inherit;
    font-size: 1.1rem;
    line-height: 1.4;
    cursor: pointer;
  }

  .agent-detail__menu-button:hover,
  .agent-detail__menu-button[aria-expanded="true"] {
    border-color: var(--border);
    color: var(--ink);
  }

  .agent-detail__menu-list {
    position: absolute;
    top: calc(100% + var(--space-2xs));
    right: 0;
    z-index: 20;
    display: flex;
    min-width: 12rem;
    flex-direction: column;
    padding: var(--space-2xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
  }

  .agent-detail__menu-item {
    display: flex;
    align-items: center;
    gap: var(--space-2xs);
    padding: var(--space-2xs) var(--space-sm);
    border: none;
    border-radius: var(--radius-sm);
    background: none;
    color: var(--ink);
    font: inherit;
    font-size: 0.85rem;
    text-align: left;
    text-decoration: none;
    white-space: nowrap;
    cursor: pointer;
  }

  .agent-detail__menu-item:hover:not(:disabled) {
    background: var(--bg);
    color: var(--accent);
  }

  .agent-detail__menu-item:disabled,
  .agent-detail__menu-item--disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .agent-detail__menu-rule {
    margin: var(--space-2xs) 0;
    border: none;
    border-top: 1px solid var(--border);
  }

  .agent-detail__note {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .agent-detail__note--fail {
    color: var(--fail);
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

  .composition__avatar-dot {
    display: inline-block;
    width: 0.5rem;
    height: 0.5rem;
    margin-right: var(--space-2xs);
    border-radius: 50%;
    background: var(--pulse);
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

  /* ONE row, and it neither wraps nor scrolls: five tabs and nothing else
     since the harness wave, with the grow-it sections inside one of them
     rather than behind a menu at the end of it. */
  .detail-tabs__bar {
    display: flex;
    flex-wrap: nowrap;
    align-items: center;
    gap: var(--space-md);
    border-bottom: 1px solid var(--border);
  }

  .detail-tabs__list {
    display: flex;
    min-width: 0;
    flex-wrap: nowrap;
    gap: var(--space-md);
    overflow: hidden;
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

</style>
