import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const CSS = readFileSync(join(__dirname, "app.css"), "utf8");
const INDEX_HTML = readFileSync(join(__dirname, "..", "index.html"), "utf8");

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
});
