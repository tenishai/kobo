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
  // Tabs (harness wave, 2026-09-23; reshaped by issue #368): five on one
  // row, no menu at the end of it. Chat and Slack are what they were. Access
  // is the vendors the agent can reach. Harness holds the sections that used
  // to be tabs hidden behind More -- persona, skills, tooling, memory,
  // learnings -- plus the model and runtime, and Agent to agent (the pairs
  // that used to be on Access). Secrets is the agent's secrets and
  // variables, nothing else. Which tab is showing, and which Harness
  // section, both live in the hash: `#/agents/<name>/harness/skills`; the
  // retired `settings` segment still lands on Secrets.
  //
  // Header (page-shell wave, 2026-09-23): two lines, not eight. Line one is
  // the avatar, the name, the status, the connected marker, and on the far
  // right one overflow menu holding every action the header used to spell
  // out in filled buttons. Line two is a single muted run -- model, repo,
  // group, who made it, persona, observability -- with the capability
  // sentence as the one ink line under it and at most three chips below
  // that. The rest of an agent's identity lives in Edit details, which is
  // where it is edited. The header's buttons (issue #368): the repository,
  // Health, Build log (a sheet), Owners (who may see and manage it, a
  // sheet, with a count) and Edit details, whose foot holds the two changes
  // there is no way back from. Below the header: a failed-status retry
  // banner and the step checklist (pre-ready only), above the tab bar; a
  // failed agent's destroy sits there too, since its tabs are dead.
  // All polling is `agentQuery`'s own adaptive cadence (status.ts): this
  // component only renders whatever state that query is in, same
  // loading/error/populated split Dashboard already uses.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { derived, writable } from "svelte/store";
  import {
    ArrowUpRight,
    ArrowLeft,
    Camera,
    CaretRight,
    ChatCircle,
    Key,
    PencilSimple,
    Scroll,
    ShieldCheck,
    SlackLogo,
    Stack,
    Users,
  } from "phosphor-svelte";
  import AccessPanel from "../components/AccessPanel.svelte";
  import BuildLogCard from "../components/BuildLogCard.svelte";
  import DangerZone from "../components/DangerZone.svelte";
  import ChatPanel from "../components/ChatPanel.svelte";
  import ColleagueConversations from "../components/ColleagueConversations.svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import HarnessPanel from "../components/HarnessPanel.svelte";
  import MetadataDetails from "../components/MetadataDetails.svelte";
  import OwnersSheet from "../components/OwnersSheet.svelte";
  import RequestAccessCard from "../components/RequestAccessCard.svelte";
  import SecretsPanel from "../components/SecretsPanel.svelte";
  import Skeleton from "../components/Skeleton.svelte";
  import SlackPanel from "../components/SlackPanel.svelte";
  import StatusBadge from "../components/StatusBadge.svelte";
  import StepChecklist from "../components/StepChecklist.svelte";
  import ThreadPanel from "../components/ThreadPanel.svelte";
  import { ApiError, errorMessage, KIND_CONNECTED } from "../lib/api";
  import {
    agentQuery,
    generateAvatarMutation,
    grantsQuery,
    invitesQuery,
    observabilityQuery,
    personasSearchQuery,
    retryAgentMutation,
    uploadAvatarMutation,
  } from "../lib/queries";
  import { formatHash, navigate, route } from "../lib/router.svelte";
  import { isUpdating } from "../lib/delivery.svelte";
  import { isDestroyComplete, isFrozen } from "../lib/status";
  import {
    DEFAULT_HARNESS_SECTION,
    defaultTab,
    harnessSections,
    isHarnessSection,
    primaryTabs,
    TAB_LABELS,
    tabFromHash,
    type HarnessSectionId,
    type TabId,
  } from "../lib/tabs";
  import { relativeTime } from "../lib/time";
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

  // The Owners button's count (issue #368): the per-person grants, read
  // only once the probe above says the caller may see them. The sheet reads
  // the same key, so this costs no second request.
  const grants = createQuery(() => ({ ...grantsQuery(name), enabled: invites.isSuccess }));
  const ownersCount = $derived(grants.data?.length ?? 0);
  let ownersOpen = $state(false);
  let buildLogOpen = $state(false);

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
  // The header shows three chips and a count; the count is a button that
  // shows the rest in place (2026-10-01: a "+1" nobody could open told
  // nobody anything).
  let chipsExpanded = $state(false);
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

  // Is a change this page pushed still rolling out (lib/delivery)? Re-read
  // whenever the detail poll brings a fresh health verdict, so a delivery
  // that went quiet is forgotten on the room's own cadence (issue #268).
  const updating = $derived.by((): boolean => {
    void agent.data?.health?.checked_at;
    return isUpdating(name);
  });

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
    const visible = primaryTabs(invites.isSuccess, data.kind, {
      slackRelay: data.slack.relay,
      slackLegacy: data.slack.legacy,
    });
    const named = tabDisabled ? null : tabFromHash(route.tab);
    const fromHash = named !== null && visible.includes(named) ? named : null;
    if (fromHash !== null) {
      activeTab = fromHash;
      if (
        fromHash === "harness" &&
        isHarnessSection(route.section) &&
        harnessSections(data.kind, { canAdminister: invites.isSuccess }).includes(route.section)
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
    // The same request resumes a WAITING agent (its owner merged the setup
    // pull request and does not want to wait for the room's sweep) and
    // retries a FAILED one; only the words differ.
    const resuming = agent.data?.status === "waiting";
    retry.mutate(undefined, {
      onSuccess: () => {
        toast.success(
          resuming ? "Resuming" : "Retrying",
          resuming ? `Picking ${name}'s setup back up…` : `Re-running ${name}'s setup…`,
        );
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
  <nav class="agent-detail__crumbs" aria-label="Breadcrumb">
    <a class="agent-detail__back" href={formatHash({ page: "dashboard" })}>
      <ArrowLeft size={13} aria-hidden="true" />
      All agents
    </a>
    <CaretRight size={12} aria-hidden="true" />
    <span aria-current="page">{name}</span>
  </nav>

  {#if agent.isPending}
    <Skeleton rows={6} />
  {:else if agent.isError}
    <ErrorState
      message={errorMessage(agent.error, "Couldn't load this agent.")}
      onretry={() => agent.refetch()}
    />
  {:else if agent.data}
    {@const data = agent.data}
    <!-- Every control that would change the agent rests while it is being
         destroyed, and while a change this page pushed is still building
         or rolling (issue #290): the banner below says which. Reading stays
         possible either way; `tabDisabled` is a different question. -->
    {@const frozen = isFrozen(data.status) || updating}
    {@const connected = data.kind === KIND_CONNECTED}
    {@const destroyAboveTabs =
      data.destroy_visible && (data.status === "failed" || data.status === "destroy_failed")}
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
    {@const shownChips = chipsExpanded ? chips : chips.slice(0, 3)}
    {@const hiddenChips = chips.length - shownChips.length}
    {@const hiddenNames = chips.slice(shownChips.length).map((chip) => chip.text).join(", ")}
    <!-- The header card (2026-10-31 redesign): who it is and its status on
         the left, the ways out (repository, dashboard, the menu) on the
         right, what it is for under that, and the plain facts in one
         labelled row at the bottom. -->
    <header class="agent-detail__header">
      <div class="agent-detail__hero">
        <div class="agent-detail__identity">
          <div class="agent-detail__menu" bind:this={menuEl}>
          <!-- The portrait is its own menu: click the face to paint a new
               one, upload one, or download it. The camera badge says so. -->
          <button
            type="button"
            class="agent-detail__portrait"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            aria-label="Change portrait"
            title="Change portrait"
            onclick={() => (menuOpen = !menuOpen)}
          >
            {#if data.has_avatar || avatarVersion !== null}
              <img class="agent-detail__avatar" src={avatarSrc} alt="{data.name}'s picture" />
            {:else}
              <span class="agent-detail__avatar agent-detail__avatar--initial" aria-hidden="true">
                {data.name.slice(0, 1).toUpperCase()}
              </span>
            {/if}
            <span class="agent-detail__portrait-badge" aria-hidden="true">
              <Camera size={11} weight="bold" />
            </span>
          </button>
          {#if menuOpen}
            <div class="agent-detail__menu-list">
              <button
                type="button"
                class="agent-detail__menu-item"
                disabled={frozen || generateAvatar.isPending}
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
                class:agent-detail__menu-item--disabled={frozen ||
                  uploadAvatar.isPending}
              >
                {uploadAvatar.isPending ? "Uploading…" : "Upload a portrait"}
                <input
                  class="composition__avatar-upload-input"
                  type="file"
                  accept=".png,image/png"
                  disabled={frozen || uploadAvatar.isPending}
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

            </div>
          {/if}
        </div>
          <div class="agent-detail__heading">
            <div class="agent-detail__title">
              <h1 class="agent-detail__name">{data.name}</h1>
              <span class="agent-detail__status">
                <StatusBadge status={data.status} health={data.health} {updating} />
              </span>
              <!-- The same marker the dashboard card carries, in the same
                   words, so the two pages agree about what this agent is. -->
              {#if connected}
                <span class="agent-detail__chip">connected</span>
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
          </div>
        </div>

        <div class="agent-detail__actions">
          {#if data.repo_full_name}
            <!-- A connected agent's page names the repository rather than
                 saying "Repository": it is somebody else's repository, and
                 which one it is is the main fact about this agent. -->
            <a
              class="agent-detail__action agent-detail__repo"
              href={`https://github.com/${data.repo_full_name}`}
              target="_blank"
              rel="noreferrer"
            >
              {connected ? data.repo_full_name : "Repository"}
              <ArrowUpRight size={12} weight="bold" aria-hidden="true" />
            </a>
          {/if}
          {#if dashboardUrl}
            <a
              class="agent-detail__action"
              href={dashboardUrl}
              target="_blank"
              rel="noreferrer"
              title={`${name}-agent-health`}
            >
              Health
              <ArrowUpRight size={12} weight="bold" aria-hidden="true" />
            </a>
          {/if}
          <!-- The last build, as the build itself told it, in a sheet
               (issue #368). Every agent has one, whoever is looking. -->
          <button
            type="button"
            class="agent-detail__action"
            aria-haspopup="dialog"
            onclick={() => (buildLogOpen = true)}
          >
            <Scroll size={13} weight="bold" aria-hidden="true" />
            Build log
          </button>
          {#if invites.isSuccess}
            <!-- Who may see and manage this agent besides its owner: a
                 sheet, for the callers who may administer it (the same
                 probe that shows the Access tab). -->
            <button
              type="button"
              class="agent-detail__action"
              aria-haspopup="dialog"
              onclick={() => (ownersOpen = true)}
            >
              <Users size={13} weight="bold" aria-hidden="true" />
              Owners
              {#if ownersCount > 0}
                <span class="agent-detail__count">{ownersCount}</span>
              {/if}
            </button>
          {/if}
          {#if !connected}
            <!-- The editor lives in the Harness tab's Persona section, so
                 this brings the page there before opening it: a modal
                 inside a hidden panel is a modal nobody can see. -->
            <button
              type="button"
              class="agent-detail__action"
              disabled={frozen}
              onclick={() => {
                activeTab = "harness";
                harnessSection = "persona";
                metadata?.openEditor();
              }}
            >
              <PencilSimple size={13} weight="bold" aria-hidden="true" />
              Edit details
            </button>
          {/if}
        </div>
      </div>

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
              <button
                type="button"
                class="agent-detail__chip agent-detail__chip--more"
                title={hiddenNames}
                aria-label={`Show ${hiddenChips} more: ${hiddenNames}`}
                onclick={() => (chipsExpanded = true)}
              >
                +{hiddenChips}
              </button>
            {:else if chipsExpanded && chips.length > 3}
              <button
                type="button"
                class="agent-detail__chip agent-detail__chip--more"
                onclick={() => (chipsExpanded = false)}
              >
                less
              </button>
            {/if}
          </p>
        {/if}

      <!-- The plain facts, each under its own label, in the order somebody
           reads them: what it runs on, who it belongs to, who made it, who
           it is, and how long it has been here. -->
      <dl class="agent-detail__stats">
        <div class="agent-detail__stat">
          <dt>Model</dt>
          <dd title={data.model}>{data.model_label}</dd>
        </div>
        <div class="agent-detail__stat">
          <dt>Group</dt>
          <dd>{data.group_name ?? "None"}</dd>
        </div>
        <div class="agent-detail__stat">
          <dt>Author</dt>
          <dd title={data.created_by ?? undefined}>{data.created_by ?? "Unknown"}</dd>
        </div>
        {#if !connected}
          <!-- Soul-line (persona-naming law): the persona's NAME, with
               who-they-are on the hover. The name opens the bundle's
               persona.md, exactly what the running agent was built from. -->
          <div class="agent-detail__stat">
            <dt>Persona</dt>
            <dd>
              {#if data.repo_full_name}
                <a
                  class="agent-detail__persona"
                  href={`https://github.com/${data.repo_full_name}/blob/main/bundle/persona.md`}
                  target="_blank"
                  rel="noreferrer"
                  title={personaLine}
                >
                  {personaName}
                </a>
              {:else}
                <span class="agent-detail__persona" title={personaLine}>{personaName}</span>
              {/if}
            </dd>
          </div>
        {/if}
        <div class="agent-detail__stat">
          <dt>Created</dt>
          <dd>{relativeTime(data.created_at)}</dd>
        </div>
      </dl>
    </header>

    <BuildLogCard agentName={data.name} open={buildLogOpen} onclose={() => (buildLogOpen = false)} />
    {#if invites.isSuccess}
      <OwnersSheet
        agentName={data.name}
        open={ownersOpen}
        onclose={() => (ownersOpen = false)}
        frozen={frozen}
      />
    {/if}

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
          <dt>Persona</dt>
          <dd>
            {personaLine}
            {#if data.repo_full_name}
              <a
                class="agent-detail__repo"
                href={`https://github.com/${data.repo_full_name}/blob/main/bundle/persona.md`}
                target="_blank"
                rel="noreferrer"
              >
                Open in GitHub
              </a>
            {/if}
          </dd>
          <dt>Source</dt>
          <dd>
            {data.team === "shared" || !data.team ? "Built-in" : `${data.team} team`}
            <span class="agent-detail__fact-help">
              Where its persona and skills come from. Who can see the agent is its group,
              shown beside the name.
            </span>
          </dd>
          <dt>Purpose</dt>
          <dd>{data.purpose_tags.length > 0 ? data.purpose_tags.join(", ") : "Not set."}</dd>
          <dt>Capability</dt>
          <dd>{data.capability ?? "Not written yet. It arrives after first contact."}</dd>
        </dl>
        <p class="panel__lead">Edit details, at the top of the page, writes these.</p>
        <MetadataDetails
          bind:this={metadata}
          agentName={data.name}
          team={data.team}
          purposeTags={data.purpose_tags}
          capability={data.capability}
          frozen={frozen}
          showTrigger={false}
          status={data.status}
          error={data.error}
          kind={data.kind}
          canConvert={!updating &&
            data.destroy_visible &&
            (data.status === "ready" || data.status === "failed")}
          canDestroy={data.destroy_visible &&
            (data.status === "ready" || data.status === "destroying")}
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
    {:else if data.status === "waiting"}
      <!-- Parked on a person, not broken: a connected repository's setup
           pull request is open and its owner merges it. The room resumes on
           its own once the deploy service reports the merge; Resume is for the
           person who just merged it and does not want to wait a minute. -->
      <div class="waiting-banner" role="status">
        <p class="waiting-banner__message">
          {data.error ?? "Waiting for someone on your team to merge the setup pull request."}
        </p>
        <button
          type="button"
          class="waiting-banner__resume"
          disabled={retry.isPending}
          onclick={handleRetry}
        >
          {retry.isPending ? "Resuming…" : "Resume"}
        </button>
      </div>
    {:else if data.status === "ready" && updating}
      <!-- A change this page pushed is still building or rolling
           (lib/delivery): the controls above and below rest until it
           lands or fails, and this says why (issues #268, #290). -->
      <div class="waiting-banner" role="status">
        <p class="waiting-banner__message">
          Updating: the changes you made are on their way. Controls come back when it lands.
        </p>
      </div>
    {:else if data.status === "ready" && data.health?.state === "down"}
      <!-- Setup finished, pod is not running: the cluster's own verdict
           (kobo_api.health), refreshed every minute. -->
      <div class="waiting-banner waiting-banner--down" role="alert">
        <p class="waiting-banner__message">
          This agent is down: {data.health.reason ?? "it is not running"}.
          {#if dashboardUrl}
            <a href={dashboardUrl} target="_blank" rel="noreferrer">Open its dashboard</a> for logs.
          {/if}
        </p>
      </div>
    {/if}

    <!-- `tabDisabled` (worked out in the script above, where the hash rules
         also read it) is the same condition the checklist has always had:
         not ready, and not on its way out. -->
    {#if tabDisabled}
      <StepChecklist steps={data.steps} />
    {/if}
    <!-- The destroy lives at the foot of Edit details, which a failed agent
         cannot open (its tabs are dead) and a destroy that failed cannot
         either (the editor rests while frozen). For those two it sits here,
         above the tabs, so a setup that never finished can still be taken
         back out of the room. Never while an agent is still being built:
         the room refuses a destroy mid-build. -->
    {#if destroyAboveTabs}
      <div class="agent-detail__danger">
        <DangerZone
          agentName={data.name}
          status={data.status}
          error={data.error}
          kind={data.kind}
        />
      </div>
    {/if}

    {@const primary = primaryTabs(invites.isSuccess, data.kind, { slackRelay: data.slack.relay, slackLegacy: data.slack.legacy })}
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
              {#if tab === "chat"}
                <ChatCircle class="detail-tabs__icon" size={15} aria-hidden="true" />
              {:else if tab === "slack"}
                <SlackLogo class="detail-tabs__icon" size={15} aria-hidden="true" />
              {:else if tab === "access"}
                <ShieldCheck class="detail-tabs__icon" size={15} aria-hidden="true" />
              {:else if tab === "harness"}
                <Stack class="detail-tabs__icon" size={15} aria-hidden="true" />
              {:else}
                <Key class="detail-tabs__icon" size={15} aria-hidden="true" />
              {/if}
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
            <!-- The agent's first reply opens the conversation, as its first
                 message, rather than sitting in a card of its own. -->
            <ChatPanel agentName={data.name} frozen={frozen} firstReply={data.first_reply} />
            <div class="agent-detail__pair">
              <ThreadPanel agentName={data.name} frozen={frozen} />
              <ColleagueConversations agentName={data.name} />
            </div>
          {:else if tab === "harness"}
            <!-- What runs the agent. The five panels that used to be tabs
                 behind the More menu are sections in here, unchanged, plus
                 the model and the image it runs on. -->
            <HarnessPanel
              agentName={data.name}
              kind={data.kind}
              frozen={frozen}
              model={data.model}
              modelLabel={data.model_label}
              runtimeImage={data.runtime_image}
              builtRuntimeImage={data.built_runtime_image}
              runtimePinned={data.runtime_pinned}
              roomRuntimeImage={data.room_runtime_image}
              canUpdateRuntime={data.destroy_visible}
              colleagueMode={data.colleague_mode}
              pendingLearnings={data.pending_learnings}
              canDecideLearnings={data.can_decide_learnings}
              capability={data.capability}
              purposeTags={data.purpose_tags}
              identity={data.identity ?? null}
              canAdminister={invites.isSuccess}
              bind:section={harnessSection}
              persona={connected ? undefined : personaSection}
            />
          {:else if tab === "secrets"}
            <!-- The agent's secrets and variables (issue #368), nothing else.
                 `active`: every tabpanel here stays mounted when another tab
                 is picked, and SecretsPanel's two reads reach the secrets
                 platform and the agent's repository. Without it, opening any
                 agent page would ask about secrets nobody was looking at,
                 and the platform's audit would say somebody did. -->
            <SecretsPanel
              agentName={data.name}
              frozen={frozen}
              active={activeTab === "secrets"}
              deployed={data.status === "ready"}
            />
            {#if connected && data.destroy_visible && (data.status === "ready" || data.status === "destroying")}
              <!-- A connected agent has no Edit details (nothing the room
                   composed to edit), so its destroy keeps a row here at the
                   foot of its last tab. -->
              <section class="panel agent-detail__oneway" aria-label="No way back">
                <DangerZone
                  agentName={data.name}
                  status={data.status}
                  error={data.error}
                  kind={data.kind}
                />
              </section>
            {/if}
          {:else if tab === "slack"}
            <SlackPanel
              agentName={data.name}
              slack={data.slack}
              frozen={frozen}
            />
          {:else}
            <!-- What this agent can reach (issues #365 and #368): the vendors,
                 what it holds at each, and the picker that asks for more. -->
            <AccessPanel
              agentName={data.name}
              groupName={data.group_name}
              frozen={frozen}
              identity={data.identity ?? null}
            />
          {/if}
        </div>
      {/each}
    </div>

  {/if}
</div>

<style>
  /* Centred in the frame like the guide, wide enough for the header's
     facts row and the side-by-side panels under the chat. */
  .agent-detail {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
    width: 100%;
    max-width: 72rem;
    margin: 0 auto;
  }

  .agent-detail__crumbs {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    color: var(--muted);
    font-family: var(--font-display-mono);
    font-size: 0.75rem;
  }

  .agent-detail__crumbs [aria-current="page"] {
    color: var(--ink);
  }

  /* The way back to the agent cards: a small button, not a faint word. */
  .agent-detail__back {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    height: 1.9rem;
    padding: 0 var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-pill);
    background: var(--panel);
    color: var(--ink);
    font-family: var(--font-sans);
    font-size: 0.8rem;
    font-weight: 500;
    text-decoration: none;
    transition: background 0.15s ease, border-color 0.15s ease;
  }

  .agent-detail__back:hover {
    border-color: var(--ring);
    background: var(--accent-soft);
  }

  .agent-detail__header {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .agent-detail__hero {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-md);
  }

  .agent-detail__identity {
    display: flex;
    min-width: 0;
    flex: 1;
    align-items: center;
    gap: var(--space-md);
  }

  .agent-detail__avatar {
    width: 3.5rem;
    height: 3.5rem;
    flex-shrink: 0;
    border-radius: var(--radius-sm);
    object-fit: cover;
  }

  .agent-detail__avatar--initial {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: var(--accent);
    color: var(--accent-ink);
    font-family: var(--font-display-mono);
    font-size: 1.4rem;
    font-weight: 600;
  }

  .agent-detail__heading {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: var(--space-2xs);
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
    font-family: var(--font-display-mono);
    font-size: 1.6rem;
    font-weight: 600;
    letter-spacing: -0.02em;
    line-height: 1.15;
  }

  /* The status as a soft pill beside the name. */
  .agent-detail__status {
    display: inline-flex;
    align-items: center;
    padding: 0.15rem var(--space-xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--bg);
    font-size: 0.8rem;
  }

  .agent-detail__capability {
    margin: 0;
    color: var(--ink);
    font-size: 0.92rem;
  }

  .agent-detail__actions {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    gap: var(--space-xs);
  }

  .agent-detail__action {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    height: 2.1rem;
    padding: 0 var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-pill);
    background: var(--panel);
    color: var(--ink);
    font-size: 0.82rem;
    font-weight: 500;
    text-decoration: none;
    white-space: nowrap;
    transition: background 0.15s ease, border-color 0.15s ease;
  }

  .agent-detail__action:hover {
    border-color: var(--ring);
    background: var(--accent-soft);
  }

  .agent-detail__tags {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2xs);
    margin: 0;
  }

  .agent-detail__chip {
    display: inline-flex;
    align-items: center;
    padding: 0.15rem var(--space-xs);
    border: 1px solid transparent;
    border-radius: var(--radius-pill);
    background: var(--accent-soft);
    color: var(--ink);
    font: inherit;
    font-size: 0.75rem;
    font-weight: 500;
  }

  .agent-detail__chip--skill {
    border-color: var(--border);
    background: var(--panel);
  }

  .agent-detail__chip--more {
    border-color: var(--border);
    background: var(--panel);
    color: var(--muted);
    cursor: pointer;
  }

  /* The facts row: label over value, on a line of its own under a rule. */
  .agent-detail__stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
    gap: var(--space-md);
    margin: 0;
    padding-top: var(--space-md);
    border-top: 1px solid var(--border);
  }

  .agent-detail__stat {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 0.2rem;
  }

  .agent-detail__stat dt {
    color: var(--muted);
    font-family: var(--font-display-mono);
    font-size: 0.65rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .agent-detail__stat dd {
    margin: 0;
    overflow: hidden;
    color: var(--ink);
    font-size: 0.88rem;
    font-weight: 500;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .agent-detail__persona {
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--border);
    text-underline-offset: 3px;
  }

  a.agent-detail__persona:hover {
    text-decoration-color: var(--ink);
  }

  /* Under the chat: the shared thread and the colleague log side by side,
     stacked when the frame is narrow. */
  .agent-detail__pair {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(22rem, 1fr));
    gap: var(--space-lg);
    align-items: stretch;
  }

  /* The Persona section's facts, as rows like Model and runtime: the
     name on the left, the value on the right, a hairline between rows. */
  .agent-detail__facts {
    display: grid;
    grid-template-columns: 11rem minmax(0, 1fr);
    margin: 0;
    border-top: 1px solid var(--border);
  }

  .agent-detail__facts dt,
  .agent-detail__facts dd {
    padding: var(--space-md) 0;
    border-bottom: 1px solid var(--border);
  }

  .agent-detail__facts dt {
    padding-right: var(--space-md);
    color: var(--muted);
    font-size: 0.85rem;
  }

  .agent-detail__facts dd {
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--ink);
    font-size: 0.875rem;
    line-height: 1.6;
  }

  .agent-detail__facts dd .agent-detail__repo {
    margin-left: var(--space-2xs);
    color: var(--link);
  }

  .agent-detail__fact-help {
    display: block;
    max-width: 40rem;
    margin-top: var(--space-2xs);
    color: var(--muted);
    font-size: 0.8rem;
    line-height: 1.5;
  }

  /* The one button in the header, and it is quiet on purpose: the header
     is for reading, and every action it used to spell out is behind this. */
  .agent-detail__menu {
    position: relative;
    flex-shrink: 0;
  }

  .agent-detail__portrait {
    position: relative;
    display: block;
    padding: 0;
    border: none;
    border-radius: var(--radius-sm);
    background: none;
    cursor: pointer;
  }

  .agent-detail__portrait:hover .agent-detail__avatar,
  .agent-detail__portrait[aria-expanded="true"] .agent-detail__avatar {
    filter: brightness(0.92);
  }

  .agent-detail__portrait-badge {
    position: absolute;
    right: -4px;
    bottom: -4px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.35rem;
    height: 1.35rem;
    border: 2px solid var(--panel);
    border-radius: 50%;
    background: var(--panel);
    color: var(--ink);
    box-shadow: 0 0 0 1px var(--border);
  }

  .agent-detail__action:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  /* The Owners count, a small pill inside the button. */
  .agent-detail__count {
    display: inline-flex;
    min-width: 1.15rem;
    height: 1.15rem;
    align-items: center;
    justify-content: center;
    padding: 0 0.3rem;
    border-radius: 999px;
    background: var(--accent-soft);
    color: var(--ink);
    font-family: var(--font-display-mono);
    font-size: 0.65rem;
    font-weight: 600;
  }

  .agent-detail__oneway {
    margin-top: var(--space-lg);
  }

  .agent-detail__menu-list {
    position: absolute;
    top: calc(100% + var(--space-xs));
    left: 0;
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

  .agent-detail__danger {
    margin-top: var(--space-md);
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

  .waiting-banner {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-md);
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .waiting-banner--down {
    border-color: var(--fail);
    background: color-mix(in srgb, var(--fail) 8%, var(--panel));
  }

  .waiting-banner__message {
    margin: 0;
    color: var(--ink);
    font-size: 0.9rem;
    overflow-wrap: anywhere;
  }

  .waiting-banner__resume {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .waiting-banner__resume:hover:not(:disabled) {
    filter: brightness(0.97);
  }

  .waiting-banner__resume:disabled {
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
  }

  /* Underline tabs: a hairline rule across the full width, the tabs on it
     at their natural widths, and the chosen one marked by dark text and a
     dark bar that sits on the rule. */
  .detail-tabs__bar {
    border-bottom: 1px solid var(--border);
  }

  .detail-tabs__list {
    display: flex;
    min-width: 0;
    flex-wrap: nowrap;
    gap: var(--space-lg);
    overflow-x: auto;
    scrollbar-width: none;
  }

  .detail-tabs__tab {
    position: relative;
    display: inline-flex;
    flex-shrink: 0;
    align-items: center;
    gap: 0.45rem;
    height: 2.75rem;
    padding: 0 var(--space-2xs);
    border: none;
    background: none;
    color: var(--muted);
    font: inherit;
    font-size: 0.875rem;
    font-weight: 500;
    letter-spacing: var(--label-tracking);
    text-transform: var(--label-case);
    cursor: pointer;
    transition: color 0.15s ease;
  }

  .detail-tabs__tab::after {
    content: "";
    position: absolute;
    right: 0;
    bottom: -1px;
    left: 0;
    height: 2px;
    border-radius: 2px 2px 0 0;
    background: transparent;
    transition: background 0.15s ease;
  }

  .detail-tabs__tab:hover:not(:disabled) {
    color: var(--ink);
  }

  .detail-tabs__tab:hover:not(:disabled):not([aria-selected="true"])::after {
    background: var(--border);
  }

  .detail-tabs__tab[aria-selected="true"] {
    color: var(--ink);
  }

  .detail-tabs__tab[aria-selected="true"]::after {
    background: var(--accent);
  }

  .detail-tabs__tab :global(.detail-tabs__icon) {
    flex-shrink: 0;
    opacity: 0.85;
  }

  .detail-tabs__tab:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .detail-tabs__badge {
    display: inline-flex;
    min-width: 1.15rem;
    height: 1.15rem;
    align-items: center;
    justify-content: center;
    padding: 0 0.3rem;
    border-radius: 999px;
    background: var(--accent-soft);
    color: var(--ink);
    font-family: var(--font-display-mono);
    font-size: 0.65rem;
    font-weight: 600;
  }

  .detail-tabs__tab[aria-selected="true"] .detail-tabs__badge {
    background: var(--accent);
    color: var(--accent-ink);
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
