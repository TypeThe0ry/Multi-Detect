#!/usr/bin/env python3
"""Repeat the X1 gimbal evaluation with fresh tags under the shared flight lock.

This replaces the ad-hoc ``x1-repeat-driver*.sh`` scripts.  For every tag it
writes ``configs/simulation/x1-gimbal-eval.json``, waits for the X1 lock, then
launches both eval lanes through ``x1_collect_next.py`` and retries pending jobs
for a bounded number of rounds.  The original config and any GPU opt-in marker
created by this run are always restored, including on exceptions and signals.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if __name__ == "__main__":
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from scripts.realworld.x1_lock import LOCK, held, sidecar  # noqa: E402

CONFIG = ROOT / "configs/simulation/x1-gimbal-eval.json"
COLLECT = ROOT / "scripts/realworld/x1_collect_next.py"
DEFAULT_LANES = ("a", "b")
DEFAULT_MARKERS = (
    Path("/data/scratch/x1-allow-training-share"),
    Path("/data/scratch/x1-allow-fluidx3d-share"),
    Path("/data/scratch/x1-allow-sunshine-compute"),
)
UNIT = "md-x1-gimbal-eval-{lane}.service"
LOCK_WORDS = ("lock held", "waiting for x1 lock")


class LockTimeout(TimeoutError):
    """The X1 lock stayed busy past the configured deadline."""


class UnitTimeout(TimeoutError):
    """A lane unit stayed active past the configured deadline."""


def dump_config(config):
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n"


def build_config(tag, sites, *, image=None, fw_legs=True, extra=None, tag_prefix=""):
    if type(tag) is not str or not tag:
        raise ValueError("tag must be a non-empty string")
    if not sites:
        raise ValueError("at least one site is required")
    config = {
        "schema_version": 1,
        "enabled": True,
        "gimbal_eval": True,
        "fw_legs": bool(fw_legs),
    }
    if image is not None:
        if type(image) is not str or not image:
            raise ValueError("image must be a non-empty string")
        config["image"] = image
    config.update(extra or {})
    config["tag"] = f"{tag_prefix}{tag}"
    config["only"] = list(sites)
    return config


def parse_extra(value):
    key, sep, raw = value.partition("=")
    if not sep or not key.strip():
        raise ValueError(f"expected KEY=JSON, got {value!r}")
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = raw
    return key.strip(), parsed


def extra_mapping(items):
    result = {}
    for item in items or ():
        key, parsed = parse_extra(item) if isinstance(item, str) else item
        result[key] = parsed
    return result


class ConfigGuard:
    """Remember the original config bytes and restore them on exit."""

    def __init__(self, path):
        self.path = Path(path)
        self.existed = False
        self.original = b""

    def __enter__(self):
        self.existed = self.path.exists()
        self.original = self.path.read_bytes() if self.existed else b""
        return self

    def _replace(self, data):
        temporary = self.path.with_name(f".{self.path.name}.x1-repeat.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, self.path)

    def write(self, config):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._replace(dump_config(config).encode("utf-8"))

    def restore(self):
        if self.existed:
            self._replace(self.original)
        else:
            self.path.unlink(missing_ok=True)

    def __exit__(self, exc_type, exc, traceback):
        self.restore()
        return False


class MarkerGuard:
    """Create opt-in markers, remembering which ones this run actually created."""

    def __init__(self, paths):
        self.paths = tuple(Path(path) for path in paths)
        self.created = []

    def __enter__(self):
        try:
            for path in self.paths:
                if path.exists():
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                self.created.append(path)
        except BaseException:
            self.cleanup()
            raise
        return self

    def cleanup(self):
        for path in reversed(self.created):
            path.unlink(missing_ok=True)
        self.created.clear()

    def __exit__(self, exc_type, exc, traceback):
        self.cleanup()
        return False


def result_text(result):
    return f"{getattr(result, 'stdout', '') or ''}\n{getattr(result, 'stderr', '') or ''}"


def ready(result):
    if getattr(result, "returncode", 1) != 0:
        return False
    stdout = getattr(result, "stdout", "") or ""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    return bool(lines) and lines[-1].startswith("ready")


def lock_conflict(result):
    text = result_text(result).lower()
    return any(word in text for word in LOCK_WORDS)


class Driver:
    """Side-effect-free plan plus a driver with injectable runner/clock/lock."""

    def __init__(
        self, *, tags, sites, extra=None, image=None, tag_prefix="", fw_legs=True,
        config_path=CONFIG, collect=COLLECT, root=ROOT, lanes=DEFAULT_LANES,
        rounds=6, poll_seconds=60.0, lock_timeout=0.0, unit_timeout=0.0,
        markers=DEFAULT_MARKERS, lock=LOCK, python=None, dry_run=False,
        run=None, sleep=time.sleep, monotonic=time.monotonic,
        lock_busy=None, unit_active=None, log=None, command_timeout=300.0,
    ):
        if rounds < 1:
            raise ValueError("rounds must be at least 1")
        self.tags = tuple(tags)
        self.sites = tuple(sites)
        self.extra = dict(extra or {})
        self.image = image
        self.tag_prefix = tag_prefix
        self.fw_legs = fw_legs
        self.config_path = Path(config_path)
        self.collect = Path(collect)
        self.root = Path(root)
        self.lanes = tuple(lanes)
        self.rounds = rounds
        self.poll_seconds = poll_seconds
        self.lock_timeout = lock_timeout
        self.unit_timeout = unit_timeout
        self.markers = tuple(Path(path) for path in markers)
        self.lock = Path(lock)
        self.python = python or sys.executable
        self.dry_run = dry_run
        self.command_timeout = command_timeout
        self.run = run or self._run_command
        self.sleep = sleep
        self.monotonic = monotonic
        self.lock_busy = lock_busy or self._lock_busy
        self.unit_active = unit_active or self._unit_active
        self.log = log or (lambda message: print(message, flush=True))

    def _environment(self):
        environment = dict(os.environ)
        search = os.pathsep.join((str(self.root), str(self.root / "src")))
        environment.setdefault("PYTHONPATH", search)
        environment.setdefault("PYTHONUTF8", "1")
        temporary = self.root / ".tmp"
        temporary.mkdir(parents=True, exist_ok=True)
        environment.setdefault("TMPDIR", str(temporary))
        return environment

    def _run_command(self, args):
        return subprocess.run(
            args, cwd=self.root, text=True, capture_output=True, timeout=self.command_timeout,
            check=False, env=self._environment(),
        )

    def _lock_busy(self):
        if held(self.lock):
            return True
        return any(held(sidecar(self.lock, f"flight-{lane}")) for lane in self.lanes)

    def _unit_active(self, lane):
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", UNIT.format(lane=lane)],
                cwd=self.root, capture_output=True, text=True,
                timeout=self.command_timeout, check=False,
            )
        except OSError:
            return False
        return result.returncode == 0

    def collect_args(self, lane, action):
        return [str(self.python), "-B", str(self.collect), "--gimbal-eval", "--lane", lane, action]

    def config_for(self, tag):
        return build_config(tag, self.sites, image=self.image, fw_legs=self.fw_legs,
                            extra=self.extra, tag_prefix=self.tag_prefix)

    def preflight(self, lane):
        return self.run(self.collect_args(lane, "preflight"))

    def launch(self, lane):
        return self.run(self.collect_args(lane, "launch"))

    def wait_for_lock(self):
        if not self.lock_busy():
            return 0.0
        started = self.monotonic()
        self.log(f"X1 lock busy, waiting for {self.lock}")
        while self.lock_busy():
            waited = self.monotonic() - started
            if self.lock_timeout and waited >= self.lock_timeout:
                raise LockTimeout(f"timed out after {self.lock_timeout:g}s waiting for {self.lock}")
            self.sleep(self.poll_seconds)
        return self.monotonic() - started

    def wait_units_idle(self):
        started = self.monotonic()
        active = [lane for lane in self.lanes if self.unit_active(lane)]
        if not active:
            return
        self.log(f"waiting for lane unit(s): {', '.join(active)}")
        while True:
            active = [lane for lane in self.lanes if self.unit_active(lane)]
            if not active:
                return
            if self.unit_timeout and self.monotonic() - started >= self.unit_timeout:
                raise UnitTimeout(
                    f"timed out after {self.unit_timeout:g}s waiting for {', '.join(active)}")
            self.sleep(self.poll_seconds)

    def run_tag(self, config_guard, tag):
        config = self.config_for(tag)
        # Do not replace the shared eval config while another X1 flight owns a
        # lane; wait first so queued tags cannot change a running job's inputs.
        self.wait_for_lock()
        config_guard.write(config)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        self.log(f"{stamp} tag {config['tag']} start")
        for round_no in range(1, self.rounds + 1):
            launched = False
            for lane in self.lanes:
                if self.unit_active(lane):
                    launched = True
                    continue
                check = self.preflight(lane)
                if not ready(check):
                    if lock_conflict(check):
                        self.wait_for_lock()
                        launched = True
                    else:
                        self.log(f"lane {lane} round {round_no}: "
                                 f"not ready ({self._summary(check)})")
                    continue
                launched = True
                self.wait_for_lock()
                result = self.launch(lane)
                if lock_conflict(result):
                    self.log(f"lane {lane} round {round_no}: lock raced, retrying after wait")
                    self.wait_for_lock()
                    result = self.launch(lane)
                if getattr(result, "returncode", 1) != 0:
                    self.log(f"lane {lane} round {round_no}: "
                             f"launch failed ({self._summary(result)})")
                else:
                    self.log(f"lane {lane} round {round_no}: launched")
            self.wait_units_idle()
            if not launched:
                self.log(f"{datetime.now().astimezone().isoformat(timespec='seconds')} "
                         f"tag {config['tag']} done")
                return True
        self.log(f"{datetime.now().astimezone().isoformat(timespec='seconds')} "
                 f"tag {config['tag']} still pending after {self.rounds} round(s)")
        return False

    @staticmethod
    def _summary(result):
        lines = [line.strip() for line in result_text(result).splitlines() if line.strip()]
        return lines[-1] if lines else f"exit {getattr(result, 'returncode', 'unknown')}"

    def plan_lines(self):
        lines = []
        for tag in self.tags:
            config = self.config_for(tag)
            lines.append(f"tag {tag} -> config tag {config['tag']}")
            lines.append(f"  write {self.config_path}")
            lines.append(f"  config: {dump_config(config).strip()}")
            if self.markers:
                lines.append("  marker(s): " + ", ".join(str(path) for path in self.markers)
                             + " (only markers created by this run are removed)")
            for lane in self.lanes:
                lines.append(f"  lane {lane}: {shlex.join(self.collect_args(lane, 'preflight'))}")
                lines.append(f"  lane {lane}: when ready -> wait for {self.lock} -> "
                             f"{shlex.join(self.collect_args(lane, 'launch'))}")
            lines.append(f"  retry pending lanes for at most {self.rounds} round(s), "
                         f"poll {self.poll_seconds:g}s")
        lines.append(f"restore {self.config_path}; remove markers created by this run")
        return lines

    def execute(self):
        if self.dry_run:
            for line in self.plan_lines():
                self.log(line)
            return 0
        settled = True
        with ConfigGuard(self.config_path) as config_guard, MarkerGuard(self.markers) as markers:
            if markers.created:
                created = ", ".join(str(path) for path in markers.created)
                self.log(f"GPU opt-in marker(s): {created}")
            for tag in self.tags:
                if not self.run_tag(config_guard, tag):
                    settled = False
        return 0 if settled else 1


def install_signal_handlers():
    """Turn SIGTERM/SIGHUP into SystemExit so the config/marker guards run."""

    def _exit(signum, _frame):
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _exit)
        except (OSError, ValueError):
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tags", nargs="+", required=True, help="fresh tag suffixes, one run each")
    parser.add_argument("--sites", nargs="+", required=True,
                        help="condition labels for the config's only list")
    parser.add_argument("--extra", action="append", default=[], metavar="KEY=JSON",
                        help="extra config keys (e.g. fw_min_leg_m=50); repeatable")
    parser.add_argument("--fw-min-leg-m", type=float, help="shortcut for --extra fw_min_leg_m=...")
    parser.add_argument("--tag-prefix", default="",
                        help="prefix added to every tag, e.g. 'fw-legs-'")
    parser.add_argument("--image", help="optional eval image override")
    parser.add_argument("--no-fw-legs", action="store_true", help="write fw_legs=false")
    parser.add_argument("--config", type=Path, default=CONFIG,
                        help="eval config to overwrite temporarily")
    parser.add_argument("--collect", type=Path, default=COLLECT)
    parser.add_argument("--lane", dest="lanes", action="append", choices=("a", "b"),
                        help="lane to use (default: a and b); repeatable")
    parser.add_argument("--rounds", type=int, default=6, help="maximum retry rounds per tag")
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument("--lock", type=Path, default=LOCK)
    parser.add_argument("--lock-timeout", type=float, default=0.0,
                        help="seconds to wait for the lock; 0 waits indefinitely")
    parser.add_argument("--unit-timeout", type=float, default=0.0,
                        help="seconds to wait for lane units; 0 waits indefinitely")
    parser.add_argument("--marker", dest="markers", action="append", type=Path,
                        help="GPU opt-in marker (default: the three /data/scratch markers)")
    parser.add_argument("--no-markers", action="store_true",
                        help="do not create GPU opt-in markers")
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter for x1_collect_next.py")
    parser.add_argument("--dry-run", action="store_true", help="print the planned steps and exit")
    args = parser.parse_args(argv)

    try:
        extra = extra_mapping(args.extra)
    except ValueError as exc:
        parser.error(str(exc))
    if args.fw_min_leg_m is not None:
        extra["fw_min_leg_m"] = args.fw_min_leg_m
    markers = () if args.no_markers else tuple(args.markers or DEFAULT_MARKERS)
    driver = Driver(
        tags=args.tags, sites=args.sites, extra=extra, image=args.image,
        tag_prefix=args.tag_prefix, fw_legs=not args.no_fw_legs,
        config_path=args.config, collect=args.collect, lanes=tuple(args.lanes or DEFAULT_LANES),
        rounds=args.rounds, poll_seconds=args.poll_seconds, lock=args.lock,
        lock_timeout=args.lock_timeout, unit_timeout=args.unit_timeout,
        markers=markers, python=args.python, dry_run=args.dry_run,
    )
    if not args.dry_run:
        install_signal_handlers()
    try:
        return driver.execute()
    except (LockTimeout, UnitTimeout, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"x1-repeat-eval: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
