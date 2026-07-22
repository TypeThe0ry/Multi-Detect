#!/usr/bin/env python3
"""Run aqtinstall with the Qt 6.11 Windows repository-layout fix.

Qt 6.11 split each Windows desktop architecture into its own metadata
directory. aqtinstall 3.3.0 still requests the pre-6.11 path, so it cannot
install a version it can list. This entry point patches only the repository
folder suffix for the MSVC 2022 architecture used by QGroundControl. Archive
hash validation and every other aqt behavior remain unchanged.

Remove this shim once the pinned aqtinstall release supports the Qt 6.11
layout: https://github.com/miurahr/aqtinstall/issues/1007
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def qt_611_repository_extension(
    architecture: str,
    is_version_ge_6: bool,
    fallback: Callable[[str, bool], str],
) -> str:
    """Return the Qt 6.11 repository suffix for supported Windows MSVC arches."""
    if architecture.startswith("win64_msvc2022_"):
        return architecture.removeprefix("win64_")
    return fallback(architecture, is_version_ge_6)


def main() -> int:
    from aqt import main as aqt_main
    from aqt.metadata import QtRepoProperty

    original = QtRepoProperty.extension_for_arch
    QtRepoProperty.extension_for_arch = staticmethod(
        lambda architecture, is_version_ge_6: qt_611_repository_extension(
            architecture,
            is_version_ge_6,
            original,
        )
    )
    return int(aqt_main())


if __name__ == "__main__":
    raise SystemExit(main())
