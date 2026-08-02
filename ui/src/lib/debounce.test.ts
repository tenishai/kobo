import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { debounced } from "./debounce";

describe("debounced", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("fires once after the quiet period elapses", () => {
    const fn = vi.fn();
    const call = debounced(fn, 350);

    call("a");
    expect(fn).not.toHaveBeenCalled();

    vi.advanceTimersByTime(350);
    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn).toHaveBeenCalledWith("a");
  });

  it("collapses rapid calls into a single trailing invocation", () => {
    const fn = vi.fn();
    const call = debounced(fn, 350);

    call("a");
    vi.advanceTimersByTime(100);
    call("b");
    vi.advanceTimersByTime(100);
    call("c");
    vi.advanceTimersByTime(349);
    expect(fn).not.toHaveBeenCalled();

    vi.advanceTimersByTime(1);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("uses the latest value when calls collapse", () => {
    const fn = vi.fn();
    const call = debounced(fn, 350);

    call("a");
    call("b");
    call("c");
    vi.advanceTimersByTime(350);

    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn).toHaveBeenCalledWith("c");
  });

  it("cancel prevents a pending call from firing", () => {
    const fn = vi.fn();
    const call = debounced(fn, 350);

    call("a");
    call.cancel();
    vi.advanceTimersByTime(1000);

    expect(fn).not.toHaveBeenCalled();
  });

  it("defaults to a 350ms quiet period", () => {
    const fn = vi.fn();
    const call = debounced(fn);

    call("a");
    vi.advanceTimersByTime(349);
    expect(fn).not.toHaveBeenCalled();

    vi.advanceTimersByTime(1);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("fires again on a later call after a previous debounce resolved", () => {
    const fn = vi.fn();
    const call = debounced(fn, 350);

    call("a");
    vi.advanceTimersByTime(350);
    expect(fn).toHaveBeenCalledTimes(1);

    call("b");
    vi.advanceTimersByTime(350);
    expect(fn).toHaveBeenCalledTimes(2);
    expect(fn).toHaveBeenCalledWith("b");
  });
});
