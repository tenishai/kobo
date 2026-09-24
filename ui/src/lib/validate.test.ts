import { describe, expect, it } from "vitest";
import {
  mergeDistillFields,
  mergeDraftFields,
  personaTeamOptions,
  splitLines,
  splitTags,
  validateName,
  validateSlug,
} from "./validate";

describe("validateName", () => {
  it("accepts a plain lowercase name", () => {
    expect(validateName("agent")).toBeNull();
  });

  it("accepts inner hyphens and digits", () => {
    expect(validateName("helper-agent-7")).toBeNull();
    expect(validateName("a1-b2-c3")).toBeNull();
  });

  it("accepts the minimum length (2 chars)", () => {
    expect(validateName("ab")).toBeNull();
  });

  it("accepts the maximum length (24 chars, matching the server's MAX_NAME_LENGTH)", () => {
    expect(validateName("a".repeat(24))).toBeNull();
  });

  it("rejects a name shorter than 2 characters", () => {
    expect(validateName("a")).toBe("Use 2-24 characters.");
    expect(validateName("")).toBe("Use 2-24 characters.");
  });

  it("rejects a name longer than 24 characters", () => {
    expect(validateName("a".repeat(25))).toBe("Use 2-24 characters.");
  });

  it("rejects uppercase letters", () => {
    expect(validateName("Agent")).toBe(
      "Use lowercase letters, numbers, and hyphens only.",
    );
  });

  it("rejects other symbols/spaces", () => {
    expect(validateName("agent_1")).toBe(
      "Use lowercase letters, numbers, and hyphens only.",
    );
    expect(validateName("my agent")).toBe(
      "Use lowercase letters, numbers, and hyphens only.",
    );
  });

  it("rejects a leading digit", () => {
    expect(validateName("1agent")).toBe("Must start with a letter.");
  });

  it("rejects a leading hyphen", () => {
    expect(validateName("-agent")).toBe("Must start with a letter.");
  });

  it("rejects a trailing hyphen (matches the server's own rule)", () => {
    expect(validateName("agent-")).toBe("Can't end with a hyphen.");
  });
});

describe("validateSlug", () => {
  it("accepts a plain lowercase slug", () => {
    expect(validateSlug("growth")).toBeNull();
  });

  it("accepts inner hyphens and digits", () => {
    expect(validateSlug("growth-team-7")).toBeNull();
  });

  it("accepts a single character (unlike validateName's 2-char floor)", () => {
    expect(validateSlug("a")).toBeNull();
  });

  it("accepts a trailing hyphen (unlike validateName, the server's slug pattern allows one)", () => {
    expect(validateSlug("growth-")).toBeNull();
  });

  it("accepts the maximum length (24 chars)", () => {
    expect(validateSlug("a".repeat(24))).toBeNull();
  });

  it("rejects an empty string", () => {
    expect(validateSlug("")).toBe("Use 1-24 characters.");
  });

  it("rejects a slug longer than 24 characters", () => {
    expect(validateSlug("a".repeat(25))).toBe("Use 1-24 characters.");
  });

  it("rejects uppercase letters", () => {
    expect(validateSlug("Growth")).toBe(
      "Use lowercase letters, numbers, and hyphens only.",
    );
  });

  it("rejects other symbols/spaces", () => {
    expect(validateSlug("growth_team")).toBe(
      "Use lowercase letters, numbers, and hyphens only.",
    );
    expect(validateSlug("growth team")).toBe(
      "Use lowercase letters, numbers, and hyphens only.",
    );
  });

  it("rejects a leading digit", () => {
    expect(validateSlug("7growth")).toBe("Must start with a letter.");
  });

  it("rejects a leading hyphen", () => {
    expect(validateSlug("-growth")).toBe("Must start with a letter.");
  });
});

describe("splitTags", () => {
  it("splits on commas", () => {
    expect(splitTags("a,b,c")).toEqual(["a", "b", "c"]);
  });

  it("splits on whitespace", () => {
    expect(splitTags("a b c")).toEqual(["a", "b", "c"]);
  });

  it("splits on a mix of commas and whitespace, trimming pieces", () => {
    expect(splitTags("a, b   c,, d ")).toEqual(["a", "b", "c", "d"]);
  });

  it("returns an empty array for blank input", () => {
    expect(splitTags("")).toEqual([]);
    expect(splitTags("   ")).toEqual([]);
    expect(splitTags(" , , ")).toEqual([]);
  });
});

describe("splitLines", () => {
  it("splits one item per line", () => {
    expect(splitLines("first\nsecond\nthird")).toEqual([
      "first",
      "second",
      "third",
    ]);
  });

  it("trims each line and drops blank lines", () => {
    expect(splitLines("  first  \n\n  \nsecond\n")).toEqual([
      "first",
      "second",
    ]);
  });

  it("returns an empty array for blank input", () => {
    expect(splitLines("")).toEqual([]);
    expect(splitLines("\n\n  \n")).toEqual([]);
  });
});

describe("mergeDraftFields", () => {
  const draft = {
    name: "billing-buddy",
    who_they_are: "a calm troubleshooter for billing questions",
    what_they_do: "Helps customers resolve billing issues quickly.",
  };

  it("fills every field when all three are empty", () => {
    const result = mergeDraftFields(
      { name: "", whoTheyAre: "", whatTheyDo: "" },
      draft,
    );

    expect(result).toEqual({
      name: draft.name,
      whoTheyAre: draft.who_they_are,
      whatTheyDo: draft.what_they_do,
    });
  });

  it("keeps a pre-filled what-they-do instead of overwriting it with the draft", () => {
    const result = mergeDraftFields(
      { name: "", whoTheyAre: "", whatTheyDo: "Operator-typed body, keep this." },
      draft,
    );

    expect(result.whatTheyDo).toBe("Operator-typed body, keep this.");
    expect(result.name).toBe(draft.name);
    expect(result.whoTheyAre).toBe(draft.who_they_are);
  });

  it("judges each field independently - one filled field doesn't block the others", () => {
    const result = mergeDraftFields(
      { name: "my-own-name", whoTheyAre: "", whatTheyDo: "" },
      draft,
    );

    expect(result.name).toBe("my-own-name");
    expect(result.whoTheyAre).toBe(draft.who_they_are);
    expect(result.whatTheyDo).toBe(draft.what_they_do);
  });

  it("treats a whitespace-only field as empty, still fillable by the draft", () => {
    const result = mergeDraftFields(
      { name: "", whoTheyAre: "", whatTheyDo: "   " },
      draft,
    );

    expect(result.whatTheyDo).toBe(draft.what_they_do);
  });

  it("keeps every field when all three already have operator content", () => {
    const current = {
      name: "kept-name",
      whoTheyAre: "kept who they are",
      whatTheyDo: "kept what they do",
    };

    expect(mergeDraftFields(current, draft)).toEqual(current);
  });

  // Fix round 1 (FINDING 2): a draft route that "always 200s" can still
  // hand back a body that doesn't match `PersonaDraft` at runtime - an
  // empty object, or fields of the wrong type. Before this fix, an absent
  // field fell through to `draft.name`/etc directly, so the merge wrote
  // `undefined` straight into form state; PersonaBuilder's `nameHint`
  // `$derived` then called `.trim()` on that `undefined` and threw the
  // uncaught TypeError the reviewer hit while waiting out the legacy
  // step-1 upload test's `{}` mock.
  it("keeps every current value and never throws when the response is an empty object", () => {
    const current = {
      name: "",
      whoTheyAre: "",
      whatTheyDo: "kept body",
    };

    expect(() => mergeDraftFields(current, {})).not.toThrow();
    expect(mergeDraftFields(current, {})).toEqual(current);
  });

  it("keeps every current value and never throws when fields are the wrong type", () => {
    const current = {
      name: "kept-name",
      whoTheyAre: "",
      whatTheyDo: "kept body",
    };
    const malformed = { name: 42, who_they_are: null, purpose_tags: "x" };

    expect(() => mergeDraftFields(current, malformed)).not.toThrow();
    expect(mergeDraftFields(current, malformed)).toEqual(current);
  });
});

describe("mergeDistillFields", () => {
  // Persona-first station addendum: the same no-clobber contract as
  // mergeDraftFields above, just a two-field shape (no whatTheyDo - the
  // operator's pasted/uploaded persona document already fills that field
  // directly, never through this merge) since a distill reply carries no
  // what_they_do at all.
  const distilled = {
    name: "billing-buddy",
    who_they_are: "a calm troubleshooter for billing questions",
  };

  it("fills both fields when they're empty", () => {
    const result = mergeDistillFields({ name: "", whoTheyAre: "" }, distilled);

    expect(result).toEqual({
      name: distilled.name,
      whoTheyAre: distilled.who_they_are,
    });
  });

  it("keeps a pre-typed name instead of overwriting it with the distilled one", () => {
    const result = mergeDistillFields(
      { name: "my-own-name", whoTheyAre: "" },
      distilled,
    );

    expect(result.name).toBe("my-own-name");
    expect(result.whoTheyAre).toBe(distilled.who_they_are);
  });

  it("judges each field independently - one filled field doesn't block the other", () => {
    const result = mergeDistillFields(
      { name: "", whoTheyAre: "kept who they are" },
      distilled,
    );

    expect(result.name).toBe(distilled.name);
    expect(result.whoTheyAre).toBe("kept who they are");
  });

  it("treats a whitespace-only field as empty, still fillable by the distill", () => {
    const result = mergeDistillFields({ name: "   ", whoTheyAre: "" }, distilled);

    expect(result.name).toBe(distilled.name);
  });

  it("keeps both fields when they already have operator content", () => {
    const current = { name: "kept-name", whoTheyAre: "kept who they are" };

    expect(mergeDistillFields(current, distilled)).toEqual(current);
  });

  // Fix round 1 (FINDING 2): same exposure as mergeDraftFields above - a
  // distill route that "always 200s" can still hand back a body that
  // doesn't match `PersonaDistillResult` at runtime. See that describe
  // block's matching tests for the exact crash this reproduces
  // (`nameHint`'s `$derived .trim()` on an `undefined` name).
  it("keeps both current values and never throws when the response is an empty object", () => {
    const current = { name: "", whoTheyAre: "" };

    expect(() => mergeDistillFields(current, {})).not.toThrow();
    expect(mergeDistillFields(current, {})).toEqual(current);
  });

  it("keeps both current values and never throws when fields are the wrong type", () => {
    const current = { name: "kept-name", whoTheyAre: "" };
    const malformed = { name: 42, who_they_are: null, purpose_tags: "x" };

    expect(() => mergeDistillFields(current, malformed)).not.toThrow();
    expect(mergeDistillFields(current, malformed)).toEqual(current);
  });
});

describe("personaTeamOptions", () => {
  const ROOM = [
    { name: "shared", kind: "shared" as const },
    { name: "sre", kind: "engineering" as const },
    { name: "qa", kind: "engineering" as const },
    { name: "design", kind: "non-engineering" as const },
  ];

  it("is the caller's own groups, without the commons", () => {
    expect(
      personaTeamOptions({ operator: false, groups: ["qa", "shared", "sre"] }, ROOM),
    ).toEqual(["qa", "sre"]);
  });

  it("is every named group for an operator, then any of their own the room does not list", () => {
    expect(personaTeamOptions({ operator: true, groups: ["ops"] }, ROOM)).toEqual([
      "sre",
      "qa",
      "design",
      "ops",
    ]);
  });

  it("is empty before whoami has answered", () => {
    expect(personaTeamOptions(undefined, ROOM)).toEqual([]);
  });
});
