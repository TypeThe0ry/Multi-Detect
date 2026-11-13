# Offline test record — 2026-11-11

Candidate base: `59c4dcbe7be045d51964665c143365b9e543f8ae`. Changes remain uncommitted and isolated; no GPU, SITL, or active source was used or modified.

- `PYTHONPATH=src pytest tests/test_onboard_loop.py tests/test_mission_executor.py tests/test_mission_executor_remaining_gaps.py tests/test_onboard_ground_reid.py -q`: 284 passed, 1 skipped. This run preceded the addition of the two focused tests below.
- `PYTHONPATH=src pytest tests/test_mission_executor.py -q -k 'fixed_wing_count_does_not_enable or deferred_count_place or non_count_continuing'`: 4 passed.
- `PYTHONPATH=src pytest tests/test_onboard_loop.py -q -k 'count_guard_keeps_valid or count_confirmed_focus_repeatedly_lost'`: 2 passed.
- `git diff --check`: passed at the prior checkpoint; only the focused independent-confirmation test was added afterward.

The lost/reacquired sequence is a synthetic reproduction informed by the recorded flight, not a full recorded detector/action replay or closed-loop flight. Place-level suppression depends on ground and appearance association and can still miss a new identity if those are unavailable or inconsistent. Independent review is pending; do not connect to the active queue on this evidence.

## Independent review: unresolved P1

The candidate is not ready for integration. The same-place suppression can be bypassed after a new track is created if ground or appearance evidence is missing or inconsistent. Conversely, its object-sized radius and compatible appearance can suppress a distinct, nearby object that has not been co-visible. Missing features must not be treated as proof of identity. The existing tests do not establish correctness in either case.

Proposed direction for a revised candidate: distinguish strong identity evidence, proven distinctness, and unresolved association. Keep strongly identified tracks on a bounded per-track retry; never merge or count an ambiguous candidate on proximity alone. For unresolved association use a separately bounded, task-wide budget for lost-positive approach attempts, and when that budget is reached stop further ambiguous approaches while retaining every unresolved observation and reporting a lower bound. This can sacrifice recall and requires an explicit policy decision and tests; it is a design proposal, not an implemented or validated repair. Allowing bounded search to proceed for distinct candidates requires a precise definition that does not assume non-co-visible nearby tracks are identical.

Required tests: missing appearance, missing ground, inconsistent appearance and expired re-identification; two similar nearby non-co-visible objects; explicit co-visible distinct objects; true same-track recurrence; zero false counts; pending observations and lower-bound status through coverage and exhausted revisit; valid independent confirmation; count versus non-count and fixed-wing isolation. Test the actual log-derived sequence and exercise a closed-loop flight only after independent review and suitable scheduling.

## Subsequent isolated revision

Independent-review counterexamples for a co-visible neighbour resolving a deferred target, switching to another visible focus without recording loss, and an unlocated positive disappearing from the lower-bound decision all failed before repair. The candidate now preserves deferred evidence when another nearby track is counted, records a lost focus when switching to a visible candidate, and counts unlocated positive observations as unresolved for the final lower-bound decision. Unlocated observations are not submitted to coordinate-dependent revisit planning.

Place/appearance-based suppression of new IDs has been removed to avoid suppressing an unproven distinct nearby object. Consequently a recurring target with a new ID can still evade the per-track guard; there is no demonstrated safe, bounded solution for ambiguous new IDs here. This remains an in-scope blocker, and the candidate must not be queued or presented as complete.

Latest full command: `PYTHONPATH=src pytest tests/test_onboard_loop.py tests/test_mission_executor.py tests/test_mission_executor_remaining_gaps.py tests/test_onboard_ground_reid.py -q -rA` — 289 passed, 1 skipped (shared read-only benchmark artifacts unavailable). The missing-ground check executes the executor coverage-completion path; it does not establish actual-flight behavior. No GPU or SITL was used.

## Ambiguous new-id budget (offline, 2026-11-11)

New ids that cannot be shown distinct from an earlier lost positive (no ground, expired re-identification, inconsistent appearance, or merely nearby and never co-visible) now share one task-wide budget (`LoopConfig.count_lost_cycle_ambiguous_budget`, default 1, only with `count_lost_cycle_guard`). Once spent they are not approached; their positive observations stay unresolved (lower bound). Proven distinct = co-visible separate boxes (existing `co_visibility_proves_distinct`) or both positions known and farther apart than the same-object place radius. This sacrifices recall for ambiguous nearby objects and needs an explicit policy decision; it is not validated in flight.

Command (`tests/test_mission_executor.py tests/test_onboard_loop.py tests/test_mission_executor_remaining_gaps.py tests/test_onboard_ground_reid.py`): 297 passed, 1 skipped (same benchmark-artifact skip).
