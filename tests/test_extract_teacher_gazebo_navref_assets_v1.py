from __future__ import annotations

import copy
import hashlib
import io
import json
import tarfile

import pytest

from scripts import extract_teacher_gazebo_navref_assets_v1 as probe


def _archive(name="model.sdf", body=b"<sdf/>", kind=tarfile.REGTYPE):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        item = tarfile.TarInfo(name)
        item.type = kind
        if kind == tarfile.REGTYPE:
            item.size = len(body)
        archive.addfile(item, io.BytesIO(body))
    return stream.getvalue()


def _result(stdout=b"", stderr=b"", code=0):
    return {
        "native_exit_code": code,
        "complete_output_observed": True,
        "reader_threads_exited": True,
        "streams": {
            label: {"prefix": raw, "all_bytes_retained": True}
            for label, raw in (("stdout", stdout), ("stderr", stderr))
        },
    }


class Native:
    def __init__(self):
        self.calls = []
        self.created = False
        self.removed = False
        self.identity = "1" * 64
        self.name = self.owner = None
        self.bad_archive = False
        self.bad_owner = False

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        assert kwargs["timeout_s"] == 15
        assert kwargs["prefix_bytes"] <= 256 * 1024
        args = argv[1:]
        if args[:2] == ["image", "inspect"]:
            return _result(b"[]")
        if args[0] == "create":
            assert "--network" in args and "none" in args
            self.created = True
            self.name = args[args.index("--name") + 1]
            self.owner = next(
                v.split("=", 1)[1] for v in args if v.startswith(probe.OWNER_LABEL + "=")
            )
            return _result((self.identity + "\n").encode())
        if args[0] == "inspect":
            assert args[1] == self.identity
            if self.removed:
                return _result(b"[]\n", ("Error: No such object: " + self.identity).encode(), 1)
            record = {
                "Id": self.identity,
                "Name": "/" + self.name,
                "Config": {
                    "Image": probe.PINNED_IMAGE,
                    "Labels": {
                        probe.OWNER_LABEL: "foreign" if self.bad_owner else self.owner,
                        "multidetect.purpose": probe.PURPOSE,
                    },
                },
                "State": {"Status": "created", "Running": False},
            }
            return _result(json.dumps([record]).encode())
        if args[0] == "cp":
            assert args[1].startswith(self.identity + ":") and args[2] == "-"
            name = args[1].rsplit("/", 1)[1]
            return _result(b"invalid" if self.bad_archive else _archive(name))
        if args[0] == "rm":
            assert args == ["rm", self.identity]  # no broad name/-f deletion
            self.removed = True
            return _result((self.identity + "\n").encode())
        raise AssertionError(argv)


def test_never_started_extraction_keeps_original_bytes_and_verifies_exact_cleanup(tmp_path):
    (tmp_path / "artifacts/diagnostics").mkdir(parents=True)
    native = Native()
    directory = tmp_path / "artifacts/diagnostics/probe"
    report = probe.extract_navref_assets(directory, repository_root=tmp_path, native=native)
    assert report["status"] == "original-image-assets-recorded"
    assert report["never_started_identity_verified"] is True
    assert report["owned_container_absent_after_cleanup"] is True
    assert report["injected_native_used"] is True
    assert len(report["assets"]) == 3
    for asset in report["assets"].values():
        source = asset["source"]
        assert source["bytes"] == 6
        assert source["sha256"] == hashlib.sha256(b"<sdf/>").hexdigest()
    assert all(v is False for v in report["claims"].values())
    assert not any(call[1] in {"run", "start", "exec", "stop", "kill"} for call in native.calls)
    assert (directory / "extraction.json").is_file()


def test_invalid_original_archive_fails_and_still_cleans_only_owned_container(tmp_path):
    (tmp_path / "artifacts/diagnostics").mkdir(parents=True)
    native = Native()
    native.bad_archive = True
    report = probe.extract_navref_assets(
        tmp_path / "artifacts/diagnostics/probe", repository_root=tmp_path, native=native
    )
    assert report["status"] == "asset-extraction-failed"
    assert native.removed is True
    assert report["owned_container_absent_after_cleanup"] is True
    assert "TAR" in report["error"]


def test_foreign_inspection_is_never_deleted_even_after_extraction_failure(tmp_path):
    (tmp_path / "artifacts/diagnostics").mkdir(parents=True)
    native = Native()
    native.bad_owner = True
    report = probe.extract_navref_assets(
        tmp_path / "artifacts/diagnostics/probe", repository_root=tmp_path, native=native
    )
    assert report["status"] == "asset-extraction-failed"
    assert native.removed is False
    assert not any(call[1] == "rm" for call in native.calls)
    assert report["cleanup_error"] is not None


@pytest.mark.parametrize("name", ["../model.sdf", "/model.sdf", "nested/model.sdf", "other.sdf"])
def test_archive_paths_are_not_extracted_or_forgiven(name):
    with pytest.raises(ValueError, match="exact single"):
        probe._single_tar_file(_archive(name), "model.sdf")


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE]
)
def test_archive_links_or_special_members_are_refused(kind):
    with pytest.raises(ValueError, match="exact single"):
        probe._single_tar_file(_archive(kind=kind), "model.sdf")


@pytest.mark.parametrize("body", [b"", b"x" * (128 * 1024 + 1)], ids=["empty", "oversized"])
def test_archive_body_is_bounded_and_nonempty(body):
    with pytest.raises(ValueError):
        probe._single_tar_file(_archive(body=body), "model.sdf")


@pytest.mark.parametrize(
    "trailing",
    [b"nonzero ignored suffix", _archive(name="other.sdf")],
    ids=["junk", "concatenated"],
)
def test_archive_nonzero_trailing_bytes_or_concatenated_archive_are_not_ignored(trailing):
    with pytest.raises(ValueError, match="trailing"):
        probe._single_tar_file(_archive() + trailing, "model.sdf")


def test_existing_or_outside_directory_refused_without_any_native_call(tmp_path):
    directory = tmp_path / "artifacts/diagnostics/probe"
    directory.mkdir(parents=True)
    native = Native()
    for path in (directory, tmp_path / "outside"):
        with pytest.raises(ValueError, match="fresh contained"):
            probe.extract_navref_assets(path, repository_root=tmp_path, native=native)
    assert native.calls == []


@pytest.mark.parametrize(
    "field,value",
    [("Running", True), ("Status", "running"), ("Status", "exited")],
)
def test_owned_but_started_container_is_not_removed(field, value):
    record = {
        "Id": "1" * 64,
        "Name": "/test",
        "Config": {
            "Image": probe.PINNED_IMAGE,
            "Labels": {probe.OWNER_LABEL: "owner", "multidetect.purpose": probe.PURPOSE},
        },
        "State": {"Status": "created", "Running": False},
    }
    record = copy.deepcopy(record)
    record["State"][field] = value
    with pytest.raises(ValueError, match="never-started"):
        probe._owned_created(record, identity="1" * 64, name="test", owner="owner")
