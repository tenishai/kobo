// What a guardrail rule can name besides its words (2026-11-20): people,
// emoji, Slack channels and repositories. The server is the check
// (kobo_api.guardrails.clean_lists); these mirror it so a picker can say at
// once whether what was typed can be added.

/** Slack's names for the two thumbs in its reaction events. */
const EMOJI_ALIASES: Record<string, string> = { thumbsup: "+1", thumbsdown: "-1" };
const EMOJI_NAME = /^[a-z0-9_+-]{1,64}$/;
const CHANNEL_NAME = /^[a-z0-9_-]{1,80}$/;
const REPOSITORY = /^[A-Za-z0-9._-]{1,100}\/[A-Za-z0-9._-]{1,100}$/;

/** An emoji as Slack's reaction events name it (no colons, lower case, no
 * skin tone, thumbsup as +1), or null when it can't be one. */
export function emojiName(raw: string): string | null {
  const name = raw.trim().replace(/^:+|:+$/g, "").split("::", 1)[0].toLowerCase();
  const canonical = EMOJI_ALIASES[name] ?? name;
  return EMOJI_NAME.test(canonical) ? canonical : null;
}

/** A Slack channel's name without the #, lower case, a space as a hyphen
 * (Slack's own rule), or null. */
export function channelName(raw: string): string | null {
  const name = raw.trim().replace(/^#+/, "").trim().replace(/\s+/g, "-").toLowerCase();
  return CHANNEL_NAME.test(name) ? name : null;
}

/** A repository as owner/name, or null. */
export function repositoryName(raw: string): string | null {
  const name = raw.trim();
  return REPOSITORY.test(name) ? name : null;
}

/** Emoji people often build rules around, offered as they type. Any other
 * name Slack shows can be typed in full. */
export const COMMON_EMOJI = [
  "+1", "-1", "white_check_mark", "heavy_check_mark", "x", "no_entry", "eyes",
  "ticket", "rotating_light", "warning", "fire", "rocket", "bug", "question",
  "exclamation", "memo", "pushpin", "lock", "key", "sos", "hourglass", "tada",
  "pray", "raised_hands", "thinking_face", "mag", "bell", "star", "link",
  "page_facing_up", "hammer_and_wrench", "construction", "stop_sign",
  "octagonal_sign", "ok", "new", "speech_balloon", "inbox_tray", "outbox_tray",
];
