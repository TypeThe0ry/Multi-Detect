"""Command-line entry point for the Polaris simulation AI gateway."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .service import AIService, create_server


def _default_config() -> Path:
    return Path(__file__).resolve().parents[4] / "configs" / "polaris_sim" / "ai.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="polaris-sim-ai")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("POLARIS_SIM_AI_CONFIG", _default_config())),
    )
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        parser.error("AI config must contain a JSON object")
    vehicles_json = os.environ.get("POLARIS_SIM_VEHICLES")
    if vehicles_json:
        try:
            vehicles = json.loads(vehicles_json)
        except json.JSONDecodeError as exc:
            parser.error(f"POLARIS_SIM_VEHICLES is invalid JSON: {exc}")
        if not isinstance(vehicles, list):
            parser.error("POLARIS_SIM_VEHICLES must be a JSON array")
        rtsp_by_slot = {
            str(row["slot"]): row["rtsp"]
            for row in vehicles
            if isinstance(row, dict)
            and isinstance(row.get("slot"), int)
            and isinstance(row.get("rtsp"), str)
        }
        inputs = config.setdefault("inputs", {})
        if isinstance(inputs, dict):
            camera = inputs.setdefault("camera", {})
            if isinstance(camera, dict):
                camera["rtsp_by_slot"] = rtsp_by_slot
    host = args.host or str(config.get("host", "0.0.0.0"))
    port = args.port or int(config.get("port", 8790))
    service = AIService(config)
    server = create_server(service, host, port)
    try:
        print(f"polaris-sim-ai listening on {host}:{port}", flush=True)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
