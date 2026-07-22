#!/usr/bin/env python3
"""Tests for tools/common/proc.py."""

from __future__ import annotations

import subprocess
import sys

import pytest
from common.proc import run_captured, run_text


def test_run_captured_returns_completed_process() -> None:
    result = run_captured([sys.executable, "-c", "print('hello')"])
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"
    assert isinstance(result.stdout, str)


def test_run_captured_check_raises() -> None:
    with pytest.raises(subprocess.CalledProcessError):
        run_captured([sys.executable, "-c", "raise SystemExit(1)"], check=True)


def test_run_captured_failing_command_does_not_raise_by_default() -> None:
    result = run_captured([sys.executable, "-c", "raise SystemExit(1)"])
    assert result.returncode != 0


def test_run_captured_input() -> None:
    result = run_captured(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        input_text="payload\n",
    )
    assert result.stdout == "payload\n"


def test_run_text_returns_stdout() -> None:
    assert run_text([sys.executable, "-c", "print('hi')"]) == "hi"


def test_run_text_default_on_missing_binary() -> None:
    assert run_text(["this-binary-does-not-exist"], default="fallback") == "fallback"


def test_run_text_default_on_nonzero_exit() -> None:
    assert run_text([sys.executable, "-c", "raise SystemExit(1)"], default="fb") == "fb"
