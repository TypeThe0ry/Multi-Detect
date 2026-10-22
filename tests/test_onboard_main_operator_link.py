"""Real-runtime wiring of the operator link in scripts/onboard_main.py (no hardware)."""

from __future__ import annotations

import scripts.onboard_main as onboard_main


class _Closable:
    def __init__(self, name, calls, *, fail=False):
        self.name, self.calls, self.fail = name, calls, fail

    def close(self):
        self.calls.append(self.name)
        if self.fail:
            raise RuntimeError(f"{self.name} failed")


def test_shutdown_closes_each_resource_once_on_every_path() -> None:
    calls: list[str] = []
    runtime = _Closable("runtime", calls)
    errors = onboard_main._shutdown(
        runtime,
        _Closable("server", calls),
        _Closable("policy", calls),
        _Closable("camera", calls),
        _Closable("vehicle", calls),
    )
    # The runtime closes policy, camera and vehicle itself: no second close.
    assert errors == [] and calls == ["runtime", "server"]
    calls.clear()
    errors = onboard_main._shutdown(
        None,
        _Closable("server", calls),
        _Closable("policy", calls, fail=True),
        _Closable("camera", calls),
        _Closable("vehicle", calls),
    )
    # Without a runtime nothing leaked, even with an operator link open.
    assert calls == ["policy", "camera", "vehicle", "server"]
    assert errors == ["policy failed"]
