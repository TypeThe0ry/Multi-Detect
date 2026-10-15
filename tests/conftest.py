"""Test bootstrap for a source checkout.

Keep the source-path setup local to the test suite instead of mutating the
user's global ``PYTHONPATH`` or requiring an editable install just to run CI.
This also keeps the suite quiet on older pytest versions that do not implement
the ``pythonpath`` configuration option.
"""

from __future__ import annotations

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
