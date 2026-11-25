"""Test bootstrap for a source checkout.

Keep the source-path setup local to the test suite instead of mutating the
user's global ``PYTHONPATH`` or requiring an editable install just to run CI.
This also keeps the suite quiet on older pytest versions that do not implement
the ``pythonpath`` configuration option.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import socket
import subprocess
import tempfile
import sys
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

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


def _checkout_path(raw: object) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = _ROOT / path
    return path


def requires_path(
    path: object,
    *,
    reason: str = "external resource",
    module_level: bool = False,
) -> None:
    """Skip a test when an external file or directory is not available.

    ``artifacts/`` is a shared, read-only directory that is absent from a plain
    git checkout.  Evidence tests must not fail merely because those generated
    files were never committed; they run in full whenever the path is present.

    ``module_level`` is for artifacts read while a test module is imported, so
    collection skips instead of raising ``FileNotFoundError``.
    """

    target = _checkout_path(path)
    if not target.exists():
        pytest.skip(
            f"{reason} is not available in this checkout: {target}",
            allow_module_level=module_level,
        )


def requires_artifact(*paths: object, module_level: bool = False) -> None:
    """Skip when a shared evidence artifact is missing or outside the checkout.

    Worktrees usually expose the shared artifacts directory as a symlink to the
    primary checkout.  Evidence verifiers bind paths to the checkout root, so an
    artifact that resolves outside this worktree is not usable here even though
    it exists on disk.  In the primary checkout the same artifact resolves
    inside the root and every test runs in full.
    """

    for raw in paths:
        target = _checkout_path(raw)
        requires_path(target, reason="evidence artifact", module_level=module_level)
        try:
            target.resolve().relative_to(_ROOT.resolve())
        except ValueError:
            pytest.skip(
                "evidence artifact resolves outside this checkout; run in the "
                f"primary checkout that owns the shared artifacts: {target}",
                allow_module_level=module_level,
            )


def requires_module(name: str, *, module_level: bool = False) -> None:
    """Skip when an optional runtime dependency (torch, ultralytics, ...) is absent."""

    if importlib.util.find_spec(name) is None:
        pytest.skip(
            f"requires optional dependency not installed in this environment: {name}",
            allow_module_level=module_level,
        )


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
    """Apply onboard and frozen-source test collection markers.

    ``item.path`` is a pathlib object on current pytest releases; using
    ``name`` keeps this compatible with older releases as well.
    """

    # The R1 receipt hashes these test modules, so their optional-input guards
    # live here instead of changing the frozen bytes.
    schedule = _ROOT / "artifacts/training/onboard-navigation-balanced-schedule-20261007-r2/schedule.json"
    torch_available = importlib.util.find_spec("torch") is not None
    for item in items:
        filename = getattr(item, "path", None)
        name = filename.name if filename is not None else Path(str(item.fspath)).name
        if name in _ONBOARD_NAMES:
            item.add_marker(pytest.mark.onboard)
        if (
            name == "test_onboard_navigation_balanced_encoding.py"
            and "torch_cpu" in getattr(item, "fixturenames", ())
            and not torch_available
        ):
            item.add_marker(pytest.mark.skip(reason="navigation encoding CPU fixtures require optional PyTorch"))
        if (
            name == "test_onboard_navigation_balanced_journal.py"
            and item.name == "test_actual_frozen_schedule_static_admission_is_not_training_evidence"
            and not schedule.exists()
        ):
            item.add_marker(pytest.mark.skip(reason=f"required frozen R2 schedule is missing: {schedule}"))


# Keep this selection out of test modules whose exact bytes are evidence inputs.
_FRESH_INTERPRETER_TESTS = {
    "test_analyze_onboard_navigation_original59.py": {
        "test_cli_create_only_and_explicit_report_sha",
        "test_closed_source_only59_exposes_majority_baseline_and_failures",
        "test_null_metrics_and_missing_class_support_are_never_zero_errors",
        "test_only_small_consumed_files_are_opened_no_images_weights_wal_or_annotations",
        "test_raw_correct_budget_failure_never_wins_budget_qualified_pair",
    },
    "test_audit_onboard_navigation_dataset_overlap.py": {
        "test_actual_pillow_runtime_origin_and_wrong_source_rejected",
        "test_execute_fixture_closes_sources_and_never_claims_training",
    },
    "test_audit_onboard_navigation_live_features.py": {
        "test_complete_cpu_feature_byte_replay_and_distribution",
        "test_hash_consistent_false_six_values_cannot_replay_feature",
        "test_late_bound_input_drift_prevents_success",
        "test_nonfinite_feature_bytes_are_rejected_even_with_matching_hashes",
        "test_output_create_only_and_no_recompute_on_existing",
    },
    "test_audit_onboard_navigation_recovery267_terminal.py": {
        "test_frozen_journal_lazy_import_does_not_load_ml",
    },
    "test_verify_onboard_navigation_indexed_cpu_inputs.py": {
        "test_run122_software_orchestration_success_partial_and_source_drift",
    },
    "test_verify_onboard_navigation_indexed_cpu_inputs_v2.py": {
        "test_cleanup_error_does_not_replace_existing_body_error",
        "test_exception_cleanup_preserves_body_error_and_removes_only_owned_finder",
        "test_preloaded_forbidden_modules_rejected_without_module_or_finder_deletion",
        "test_run_real122_loop_fixture_partial_and_false_final_gates",
        "test_scope_blocks_optional_arrow_only_and_preserves_legal_dependency_finder",
        "test_scope_integrity_failure_cannot_be_hidden_by_finder_equality",
    },
}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    """Adapt Windows fixture paths without changing receipt-bound test files."""
    with pytest.MonkeyPatch.context() as patch:
        if os.name != "nt" and item.path.name == "test_onboard_live_vlm_backend.py":
            factory = item.module.LiveQwenVlmBackendFactory

            def local_factory(snapshot_path, *args, **kwargs):
                if snapshot_path in {"D:/fixture", "D:/fixture-local-model"}:
                    snapshot_path = str(item.path.with_name(snapshot_path.split("/")[-1]))
                return factory(snapshot_path, *args, **kwargs)

            patch.setattr(item.module, "LiveQwenVlmBackendFactory", local_factory)
        elif item.path.name == "test_onboard_vlm_shadow_input.py":
            bad_path = item.funcargs.get("bad_path")
            if bad_path and not Path(bad_path).anchor and ":" in bad_path:
                outside = item.funcargs["tmp_path"].parent / bad_path
                patch.setitem(item.funcargs, "bad_path", str(outside))
        elif (
            item.path.name == "test_run_onboard_vlm_runtime_replay.py"
            and item.originalname == "test_bad_case_and_manifest_identity_reject_before_creating_output"
        ):
            original_args = item.module._args

            def args_with_fixture_drive(tmp_path):
                args = original_args(tmp_path)
                output = args.output_directory
                args.output_directory = SimpleNamespace(
                    resolve=lambda: SimpleNamespace(exists=output.exists, drive="D:"),
                    exists=output.exists,
                )
                return args

            patch.setattr(item.module, "_args", args_with_fixture_drive)
        yield


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    """Run import-sensitive tests with their original fixtures in a clean interpreter."""
    if pyfuncitem.originalname not in _FRESH_INTERPRETER_TESTS.get(pyfuncitem.path.name, ()):
        return None
    worker_node = "MULTIDETECT_PYTEST_FRESH_NODE"
    if os.environ.get(worker_node) == pyfuncitem.nodeid:
        return None

    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env[worker_node] = pyfuncitem.nodeid
    with tempfile.TemporaryDirectory(prefix="pytest-fresh-interpreter-") as directory:
        temp = Path(directory)
        report = temp / "report.xml"
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:xdist", "-p", "no:cacheprovider",
             "--color=no", "--basetemp", str(temp / "pytest"), "--junitxml", str(report),
             pyfuncitem.nodeid],
            cwd=pyfuncitem.config.invocation_params.dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        output = proc.stdout + proc.stderr
        if proc.returncode:
            pytest.fail(
                f"Fresh interpreter exited {proc.returncode} for {pyfuncitem.nodeid}\n{output}",
                pytrace=False,
            )
        cases = ElementTree.parse(report).findall(".//testcase")
    if len(cases) != 1:
        pytest.fail(f"Expected one worker test result, got {len(cases)}\n{output}", pytrace=False)
    skipped = cases[0].find("skipped")
    if skipped is not None:
        pytest.skip(skipped.get("message") or skipped.text or "Fresh interpreter skipped the test")
    return True
