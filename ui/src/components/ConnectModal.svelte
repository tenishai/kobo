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
    type ConnectRepoOption,
    type ScanFinding,
    type ScanReport,
  } from "../lib/api";
  import { debounced } from "../lib/debounce";
  import {
    connectAgentMutation,
    connectReposQuery,
    groupsQuery,
    scanRepoMutation,
  } from "../lib/queries";
  import { navigate } from "../lib/router.svelte";
  import { toast } from "../lib/toast.svelte";
  import { validateName } from "../lib/validate";

  let { open, onclose }: { open: boolean; onclose: () => void } = $props();

  // Mirrors `kobo_api/connect_scan.py`'s own code verbatim. The scan reports
  // it as a blocker only when "no HTTP port" was the answer AND the room has
  // no class for one, so seeing it once is enough to know the second option
  // is not available in this room at all.
  const HEADLESS_UNAVAILABLE = "headless_unavailable";

  // The Repository field is a combobox over the repositories the room's
  // GitHub App can already reach. Clicking into it opens a short list of
  // the ones worked on most recently, with the count of how many there
  // are underneath: without that, the field is a blank box that gives no
  // sign the room reaches anything at all. Typing filters it. The quiet
  // spell before a search is short enough to feel immediate and long
  // enough that a typed name is one request, not twelve.
  const REPO_SEARCH_DEBOUNCE_MS = 200;
  const REPO_LIST_ID = "connect-repo-list";

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

  // The search term the field is actually asking about, a debounce behind
  // what has been typed. Same "debounced into a plain $state the query
  // options read" idiom AccessPanel's own people search uses.
  let repoSearchTerm = $state("");
  // Set the moment a repository is picked or the list is dismissed, and
  // cleared by the next real keystroke. Without it, picking a row would
  // reopen the list a fifth of a second later over the value just filled
  // in. `oninput` fires on genuine typing only, never on the programmatic
  // write a pick makes.
  let repoListDismissed = $state(false);
  // Which row the keyboard is on, or -1 for none. None by default, so
  // Enter over a fully typed `owner/name` cannot swap it for whichever
  // row happened to be first.
  let activeRepo = $state(-1);
  // The name this field last filled in for itself. The search that would
  // otherwise follow it is not worth making: the room just said this
  // repository exists, and asking again would only reopen the list over
  // the answer somebody already chose.
  let pickedRepo = $state<string | null>(null);
  // Whether the cursor is in the field. Only the empty list is gated on
  // it: that list is an offer nobody asked for, so it belongs to the
  // moment somebody is actually in the box, while rows for something
  // typed stay where they were put.
  let repoFieldFocused = $state(false);

  const repoMatches = createQuery(() => (connectReposQuery(repoSearchTerm, open)));

  const commitRepoSearch = debounced((value: string) => {
    repoSearchTerm = value;
  }, REPO_SEARCH_DEBOUNCE_MS);

  $effect(() => {
    const value = repo.trim();
    if (!open || value === pickedRepo) {
      // A closing form and a just-picked name are both "nothing to ask".
      // Cancelled rather than left pending: the answer would arrive for a
      // form nobody is looking at, or for a choice already made.
      commitRepoSearch.cancel();
      return;
    }
    commitRepoSearch(value);
    return () => {
      commitRepoSearch.cancel();
    };
  });

  const repoSuggestions = $derived(repoMatches.data?.repos ?? []);
  /** Whether what came back answers what is in the box right now. Rows
   * for a term that has been typed past answer a question nobody is
   * asking any more, so while the debounce runs the list is shut. */
  const repoAnswerIsCurrent = $derived(repoSearchTerm === repo.trim());
  /** The field is showing the room's offer rather than a search: nothing
   * has been typed, so the rows are the most recently pushed. */
  const repoShowsRecent = $derived(repoAnswerIsCurrent && repo.trim() === "");
  /** Whoever opens the form first can be waiting on a cold listing, and a
   * dropdown that is silent about it looks broken. */
  const repoListLoading = $derived(repoAnswerIsCurrent && repoMatches.isPending);
  /** The field has to be in play for a failure to be worth saying: a
   * modal that has only just opened should not lead with an error. */
  const repoSearchFailed = $derived(
    repoMatches.isError && (repo.trim().length >= 1 || repoFieldFocused),
  );
  const repoListOpen = $derived(
    !repoListDismissed &&
      repoAnswerIsCurrent &&
      (repo.trim().length >= 1 || repoFieldFocused) &&
      (repoListLoading || repoSuggestions.length > 0),
  );
  const repoTotal = $derived(repoMatches.data?.total ?? 0);
  const repoMatchesTruncated = $derived(
    repoListOpen && !repoShowsRecent && repoMatches.data?.truncated === true,
  );

  /** "1 repository" / "7 repositories", which both the live region and
   * the list's own footer count in. */
  function repositoryCount(n: number): string {
    return n === 1 ? "1 repository" : `${n} repositories`;
  }

  /** What a screen reader hears when the rows change. Silent while a
   * search is in flight and when the search itself failed, since the line
   * under the input says that one. With nothing typed it says how many
   * the room reaches in all, not how many rows are showing: eight rows
   * out of two hundred is the number worth hearing. */
  const repoAnnouncement = $derived.by(() => {
    if (!repoAnswerIsCurrent || repoMatches.isPending || repoMatches.isError) {
      return "";
    }
    if (repoShowsRecent) {
      return repoFieldFocused ? repositoryCount(repoTotal) : "";
    }
    if (repoSuggestions.length === 0) {
      return "No repositories match";
    }
    return repositoryCount(repoSuggestions.length);
  });

  // The highlight belongs to the rows it was made over. New rows (a
  // narrower query, a refetch that came back shorter) mean it points at
  // something that may not be there, so it goes with them -- which also
  // keeps `aria-activedescendant` from naming a row that no longer exists.
  $effect(() => {
    repoSuggestions;
    activeRepo = -1;
  });

  // Keeps the highlighted row in view in a list taller than its box.
  // `?.()` because jsdom has no scrollIntoView and a test is not a reason
  // to skip the behaviour.
  $effect(() => {
    if (activeRepo < 0) {
      return;
    }
    const row = document.getElementById(`${REPO_LIST_ID}-${activeRepo}`);
    row?.scrollIntoView?.({ block: "nearest" });
  });

  function handleRepoTyped(): void {
    repoListDismissed = false;
    pickedRepo = null;
    activeRepo = -1;
  }

  function pickRepo(option: ConnectRepoOption): void {
    repo = option.full_name;
    pickedRepo = option.full_name;
    repoListDismissed = true;
    activeRepo = -1;
  }

  function handleRepoKeydown(event: KeyboardEvent): void {
    if (!repoListOpen) {
      // Down is how a list that was dismissed comes back, which is the
      // only way back to it without retyping. Over an empty field it is
      // also how the keyboard asks for the list the mouse gets by
      // clicking in.
      if (event.key === "ArrowDown") {
        event.preventDefault();
        repoListDismissed = false;
        repoFieldFocused = true;
      }
      return;
    }
    if (event.key === "Escape") {
      // Stopped here rather than allowed to bubble: the modal closes on
      // Escape, and the first press is about the list, not the form.
      event.preventDefault();
      event.stopPropagation();
      repoListDismissed = true;
      activeRepo = -1;
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      activeRepo = (activeRepo + 1) % repoSuggestions.length;
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      activeRepo =
        activeRepo <= 0 ? repoSuggestions.length - 1 : activeRepo - 1;
      return;
    }
    if (event.key === "Enter") {
      // Always swallowed while the list is open, so Enter never submits
      // the form out from under an open dropdown. With no row highlighted
      // it just closes the list and leaves what was typed alone.
      event.preventDefault();
      // Bounds-checked as well as reset above: the rows can change
      // between the highlight and the Enter, and a pick of a row that is
      // not there any more must leave what was typed alone.
      const highlighted = activeRepo >= 0 ? repoSuggestions[activeRepo] : undefined;
      if (highlighted !== undefined) {
        pickRepo(highlighted);
      } else {
        repoListDismissed = true;
      }
    }
  }

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
      repoSearchTerm = "";
      repoListDismissed = false;
      activeRepo = -1;
      pickedRepo = null;
      repoFieldFocused = false;
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
      <div class="connect-form__combobox">
        <input
          id="connect-repo"
          class="connect-form__input"
          type="text"
          role="combobox"
          aria-expanded={repoListOpen}
          aria-controls={REPO_LIST_ID}
          aria-autocomplete="list"
          aria-activedescendant={activeRepo >= 0
            ? `${REPO_LIST_ID}-${activeRepo}`
            : undefined}
          bind:value={repo}
          oninput={handleRepoTyped}
          onkeydown={handleRepoKeydown}
          onfocus={() => (repoFieldFocused = true)}
          onblur={() => (repoFieldFocused = false)}
          autocomplete="off"
          spellcheck="false"
        />
        {#if repoListOpen}
          <ul
            id={REPO_LIST_ID}
            class="connect-form__suggestions"
            role="listbox"
            aria-label="Repositories"
          >
            {#if repoListLoading}
              <!-- Not an option: there is nothing here to pick. The
                   keyboard skips it and the live region stays quiet
                   until there is a count to say. -->
              <li class="connect-form__suggestion-note" role="presentation">
                Loading repositories
              </li>
            {/if}
            <!-- The row is the option. Its mousedown is swallowed so the
                 field keeps focus through the click, and the whole row
                 picks rather than a button inside it, which would put an
                 interactive control inside an option.

                 The keyboard never lands here: this is the
                 aria-activedescendant pattern, where focus stays on the
                 input and the arrow keys and Enter are handled there. A
                 key handler on the row would be a control nobody can
                 reach. -->
            <!-- svelte-ignore a11y_click_events_have_key_events -->
            {#each repoSuggestions as option, index (option.full_name)}
              <li
                id={`${REPO_LIST_ID}-${index}`}
                class="connect-form__suggestion"
                class:connect-form__suggestion--active={index === activeRepo}
                role="option"
                aria-selected={index === activeRepo}
                onmousedown={(event) => event.preventDefault()}
                onclick={() => pickRepo(option)}
              >
                <span>{option.full_name}</span>
                <span class="connect-form__suggestion-kind">
                  {option.private ? "Private" : "Public"}
                </span>
              </li>
            {/each}
            {#if repoShowsRecent && !repoListLoading}
              <!-- The rows above are a way in, not the list. This is
                   what says how much more there is, and that typing is
                   how to get at it. -->
              <li class="connect-form__suggestion-note" role="presentation">
                {repositoryCount(repoTotal)} in the organization. Start typing
                to filter.
              </li>
            {/if}
          </ul>
        {/if}
      </div>
      <!-- Said out loud, once per change, for anyone who cannot see the
           rows appear under the field. -->
      <p class="connect-form__sr-only" role="status" aria-live="polite">
        {repoAnnouncement}
      </p>
      {#if repoMatchesTruncated}
        <p class="connect-form__help">More matched. Keep typing to narrow it down.</p>
      {/if}
      {#if repoSearchFailed}
        <!-- Not an error the form stops for: the field still takes a
             typed owner/name, and the scan is still what answers for it. -->
        <p class="connect-form__status">
          Repository search is unavailable right now. You can still type
          owner/name.
        </p>
      {/if}
      <p class="connect-form__help">
        Start typing to search the repositories the room's GitHub App can
        reach. The room proposes its setup by pull request and never pushes to
        your branches.
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

  /* The list sits directly under the input and over whatever follows it,
     so the fields below do not jump as rows arrive. */
  .connect-form__combobox {
    position: relative;
    display: flex;
    flex-direction: column;
  }

  .connect-form__suggestions {
    position: absolute;
    top: 100%;
    right: 0;
    left: 0;
    z-index: 1;
    max-height: 15rem;
    margin: var(--space-2xs) 0 0;
    padding: 0;
    overflow-y: auto;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    list-style: none;
  }

  .connect-form__suggestion {
    display: flex;
    justify-content: space-between;
    gap: var(--space-sm);
    padding: var(--space-xs) var(--space-sm);
    color: var(--ink);
    font-size: 0.85rem;
    cursor: pointer;
  }

  .connect-form__suggestion:hover,
  .connect-form__suggestion--active {
    background: var(--bg);
  }

  .connect-form__suggestion-kind {
    color: var(--muted);
    font-size: 0.75rem;
  }

  /* The loading hint and the count under the recent rows. Quieter than a
     row and not clickable, so nobody reads either as something to pick.
     The count is separated by a rule because it is about the list, not
     the last row of it. */
  .connect-form__suggestion-note {
    padding: var(--space-xs) var(--space-sm);
    color: var(--muted);
    font-size: 0.75rem;
  }

  .connect-form__suggestion + .connect-form__suggestion-note {
    border-top: 1px solid var(--border);
  }

  /* Same visually-hidden line StepChecklist uses for its own live
     region: announced, never drawn. */
  .connect-form__sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: 0;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
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
