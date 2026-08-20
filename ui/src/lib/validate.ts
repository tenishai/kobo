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

/**
 * Coerces one field off an API response into a usable string, or `null`
 * when it's missing or came back some other type entirely (`undefined`,
 * `null`, a number, ...). The declared response types (`PersonaDraft`,
 * `PersonaDistillResult`) say these fields are always strings, but that's
 * only a compile-time promise about the client's own code - a live 200
 * whose body doesn't honor its own shape (an empty `{}`, a field of the
 * wrong type) is still valid JSON as far as `fetch`/`JSON.parse` are
 * concerned, and shows up here at runtime exactly like a well-formed one
 * would.
 */
function fillableString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/**
 * The shared no-clobber decision for one field, used by both merge
 * functions below: keep the current (operator) value unless it's empty AND
 * the incoming value is a usable string. `incoming === null` means the
 * field was missing or malformed (see `fillableString`) - "nothing to
 * fill" either way, same treatment an incoming empty string would get, so
 * a malformed response degrades to "leave the field exactly as it was"
 * rather than writing `undefined`/`null`/a number into form state that
 * every caller (bindings, `$derived`s like `nameHint`) assumes is always a
 * string.
 */
function pickField(current: string, incoming: string | null): string {
  if (incoming === null) {
    return current;
  }
  return current.trim().length > 0 ? current : incoming;
}

/**
 * The persona builder's no-clobber rule (Persona v3 upload/limits
 * addendum): a drafted name/who-they-are/what-they-do only ever fills in a
 * field that's still empty. Operator text - typed, pasted, or uploaded -
 * is never overwritten by a draft, no matter which field it landed in.
 *
 * Pulled out of PersonaBuilder.svelte as its own pure function (same
 * "logic here, wiring there" split the API's `kobo_api.persona_draft`
 * module uses) so the fill-vs-keep decision for each field is directly
 * testable without going through a mutation, a mocked fetch, or the
 * component's two-step form at all.
 *
 * `draft`'s fields are typed `unknown` on purpose (fix round 1): a route
 * that "always 200s" (see `draftPersona`'s doc in ../lib/api) can still
 * hand back a body that doesn't match `PersonaDraft` - an empty `{}`, or a
 * field of the wrong type - and this used to write that value straight
 * into form state unchecked, so a name/who_they_are of `undefined` would
 * survive all the way to `nameHint`'s `$derived .trim()` in
 * PersonaBuilder.svelte and throw. `purpose_tags` is accepted but never
 * read - callers pass the full response object through, and this function
 * only ever judges the three fields it owns.
 */
export function mergeDraftFields(
  current: { name: string; whoTheyAre: string; whatTheyDo: string },
  draft: {
    name?: unknown;
    who_they_are?: unknown;
    what_they_do?: unknown;
    purpose_tags?: unknown;
  },
): { name: string; whoTheyAre: string; whatTheyDo: string } {
  return {
    name: pickField(current.name, fillableString(draft.name)),
    whoTheyAre: pickField(current.whoTheyAre, fillableString(draft.who_they_are)),
    whatTheyDo: pickField(current.whatTheyDo, fillableString(draft.what_they_do)),
  };
}

/**
 * `mergeDraftFields`'s counterpart for a distilled persona document
 * (persona-first station addendum): the same no-clobber rule, just a
 * two-field shape - no `whatTheyDo` here at all, since a distill reply
 * never carries one (the operator's pasted/uploaded persona document
 * already fills that field directly, the moment it arrives - long before
 * this merge ever runs). Only Name/Who they are can still be empty when a
 * distill lands, so only those two are judged.
 *
 * Same defensive typing as `mergeDraftFields` above, and for the same
 * reason: `distillPersona`'s route always 200s too, and a malformed body
 * (missing keys, wrong types) is "nothing to fill" here rather than a
 * value that ends up written into `name`/`whoTheyAre` unchecked.
 */
export function mergeDistillFields(
  current: { name: string; whoTheyAre: string },
  distilled: { name?: unknown; who_they_are?: unknown; purpose_tags?: unknown },
): { name: string; whoTheyAre: string } {
  return {
    name: pickField(current.name, fillableString(distilled.name)),
    whoTheyAre: pickField(current.whoTheyAre, fillableString(distilled.who_they_are)),
  };
}
