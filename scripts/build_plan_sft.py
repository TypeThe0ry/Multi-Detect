"""Synthetic task-text -> MissionPlan rows for the slow model (text only).

Compositional templates in Chinese and English cover every ungated skill and the
common combinations (find, find+record/inspect/follow, goto+search, count,
patrol, survey, monitor, hover, return home, land). Each row carries the exact
prompt the onboard planner uses (``mission_planner.plan_prompt``) and the plan
as the answer; every plan passes ``validate_mission_plan``. Texts identical to
the hand-written evaluation tasks are dropped, so that set stays held out.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.mission_plan import validate_mission_plan  # noqa: E402
from multidetect.autonomy.mission_planner import plan_prompt  # noqa: E402

OBJECTS = [  # (english query, chinese)
    ("car", "轿车"),
    ("red car", "红色轿车"),
    ("blue car", "蓝色轿车"),
    ("white van", "白色面包车"),
    ("truck", "卡车"),
    ("bus", "公交车"),
    ("ambulance", "救护车"),
    ("fire truck", "消防车"),
    ("motorcycle", "摩托车"),
    ("bicycle", "自行车"),
    ("person", "行人"),
    ("worker", "工人"),
    ("dog", "狗"),
    ("traffic cone", "交通锥"),
    ("barrel", "路障桶"),
    ("fire hydrant", "消防栓"),
    ("dumpster", "垃圾箱"),
    ("mailbox", "邮箱"),
    ("cardboard box", "纸箱"),
    ("backpack", "背包"),
    ("tent", "帐篷"),
    ("boat", "船"),
    ("ladder", "梯子"),
    ("pallet", "托盘"),
    ("tractor", "拖拉机"),
    ("solar panel", "太阳能板"),
    ("antenna", "天线"),
    ("pipe", "管道"),
    ("crate", "木箱"),
]
ZONES = [
    ("zone:lot", "停车场", "the lot"),
    ("zone:field", "场地", "the field"),
    ("zone:yard", "院子", "the yard"),
    ("zone:roof", "屋顶", "the roof"),
    ("zone:dock", "码头", "the dock"),
    ("zone:farm", "农田", "the farm"),
]
POIS = [
    ("poi:gate", "大门", "the gate"),
    ("poi:pad", "起降坪", "the pad"),
    ("poi:tower", "水塔", "the tower"),
    ("poi:shed", "仓库", "the shed"),
    ("poi:bridge", "桥", "the bridge"),
]
ROUTES = [
    ("route:fence", "围栏", "the fence route"),
    ("route:north", "北线", "the north route"),
    ("route:road", "道路", "the road"),
]
ZH_NUM = {2: "两", 3: "三", 4: "四", 5: "五", 6: "六", 8: "八", 10: "十"}


ALIASES = [
    "site",
    "area_a",
    "area_b",
    "plaza",
    "north",
    "south",
    "east",
    "west",
    "zone1",
    "zone2",
    "lot_b",
    "park",
    "court",
    "square",
    "base",
    "camp",
    "point_a",
    "spot",
]
DIGITS = "零一二三四五六七八九"


def zh_number(n: int) -> str:
    """Chinese numeral for 0..999 (十二, 二十, 一百二十)."""
    if n < 10:
        return DIGITS[n]
    if n < 20:
        return "十" + (DIGITS[n % 10] if n % 10 else "")
    if n < 100:
        return DIGITS[n // 10] + "十" + (DIGITS[n % 10] if n % 10 else "")
    rest = n % 100
    tail = "" if rest == 0 else ("零" + DIGITS[rest] if rest < 10 else zh_number(rest))
    return DIGITS[n // 100] + "百" + tail


def localise_places(rng, steps, text, labels):
    """Rename the plan's place refs to random aliases and list them as known places.

    The model must copy references from the prompt instead of memorising names; a
    few unrelated places are listed too. Returns (steps, known place strings).
    """
    mapping = {}
    for step in steps:
        for key in ("region", "target_ref", "route_ref"):
            ref = step["params"].get(key)
            if (
                not isinstance(ref, str)
                or ref in {"zone:all", "home"}
                or ref.startswith("finding:")
            ):
                continue
            if ref not in mapping:
                kind = ref.split(":", 1)[0]
                mapping[ref] = ref if rng.random() < 0.3 else f"{kind}:{rng.choice(ALIASES)}"
            step["params"][key] = mapping[ref]
    known = {new: labels.get(old, old) for old, new in mapping.items()}
    for kind, pool in (("zone", ZONES), ("poi", POIS), ("route", ROUTES)):
        for _ in range(rng.randint(0, 2)):
            ref, zh, en = rng.choice(pool)
            alias = f"{kind}:{rng.choice(ALIASES)}"
            known.setdefault(alias, f"{zh} / {en}")
    items = [f"{ref} ({label})" for ref, label in known.items()]
    rng.shuffle(items)
    return steps, tuple(items)


def S(i, skill, params, ok, fail):  # noqa: N802
    return {"id": i, "skill": skill, "params": params, "on_success": ok, "on_fail": fail}


def H(i):  # noqa: N802
    return S(i, "return_home", {}, None, None)


def L(i):  # noqa: N802
    return S(i, "land", {}, None, None)


def end(rng, land_text):
    """Terminal step and phrase: land when the text says so, else return home."""
    if land_text:
        return L, rng.choice(["然后降落", "最后降落"]), rng.choice(["then land", "and land"])
    return (
        H,
        rng.choice(["然后返航", "再回来", "后回家", ""]),
        rng.choice(["and come back", "then return home", "and return", ""]),
    )


def mission(rng: random.Random) -> tuple[str, str, list[dict], list[str]]:
    """One (zh text, en text, steps, report) sample."""
    q, qz = rng.choice(OBJECTS)
    z, zz, ze = rng.choice(ZONES)
    p, pz, pe = rng.choice(POIS)
    land = rng.random() < 0.25
    term, zend, eend = end(rng, land)
    kind = rng.choice(
        [
            "find",
            "find",
            "find_record",
            "find_inspect",
            "goto_find",
            "count",
            "count",
            "count_goto",
            "patrol",
            "survey",
            "monitor",
            "goto_hover",
            "hover",
            "home",
            "land",
            "find_follow",
            "find_record_hover",
        ]
    )
    if kind == "find":
        where = rng.choice([("zone:all", "", ""), (z, f"在{zz}", f" in {ze}")])
        zh = (
            rng.choice(
                [
                    f"{where[1]}找到{qz}并报告位置",
                    f"{where[1]}寻找{qz}",
                    f"帮我{where[1]}找一下{qz}",
                ]
            )
            + zend
        )
        en = (
            rng.choice(
                [
                    f"Find the {q}{where[2]}",
                    f"Look for a {q}{where[2]}",
                    f"Search{where[2] or ' the area'} for the {q}",
                ]
            )
            + f" {eend}".rstrip()
            + "."
        )
        steps = [S("s1", "area_search", {"region": where[0], "query": q}, "s2", "s2"), term("s2")]
        return zh, en, steps, ["findings", "positions"]
    if kind in {"find_record", "find_record_hover"}:
        hover = kind == "find_record_hover"
        sec = rng.choice([10, 20, 30, 60])
        zh = f"找到{qz}，拍照记录" + (f"，悬停{sec}秒" if hover else "") + zend
        en = (
            f"Find the {q}, take a photo of it"
            + (f", hover {sec} seconds" if hover else "")
            + f" {eend}".rstrip()
            + "."
        )
        steps = [
            S("s1", "area_search", {"region": "zone:all", "query": q}, "s2", "s9"),
            S("s2", "record", {"query": q}, "s3" if hover else "s9", "s9"),
        ]
        if hover:
            steps.append(S("s3", "hover", {"duration_s": sec}, "s9", "s9"))
        steps.append(term("s9"))
        return zh, en, steps, ["findings", "evidence"]
    if kind == "find_inspect":
        views = rng.choice([3, 4, 6, 8])
        zh = (
            rng.choice(
                [
                    f"找到{qz}，从{ZH_NUM[views]}个角度检查",
                    f"找到{qz}并绕着它拍{ZH_NUM[views]}张照片",
                ]
            )
            + zend
        )
        en = (
            rng.choice(
                [
                    f"Find the {q} and inspect it from {views} sides",
                    f"Find the {q} and circle it taking {views} photos",
                ]
            )
            + f" {eend}".rstrip()
            + "."
        )
        steps = [
            S("s1", "area_search", {"region": "zone:all", "query": q}, "s2", "s3"),
            S(
                "s2",
                "inspect",
                {
                    "target_ref": "finding:1",
                    "query": q,
                    "views": views,
                    "standoff_m": rng.choice([8, 10, 12, 15]),
                },
                "s3",
                "s3",
            ),
            term("s3"),
        ]
        return zh, en, steps, ["findings", "views"]
    if kind == "goto_find":
        zh = rng.choice([f"先飞到{pz}，再找{qz}", f"去{pz}看看有没有{qz}"]) + zend
        en = (
            rng.choice([f"Fly to {pe} and look for a {q}", f"Go to {pe}, then search for the {q}"])
            + f" {eend}".rstrip()
            + "."
        )
        steps = [
            S("s1", "goto", {"target_ref": p}, "s2", "s3"),
            S(
                "s2",
                "area_search",
                {"region": z if rng.random() < 0.5 else "zone:all", "query": q},
                "s3",
                "s3",
            ),
            term("s3"),
        ]
        return zh, en, steps, ["findings", "positions"]
    if kind in {"count", "count_goto"}:
        limit = rng.random() < 0.3
        n = rng.choice([5, 10, 20])
        zh = rng.choice([f"数一数{zz}里有几个{qz}", f"统计{zz}的{qz}数量", f"{zz}里有多少{qz}"]) + (
            f"，最多数到{n}个" if limit else ""
        )
        en = rng.choice(
            [f"Count the {q}s in {ze}", f"How many {q}s are in {ze}?", f"Count every {q} in {ze}"]
        ) + (f", up to {n}" if limit else "")
        params = {"region": z, "query": q, **({"max_count": n} if limit else {})}
        if kind == "count_goto":
            zh += f"，然后飞到{pz}降落"
            en = en.rstrip("?") + f", then land at {pe}."
            steps = [
                S("s1", "count", params, "s2", "s2"),
                S("s2", "goto", {"target_ref": p}, "s3", "s3"),
                L("s3"),
            ]
        else:
            zh += zend
            en = en.rstrip(".?") + f" {eend}".rstrip() + "."
            steps = [S("s1", "count", params, "s2", "s2"), term("s2")]
        return zh, en, steps, ["count", "positions"]
    if kind == "patrol":
        r, rz, re_ = rng.choice(ROUTES)
        loops = rng.choice([1, 2, 3, 5])
        zh = f"沿{rz}巡逻{ZH_NUM.get(loops, '一')}圈，注意{qz}" + zend
        en = (
            f"Patrol {re_} {loops} time{'s' if loops > 1 else ''} watching for {q}s"
            + f" {eend}".rstrip()
            + "."
        )
        steps = [
            S("s1", "patrol", {"route_ref": r, "query": q, "loops": loops}, "s2", "s2"),
            term("s2"),
        ]
        return zh, en, steps, ["alerts"]
    if kind == "survey":
        ov = rng.choice([0.6, 0.7, 0.75, 0.8])
        zh = f"对{zz}做一次测绘，重叠率{int(ov * 100)}%" + zend
        en = f"Survey {ze} with {int(ov * 100)} percent overlap" + f" {eend}".rstrip() + "."
        steps = [S("s1", "survey", {"region": z, "overlap": ov}, "s2", "s2"), term("s2")]
        return zh, en, steps, ["coverage"]
    if kind == "monitor":
        mins = rng.choice([2, 5, 10, 15])
        zh = f"监视{zz}{mins}分钟，看有没有{qz}" + zend
        en = f"Watch {ze} for {q}s for {mins} minutes" + f" {eend}".rstrip() + "."
        steps = [
            S("s1", "monitor", {"region": z, "query": q, "duration_s": mins * 60}, "s2", "s2"),
            term("s2"),
        ]
        return zh, en, steps, ["alerts"]
    if kind == "goto_hover":
        sec = rng.choice([15, 30, 45, 60, 90])
        zh = f"飞到{pz}，悬停{sec}秒" + zend
        en = f"Fly to {pe} and hover for {sec} seconds" + f" {eend}".rstrip() + "."
        steps = [
            S("s1", "goto", {"target_ref": p}, "s2", "s3"),
            S("s2", "hover", {"duration_s": sec}, "s3", "s3"),
            term("s3"),
        ]
        return zh, en, steps, ["status"]
    if kind == "hover":
        sec = rng.choice([10, 20, 30, 60, 120])
        zh, en = (
            f"原地悬停{sec}秒" + zend,
            f"Hover in place for {sec} seconds" + f" {eend}".rstrip() + ".",
        )
        return zh, en, [S("s1", "hover", {"duration_s": sec}, "s2", "s2"), term("s2")], ["status"]
    if kind == "home":
        return (
            rng.choice(["返航", "现在回家", "回到起飞点"]),
            rng.choice(["Return home.", "Come back now.", "Fly back to the start."]),
            [H("s1")],
            ["status"],
        )
    if kind == "land":
        return (
            rng.choice(["降落", "马上降落", "就地降落"]),
            rng.choice(["Land.", "Land now.", "Land here."]),
            [L("s1")],
            ["status"],
        )
    # find_follow
    sec = rng.choice([60, 120, 180])
    zh = f"找到{qz}并跟随{sec // 60}分钟" + zend
    en = f"Find the {q} and follow it for {sec // 60} minutes" + f" {eend}".rstrip() + "."
    steps = [
        S("s1", "area_search", {"region": "zone:all", "query": q}, "s2", "s3"),
        S("s2", "follow", {"track_ref": "track:1", "query": q, "duration_s": sec}, "s3", "s3"),
        term("s3"),
    ]
    return zh, en, steps, ["findings", "track"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=1200)
    parser.add_argument("--val-rows", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261015)
    parser.add_argument(
        "--eval-tasks", type=Path, default=ROOT / "configs/onboard/plan-eval-tasks.json"
    )
    args = parser.parse_args(argv)
    held_out = {t["text"] for t in json.loads(args.eval_tasks.read_text(encoding="utf-8"))["tasks"]}
    labels = {r[0]: f"{r[1]} / {r[2]}" for r in ZONES + POIS + ROUTES}
    rng = random.Random(args.seed)
    rows, seen = [], set()
    while len(rows) < args.rows + args.val_rows:
        zh, en, steps, report = mission(rng)
        if rng.random() < 0.5:
            # Chinese numerals as often as digits.
            zh = re.sub(r"\d+", lambda m: zh_number(int(m.group())), zh)
        text = zh if rng.random() < 0.5 else en
        text = " ".join(text.split())
        steps, refs = localise_places(rng, steps, text, labels)
        if text in held_out or text in seen:
            continue
        seen.add(text)
        plan = {"plan_version": 1, "goal": en.rstrip(".")[:160], "steps": steps, "report": report}
        validate_mission_plan(plan)
        sample_id = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        rows.append(
            {
                "sample_id": f"plan-{sample_id}",
                "kind": "mission_plan",
                "world": "synthetic_plan",
                "task_text": text,
                "prompt": plan_prompt(text, refs=refs),
                "assistant_target": plan,
            }
        )
    args.output.mkdir(parents=True, exist_ok=True)
    splits = {"train": rows[: args.rows], "val": rows[args.rows :]}
    for name, part in splits.items():
        with (args.output / f"{name}.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
            for row in part:
                handle.write(
                    json.dumps({**row, "split": name}, ensure_ascii=False, sort_keys=True) + "\n"
                )
    print(json.dumps({k: len(v) for k, v in splits.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
