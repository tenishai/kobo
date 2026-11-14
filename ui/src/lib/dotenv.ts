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
 * the three Slack values, the two model-access variables, the model the
 * room picked and how Slack wakes the agent.
 *
 * A mirror of `RESERVED_KEYS` in `api/kobo_api/agent_env.py`, and the
 * server's test checks the two are equal: when this list lagged the
 * server's (issue #250) a room-set row rendered as editable, rode along
 * on every save and had the whole write refused by a name nobody typed. */
export const RESERVED_KEYS = [
  "CABAL_COLLEAGUE_TOKEN",
  "SLACK_BOT_TOKEN",
  "SLACK_APP_TOKEN",
  "SLACK_SIGNING_SECRET",
  "SLACK_ICON_URL",
  "VERTEXAI_PROJECT",
  "VERTEXAI_LOCATION",
  "AGENT_MODEL",
  "SLACK_WAKE_MODE",
  "SLACK_WAKE_EMOJI",
  "MODELS_GATEWAY_KEY",
  "KOBO_DATA_DIR",
  "KOBO_SWITCHBOARD_URL",
  "KOBO_GATEWAY_URL",
  "KOBO_MODELS_URL",
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
 *
 * `released` is the reserved names this agent's team keeps for itself, as
 * the secrets read lists them: Slack's, for an agent that runs its team's
 * own code on a Slack app of its own.
 */
export function keyRefusal(key: string, released: readonly string[] = []): string | null {
  if (!KEY_RE.test(key)) {
    return `${key || "that name"}: use capital letters, digits and underscores, starting with a letter`;
  }
  if (key.length > MAX_KEY_LENGTH) {
    return `${key}: keep a name to ${MAX_KEY_LENGTH} characters or fewer`;
  }
  if (RESERVED_KEYS.includes(key) && !released.includes(key)) {
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
 * Blank lines and `#` comments are skipped, and so is an inline comment:
 * a ` #` with whitespace in front of it, outside quotes, ends the value.
 * That is what a .env file means by it, and a value that silently carried
 * `# the staging one` into a credential would fail somewhere far from
 * here.
 *
 * A value may be wrapped in single or double quotes, which are stripped
 * and which protect a `#` inside them. Everything else is taken as typed,
 * except for the surrounding whitespace every line is trimmed of before
 * anything else happens: a trailing space is far more often a paste
 * artifact than a deliberate part of a credential, and a person who needs
 * one can quote it.
 *
 * `export KEY=VALUE` is accepted, with any whitespace after the keyword,
 * since that is what a shell file people already have looks like.
 *
 * A value that opens a quote and never closes it is ONE problem naming
 * that line, not a cascade of confusing ones about the lines beneath it:
 * multi-line values are not something this box can carry, and the
 * sentence says to use the form instead.
 *
 * Every key is put through `keyRefusal` (with the same `released` names),
 * so a reserved or malformed name is a problem rather than something the
 * save discovers.
 */
export function parseEnvPaste(text: string, released: readonly string[] = []): ParsedEnv {
  const values: Record<string, string> = {};
  const problems: string[] = [];

  const lines = text.split(/\r?\n/);
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index].trim();
    if (line.length === 0 || line.startsWith("#")) {
      continue;
    }
    const withoutExport = line.replace(/^export\s+/, "");
    const eq = withoutExport.indexOf("=");
    if (eq <= 0) {
      problems.push(`Line ${index + 1}: write this as KEY=value.`);
      continue;
    }
    const key = withoutExport.slice(0, eq).trim();
    const refusal = keyRefusal(key, released);
    if (refusal !== null) {
      problems.push(`Line ${index + 1}: ${refusal}`);
      continue;
    }
    const value = readValue(withoutExport.slice(eq + 1).trim());
    if (value === null) {
      // One problem, and then stop: every line after this one is the rest
      // of the value somebody pasted, and reporting each of them as "write
      // this as KEY=value" would bury the sentence that actually helps.
      problems.push(
        `Line ${index + 1}: this value opens a quote and never closes it. ` +
          "A value that spans lines has to be added one at a time in the form above.",
      );
      break;
    }
    values[key] = value;
  }

  return { values, problems };
}

/**
 * One value as typed: quotes stripped, an unquoted inline comment cut.
 * `null` when a quote opens and never closes, which is the only shape
 * this cannot answer for.
 */
function readValue(raw: string): string | null {
  const quote = raw[0];
  if (quote === '"' || quote === "'") {
    const close = raw.indexOf(quote, 1);
    if (close === -1) {
      return null;
    }
    // Anything after the closing quote is a comment or stray text; a
    // quoted value is exactly what is between the quotes either way.
    return raw.slice(1, close);
  }
  // Unquoted: a ` #` with whitespace in front of it starts a comment, the
  // way a .env file means it. A `#` with no space before it is part of
  // the value (a URL fragment, a colour), which is also what a shell
  // would do with it.
  const comment = raw.search(/\s#/);
  return (comment === -1 ? raw : raw.slice(0, comment)).trimEnd();
}
