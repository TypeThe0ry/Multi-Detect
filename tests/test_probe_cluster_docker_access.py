from __future__ import annotations

import json
import sys

import pytest

from scripts import probe_cluster_docker_access as diagnostic


def result(stdout="", stderr="", *, code=0, timed_out=False):
    return {
        "exit_code": None if timed_out else code,
        "succeeded": code == 0 and not timed_out,
        "failure": "TimeoutExpired" if timed_out else None,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "timed_out": timed_out,
        "elapsed_s": 0.01,
    }


def configure(monkeypatch, outputs, *, launcher="/usr/bin/docker", real_path=None):
    calls = []
    monkeypatch.setattr(
        diagnostic.shutil, "which", lambda name: launcher if name == "docker" else None
    )
    monkeypatch.setattr(diagnostic.os.path, "realpath", lambda path: real_path or path)

    def run(command, timeout):
        calls.append((command, timeout))
        return dict(outputs[len(calls) - 1])

    monkeypatch.setattr(diagnostic, "command_result", run)
    return calls


def test_absent_cli_never_runs_commands_or_claims_runtime(monkeypatch):
    calls = configure(monkeypatch, [], launcher=None)
    report = diagnostic.probe()
    assert not calls
    assert report["diagnosis"] == "docker_cli_absent"
    assert report["launcher"] == {
        "which_path": None,
        "real_path": None,
        "snap_path_evidence": False,
    }
    assert report["ready"] is False
    for key in (
        "image_inspection_performed",
        "service_lifecycle_changed",
        "gazebo_verified",
        "rendering_verified",
        "flight_verified",
        "training_ready",
        "candidate_promoted",
        "hil_real_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
    ):
        assert report[key] is False


def test_ready_means_only_native_daemon_response(monkeypatch):
    calls = configure(
        monkeypatch,
        [
            result("Docker version 29.7.2"),
            result("desktop-linux"),
            result("npipe:////./pipe/dockerDesktopLinuxEngine"),
            result("29.7.2"),
        ],
    )
    report = diagnostic.probe(5)
    assert report["ready"] and report["daemon_ready"]
    assert report["context_name"] == "desktop-linux"
    assert report["endpoint_host"] == "npipe:////./pipe/dockerDesktopLinuxEngine"
    assert report["flight_verified"] is False
    assert len(calls) == 4
    assert all(timeout == 5 for _, timeout in calls)
    assert calls[2][0] == [
        "/usr/bin/docker",
        "context",
        "inspect",
        "desktop-linux",
        "--format",
        diagnostic.HOST_FORMAT,
    ]
    assert calls[3][0] == ["/usr/bin/docker", "info", "--format", "{{.ServerVersion}}"]
    assert not any(
        word in {"image", "pull", "run", "start", "stop", "sudo"}
        for command, _ in calls
        for word in command
    )


def test_snap_profile_failure_before_daemon_is_not_image_absence(monkeypatch):
    error = "cannot change profile for the next exec call: Operation not permitted"
    calls = configure(
        monkeypatch,
        [result(stderr=error, code=1), result(stderr=error, code=1)],
        launcher="/snap/bin/docker",
        real_path="/usr/bin/snap",
    )
    report = diagnostic.probe()
    assert len(calls) == 2
    assert report["launcher"]["snap_path_evidence"] is True
    assert report["profile_error_stages"] == ["client_version", "docker_info"]
    assert report["diagnosis"] == "launcher_profile_restriction_before_daemon_query"
    assert not report["ready"]
    assert "image" not in report["diagnosis"]


def test_profile_failure_only_at_info_does_not_claim_launcher_or_daemon_proof(monkeypatch):
    error = "cannot change profile for the next exec call: Operation not permitted"
    configure(
        monkeypatch,
        [
            result("Docker version 27.0"),
            result("default"),
            result("unix:///var/run/docker.sock"),
            result(stderr=error, code=1),
        ],
    )
    report = diagnostic.probe()
    assert report["profile_error_stages"] == ["docker_info"]
    assert report["diagnosis"] == "profile_restriction_at_command_stage_daemon_state_unknown"


def test_info_timeout_is_not_daemon_missing(monkeypatch):
    configure(monkeypatch, [result("Docker version 27.0"), result(code=1), result(timed_out=True)])
    report = diagnostic.probe(0.5)
    assert report["docker_info"]["exit_code"] is None
    assert report["diagnosis"] == "daemon_query_timed_out_state_unknown"
    assert not report["ready"]


def test_context_failure_does_not_block_independent_daemon_query(monkeypatch):
    configure(monkeypatch, [result("Docker version 27.0"), result(code=1), result("27.0")])
    report = diagnostic.probe()
    assert report["context_inspect_host"] is None
    assert report["ready"]


def test_full_context_configuration_ignored_even_if_launcher_ignores_format(monkeypatch):
    secret = '{"Endpoints":{"docker":{"Host":"unix:///x","Auth":"TOP-SECRET"}}}'
    configure(
        monkeypatch,
        [
            result("Docker version 27.0"),
            result("default"),
            result(secret, "context token=SIDE-SECRET"),
            result(code=1),
        ],
    )
    report = diagnostic.probe()
    encoded = json.dumps(report)
    assert report["endpoint_host"] is None
    assert "TOP-SECRET" not in encoded and "SIDE-SECRET" not in encoded
    assert "Endpoints" not in encoded
    assert report["context_inspect_host"]["stdout"] == "[invalid Host omitted]"


def test_malformed_context_name_never_becomes_argument(monkeypatch):
    calls = configure(
        monkeypatch,
        [result("Docker version 27.0"), result("context token=supersecret"), result(code=1)],
    )
    report = diagnostic.probe()
    assert len(calls) == 3
    assert report["context_name"] is None
    assert "supersecret" not in json.dumps(report)


def test_real_redacted_context_host_remains_parseable(monkeypatch):
    host, _ = diagnostic._bounded_text("ssh://operator:secret@host.example:22?token=private")
    configure(
        monkeypatch, [result("Docker version 27.0"), result("remote"), result(host), result(code=1)]
    )
    report = diagnostic.probe()
    assert report["endpoint_host"] == "ssh://host.example:22"
    assert all(secret not in json.dumps(report) for secret in ("operator", "secret", "private"))


@pytest.mark.parametrize("value", [0, 0.09, 5.01, 100, float("nan"), float("inf"), True, "3"])
def test_invalid_timeout_does_not_launch_anything(monkeypatch, value):
    calls = configure(monkeypatch, [])
    with pytest.raises(ValueError, match="timeout"):
        diagnostic.probe(value)
    assert not calls


@pytest.mark.parametrize(
    "host, expected",
    [
        ("unix:///var/run/docker.sock", "unix:///var/run/docker.sock"),
        ("tcp://user:password@127.0.0.1:2375?token=private#password", "tcp://127.0.0.1:2375"),
        (
            "ssh://operator:password@host.example/run/docker.sock?private=secret",
            "ssh://host.example/run/docker.sock",
        ),
        ("https://host.example:2376?token=private", "https://host.example:2376"),
        ("invalid data", None),
        ("file:///private/path", None),
        ('{"Auth":"secret"}', None),
    ],
)
def test_only_safe_host_is_returned(host, expected):
    assert diagnostic.safe_endpoint(host) == expected


def test_command_native_success_and_redaction():
    native = diagnostic.command_result(
        [
            sys.executable,
            "-c",
            "print('password=notforreport Bearer sensitive https://user:secret@host token=hidden')",
        ],
        5,
    )
    assert native["exit_code"] == 0
    assert native["succeeded"]
    assert all(
        secret not in native["stdout"]
        for secret in ("notforreport", "sensitive", "user:secret", "hidden")
    )
    assert "[redacted]" in native["stdout"]


def test_command_bounds_flooded_stdout_and_stderr():
    native = diagnostic.command_result(
        [
            sys.executable,
            "-c",
            "import sys;sys.stdout.write('x'*40000);sys.stderr.write('y'*40000)",
        ],
        5,
    )
    assert native["exit_code"] == 0
    assert len(native["stdout"]) == diagnostic.MAX_OUTPUT_CHARS
    assert len(native["stderr"]) == diagnostic.MAX_OUTPUT_CHARS
    assert native["stdout_truncated"] and native["stderr_truncated"]


def test_timeout_kills_only_owned_diagnostic_and_returns_bounded_result():
    native = diagnostic.command_result([sys.executable, "-c", "import time;time.sleep(10)"], 0.1)
    assert not native["succeeded"]
    assert native["exit_code"] is None
    assert native["failure"] == "TimeoutExpired"
    assert native["timed_out"]
    assert native["elapsed_s"] < 2


def test_os_error_is_diagnostic_not_uncaught(monkeypatch):
    def failed(*args, **kwargs):
        raise OSError("launcher denied token=hidden")

    monkeypatch.setattr(diagnostic.subprocess, "Popen", failed)
    native = diagnostic.command_result(["missing", "--version"], 1)
    assert not native["succeeded"]
    assert native["failure"] == "OSError"
    assert "hidden" not in native["stderr"]


@pytest.mark.parametrize("ready, expected_exit", [(True, 0), (False, 2)])
def test_main_records_readiness_native_code_and_new_report(
    tmp_path, monkeypatch, capsys, ready, expected_exit
):
    report = {"ready": ready, "read_only": True}
    monkeypatch.setattr(diagnostic, "probe", lambda timeout: report)
    output = tmp_path / "exclusive" / "docker.json"
    assert diagnostic.main(["--output", str(output)]) == expected_exit
    assert json.loads(output.read_text()) == report
    assert json.loads(capsys.readouterr().out) == report


def test_existing_output_is_untouched_and_probe_not_called(tmp_path, monkeypatch):
    output = tmp_path / "existing.json"
    output.write_bytes(b"original exact bytes")
    monkeypatch.setattr(diagnostic, "probe", lambda timeout: pytest.fail("probe must not run"))
    with pytest.raises(FileExistsError):
        diagnostic.main(["--output", str(output)])
    assert output.read_bytes() == b"original exact bytes"


def test_native_failure_code_is_preserved():
    native = diagnostic.command_result([sys.executable, "-c", "raise SystemExit(7)"], 5)
    assert native["exit_code"] == 7
    assert not native["succeeded"]


def test_no_full_environment_or_security_profile_overrides_in_launch(monkeypatch):
    def inspect_call(command, **kwargs):
        assert command == ["docker", "info", "--format", "{{.ServerVersion}}"]
        assert "env" not in kwargs
        assert kwargs["shell"] is False
        raise OSError("denied")

    monkeypatch.setattr(diagnostic.subprocess, "Popen", inspect_call)
    diagnostic.command_result(["docker", "info", "--format", "{{.ServerVersion}}"], 1)


def test_output_does_not_interpret_subprocess_timeout_as_gazebo_readiness(monkeypatch):
    configure(monkeypatch, [result(code=1), result(timed_out=True)])
    report = diagnostic.probe()
    assert report["docker_info"]["timed_out"]
    assert report["gazebo_verified"] is False and report["rendering_verified"] is False
    assert report["sensor_capture_performed"] is False


def test_profile_stage_is_matched_case_insensitively(monkeypatch):
    configure(
        monkeypatch, [result(stderr=diagnostic.PROFILE_ERROR.upper(), code=1), result(code=1)]
    )
    assert diagnostic.probe()["profile_error_stages"] == ["client_version"]


def test_no_service_changes_when_launcher_version_is_missing(monkeypatch):
    calls = configure(monkeypatch, [result(code=127), result(code=127)])
    report = diagnostic.probe()
    assert [command[1] for command, _ in calls] == ["--version", "info"]
    assert report["diagnosis"] == "launcher_or_client_failed_daemon_not_proven_ready"


def test_stdout_cap_does_not_expose_redacted_tail():
    text, truncated = diagnostic._bounded_text("password=" + "s" * 10000)
    assert "s" * 30 not in text
    assert text == "password=[redacted]"
    assert truncated is False


def test_context_profile_error_marker_survives_safe_host_redaction(monkeypatch):
    configure(
        monkeypatch,
        [
            result("Docker version 27.0"),
            result("default"),
            result(stderr=diagnostic.PROFILE_ERROR, code=1),
            result(code=1),
        ],
    )
    report = diagnostic.probe()
    assert report["profile_error_stages"] == ["context_inspect_host"]
    assert report["context_inspect_host"]["stderr"] == "[context error omitted]"


@pytest.mark.parametrize(
    "text",
    [
        '{"auth":{"username":"operator","password":"unknown"}}',
        'failed authentication: auth={"username": "operator", "token": "secret"}',
        '["environment", "PRIVATE_TOKEN=unknown"]',
    ],
)
def test_structured_config_cannot_leak_through_error_output(text):
    redacted = diagnostic.redact_text(text)
    assert "unknown" not in redacted and "operator" not in redacted and "secret" not in redacted
    assert "omitted" in redacted
