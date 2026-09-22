// The key rule and the .env paste, on this side of the wire.
//
// Client-side mirror of `kobo_api.agent_env` (`KEY_RE`, `MAX_KEY_LENGTH`,
// `MAX_KEYS`, `RESERVED_KEYS`), the same relationship `validate.ts` has to
// the API's name rule: the server is still the one that decides, and every
// refusal it sends is rendered verbatim. This exists so a bad key name is
// answered while somebody is typing it, and so a pasted .env file can be
// shown back line by line rather than refused as a whole.
//
// The paste is parsed HERE and sent as a `set` map, never as a blob of
// text: the server validates every key it receives exactly as it would one
// typed by hand, so parsing on this side is a convenience and never the
// gate.

const KEY_RE = /^[A-Z][A-Z0-9_]*$/;
const MAX_KEY_LENGTH = 64;

/** Per list, matching the server's own cap. */
export const MAX_KEYS = 100;

/** What the room says about a key it sets itself, word for word as the
 * server says it. */
export const ROOM_SETS_THIS_ONE = "the room sets this one";

/** And about a key the container sets. A different sentence because it is
 * a different fact with a different fix. */
export const CONTAINER_SETS_THIS_ONE = "the container sets this one";

/** Every key the room fills in on an agent's behalf: the colleague pass,
 * the three Slack values, and the two model-access variables. */
export const RESERVED_KEYS = [
  "CABAL_COLLEAGUE_TOKEN",
  "SLACK_BOT_TOKEN",
  "SLACK_APP_TOKEN",
  "SLACK_SIGNING_SECRET",
  "VERTEXAI_PROJECT",
  "VERTEXAI_LOCATION",
];

/** Names the runtime owns: how the container finds its binaries, its
 * interpreter, and the shared objects it loads before any of the agent's
 * own code runs. Not the room's to set and not an owner's either. */
export const CONTAINER_KEYS = [
  "PATH",
  "HOME",
  "PYTHONPATH",
  "PYTHONHOME",
  "LD_PRELOAD",
  "LD_LIBRARY_PATH",
];

/**
 * The plain sentence refusing `key`, or null when it is allowed. Same
 * three checks in the same order as the server's `key_refusal`, so the
 * inline answer and the one that comes back from a save agree.
 */
export function keyRefusal(key: string): string | null {
  if (!KEY_RE.test(key)) {
    return `${key || "that name"}: use capital letters, digits and underscores, starting with a letter`;
  }
  if (key.length > MAX_KEY_LENGTH) {
    return `${key}: keep a name to ${MAX_KEY_LENGTH} characters or fewer`;
  }
  if (RESERVED_KEYS.includes(key)) {
    return `${key}: ${ROOM_SETS_THIS_ONE}`;
  }
  if (CONTAINER_KEYS.includes(key)) {
    return `${key}: ${CONTAINER_SETS_THIS_ONE}`;
  }
  return null;
}

export type ParsedEnv = {
  /** The keys and values every good line produced, in the order they were
   * pasted; a repeated key keeps the last one, the way a shell would. */
  values: Record<string, string>;
  /** One sentence per line that is not usable, each naming the line
   * number, so nothing is written until they are fixed. */
  problems: string[];
};

/**
 * Parse pasted `KEY=VALUE` lines.
 *
 * Blank lines and `#` comments are skipped. A value may be wrapped in
 * single or double quotes, which are stripped; anything else is taken
 * literally, trailing spaces and all, because a trailing space in a
 * credential is a real thing somebody meant. `export KEY=VALUE` is
 * accepted, since that is what a shell file people already have looks
 * like.
 *
 * Every key is put through `keyRefusal`, so a reserved or malformed name
 * is a problem rather than something the save discovers.
 */
export function parseEnvPaste(text: string): ParsedEnv {
  const values: Record<string, string> = {};
  const problems: string[] = [];

  text.split(/\r?\n/).forEach((raw, index) => {
    const line = raw.trim();
    if (line.length === 0 || line.startsWith("#")) {
      return;
    }
    const withoutExport = line.startsWith("export ") ? line.slice(7).trim() : line;
    const eq = withoutExport.indexOf("=");
    if (eq <= 0) {
      problems.push(`Line ${index + 1}: write this as KEY=value.`);
      return;
    }
    const key = withoutExport.slice(0, eq).trim();
    const refusal = keyRefusal(key);
    if (refusal !== null) {
      problems.push(`Line ${index + 1}: ${refusal}`);
      return;
    }
    values[key] = unquote(withoutExport.slice(eq + 1).trim());
  });

  return { values, problems };
}

function unquote(value: string): string {
  const quoted =
    value.length >= 2 &&
    ((value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'")));
  return quoted ? value.slice(1, -1) : value;
}
