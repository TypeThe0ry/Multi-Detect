from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_queue_continues_after_fresh_failed_identity_verdict(tmp_path: Path) -> None:
    fake_eval = tmp_path / "eval_r21id_identity.py"
    fake_eval.write_text(
        "import json, sys\n"
        "args = sys.argv\n"
        "Path = __import__('pathlib').Path\n"
        "Path(args[args.index('--output-json') + 1]).write_text("
        "json.dumps({'candidate_pass': False, 'decision': '保留 r21id-r3'}), encoding='utf-8')\n"
        "Path(args[args.index('--output-md') + 1]).write_text('failed acceptance', encoding='utf-8')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    report_json = tmp_path / "identity-report.json"
    report_md = tmp_path / "identity-report.md"
    environment = {
        **os.environ,
        "R": str(ROOT),
        "V": sys.executable,
        "DRY_RUN": "1",
        "PREBUILT_DATA": "test-fixture",
        "PREBUILT_ADDITIONS": "test-fixture",
        "FAKE_EVAL": str(fake_eval),
        "REPORT_JSON": str(report_json),
        "REPORT_MD": str(report_md),
    }
    command = r'''
source "$R/scripts/queue_r21id_round_r10.sh"
DRY_RUN=0
run "$V" "$FAKE_EVAL" --output-json "$REPORT_JSON" --output-md "$REPORT_MD"
printf 'NEXT_EVALUATION_REACHED\n'
'''

    result = subprocess.run(
        ["bash", "-e", "-c", command],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "identity acceptance: FAIL — 保留 r21id-r3" in result.stdout
    assert "acceptance failed; report recorded, continuing remaining evaluations" in result.stderr
    assert "NEXT_EVALUATION_REACHED" in result.stdout
    assert '"candidate_pass": false' in report_json.read_text(encoding="utf-8")
