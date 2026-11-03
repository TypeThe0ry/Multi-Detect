"""Test bootstrap for a source checkout.

Keep the source-path setup local to the test suite instead of mutating the
user's global ``PYTHONPATH`` or requiring an editable install just to run CI.
This also keeps the suite quiet on older pytest versions that do not implement
the ``pythonpath`` configuration option.
"""

from __future__ import annotations

import errno
import json
import socket
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SRC = _ROOT / "src"
if _SRC.is_dir():
    _src_text = str(_SRC)
    if _src_text not in sys.path:
        sys.path.insert(0, _src_text)


# Keep the onboard gate's scope in one place.  A collection hook is used
# instead of decorators in every file so newly added tests in one of these
# modules are included automatically and existing assertions stay untouched.
# The patterns intentionally describe test *files*, not production modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from onboard_gate_files import files as _onboard_gate_files  # noqa: E402

_ONBOARD_NAMES = frozenset(p.name for p in _onboard_gate_files())


@pytest.fixture(scope="session")
def require_loopback_sockets() -> None:
    """Skip network integration tests only when host policy denies local sockets."""
    for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
        try:
            with socket.socket(socket.AF_INET, kind) as channel:
                channel.bind(("127.0.0.1", 0))
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EACCES):
                raise
            pytest.skip(f"loopback sockets denied by host sandbox: {exc}")


@pytest.fixture(scope="session")
def require_current_onboard_model() -> None:
    """Keep real model-reference integration separate from missing shared artifacts."""
    reference = _ROOT / "configs/training/onboard-policy-current-reference-v1.json"
    document = json.loads(reference.read_text(encoding="utf-8"))
    for key in ("model", "training_config", "training_report"):
        path = (_ROOT / document[key]["path"]).resolve()
        if not path.is_relative_to(_ROOT) or not path.is_file():
            pytest.skip(f"current onboard model requires repository-local bound artifacts: {path}")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark the deterministic onboard autonomy subset for ``-m onboard``.

    ``item.path`` is a pathlib object on current pytest releases; using
    ``name`` keeps this compatible with older releases as well.
    """

    for item in items:
        filename = getattr(item, "path", None)
        name = filename.name if filename is not None else Path(str(item.fspath)).name
        if name in _ONBOARD_NAMES:
            item.add_marker(pytest.mark.onboard)
