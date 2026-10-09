"""CPU-only official processor probe on one retained actual Gazebo frame."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]


def main():
    from scripts.run_onboard_navigation_gazebo_learned_shadow import record, write_new

    directory = ROOT / "artifacts/evaluation/onboard-navigation-live-shadow-20261008-r1/capture"
    path = directory / "rgb/gazebo-bgr24-frame-set.manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    input_path = directory / "input-report.json"
    inputs = json.loads(input_path.read_text(encoding="utf-8"))
    row = manifest["frames"][0]
    frame_path = path.parent / row["file"]
    bgr = frame_path.read_bytes()
    if len(bgr) != row["bytes"] or hashlib.sha256(bgr).hexdigest() != row["sha256"]:
        raise ValueError("Retained actual frame SHA differs")
    rgb = bytearray(bgr)
    rgb[0::3], rgb[2::3] = bgr[2::3], bgr[0::3]
    rgb = bytes(rgb)
    plan_path = ROOT / "configs/training/onboard-navigation-live-shadow-20261008-r1.json"
    if (
        record(plan_path)["sha256"]
        != "8904c9b2b14216e1befcb867098680ef2a194ecd1c11e7fc82c904dc4423d77a"
    ):
        raise ValueError("Frozen plan SHA differs")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    import torch
    from transformers import AutoProcessor

    from multidetect.autonomy import onboard_navigation_actor as actor_api
    from scripts.train_onboard_vlm_navigation_pilot import tensor_record
    from scripts.verify_onboard_navigation_actor_inputs import process_actor, validate_prefix_inputs

    torch.set_num_threads(1)
    provenance = row["provenance"]
    actor = actor_api.NavigationActorInput(
        inputs["prompt_text_original"],
        row["sequence"],
        (
            actor_api.NavigationActorFrame(
                row["sequence"],
                rgb,
                len(rgb),
                hashlib.sha256(rgb).hexdigest(),
                provenance["width"],
                provenance["height"],
                "rgb8",
            ),
        ),
    )
    if actor.identity_sha256 != inputs["prepared_requests"][0]["actor_identity_sha256"]:
        raise ValueError("Reconstructed actual Actor differs")
    processor = AutoProcessor.from_pretrained(
        plan["load_config"]["model"]["base_snapshot"], local_files_only=True
    )
    encoded = process_actor(actor, processor, actor_api)
    prefix = validate_prefix_inputs(
        encoded, processor=processor, image_count=1, torch_module=torch, tensor_record=tensor_record
    )
    value = {
        "status": "actual_CPU_processor_retained_camera_not_fresh_model_inference",
        "source": [
            record(path),
            record(input_path),
            record(frame_path),
            record(plan_path),
            record(__file__),
        ],
        "actor_identity_sha256": actor.identity_sha256,
        "actual_container_type": type(encoded).__module__ + "." + type(encoded).__qualname__,
        "actual_keys": sorted(encoded),
        "actual_prefix": prefix,
        "all_tensors_CPU": all(value.device.type == "cpu" for value in encoded.values()),
        "strict_builtin_dict_gate_would_reject": type(encoded) is not dict,
        "model_loaded": False,
        "fused_forward_performed": False,
        "training_performed": False,
        "execution_authority": False,
        "quality_claim": False,
    }
    output = ROOT / "artifacts/diagnostics/onboard-navigation-native-prefix-20261008-r1"
    output.mkdir(parents=True, exist_ok=False)
    write_new(output / "report.json", value)
    print(
        json.dumps(
            {
                "report": record(output / "report.json"),
                "container": value["actual_container_type"],
                "keys": value["actual_keys"],
                "all_tensors_CPU": value["all_tensors_CPU"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
