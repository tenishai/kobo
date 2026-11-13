// The Models pane's rows (kobo#405): what every connected provider lists,
// turned into one row per model with a cell per provider, grouped by who
// publishes it. Two clouds' entries become one row only when they are safely
// the same model: the same publisher and the same name once each cloud's own
// dressing is taken off its id (a Bedrock routing prefix, a date, a `-v1`),
// and nothing else in either cloud's list reads the same. Anything less
// certain stays two rows; a row is never a guess.
import type { AdminDiscoveredModel, AdminModelProvider } from "../api";

const SELF_HOSTED = "self-hosted";
const BEDROCK = "bedrock";
const BEDROCK_ROUTING = /^(us|us-gov|eu|apac|ca|sa|me|af|il|au|jp|global)\./;
/** The groups for models no cloud names a publisher of, always last. */
export const GROUP_SELF_HOSTED = "Self-hosted";
export const GROUP_OTHER = "Other";

export type ModelRow = {
  key: string;
  /** The model's own name, as a cloud lists it. */
  name: string;
  publisher: string;
  /** By provider section (`source`): that provider's entry for this model. */
  cells: Record<string, AdminDiscoveredModel>;
};

export type ModelGroup = { publisher: string; rows: ModelRow[] };

/** The name two clouds share for one model: `us.anthropic.claude-sonnet-4-5-20250929-v1:0`
 * on Bedrock and `claude-sonnet-4-5` on Vertex AI are both `claude-sonnet-4-5`. */
export function matchKey(provider: string, model: string): string {
  let name = model.toLowerCase();
  if (provider === BEDROCK) {
    // `[routing.]vendor.name:version`: the routing prefix of an inference profile, then the vendor.
    name = name.split(":")[0].replace(BEDROCK_ROUTING, "");
    name = name.slice(name.indexOf(".") + 1);
  } else {
    name = name.split("@")[0];
  }
  return name.replace(/-\d{8}(?=-|$)/g, "").replace(/-v1$/, "");
}

function titled(publisher: string): string {
  return publisher === publisher.toLowerCase() ? publisher.replace(/^[a-z]/, (c) => c.toUpperCase()) : publisher;
}

export function isOffered(row: ModelRow): boolean {
  return Object.values(row.cells).some((m) => m.enabled);
}

/** One row per model across the providers given, in the providers' order. */
export function modelRows(providers: AdminModelProvider[]): ModelRow[] {
  // A row the room holds that its cloud does not list carries no publisher;
  // it takes the one its family has everywhere else, when that is one.
  const byFamily = new Map<string, Set<string>>();
  for (const p of providers) {
    for (const m of p.discovered) {
      if (!m.publisher || !m.family) continue;
      const family = m.family.toLowerCase();
      byFamily.set(family, (byFamily.get(family) ?? new Set()).add(m.publisher.toLowerCase()));
    }
  }
  function publisherOf(m: AdminDiscoveredModel): string {
    if (m.publisher) return m.publisher.toLowerCase();
    const known = byFamily.get((m.family ?? "").toLowerCase());
    return known?.size === 1 ? [...known][0] : "";
  }
  function shared(p: AdminModelProvider, m: AdminDiscoveredModel): string | null {
    const publisher = publisherOf(m);
    return p.provider === SELF_HOSTED || !publisher ? null : `${publisher}|${matchKey(p.provider, m.model)}`;
  }

  // A shared name two models of one provider both carry settles nothing.
  const unsure = new Set<string>();
  for (const p of providers) {
    const seen = new Set<string>();
    for (const m of p.discovered) {
      const k = shared(p, m);
      if (k === null) continue;
      if (seen.has(k)) unsure.add(k);
      seen.add(k);
    }
  }

  const rows = new Map<string, ModelRow>();
  const display = new Map<string, string>();
  for (const p of providers) {
    for (const m of p.discovered) {
      if (m.publisher) display.set(m.publisher.toLowerCase(), titled(m.publisher));
      const k = shared(p, m);
      const key = k !== null && !unsure.has(k) ? k : m.id;
      const row = rows.get(key) ?? { key, name: "", publisher: "", cells: {} };
      row.cells[p.source] = m;
      const own = m.name ?? m.label;
      // The name is a listed entry's when one is: a row the room alone holds knows only the room's label.
      if (!row.name || (m.discovered && !Object.values(row.cells).some((c) => c !== m && c.discovered))) row.name = own;
      row.publisher = row.publisher || publisherOf(m) || (p.provider === SELF_HOSTED ? GROUP_SELF_HOSTED : "");
      rows.set(key, row);
    }
  }
  return [...rows.values()].map((r) => ({ ...r, publisher: display.get(r.publisher) ?? (r.publisher || GROUP_OTHER) }));
}

/** The rows by publisher, A to Z, each group's models by name; the groups with no publisher last. */
export function groupRows(rows: ModelRow[]): ModelGroup[] {
  const groups = new Map<string, ModelRow[]>();
  for (const row of rows) groups.set(row.publisher, [...(groups.get(row.publisher) ?? []), row]);
  const last = (name: string): number => (name === GROUP_OTHER ? 2 : name === GROUP_SELF_HOSTED ? 1 : 0);
  return [...groups.entries()]
    .map(([publisher, list]) => ({ publisher, rows: list.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true })) }))
    .sort((a, b) => last(a.publisher) - last(b.publisher) || a.publisher.localeCompare(b.publisher));
}
