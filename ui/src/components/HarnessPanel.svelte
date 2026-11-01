<script lang="ts">
  import {
    Brain,
    CheckCircle,
    ClockCounterClockwise,
    Cpu,
    Handshake,
    IdentificationCard,
    Lightbulb,
    Lightning,
    PushPin,
    UserCircle,
    WarningCircle,
    Wrench,
  } from "phosphor-svelte";

  // One icon per Harness section, in the order of the list.
  const SECTION_ICONS = {
    runtime: Cpu,
    identity: IdentificationCard,
    persona: UserCircle,
    skills: Lightning,
    tooling: Wrench,
    a2a: Handshake,
    memory: Brain,
    learnings: Lightbulb,
    activity: ClockCounterClockwise,
  } as const;
  import type { AgentIdentity } from "../lib/api";
  // The Harness tab (harness wave, 2026-09-23): what runs the agent.
  //
  // "People think of Harness as what runs the agent: the tooling, the
  // skills, the LLM model etc." Before this, those lived as five separate
  // tabs behind a More menu that was shut by default for everybody, which
  // meant the five things that describe an agent were the five things
  // nobody saw. They are sections of one tab now, picked from a small list
  // down the left, and the page's tab row is five plain tabs with no menu
  // at the end of it.
  //
  // Nothing inside changed. The five panels are the same components with
  // the same props they had as tabs, and Persona is `MetadataDetails`
  // mounted exactly where it always was -- in the page, bound so the
  // header's Edit details item can still open it -- and handed in here as a
  // snippet. Moving it would have taken that binding with it.
  //
  // Every section stays mounted and is merely hidden, the same rule the
  // page's own tabpanels follow: switching sections must not refetch a
  // panel's query or throw away a half-typed form.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import type { Snippet } from "svelte";
  import LearningsPanel from "./LearningsPanel.svelte";
  import ActivityPanel from "./ActivityPanel.svelte";
  import AgentToAgentPanel from "./AgentToAgentPanel.svelte";
  import MemoryPanel from "./MemoryPanel.svelte";
  import SkillsPanel from "./SkillsPanel.svelte";
  import ToolingPanel from "./ToolingPanel.svelte";
  import { errorMessage, KIND_COMPOSED, KIND_CONNECTED, type ColleagueMode } from "../lib/api";
  import {
    colleagueModeMutation,
    modelsQuery,
    updateAgentModelMutation,
    updateAgentRuntimeMutation,
    whoamiQuery,
} from "../lib/queries";
  import {
    DEFAULT_HARNESS_SECTION,
    HARNESS_SECTION_LABELS,
    harnessSections,
    type HarnessSectionId,
  } from "../lib/tabs";
  import { toast } from "../lib/toast.svelte";

  let {
    agentName,
    kind = KIND_COMPOSED,
    frozen = false,
    model,
    modelLabel,
    runtimeImage = undefined,
    builtRuntimeImage = undefined,
    runtimePinned = false,
    roomRuntimeImage = undefined,
    canUpdateRuntime = false,
    colleagueMode = undefined,
    pendingLearnings = 0,
    canDecideLearnings = false,
    capability = null,
    purposeTags = [],
    identity = null,
    canAdminister = false,
    section = $bindable(DEFAULT_HARNESS_SECTION),
    persona = undefined,
  }: {
    agentName: string;
    kind?: string;
    frozen?: boolean;
    /** The full model id, for the hover; `modelLabel` is what is printed,
     * the same pair the page header shows. */
    model: string;
    modelLabel: string;
    /** The runtime this agent runs on, already resolved server-side against
     * the room's default. `undefined` on a room whose API predates the
     * field, which the Runtime row says plainly rather than guessing. */
    runtimeImage?: string;
    /** What the agent is built on right now, read from its files
     * (2026-11-01). `null` until the room has read them once after boot;
     * `undefined` on an older server. The state line compares this with
     * the room's, never `runtimeImage`, which for an agent that follows
     * the room is the room's own pin and so always read as current. */
    builtRuntimeImage?: string | null;
    /** Whether the agent deliberately holds a runtime of its own. */
    runtimePinned?: boolean;
    /** The runtime the room builds on today. */
    roomRuntimeImage?: string;
    /** Whether the update is this caller's to make: an operator, per the
     * same flag the Convert row reads. The route's own owner-or-operator
     * gate stays the real enforcement. */
    canUpdateRuntime?: boolean;
    /** Whether the agent may consult its colleagues (issue #131). `undefined`
     * on a room whose API predates the field: the row is then not shown. */
    colleagueMode?: ColleagueMode;
    pendingLearnings?: number;
    canDecideLearnings?: boolean;
    capability?: string | null;
    purposeTags?: string[];
    /** The identity the agent runs as (`AgentDetail.identity`); absent on an
     * older server, in which case the section says so. */
    identity?: AgentIdentity | null;
    /** Whether the caller may administer the agent: the page's own probe of
     * `GET /api/agents/{name}/invites` answered 200, the same gate the
     * Access tab has. Agent to agent (issue #368) is shown only then, since
     * the routes behind it answer nobody else. */
    canAdminister?: boolean;
    /** Which section is showing. Bindable so the page can keep it in the
     * URL hash (`#/agents/<name>/harness/skills`) while a click in here
     * still moves it on its own. */
    section?: HarnessSectionId;
    /** Persona: `MetadataDetails`, mounted by the page and rendered here. */
    persona?: Snippet;
  } = $props();

  // --- Access request (2026-09-29) ------------------------------------------
  // Plain text the owner hands to the SRE team: which service account needs
  // which roles on which project. Basic roles are refused here, not just
  // discouraged, because a request naming one would be sent back anyway.
  const BASIC_ROLES = ["roles/owner", "roles/editor", "roles/viewer", "roles/browser"];
  let accessProject = $state("");
  let accessRoles = $state("");
  let accessCopied = $state(false);
  const accessRoleList = $derived(
    accessRoles
      .split(/[\n,]/)
      .map((r) => r.trim())
      .filter((r) => r.length > 0),
  );
  const accessRefusal = $derived.by(() => {
    const basic = accessRoleList.filter((r) => BASIC_ROLES.includes(r.toLowerCase()));
    if (basic.length > 0) {
      return `Basic roles are not granted to agents: ${basic.join(", ")}. Ask for the narrowest role that does the job.`;
    }
    const malformed = accessRoleList.filter((r) => !/^roles\/[A-Za-z0-9_.]+$/.test(r));
    if (malformed.length > 0) {
      return `Not a role name: ${malformed.join(", ")}. Roles look like roles/service.role.`;
    }
    return null;
  });
  const accessRequest = $derived.by(() => {
    const project = accessProject.trim();
    if (!identity || !project || accessRoleList.length === 0 || accessRefusal !== null) {
      return null;
    }
    return [
      `Access request for agent ${agentName}`,
      `Service account: ${identity.gsa_email}`,
      `Needs access to GCP project: ${project}`,
      "Roles:",
      ...accessRoleList.map((r) => `  - ${r}`),
    ].join("\n");
  });
  async function copyAccessRequest(): Promise<void> {
    if (accessRequest === null) {
      return;
    }
    try {
      await navigator.clipboard.writeText(accessRequest);
      accessCopied = true;
      setTimeout(() => (accessCopied = false), 1500);
    } catch {
      accessCopied = false;
    }
  }

  const connected = $derived(kind === KIND_CONNECTED);
  // The permission service on means no Identity and access section (#292).
  const who = createQuery(() => (whoamiQuery()));
  const accessOn = $derived(who.data?.access_on === true);
  const sections = $derived(harnessSections(kind, { accessOn, canAdminister }));

  /** The version half of an image ref: the digest after an `@`, else the
   * tag after the last `:`, else (an untagged ref) the ref itself. Used
   * only in the Behind line, where the full ref is already on the row above
   * and repeating it would bury the one word that matters. */
  function versionOf(ref: string): string {
    // The card's rule (composer.runtime_version): the tag when the ref has
    // one, also for `repo:tag@sha256:...`; the digest's first 12 hex for a
    // digest-only ref; `latest` for a bare repository.
    const at = ref.lastIndexOf("@");
    const name = at === -1 ? ref : ref.slice(0, at);
    const colon = name.lastIndexOf(":");
    if (colon > name.lastIndexOf("/")) {
      return name.slice(colon + 1);
    }
    if (at !== -1) {
      const digest = ref.slice(at + 1);
      const sep = digest.indexOf(":");
      return sep === -1 ? digest.slice(0, 12) : digest.slice(0, sep + 13);
    }
    return "latest";
  }

  // The state line compares what the agent is BUILT on with what the room
  // builds on now. Not `runtimeImage`: for an agent that follows the room
  // that is the room's pin itself, and comparing the pin with itself said
  // "up to date" to every agent through a whole runtime move (2026-11-01).
  // States: nothing to compare (an older room, or a connected agent that
  // brings its own), not read yet, behind, current; pinned colours the
  // last two, since a pinned agent stays put when the room moves.
  const runtimeStated = $derived(Boolean(roomRuntimeImage) && !connected);
  const behind = $derived(
    Boolean(builtRuntimeImage) && builtRuntimeImage !== roomRuntimeImage,
  );
  const moveLabel = $derived(runtimePinned ? "Move to the room's runtime" : "Update to current");

  const queryClient = useQueryClient();
  // svelte-ignore state_referenced_locally
  const updateRuntime = createMutation(() =>
    updateAgentRuntimeMutation(queryClient, agentName),
  );

  // Change the model after deploy (2026-10-01): the room's list, the
  // current one selected, Change enabled once another is picked. Same
  // gate as the runtime update: owner or operator, composed, not frozen.
  const canChangeModel = $derived(canUpdateRuntime && kind !== KIND_CONNECTED);
  const models = createQuery(() => ({ ...modelsQuery(), enabled: canChangeModel }));
  let pickedModel = $state<string | null>(null);
  const modelChoice = $derived(pickedModel ?? model);
  // svelte-ignore state_referenced_locally
  const updateModel = createMutation(() => updateAgentModelMutation(queryClient, agentName));

  function handleChangeModel(): void {
    if (modelChoice === model || updateModel.isPending) {
      return;
    }
    updateModel.mutate(modelChoice, {
      onSuccess: (change) => {
        pickedModel = null;
        toast.success("Model changed", `${agentName} rebuilds on ${change.model_label} now.`);
      },
      onError: (err: unknown) => {
        toast.error("Couldn't change the model", errorMessage(err, "Try again."));
      },
    });
  }

  // Colleague mode (issue #131): the same select-then-Change shape and the
  // same gate as the model row.
  // Labels read as a choice an owner can make, not as mode names (#264);
  // the default says so, so a first look needs no change.
  const COLLEAGUE_MODE_LABELS: Record<ColleagueMode, string> = {
    default: "Yes, when it helps (default)",
    "roster-only": "No, but knows who they are",
    none: "No",
  };
  // What each mode does, under the row (issue #133): roster-only also turns
  // off general knowledge, which the label alone doesn't say.
  const COLLEAGUE_MODE_NOTES: Record<ColleagueMode, string> = {
    default: "Asks the colleague whose field a question is in before answering from general knowledge.",
    "roster-only":
      "Knows who is in the room but can't ask them. Answers only from its own tools and memory, not general knowledge; otherwise says it doesn't know and asks you.",
    none: "No colleague tools and no roster.",
  };
  // True in every mode: the mode is about what this agent may ask, not what
  // it may be asked.
  const COLLEAGUE_MODE_ALWAYS = "Other agents can still ask it.";
  let pickedColleagueMode = $state<ColleagueMode | null>(null);
  const colleagueModeChoice = $derived(pickedColleagueMode ?? colleagueMode);
  // svelte-ignore state_referenced_locally
  const updateColleagueMode = createMutation(() => colleagueModeMutation(queryClient, agentName));

  function handleChangeColleagueMode(): void {
    if (!colleagueModeChoice || colleagueModeChoice === colleagueMode || updateColleagueMode.isPending) {
      return;
    }
    updateColleagueMode.mutate(colleagueModeChoice, {
      onSuccess: () => {
        pickedColleagueMode = null;
        toast.success("Colleague mode changed", `${agentName} rebuilds with it now.`);
      },
      onError: (err: unknown) => {
        toast.error("Couldn't change the colleague mode", errorMessage(err, "Try again."));
      },
    });
  }

  function handleUpdateRuntime(): void {
    if (!roomRuntimeImage) {
      return;
    }
    updateRuntime.mutate(roomRuntimeImage, {
      onSuccess: () => {
        toast.success(
          "Runtime updated",
          `${agentName} rebuilds on the room's current runtime now.`,
        );
      },
      onError: (error: unknown) => {
        toast.error(
          "Couldn't update the runtime",
          errorMessage(error, "Something went wrong."),
        );
      },
    });
  }
</script>

<div class="harness">
  <nav class="harness__nav" aria-label="Harness sections">
    {#each sections as id (id)}
      {@const Icon = SECTION_ICONS[id]}
      <button
        type="button"
        class="harness__item"
        aria-current={section === id}
        aria-controls={`harness-${id}`}
        onclick={() => (section = id)}
      >
        <span class="harness__item-label">
          <Icon class="harness__icon" size={17} aria-hidden="true" />
          {HARNESS_SECTION_LABELS[id]}
        </span>
        {#if id === "learnings" && pendingLearnings > 0}
          <span class="harness__badge">{pendingLearnings}</span>
        {/if}
      </button>
    {/each}
  </nav>

  <div class="harness__body">
    {#each sections as id (id)}
      <div id={`harness-${id}`} class="harness__section" hidden={section !== id}>
        {#if id === "runtime"}
          <section class="panel">
            <h2 class="panel__heading">Model and runtime</h2>
            <dl class="harness__facts">
              <dt>Model</dt>
              <dd title={model}>
                {#if canChangeModel && models.data}
                  <span class="harness__model-change">
                    <select
                      class="field__input harness__model-select"
                      aria-label="Model"
                      disabled={frozen || updateModel.isPending}
                      value={modelChoice}
                      onchange={(event) => (pickedModel = event.currentTarget.value)}
                    >
                      {#each models.data.models as choice (choice.id)}
                        <option value={choice.id}>{choice.label}</option>
                      {/each}
                    </select>
                    <button
                      type="button"
                      class="btn"
                      disabled={frozen || updateModel.isPending || modelChoice === model}
                      onclick={handleChangeModel}
                    >
                      {updateModel.isPending ? "Changing…" : "Change model"}
                    </button>
                  </span>
                {:else}
                  {modelLabel}
                {/if}
              </dd>
              <dt>Runtime</dt>
              <dd class="harness__ref">{runtimeImage ?? "Not reported."}</dd>
              {#if colleagueMode && !connected}
                <dt>Asks other agents</dt>
                <dd>
                  {#if canChangeModel}
                    <span class="harness__model-change">
                      <select
                        class="field__input harness__model-select"
                        aria-label="Colleague mode"
                        disabled={frozen || updateColleagueMode.isPending}
                        value={colleagueModeChoice}
                        onchange={(event) =>
                          (pickedColleagueMode = event.currentTarget.value as ColleagueMode)}
                      >
                        {#each Object.entries(COLLEAGUE_MODE_LABELS) as [value, label] (value)}
                          <option {value}>{label}</option>
                        {/each}
                      </select>
                      <button
                        type="button"
                        class="btn"
                        disabled={frozen ||
                          updateColleagueMode.isPending ||
                          colleagueModeChoice === colleagueMode}
                        onclick={handleChangeColleagueMode}
                      >
                        {updateColleagueMode.isPending ? "Changing…" : "Change"}
                      </button>
                    </span>
                  {:else}
                    {COLLEAGUE_MODE_LABELS[colleagueMode]}
                  {/if}
                  {#if colleagueModeChoice}
                    <p class="panel__note">{COLLEAGUE_MODE_NOTES[colleagueModeChoice]} {COLLEAGUE_MODE_ALWAYS}</p>
                  {/if}
                </dd>
              {/if}
            </dl>
            {#if runtimeStated && roomRuntimeImage}
              {#if !builtRuntimeImage}
                <p class="harness__status harness__status--quiet">Checking what it runs on.</p>
              {:else if behind}
                <div class="harness__state">
                  {#if runtimePinned}
                    <p class="harness__status harness__status--pinned">
                      <PushPin size={15} weight="fill" aria-hidden="true" />
                      Pinned to {versionOf(builtRuntimeImage)}. The room builds on {versionOf(roomRuntimeImage)}.
                    </p>
                  {:else}
                    <p class="harness__status harness__status--warn">
                      <WarningCircle size={15} weight="fill" aria-hidden="true" />
                      Runs on {versionOf(builtRuntimeImage)}. The room now builds on {versionOf(roomRuntimeImage)}.
                    </p>
                  {/if}
                  {#if canUpdateRuntime}
                    <button
                      type="button"
                      class="btn btn--quiet"
                      disabled={frozen || updateRuntime.isPending}
                      onclick={handleUpdateRuntime}
                    >
                      {updateRuntime.isPending ? "Updating…" : moveLabel}
                    </button>
                  {/if}
                </div>
              {:else}
                <div class="harness__state">
                  <p class="harness__status">
                    <CheckCircle size={15} weight="fill" aria-hidden="true" />
                    Runtime is up to date
                  </p>
                  {#if runtimePinned}
                    <!-- Current today, but held: it stays here when the room
                         moves. The same move clears the hold. -->
                    <p class="harness__status harness__status--pinned">
                      <PushPin size={15} weight="fill" aria-hidden="true" />
                      Pinned to {versionOf(builtRuntimeImage)}. It stays here when the room moves.
                    </p>
                    {#if canUpdateRuntime}
                      <button
                        type="button"
                        class="btn btn--quiet"
                        disabled={frozen || updateRuntime.isPending}
                        onclick={handleUpdateRuntime}
                      >
                        {updateRuntime.isPending ? "Updating…" : moveLabel}
                      </button>
                    {/if}
                  {/if}
                </div>
              {/if}
            {/if}
            {#if connected}
              <p class="panel__note">
                This agent brings its own harness. The room shows what it runs on
                and nothing more.
              </p>
            {/if}
          </section>
        {:else if id === "identity"}
          <section class="panel">
            <h2 class="panel__heading">Identity and access</h2>
            {#if identity}
              <dl class="harness__facts">
                <dt>Service account</dt>
                <dd class="harness__ref">{identity.service_account}</dd>
                <dt>Google service account</dt>
                <dd class="harness__ref">{identity.gsa_email}</dd>
                <dt>Home project</dt>
                <dd class="harness__ref">{identity.project}</dd>
              </dl>
              <h3 class="panel__subheading">Ask for access to another project</h3>
              <p class="panel__note">
                Name the project and the roles the agent needs there, one role per
                line. The room writes the request you hand to the SRE team. Basic
                roles (owner, editor, viewer, browser) are refused: ask for the
                narrowest predefined or custom role that does the job.
              </p>
              <div class="field">
                <label class="field__label" for="access-project">Project</label>
                <input
                  id="access-project"
                  class="field__input"
                  type="text"
                  bind:value={accessProject}
                  placeholder="e.g. example-data-prod"
                  autocomplete="off"
                  spellcheck="false"
                />
              </div>
              <div class="field">
                <label class="field__label" for="access-roles">Roles</label>
                <textarea
                  id="access-roles"
                  class="field__input"
                  rows="3"
                  bind:value={accessRoles}
                  placeholder="roles/bigquery.dataViewer&#10;roles/storage.objectViewer"
                  spellcheck="false"
                ></textarea>
              </div>
              {#if accessRefusal}
                <p class="field__status field__status--error" role="alert">{accessRefusal}</p>
              {/if}
              <button
                type="button"
                class="btn"
                disabled={accessRequest === null}
                onclick={copyAccessRequest}
              >
                {accessCopied ? "Copied" : "Copy the request"}
              </button>
              {#if accessRequest}
                <pre class="harness__request">{accessRequest}</pre>
              {/if}
            {:else}
              <p class="panel__note">Not reported by this room yet.</p>
            {/if}
          </section>
        {:else if id === "persona"}
          {@render persona?.()}
        {:else if id === "skills"}
          <SkillsPanel
            {agentName}
            {frozen}
            recommendWho={capability ?? ""}
            recommendWhat={purposeTags.join(" ")}
          />
        {:else if id === "tooling"}
          <ToolingPanel {agentName} {frozen} />
        {:else if id === "a2a"}
          <AgentToAgentPanel {agentName} {frozen} />
        {:else if id === "memory"}
          <MemoryPanel {agentName} {frozen} />
        {:else if id === "learnings"}
          <LearningsPanel {agentName} canDecide={canDecideLearnings} {frozen} />
        {:else if id === "activity"}
          <ActivityPanel {agentName} />
        {/if}
      </div>
    {/each}
  </div>
</div>

<style>
  .harness__request {
    margin: var(--space-sm) 0 0;
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    font-size: 0.85rem;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .panel__subheading {
    margin: var(--space-md) 0 var(--space-2xs);
    font-size: 0.95rem;
  }

  /* The list down the left in a card of its own, the section beside it.
     One column at narrow widths. */
  .harness {
    display: grid;
    grid-template-columns: 13rem minmax(0, 1fr);
    gap: var(--space-lg);
    align-items: start;
  }

  @media (max-width: 52rem) {
    .harness {
      grid-template-columns: minmax(0, 1fr);
    }
  }

  .harness__nav {
    position: sticky;
    top: var(--space-md);
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  @media (max-width: 52rem) {
    .harness__nav {
      position: static;
      flex-direction: row;
      flex-wrap: wrap;
    }
  }

  .harness__item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-xs);
    min-height: 2.6rem;
    padding: 0 var(--space-sm);
    border: none;
    border-radius: var(--radius-sm);
    background: none;
    color: var(--muted);
    font: inherit;
    font-size: 0.875rem;
    font-weight: 500;
    text-align: left;
    cursor: pointer;
    transition: background 0.15s ease, color 0.15s ease;
  }

  .harness__item:hover {
    background: var(--accent-soft);
    color: var(--ink);
  }

  .harness__item[aria-current="true"] {
    background: var(--accent-soft);
    color: var(--ink);
    box-shadow: inset 2px 0 0 var(--accent);
  }

  .harness__item-label {
    display: inline-flex;
    align-items: center;
    gap: var(--space-sm);
  }

  .harness__item :global(.harness__icon) {
    flex-shrink: 0;
    opacity: 0.8;
  }

  .harness__item[aria-current="true"] :global(.harness__icon) {
    opacity: 1;
  }

  .harness__badge {
    display: inline-flex;
    min-width: 1.15rem;
    height: 1.15rem;
    align-items: center;
    justify-content: center;
    padding: 0 0.3rem;
    border-radius: 999px;
    background: var(--accent);
    color: var(--accent-ink);
    font-family: var(--font-display-mono);
    font-size: 0.65rem;
    font-weight: 600;
  }

  .harness__body {
    display: flex;
    min-width: 0;
    flex-direction: column;
  }

  .harness__section {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
  }

  /* An author-level `display` beats the UA stylesheet's `[hidden]` rule,
     so without this every section renders stacked. */
  .harness__section[hidden] {
    display: none;
  }

  /* Each fact is a row: its name on the left, the value or control on
     the right, a hairline between rows. */
  .harness__facts {
    display: grid;
    grid-template-columns: 11rem minmax(0, 1fr);
    margin: 0;
    border-top: 1px solid var(--border);
  }

  .harness__facts dt,
  .harness__facts dd {
    padding: var(--space-md) 0;
    border-bottom: 1px solid var(--border);
  }

  .harness__facts dt {
    padding-right: var(--space-md);
    color: var(--muted);
    font-size: 0.85rem;
    line-height: 2.25rem;
  }

  .harness__facts dd {
    display: flex;
    min-width: 0;
    flex-direction: column;
    justify-content: center;
    gap: var(--space-xs);
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--ink);
    font-size: 0.875rem;
    min-height: 2.25rem;
  }

  .harness__facts dd :global(.panel__note) {
    max-width: 40rem;
    line-height: 1.55;
  }

  .harness__status {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    margin: 0;
    color: var(--ok);
    font-size: 0.85rem;
    font-weight: 500;
  }

  .harness__status--warn {
    color: var(--warn);
  }

  .harness__status--pinned {
    color: var(--ink);
  }

  /* Not read yet: a state the room is still finding out, so grey, the
     one case metadata colour is right for. */
  .harness__status--quiet {
    color: var(--muted);
    font-weight: 400;
  }

  .harness__ref {
    font-family: var(--font-display-mono);
    font-size: 0.8rem;
  }

  .harness__state {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-sm);
  }
  .harness__model-change {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-xs);
  }

  .harness__model-select {
    width: auto;
    min-width: 14rem;
  }
</style>
