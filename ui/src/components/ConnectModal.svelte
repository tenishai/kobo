<script lang="ts">
  // "Connect your own agent" (connected-agents design 2026-09-21): one
  // station, a sibling of Create agent and Quick-start team agent, for a
  // repository a team already owns and the room only deploys. Same
  // controlled-visibility contract as every other modal in this room
  // (`open` + `onclose`, owned by the caller) and the same field styling as
  // TeamGuideModal, the other one-station form.
  //
  // Two buttons, in the order the work happens. SCAN reads the repository
  // and reports what the room can honestly say about it: blockers first
  // (nothing goes ahead until they are gone), then warnings (true things
  // the owner may still choose), then the list of what the room will ever
  // write into the repository. CONNECT is disabled until a scan of the
  // CURRENT field values came back with no blockers, because a report is a
  // claim about a repository under one set of answers: change any of them
  // and the report describes something else. The server does not take this
  // form's word for any of it either -- the connect route runs its own scan
  // again and refuses with the same blockers, which is what `connectBlockers`
  // below renders.
  //
  // Every sentence shown here about a repository is the SERVER's, verbatim
  // (`kobo_api/connect_scan.py`): the findings carry a code for this file to
  // key on and a message for a person to read, and rewording the message
  // here is how the room would start saying two different things about one
  // fact. The only code this file keys on is `headless_unavailable`.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import Modal from "./Modal.svelte";
  import Skeleton from "./Skeleton.svelte";
  import ErrorState from "./ErrorState.svelte";
  import {
    ApiError,
    errorMessage,
    RUN_MODE_HEADLESS,
    RUN_MODE_HTTP,
    type ScanFinding,
    type ScanReport,
  } from "../lib/api";
  import { connectAgentMutation, groupsQuery, scanRepoMutation } from "../lib/queries";
  import { navigate } from "../lib/router.svelte";
  import { toast } from "../lib/toast.svelte";
  import { validateName } from "../lib/validate";

  let { open, onclose }: { open: boolean; onclose: () => void } = $props();

  // Mirrors `kobo_api/connect_scan.py`'s own code verbatim. The scan reports
  // it as a blocker only when "no HTTP port" was the answer AND the room has
  // no class for one, so seeing it once is enough to know the second option
  // is not available in this room at all.
  const HEADLESS_UNAVAILABLE = "headless_unavailable";

  const queryClient = useQueryClient();
  const groups = createQuery(() => (groupsQuery()));
  const scan = createMutation(() => (scanRepoMutation()));
  const connect = createMutation(() => (connectAgentMutation(queryClient)));

  let repo = $state("");
  let name = $state("");
  let groupValue = $state("");
  let runMode = $state<string>(RUN_MODE_HTTP);
  let confirmOwner = $state(false);

  // The scan's answer, plus the exact field values it answered FOR. Connect
  // reads both: a report alone says nothing about the form as it stands now.
  let report = $state<ScanReport | null>(null);
  let scannedFields = $state<string | null>(null);
  let scanError = $state<string | null>(null);

  // Set once a scan says this room has no class for an agent with no HTTP
  // port. Holds the server's own sentence, and disables that option from
  // then on rather than letting someone scan their way into it twice.
  let headlessMessage = $state<string | null>(null);

  // What the server's own re-scan found when Connect was pressed: the form's
  // report was clean, the route's was not (access withdrawn, a Dockerfile
  // deleted, somebody else's connect landing first). Rendered in the same
  // place as the scan's own blockers.
  let connectBlockers = $state<ScanFinding[]>([]);
  let submitError = $state<string | null>(null);
  // The name a 409 said was taken, so the error sits on the name field and
  // clears itself the moment the name is edited.
  let nameTaken = $state<string | null>(null);

  const groupOptionsAvailable = $derived((groups.data?.length ?? 0) > 0);
  const sharedGroupOptions = $derived(
    (groups.data ?? []).filter((g) => g.kind === "shared"),
  );
  const engineeringGroupOptions = $derived(
    (groups.data ?? []).filter((g) => g.kind === "engineering"),
  );
  const nonEngineeringGroupOptions = $derived(
    (groups.data ?? []).filter((g) => g.kind === "non-engineering"),
  );
  // Whether the group question has been answered, which a room with no
  // groups configured answers by having none. Gated on the list having
  // actually arrived: while it is still loading there are no options to
  // show, and without this the form would read that as "no groups in this
  // room" and let a scan go without the group a gated room requires.
  const groupChosen = $derived(
    groups.isSuccess && (!groupOptionsAvailable || groupValue !== ""),
  );

  const nameError = $derived(name.length > 0 ? validateName(name) : null);
  const nameValid = $derived(name.length > 0 && nameError === null);

  const repoValue = $derived(repo.trim());
  /** owner/name, the shape the server takes. Checked here only so Scan is
   * not spent on something that cannot be a repository at all; which
   * repositories this room may connect is the scan's own answer. */
  const repoLooksRight = $derived(/^[^/\s]+\/[^/\s]+$/.test(repoValue));

  /** Every answer the SCAN depends on, as one comparable value: exactly the
   * fields the scan request carries, and nothing else. The ownership
   * checkbox is deliberately not in here: it is not part of the question
   * the scan answers, so editing it cannot make a report untrue, and
   * staling a clean report over it would spend a scan (which this room
   * counts) on an answer that would come back identical. */
  const fields = $derived(
    JSON.stringify([repoValue, name, groupValue, runMode]),
  );
  const scanIsStale = $derived(report !== null && scannedFields !== fields);
  const scanIsClean = $derived(
    report !== null && !scanIsStale && report.blockers.length === 0,
  );

  /** The blockers to show: the server's own, when a Connect was refused,
   * otherwise the scan's. A refusal is about the answers that were sent, so
   * it stops being shown the moment those answers change, same as the
   * report it arrived over. */
  const blockers = $derived(
    connectBlockers.length > 0 && !scanIsStale
      ? connectBlockers
      : (report?.blockers ?? []),
  );

  const canScan = $derived(
    repoLooksRight && nameValid && groupChosen && !scan.isPending,
  );
  const canConnect = $derived(
    scanIsClean &&
      confirmOwner &&
      blockers.length === 0 &&
      nameTaken !== name &&
      !connect.isPending,
  );

  // Fresh form every time the modal opens -- same idiom CreateModal and
  // TeamGuideModal use. `headlessMessage` resets with everything else: it is
  // an answer about this room, but it was learned from one scan, and a room
  // that grew the class between two opens should not keep being told it has
  // not.
  $effect(() => {
    if (open) {
      repo = "";
      name = "";
      groupValue = "";
      runMode = RUN_MODE_HTTP;
      confirmOwner = false;
      report = null;
      scannedFields = null;
      scanError = null;
      headlessMessage = null;
      connectBlockers = [];
      submitError = null;
      nameTaken = null;
    }
  });

  function scanBody() {
    return {
      repo: repoValue,
      name,
      run_mode: runMode,
      group_name: groupOptionsAvailable ? groupValue : undefined,
    };
  }

  function handleScan(): void {
    if (!canScan) {
      return;
    }
    scanError = null;
    submitError = null;
    connectBlockers = [];
    // Captured before the request, so a report can never be credited to
    // field values typed while it was in flight.
    const scannedFor = fields;
    scan.mutate(scanBody(), {
      onSuccess: (result) => {
        report = result;
        scannedFields = scannedFor;
        const headless = result.blockers.find(
          (finding) => finding.code === HEADLESS_UNAVAILABLE,
        );
        if (headless) {
          headlessMessage = headless.message;
          // The option that was just disabled cannot be left checked: a
          // disabled control holding the answer is a form nobody can
          // finish. Moving back to the one mode this room has marks the
          // report stale, which is right, since it answered for the other.
          runMode = RUN_MODE_HTTP;
        }
      },
      onError: (error: unknown) => {
        report = null;
        scannedFields = null;
        scanError = errorMessage(error, "Couldn't read that repository.");
      },
    });
  }

  function handleSubmit(event: SubmitEvent): void {
    event.preventDefault();
    if (!canConnect) {
      return;
    }
    submitError = null;
    connectBlockers = [];
    nameTaken = null;
    const connectedName = name;
    connect.mutate(
      {
        ...scanBody(),
        secret_names: [],
        confirm_repo_owner: confirmOwner,
      },
      {
        onSuccess: () => {
          toast.success("Agent connected", `Setting up ${connectedName}…`);
          onclose();
          navigate({ page: "agent", name: connectedName });
        },
        onError: (error: unknown) => {
          if (error instanceof ApiError) {
            if (error.status === 409) {
              // The name was free when the form scanned and is not now.
              nameTaken = connectedName;
              return;
            }
            const body = error.body as { blockers?: unknown } | undefined;
            if (Array.isArray(body?.blockers)) {
              connectBlockers = body.blockers as ScanFinding[];
              return;
            }
          }
          submitError = errorMessage(error, "Something went wrong. Try again.");
        },
      },
    );
  }
</script>

<Modal {open} title="Connect your own agent" {onclose}>
  <form class="connect-form" onsubmit={handleSubmit}>
    <div class="connect-form__field">
      <label class="connect-form__label" for="connect-repo">Repository</label>
      <input
        id="connect-repo"
        class="connect-form__input"
        type="text"
        bind:value={repo}
        placeholder="owner/name"
        autocomplete="off"
        spellcheck="false"
      />
      <p class="connect-form__help">
        The repository to deploy, as owner/name. The room reads it, and
        proposes its setup by pull request. It never pushes to your branches.
      </p>
    </div>

    <div class="connect-form__field">
      <label class="connect-form__label" for="connect-name">Name</label>
      <input
        id="connect-name"
        class="connect-form__input"
        type="text"
        bind:value={name}
        placeholder="e.g. release-bot"
        autocomplete="off"
        spellcheck="false"
        aria-invalid={nameError !== null}
      />
      <p class="connect-form__help">
        Lowercase letters, numbers, and hyphens. 2-24 characters.
      </p>
      {#if nameError}
        <p class="connect-form__status connect-form__status--error" role="alert">
          {nameError}
        </p>
      {:else if nameTaken !== null && nameTaken === name}
        <p class="connect-form__status connect-form__status--error" role="alert">
          That name is already taken in this room.
        </p>
      {/if}
    </div>

    <div class="connect-form__field">
      <label class="connect-form__label" for="connect-group">Group</label>
      {#if groups.isPending}
        <Skeleton rows={1} />
      {:else if groups.isError}
        <ErrorState
          message={errorMessage(groups.error, "Couldn't load groups.")}
          onretry={() => groups.refetch()}
        />
      {:else if !groupOptionsAvailable}
        <p class="connect-form__help">No groups are configured yet.</p>
      {:else}
        <select
          id="connect-group"
          class="connect-form__input"
          required
          value={groupValue}
          onchange={(event) =>
            (groupValue = (event.currentTarget as HTMLSelectElement).value)}
        >
          <option value="" disabled>Choose a group</option>
          {#if sharedGroupOptions.length > 0}
            <optgroup label="Shared">
              {#each sharedGroupOptions as group (group.name)}
                <option value={group.name}>{group.name} (everyone in the company)</option>
              {/each}
            </optgroup>
          {/if}
          {#if nonEngineeringGroupOptions.length > 0}
            <optgroup label="Non-engineering groups">
              {#each nonEngineeringGroupOptions as group (group.name)}
                <option value={group.name}>{group.name}</option>
              {/each}
            </optgroup>
          {/if}
          {#if engineeringGroupOptions.length > 0}
            <optgroup label="Engineering groups">
              {#each engineeringGroupOptions as group (group.name)}
                <option value={group.name}>{group.name}</option>
              {/each}
            </optgroup>
          {/if}
        </select>
        <p class="connect-form__help">Who owns this agent.</p>
      {/if}
    </div>

    <fieldset class="connect-form__fieldset">
      <legend class="connect-form__label">How does it run?</legend>
      <label class="connect-form__choice">
        <input
          type="radio"
          name="connect-run-mode"
          value={RUN_MODE_HTTP}
          checked={runMode === RUN_MODE_HTTP}
          onchange={() => (runMode = RUN_MODE_HTTP)}
        />
        It listens on a port (the platform checks its health there)
      </label>
      <label
        class="connect-form__choice"
        class:connect-form__choice--disabled={headlessMessage !== null}
      >
        <!-- The reason this option is off is tied to the control itself,
             not just printed under it: a screen reader meeting a disabled
             radio should hear why without hunting for the line below. -->
        <input
          type="radio"
          name="connect-run-mode"
          value={RUN_MODE_HEADLESS}
          checked={runMode === RUN_MODE_HEADLESS}
          disabled={headlessMessage !== null}
          aria-describedby={headlessMessage ? "connect-headless-reason" : undefined}
          onchange={() => (runMode = RUN_MODE_HEADLESS)}
        />
        It does not listen on a port (a chat bot or a worker)
      </label>
      {#if headlessMessage}
        <p id="connect-headless-reason" class="connect-form__help">
          {headlessMessage}
        </p>
      {/if}
    </fieldset>
    <p class="connect-form__help">
      Either way the agent gets an address inside the cluster only. Nothing
      here makes it reachable from outside.
    </p>

    <label class="connect-form__choice">
      <input type="checkbox" bind:checked={confirmOwner} />
      I own this repository or have its owner's agreement
    </label>

    <div class="connect-form__actions">
      <button type="button" class="connect-form__cancel" onclick={onclose}>
        Cancel
      </button>
      <button
        type="button"
        class="connect-form__scan"
        disabled={!canScan}
        onclick={handleScan}
      >
        {scan.isPending ? "Reading…" : "Scan"}
      </button>
      <button type="submit" class="connect-form__submit" disabled={!canConnect}>
        {connect.isPending ? "Connecting…" : "Connect"}
      </button>
    </div>

    {#if scanError}
      <p class="connect-form__status connect-form__status--error" role="alert">
        {scanError}
      </p>
    {/if}

    {#if submitError}
      <p class="connect-form__status connect-form__status--error" role="alert">
        {submitError}
      </p>
    {/if}

    {#if report}
      <!-- Blockers above warnings, always: one of them stops the connect
           and the other does not, and reading order is how a person tells
           them apart at a glance. -->
      <div class="connect-report">
        {#if scanIsStale}
          <p class="connect-form__status">
            Something changed since this scan. Scan again before connecting.
          </p>
        {/if}

        {#if blockers.length > 0}
          <div class="connect-report__group">
            <p class="connect-report__heading">Fix these first</p>
            <ul class="connect-report__list">
              {#each blockers as finding (finding.code + finding.message)}
                <li class="connect-report__blocker">{finding.message}</li>
              {/each}
            </ul>
          </div>
        {:else if !scanIsStale}
          <p class="connect-report__heading">Nothing is in the way.</p>
        {/if}

        {#if report.warnings.length > 0}
          <div class="connect-report__group">
            <p class="connect-report__heading">Worth knowing</p>
            <ul class="connect-report__list">
              {#each report.warnings as finding (finding.code + finding.message)}
                <li class="connect-report__warning">{finding.message}</li>
              {/each}
            </ul>
          </div>
        {/if}

        <div class="connect-report__group">
          <p class="connect-form__help">{report.adds_preamble}</p>
          <p class="connect-report__heading">What the room will add, by pull request</p>
          <ul class="connect-report__list">
            {#each report.adds as entry (entry)}
              <li class="connect-report__add">{entry}</li>
            {/each}
          </ul>
        </div>
      </div>
    {/if}
  </form>
</Modal>

<style>
  /* Same visual language as TeamGuideModal's own station -- a sibling, not
     a new design system. */
  .connect-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
    background-color: var(--bg);
  }

  .connect-form__field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .connect-form__fieldset {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    margin: 0;
    padding: 0;
    border: none;
  }

  .connect-form__label {
    padding: 0;
    color: var(--ink);
    font-weight: 600;
    font-size: 0.9rem;
  }

  .connect-form__input {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .connect-form__input:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .connect-form__choice {
    display: flex;
    align-items: flex-start;
    gap: var(--space-xs);
    color: var(--ink);
    font-size: 0.9rem;
  }

  .connect-form__choice--disabled {
    color: var(--muted);
  }

  .connect-form__help {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .connect-form__status {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .connect-form__status--error {
    color: var(--fail);
  }

  .connect-form__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-sm);
  }

  .connect-form__cancel,
  .connect-form__scan {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    cursor: pointer;
  }

  .connect-form__scan {
    font-weight: 600;
  }

  .connect-form__cancel:hover,
  .connect-form__scan:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .connect-form__scan:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .connect-form__submit {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .connect-form__submit:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .connect-form__submit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .connect-report {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
  }

  .connect-report__group {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .connect-report__heading {
    margin: 0;
    color: var(--ink);
    font-weight: 600;
    font-size: 0.85rem;
  }

  .connect-report__list {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    margin: 0;
    padding-left: var(--space-md);
    font-size: 0.85rem;
  }

  .connect-report__blocker {
    color: var(--fail);
  }

  .connect-report__warning,
  .connect-report__add {
    color: var(--muted);
  }
</style>
