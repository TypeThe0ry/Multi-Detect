# MVP safety case

This prototype demonstrates software separation and fail-closed behavior for deployment of
registered, non-hazardous mission payloads. It is not evidence that an aircraft or payload is
safe to operate.

## Enforced invariants

1. Human authorization is mandatory and cannot be disabled in mission configuration.
2. Authorization is short-lived, single-use and bound to mission, target revision, bay, scene
   digest and ruleset version.
3. Any unknown, missing or stale safety input denies deployment.
4. A safety recheck is required after authorization and before the simulated effect.
5. At most one bay may be armed, requested or awaiting confirmation.
6. A request is idempotent by `release_id`; it is never automatically retried after uncertainty.
7. `RELEASED` is not success. Success requires matching execution feedback and an independent
   confirmation source.
8. Remaining inventory is derived only from `RELEASE_CONFIRMED` slots.
9. Perception, tracking and ranking have no payload-controller reference.
10. Fault and terminal mission phases cannot create a new release transaction.
11. Live observations keep an authorization bound to the newest snapshot only while target
    continuity and every rule verdict remain safety-equivalent. A semantic change invalidates the
    challenge; an armed simulated bay is relocked before any new challenge can be created.
12. A recently served scene-local target region is suppressed for the configured cooldown even
    when the short-lived tracker identity is rebuilt.

## Before any field integration

- Replace demo detections with calibrated models covering both target and safety-object classes.
- Validate RGB/thermal time synchronization and spatial registration.
- Establish geofence, altitude and platform-envelope sources with freshness and integrity checks.
- Add persistent event/outbox storage and test power-loss recovery at every transition.
- Define authenticated, acknowledged and versioned device protocols with physical interlocks.
- Verify release confirmation using independent hardware evidence; visual evidence alone is
  insufficient.
- Run scenario replay, software-in-the-loop, hardware-in-the-loop, environmental and human-factor
  tests under the responsible aviation and emergency-response authority.
- Complete model, dataset and third-party license provenance review.
