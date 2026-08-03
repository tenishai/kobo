import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { dismiss, toast, toasts } from "./toast.svelte";

// The queue is a module-level singleton, so each test clears it first rather
// than relying on import isolation.
beforeEach(() => {
  vi.useFakeTimers();
  toasts.splice(0, toasts.length);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("toast store", () => {
  it("starts empty", () => {
    expect(toasts).toEqual([]);
  });

  it("pushes a success toast", () => {
    toast.success("Agent created");
    expect(toasts).toHaveLength(1);
    expect(toasts[0]).toMatchObject({
      kind: "success",
      title: "Agent created",
      body: undefined,
    });
  });

  it("pushes an error toast with an optional body", () => {
    toast.error("Create failed", "name already taken");
    expect(toasts).toHaveLength(1);
    expect(toasts[0]).toMatchObject({
      kind: "error",
      title: "Create failed",
      body: "name already taken",
    });
  });

  it("gives each toast a distinct id", () => {
    toast.success("first");
    toast.success("second");
    expect(toasts[0].id).not.toBe(toasts[1].id);
  });

  it("keeps multiple toasts queued independently, oldest first", () => {
    toast.success("first");
    toast.error("second");
    expect(toasts.map((item) => item.title)).toEqual(["first", "second"]);
  });

  it("auto-dismisses a toast after 5s", () => {
    toast.success("bye soon");
    expect(toasts).toHaveLength(1);
    vi.advanceTimersByTime(5000);
    expect(toasts).toHaveLength(0);
  });

  it("does not auto-dismiss before 5s", () => {
    toast.success("not yet");
    vi.advanceTimersByTime(4999);
    expect(toasts).toHaveLength(1);
  });

  it("dismiss removes a toast by id manually, ahead of its timer", () => {
    toast.success("manual");
    const id = toasts[0].id;
    dismiss(id);
    expect(toasts).toHaveLength(0);
  });

  it("dismiss is a no-op for an id that is already gone", () => {
    toast.success("temp");
    const id = toasts[0].id;
    dismiss(id);
    expect(() => dismiss(id)).not.toThrow();
    expect(toasts).toHaveLength(0);
  });

  it("dismissing one toast does not affect the others", () => {
    toast.success("keep");
    toast.error("remove");
    dismiss(toasts[1].id);
    expect(toasts).toHaveLength(1);
    expect(toasts[0].title).toBe("keep");
  });

  it("a manually dismissed toast's timer firing later is harmless", () => {
    toast.success("dismissed early");
    dismiss(toasts[0].id);
    expect(() => vi.advanceTimersByTime(5000)).not.toThrow();
    expect(toasts).toHaveLength(0);
  });

  it("clears the pending auto-dismiss timer handle on manual dismiss", () => {
    const clearSpy = vi.spyOn(globalThis, "clearTimeout");
    toast.success("manual clear");
    const id = toasts[0].id;
    dismiss(id);
    expect(clearSpy).toHaveBeenCalledTimes(1);
    clearSpy.mockRestore();
  });

  it("a cleared timer never runs its dismissal path again after manual dismiss", () => {
    const clearSpy = vi.spyOn(globalThis, "clearTimeout");
    toast.success("manual then advance");
    dismiss(toasts[0].id);
    clearSpy.mockClear();
    // The timer was already cleared above, so letting fake time run past
    // its original 5s deadline must not clear (or otherwise touch) it a
    // second time — there's nothing left pending to run.
    vi.advanceTimersByTime(5000);
    expect(clearSpy).not.toHaveBeenCalled();
    expect(toasts).toHaveLength(0);
    clearSpy.mockRestore();
  });
});
