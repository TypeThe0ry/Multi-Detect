"""Add only scoped repository/src admission before the sealed child ctor import.

The r1 no-warmup body remains unchanged and explicitly bound. Parent admission
is standard-library-only; this source becomes the hash-bound dispatch entrypoint.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
from scripts import run_onboard_navigation_pilot_streaming_no_warmup as body  # noqa: E402

sup = body.sup
BODY_SOURCE = Path(body.__file__).resolve(strict=True)
BODY_SHA256 = "5e46b8b025175ad53da211b31eef81f7dabb7586ec8b2f7ba35f3e8a249005e2"
SRC = (WORKSPACE / "src").resolve(strict=True)
CONTRACT = "multidetect.onboard-navigation-streaming-no-warmup-importpath-runtime-v1"
POLICY = "install_repository_src_before_constructor_import"
SCOPE = "this_child_only_constructor_import_scoped_sys_path"
DECLARATIONS = {
    "constructor_importpath_policy": POLICY,
    "constructor_importpath_scope": SCOPE,
    "constructor_importpath": str(SRC),
}


def validate_importpath_manifest(manifest, records):
    by_path = {record["path"]: record for record in records}
    if (
        by_path.get(str(BODY_SOURCE)) != sup.file_record(BODY_SOURCE)
        or sup.sha256_file(BODY_SOURCE) != BODY_SHA256
    ):
        raise ValueError("Sealed no-warmup r1 implementation must be explicitly bound")
    if not SRC.is_dir() or any(manifest.get(key) != value for key, value in DECLARATIONS.items()):
        raise ValueError("Constructor repository/src import path identity or scope differs")


@contextlib.contextmanager
def repository_src_scope():
    original = sys.path.copy()
    try:
        sys.path.insert(0, str(SRC))
        yield
    finally:
        sys.path[:] = original


@contextlib.contextmanager
def dispatch_scope():
    """Redirect only dispatch identity and this child's constructor import hook."""
    original = (
        body.CONTRACT,
        body.__file__,
        body.validate_manifest,
        body.install_constructor_traceback,
        body.DECLARATIONS,
    )
    original_path = sys.path.copy()

    def checked(manifest, records):
        validate_importpath_manifest(manifest, records)
        return original[2](manifest, records)

    def constructor(manifest):
        with repository_src_scope():
            sup.emit("constructor_importpath_installed", pid=os.getpid(), **DECLARATIONS)
            return original[3](manifest)

    try:
        body.CONTRACT, body.__file__, body.validate_manifest = CONTRACT, __file__, checked
        body.install_constructor_traceback = constructor
        body.DECLARATIONS = {**original[4], **DECLARATIONS}
        yield
    finally:
        (
            body.CONTRACT,
            body.__file__,
            body.validate_manifest,
            body.install_constructor_traceback,
            body.DECLARATIONS,
        ) = original
        sys.path[:] = original_path


def worker(args):
    with dispatch_scope():
        return body.worker(args)


def supervise(args, *, popen_factory=sup.subprocess.Popen):
    with dispatch_scope():
        return body.supervise(args, popen_factory=popen_factory)


def main(argv=None):
    with dispatch_scope():
        return body.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
