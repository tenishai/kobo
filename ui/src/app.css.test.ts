import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

/** Every stylesheet and component under `src`, as paths relative to it. */
function sources(root: string, dir = root): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...sources(root, path));
    else if (/\.(svelte|css)$/.test(entry.name)) out.push(relative(root, path));
  }
  return out.sort();
}

const CSS = readFileSync(join(__dirname, "app.css"), "utf8");
const INDEX_HTML = readFileSync(join(__dirname, "..", "index.html"), "utf8");

// The two scopes the palette is declared in: the first `:root` block is the
// light values, the `prefers-color-scheme: dark` block overrides a subset.
// Read straight out of the file so a token that moves has to move here too.
function scope(css: string, start: number): Record<string, string> {
  const open = css.indexOf("{", start);
  const end = css.indexOf("}", open);
  const out: Record<string, string> = {};
  for (const line of css.slice(open + 1, end).split("\n")) {
    const m = line.match(/^\s*(--[a-z-]+):\s*(.+?);\s*$/);
    if (m) out[m[1]] = m[2];
  }
  return out;
}

const LIGHT = scope(CSS, 0);
const DARK = scope(CSS, CSS.indexOf("prefers-color-scheme: dark"));

/** A token's value in one scheme, falling back to light the way CSS does. */
function token(scheme: Record<string, string>, name: string): string {
  const raw = scheme[name] ?? LIGHT[name];
  const ref = raw.match(/^var\((--[a-z-]+)\)$/);
  return ref ? token(scheme, ref[1]) : raw;
}

/** WCAG relative luminance of a `#rrggbb` string. */
function luminance(hex: string): number {
  const channels = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const [r, g, b] = channels.map((c) =>
    c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG contrast ratio between two `#rrggbb` strings, 1 to 21. */
function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

describe("app.css typography", () => {
  // The Fraunces display serif was the create flow's station headings. The
  // dialog redesign took those away and left a self-hosted 35KB woff2 that
  // nothing asked for. It is gone, and this is what keeps it gone: a
  // heading is the body sans at weight 600, not a second face.
  it("has no Fraunces face, no --font-display, and no font to load", () => {
    expect(CSS).not.toMatch(/fraunces/i);
    expect(CSS).not.toContain("--font-display");
    expect(CSS).not.toContain("@font-face");
    expect(existsSync(join(__dirname, "assets", "fonts"))).toBe(false);
  });

  it("loads no font from anywhere else either", () => {
    expect(INDEX_HTML).not.toMatch(/fraunces|fonts\.googleapis|fonts\.gstatic/i);
  });

  it("names Inter first in the sans stack and keeps a mono for identifiers", () => {
    expect(CSS).toMatch(/--font-sans:\s*\n?\s*Inter,/);
    expect(CSS).toContain("--font-mono:");
  });

  // Palette v2, typography half: the room reads a notch smaller than a
  // browser default, set once on `html` so every rem in the file follows it.
  it("sets the base size on html, in px, so the rem scale has one origin", () => {
    expect(CSS).toMatch(/html\s*{[^}]*font-size:\s*15px;/);
  });

  // The rule the mono sweep was run against, written down so the next hand
  // has to answer the same question: mono is for things a person copies,
  // types or matches character by character -- an agent name, a secret key,
  // a channel id, a path, a version, a file's contents, a diff. Never for a
  // sentence, a timestamp, a byline, a label or a button.
  //
  // Every selector that asks for the mono face, by file. Adding one means
  // adding it here and saying which identifier it carries.
  it("asks for the mono face only where an identifier is printed", () => {
    const allowed: Record<string, string[]> = {
      "app.css": [
        "code,pre,kbd", // literal code, wherever it appears
        ".panel__name", // the named thing a panel row is about
        ".list__name", // ditto, in a picker
      ],
      "components/AccessPanel.svelte": [".access-panel__row-name"], // repo, person
      "components/AgentCard.svelte": [".agent-card__name"], // the agent
      "components/ColleagueConversations.svelte": [
        ".colleague-terminal__chip", // agent names
        ".colleague-terminal__screen", // the terminal's own chrome
      ],
      "components/GroupRequestsCard.svelte": [".group-requests-card__name"], // group
      "components/HarnessPanel.svelte": [".harness__ref"], // image reference
      "components/PersonaHoverCard.svelte": [".hover-card__name"], // the persona
      "components/MemoryPanel.svelte": [
        ".memory-row__name",
        ".memory-form__filename",
        ".memory-diff-line",
        ".intake-row__name",
      ],
      "components/RepoAccessCard.svelte": [".repo-access__draft"], // owner/name
      "components/SecretsPanel.svelte": [
        ".secrets__textarea", // pasted KEY=VALUE
        ".secrets__name",
        ".secrets__input", // the value itself
      ],
      "components/SkillsPanel.svelte": [
        ".catalog-row__name",
        ".skills-form__filename",
        ".skills-form__textarea", // file contents
        ".skills-diff-line",
      ],
      "components/SlackPanel.svelte": [
        ".slack-panel__manifest-block",
        ".slack-panel__mono", // ids
        ".slack-panel__cell-input", // channel
      ],
      "components/ToolingPanel.svelte": [".tooling-form__textarea"], // contents
    };

    const found: Record<string, string[]> = {};
    for (const file of sources(__dirname)) {
      const text = readFileSync(join(__dirname, file), "utf8").replace(
        /\/\*[\s\S]*?\*\//g,
        "",
      );
      // Rule by rule: the selector is whatever precedes the brace.
      const selectors = (text.match(/[^{}]+{[^{}]*}/g) ?? [])
        .filter((rule) => rule.includes("font-family: var(--font-mono)"))
        .map((rule) => rule.slice(0, rule.indexOf("{")).trim().replace(/\s+/g, ""));
      if (selectors.length > 0) found[file] = selectors;
    }

    expect(found).toEqual(allowed);
  });
});

// Palette v2 (graphite): the accent is a near-black a primary button can be
// filled with, and a link is its own token -- a deep slate that reads as
// "this goes somewhere" without the old all-blue room.
describe("app.css palette", () => {
  it("makes the light accent graphite, on white ink, with its own soft wash", () => {
    expect(LIGHT["--accent"]).toBe("#1f2937");
    expect(LIGHT["--accent-ink"]).toBe("#ffffff");
    expect(LIGHT["--accent-soft"]).toBe("#eef1f5");
  });

  it("keeps the dark accent light, on dark ink", () => {
    expect(DARK["--accent"]).toBe("#e5e7eb");
    expect(DARK["--accent-ink"]).toBe("#0b1220");
    expect(DARK["--accent-soft"]).toBe("#1f2937");
  });

  it("gives links their own token in both schemes", () => {
    expect(LIGHT["--link"]).toBe("#1e3a8a");
    expect(DARK["--link"]).toBe("#93c5fd");
  });

  it("keeps the status colours and pulses in the ok colour", () => {
    expect(LIGHT["--ok"]).toBe("#15803d");
    expect(LIGHT["--fail"]).toBe("#b91c1c");
    expect(LIGHT["--warn"]).toBe("#b45309");
    expect(token(LIGHT, "--pulse")).toBe(token(LIGHT, "--ok"));
    expect(token(DARK, "--pulse")).toBe(token(DARK, "--ok"));
  });

  // The whole point of the re-value: text has to pass AA on the surface it
  // is printed on, in both schemes, or the palette is not done.
  it.each([
    ["light", LIGHT],
    ["dark", DARK],
  ])("clears AA for body, muted and link text in %s", (_name, scheme) => {
    const panel = token(scheme, "--panel");
    expect(contrast(token(scheme, "--ink"), panel)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(token(scheme, "--muted"), panel)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(token(scheme, "--link"), panel)).toBeGreaterThanOrEqual(4.5);
  });

  it.each([
    ["light", LIGHT],
    ["dark", DARK],
  ])("clears AA for a filled primary button in %s", (_name, scheme) => {
    expect(
      contrast(token(scheme, "--accent-ink"), token(scheme, "--accent")),
    ).toBeGreaterThanOrEqual(4.5);
  });

  // "Never accent-on-soft": the soft wash is a background for ink, not for
  // the accent itself, and in light mode the two are too close to read.
  it("never asks for accent text on the accent wash", () => {
    expect(CSS).not.toMatch(
      /background:\s*var\(--accent-soft\);[^}]*color:\s*var\(--accent\);/,
    );
  });
});
