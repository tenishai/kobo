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
