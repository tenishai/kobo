// @vitest-environment jsdom
//
// Persona builder upload/limits addendum: file-upload affordances for the
// rules textareas and the step-2 persona body, honest new limits (150
// rules, no more per-rule/what-they-do length caps in the UI's own client
// checks), and the no-clobber rule for the draft step (mergeDraftFields,
// ../lib/validate, has the exhaustive fill-vs-keep matrix - these tests
// cover the component's own wiring: uploads land in the right field, and a
// fresh draft still fills an empty form end to end).
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PersonaBuilder from "./PersonaBuilder.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const DRAFT_RESPONSE = {
  name: "billing-buddy",
  who_they_are: "a calm troubleshooter for billing questions",
  what_they_do: "Helps customers resolve billing issues quickly.",
  purpose_tags: ["billing", "support"],
};

function textFile(content: string, name = "notes.txt"): File {
  return new File([content], name, { type: "text/plain" });
}

async function reachStep2(): Promise<void> {
  const jobField = screen.getByLabelText("What should this agent do for you?");
  await fireEvent.input(jobField, {
    target: { value: "Help customers with billing questions." },
  });
  await fireEvent.click(screen.getByRole("button", { name: "Draft persona" }));
  await screen.findByRole("button", { name: "Create persona" });
}

describe("PersonaBuilder - file upload", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("uploading a file into the step-1 rules textarea replaces its content", async () => {
    stubFetch({ "/api/": {} });
    renderWithClient(PersonaBuilder, { oncancel: () => {}, oncreated: () => {} });

    const rulesInput = screen.getByLabelText("Upload rules file") as HTMLInputElement;
    await fireEvent.change(rulesInput, {
      target: { files: [textFile("Never share account numbers.\nBe concise.")] },
    });

    await waitFor(() => {
      const rulesTextarea = screen.getByLabelText("Rules (optional)") as HTMLTextAreaElement;
      expect(rulesTextarea.value).toBe("Never share account numbers.\nBe concise.");
    });
  });

  it("a rules upload read failure shows the same plain copy MemoryPanel uses", async () => {
    stubFetch({ "/api/": {} });
    renderWithClient(PersonaBuilder, { oncancel: () => {}, oncreated: () => {} });

    const rulesInput = screen.getByLabelText("Upload rules file") as HTMLInputElement;
    const reader = {
      readAsText: vi.fn(function (this: FileReader) {
        this.onerror?.(new Event("error") as ProgressEvent<FileReader>);
      }),
    };
    vi.stubGlobal(
      "FileReader",
      vi.fn(() => reader),
    );

    await fireEvent.change(rulesInput, { target: { files: [textFile("x")] } });

    expect(await screen.findByText("Couldn't read this file. Try another.")).not.toBeNull();
  });

  it("uploading a file into the step-2 what-they-do textarea replaces its content", async () => {
    stubFetch({
      "/api/personas/draft": DRAFT_RESPONSE,
      "/api/": {},
    });
    renderWithClient(PersonaBuilder, { oncancel: () => {}, oncreated: () => {} });

    await reachStep2();

    const bodyUpload = screen.getByLabelText("Upload body file") as HTMLInputElement;
    await fireEvent.change(bodyUpload, {
      target: { files: [textFile("Answer billing questions from an uploaded playbook.")] },
    });

    await waitFor(() => {
      const whatTheyDo = screen.getByLabelText("What they do") as HTMLTextAreaElement;
      expect(whatTheyDo.value).toBe("Answer billing questions from an uploaded playbook.");
    });
  });
});

describe("PersonaBuilder - honest new limits (upload/limits addendum)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("step 1's rules help text says up to 150, not 8", async () => {
    stubFetch({ "/api/": {} });
    renderWithClient(PersonaBuilder, { oncancel: () => {}, oncreated: () => {} });

    expect(screen.getByText("Up to 150, one per line.")).not.toBeNull();
    expect(screen.queryByText("Up to 8, one per line.")).toBeNull();
  });

  it("shows the 150 rules cap message once more than 150 lines are entered", async () => {
    stubFetch({ "/api/": {} });
    renderWithClient(PersonaBuilder, { oncancel: () => {}, oncreated: () => {} });

    const rulesTextarea = screen.getByLabelText("Rules (optional)") as HTMLTextAreaElement;
    const tooMany = Array.from({ length: 151 }, (_, i) => `rule ${i}`).join("\n");
    await fireEvent.input(rulesTextarea, { target: { value: tooMany } });

    expect(await screen.findByText("Use up to 150, one per line.")).not.toBeNull();
  });

  it("the merged-render 422 message surfaces verbatim through the existing error plumbing", async () => {
    stubFetch({ "/api/personas/draft": DRAFT_RESPONSE, "/api/": {} });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/personas/draft")) {
          return new Response(JSON.stringify(DRAFT_RESPONSE), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url.includes("/api/personas")) {
          return new Response(
            JSON.stringify({ error: "persona renders to 151 lines; the limit is 150" }),
            { status: 422, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({}), { status: 200 });
      }),
    );
    renderWithClient(PersonaBuilder, { oncancel: () => {}, oncreated: () => {} });

    await reachStep2();
    await fireEvent.click(screen.getByRole("button", { name: "Create persona" }));

    expect(
      await screen.findByText("persona renders to 151 lines; the limit is 150"),
    ).not.toBeNull();
  });
});

describe("PersonaBuilder - draft no-clobber (component wiring)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("a successful draft fills every field when the form started empty", async () => {
    stubFetch({
      "/api/personas/draft": DRAFT_RESPONSE,
      "/api/": {},
    });
    renderWithClient(PersonaBuilder, { oncancel: () => {}, oncreated: () => {} });

    await reachStep2();

    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe(
      DRAFT_RESPONSE.name,
    );
    expect((screen.getByLabelText("Who they are") as HTMLInputElement).value).toBe(
      DRAFT_RESPONSE.who_they_are,
    );
    expect((screen.getByLabelText("What they do") as HTMLTextAreaElement).value).toBe(
      DRAFT_RESPONSE.what_they_do,
    );
  });

  it("a failed draft still reaches the editable preview with an error message, not a dead end", async () => {
    // A bare rejection (not an Error instance) is how the draft mutation's
    // `errorMessage(error, fallback)` call ends up on its fallback copy -
    // the same shape a genuine network failure (not the generator being
    // down, which the route always 200s through) would take.
    stubFetch({ "/api/": {} });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/personas/draft")) {
          return Promise.reject("network down");
        }
        return new Response(JSON.stringify({}), { status: 200 });
      }),
    );
    renderWithClient(PersonaBuilder, { oncancel: () => {}, oncreated: () => {} });

    await reachStep2();

    expect(
      await screen.findByText("Couldn't draft a persona right now. Fill it in yourself below."),
    ).not.toBeNull();
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("");
  });
});
