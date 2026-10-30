<script lang="ts">
  // What a ready agent can do, on top of what it knows (MemoryPanel's
  // `bundle/knowledge/*.md`). Two provenance classes share one list: a
  // catalog pick (read-only content, lives in the org's skills catalog —
  // Remove only) and a local, guardrail-authored skill (edited here —
  // Edit + Remove). Same rebuild-the-user-never-names convention as
  // MemoryPanel: copy speaks only of saving/adding/removing, never
  // "commit"/"deploy"/"repo"/"pipeline".
  //
  // Three affordances, three different shapes:
  //   - One picker over two lists (issue #266): "Browse the catalog" (an
  //     inline, toggleable debounced search adapted from CreateModal's old
  //     skill search) and "Suggestions" each show a checkbox per row; the
  //     ticks feed one sticky "Save N skills" bar that sends ONE batch
  //     request at once, no second confirm (issue #287). The chosen rows
  //     appear in the table the moment Save is pressed and wear a state
  //     chip (Pending, Building, Rolling, Active, Failed) that follows the
  //     delivery of the sha the save returned; errors show in both lists.
  //   - "New guardrail skill": opens the shared editor Modal (copied from
  //     MemoryPanel: filename + textarea, review-with-diff, then save).
  //   - Edit / Remove on an existing row: Edit re-opens that same Modal in
  //     edit mode (after fetching the skill's current content — unlike
  //     MemoryPanel's knowledge list, `agentSkillsQuery` doesn't carry
  //     content, only provenance, so edit mode has its own tiny load state
  //     ahead of the familiar review/diff/save flow); Remove opens a
  //     confirm step, same Modal again.
  //
  // Delivery reactivity: `deliveryQuery` is shared with MemoryPanel/SlackPanel
  // as-is (one poll per sha, stops on a terminal state). It checks
  // git ancestry vs. the live image for a given sha, a question that has
  // nothing knowledge-specific about it despite living under a
  // "knowledge/delivery" path — any sha this room pushes (a knowledge save,
  // a skill add/edit/remove) is delivered and checked the same way.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { untrack } from "svelte";
  import ErrorState from "./ErrorState.svelte";
  import Modal from "./Modal.svelte";
  import Skeleton from "./Skeleton.svelte";
  import { errorMessage, readLocalSkill, type AgentSkill, type CatalogEntry } from "../lib/api";
  import { debounced } from "../lib/debounce";
  import {
    beginDelivery as noteDeliveryBegan,
    reportDelivery as noteDeliveryState,
  } from "../lib/delivery.svelte";
  import { lineDiff, type DiffLine } from "../lib/diff";
  import { toast } from "../lib/toast.svelte";
  import {
    addCatalogSkillsMutation,
    agentSkillsQuery,
    deleteSkillMutation,
    deliveryQuery,
    recommendedSkillsQuery,
    saveLocalSkillMutation,
    skillsSearchQuery,
    teamsQuery,
  } from "../lib/queries";

  let {
    agentName,
    frozen = false,
    recommendWho = "",
    recommendWhat = "",
  }: { agentName: string; frozen?: boolean; recommendWho?: string; recommendWhat?: string } =
    $props();

  const queryClient = useQueryClient();
  // `agentName` is read once here rather than tracked reactively: same
  // reasoning as MemoryPanel/ChatPanel — AgentDetail (the only caller)
  // remounts this component via `{#key route.name}` whenever the routed
  // agent changes, so this prop is fixed for the component's entire lifetime.
  // svelte-ignore state_referenced_locally
  const skills = createQuery(() => (agentSkillsQuery(agentName)));
  // svelte-ignore state_referenced_locally
  const teams = createQuery(() => (teamsQuery()));
  // `recommendWho`/`recommendWhat` are AgentDetail's own `capability`/
  // `purpose_tags` for this same agent — fixed for the component's lifetime
  // by the same remount-per-agent reasoning as `agentName` above.
  // svelte-ignore state_referenced_locally
  const recommended = createQuery(() => (recommendedSkillsQuery(recommendWho, recommendWhat)));
  // svelte-ignore state_referenced_locally
  const addPicked = createMutation(() => (addCatalogSkillsMutation(queryClient, agentName)));
  // svelte-ignore state_referenced_locally
  const saveMutation = createMutation(() => (saveLocalSkillMutation(queryClient, agentName)));
  // svelte-ignore state_referenced_locally
  const deleteMutation = createMutation(() => (deleteSkillMutation(queryClient, agentName)));

  // --- Delivery, per row (issues #266, #287) -------------------------------
  //
  // Every change this panel pushes comes back with a sha. The rows that sha
  // carried (`carried`, by skill name) wear a chip that follows the delivery
  // poll of that sha: Pending (saved, or still being saved), Building (the
  // agent is being rebuilt), Rolling (the new agent is starting), Active
  // (in place; the chip fades after a few seconds and the row is an ordinary
  // row again), Failed (the server's sentence; the row stays until the owner
  // removes it or saves again). One state per sha applies to every row it
  // carried. Only the newest sha is polled: a later commit carries the
  // earlier content too, so a new save re-points every row still on its way
  // onto its own sha, and that one delivery answers for all of them. A row
  // with no sha yet is a save whose request is still in flight.

  const DELIVERED_CLEAR_MS = 5000;

  type Phase = "pending" | "building" | "rolling" | "delivered" | "failed" | "unknown";
  type Carry = { sha: string | null; row: AgentSkill };
  type ShaState = { state: string; detail: string | null };

  let carried = $state<Record<string, Carry>>({});
  /** What the poll has said so far about each sha this panel pushed. */
  const shaStates = $state<Record<string, ShaState>>({});
  let deliverySha = $state<string | null>(null);
  const delivery = createQuery(() => (deliveryQuery(agentName, deliverySha)));

  let clearTimer: ReturnType<typeof setTimeout> | undefined;

  function phaseOf(sha: string | null): Phase {
    if (sha === null) {
      return "pending";
    }
    const known = shaStates[sha];
    if (known === undefined) {
      return "pending";
    }
    switch (known.state) {
      case "building":
      case "rolling":
      case "delivered":
      case "failed":
      case "unknown":
        return known.state;
      default:
        // A state this code does not know yet is still on its way: never
        // claim success or failure it cannot back up.
        return "pending";
    }
  }

  /** A save just returned `sha` for `names`. Rows still on their way (from
   * an earlier save) ride this sha from now on; rows already Active are
   * done and leave the record. */
  function beginDelivery(sha: string, names: string[]): void {
    if (clearTimer !== undefined) {
      clearTimeout(clearTimer);
      clearTimer = undefined;
    }
    const next: Record<string, Carry> = {};
    for (const [name, carry] of Object.entries(carried)) {
      if (names.includes(name)) {
        next[name] = { ...carry, sha };
      } else if (phaseOf(carry.sha) !== "delivered") {
        next[name] = { ...carry, sha };
      }
    }
    carried = next;
    deliverySha = sha;
    // The page reads this to freeze its controls and say Updating while
    // the rebuild this push started is on its way (issues #268, #290).
    noteDeliveryBegan(agentName, sha);
  }

  // Every poll answer is recorded against its sha (the rows read it) and
  // goes to the page-wide delivery record (the header and the freeze).
  // The writes are untracked: both records are read by what this effect
  // writes to, and an effect that depends on its own writes never settles.
  $effect(() => {
    const sha = deliverySha;
    if (sha === null) {
      return;
    }
    const answer: ShaState | null = delivery.isError
      ? {
          state: "unknown",
          detail: errorMessage(delivery.error, "Couldn't check on your changes."),
        }
      : (delivery.data ?? null);
    if (answer === null) {
      return;
    }
    untrack(() => {
      shaStates[sha] = answer;
      noteDeliveryState(agentName, sha, answer.state);
    });
  });

  // Active fades: a few seconds after the newest sha lands, the rows it
  // carried are ordinary rows again (the skills query already lists them).
  $effect(() => {
    const sha = deliverySha;
    if (sha === null || shaStates[sha]?.state !== "delivered") {
      return;
    }
    clearTimer = setTimeout(() => {
      const next: Record<string, Carry> = {};
      for (const [name, carry] of Object.entries(carried)) {
        if (carry.sha !== sha) {
          next[name] = carry;
        }
      }
      carried = next;
      deliverySha = null;
      clearTimer = undefined;
    }, DELIVERED_CLEAR_MS);
    return () => {
      if (clearTimer !== undefined) {
        clearTimeout(clearTimer);
        clearTimer = undefined;
      }
    };
  });

  type Chip = { tone: "pending" | "ok" | "fail" | "unknown"; label: string; text: string | null };

  /** The chip a row (or the table head) wears for a phase. `detail` is the
   * server's sentence, shown for Failed and for a poll that could not be
   * made. */
  function chipFor(phase: Phase, detail: string | null): Chip {
    switch (phase) {
      case "building":
        return { tone: "pending", label: "Building", text: null };
      case "rolling":
        return { tone: "pending", label: "Rolling", text: null };
      case "delivered":
        return { tone: "ok", label: "Active", text: null };
      case "failed":
        return {
          tone: "fail",
          label: "Failed",
          text: detail ?? "Something went wrong. Try saving again.",
        };
      case "unknown":
        return { tone: "unknown", label: "Unknown", text: detail };
      default:
        return { tone: "pending", label: "Pending", text: null };
    }
  }

  function chipOf(sha: string | null): Chip {
    return chipFor(phaseOf(sha), sha === null ? null : (shaStates[sha]?.detail ?? null));
  }

  /** The table head wears the chip only when no row does: a removal has no
   * row left to carry it. */
  const headChip = $derived.by((): Chip | null => {
    const sha = deliverySha;
    if (sha === null || Object.values(carried).some((carry) => carry.sha === sha)) {
      return null;
    }
    return chipOf(sha);
  });

  // --- The picker: debounced catalog search + suggestions, ticks, one save -
  // (search markup adapted from CreateModal's skill search — see its own
  // doc comment for why the debounced term lives in a plain `writable`
  // rather than `$derived`; same @tanstack/svelte-query version constraint.)

  let catalogOpen = $state(false);
  // The suggestions disclosure, separate from the catalog one so neither
  // hides the other. Both shut on arrival.
  let suggestionsOpen = $state(false);
  let catalogQ = $state("");
  // "" means "All teams" — a <select>'s DOM value is always a string;
  // converted to `null` only where the search params are committed, below.
  let catalogTeamFilter = $state("");
  // One error for the whole picker, rendered in BOTH lists: a failed add
  // from Suggestions used to show nothing at all (issue #266).
  let pickerError = $state<string | null>(null);
  // Ticked rows, by skill name, across both lists. Reassigned on every
  // change so the derived count and the row checkboxes follow.
  let picked = $state<Record<string, CatalogEntry>>({});
  const pickedCount = $derived(Object.keys(picked).length);
  let catalogSearchParams = $state<{ q: string; team: string | null }>({
    q: "",
    team: null,
  });
  const catalogResults = createQuery(() => (skillsSearchQuery(catalogSearchParams.q, catalogSearchParams.team)));

  const commitCatalogSearch = debounced((value: { q: string; team: string | null }) => {
    catalogSearchParams = value;
  }, 250);

  $effect(() => {
    const value = { q: catalogQ, team: catalogTeamFilter === "" ? null : catalogTeamFilter };
    if (!catalogOpen) {
      return;
    }
    commitCatalogSearch(value);
    return () => {
      commitCatalogSearch.cancel();
    };
  });

  // Search combobox (shape ported from CreateModal's old skill-search
  // station, adapted to this panel's BEM root and single-select-with-
  // immediate-add semantics): the results list only shows while focus is
  // somewhere in the search row or the dropdown itself, closing once focus
  // leaves that subtree entirely — an Add click keeps focus inside (it moves
  // to the button, still a descendant of the combobox root), so it never
  // closes the dropdown out from under itself.
  let catalogDropdownOpen = $state(false);

  function closeCatalogDropdownOnFocusout(event: FocusEvent): void {
    const next = event.relatedTarget;
    const root = event.currentTarget as HTMLElement;
    if (!(next instanceof Node) || !root.contains(next)) {
      catalogDropdownOpen = false;
    }
  }

  /** Toggles the catalog search section, resetting it fresh every time it
   * opens — same "clean slate on open" idiom as CreateModal's own reset
   * effect, just scoped to this toggle instead of a whole-modal `open`. */
  function toggleCatalog(): void {
    catalogOpen = !catalogOpen;
    if (catalogOpen) {
      catalogQ = "";
      catalogTeamFilter = "";
      pickerError = null;
      catalogSearchParams = { q: "", team: null };
      catalogDropdownOpen = false;
    }
  }

  /** Carried already, or on its way: either way the row says Added. */
  function alreadyCarried(skillName: string): boolean {
    return (skills.data?.some((s) => s.name === skillName) ?? false) || skillName in carried;
  }

  function togglePick(entry: CatalogEntry): void {
    if (alreadyCarried(entry.name)) {
      return;
    }
    const next = { ...picked };
    if (entry.name in next) {
      delete next[entry.name];
    } else {
      next[entry.name] = entry;
    }
    picked = next;
  }

  function clearPicks(): void {
    picked = {};
  }

  /** The table's rows: what the agent carries, then what is on its way
   * and not yet listed (the rows a save put there), each shaped like a
   * catalog pick with no pin yet. */
  const tableRows = $derived.by((): AgentSkill[] => {
    const listed = skills.data ?? [];
    const names = new Set(listed.map((s) => s.name));
    const pending = Object.values(carried)
      .map((carry) => carry.row)
      .filter((row) => !names.has(row.name));
    return [...listed, ...pending];
  });

  function saveLabel(count: number): string {
    return count === 1 ? "Save 1 skill" : `Save ${count} skills`;
  }

  /** Save at once (issue #287): the picked rows join the table as Pending
   * before the request answers; the sha it returns drives their chips from
   * then on. A refused pick leaves the table and is named in the lists; a
   * failed request takes every row back and keeps the ticks for a retry. */
  function handleSavePicked(): void {
    const entries = Object.values(picked);
    if (entries.length === 0 || addPicked.isPending) {
      return;
    }
    pickerError = null;
    const names = entries.map((entry) => entry.name);
    const next = { ...carried };
    for (const entry of entries) {
      next[entry.name] = {
        sha: null,
        row: { name: entry.name, team: entry.team, source: "catalog", sha: null, editable: false },
      };
    }
    carried = next;
    addPicked.mutate(
      entries.map((entry) => ({ name: entry.name, team: entry.team })),
      {
        onSuccess: (result) => {
          const kept = { ...carried };
          for (const name of names) {
            if (!result.added.includes(name)) {
              delete kept[name];
            }
          }
          carried = kept;
          if (result.added.length > 0) {
            beginDelivery(result.sha, result.added);
          }
          if (result.refused.length > 0) {
            pickerError =
              "Not added: " +
              result.refused.map((r) => `${r.name} (${r.reason})`).join("; ") +
              ".";
          }
          picked = {};
        },
        onError: (err: unknown) => {
          const kept = { ...carried };
          for (const name of names) {
            delete kept[name];
          }
          carried = kept;
          const message = errorMessage(err, "Couldn't save those skills. Try again.");
          pickerError = message;
          toast.error("Couldn't save the skills", message);
        },
      },
    );
  }

  // --- Modal: new/edit local skill, or delete (either class) --------------

  type Target =
    | { kind: "edit"; skill: AgentSkill }
    | { kind: "add" }
    | { kind: "delete"; skill: AgentSkill };

  type EditLoad = { status: "loading" } | { status: "error"; message: string } | { status: "ready" };

  let target = $state<Target | null>(null);
  let content = $state("");
  let originalContent = $state("");
  let filename = $state("");
  let reviewing = $state(false);
  let modalError = $state<string | null>(null);
  let editLoad = $state<EditLoad>({ status: "ready" });

  // Above this many (before-lines × after-lines) cells, the LCS table in
  // diff.ts's O(n·m) algorithm gets expensive enough that it's not worth
  // paying for a preview the user is about to skim past anyway.
  const DIFF_LINE_PRODUCT_LIMIT = 1_000_000;

  /** Line count under diff.ts's own convention: an empty string is zero
   * lines, not one (see diff.ts's `splitLines`). This only estimates the
   * cost of a diff for the guard below — it doesn't compute one. */
  function lineCount(text: string): number {
    return text === "" ? 0 : text.split("\n").length;
  }

  function diffMarker(kind: DiffLine["kind"]): string {
    switch (kind) {
      case "added":
        return "+ ";
      case "removed":
        return "− ";
      default:
        return "  ";
    }
  }

  function shortSha(sha: string): string {
    return sha.slice(0, 12);
  }

  /** The provenance line under a row's name: `team · sha12` for a catalog
   * pick, `local · editable` for a guardrail file. */
  function provenanceLine(skill: AgentSkill): string {
    if (skill.source === "local") {
      return "local · editable";
    }
    const team = skill.team ?? "unknown team";
    const sha = skill.sha !== null ? shortSha(skill.sha) : "no pin yet";
    return `${team} · ${sha}`;
  }

  // A new skill starts from a template (#265): the frontmatter the runtime
  // requires, with name: following the skill name field while the template
  // is untouched, and a body stub to replace. The template counts as
  // "unchanged" so Review waits for the owner to write something.
  function skillTemplate(name: string): string {
    return (
      `---\nname: ${name || "skill-name"}\n` +
      "description: One line on when the agent should use this.\n" +
      "---\n\n# What to do\n\nWrite the guardrail here: what to check, what to refuse, what to say.\n"
    );
  }
  let templateName = $state("");

  function openAdd(): void {
    target = { kind: "add" };
    templateName = "";
    content = skillTemplate("");
    originalContent = content;
    filename = "";
    reviewing = false;
    modalError = null;
    editLoad = { status: "ready" };
  }

  $effect(() => {
    if (target?.kind !== "add") return;
    const next = filename;
    if (content === skillTemplate(templateName) && next !== templateName) {
      content = skillTemplate(next);
      originalContent = content;
      templateName = next;
    }
  });

  const missingFrontmatter = $derived(
    target?.kind === "add" && content.trim().length > 0 && !content.startsWith("---"),
  );

  /** Opens the editor on an existing local skill, fetching its current
   * content first (unlike MemoryPanel's knowledge files, `AgentSkill` from
   * the list query carries provenance only, not content). Guards against a
   * stale response landing after the user has already closed the modal or
   * moved on to a different skill — same idiom as CreateModal's debounced
   * availability check guarding on `value !== name`. */
  async function openEdit(skill: AgentSkill): Promise<void> {
    const requestedName = skill.name;
    target = { kind: "edit", skill };
    content = "";
    originalContent = "";
    filename = "";
    reviewing = false;
    modalError = null;
    editLoad = { status: "loading" };
    try {
      const result = await readLocalSkill(agentName, requestedName);
      if (target?.kind !== "edit" || target.skill.name !== requestedName) {
        return;
      }
      content = result.content;
      originalContent = result.content;
      editLoad = { status: "ready" };
    } catch (err) {
      if (target?.kind !== "edit" || target.skill.name !== requestedName) {
        return;
      }
      editLoad = { status: "error", message: errorMessage(err, "Couldn't load this skill.") };
    }
  }

  function openDelete(skill: AgentSkill): void {
    target = { kind: "delete", skill };
    modalError = null;
  }

  function closeModal(): void {
    target = null;
    content = "";
    originalContent = "";
    filename = "";
    reviewing = false;
    modalError = null;
    editLoad = { status: "ready" };
  }

  function backToEditing(): void {
    reviewing = false;
    modalError = null;
  }

  const contentUnchanged = $derived(content === originalContent);

  const filenameError = $derived.by((): string | null => {
    if (target?.kind !== "add") {
      return null;
    }
    if (filename.length === 0) {
      return null; // nothing to say about an empty field yet
    }
    if (filename.length > 40) {
      return "Use 1-40 characters.";
    }
    if (!/^[a-z0-9-]+$/.test(filename)) {
      return "Use lowercase letters, numbers, and hyphens only.";
    }
    if (!/^[a-z]/.test(filename)) {
      return "Must start with a letter.";
    }
    if (alreadyCarried(filename)) {
      return "There's already a skill with this name.";
    }
    return null;
  });

  const canReview = $derived.by((): boolean => {
    if (target === null || target.kind === "delete") {
      return false;
    }
    if (target.kind === "edit" && editLoad.status !== "ready") {
      return false;
    }
    if (content.trim().length === 0 || contentUnchanged) {
      return false;
    }
    if (target.kind === "add" && (filename.length === 0 || filenameError !== null)) {
      return false;
    }
    return true;
  });

  const diffTooLarge = $derived(
    lineCount(originalContent) * lineCount(content) > DIFF_LINE_PRODUCT_LIMIT,
  );

  const diffLines = $derived.by((): DiffLine[] => {
    if (!reviewing || diffTooLarge) {
      return [];
    }
    return lineDiff(originalContent, content);
  });

  const modalTitle = $derived.by((): string | undefined => {
    if (target === null) {
      return undefined;
    }
    if (target.kind === "add") {
      return "New guardrail skill";
    }
    if (target.kind === "delete") {
      return `Remove ${target.skill.name}`;
    }
    return `Edit ${target.skill.name}`;
  });

  /** A guardrail skill saved from the editor ends in the same table with
   * the same chips as a catalog pick (issue #287): the row is added (or,
   * for an edit, marked) and rides the sha the save returned. */
  function handleSave(): void {
    if (target === null || target.kind === "delete") {
      return;
    }
    const skillId = target.kind === "edit" ? target.skill.name : filename;
    const row: AgentSkill =
      target.kind === "edit"
        ? target.skill
        : { name: skillId, team: null, source: "local", sha: null, editable: true };
    modalError = null;
    saveMutation.mutate(
      { skillId, content },
      {
        onSuccess: (result) => {
          carried = { ...carried, [skillId]: { sha: result.sha, row } };
          beginDelivery(result.sha, [skillId]);
          closeModal();
        },
        onError: (err: unknown) => {
          modalError = errorMessage(err, "Couldn't save. Try again.");
        },
      },
    );
  }

  function handleDelete(): void {
    if (target?.kind !== "delete") {
      return;
    }
    const skillId = target.skill.name;
    modalError = null;
    deleteMutation.mutate(skillId, {
      onSuccess: (result) => {
        const kept = { ...carried };
        delete kept[skillId];
        carried = kept;
        beginDelivery(result.sha, []);
        closeModal();
      },
      onError: (err: unknown) => {
        modalError = errorMessage(err, "Couldn't remove it. Try again.");
      },
    });
  }
</script>

<section class="panel" aria-labelledby="skills-panel-heading">
  <div class="panel__header">
    <h2 id="skills-panel-heading" class="panel__heading">Skills</h2>
    <p class="panel__lead">What your agent can do, on top of what it knows.</p>
  </div>

  <!-- The two disclosures stay usable while frozen: they reveal lists, they
       change nothing. Every Add inside them is dead. -->
  <div class="panel__toolbar">
    <div class="panel__tools">
      <button type="button" class="btn" disabled={frozen} onclick={openAdd}>
        New guardrail skill
      </button>
      <button
        type="button"
        class="btn"
        aria-expanded={catalogOpen}
        onclick={toggleCatalog}
      >
        Browse the catalog
      </button>
      <button
        type="button"
        class="btn"
        aria-expanded={suggestionsOpen}
        onclick={() => (suggestionsOpen = !suggestionsOpen)}
      >
        Suggestions
      </button>
    </div>
  </div>

  {#snippet pickRow(entry: CatalogEntry)}
    {@const already = alreadyCarried(entry.name)}
    <li class="catalog-row">
      <span class="catalog-row__body">
        <span class="catalog-row__head">
          <span class="catalog-row__name">{entry.name}</span>
          <span class="skills-panel__badge">{entry.team}</span>
        </span>
        <span class="catalog-row__description">{entry.description}</span>
      </span>
      <label class="catalog-row__pick" class:catalog-row__pick--added={already}>
        <input
          type="checkbox"
          checked={already || entry.name in picked}
          disabled={frozen || already}
          aria-label={`Add ${entry.name}`}
          onchange={() => togglePick(entry)}
        />
        <span>{already ? "Added" : "Add"}</span>
      </label>
    </li>
  {/snippet}

  {#if suggestionsOpen}
    <div class="panel__drawer skills-panel__catalog">
      {#if pickerError}
        <p class="panel__note panel__note--fail" role="alert">{pickerError}</p>
      {/if}
      {#if recommended.data && recommended.data.length > 0}
        <p class="panel__note">Recommended for this agent</p>
        <ul class="skills-panel__catalog-list">
          {#each recommended.data as entry (entry.team + "/" + entry.name)}
            {@render pickRow(entry)}
          {/each}
        </ul>
      {:else}
        <p class="panel__note">
          Nothing to suggest yet. Write what this agent is for, and suggestions follow.
        </p>
      {/if}
    </div>
  {/if}

  {#if catalogOpen}
    <div class="panel__drawer skills-panel__catalog">
      {#if pickerError}
        <p class="panel__note panel__note--fail" role="alert">{pickerError}</p>
      {/if}

      <div
        class="skills-panel__combobox"
        onfocusin={() => (catalogDropdownOpen = true)}
        onfocusout={closeCatalogDropdownOnFocusout}
      >
        <div class="skills-panel__catalog-row">
          <label class="skills-panel__sr-only" for="skills-catalog-search">
            Search skills catalog
          </label>
          <input
            id="skills-catalog-search"
            class="skills-panel__catalog-input"
            type="text"
            bind:value={catalogQ}
            placeholder="Search skills"
            autocomplete="off"
            disabled={frozen}
          />
          <label class="skills-panel__sr-only" for="skills-catalog-team">Filter by team</label>
          <select
            id="skills-catalog-team"
            class="skills-panel__catalog-input"
            value={catalogTeamFilter}
            disabled={frozen}
            onchange={(event) =>
              (catalogTeamFilter = (event.currentTarget as HTMLSelectElement).value)}
          >
            <option value="">All teams</option>
            {#if teams.data}
              {#each teams.data as t (t)}
                <option value={t}>{t}</option>
              {/each}
            {/if}
          </select>
        </div>

        {#if catalogDropdownOpen}
          <div class="skills-panel__dropdown" aria-label="Skills catalog">
            {#if catalogResults.isPending}
              <Skeleton rows={2} />
            {:else if catalogResults.isError}
              <ErrorState
                message={errorMessage(catalogResults.error, "Couldn't load the skills catalog.")}
                onretry={() => catalogResults.refetch()}
              />
            {:else if catalogResults.data.length === 0}
              <p class="skills-panel__catalog-help">
                {catalogQ.length > 0 ? "Nothing matches your search." : "No skills in the catalog yet."}
              </p>
            {:else}
              <ul class="skills-panel__catalog-list">
                {#each catalogResults.data as entry (entry.id)}
                  {@render pickRow(entry)}
                {/each}
              </ul>
            {/if}
          </div>
        {/if}
      </div>
    </div>
  {/if}

  {#if pickedCount > 0}
    <!-- Sticks to the bottom of the view while the lists above are long,
         so the save is always in reach. One press saves (issue #287). -->
    <div class="skills-panel__pickbar" role="region" aria-label="Skills picked">
      <span class="skills-panel__pickbar-count">
        {pickedCount === 1 ? "1 skill picked" : `${pickedCount} skills picked`}
      </span>
      <span class="skills-panel__pickbar-actions">
        <button type="button" class="btn btn--quiet" disabled={frozen} onclick={clearPicks}>
          Clear
        </button>
        <button
          type="button"
          class="btn btn--primary"
          disabled={frozen || addPicked.isPending}
          onclick={handleSavePicked}
        >
          {addPicked.isPending ? "Saving…" : saveLabel(pickedCount)}
        </button>
      </span>
    </div>
  {/if}

  <!-- A change with no row to wear its chip (a removal) reports here,
       beside the table, so it is in view without scrolling (issue #266). -->
  <div class="skills-panel__table-head">
    <h3 class="panel__subheading">What it carries</h3>
    {#if headChip !== null}
      <div
        class="skills-panel__delivery skills-panel__delivery--{headChip.tone}"
        role="status"
        aria-live="polite"
      >
        {#if headChip.tone === "pending"}
          <span class="skills-panel__delivery-dot pulse" aria-hidden="true"></span>
        {/if}
        <span>{headChip.label}</span>
        {#if headChip.text !== null}
          <span class="skills-panel__delivery-text">{headChip.text}</span>
        {/if}
      </div>
    {/if}
  </div>

  {#if skills.isPending}
    <Skeleton rows={3} />
  {:else if skills.isError}
    <ErrorState
      message={errorMessage(skills.error, "Couldn't load this agent's skills.")}
      onretry={() => skills.refetch()}
    />
  {:else}
    <table class="panel__table">
      <thead>
        <tr>
          <th scope="col" class="skills-panel__col-name">Skill</th>
          <th scope="col">Where it came from</th>
          <th scope="col" class="skills-panel__col-action">
            <span class="panel__sr">Actions</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {#if tableRows.length === 0}
          <tr>
            <td class="panel__empty" colspan="3">
              Nothing here yet. Add a skill from the catalog or write a guardrail.
            </td>
          </tr>
        {/if}
        {#each tableRows as skill (skill.name)}
          {@const carry = carried[skill.name]}
          {@const chip = carry === undefined ? null : chipOf(carry.sha)}
          <!-- Edit and Remove rest only while the row is on its way. A
               Failed row keeps both: removing it or saving again is how
               the owner moves on. -->
          {@const onItsWay = chip !== null && chip.tone === "pending"}
          <tr class="skill-row">
            <td><code class="panel__name">{skill.name}</code></td>
            <td>
              <span class="skill-row__provenance">{provenanceLine(skill)}</span>
              {#if chip !== null}
                <span class="skill-row__state skill-row__state--{chip.tone}">
                  {#if chip.tone === "pending"}
                    <span class="skills-panel__delivery-dot pulse" aria-hidden="true"></span>
                  {/if}
                  {chip.label}
                </span>
                {#if chip.text !== null}
                  <span class="skill-row__state-text">{chip.text}</span>
                {/if}
              {/if}
            </td>
            <td class="panel__cell--right">
              <span class="skill-row__actions">
                {#if skill.editable}
                  <button
                    type="button"
                    class="btn btn--quiet"
                    disabled={frozen || onItsWay}
                    aria-label={`Edit ${skill.name}`}
                    onclick={() => openEdit(skill)}
                  >
                    Edit
                  </button>
                {/if}
                <button
                  type="button"
                  class="btn btn--quiet btn--danger"
                  disabled={frozen || onItsWay}
                  aria-label={`Remove ${skill.name}`}
                  onclick={() => openDelete(skill)}
                >
                  Remove
                </button>
              </span>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</section>

<Modal open={target !== null} title={modalTitle} onclose={closeModal}>
  {#if target}
    {@const t = target}
    {#if t.kind === "delete"}
      <p class="skills-modal__confirm-text">
        This removes it from what {agentName} can do. Remove?
      </p>
      {#if modalError}
        <p class="skills-modal__error" role="alert">{modalError}</p>
      {/if}
      <div class="skills-modal__actions">
        <button type="button" class="skills-modal__cancel" disabled={frozen} onclick={closeModal}>Cancel</button>
        <button
          type="button"
          class="skills-modal__danger"
          disabled={frozen || deleteMutation.isPending}
          onclick={handleDelete}
        >
          {deleteMutation.isPending ? "Removing…" : "Remove"}
        </button>
      </div>
    {:else if t.kind === "edit" && editLoad.status !== "ready"}
      {@const load = editLoad}
      {#if load.status === "loading"}
        <Skeleton rows={4} />
      {:else if load.status === "error"}
        <ErrorState message={load.message} onretry={() => openEdit(t.skill)} />
      {/if}
    {:else if reviewing}
      {#if diffTooLarge}
        <p class="skills-modal__diff-notice">
          Too much changed to preview line by line. Review your text, then save.
        </p>
      {:else}
        <div class="skills-modal__diff">
          {#each diffLines as line, i (i)}
            <p class="skills-diff-line skills-diff-line--{line.kind}">{diffMarker(
                line.kind,
              )}{line.text}</p>
          {/each}
        </div>
      {/if}
      {#if modalError}
        <p class="skills-modal__error" role="alert">{modalError}</p>
      {/if}
      <div class="skills-modal__actions">
        <button type="button" class="skills-modal__cancel" disabled={frozen} onclick={backToEditing}>
          Back to editing
        </button>
        <button
          type="button"
          class="skills-modal__primary"
          disabled={frozen || saveMutation.isPending}
          onclick={handleSave}
        >
          {saveMutation.isPending ? "Saving…" : "Save"}
        </button>
      </div>
    {:else}
      <form
        class="skills-form"
        onsubmit={(event) => {
          event.preventDefault();
          if (canReview) {
            reviewing = true;
          }
        }}
      >
        {#if t.kind === "add"}
          <div class="skills-form__field">
            <label class="skills-form__label" for="skills-filename">Name</label>
            <input
              id="skills-filename"
              class="skills-form__input"
              type="text"
              bind:value={filename}
              autocomplete="off"
              spellcheck="false"
              aria-describedby="skills-filename-status"
              aria-invalid={filenameError !== null}
              disabled={frozen}
            />
            <p class="skills-form__help">
              Lowercase letters, numbers, and hyphens. 1-40 characters.
            </p>
            <div id="skills-filename-status">
              {#if filenameError}
                <p class="skills-form__status skills-form__status--error" role="alert">
                  {filenameError}
                </p>
              {/if}
            </div>
          </div>
        {:else}
          <p class="skills-form__filename">{t.skill.name}</p>
        {/if}

        <div class="skills-form__field">
          <label class="skills-form__label" for="skills-content">Guardrail text</label>
          <textarea
            id="skills-content"
            class="skills-form__textarea"
            bind:value={content}
            rows="10"
            disabled={frozen}
          ></textarea>
          {#if missingFrontmatter}
            <p class="skills-form__help">
              No --- block at the top: the room adds one, named after the skill, with your first
              line as the description.
            </p>
          {:else}
            <p class="skills-form__help">
              Keep the name: and description: lines at the top; write the guardrail below them.
            </p>
          {/if}
          {#if content.trim().length === 0}
            <p class="skills-form__help">Add a bit of text before saving.</p>
          {/if}
        </div>

        {#if modalError}
          <p class="skills-modal__error" role="alert">{modalError}</p>
        {/if}

        <div class="skills-modal__actions">
          <button type="button" class="skills-modal__cancel" disabled={frozen} onclick={closeModal}>Cancel</button>
          <button type="submit" class="skills-modal__primary" disabled={frozen || !canReview}>
            {contentUnchanged ? "No changes yet" : "Review changes"}
          </button>
        </div>
      </form>
    {/if}
  {/if}
</Modal>

<style>
  .skills-panel__sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  .skill-row__provenance {
    color: var(--muted);
    font-size: 0.8rem;
  }

  /* A drawer, not a box: it lives inside the panel and needs no frame of
     its own. */
  .skills-panel__catalog {
    gap: var(--space-sm);
    width: 100%;
  }

  .skills-panel__col-name {
    width: 16rem;
  }

  .skills-panel__col-action {
    width: 12rem;
  }

  .skill-row__actions {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: var(--space-xs);
  }

  .skills-panel__catalog-row {
    display: flex;
    gap: var(--space-xs);
  }

  .skills-panel__catalog-input {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .skills-panel__catalog-row .skills-panel__catalog-input:first-child {
    flex: 1;
  }

  .skills-panel__catalog-input:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .skills-panel__catalog-help {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  /* Combobox shape ported from CreateModal's old skill-search station
     (deleted in the create-modal skills-station removal): the dropdown
     floats over whatever sits below it rather than pushing it down, closing
     on focusout via `closeCatalogDropdownOnFocusout` above. */
  .skills-panel__combobox {
    position: relative;
  }

  .skills-panel__dropdown {
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    z-index: 30;
    margin-top: 0.25rem;
    max-height: 20rem;
    overflow-y: auto;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    box-shadow: 0 8px 24px rgba(15, 23, 42, 0.12);
    padding: var(--space-2xs);
  }

  .skills-panel__catalog-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .catalog-row {
    display: flex;
    align-items: flex-start;
    gap: var(--space-xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
  }

  .catalog-row__body {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 0;
  }

  .catalog-row__head {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
  }

  .catalog-row__name {
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.85rem;
    font-weight: 600;
  }

  .catalog-row__description {
    color: var(--muted);
    font-size: 0.8rem;
  }

  .skills-panel__badge {
    display: inline-flex;
    padding: 0.1rem var(--space-2xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.7rem;
    white-space: nowrap;
  }

  .catalog-row__pick {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2xs);
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-size: 0.85rem;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .catalog-row__pick:hover:not(.catalog-row__pick--added) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .catalog-row__pick--added {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .catalog-row__pick input {
    margin: 0;
    accent-color: var(--accent);
  }

  .skills-panel__pickbar {
    position: sticky;
    bottom: var(--space-sm);
    z-index: 20;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--accent);
    border-radius: var(--radius-sm);
    background: var(--panel);
    box-shadow: 0 8px 24px rgba(15, 23, 42, 0.12);
  }

  .skills-panel__pickbar-count {
    color: var(--ink);
    font-size: 0.85rem;
    font-weight: 600;
  }

  .skills-panel__pickbar-actions {
    display: flex;
    gap: var(--space-xs);
  }

  .skills-panel__table-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-xs);
    margin-top: var(--space-xs);
  }

  /* The state chip a row wears while a save is on its way: amber and
     pulsing until it lands, green for Active, red for Failed. */
  .skill-row__state {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2xs);
    margin-left: var(--space-xs);
    padding: 0 var(--space-xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.75rem;
    line-height: 1.5;
  }

  .skill-row__state--ok {
    border-color: var(--ok);
    color: var(--ok);
  }

  .skill-row__state--fail {
    border-color: var(--fail);
    color: var(--fail);
  }

  .skill-row__state-text {
    display: block;
    margin-top: var(--space-2xs);
    color: var(--muted);
    font-size: 0.8rem;
  }

  .skills-panel__delivery-text {
    color: var(--muted);
  }

  .skills-panel__delivery {
    display: inline-flex;
    align-self: flex-start;
    align-items: center;
    gap: var(--space-xs);
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .skills-panel__delivery--ok {
    border-color: var(--ok);
    color: var(--ok);
  }

  .skills-panel__delivery--fail {
    border-color: var(--fail);
    color: var(--fail);
  }

  .skills-panel__delivery-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--pulse);
  }

  .skills-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
  }

  .skills-form__field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .skills-form__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.9rem;
  }

  .skills-form__filename {
    margin: 0;
    color: var(--ink);
    font-family: var(--font-mono);
    font-weight: 600;
    font-size: 0.95rem;
  }

  .skills-form__input,
  .skills-form__textarea {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .skills-form__textarea {
    font-family: var(--font-mono);
    font-size: 0.85rem;
    resize: vertical;
  }

  .skills-form__input:focus-visible,
  .skills-form__textarea:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .skills-form__help {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .skills-form__status {
    margin: 0;
    font-size: 0.8rem;
  }

  .skills-form__status--error {
    color: var(--fail);
  }

  .skills-modal__confirm-text {
    margin: 0;
    color: var(--ink);
  }

  .skills-modal__diff {
    display: flex;
    flex-direction: column;
    max-height: 24rem;
    overflow-y: auto;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }

  .skills-diff-line {
    margin: 0;
    padding: 0.1rem var(--space-xs);
    overflow-wrap: anywhere;
    font-family: var(--font-mono);
    font-size: 0.85rem;
    white-space: pre-wrap;
  }

  .skills-diff-line--added {
    background: color-mix(in srgb, var(--ok) 12%, transparent);
    color: var(--ok);
  }

  .skills-diff-line--removed {
    background: color-mix(in srgb, var(--fail) 12%, transparent);
    color: var(--fail);
  }

  .skills-diff-line--same {
    color: var(--muted);
  }

  .skills-modal__diff-notice {
    margin: 0;
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: var(--muted);
    font-size: 0.85rem;
  }

  .skills-modal__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .skills-modal__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-sm);
  }

  .skills-modal__cancel {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    cursor: pointer;
  }

  .skills-modal__cancel:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .skills-modal__cancel:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .skills-modal__primary {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .skills-modal__primary:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .skills-modal__primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .skills-modal__danger {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--fail);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .skills-modal__danger:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .skills-modal__danger:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
