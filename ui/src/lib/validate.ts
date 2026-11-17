// Client-side mirror of the API's name rule (kobo_api's `NAME_RE`:
// `^[a-z][a-z0-9-]{1,23}$` plus `MAX_NAME_LENGTH = 24`, so 2-24 chars,
// lowercase letter/digit/hyphen, must start with a letter, must not end
// with a hyphen — kobo_api.catalog.validate_name enforces the exact same
// four checks, in the exact same order). The server is still authoritative
// (`checkAvailability`'s round trip is the source of truth) — this only
// exists so the Create modal can reject an obviously-bad name on every
// keystroke, before spending a debounced round trip on it.

const CHARSET_RE = /^[a-z0-9-]+$/;

/**
 * Validate a candidate agent name against the client-side rule. Returns
 * `null` when the name is valid, or a short human-readable reason when it
 * isn't (rendered inline under the name field).
 */
export function validateName(name: string): string | null {
  if (name.length < 2 || name.length > 24) {
    return "Use 2-24 characters.";
  }
  if (!CHARSET_RE.test(name)) {
    return "Use lowercase letters, numbers, and hyphens only.";
  }
  if (!/^[a-z]/.test(name)) {
    return "Must start with a letter.";
  }
  if (name.endsWith("-")) {
    return "Can't end with a hyphen.";
  }
  return null;
}

// Shared slug rule for a team or a persona tag (Task 10): kobo_api's own
// pattern for both — `main.py`'s `CreateAgentRequest.team` /
// `CreatePersonaRequest.team` are `Field(pattern=r"^[a-z][a-z0-9-]{0,23}$")`,
// and `personas.py`'s `_TAG_RE` is that exact same pattern — one slug rule,
// two call sites on the server, so one validator here too. 1-24 chars,
// lowercase letter/digit/hyphen, must start with a letter. Unlike
// `validateName` above, a trailing hyphen is legal here: the server's
// pattern has no rule against ending in one.
const SLUG_CHARSET_RE = /^[a-z0-9-]+$/;

/**
 * Validate a candidate team slug or persona tag. Returns `null` when valid,
 * or a short human-readable reason when it isn't.
 */
export function validateSlug(value: string): string | null {
  if (value.length < 1 || value.length > 24) {
    return "Use 1-24 characters.";
  }
  if (!SLUG_CHARSET_RE.test(value)) {
    return "Use lowercase letters, numbers, and hyphens only.";
  }
  if (!/^[a-z]/.test(value)) {
    return "Must start with a letter.";
  }
  return null;
}

/**
 * Client-side mirror of `kobo_api.purpose_tags.normalize_tags`'s per-tag
 * fold (team-guide spec 2026-09-14): lowercase, then collapse any run of
 * internal whitespace to a single hyphen — the exact same
 * `"-".join(tag.lower().split())` the server applies to every purpose tag,
 * including the picked group name a team guide's identity tag is derived
 * from. Used by TeamGuideModal to turn a picked group ("Data Eng") into the
 * slug ("data-eng") it must match against an existing guide's purpose_tags,
 * without a round trip — the server is still authoritative (its own fold
 * is what actually got stored), this only has to agree with it.
 */
export function normalizeGroupTag(value: string): string {
  return value
    .toLowerCase()
    .split(/\s+/)
    .filter((piece) => piece.length > 0)
    .join("-");
}

/**
 * Splits a comma/whitespace-separated tags field into individual candidate
 * slugs: trims each piece and drops empties, so `"a, b   c,,d"` becomes
 * `["a", "b", "c", "d"]`. Doesn't validate the pieces — pair with
 * `validateSlug` for that (the persona builder does, one tag at a time).
 */
export function splitTags(input: string): string[] {
  return input
    .split(/[\s,]+/)
    .map((tag) => tag.trim())
    .filter((tag) => tag.length > 0);
}

/**
 * Splits a one-boundary-per-line textarea into individual lines: trims each
 * and drops empties, so a blank line (or trailing whitespace-only line)
 * never becomes an empty boundary.
 */
export function splitLines(input: string): string[] {
  return input
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}

/** The four parts of a persona as the builder's form holds them. `rules`
 * is the textarea's own text, one rule per line. */
export type PersonaText = {
  name: string;
  whoTheyAre: string;
  whatTheyDo: string;
  rules: string;
};

export const EMPTY_PERSONA_TEXT: PersonaText = {
  name: "",
  whoTheyAre: "",
  whatTheyDo: "",
  rules: "",
};

/** One drafted field as text, or `null` when the response did not carry a
 * usable one. A 200 whose body does not match its own type (an empty
 * object, a number where a string belongs) is still valid JSON, so the
 * fields are judged here rather than trusted. */
function draftedText(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

function draftedRules(value: unknown): string | null {
  if (!Array.isArray(value)) {
    return null;
  }
  const lines = value.filter((rule): rule is string => typeof rule === "string" && rule.trim().length > 0);
  return lines.length > 0 ? lines.join("\n") : null;
}

/**
 * Lays a draft over the persona builder's form.
 *
 * A field takes the drafted text when it is empty, or when it still holds
 * exactly what the last draft put there. Anything a person typed, pasted
 * or changed stays, so pressing Draft persona a second time refreshes what
 * the room wrote and never what they wrote.
 *
 * `drafted` is what each field last received from a draft (empty for a
 * field no draft has filled); the returned `drafted` is what to pass next
 * time. A field the response lacks, or carries in the wrong type, is left
 * as it was.
 */
export function mergeDraftFields(
  current: PersonaText,
  drafted: PersonaText,
  draft: {
    name?: unknown;
    who_they_are?: unknown;
    what_they_do?: unknown;
    rules?: unknown;
    purpose_tags?: unknown;
  },
): { fields: PersonaText; drafted: PersonaText } {
  const incoming: Record<keyof PersonaText, string | null> = {
    name: draftedText(draft.name),
    whoTheyAre: draftedText(draft.who_they_are),
    whatTheyDo: draftedText(draft.what_they_do),
    rules: draftedRules(draft.rules),
  };
  const fields = { ...current };
  const nextDrafted = { ...drafted };
  for (const key of Object.keys(incoming) as (keyof PersonaText)[]) {
    const value = incoming[key];
    if (value === null) {
      continue;
    }
    if (current[key].trim().length === 0 || current[key] === drafted[key]) {
      fields[key] = value;
      nextDrafted[key] = value;
    }
  }
  return { fields, drafted: nextDrafted };
}

/** The room's floor for a persona (`kobo_api.personas.check_substance`),
 * mirrored so the form says what is missing before the round trip. The
 * server is still the check. */
export const PERSONA_WHAT_MIN_CHARS = 300;
export const PERSONA_RULES_MIN = 3;
export const PERSONA_RULES_MAX = 150;
export const PERSONA_NAME_MAX_WORDS = 3;

/**
 * The first thing a persona form still needs, as one sentence, or `null`
 * when it can be sent. Top to bottom, the order the fields are in, so the
 * line under the form always names the next thing to do. `team` is `null`
 * while no group has been chosen. `nameFixed` is the edit form, where the
 * name is not a field: a persona made before the three-word rule keeps its
 * name, and a line about it would be one nobody could act on.
 */
export function personaMissing(
  text: PersonaText,
  team: string | null,
  nameFixed = false,
): string | null {
  const words = (text.name.toLowerCase().match(/[a-z0-9]+/g) ?? []).length;
  if (!nameFixed && words === 0) {
    return "Give it a name.";
  }
  if (!nameFixed && words > PERSONA_NAME_MAX_WORDS) {
    return "Keep the name to one or two words, three at most.";
  }
  if (text.whoTheyAre.trim().length === 0) {
    return "Say who they are in one line.";
  }
  const length = text.whatTheyDo.trim().length;
  if (length === 0) {
    return "Say what they do.";
  }
  if (length < PERSONA_WHAT_MIN_CHARS) {
    return `Say more about what they do: ${length} of at least ${PERSONA_WHAT_MIN_CHARS} characters.`;
  }
  const rules = splitLines(text.rules).length;
  if (rules < PERSONA_RULES_MIN) {
    return `Add at least ${PERSONA_RULES_MIN} rules, one per line.`;
  }
  if (rules > PERSONA_RULES_MAX) {
    return `Use up to ${PERSONA_RULES_MAX} rules.`;
  }
  if (team === null) {
    return "Choose a group.";
  }
  return null;
}

/** What a persona's group is called on screen. The commons is stored as
 * "shared" and reads as Everyone, in the picker, the builder and the
 * sheet alike. */
export function personaGroupLabel(team: string): string {
  return team === "shared" ? "Everyone" : team;
}

export type PersonaGroup<T> = { key: string; label: string; personas: T[] };

/** The key of the built-ins' group in `personaGroups`. Not a name a real
 * group can have. */
export const BUILTIN_GROUP = "(built-in)";

/**
 * The persona list in the groups the picker draws: Built-in first, then
 * the caller's own groups, then every other group, and Everyone last
 * (teams reach for their own before the commons). A group with nothing in
 * it is left out, so a search narrows the groups along with the rows.
 */
export function personaGroups<T extends { team: string; builtin?: boolean }>(
  personas: T[],
  ownGroups: string[],
): PersonaGroup<T>[] {
  const builtins = personas.filter((p) => p.builtin);
  const made = personas.filter((p) => !p.builtin);
  const teams = [...new Set(made.map((p) => p.team))].filter((t) => t !== "shared").sort();
  const ordered = [
    ...teams.filter((t) => ownGroups.includes(t)),
    ...teams.filter((t) => !ownGroups.includes(t)),
    ...(made.some((p) => p.team === "shared") ? ["shared"] : []),
  ];
  return [
    ...(builtins.length > 0
      ? [{ key: BUILTIN_GROUP, label: "Built-in", personas: builtins }]
      : []),
    ...ordered.map((team) => ({
      key: team,
      label: personaGroupLabel(team),
      personas: made.filter((p) => p.team === team),
    })),
  ];
}

/**
 * The groups a persona may be filed under from this caller's seat (persona
 * team scope), for the builder's Group select and the persona sheet's
 * "Move to..." menu. Never includes the commons: both surfaces add
 * Everyone as their own last option.
 *
 * A caller's own groups come from whoami (config names, the same names the
 * create form's Group select lists). An operator may file anywhere, so it
 * is every group the room has named, then any of their own the room does
 * not list. The server's team gate is still the real check.
 */
export function personaTeamOptions(
  whoami: { operator: boolean; groups: string[] } | undefined,
  roomGroups: { name: string; kind: string }[] | undefined,
): string[] {
  const own = (whoami?.groups ?? []).filter((g) => g !== "shared");
  if (!whoami?.operator) {
    return own;
  }
  const named = (roomGroups ?? []).filter((g) => g.kind !== "shared").map((g) => g.name);
  return [...named, ...own.filter((g) => !named.includes(g))];
}
