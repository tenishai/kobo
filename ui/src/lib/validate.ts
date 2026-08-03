// Client-side mirror of the API's name rule (cortex_api's `NAME_RE`:
// `^[a-z][a-z0-9-]{1,23}$` plus `MAX_NAME_LENGTH = 24`, so 2-24 chars,
// lowercase letter/digit/hyphen, must start with a letter, must not end
// with a hyphen — cortex_api.catalog.validate_name enforces the exact same
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
