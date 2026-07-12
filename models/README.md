# Model artifact policy

This directory is for model manifests and local, ignored runtime artifacts.
Model weights and engines are not source code and must not be committed.

The repository ignore rules cover `models/*.pt`, `models/*.weights`,
`models/*.onnx`, and `models/*.engine`. Store each artifact's digest,
provenance, conversion environment, validation evidence, and approval state in
a manifest based on [`manifest.example.json`](manifest.example.json).

## Runtime model boundary

A model is a perception component only. Its adapter may create detection
candidates, but it must not authorize or initiate a payload release.

The adapter contract is:

```text
input:
  one RGB frame with an explicit frame ID and capture timestamp

output per detection:
  class name
  confidence in [0, 1]
  normalized XYXY box in [0, 1]
  sensor kind
  immutable model version/artifact digest
```

For the pinned legacy YOLOv5 baseline, the decoder receives raw model output,
performs confidence filtering and class-aware NMS, scales boxes to source-image
pixels, and finally normalizes them to Multi-Detect's `BoundingBox` contract.
The source class mapping is `0=fire`, `1=smoke`; the adapter maps `fire` to the
project's canonical `flame` label and leaves `smoke` unchanged.

## Prohibited direct checkpoint loading

The legacy `best.pt` is a pickle-based full-model checkpoint. Its upstream
loader calls `torch.load`, which can execute code during deserialization.
Direct loading is prohibited in:

- Multi-Detect runtime code
- flight hardware
- developer workstations
- normal CI jobs
- any environment containing credentials, signing keys, source tokens, or
  access to deployment systems

The source checkpoint may only be opened in the quarantined export workflow
below after its byte size and SHA-256 have been verified.

The byte-only verification command never imports PyTorch or deserializes the checkpoint:

```powershell
multi-detect legacy-checkpoint-verify C:\isolated-staging\best.pt
```

It returns success only when both the audited size and SHA-256 match, while still reporting `safe_to_run_directly=false` and `requires_isolated_export=true`.

## Quarantined export workflow

1. **Approve an evaluation ticket.** Record the intended non-release use,
   upstream repository, pinned commit, source path, expected byte size, and
   expected SHA-256.
2. **Retrieve into disposable staging.** Use an ephemeral, unprivileged
   environment with no mounted workspace, SSH agent, cloud credentials, Docker
   socket, device access, or access to flight/control networks.
3. **Verify before deserialization.** For the audited legacy checkpoint, require
   exactly `14,758,954` bytes and SHA-256
   `d1eae6859229ac1f5699c60f9445fa054dafc6a2cc59f00fc30ea6379dc3247e`.
   Abort on any mismatch.
4. **Disconnect the sandbox.** Remove network access before invoking any Python
   checkpoint loader. Preserve only an append-only export log outside the
   sandbox.
5. **Load with pinned legacy code and dependencies.** This is the only stage in
   which pickle deserialization is allowed. Treat the entire sandbox as
   compromised after the load.
6. **Export a non-executable tensor graph.** Prefer ONNX with fixed input/output
   names. Record opset, input shape, preprocessing, class order, whether NMS is
   embedded, and every tool version. The old upstream export does not embed a
   trustworthy mission interface; postprocessing remains explicit.
7. **Destroy the loader environment.** Do not promote the original `.pt`, its
   Python environment, or cached modules. Retain only the candidate ONNX,
   conversion log, manifest, and hashes.
8. **Validate the candidate.** Compare the exported model with the quarantined
   baseline on a fixed golden set. Check class IDs, confidence deltas, box IoU,
   empty frames, multiple detections, aspect-ratio changes, and NMS behavior.
9. **Build target engines separately.** Build TensorRT engines on the exact
   target JetPack/TensorRT architecture. Engines are target-specific and must
   have their own SHA-256 and compatibility record.
10. **Require governance approval.** A converted model remains quarantined
    until software-license, dataset-rights, model-quality, security, and safety
    reviews are all complete.

## Manifest requirements

Every candidate model must record at least:

- explicit `model_role`: `fire_candidate` or `safety_object_evidence`
- immutable source repository, commit, path, size, and SHA-256
- source serialization format and isolation status
- class mapping and preprocessing
- native output and adapter output contracts
- exporter, framework, ONNX opset, and target runtime versions
- derived artifact SHA-256
- golden-set comparison results and deployment-domain validation metrics
- software-license and dataset-rights decisions
- production approval state and named reviewer/date fields

`minimum_confidence` in mission configuration is a policy threshold, not proof
that model confidence is calibrated. Calibration evidence must be collected for
each camera, altitude range, weather/lighting domain, and deployed artifact.

After creating the local ONNX and replacing every placeholder in the manifest, bind the two with:

For a new operator-supplied ONNX with known provenance, a quarantined starter manifest can be created without hand-editing the digest:

```powershell
multi-detect model-manifest-init --onnx-model models/fire-smoke-nms.onnx --out models/fire-smoke-nms.manifest.json --model-id fire-smoke-candidate --model-version candidate-v1 --source-description "REPLACE WITH TRAINING AND EXPORT PROVENANCE" --class-names fire,smoke --output-coordinates normalized_xyxy
```

An independently governed person/firefighter model must use a different role; class names alone do
not make a model valid safety evidence:

```powershell
multi-detect model-manifest-init --onnx-model models/person-safety-nms.onnx --out models/person-safety-nms.manifest.json --model-id person-safety-candidate --model-version candidate-v1 --source-description "REPLACE WITH TRAINING AND EXPORT PROVENANCE" --model-role safety_object_evidence --class-names person,firefighter --output-coordinates normalized_xyxy
multi-detect model-check --onnx-model models/person-safety-nms.onnx --model-manifest models/person-safety-nms.manifest.json --model-role safety_object_evidence --class-names person,firefighter --output-coordinates normalized_xyxy
```

The safety-object model still cannot authorize deployment or directly declare person clearance. It
only supplies governed evidence to the independent fail-closed rules engine.

The initializer always writes `status=quarantined`, `production_approved=false`, and empty validation metrics. It does not grant approval. Then bind and execute the runtime contract check with:

```powershell
multi-detect model-check --onnx-model models/fire-smoke-nms.onnx --model-manifest models/fire-smoke-nms.manifest.json --class-names fire,smoke
```

Use `--require-production-approved` for a production gate. `live-camera` accepts the corresponding `--model-manifest` and `--require-production-approved-models` flags. These checks verify identity and declared governance; they do not replace accuracy validation.

The manifest `output.adapter_contract.box_format` is also binding. It must be either `normalized_xyxy` with `box_range: [0.0, 1.0]` or `letterbox_xyxy_px`, and it must match the runtime `--output-coordinates` option. A mismatch is rejected because it would silently place boxes at incorrect image locations.

## Legacy baseline reference

See [`third_party/fire_smoke_legacy/README.md`](../third_party/fire_smoke_legacy/README.md)
for upstream interfaces and supply-chain findings, and
[`docs/upstream-baseline.md`](../docs/upstream-baseline.md) for the architectural
gap analysis.
