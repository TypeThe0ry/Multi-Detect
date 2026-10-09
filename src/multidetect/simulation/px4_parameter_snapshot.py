"""Strict numeric extraction from recorded PX4 param-show output."""

import math
import re


def parse_parameter_observation(name: str, observation: dict) -> float:
    if not isinstance(name, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", name) is None:
        raise ValueError("invalid parameter name")
    if type(observation.get("returncode")) is not int or observation["returncode"] != 0:
        raise ValueError("parameter read failed")
    if observation.get("output_truncated") is not False:
        raise ValueError("truncated or unqualified parameter output")
    raw = observation.get("stdout")
    if not isinstance(raw, str) or len(raw) > 4096:
        raise ValueError("bounded parameter output required")
    pattern = rf"^[x+*l\s]*{re.escape(name)}\s+\[\d+,\d+\]\s*:\s*([^\s]+)\s*$"
    matches = re.findall(pattern, raw, flags=re.MULTILINE)
    if len(matches) != 1:
        raise ValueError("exactly one matching parameter value required")
    value = float(matches[0])
    if not math.isfinite(value):
        raise ValueError("finite parameter value required")
    return value
