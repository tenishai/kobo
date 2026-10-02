// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/svelte";
import { createRawSnippet } from "svelte";
import { describe, expect, it, vi } from "vitest";
import Modal from "./Modal.svelte";

// The dialog shell (modal idiom wave): one width scale, a header row, a body
// that scrolls on its own, and a footer pinned under it. The structure is the
// contract -- every dialog in this room hands it a body and a footer and gets
// the same shape back.

const body = createRawSnippet(() => ({
  render: () => `<p>Some body copy</p>`,
}));

const footer = createRawSnippet(() => ({
  render: () =>
    `<div class="modal__actions"><button type="button" class="btn btn--quiet">Cancel</button><button type="button" class="btn btn--primary">Do it</button></div>`,
}));

function renderModal(props: Record<string, unknown> = {}) {
  return render(Modal, {
    props: { open: true, title: "A dialog", onclose: () => {}, children: body, ...props },
  });
}

describe("Modal shell", () => {
  it("names itself with one h2 and closes from the header", async () => {
    const onclose = vi.fn();
    renderModal({ onclose });

    const heading = screen.getByRole("heading", { level: 2 });
    expect(heading.textContent).toBe("A dialog");
    expect(screen.getByRole("dialog").getAttribute("aria-labelledby")).toBe("modal-title");

    await fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onclose).toHaveBeenCalled();
  });

  it("still closes on Escape", async () => {
    const onclose = vi.fn();
    renderModal({ onclose });

    await fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(onclose).toHaveBeenCalled();
  });
});
