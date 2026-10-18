"""`_coalesced`: one fan-out per burst of concurrent callers."""

import threading
import time

import pytest

from kobo_api.main import _coalesced, _InFlight


def test_concurrent_callers_share_one_run_and_sequential_callers_run_again():
    table: dict[str, _InFlight] = {}
    runs: list[int] = []
    gate = threading.Event()

    def slow():
        runs.append(1)
        gate.wait(timeout=5)
        return ("ok", len(runs))

    results: list[object] = []
    threads = [
        threading.Thread(target=lambda: results.append(_coalesced(table, "feed:a", slow)))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(timeout=5)

    assert results == [("ok", 1)] * 5
    assert table == {}

    assert _coalesced(table, "feed:a", slow) == ("ok", 2)


def test_the_leaders_exception_reaches_every_waiter():
    table: dict[str, _InFlight] = {}
    gate = threading.Event()

    def boom():
        gate.wait(timeout=5)
        raise RuntimeError("switchboard down")

    errors: list[str] = []

    def call():
        try:
            _coalesced(table, "feed:b", boom)
        except RuntimeError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=call) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(timeout=5)

    assert errors == ["switchboard down"] * 3
    assert table == {}


def test_a_different_key_is_a_different_run():
    table: dict[str, _InFlight] = {}
    assert _coalesced(table, "feed:x", lambda: 1) == 1
    assert _coalesced(table, "feed:y", lambda: 2) == 2
    with pytest.raises(ValueError):
        _coalesced(table, "feed:x", lambda: (_ for _ in ()).throw(ValueError("x")))
