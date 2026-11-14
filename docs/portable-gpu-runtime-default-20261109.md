# Portable GPU SITL runtime selection (gz-rendering 8.3.0 candidate)

This isolated candidate selects the repaired GPU runtime by the versioned reference
`multidetect/px4-sitl-gazebo:gz-rendering-830-r1`. The image keeps the patched
PX4/gz-transport base and upgrades only the gz-rendering 8.3.0 package closure. It does not assume a
machine-specific Docker image ID is portable. No active tag, running source,
main branch or aircraft configuration is changed by preparing this branch.
Promotion still requires the original ten w0000, 21×2 and both delta flight gates;
content verification and CPU tests do not establish those flight outcomes.

The GPU default is resolved only after actual GPU/device selection. Explicit
API/CLI image, then `MULTIDETECT_SIM_IMAGE`, override that default. Original
`PINNED_IMAGE` remains bcf as the asset-source contract. CPU still uses bcf:
**CPU defaults have not received these repairs or this flight qualification.**
The default worker pool is GPU0/1 with GPU2/3 avoided. API device/pool and
GPU-enabling environment selectors cannot re-enable GPU2/3 or other devices;
invalid selections fail before Docker inspection/launch. Automatic collector defaults freeze their image and resolved GPU mode/device/pool
together at construction, in both CPU-to-GPU and GPU-to-CPU directions. Later
ambient settings cannot pair the automatic old CPU image with a GPU execution.
Explicit API/environment old-image overrides keep their deliberate A/B semantics.
The qualified launch also freezes resolved execution settings with its image ID;
an incompatible explicitly configured GPU-runtime plan fails closed.

Real-aircraft serial/parameter paths and the active r21id-r3/r24/pregate config are
unchanged. Explicit old image overrides remain available for A/B and do not
acquire candidate qualification.

## Content identity and execution boundary

`simulation/gz-transport/runtime-image-contract.json` binds the validated gz-rendering
8.3.0 image's
platform, all ordered uncompressed RootFS layer digests, and complete Docker
runtime Config, together with the PX49c2280 / transport aff44 hashes and exact
source/patch identities. These layer digests cover the full filesystem; checking
only two binaries would allow unrelated modified dependencies or scripts.
The contract deliberately contains no required local image ID. A restored image
with a different local ID but exactly the same content/config is accepted.
Different layers/config/platform, missing or ambiguous inspect output, and a
missing image are rejected. No pull, build, tag or fallback occurs automatically.

Pure API/CLI planning stays offline and labels its image identity as
`planned_reference`; it does not claim that a container ran. Actual repository
launch seams (collector, teacher session, Docker acceptance, GPU check,
crash-resume and VTOL capture) first inspect and verify the default reference,
then replace the Docker argv's tag with the observed immutable local ID. A tag
change after inspection cannot change this launch. Collector episode metadata
and raw container-started headers record this ID and the separately retained
requested reference, with a real container identity check before recording.
Attached viewer recordings keep the inspected borrowed container's actual
identity; they never inherit the new GPU qualification or stop/remove a borrowed
container. Existing viewer remains its explicit software-rendering image.

This contract is strict. Rebuilt images with matching binaries but different
RootFS layers or runtime configuration are **new candidates**, not automatically
qualified. Investigate build metadata/dependencies and retain the rejection;
never widen the contract or rewrite an old flight receipt just to accept them.
An exact-content OCI archive restore can retain the layer contract even when a
storage backend gives a different local image ID. Cross-backend acceptance has
CPU fixture coverage; an actual archive restore on another daemon/host has
**not** been performed in this change.

## Availability and reproduction

The current workstation already has the versioned reference. Read-only content
verification is available through the existing build tool:

```bash
python scripts/gz_transport_backport.py verify-runtime \
  --output /data/scratch/runtime-verification-FRESH
# After a separately controlled archive restore, verify its actual ID explicitly:
python scripts/gz_transport_backport.py verify-runtime \
  --image sha256:RESTORED_FULL_ID \
  --output /data/scratch/runtime-restored-verification-FRESH
```

Both commands only inspect the image and write a new receipt. They never load,
tag, execute a container or declare flight PASS. A missing reference explains
that the pinned runtime must first be built/imported; ordinary flight startup
must not perform that provisioning or fall back to old code.

The existing PX4/transport build scripts remain the source-rebuild route, with
fixed sources, patches, expected binary hashes, dependency checks and fresh
output tags. See the two backport documents. Transport was reproduced byte for
byte under SDK90c; PX4 remains expected9c2280. Full Docker identity is not claimed
bit-reproducible. Fresh builds must pass content verification or remain separate
new candidates requiring reviewed content and flight acceptance.

For another workstation, transfer an audited full image archive and its hash/
reference closure, import it explicitly, verify actual content, and only then
restore the versioned lookup reference without overwriting any existing target.
No archive transfer/load/tag is implemented as an automatic launch side effect.
There is no invented publicly pullable registry location for local630/90c/b5.
Current source builders still need their fixed SDK/base inputs; apt/pip lock and
fresh-public-network reconstruction are not claimed complete. Reproducible
component compilation is distinct from complete portable provisioning. If an
approved registry release is provided later, a verified real repository manifest
digest may replace local archive provisioning.

## Preserved diagnostics and qualification boundaries

The exact76736981 → bca5ba82 →6f059470 diagnostics are carried forward: bounded
head/tail GDB retention up to1MiB stdout/64KiB stderr with explicit truncation
metadata, task starttime identity and opt-in procfs mutex-owner observation.
Owner probing requires a measured layout proof for the target libc hash, uses
two20B reads (40B total), and reports unknown/error when identity, permissions or
layout cannot be proved. Its0.25s read-between deadline is not a kernel I/O hard
timeout. Observation is candidate evidence, not an atomic owner proof or a
standalone causal attribution. These diagnostics remain failure-path-only; no
45s watchdog, failure declaration or flight assertion is weakened.

Teacher inherited size/SHA guards remain exact. The six mechanical pin consumers
and old→new values are recorded in
`docs/evidence/portable-runtime-pin-review-20261109.json`. Historical receipts
were not changed and do not automatically qualify the new teacher source.

Five legacy VTOL focused cases fail `evaluator_endpoint_bound` identically when
the exact1ec4 original module is loaded in a separate CPU process. Their failures
are retained; this change neither bypasses them nor claims the entire expanded
VTOL suite is green. The original failure/contrast evidence and final CPU gate
are stored under `runs/portable-runtime-candidate-review-20261109/` outside Git.
