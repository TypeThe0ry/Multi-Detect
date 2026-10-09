"""Host lifecycle for a read-only clock sidecar in an already-owned container."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import time
from pathlib import Path

from multidetect.clock import HostClockStamp
from scripts.clock_offset_probe import describe_probes, host_clock_binding, sample_probes
from scripts.gazebo_clock_capture import describe_clock_progress


def wait_clock_publisher(docker, container_id, timeout=60):
    """Do not advertise a subscriber before PX4 has launched Gazebo.

    The pinned PX4 startup uses topic existence as its running-world test.
    Topic introspection is read-only and does not create the clock topic.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [docker, "exec", container_id, "gz", "topic", "-i", "-t", "/world/default/clock"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        publishers = result.stdout.split("Subscribers", 1)[0]
        if (
            result.returncode == 0
            and "Publishers [" in publishers
            and "gz.msgs.Clock" in publishers
        ):
            return
        time.sleep(0.2)
    raise ValueError("Gazebo clock publisher not ready; no subscriber started")


class MissionClockCapture:
    def __init__(self, docker, container_id, directory, *, clock=None):
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise ValueError("exact container ID required")
        self.clock = clock
        self.now_ns, self.host_clock_identity = host_clock_binding(clock)
        wait_clock_publisher(docker, container_id)
        self.docker = docker
        self.start_probes = sample_probes(
            docker, container_id, **({"clock": clock} if clock else {})
        )
        self.container_id = container_id
        self.holdout_probes = []
        self.holdout_attempted = False
        self.holdout_path = Path(directory) / "host-container-holdout.json"
        self.path = Path(directory) / "gazebo-clock.jsonl"
        self.stderr_path = Path(directory) / "gazebo-clock.stderr.log"
        self.rows = []
        self.error = None
        self.condition = threading.Condition()
        self.process = None
        self.reader = None
        self.output = self.path.open("x", encoding="utf-8")
        try:
            self.stderr = self.stderr_path.open("x", encoding="utf-8")
            self.process = subprocess.Popen(
                [
                    docker,
                    "exec",
                    "-i",
                    container_id,
                    "python3",
                    "/workspace/scripts/gazebo_clock_capture.py",
                    "--world",
                    "default",
                    "--samples",
                    "10000",
                    "--timeout-seconds",
                    "1800",
                    "--min-interval-seconds",
                    "0.1",
                    "--stop-on-stdin",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.stderr,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.reader.start()
        except BaseException:
            self.output.close()
            if hasattr(self, "stderr"):
                self.stderr.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                received = self.now_ns()
                row = json.loads(line)
                with self.condition:
                    if len(self.rows) >= 10000:
                        raise ValueError("clock sample cap exceeded")
                    self.rows.append(row)
                    self.output.write(
                        json.dumps(
                            {
                                "host_receive_monotonic_ns": received,
                                "clock": row,
                                **(
                                    {"host_clock_identity": self.host_clock_identity}
                                    if self.host_clock_identity
                                    else {}
                                ),
                            }
                        )
                        + "\n"
                    )
                    self.output.flush()
                    self.last_received_ns = received
                    self.condition.notify_all()
        except BaseException as exc:
            self.error = str(exc)
        finally:
            with self.condition:
                self.condition.notify_all()

    def wait_after(self, host_ns, timeout=20):
        if self.host_clock_identity:
            if not isinstance(host_ns, HostClockStamp) or (
                host_ns.source != self.host_clock_identity["source"]
                or host_ns.session_id != self.host_clock_identity["session_id"]
            ):
                raise ValueError("capture threshold requires matching host clock stamp")
            host_ns = host_ns.value_ns
        deadline = time.monotonic() + timeout
        with self.condition:
            while getattr(self, "last_received_ns", -1) <= host_ns:
                if self.error or self.process.poll() is not None:
                    raise ValueError(f"clock capture stopped early: {self.error}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("clock sample readiness timed out")
                self.condition.wait(min(remaining, 0.2))

    def sample_holdout(self):
        """One bounded parent-side batch, excluded from endpoint fitting.

        Call after the sensor-complete marker, before waiting for the Mission
        child. This does not run in its telemetry loop. Actual Mission-window
        membership still needs timestamp validation in the resulting evidence.
        """
        if self.holdout_attempted:
            raise ValueError("holdout probe batch already attempted")
        self.holdout_attempted = True
        probes = sample_probes(
            self.docker, self.container_id, **({"clock": self.clock} if self.clock else {})
        )
        describe_probes(self.start_probes + probes)
        # Persist before waiting for Mission completion: a failed flight must
        # not erase diagnostic probes that were actually collected.
        with self.holdout_path.open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "schema_version": 1,
                    "container_id": self.container_id,
                    "host_clock_identity": self.host_clock_identity,
                    "start_probes": self.start_probes,
                    "holdout_probes": probes,
                    "used_for_endpoint_fit": False,
                    "mission_window_membership_verified": False,
                },
                stream,
                indent=2,
                allow_nan=False,
            )
            stream.write("\n")
        self.holdout_probes = probes

    def finish(self):
        if self.holdout_attempted and not self.holdout_probes:
            raise ValueError("holdout probe attempt failed")
        probes = self.start_probes + sample_probes(
            self.docker, self.container_id, **({"clock": self.clock} if self.clock else {})
        )
        offset = describe_probes(probes)
        if self.holdout_probes:
            describe_probes(
                self.start_probes + self.holdout_probes + probes[len(self.start_probes) :]
            )
        self.process.stdin.write("STOP\n")
        self.process.stdin.flush()
        self.process.stdin.close()
        code = self.process.wait(timeout=10)
        self.reader.join(timeout=5)
        if self.reader.is_alive() or self.error or code != 0:
            raise ValueError(f"clock capture unsuccessful: {code}, {self.error}")
        self.output.close()
        self.stderr.close()
        summary = describe_clock_progress(self.rows)
        return {
            "container_id": self.container_id,
            "summary": summary,
            "path": str(self.path),
            "sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
            "bytes": self.path.stat().st_size,
            "exit_code": code,
            "host_container_probes": probes,
            "host_container_offset": offset,
            **(
                {
                    "host_container_holdout": {
                        "phase": "after_sensor_complete_before_mission_wait",
                        "probes": self.holdout_probes,
                        "used_for_endpoint_fit": False,
                        "mission_window_membership_verified": False,
                    }
                }
                if self.holdout_probes
                else {}
            ),
            **(
                {"host_clock_identity": self.host_clock_identity}
                if self.host_clock_identity
                else {}
            ),
            "host_px4_alignment_claimed": False,
        }

    def cleanup(self):
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.stdin.write("STOP\n")
                self.process.stdin.flush()
                self.process.wait(timeout=5)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        if self.reader is not None:
            self.reader.join(timeout=5)
        self.output.close()
        self.stderr.close()
