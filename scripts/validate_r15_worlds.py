#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from jsonschema import validate

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402
from multidetect.simulator.stack import _validate_world_sdf  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--scenarios", type=Path, default=ROOT / "configs/scenarios/generated-r15")
    p.add_argument("--worlds", type=Path, default=ROOT / "simulation/gazebo/worlds")
    a = p.parse_args(argv)
    sc = a.scenarios
    cfg = json.loads((sc / "r15-world-set.json").read_text(encoding="utf-8"))
    idx = json.loads((sc / "index.json").read_text(encoding="utf-8"))
    worlds = idx["worlds"]
    assert (
        idx["count"] == cfg["count"] == 20
        and sum(e["split"] == "train" for e in worlds) == 16
        and sum(e["split"] == "val" for e in worlds) == 4
        and not any(e["split"] == "test" for e in worlds)
    )
    catalog = {
        x["slug"]: x
        for x in json.loads(
            (ROOT / "configs/sim-assets/fuel-catalog.json").read_text(encoding="utf-8")
        )["assets"]
    }
    held = set(cfg.get("leave_out_target_slugs", ("pickup", "postbox", "stop_sign")))
    schemas = {
        n: json.loads(
            (ROOT / "configs/schemas" / f"scenario-{n}.schema.json").read_text(encoding="utf-8")
        )
        for n in ("manifest", "runtime-input", "truth", "expectations")
    }
    for e in worlds:
        wid = e["world_id"]
        wp = a.worlds / e["world_file"]
        _validate_world_sdf(wp, expected_name="default")
        root = ET.parse(wp).getroot()
        tr = json.loads((sc / f"{wid}.truth.json").read_text(encoding="utf-8"))
        assert tr["asset_mode"] == "fuel_v1"
        expected = cfg["worlds"][int(wid[-4:])]
        target = tr["targets"][0]
        assert (
            target["asset_slug"] == expected["target"]
            and target.get("true_target") is True
            and target["asset_slug"] not in held
        )
        assert set(expected.get("confusers", ())) <= {
            r["asset_slug"] for r in tr["targets"] if r.get("confuser")
        }
        assert set(expected.get("forced_distractors", ())) <= {
            r["asset_slug"] for r in tr["targets"] if r.get("forced_distractor")
        }
        core = {k: tr[k] for k in ("schema_version", "scenario_id", "scenario_version")}
        core["targets"] = []
        for row in tr["targets"]:
            item = {
                k: row[k]
                for k in ("target_id", "label", "class", "color", "pose", "behavior", "position")
                if k in row
            }
            item.pop("color", None) if item.get("color") is None else None
            core["targets"].append(item)
            assert row["asset_slug"] in catalog
            if row.get("true_target"):
                assert row["asset_slug"] not in held
        validate(core, schemas["truth"])
        labels = tr.get("segmentation_labels", {})
        assert labels and len(labels) == len(root.find("world").findall("model")) - 1
        scenario = json.loads((sc / f"{wid}.scenario.json").read_text(encoding="utf-8"))
        runtime = json.loads((sc / f"{wid}.runtime_input.json").read_text(encoding="utf-8"))
        expectations = json.loads((sc / f"{wid}.expectations.json").read_text(encoding="utf-8"))
        validate(scenario, schemas["manifest"])
        validate(runtime, schemas["runtime-input"])
        validate(expectations, schemas["expectations"])
        assert scenario["channels"]["runtime_input"]["sha256"] == canonical_sha256(runtime)
        assert scenario["channels"]["truth"]["sha256"] == canonical_sha256(tr)
        assert scenario["channels"]["expectations"]["sha256"] == canonical_sha256(expectations)
    print(
        json.dumps(
            {
                "ok": True,
                "worlds": len(worlds),
                "sdf_bytes": sum((a.worlds / e["world_file"]).stat().st_size for e in worlds),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
