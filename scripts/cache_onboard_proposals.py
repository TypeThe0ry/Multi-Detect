"""Cache the onboard FAST proposals for every recorded campaign frame.

Runs the same class-agnostic YOLOE proposer as ``scripts/onboard_policy.py``
(weights, generic vocabulary, confidence, image size) and the same box filter
as ``OnboardLoop.filter_detections``, so slow-system training rows can use the
boxes the vehicle actually sees in flight, including clutter and scenery.
Output: one JSON line per frame ``{"image": <repo-relative path>, "boxes":
[[x1, y1, x2, y2, score], ...]}``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.onboard_loop import CameraModel, OnboardLoop  # noqa: E402
from multidetect.sim_teacher.primitives import ActionPrimitive  # noqa: E402
from scripts.onboard_policy import DEFAULTS, YoloeProposer  # noqa: E402


def _frames(campaign: Path):
    for steps in sorted(campaign.rglob("steps.jsonl")):
        episode = steps.parent
        for line in steps.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # An attempt killed mid-write (e.g. host reboot) leaves a NUL-padded tail.
                print(f"[cache] skip unreadable line in {steps}", file=sys.stderr)
                break
            for side in ("before", "after"):
                obs = row.get(f"obs_{side}")
                if isinstance(obs, dict) and isinstance(obs.get("rgb_path"), str):
                    frame = episode / Path(obs["rgb_path"].replace("\\", "/"))
                    if frame.is_file():
                        yield frame


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--weights", type=Path, default=DEFAULTS["yoloe_weights"])
    parser.add_argument("--confidence", type=float, default=0.05)
    parser.add_argument("--imgsz", type=int, default=1280)
    parser.add_argument(
        "--max-frames", type=int,
        help="optional limit for a bounded smoke run; the default processes every frame",
    )
    parser.add_argument(
        "--device", help="inference device for the proposer (default: the proposer's own default)"
    )
    parser.add_argument(
        "--source-root", type=Path, default=ROOT,
        help="root that cache image keys are relative to (the checkout holding the campaigns)",
    )
    args = parser.parse_args(argv)
    import cv2

    proposer_kwargs = {} if args.device is None else {"device": args.device}
    proposer = YoloeProposer(
        args.weights, confidence=args.confidence, imgsz=args.imgsz, **proposer_kwargs
    )
    done: set[str] = set()
    if args.output.is_file():
        done = {
            json.loads(line)["image"]
            for line in args.output.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    loops: dict[tuple[int, int], OnboardLoop] = {}
    written = 0
    with args.output.open("a", encoding="utf-8", newline="\n") as handle:
        for campaign in args.campaign:
            for frame in _frames(campaign):
                key = frame.resolve().relative_to(args.source_root.resolve()).as_posix()
                if key in done:
                    continue
                done.add(key)
                bgr = cv2.imread(str(frame))
                height, width = bgr.shape[:2]
                loop = loops.setdefault(
                    (width, height),
                    OnboardLoop(
                        camera=CameraModel(
                            fx=1.0, fy=1.0, cx=width / 2, cy=height / 2, width=width, height=height
                        ),
                        search_action=lambda pose, yaw: ActionPrimitive.hold(1.0),
                        segment_clear=lambda pose, n, e: True,
                    ),
                )
                kept = loop.filter_detections(proposer(bgr))
                boxes = [[round(v, 1) for v in d.bbox] + [round(d.score, 4)] for d in kept]
                handle.write(json.dumps({"image": key, "boxes": boxes}) + "\n")
                written += 1
                if args.max_frames is not None and written >= args.max_frames:
                    handle.flush()
                    print(json.dumps({"frames": written, "output": str(args.output)}))
                    return 0
                if written % 200 == 0:
                    handle.flush()
                    print(json.dumps({"frames": written}), flush=True)
    print(json.dumps({"frames": written, "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
