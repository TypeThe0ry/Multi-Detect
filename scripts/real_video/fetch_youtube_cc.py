#!/usr/bin/env python3
"""按关键词检索 YouTube 上标为 Creative Commons 的无人机视频并下载（A 档数据）。

只下载元数据 license 含 'Creative Commons' 的条目；每条写入 manifest 以便生成署名清单。
不会覆盖或删除已有文件。需要 yt-dlp 与 ffmpeg；不需要 GPU。

  python fetch_youtube_cc.py --query "FPV drone flight cc" --max 20 --max-height 720
  python fetch_youtube_cc.py --dry-run --query "drone first person view car chase"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import data_root, read_jsonl, write_jsonl  # noqa: E402

# 与任务贴近的检索词：目标类（车、人、消防栓、锥桶）+ 视角（斜视、跟拍、云台）
DEFAULT_QUERIES = [
    "drone follow car aerial footage",
    "drone gimbal tracking person outdoor",
    "FPV drone flight street",
    "fixed wing drone camera flight footage",
    "VTOL tailsitter flight test camera",
    "drone landing on vehicle roof footage",
]


def search(query: str, n: int) -> list[dict]:
    # 搜索结果用 flat 模式很快，但没有 license；先取 id，再逐条取完整元数据。
    out = subprocess.run(
        ["yt-dlp", f"ytsearch{n}:{query} creative commons", "--flat-playlist", "-J"],
        capture_output=True, text=True, check=True).stdout
    return json.loads(out).get("entries", [])


def metadata(video_id: str) -> dict:
    out = subprocess.run(
        ["yt-dlp", "-J", "--skip-download", f"https://www.youtube.com/watch?v={video_id}"],
        capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def is_cc(meta: dict) -> bool:
    return "creative commons" in str(meta.get("license") or "").lower()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", action="append", help="可重复；缺省用内置检索词")
    ap.add_argument("--max", type=int, default=10, help="每个检索词最多检查的条数")
    ap.add_argument("--max-height", type=int, default=720)
    ap.add_argument("--max-duration", type=int, default=1800, help="秒，过长视频跳过")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = data_root()
    manifest = root / "youtube_cc" / "manifest.jsonl"
    seen = {r["id"] for r in read_jsonl(manifest)}
    new_rows = []
    for q in args.query or DEFAULT_QUERIES:
        for e in search(q, args.max):
            vid = e.get("id")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            try:
                m = metadata(vid)
            except subprocess.CalledProcessError:
                continue
            if not is_cc(m) or (m.get("duration") or 0) > args.max_duration:
                continue
            row = {"id": vid, "url": m.get("webpage_url"), "title": m.get("title"),
                   "uploader": m.get("uploader"), "license": m.get("license"),
                   "duration": m.get("duration"), "query": q, "tier": "A",
                   "file": f"youtube_cc/videos/{vid}.mp4"}
            if not args.dry_run:
                dest = root / row["file"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    subprocess.run(
                        ["yt-dlp", "-f", f"bv*[height<={args.max_height}]+ba/b[height<={args.max_height}]",
                         "--merge-output-format", "mp4", "-o", str(dest), row["url"]], check=False)
            new_rows.append(row)
            print("CC", vid, row["title"])
    if not args.dry_run:
        write_jsonl(manifest, new_rows)
    print(f"{len(new_rows)} 条新 CC 视频" + ("（dry-run，未下载）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
