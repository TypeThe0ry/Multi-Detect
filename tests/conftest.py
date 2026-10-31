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

_ARTIFACTS = _ROOT / "artifacts"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Skip tests whose only failure is a missing local artifacts/ file.

    Fresh checkouts and git worktrees do not carry the heavyweight local
    artifacts tree. Tests that need it should skip with a clear reason instead
    of erroring. Any other failure is reported normally.
    """
    outcome = yield
    report = outcome.get_result()
    if not report.failed or call.excinfo is None:
        return
    exc = call.excinfo.value
    if not isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return
    missing = Path(getattr(exc, "filename", "") or "")
    if not missing.is_absolute() or _ARTIFACTS not in missing.parents:
        return
    report.outcome = "skipped"
    report.longrepr = (
        "Skipped: local artifact is absent in this checkout: " + str(missing)
    )
