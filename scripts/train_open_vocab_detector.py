#!/usr/bin/env python3
"""Fine-tune an unfused text-prompt YOLOE detection model on a holdout view."""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path

from open_vocab_detector import (
    DEFAULT_HOLDOUT,
    DEFAULT_WEIGHTS,
    HOLDOUT_POLICIES,
    ROOT,
    build_dataset_view,
    configure_offline,
    configure_ultralytics,
    holdout_list,
    prompt_for,
    write_json,
)


def trainer_class(mode: str):
    """Import model dependencies only on the explicitly requested training path."""
    import torch
    from ultralytics.models.yolo.detect import DetectionTrainer, DetectionValidator
    from ultralytics.models.yolo.yoloe.train import YOLOETrainer
    from ultralytics.nn.tasks import YOLOEModel
    from ultralytics.utils.torch_utils import unwrap_model

    class PromptValidator(DetectionValidator):
        def __call__(self, trainer=None, model=None, **kwargs):
            if trainer is not None:
                network = trainer.ema.ema if trainer.ema else unwrap_model(trainer.model)
                parameter = next(network.parameters())
                with torch.no_grad():
                    adapted = network.model[-1].get_tpe(
                        trainer.raw_text.to(device=parameter.device, dtype=parameter.dtype)
                    )
                network.set_classes(list(trainer.data["names"].values()), adapted)
            return super().__call__(trainer=trainer, model=model, **kwargs)

    class OpenVocabularyTrainer(YOLOETrainer):
        def get_model(self, cfg=None, weights=None, verbose=True):
            if weights is None:
                raise ValueError("A local pretrained YOLOE checkpoint is required")
            config = deepcopy(weights.yaml)
            config["head"][-1][2:] = ["YOLOEDetect", [self.data["nc"], 512, True]]
            config["scale"] = "s"
            network = YOLOEModel(
                config, ch=self.data["channels"], nc=self.data["nc"], verbose=verbose
            )
            network.load(weights)
            if network.model[-1].is_fused:
                raise ValueError("Training requires an unfused open-vocabulary head")
            network.eval()
            phrases = [prompt_for(n) for n in self.data["names"].values()]
            # MobileCLIP is used once, under inference mode; it is not an optimizer member.
            self.raw_text = network.get_text_pe(
                phrases, without_reprta=True, cache_clip_model=False
            ).clone()
            with torch.no_grad():
                network.set_classes(
                    list(self.data["names"].values()), network.model[-1].get_tpe(self.raw_text)
                )
            if mode == "linear_probe":
                # BaseTrainer respects these patterns and freezes BatchNorm statistics too.
                allowed = {f"model.{len(network.model) - 1}.cv3.{i}.2" for i in range(3)}
                frozen = {
                    name.rsplit(".", 1)[0].removeprefix("model.")
                    for name, _ in network.named_parameters()
                    if not any(name.startswith(prefix + ".") for prefix in allowed)
                }
                self.args.freeze = sorted(frozen)
            network.train()
            return network

        def build_dataset(self, img_path, mode="train", batch=None):
            return DetectionTrainer.build_dataset(self, img_path, mode, batch)

        def preprocess_batch(self, batch):
            batch = DetectionTrainer.preprocess_batch(self, batch)
            batch["txt_feats"] = self.raw_text.to(self.device).expand(batch["img"].shape[0], -1, -1)
            return batch

        def _model_train(self):
            super()._model_train()
            if mode == "linear_probe":
                # Parameter patterns include a trailing dot; BatchNorm module names do not.
                for module in self.model.modules():
                    if isinstance(module, torch.nn.modules.batchnorm._BatchNorm):
                        module.eval()

        def get_validator(self):
            self.loss_names = "box", "cls", "dfl"
            return PromptValidator(
                self.test_loader,
                save_dir=self.save_dir,
                args=deepcopy(self.args),
                _callbacks=self.callbacks,
            )

    return OpenVocabularyTrainer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--holdout-classes", default=DEFAULT_HOLDOUT)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS)
    parser.add_argument("--weights-dir", type=Path, default=ROOT / "weights")
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--imgsz", type=int, default=1280)
    parser.add_argument("--batch", type=int, default=4)
    parser.add_argument("--mode", choices=("linear_probe", "full"), default="full")
    parser.add_argument("--holdout-policy", choices=HOLDOUT_POLICIES, default="background")
    parser.add_argument("--device", default="0")
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--preflight-memory-out", type=Path)
    args = parser.parse_args()
    if min(args.epochs, args.imgsz, args.batch) <= 0:
        parser.error("epochs, imgsz and batch must be positive")
    manifest = build_dataset_view(
        args.data, args.out, holdout_list(args.holdout_classes), args.holdout_policy
    )
    plan = {
        "status": "dry_run" if args.dry_run else "prepared",
        "data": str((args.out / "dataset/data.yaml").resolve()),
        "weights": str(args.weights.resolve()),
        "mode": args.mode,
        "trainer": "OpenVocabularyTrainer(YOLOETrainer)",
        "epochs": args.epochs,
        "imgsz": args.imgsz,
        "batch": args.batch,
        "prompts": {name: prompt_for(name) for name in manifest["names"]},
        "holdout_classes": manifest["holdout_classes"],
        "counts": manifest["splits"],
    }
    write_json(args.out / "train-plan.json", plan)
    print(json.dumps(plan, indent=2))
    if args.dry_run:
        return  # No torch or Ultralytics import, device discovery, or model loading.
    configure_offline(args.weights, args.weights_dir)
    from ultralytics import YOLOE

    if args.preflight_memory_out is not None:
        import torch
        cuda_device = torch.device(
            f"cuda:{args.device}" if str(args.device).isdigit() else args.device
        )
        torch.cuda.init()
        torch.cuda.reset_peak_memory_stats(cuda_device)

    configure_ultralytics(args.weights_dir)
    model = YOLOE(str(args.weights.resolve()))
    model.task = "detect"
    model.overrides["task"] = "detect"
    model.train(
        trainer=trainer_class(args.mode),
        data=plan["data"],
        task="detect",
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        workers=args.workers,
        seed=args.seed,
        project=str(args.out.resolve()),
        name="train",
        exist_ok=False,
        amp=False,
        plots=False,
    )
    if args.preflight_memory_out is not None:
        import torch
        properties = torch.cuda.get_device_properties(cuda_device)
        result = {
            "peak_gpu_memory_bytes": torch.cuda.max_memory_allocated(cuda_device),
            "peak_gpu_reserved_bytes": torch.cuda.max_memory_reserved(cuda_device),
            "device_total_memory_bytes": properties.total_memory,
        }
        args.preflight_memory_out.parent.mkdir(parents=True, exist_ok=True)
        args.preflight_memory_out.write_text(json.dumps(result) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
