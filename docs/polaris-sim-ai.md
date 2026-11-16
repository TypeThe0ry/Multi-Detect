# Polaris Sim AI Gateway

`polaris-sim-ai` is the read-only perception and target-query service for the Polaris simulator. It runs beside the Sim Gateway on the workstation and uses the Sirius runtime when the configured Sirius source is available. The HTTP listener defaults to `0.0.0.0:8790`; every request is rejected unless its peer address is loopback or a private/link-local IPv4 or IPv6 address. Set `POLARIS_SIM_TOKEN` to require a bearer token on all POST endpoints.

## Start

```bash
python scripts/polaris_sim/ai.py --config configs/polaris_sim/ai.json
```

The installed entry point is `polaris-sim-ai`. The config points at the requested Sirius worktree source path; `SIRIUS_RUNTIME_SRC` overrides it. A missing runtime leaves query processing unavailable and fail-closed. All Sirius component flags are false by default. No model is loaded and no GPU is claimed. Existing `jsonschema` validates requests using bundled snapshots of Spica's 1.0 schemas. The snapshots are copied without edits from Spica worktree commit `3eb63f02d1d461cc69467b74962ab574bc66459c`; the schema owner remains Spica.

Frame capture uses the CPU `ffmpeg` executable to grab one JPEG from `rtsp://127.0.0.1:8554/v<index>/cam`, where slot 5/6/7 maps to index 0/1/2. If that fails, `inputs.camera.fallback_video` can name a local replay video; successive calls advance through the file at `fallback_fps` and loop by default. OpenCV is used opportunistically to decode the JPEG; without it, the frame bytes are passed to the adapter. Telemetry is sampled through a read-only `pymavlink` connection to `tcp:127.0.0.1:5770` and keyed by sysid 1/2/3. Install the existing `pixhawk` extra for pymavlink; do not send commands on this connection.

## Query API

API version is `1.0`. `POST /v1/query_target` accepts a canonical Spica request envelope, a bare TargetQuery, or a convenience natural-language wrapper. The vehicle is selected with `X-Polaris-Slot: 5|6|7`; it is intentionally outside the strict Spica request schema.

Canonical request:

```json
{
  "request_id": "q-123",
  "query": {
    "schema_version": "1.0",
    "categories": ["vehicle"],
    "attributes": [{"name": "color", "op": "eq", "value": "red"}],
    "outputs": ["wgs84", "covariance", "confidence", "track_id"],
    "watch": false
  }
}
```

Prompt convenience form:

```json
{"request_id":"q-124","prompt":"find the red vehicle","outputs":["wgs84","confidence"],"watch":false}
```

The response wraps a schema-valid Spica `query_target` envelope in the service API envelope. Runtime degradation is outside the Spica envelope because its schema rejects unknown fields.

```json
{
  "api_version": "1.0",
  "result": {
    "id": "q-123",
    "name": "query_target",
    "status": "unavailable",
    "latency_ms": 3.4,
    "data": null,
    "warnings": ["sirius runtime is disabled"]
  },
  "runtime": {"status":"disabled","degradation":"abstain","reason":"sirius runtime is disabled"},
  "input": {"vehicle_slot":5,"frame_id":"sim-5-a1b2c3d4e5f6","frame_source":"rtsp","telemetry_available":true,"telemetry_age_ms":42.0}
}
```

The wrapper's `result` object is validated against Spica's `query_target.json` response definition. Its `data.results[]` entries are validated TargetResult objects. When runtime stage is `abstain`, the Spica envelope is `unavailable` with `data: null`; the service never invents a detection or coordinate. A `degraded` runtime response preserves only fields produced before the failing component. WGS84 is expressed using Spica `lat`, `lon`, and ellipsoidal `alt`; covariance is local north/east metres squared. Omitted outputs remain omitted.

| Spica field | Sirius mapping | Notes |
| --- | --- | --- |
| `schema_version` | `TargetQuery.schema_version` | Fixed to `1.0`. |
| `categories` | `TargetQuery.categories` | Open vocabulary; no category list is hard-coded here. |
| `attributes[]` | `TargetQuery.attributes` | `{name, op, value, weight?}` filters. |
| `relations[]` | `TargetQuery.relations` | Nested anchors stay Spica-validated. |
| `count_or_condition`, `temporal` | same-name query fields | Passed through without schema changes. |
| `outputs`, `watch`, `metrics`, `limits` | runtime output/watch options | Forwarded to Sirius and its registered metrics. |
| `prompt` convenience form | Sirius Altair parser input | Not a Spica schema field; output is still projected and validated as Spica 1.0. |
| result `lat`, `lon`, `alt`, `cov`, `ellipse`, `confidence`, `track_id` | Sirius detection/location/track DTOs | Only emitted when present and requested. |

## Events and Components

`GET /v1/events` streams Server-Sent Events. `GET /v1/events/ws` provides the same journal as WebSocket text frames. Each event has a monotonically increasing journal `id`, an `event` name, a wall-clock timestamp, and `data` containing request ID and vehicle slot when bound to a query. The lifecycle names are `query.parsed`, `vega.detected`, `vlm.reviewed`, `mizar.located`, `tracking.updated`, and `published`; Sirius fault/fallback events are `runtime.degraded`, `runtime.failed`, and `adapter.fallback`. `Last-Event-ID` resumes from the first retained event after that ID. The in-memory journal is bounded by `event_capacity` (default 2048); a client older than retained history can only resume from the oldest retained entry.

`GET /v1/status` reports runtime availability, enabled/available/stub flags, last stage-event latency, version label, input status, and target-push state. Latency is the elapsed time from the preceding Sirius lifecycle event to the current event, not a hardware benchmark. `POST /v1/components` accepts `{"components":{"vega":true,"mizar":true}}`; allowed names are `altair`, `vega`, `vlm`, `mizar`, `tracking`, and `publish_polaris`. Unknown names and non-boolean values return 400. All components are off initially. The default parser/detector/reviewer/locator/tracker are Sirius CPU stubs; empty stub output remains empty and is not reported as a positive detection. Enable a real adapter by configuring an importable `module:attribute` under `adapters.vega` or `adapters.mizar`. Vega receives `(query, FrameSample)`; Mizar receives `(query, detections, outputs)` and can additionally accept `context=FrameSample` and/or `telemetry=...`. Adapter imports are explicit and happen only when the service starts. The `published` lifecycle event means the result has passed through Sirius' Polaris service sink; it does not imply that a MAVLink datagram was sent.

`watch` follows Sirius' per-frame API: a caller that wants continuous updates repeats the query with the same request ID/query on new frames. Each call publishes its own ordered lifecycle events; this service does not create an unbounded background watch task.

## Tracking and Target Return

`POST /v1/track` accepts a repeated pixel observation and returns a bounded camera-body-relative gimbal suggestion. Example:

```json
{
  "track_id":"track-17",
  "target_bbox":{"x":900,"y":250,"width":60,"height":60},
  "image_size":{"width":1280,"height":720},
  "camera_fov_deg":{"horizontal":90,"vertical":60}
}
```

The response contains `suggestion.yaw_delta_deg` (positive right) and `pitch_delta_deg` (positive up), a 500 ms validity, and `advisory_only: true`, `mavlink_sent: false`, `flight_control_enabled: false`. Each correction is clamped to 15 degrees. Polaris remains responsible for MAVLink Gimbal Manager commands and operator confirmation.

Polaris currently has no confirmed HTTP query-result receiver in `custom/src`; its existing operator metadata path is signed MAVLink2 `TUNNEL` payload type `42000`, with internal message type 22 (`TargetGeolocationStatus`). Therefore target push uses that existing protocol when explicitly enabled; no custom MAVLink message ID is introduced. It requires `target_push.enabled`, `runtime.components.publish_polaris`, a private numeric IPv4 destination, matching endpoint IDs, and 32-byte hex keys in `POLARIS_SIM_MAVLINK_SIGNING_KEY` and `POLARIS_SIM_OPERATOR_HMAC_KEY`. The destination host and system/component IDs must be filled from the Polaris deployment's signed-link configuration; the checked-in config does not guess them. It emits only when the read-only position and GPS telemetry caches are each fresh (<=2 seconds), GPS fix type meets `min_gps_fix_type`, Sirius location validity is `available`, location provenance includes `gps_qualified`, and a 2x2 covariance supplies horizontal sigma. The existing type-22 payload carries WGS84 latitude/longitude and horizontal sigma only; it cannot carry altitude, full covariance, or confidence. Those remain available in the HTTP/SSE Spica result. The target frame must also pass Polaris' 1.5-second source-age validation. The publisher is disabled in the checked-in config.

## Errors and Security

Errors use `{"api_version":"1.0","error":{"code":"...","message":"..."}}`.

| HTTP | Code | Meaning |
| --- | --- | --- |
| 400 | `invalid_json`, `invalid_length`, `invalid_request`, `invalid_spica_query`, `invalid_components`, `invalid_vehicle_slot`, `invalid_event_id`, `websocket_handshake_required` | Malformed body, schema, option, slot, event cursor, or WebSocket upgrade. |
| 401 | `unauthorized` | Bearer token is required or incorrect when `POLARIS_SIM_TOKEN` is set. |
| 403 | `private_network_required` | Peer address is not loopback/private/link-local. |
| 411 / 413 / 415 | `length_required` / `body_too_large` / `content_type_required` | Request body transport constraint. |
| 503 | `busy` / `runtime_unavailable` | Query capacity exhausted or Sirius runtime unavailable for a component update. |
| 404 / 500 | `not_found` / `internal_error` | Unknown API path or unexpected request failure. |

`GET /health` is a simple service status alias. The listener defaults to the required `0.0.0.0:8790`, but non-private peers are rejected before any endpoint is served. No proxy forwarding headers are trusted. Do not expose the port outside the LAN.
