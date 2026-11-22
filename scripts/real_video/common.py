"""real_video 流水线的公共路径与清单读写。纯 CPU、无第三方依赖。"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_ROOT = "/mnt/nas/AdminHome/md-real-video"


def data_root() -> Path:
    return Path(os.environ.get("MD_REAL_VIDEO_ROOT", DEFAULT_ROOT))


def write_jsonl(path: Path, rows) -> int:
    """追加写，绝不覆盖已有数据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("a", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
