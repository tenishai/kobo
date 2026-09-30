<script lang="ts">
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
  import { createMutation, useQueryClient } from "@tanstack/svelte-query";
  import type { Snippet } from "svelte";
  import LearningsPanel from "./LearningsPanel.svelte";
  import MemoryPanel from "./MemoryPanel.svelte";
  import SkillsPanel from "./SkillsPanel.svelte";
  import ToolingPanel from "./ToolingPanel.svelte";
  import { errorMessage, KIND_COMPOSED, KIND_CONNECTED } from "../lib/api";
  import { updateAgentRuntimeMutation } from "../lib/queries";
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
    roomRuntimeImage = undefined,
    canUpdateRuntime = false,
    pendingLearnings = 0,
    canDecideLearnings = false,
    capability = null,
    purposeTags = [],
    identity = null,
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
    /** The image this agent runs on, already resolved server-side against
     * the room's default. `undefined` on a room whose API predates the
     * field, which the Runtime row says plainly rather than guessing. */
    runtimeImage?: string;
    /** The image the room builds on today. */
    roomRuntimeImage?: string;
    /** Whether the update is this caller's to make: an operator, per the
     * same flag the Convert row reads. The route's own owner-or-operator
     * gate stays the real enforcement. */
    canUpdateRuntime?: boolean;
    pendingLearnings?: number;
    canDecideLearnings?: boolean;
    capability?: string | null;
    purposeTags?: string[];
    /** The identity the agent runs as (`AgentDetail.identity`); absent on an
     * older server, in which case the section says so. */
    identity?: AgentIdentity | null;
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
  const sections = $derived(harnessSections(kind));

  /** The version half of an image ref: the digest after an `@`, else the
   * tag after the last `:`, else (an untagged ref) the ref itself. Used
   * only in the Behind line, where the full ref is already on the row above
   * and repeating it would bury the one word that matters. */
  function versionOf(ref: string): string {
    const at = ref.lastIndexOf("@");
    if (at !== -1) {
      return ref.slice(at + 1);
    }
    const colon = ref.lastIndexOf(":");
    return colon > ref.lastIndexOf("/") ? ref.slice(colon + 1) : ref;
  }

  // Three states, and the middle one is the only one with a control:
  // nothing to compare (an older room), current, behind.
  const runtimeKnown = $derived(Boolean(runtimeImage && roomRuntimeImage));
  const behind = $derived(runtimeKnown && runtimeImage !== roomRuntimeImage);

  const queryClient = useQueryClient();
  // svelte-ignore state_referenced_locally
  const updateRuntime = createMutation(() =>
    updateAgentRuntimeMutation(queryClient, agentName),
  );

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
      <button
        type="button"
        class="harness__item"
        aria-current={section === id}
        aria-controls={`harness-${id}`}
        onclick={() => (section = id)}
      >
        <span>{HARNESS_SECTION_LABELS[id]}</span>
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
              <dd title={model}>{modelLabel}</dd>
              <dt>Runtime</dt>
              <dd class="harness__ref">{runtimeImage ?? "Not reported."}</dd>
            </dl>
            {#if runtimeKnown && roomRuntimeImage}
              {#if behind}
                <div class="harness__state">
                  <p class="panel__note">
                    Behind: the room now builds on {versionOf(roomRuntimeImage)}
                  </p>
                  {#if canUpdateRuntime}
                    <button
                      type="button"
                      class="btn btn--quiet"
                      disabled={frozen || updateRuntime.isPending}
                      onclick={handleUpdateRuntime}
                    >
                      {updateRuntime.isPending ? "Updating…" : "Update to current"}
                    </button>
                  {/if}
                </div>
              {:else}
                <p class="panel__note">Current</p>
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
        {:else if id === "memory"}
          <MemoryPanel {agentName} {frozen} />
        {:else if id === "learnings"}
          <LearningsPanel {agentName} canDecide={canDecideLearnings} {frozen} />
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

  /* The list down the left, the section beside it. One column at narrow
     widths, where a 10rem gutter would leave the panels too cramped to
     read. */
  .harness {
    display: grid;
    grid-template-columns: 10rem minmax(0, 1fr);
    gap: var(--space-lg);
  }

  @media (max-width: 44rem) {
    .harness {
      grid-template-columns: minmax(0, 1fr);
    }
  }

  .harness__nav {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  @media (max-width: 44rem) {
    .harness__nav {
      flex-direction: row;
      flex-wrap: wrap;
    }
  }

  /* Small caps, quiet: the list names the sections, it does not compete
     with the panel beside it. */
  .harness__item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2xs);
    padding: var(--space-2xs) var(--space-xs);
    border: none;
    border-left: 2px solid transparent;
    border-radius: 0;
    background: none;
    color: var(--muted);
    font: inherit;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-align: left;
    text-transform: uppercase;
    cursor: pointer;
  }

  .harness__item:hover {
    color: var(--ink);
  }

  .harness__item[aria-current="true"] {
    border-left-color: var(--accent);
    color: var(--ink);
  }

  @media (max-width: 44rem) {
    .harness__item {
      border-bottom: 2px solid transparent;
      border-left: none;
    }

    .harness__item[aria-current="true"] {
      border-bottom-color: var(--accent);
    }
  }

  .harness__badge {
    display: inline-block;
    min-width: 1.2em;
    padding: 0 0.35em;
    border-radius: 999px;
    background: var(--accent);
    color: var(--accent-ink);
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0;
    line-height: 1.5;
    text-align: center;
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

  /* Two columns: the fact's name, then the fact. */
  .harness__facts {
    display: grid;
    grid-template-columns: 7rem minmax(0, 1fr);
    gap: var(--space-2xs) var(--space-sm);
    margin: 0;
  }

  .harness__facts dt {
    color: var(--muted);
    font-size: 0.8rem;
  }

  .harness__facts dd {
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--ink);
    font-size: 0.85rem;
  }

  .harness__ref {
    font-family: var(--font-mono);
    font-size: 0.78rem;
  }

  .harness__state {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-sm);
  }
</style>
