# SIM mission suite v2

`scripts/run_mission_suite.py` defaults to `configs/onboard/mission-suite-v2.json`.
It keeps the eight v1 text missions and adds six precompiled mission packages:
comms-free count, patrol silence timeout, high-risk monitor link loss, operator
abort, pause/resume, and time-reserve escalation. Explicit `--suite` selection
continues to support v1 and the model-specific v1 configurations.

Validate and generate per-mission onboard configurations without starting a
simulator, model server, or container:

```powershell
python scripts/run_mission_suite.py --dry-run --out artifacts/mission-suite-v2-dry-run
```

The onboard SIM policy accepts `sim_ground_events`, an ordered list of
`{"t_s": 120, "event": "link_lost"}` objects. Supported events are `link_lost`,
`link_restored`, `abort`, `pause`, and `resume`. Times are seconds since the first
SIM camera frame. Frame timestamps advance the supervisor clock, so model
startup and inference latency do not consume the mission budget. Timeline
delivery occurs before perception at the next policy call and preserves the
scheduled times and ordering of events crossed between frames.

A nonempty timeline models a healthy ground link before `link_lost`; loss stops
synthetic heartbeats. The package's `heartbeat_timeout_s` still applies, and a
`continue_then_return` deadline measures silence from the last heartbeat. An
empty timeline emits **no ground events or heartbeats**. Omitting the key retains
the existing host-clock and optional transport behavior. This injection is
specific to `scripts/onboard_policy.py`, the SIM teacher policy; the collector
does not inject events.

Package episodes retain durable evidence under
`<campaign>/<world>/<episode-id>-onboard-frames/mission_log/mission.jsonl` and
`summary.json`. The scorer extracts supervisor outcomes from that log, including
the final decision and reason, logged decisions, actual return-home execution,
original mission completion, and abort hover before recovery. Suite JSON reports
include this evidence for missions with `expect.supervisor`; missing or invalid
evidence fails the expectations. A supervisor expectation failure makes the suite
command exit unsuccessfully. Legacy v1 rows and exit behavior stay unchanged.

Dry-run checks package plans and timeline syntax. Actual flight outcomes and
model quality require a subsequent authorized SIM regression run.
