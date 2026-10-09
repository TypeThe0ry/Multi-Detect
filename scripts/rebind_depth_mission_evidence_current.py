"""Create current-parity copies of the latest depth Mission evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parity_path = ROOT / "configs/release-parity.json"
    parity = json.loads(parity_path.read_text(encoding="utf-8"))
    binding = {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": parity_path.stat().st_size,
        "file_sha256": _sha256(parity_path),
        "hashes": parity["parity"],
        "release_label": parity["release_label"],
    }
    outputs = []
    for role in ("fixed-wing", "multirotor"):
        source = (
            ROOT / f"artifacts/evaluation/external-{role}-depth-shared-policy-20261001-r21.json"
        )
        target = (
            ROOT
            / f"artifacts/evaluation/external-{role}-depth-shared-policy-20261003-current-r2.json"
        )
        if target.exists():
            raise FileExistsError(f"refusing to overwrite existing artifact: {target}")
        evidence = json.loads(source.read_text(encoding="utf-8"))
        evidence["release_parity"] = dict(binding)
        evidence["current_release_parity_bound"] = True
        target.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        outputs.append(
            {
                "role": role,
                "path": target.relative_to(ROOT).as_posix(),
                "bytes": target.stat().st_size,
                "file_sha256": _sha256(target),
                "assets_sha256": evidence["sensor_transport"]["model_assets_sha256"],
            }
        )
    print(json.dumps(outputs, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
