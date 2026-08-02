// Client-side mirror of the API's name rule (cortex_api's `NAME_RE`:
// `^[a-z][a-z0-9-]{1,29}$`, 2-30 chars, lowercase letter/digit/hyphen,
// must start with a letter). The server is still authoritative — this only
// exists so the Create modal can reject an obviously-bad name on every
// keystroke, before spending a debounced round trip on `checkAvailability`.
//
// Deliberately stricter than the server in one place: trailing hyphens are
// technically allowed server-side today, but the UI forbids them (see the
// P1b-backlog finding) since a name like "agent-" reads as truncated.

const CHARSET_RE = /^[a-z0-9-]+$/;

/**
 * Validate a candidate agent name against the client-side rule. Returns
 * `null` when the name is valid, or a short human-readable reason when it
 * isn't (rendered inline under the name field).
 */
export function validateName(name: string): string | null {
  if (name.length < 2 || name.length > 30) {
    return "Use 2-30 characters.";
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
