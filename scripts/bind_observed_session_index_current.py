"""Bind active observed-session index entries to a retained current-parity copy."""

# ruff: noqa: E501 -- immutable evidence paths are intentionally explicit.

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "docs/current-progress.json"


def main() -> int:
    text = INDEX.read_text(encoding="utf-8")
    old_path = "artifacts/evaluation/shared-policy-observed-session-20261004-onboard-adapter-parity-audit-r1.json"
    new_path = "artifacts/evaluation/shared-policy-observed-session-20261004-px4-path-rebind-r1.json"
    old_sha = "5cbd0f2f99dca2f0d02e18b8761143a1a47819f0b81a8e3ac58849f718a857b2"
    new_sha = "27bda2804a9c6e07f59cdba802fe403abce1ebf7600c68a4ad07f70b2100f8fc"
    def patch_entry(source: str, key: str) -> str:
        marker = f'  "{key}": {{'
        start = source.index(marker)
        end = source.index("\n  },", start) + len("\n  },")
        block = source[start:end]
        block = block.replace(old_path, new_path)
        block = block.replace('"evidence_sha256": "' + old_sha + '"', '"evidence_sha256": "' + new_sha + '"')
        block = block.replace('"evidence_bytes": 4761', '"evidence_bytes": 5152')
        block = block.replace('"release_parity_file_sha256": "e38acc75680dde795ba8ba2ce922d6badb12e543b86af080c05dc34b3c8dc06d"', '"release_parity_file_sha256": "adba0be47654b7d7b2d90f037ff17275edd038dc4dad501b6067be549154dfdc"')
        return source[:start] + block + source[end:]

    for key in ("shared_policy_observed_session_20260927_r17", "shared_policy_observed_session_20260924"):
        text = patch_entry(text, key)
    INDEX.write_text(text, encoding="utf-8", newline="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
