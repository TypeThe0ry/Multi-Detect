"""Synthetic task-text -> MissionPlan rows for the slow model (text only).

Compositional templates in Chinese and English cover every ungated skill and the
common combinations (find, find+record/inspect/follow, goto+search, count,
patrol, survey, monitor, hover, return home, land). Each row carries the exact
prompt the onboard planner uses (``mission_planner.plan_prompt``) and the plan
as the answer; every plan passes ``validate_mission_plan``. Texts identical to
the hand-written evaluation tasks are dropped, so that set stays held out.
V5 retains a 70% v4 backbone and adds conditional/compositional operator phrasing;
it also excludes normalised duplicates and held-out few-shot prompt examples.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
DOUBLED_ARTICLE = re.compile(r"\b([Tt]he) the\b")

from multidetect.autonomy.mission_plan import SKILLS, validate_mission_plan  # noqa: E402
from multidetect.autonomy.mission_planner import (  # noqa: E402
    EXAMPLES,
    PLAN_PROMPT_VARIANTS,
    check_plan_refs,
    plan_prompt,
)
from multidetect.autonomy.plan_intent import check_plan_intent  # noqa: E402
from multidetect.autonomy.capability_contract import capability_violations  # noqa: E402

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


def mission_v3(rng: random.Random) -> tuple[str, str, list[dict], list[str]]:
    """Expanded v3 templates; ``mission`` remains the byte-for-byte v2 generator."""
    q, qz = rng.choice(OBJECTS)
    z, zz, ze = rng.choice(ZONES)
    p, pz, pe = rng.choice(POIS)
    ending = rng.choice(
        [
            ("然后返航", "then return home", "return"),
            ("再回来", "then come back", "return"),
            ("回到起飞点", "return to the launch point", "return"),
            ("降落", f"land at {pe}", "land"),
            ("就地降落", "land here", "land"),
        ]
    )
    terminal = L if ending[2] == "land" else H
    kind = rng.choice(["count", "follow", "record_hover", "goto_count", "patrol", "record"])
    if kind == "count":
        n = rng.choice([5, 8, 10, 12])
        zh_verb = rng.choice(["统计", "清点", "数一数"])
        en_verb = rng.choice(["How many", "Tally", "Count up"])
        zh_limit = rng.choice([f"，最多数到{n}个", f"，上限{n}个", ""])
        en_limit = rng.choice([f", up to {n}", f", no more than {n}", ""])
        zh = f"在{zz}里{zh_verb}{qz}{zh_limit}，{ending[0]}"
        en = f"{en_verb} {q}s in {ze}{en_limit}, {ending[1]}."
        steps = [
            S("s1", "count", {"region": z, "query": q, "max_count": n}, "s2", "s2"),
            terminal("s2"),
        ]
        return zh, en, steps, ["count", "positions"]
    if kind == "follow":
        minutes = rng.choice([1, 2, 3, 5])
        zh = f"找到{qz}后{rng.choice(['跟随', '跟着', '盯住并跟着'])}它{minutes}分钟，{ending[0]}"
        follow_phrase = rng.choice(["track and follow it", "keep following it", "shadow it"])
        en = f"Find the {q}, {follow_phrase} for {minutes} minutes, {ending[1]}."
        sec = minutes * 60
        steps = [
            S("s1", "area_search", {"region": "zone:all", "query": q}, "s2", "s3"),
            S("s2", "follow", {"track_ref": "track:1", "query": q, "duration_s": sec}, "s3", "s3"),
            terminal("s3"),
        ]
        return zh, en, steps, ["findings", "track"]
    if kind == "record_hover":
        sec = rng.choice([15, 20, 30, 45])
        zh = f"找到{qz}后拍照留存证据，再悬停{sec}秒，{ending[0]}"
        photo_phrase = rng.choice(["take a picture", "snap a photo", "capture evidence"])
        en = f"Find the {q}, {photo_phrase}, then hover {sec} seconds before {ending[1]}."
        steps = [
            S("s1", "area_search", {"region": "zone:all", "query": q}, "s2", "s9"),
            S("s2", "record", {"query": q}, "s3", "s9"),
            S("s3", "hover", {"duration_s": sec}, "s9", "s9"),
            terminal("s9"),
        ]
        return zh, en, steps, ["findings", "evidence"]
    if kind == "goto_count":
        n = rng.choice([3, 6, 10])
        zh = f"先飞到{pz}，再在{zz}清点{qz}，最后{ending[0]}"
        en = f"Go to {pe}, count the {q}s in {ze}, and {ending[1]}."
        steps = [
            S("s1", "goto", {"target_ref": p}, "s2", "s3"),
            S("s2", "count", {"region": z, "query": q, "max_count": n}, "s3", "s3"),
            terminal("s3"),
        ]
        return zh, en, steps, ["count", "positions"]
    if kind == "patrol":
        route, routez, routee = rng.choice(ROUTES)
        loops = rng.choice([2, 3, 4])
        zh = f"沿{routez}巡逻{loops}圈，观察{qz}，{ending[0]}"
        en = f"Patrol {routee} for {loops} loops while watching for {q}s, {ending[1]}."
        steps = [
            S("s1", "patrol", {"route_ref": route, "query": q, "loops": loops}, "s2", "s2"),
            terminal("s2"),
        ]
        return zh, en, steps, ["alerts"]
    zh = f"找到{qz}，拍一张照片并记录，{ending[0]}"
    en = f"Locate the {q}, take a picture and record it, then {ending[1]}."
    steps = [
        S("s1", "area_search", {"region": "zone:all", "query": q}, "s2", "s9"),
        S("s2", "record", {"query": q}, "s9", "s9"),
        terminal("s9"),
    ]
    return zh, en, steps, ["findings", "evidence"]


R23_SEQUENCE_QUOTAS = (
    "goto_hover_return", "record_hover_land", "patrol_goto_return",
    "monitor_monitor_return", "survey_survey_land", "current_view_record",
)


def r23_target_case(rng: random.Random, sequence: str):
    """Natural v3 regression coverage with an explicit target sequence."""
    q, qz = rng.choice(OBJECTS)
    first, firstz, firste = rng.choice(POIS)
    second, secondz, seconde = rng.choice(POIS)
    zone, zonez, zonee = rng.choice(ZONES)
    zone2, zone2z, zone2e = rng.choice(ZONES)
    route, routez, routee = rng.choice(ROUTES)
    sec = rng.choice([15, 30, 45, 60, 90])
    use_zh = rng.random() < 0.5
    if sequence == "goto_hover_return":
        plan = [S("s1", "goto", {"target_ref": first}, "s2", "s2"),
                S("s2", "hover", {"region": zone, "duration_s": sec}, "s3", "s3"), H("s3")]
        zh = rng.choice([f"先到{firstz}，在{zonez}上空悬停{sec}秒，然后返航",
                         f"飞往{firstz}后稳住{sec}秒，再回到起飞点",
                         f"去{firstz}等{sec}秒，完成后回家"])
        en = rng.choice([f"Go to {firste}, hold over the {zonee} for {sec} seconds, then return home.",
                         f"Fly to {firste}, hover for {sec} seconds, and come back to base.",
                         f"Reposition to {firste}, wait there for {sec} seconds, then return."])
    elif sequence == "record_hover_land":
        plan = [S("s1", "record", {"query": q}, "s2", "s2"),
                S("s2", "hover", {"duration_s": sec}, "s3", "s3"),
                L("s3")]
        zh = rng.choice([f"把当前看到的{qz}拍照留档，悬停{sec}秒后降落",
                         f"拍下{qz}作为证据，等{sec}秒，然后落地",
                         f"记录这只{qz}的照片，保持{sec}秒再降落"])
        en = rng.choice([f"Photograph the {q} in view, hover for {sec} seconds, then land.",
                         f"Take a still of the {q}, wait {sec} seconds, and set down.",
                         f"Record the {q} on camera, hold position for {sec} seconds, then land."])
    elif sequence == "patrol_goto_return":
        loops = rng.choice([2, 3, 4, 5])
        plan = [S("s1", "patrol", {"route_ref": route, "query": q, "loops": loops}, "s2", "s2"),
                S("s2", "goto", {"target_ref": second}, "s3", "s3"), H("s3")]
        zh = rng.choice([f"沿{routez}巡逻{loops}圈观察{qz}，结束后去{secondz}，再返航",
                         f"巡查{routez}{loops}趟，随后飞到{secondz}，最后回家",
                         f"先巡视{routez}{loops}圈，再转到{secondz}并回到起飞点"])
        en = rng.choice([f"Patrol {routee} for {loops} loops watching for {q}s, then go to {seconde} and return home.",
                         f"Run {loops} circuits of {routee}, reposition to {seconde}, and come back.",
                         f"Inspect {routee} {loops} times, then fly to {seconde} before returning."])
    elif sequence == "monitor_monitor_return":
        sec2 = rng.choice([120, 180, 240, 300])
        plan = [S("s1", "monitor", {"region": zone, "query": q, "duration_s": sec}, "s2", "s2"),
                S("s2", "monitor", {"region": zone2, "query": rng.choice(OBJECTS)[0], "duration_s": sec2}, "s3", "s3"), H("s3")]
        q2 = plan[1]["params"]["query"]
        q2z = next(z for e, z in OBJECTS if e == q2)
        zh = rng.choice([f"在{zonez}观察{qz}{sec}秒，再到{zone2z}观察{q2z}{sec2}秒，最后返航",
                         f"先盯着{zonez}里的{qz}，然后监看{zone2z}的{q2z}，完成后回家",
                         f"监视{zonez}{sec}秒，再监视{zone2z}{sec2}秒，带着{q2z}记录返航"])
        en = rng.choice([f"Monitor the {zonee} for {q}s for {sec} seconds, then monitor {zone2e} for {q2}s for {sec2} seconds and return home.",
                         f"Watch {zonee} for {sec} seconds, then observe {zone2e} for {sec2} seconds before coming back.",
                         f"Monitor {zonee} and then {zone2e} for the requested durations, then return."])
    elif sequence == "survey_survey_land":
        overlap = rng.choice([0.6, 0.7, 0.8, 0.85])
        plan = [S("s1", "survey", {"region": zone, "overlap": overlap}, "s2", "s2"),
                S("s2", "survey", {"region": zone2, "overlap": overlap}, "s3", "s3"), L("s3")]
        pct = int(overlap * 100)
        zh = rng.choice([f"先测绘{zonez}，再测{zone2z}，两处影像重叠{pct}%，完成后降落",
                         f"给{zonez}和{zone2z}各做一次航测，保持{pct}%重叠，最后落地",
                         f"连续测量{zonez}、{zone2z}，相邻照片重叠{pct}%，结束降落"])
        en = rng.choice([f"Survey the {zonee} and then the {zone2e} with {pct}% overlap, then land.",
                         f"Map {zonee} followed by {zone2e}, keeping {pct}% image overlap before setting down.",
                         f"Collect mapping imagery over {zonee} and {zone2e} at {pct}% overlap, then land."])
    else:
        plan = [S("s1", "record", {"query": q}, "s2", "s2"), H("s2")]
        zh = rng.choice([f"把当前视野里的{qz}拍照存档，然后返航",
                         f"不用搜索，直接记录眼前的{qz}并回到起飞点",
                         f"拍下已经看到的{qz}，保存证据后回家"])
        en = rng.choice([f"Photograph the {q} already in view, then return home; no search is needed.",
                         f"Save a still of the {q} currently visible and come back without searching.",
                         f"Record the {q} in the current view, then return to launch."])
    return (zh if use_zh else en), plan, ["coverage", "positions"]


def r23_deliver_case(rng: random.Random, index: int):
    ref, zh, en = rng.choice(POIS)
    payload = rng.choice([("包裹", "parcel"), ("药品箱", "medical case"), ("工具箱", "tool case"), ("样品盒", "sample box")])
    return_home = rng.random() < 0.75
    use_zh = rng.random() < 0.5
    if use_zh:
        choices = ([f"把{payload[0]}投放到{zh}，然后返航。",
                    f"携带{payload[0]}送到{zh}，交付后回到起飞点。",
                    f"将{payload[0]}空投至{zh}，完成后返航。"] if return_home else
                   [f"把{payload[0]}投放到{zh}，然后在当地降落。",
                    f"携带{payload[0]}送到{zh}并完成交付，不必返航，随后降落。",
                    f"将{payload[0]}空投至{zh}，交付后原地降落。"])
        text = rng.choice(choices)
    else:
        choices = ([f"Drop the {payload[1]} at the {en}, then return home.",
                    f"Carry the {payload[1]} to the {en} and bring the aircraft back.",
                    f"Deliver the {payload[1]} to the {en}, then fly back to launch."] if return_home else
                   [f"Drop the {payload[1]} at the {en} and finish there.",
                    f"Deliver the {payload[1]} to the {en}; land there without returning.",
                    f"Carry the {payload[1]} to the {en}, complete delivery, then land on site."])
        text = rng.choice(choices)
    steps = [S("s1", "deliver", {"target_ref": ref, "payload_ref": f"payload:{payload[1].replace(' ', '_')}"}, "s2", "s2")]
    if return_home:
        steps.append(H("s2"))
    else:
        steps.append(L("s2"))
    return text, steps, [f"{ref} ({zh} / {en})"], ("payload_drop",)


def r24_reference_rows(args, held_out: set[str], seen: set[str]) -> list[dict]:
    """Ground targets to the supplied ref kind, and preserve unresolved places."""
    rng = random.Random(f"{args.seed}:r24-reference")
    rows = []
    locations = [
        ("bridge", "桥", "bridge"), ("yard", "院子", "yard"),
        ("tower", "塔楼", "tower"), ("farm", "农场", "farm"),
        ("station", "气象站", "weather station"),
    ]
    unknowns = [
        ("青石码头", "Blue Stone Wharf"), ("旧气象站", "Old Weather Station"),
        ("北岸仓库", "North Shore Depot"), ("石拱渡口", "Stone Arch Ferry"),
        ("林场观测点", "Forest Survey Point"),
    ]
    # Landing stays where the aircraft is; returning goes back to launch/home.
    # Neither names a mapped place, so the plan carries no target_ref.
    land_spots = [
        ("脚下", "right here"), ("当前位置", "at the current position"),
        ("这里", "on this spot"), ("原地", "where it is now"),
    ]
    return_spots = [
        ("起飞的地方", "where it took off"), ("起飞点", "the launch point"),
        ("home", "home"), ("出发点", "the starting point"),
    ]
    type_count, unknown_count, context_count = (
        args.r24_reference_count, args.r24_unknown_count, args.r24_context_count
    )

    def emit(text, refs, plan, family, *, valid=True, finding=None):
        key = normalize_task(text)
        if key in held_out or key in seen:
            return False
        seen.add(key)
        row = {
            "sample_id": f"plan-r24-{family}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]}",
            "kind": "mission_plan", "world": "synthetic_plan_r24", "family": family,
            "task_text": text, "refs": list(refs), "expected_valid": valid,
            "expected_findings": [] if valid else [finding],
            "prompt": plan_prompt(text, refs=refs, prompt_variant=args.prompt_variant,
                                  capabilities=frozenset()),
            "assistant_target": plan,
        }
        if not valid:
            row["rejected_candidate"] = plan
            row["negative_target_mode"] = "rejected_candidate"
            assert_no_unknown_place_substitution(row)
        rows.append(row)
        return True

    index = 0
    while sum(row["family"] == "r24_reference_type" for row in rows) < type_count:
        if index > 10_000:
            raise ValueError("r24 reference template pool exhausted; reduce --r24-reference-count")
        name, zh, en = locations[index % len(locations)]
        mode = (index // len(locations)) % 4
        sec = (index % 6 + 1) * 15
        use_zh = index % 2 == 0
        poi, zone = f"poi:{name}", f"zone:{name}"
        if mode == 0:
            # A point cannot be placed in a region field. Hover has no required
            # region, so navigate to the point and hover at the current position.
            refs = (f"{poi} ({zh} / {en})",)
            zh_forms = (f"飞到{zh}上方悬停{sec}秒，再返航。", f"抵达{zh}后定点{sec}秒，随后回家。",
                        f"去{zh}停留{sec}秒，任务结束返航。", f"到{zh}附近保持{sec}秒，再回到起飞点。")
            en_forms = (f"Reach the {en}, hold position for {sec} seconds, then return home.",
                        f"Fly to the {en} and stay there for {sec} seconds before heading back.",
                        f"Go to the {en}, hover in place for {sec} seconds, and come back to base.",
                        f"Reposition to the {en}; remain stationary for {sec} seconds, then return.")
            text = (zh_forms[(index // 30) % len(zh_forms)] if use_zh else
                    en_forms[(index // 30) % len(en_forms)])
            steps = [S("s1", "goto", {"target_ref": poi}, "s2", "s2"),
                     S("s2", "hover", {"duration_s": sec}, "s3", "s3"), H("s3")]
        elif mode == 1:
            # Whole-area mapping/search is represented by the contract's zone:all.
            refs = (f"{poi} ({zh} / {en})", "zone:all (entire operating area / 全部作业区)")
            overlap = (0.6, 0.7, 0.8, 0.85)[index % 4]
            pct = int(overlap * 100)
            zh_forms = (f"先到{zh}，再测绘整个作业区，照片重叠{pct}%，最后降落。",
                        f"前往{zh}后对全部作业区航测，影像重叠{pct}%，完成着陆。",
                        f"到{zh}一趟，然后覆盖全域测绘，保持{pct}%重叠再落地。",
                        f"经过{zh}，对整个任务区做{pct}%影像重叠的测绘，之后降落。")
            en_forms = (f"Visit the {en}, survey the entire operating area at {pct}% overlap, and then land.",
                        f"Go by the {en}, map the full operating area with {pct}% image overlap, then land.",
                        f"After visiting the {en}, survey the whole mission area at {pct}% overlap before landing.",
                        f"Reach the {en} and collect mapping imagery across the full area at {pct}% overlap, then set down.")
            text = (zh_forms[(index // 30) % len(zh_forms)] if use_zh else
                    en_forms[(index // 30) % len(en_forms)])
            steps = [S("s1", "goto", {"target_ref": poi}, "s2", "s2"),
                     S("s2", "survey", {"region": "zone:all", "overlap": overlap}, "s3", "s3"), L("s3")]
        elif mode == 2:
            refs = (f"{poi} ({zh}点 / {en} point)", f"{zone} ({zh}区域 / {en} zone)")
            query = "vehicle"
            zh_forms = (f"在{zh}区域搜索车辆，完成后返航。", f"对{zh}范围做区域搜索，寻找车辆再回家。",
                        f"搜索{zh}区域中的车辆，结束后返回起飞点。", f"在{zh}区域查找车辆并随后返航。")
            en_forms = (f"Search the {en} zone for vehicles, then return home.",
                        f"Run an area search for vehicles in the {en} zone before heading back.",
                        f"Look for vehicles across the {en} area and come back to base.",
                        f"Search the {en} zone to find vehicles, then return to launch.")
            text = (zh_forms[(index // 30) % len(zh_forms)] if use_zh else
                    en_forms[(index // 30) % len(en_forms)])
            steps = [S("s1", "area_search", {"region": zone, "query": query}, "s2", "s2"), H("s2")]
        else:
            refs = (f"{poi} ({zh}点 / {en} point)", f"{zone} ({zh}区域 / {en} zone)")
            duration = (30, 60, 90, 120)[index % 4]
            zh_forms = (f"监看{zh}区域里的车辆{duration}秒，然后返航。", f"观察{zh}范围内的车辆{duration}秒，再回家。",
                        f"在{zh}区域盯守车辆{duration}秒，结束后返回起飞点。", f"对{zh}范围内车辆持续监控{duration}秒，随后返航。")
            en_forms = (f"Monitor vehicles in the {en} zone for {duration} seconds, then return home.",
                        f"Watch for vehicles within the {en} area for {duration} seconds before returning.",
                        f"Keep the {en} zone under vehicle observation for {duration} seconds, then head back.",
                        f"Observe vehicles in the {en} zone for {duration} seconds and come back to base.")
            text = (zh_forms[(index // 30) % len(zh_forms)] if use_zh else
                    en_forms[(index // 30) % len(en_forms)])
            steps = [S("s1", "monitor", {"region": zone, "query": "vehicle", "duration_s": duration}, "s2", "s2"), H("s2")]
        plan = {"plan_version": 1, "goal": text[:160], "steps": steps, "report": ["status"]}
        # Advance even when a row is rejected as a duplicate/holdout, so a
        # collision walks to the next template instead of retrying it forever.
        emit(text, refs, plan, "r24_reference_type")
        index += 1
    index = 0
    while sum(row["family"] == "r24_unknown_place" for row in rows) < unknown_count:
        if index > 10_000:
            raise ValueError("r24 unknown-place template pool exhausted; reduce --r24-unknown-count")
        zh, en = unknowns[index % len(unknowns)]
        known, known_zh, known_en = locations[(index + 1) % len(locations)]
        use_zh = index % 2 == 0
        variants = ("巡查情况，然后返航", "看看现场再回到起飞点", "核实那里发生什么后返航", "前往查看并回家",
                    "确认周边状况后回家", "检查一遍再飞回基地", "到现场了解情况，之后返航", "查看是否有异常后回到出发点")
        en_variants = ("Inspect the unlisted {place} and return home", "Check the unlisted {place} before heading back",
                       "Take a look at the unlisted {place}, then return to launch", "Visit the unlisted {place} and come back to base",
                       "Check the unlisted {place} for activity before flying home", "Look over the unlisted {place}, then head back to base",
                       "Review the unlisted {place} and return to the launch point", "Assess the unlisted {place}, then come back")
        variant = index % len(variants)
        use_zh = (index // len(variants)) % 2 == 0
        text = (f"去地图未标出的{zh}{variants[variant]}。" if use_zh else
                en_variants[variant].format(place=en) + ".")
        ref = "poi:" + re.sub(r"\s+", "_", en.strip().lower())
        candidate = {"plan_version": 1, "goal": text[:160],
                     "steps": [S("s1", "goto", {"target_ref": ref}, "s2", "s2"), H("s2")],
                     "report": ["status"]}
        # The known alternative is deliberately present; the target must not use it.
        refs = (f"poi:{known} ({known_zh} / {known_en})",)
        emit(text, refs, candidate, "r24_unknown_place", valid=False, finding="ungrounded_place")
        index += 1
    index = 0
    while sum(row["family"] in {"r24_context_land", "r24_context_return"} for row in rows) < context_count:
        if index > 10_000:
            raise ValueError("r24 context template pool exhausted; reduce --r24-context-count")
        action_land = index % 2 == 0
        spots = land_spots if action_land else return_spots
        place_zh, place_en = spots[(index // 2) % len(spots)]
        variant_index = (index // (len(spots) * 2)) % 6
        use_zh = (index // (len(spots) * 12)) % 2 == 0
        if action_land:
            variants = ("就在{p}降落。", "直接在{p}着陆。", "不用去别处，{p}落地。", "在{p}降落就行。",
                        "{p}原位降落，不要移动。", "现在就在{p}着陆。")
            en_variants = ("Land {p}.", "Set down {p}.", "Touch down {p}, no need to move.",
                           "Just land {p}.", "Stay put and land {p}.", "Bring it down {p} now.")
            text = (variants[variant_index].format(p=place_zh) if use_zh else
                    en_variants[variant_index].format(p=place_en))
            plan = {"plan_version": 1, "goal": text[:160], "steps": [L("s1")], "report": ["status"]}
            family = "r24_context_land"
        else:
            variants = ("直接飞回{p}。", "任务到此结束，回到{p}。", "现在返回{p}。", "不再停留，飞回{p}。",
                        "收工，回{p}。", "立刻回{p}。")
            en_variants = ("Fly straight back to {p}.", "We are done here; return to {p}.",
                           "Head back to {p} now.", "Return to {p} without stopping.",
                           "Wrap up and come back to {p}.", "Go back to {p} right away.")
            text = (variants[variant_index].format(p=place_zh) if use_zh else
                    en_variants[variant_index].format(p=place_en))
            plan = {"plan_version": 1, "goal": text[:160], "steps": [H("s1")], "report": ["status"]}
            family = "r24_context_return"
        emit(text, (), plan, family)
        index += 1
    return rows


def r25_rows(args, held_out: set[str], seen: set[str]) -> list[dict]:
    """Add payload capability contrasts and negative-scope query contrasts."""
    rng = random.Random(f"{args.seed}:r25")
    rows = []
    refs_by_place = [
        ("poi:gate", "大门", "gate"), ("poi:pad", "起降坪", "landing pad"),
        ("poi:tower", "水塔", "water tower"), ("poi:shed", "仓库", "shed"),
        ("poi:bridge", "桥", "bridge"), ("poi:yard", "院子", "yard"),
        ("poi:hangar", "机库", "hangar"), ("poi:warehouse", "仓储棚", "warehouse"),
    ]
    payloads = [
        ("包裹", "parcel", "package"), ("药品箱", "medical case", "medical kit"),
        ("工具箱", "tool case", "toolkit"), ("样品盒", "sample box", "specimen case"),
    ]
    payload_zh_verbs = (
        "把{payload}送到{place}并交付", "将{payload}投放至{place}",
        "携带{payload}前往{place}完成投递", "在{place}空投{payload}",
    )
    payload_en_verbs = (
        "Deliver the {payload} to the {place}", "Drop off the {payload} at the {place}",
        "Carry the {payload} to the {place} for delivery",
        "Make an airdrop of the {payload} at the {place}",
    )
    endings = (
        ("然后返航。", "and then return home."),
        ("，完成后回到起飞点。", "and come back to the launch point."),
        ("，交付后返回基地。", "and head back to base after delivery."),
        ("，完成投递再返航。", "and fly home once the drop is complete."),
    )

    def emit(text, refs, plan, family, capabilities, *, valid=True, finding=None):
        key = normalize_task(text)
        if key in held_out or key in seen:
            return False
        seen.add(key)
        row = {
            "sample_id": f"plan-r25-{family}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]}",
            "kind": "mission_plan", "world": "synthetic_plan_r25", "family": family,
            "task_text": text, "refs": list(refs), "capabilities": list(capabilities),
            "expected_valid": valid, "expected_findings": [] if valid else [finding],
            "prompt": plan_prompt(text, refs=refs, prompt_variant=args.prompt_variant,
                                  capabilities=frozenset(capabilities)),
            "assistant_target": plan,
        }
        if not valid:
            row["rejected_candidate"] = plan
            row["negative_target_mode"] = "rejected_candidate"
        rows.append(row)
        return True

    # Each requested count is a pair: one payload-capable positive and one
    # missing-capability faithful candidate.
    index = 0
    attempts = 0
    payload_pairs = 0
    while payload_pairs < args.r25_missing_payload_count:
        if attempts >= 2400:
            raise ValueError("r25 missing-payload template pool exhausted; reduce --r25-missing-payload-count")
        place_ref, place_zh, place_en = refs_by_place[index % len(refs_by_place)]
        payload_zh, payload_en, payload_alt = payloads[(index // len(refs_by_place)) % len(payloads)]
        phrase = (index // (len(refs_by_place) * len(payloads))) % len(payload_zh_verbs)
        ending_zh, ending_en = endings[(index // (len(refs_by_place) * len(payloads) * len(payload_zh_verbs))) % len(endings)]
        use_zh = (index // 3) % 2 == 0
        base = (
            payload_zh_verbs[phrase].format(payload=payload_zh, place=place_zh)
            if use_zh else
            payload_en_verbs[phrase].format(
                payload=payload_alt if phrase % 2 else payload_en, place=place_en
            )
        )
        suffix = ending_zh if use_zh else " " + ending_en
        text = base + suffix
        alt_phrase = (phrase + 1) % len(payload_zh_verbs)
        positive_base = (
            payload_zh_verbs[alt_phrase].format(payload=payload_zh, place=place_zh)
            if use_zh else
            payload_en_verbs[alt_phrase].format(payload=payload_en, place=place_en)
        )
        positive_text = positive_base + suffix
        steps = [
            S("s1", "deliver", {
                "target_ref": place_ref,
                "payload_ref": f"payload:{payload_en.replace(' ', '_')}",
            }, "s2", "s2"),
            H("s2"),
        ]
        refs = (f"{place_ref} ({place_zh} / {place_en})",)
        plan = {
            "plan_version": 1, "goal": text[:160], "steps": steps, "report": ["delivery"],
        }
        if normalize_task(positive_text) not in held_out | seen and normalize_task(text) not in held_out | seen:
            emit(
                positive_text, refs, {**plan, "goal": positive_text[:160]},
                "r25_payload_capable", ("payload_drop",),
            )
            emit(text, refs, plan, "r25_missing_payload", (),
                 valid=False, finding="missing_capability")
            payload_pairs += 1
        index += 1
        attempts += 1

    affirmative = [
        ("入口标志", "entrance sign"), ("路障", "road barrier"),
        ("警示牌", "warning sign"), ("停放的自行车", "parked bicycle"),
        ("红色轿车", "red car"), ("行人", "pedestrian"),
        ("交通锥", "traffic cone"), ("屋顶天线", "rooftop antenna"),
    ]
    denied = [
        ("救护车", "ambulance", "救护车"), ("消防车", "fire truck", "消防车"),
        ("校车", "school bus", "校车"), ("摩托车", "motorcycle", "摩托车"),
        ("货车", "truck", "货车"), ("狗", "dog", "狗"),
        ("自行车", "bicycle", "自行车"), ("行人", "pedestrian", "行人"),
    ]
    zh_patterns = (
        "检查{place}的{wanted}，不要凭空检查{denied}。",
        "查看{place}处的{wanted}；{denied}不在检查范围内。",
        "请核验{wanted}，别把{denied}当成目标，位置是{place}。",
        "到{place}确认{wanted}，无需查询{denied}。",
    )
    en_patterns = (
        "Inspect the {place} for the {wanted}; do not invent {denied_article} {denied} target.",
        "Check the {wanted} at the {place}, not the {denied}.",
        "Verify the {wanted} near the {place}; leave the {denied} out of scope.",
        "Look for the {wanted} at the {place}, and do not query for {denied_article} {denied}.",
    )
    query_count = 0
    index = 0
    attempts = 0
    while query_count < args.r25_negated_query_count:
        if attempts >= 2400:
            raise ValueError("r25 negated-query template pool exhausted; reduce --r25-negated-query-count")
        place_ref, place_zh, place_en = refs_by_place[
            (index // (len(affirmative) * len(denied) * len(zh_patterns))) % len(refs_by_place)
        ]
        wanted_zh, wanted_en = affirmative[index % len(affirmative)]
        denied_zh, denied_en, denied_zh_query = denied[(index // len(affirmative)) % len(denied)]
        use_zh = index % 2 == 0
        pattern = index % len(zh_patterns)
        text = (
            zh_patterns[pattern].format(place=place_zh, wanted=wanted_zh, denied=denied_zh)
            if use_zh else
            en_patterns[pattern].format(
                place=place_en, wanted=wanted_en, denied=denied_en,
                denied_article="an" if denied_en[:1].lower() in "aeiou" else "a",
            )
        )
        refs = (f"{place_ref} ({place_zh} / {place_en})",)
        positive_pattern = (pattern + 1) % len(zh_patterns)
        good_text = (
            zh_patterns[positive_pattern].format(
                place=place_zh, wanted=wanted_zh, denied=denied_zh
            ) if use_zh else
            en_patterns[positive_pattern].format(
                place=place_en, wanted=wanted_en, denied=denied_en,
                denied_article="an" if denied_en[:1].lower() in "aeiou" else "a",
            )
        )
        positive = {
            "plan_version": 1, "goal": good_text[:160],
            "steps": [
                S("s1", "inspect", {
                    "target_ref": place_ref, "query": wanted_en, "standoff_m": 5, "views": 1,
                }, "s2", "s2"),
                H("s2"),
            ],
            "report": ["inspection"],
        }
        negative = {
            "plan_version": 1, "goal": text[:160],
            "steps": [
                S("s1", "inspect", {
                    "target_ref": place_ref,
                    "query": denied_en if not use_zh else denied_zh_query,
                    "standoff_m": 5, "views": 1,
                }, "s2", "s2"),
                H("s2"),
            ],
            "report": ["inspection"],
        }
        if emit(good_text, refs, positive, "r25_negated_query", ()):
            if emit(text, refs, negative, "r25_negated_query", (),
                    valid=False, finding="ungrounded_query"):
                query_count += 1
            else:
                seen.discard(normalize_task(good_text))
                rows.pop()
        index += 1
        attempts += 1
    return rows


V5_FAMILIES = (
    "conditional_branch", "two_targets", "count_stop", "alert_action", "evidence_only",
    "colloquial",
)
V5_OBJECTS = [*OBJECTS, ("blue backpack", "蓝色背包"), ("yellow bicycle", "黄色自行车")]
EN_NUMBERS = dict(enumerate((
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen", "twenty",
))) | {30: "thirty", 40: "forty", 45: "forty-five", 60: "sixty", 90: "ninety"}


def normalize_task(text: str) -> str:
    """V5 leakage key: NFKC, case, punctuation/space, and zh/en number words.

    Deliberately lexical, not a claim of semantic/paraphrase decontamination.
    The legacy whitespace-only filter is unchanged for byte reproducibility.
    """
    text = unicodedata.normalize("NFKC", text).casefold()

    def chinese(match):
        total = digit = 0
        for char in match.group():
            if char in "十百千":
                total += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[char]
                digit = 0
            else:
                digit = "零一二三四五六七八九".index({"两": "二", "〇": "零"}.get(char, char))
        return str(total + digit)

    text = re.sub(r"[零〇一二两三四五六七八九十百千]+", chinese, text)
    # Treat hyphenated compound numbers the same as their spaced spelling.
    text = re.sub(r"[-‐‑–—]", " ", text)
    units = {word: n for n, word in EN_NUMBERS.items() if n <= 20 or n % 10 == 0}
    units |= {"fifty": 50, "seventy": 70, "eighty": 80, "hundred": 100}
    words = "|".join(units)

    def english(match):
        total = 0
        for word in match.group().split():
            total = (total or 1) * 100 if word == "hundred" else total + units[word]
        return str(total)

    text = re.sub(rf"\b(?:{words})(?:\s+(?:{words}))*\b", english, text)
    return "".join(char for char in text if char.isalnum())


def load_replay_rows(path: Path, held_out: set[str]) -> list[dict]:
    """Load prior planner rows verbatim, rejecting held-out or duplicate tasks."""
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("kind") != "mission_plan":
            continue
        text = str(row.get("task_text", "")).strip()
        key = normalize_task(text)
        if not text or key in held_out:
            raise ValueError(f"replay row is held out or empty: {text!r}")
        rows.append(dict(row))
    if not rows:
        raise ValueError(f"no mission_plan rows found in replay file: {path}")
    return rows


def known_ref_ids(refs) -> set[str]:
    return {str(ref).split(" ", 1)[0] for ref in refs or ()}


def target_ref_ids(plan: dict) -> set[str]:
    return {
        str(step.get("params", {}).get("target_ref"))
        for step in plan.get("steps", ())
        if step.get("params", {}).get("target_ref") is not None
    }


def assert_no_unknown_place_substitution(row: dict) -> None:
    """Unknown-place negatives must retain an unresolved target ref."""
    if row.get("expected_valid") is not False or "ungrounded_place" not in row.get("expected_findings", ()):
        return
    known = known_ref_ids(row.get("refs", ()))
    substituted = sorted(target_ref_ids(row.get("assistant_target", {})) & known)
    if substituted:
        raise ValueError(
            f"{row.get('sample_id')} substitutes unknown place with known ref(s): {substituted}"
        )


def plural_query(query: str) -> str:
    """Pluralise the small controlled v5 vocabulary, preserving modifiers."""
    if query.endswith("person"):
        return query.removesuffix("person") + "people"
    return query + ("es" if query.endswith(("s", "x")) else "s")


def mission_v5(rng: random.Random, family: str) -> tuple[str, str, list[dict], list[str]]:
    """Minority v5 additions. Watch/patrol completion is NOT an alert event edge."""
    q, qz = rng.choice(V5_OBJECTS)
    qs = plural_query(q)
    z, zz, ze = rng.choice(ZONES)
    p, pz, pe = rng.choice(POIS)
    sec = rng.choice([5, 10, 15, 20, 30, 45, 60])
    sn = rng.choice([str(sec), EN_NUMBERS[sec]])
    zend, eend = rng.choice([
        ("然后回来", "then come back"), ("最后回起点", "then return to launch"),
        ("办完就返航", "head home afterward"), ("弄好就地降落", "then land here"),
    ])
    terminal = L if "降落" in zend else H
    if family == "conditional_branch":
        zh = rng.choice([
            f"在{zz}瞅瞅有没有{qz}，找到就拍照，找不到就去{pz}看看",
            f"帮忙在{zz}找{qz}，有就留张照片，没找着就到{pz}瞄一眼",
            f"先在{zz}找{qz}；找着了存图，没找着换到{pz}看看",
        ]) + f"，{zend}"
        en = rng.choice([
            f"Check {ze} for a {q}; if found take a photo, otherwise go look at {pe}",
            f"Look for the {q} in {ze}; snap it if found, or head to {pe} if absent",
        ]) + f", {eend}."
        steps = [
            S("s1", "area_search", {"region": z, "query": q}, "s2", "s3"),
            S("s2", "record", {"query": q}, "s9", "s9"),
            S("s3", "goto", {"target_ref": p}, "s9", "s9"), terminal("s9"),
        ]
        return zh, en, steps, ["findings", "evidence", "positions"]
    if family == "two_targets":
        q2, qz2 = rng.choice([obj for obj in V5_OBJECTS if obj[0] != q])
        if rng.random() < 0.2:
            # Anchor the motivating colour/target switch as well as random pairs.
            q, qz, q2, qz2 = "red car", "红车", "blue backpack", "蓝色背包"
        photo = rng.choice([False, True])
        zh = rng.choice([
            f"先在{zz}找{qz}，再找{qz2}，第一个没找着也继续找第二个",
            f"去{zz}瞅瞅，先找{qz}再找{qz2}，前一个找不到也别停",
            f"在{zz}按顺序找{qz}和{qz2}，头一个落空也接着找后一个",
        ]) + ("，找到哪个就给哪个拍照" if photo else "") + f"，{zend}"
        en = (f"In {ze}, look for the {q} first, then the {q2}; "
              "try the second even if the first is missing"
              + ("; photograph each one you find" if photo else "") + f", {eend}.")
        if photo:
            steps = [
                S("s1", "area_search", {"region": z, "query": q}, "s2", "s3"),
                S("s2", "record", {"query": q}, "s3", "s3"),
                S("s3", "area_search", {"region": z, "query": q2}, "s4", "s9"),
                S("s4", "record", {"query": q2}, "s9", "s9"), terminal("s9"),
            ]
        else:
            steps = [
                S("s1", "area_search", {"region": z, "query": q}, "s2", "s2"),
                S("s2", "area_search", {"region": z, "query": q2}, "s9", "s9"),
                terminal("s9"),
            ]
        return zh, en, steps, ["findings", "positions", *(["evidence"] if photo else [])]
    if family == "count_stop":
        n = rng.choice([2, 3, 5, 8, 10, 12, 15, 20])
        nn = rng.choice([str(n), EN_NUMBERS[n]])
        # Stop counting at the threshold; budget exhaustion still returns safely.
        zh = rng.choice([
            f"在{zz}数{qz}，数到{n}个就别数了",
            f"帮我清点{zz}的{qz}，凑够{n}个就收工",
            f"去{zz}数数{qz}，最多{n}个，够数就停",
        ]) + f"，{zend}"
        en = rng.choice([
            f"Count {qs} in {ze}, stop as soon as you reach {nn}",
            f"Tally {qs} in {ze} up to {nn}, stop counting at that limit",
        ]) + f", {eend}."
        return zh, en, [
            S("s1", "count", {"region": z, "query": q, "max_count": n}, "s9", "s9"),
            terminal("s9"),
        ], ["count", "positions"]
    if family == "alert_action":
        mode = rng.choice(["search", "monitor", "patrol"])
        prefix = []
        zh_start, en_start = f"在{zz}瞄一眼有没有{qz}", f"Look in {ze} for a {q}"
        if mode == "monitor":
            mins = rng.choice([1, 2, 3, 5, 8])
            mn = rng.choice([str(mins), EN_NUMBERS[mins]])
            prefix = [S("s1", "monitor", {"region": z, "query": q, "duration_s": mins * 60},
                        "s2", "s9")]
            zh_start = f"盯着{zz}的{qz}{mins}分钟，时间到了再搜一下{qz}"
            en_start = (f"Watch {ze} for {qs} for {mn} minutes; "
                        f"afterward search there for a {q}")
        elif mode == "patrol":
            r, rz, re_ = rng.choice(ROUTES)
            loops = rng.choice([1, 2, 3, 5])
            ln = rng.choice([str(loops), EN_NUMBERS[loops]])
            prefix = [S("s1", "patrol", {"route_ref": r, "query": q, "loops": loops},
                        "s2", "s9")]
            zh_start = f"沿{rz}溜{loops}圈留意{qz}，巡完再搜一下{zz}的{qz}"
            en_start = (f"Patrol {re_} for {ln} loops watching for {qs}; "
                        f"when done search {ze} for a {q}")
        zh = (f"{zh_start}，看到就悬停{sec}秒再拍照，没找到直接结束，{zend}")
        en = (f"{en_start}; if found hover for {sn} seconds and take a photo; "
              f"if absent skip those actions, {eend}.")
        return zh, en, [*prefix,
            S("s2" if prefix else "s1", "area_search", {"region": z, "query": q},
              "s3", "s9"),
            S("s3", "hover", {"duration_s": sec}, "s4", "s9"),
            S("s4", "record", {"query": q}, "s9", "s9"), terminal("s9"),
        ], ["alerts", "findings", "evidence"]
    if family == "evidence_only":
        q, qz = rng.choice([*V5_OBJECTS, ("current view", "当前画面")])
        zh = rng.choice([
            f"眼前的{qz}拍一下就好，不用再找",
            f"帮我把现在看到的{qz}存张图，别去搜索",
            f"就给镜头里的{qz}留个影，别满场跑去找",
            f"把当前看到的{qz}拍下来留底，不必搜索",
        ]) + f"，{zend}"
        en = rng.choice([
            f"Snap the {q} already in view; no search needed",
            f"Save a still of the {q} visible right now, without searching",
            f"Capture the {q} in the current camera image; skip searching",
        ]) + f", {eend}."
        if q == "current view":
            zh = rng.choice([
                "帮我拍一下现在看到的画面，不用搜索",
                "当前画面存张图就好，别去找东西",
                "就把眼前的画面拍下来留底，不必搜索",
                "现在镜头里看到的拍一张就行，别满场跑去找",
            ]) + f"，{zend}"
            en = rng.choice([
                "Take a photo of what you see right now; no search needed",
                "Save the current camera view without searching",
                "Capture this view as evidence; skip searching",
            ]) + f", {eend}."
        return zh, en, [S("s1", "record", {"query": q}, "s9", "s9"), terminal("s9")], [
            "evidence",
        ]
    if family == "colloquial":
        mode = rng.choice(["look", "photo", "patrol", "monitor", "goto"])
        if mode == "patrol":
            r, rz, re_ = rng.choice(ROUTES)
            verb = rng.choice(["转一圈", "溜一圈", "兜一圈"])
            zh = f"沿{rz}{verb}，留意{qz}，{zend}"
            en = f"Do one lap of {re_}, keep an eye out for {qs}, {eend}."
            return zh, en, [
                S("s1", "patrol", {"route_ref": r, "query": q, "loops": 1}, "s9", "s9"),
                terminal("s9"),
            ], ["alerts"]
        if mode == "monitor":
            zh = f"帮我盯着{zz}的{qz}{sec}秒，{zend}"
            en = f"Keep tabs on {qs} in {ze} for {sn} seconds, {eend}."
            return zh, en, [
                S("s1", "monitor", {"region": z, "query": q, "duration_s": sec},
                  "s9", "s9"), terminal("s9"),
            ], ["alerts"]
        if mode == "goto":
            zh = f"去{pz}瞄一眼，在那儿停{sec}秒，{zend}"
            en = f"Pop over to {pe} and hang there for {sn} seconds, {eend}."
            return zh, en, [
                S("s1", "goto", {"target_ref": p}, "s2", "s9"),
                S("s2", "hover", {"duration_s": sec}, "s9", "s9"), terminal("s9"),
            ], ["status"]
        verb = rng.choice(["瞅瞅有没有", "瞄一眼有没有", "找找看有没有", "帮忙瞧瞧有没有"])
        zh = f"在{zz}{verb}{qz}" + ("，找着了顺手拍一张" if mode == "photo" else "")
        en = f"Have a look around {ze} for a {q}" + (
            "; grab a snapshot if you spot it" if mode == "photo" else ""
        )
        steps = [S("s1", "area_search", {"region": z, "query": q},
                   "s2" if mode == "photo" else "s9", "s9")]
        if mode == "photo":
            steps.append(S("s2", "record", {"query": q}, "s9", "s9"))
        return f"{zh}，{zend}", f"{en}, {eend}.", [*steps, terminal("s9")], [
            "findings", *(["evidence"] if mode == "photo" else ["positions"]),
        ]
    raise ValueError(f"unknown v5 family: {family}")


def _labelled_refs_match(before: list[dict], after: list[dict], refs, labels) -> bool:
    """Reject legacy alias collisions that would bind a place to the wrong label."""
    known = {item.split(" (", 1)[0]: item.split(" (", 1)[1][:-1] for item in refs}
    return all(
        old not in labels or known.get(new_step["params"].get(key)) == labels[old]
        for old_step, new_step in zip(before, after, strict=True)
        for key, old in old_step["params"].items()
        if key in {"region", "target_ref", "route_ref"}
    )


def v5_plan_prompt(text: str, refs, prompt_variant: str, held_out: set[str], *, capabilities=frozenset()) -> str:
    """Remove held-out few-shot tasks as well as filtering generated task texts.

    One legacy default example is itself an eval task. V4 retains its historical
    prompt; v5 keeps the same rules and all non-held-out examples.
    """
    prompt = plan_prompt(text, refs=refs, prompt_variant=prompt_variant,
                         capabilities=frozenset(capabilities))
    for example_text, example_plan in EXAMPLES:
        if normalize_task(example_text) in held_out:
            prompt = prompt.replace(
                f"Task: {example_text}\nPlan: {json.dumps(example_plan, ensure_ascii=False)}\n",
                "",
            )
    return prompt


def v5_rows(args, held_out: set[str], labels) -> list[dict]:
    """70% unchanged v4 candidates, 30% additions, independently per split.

    RNG streams isolate the v4 backbone, additions, and ordering. Accepted legacy
    rows retain their original text, id and target. Prompt examples are filtered
    for eval leakage, and family metadata is added.
    """
    backbone_rng = random.Random(args.seed)
    extra_rng = random.Random(f"{args.seed}:v5:additions")
    order_rng = random.Random(f"{args.seed}:v5:order")
    held_out = {normalize_task(text) for text in held_out}
    seen, rows = set(), []
    for size in (args.rows, args.val_rows):
        additions = (size * 3 + 5) // 10
        schedule = ["v4_backbone"] * (size - additions) + [
            V5_FAMILIES[i % len(V5_FAMILIES)] for i in range(additions)
        ]
        order_rng.shuffle(schedule)
        for family in schedule:
            for _ in range(10_000):
                rng = backbone_rng if family == "v4_backbone" else extra_rng
                if family == "v4_backbone":
                    generator = mission_v3 if rng.random() < 1 / 3 else mission
                    zh, en, steps, report = generator(rng)
                else:
                    zh, en, steps, report = mission_v5(rng, family)
                if rng.random() < 0.5:
                    zh = re.sub(r"\d+", lambda m: zh_number(int(m.group())), zh)
                text = " ".join((zh if rng.random() < 0.5 else en).split())
                before = [{**step, "params": dict(step["params"])} for step in steps]
                steps, refs = localise_places(rng, steps, text, labels)
                key = normalize_task(text)
                if key in held_out or key in seen:
                    continue
                if not _labelled_refs_match(before, steps, refs, labels):
                    continue
                plan = {"plan_version": 1, "goal": en.rstrip(".")[:160],
                        "steps": steps, "report": report}
                validate_mission_plan(plan)
                _, _, unknown = check_plan_refs(plan, refs, substitute=False)
                if unknown:
                    raise ValueError(f"unlabelled refs in v5 row: {unknown}")
                seen.add(key)
                sample_id = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
                rows.append({
                    "sample_id": f"plan-{sample_id}", "kind": "mission_plan",
                    "world": "synthetic_plan", "family": family, "task_text": text,
                    "prompt": v5_plan_prompt(text, refs, args.prompt_variant, held_out,
                                              capabilities=frozenset()),
                    "assistant_target": plan,
                })
                break
            else:
                raise ValueError(f"v5 template pool exhausted for {family}")
    return rows


def r20_intent_rows(args, held_out: set[str], labels) -> list[dict]:
    """Small paired intent set: return-home corrections and unknown-place rejects."""
    refs = ("poi:gate (大门 / the gate)", "poi:pad (起降坪 / the pad)")
    cases = [
        ("到大门检查位置，然后返航", "Go to the gate, inspect the position, then return home.",
         [S("s1", "goto", {"target_ref": "poi:gate"}, "s2", "s2"),
          S("s2", "land", {"target_ref": "poi:gate"}, None, None)], "bad_return"),
        ("到大门检查位置，然后返航", "Go to the gate, inspect the position, then return home.",
         [S("s1", "goto", {"target_ref": "poi:gate"}, "s2", "s2"), H("s2")], "good_return"),
        ("去一个地图没有的Raven Pier，不要把它替换成大门", "Go to the unknown Raven Pier; do not substitute the gate.",
         [S("s1", "goto", {"target_ref": "poi:gate"}, None, None)], "bad_unknown_place"),
        ("去大门查看，然后回到起飞点", "Go to the gate, check it, then return to the launch point.",
         [S("s1", "goto", {"target_ref": "poi:gate"}, "s2", "s2"), H("s2")], "good_return_alias"),
    ]
    rows = []
    for index, (zh, en, steps, family) in enumerate(cases):
        text = zh if index % 2 == 0 else en
        plan = {"plan_version": 1, "goal": en, "steps": steps, "report": ["status"]}
        findings = check_plan_intent(plan, text, refs)
        expected_bad = family.startswith("bad_")
        if expected_bad and not findings:
            raise ValueError(f"r20 intent case was not rejected: {family}")
        if not expected_bad and findings:
            raise ValueError(f"r20 intent case was rejected: {family}: {findings}")
        if normalize_task(text) in held_out:
            continue
        rows.append({"sample_id": f"plan-r20-intent-{index}", "kind": "mission_plan",
                     "world": "synthetic_plan_r20", "family": family,
                     "task_text": text,
                     "prompt": plan_prompt(text, refs=refs, prompt_variant=args.prompt_variant,
                                           capabilities=frozenset()),
                     "assistant_target": plan})
    return rows


def cx_r20_rows(args, held_out: set[str]) -> list[dict]:
    """Paired correct/incorrect examples for planner intent and capability gates."""
    refs = ("poi:gate (大门 / gate)", "poi:pad (起降坪 / landing pad)",
            "zone:lot (停车场 / parking lot)")
    cases = [
        ("请到大门检查，然后回到出发点", "return_intent", True,
         [S("s1", "goto", {"target_ref":"poi:gate"}, "s2", "s2"), H("s2")], (), None),
        ("Check the gate and head back to where you took off.", "return_intent", True,
         [S("s1", "goto", {"target_ref":"poi:gate"}, "s2", "s2"), H("s2")], (), None),
        ("去大门看一眼，不用返航，就地降落", "return_intent", True,
         [S("s1", "goto", {"target_ref":"poi:gate"}, "s2", "s2"), L("s2")], (), None),
        ("Inspect the gate; do not return home, land here.", "return_intent", True,
         [S("s1", "goto", {"target_ref":"poi:gate"}, "s2", "s2"), L("s2")], (), None),
        ("到地图没有的Raven Pier巡查", "unknown_place", False,
         [S("s1", "goto", {"target_ref":"poi:gate"}, None, None)], (), "ungrounded_place"),
        ("Fly to a nonexistent Raven Pier, do not substitute the gate.", "unknown_place", False,
         [S("s1", "goto", {"target_ref":"poi:gate"}, None, None)], (), "ungrounded_place"),
        ("把包裹投到大门", "capability_payload", False,
         [S("s1", "deliver", {"target_ref":"poi:gate"}, None, None)], (), "missing_capability"),
        ("Drop the parcel at the gate.", "capability_payload", False,
         [S("s1", "deliver", {"target_ref":"poi:gate"}, None, None)], (), "missing_capability"),
        ("固定翼在停车场上空悬停", "capability_fixed_wing", False,
         [S("s1", "hover", {"region":"zone:lot"}, None, None)], ("airframe:fixed_wing",), "missing_capability"),
        ("Fixed wing: hover over the parking lot.", "capability_fixed_wing", False,
         [S("s1", "hover", {"region":"zone:lot"}, None, None)], ("airframe:fixed_wing",), "missing_capability"),
        ("固定翼飞到大门上方定点降落", "capability_fixed_wing", False,
         [S("s1", "land", {"target_ref":"poi:gate"}, None, None)], ("airframe:fixed_wing",), "missing_capability"),
        ("A fixed-wing aircraft should land precisely on the gate.", "capability_fixed_wing", False,
         [S("s1", "land", {"target_ref":"poi:gate"}, None, None)], ("airframe:fixed_wing",), "missing_capability"),
        ("到大门核实入口，随后返回home", "ref_alias", True,
         [S("s1", "goto", {"target_ref":"poi:gate"}, "s2", "s2"), H("s2")], (), None),
        ("Go to the landing pad, inspect it, and come back to base.", "ref_alias", True,
         [S("s1", "goto", {"target_ref":"poi:pad"}, "s2", "s2"), H("s2")], (), None),
        ("数停车场里的红色轿车，然后返航", "query_grounding", True,
         [S("s1", "count", {"region":"zone:lot", "query":"red car"}, "s2", "s2"), H("s2")], (), None),
        ("Count the bicycles in the parking lot and return home.", "query_grounding", True,
         [S("s1", "count", {"region":"zone:lot", "query":"bicycle"}, "s2", "s2"), H("s2")], (), None),
        ("检查大门并返航", "query_grounding", False,
         [S("s1", "inspect", {"target_ref":"poi:gate", "query":"ambulance"}, "s2", "s2"), H("s2")], (), "ungrounded_query"),
        ("Inspect the gate and return home.", "query_grounding", False,
         [S("s1", "inspect", {"target_ref":"poi:gate", "query":"fire truck"}, "s2", "s2"), H("s2")], (), "ungrounded_query"),
    ]
    rows = []
    for i, (text, family, valid, steps, caps, expected_code) in enumerate(cases):
        plan = {"plan_version":1, "goal":text, "steps":steps, "report":[]}
        if steps[-1]["skill"] not in {"return_home", "land"}:
            terminal_id = f"s{len(steps) + 1}"
            steps[-1]["on_success"] = terminal_id
            steps[-1]["on_fail"] = terminal_id
            steps.append(H(terminal_id))
            plan["steps"] = steps
        if valid:
            validate_mission_plan(plan, capabilities=frozenset(capabilities))
        intent = check_plan_intent(plan, text, refs)
        capability = capability_violations(plan, caps, airframe=("airframe:fixed_wing" if caps else None))
        codes = {f.code for f in intent} | {f["code"] for f in capability}
        if valid and codes:
            raise ValueError(f"invalid positive {family}: {sorted(codes)}")
        if not valid and expected_code not in codes:
            raise ValueError(f"negative case not caught {family}: {sorted(codes)}")
        rows.append({"sample_id":f"plan-cx-r20-{i:02d}", "kind":"mission_plan",
                     "world":"synthetic_plan_cx_r20", "family":family,
                     "task_text":text, "refs":list(refs), "capabilities":list(caps),
                     "expected_valid":valid, "expected_findings":[] if valid else [expected_code],
                     "prompt":plan_prompt(text, refs=refs, prompt_variant=args.prompt_variant,
                                          capabilities=frozenset(caps)),
                     "assistant_target":plan})
    return rows


def r20_plan_intent_rows(args, held_out: set[str], labels) -> list[dict]:
    """Generate deterministic paired intent/capability examples at scale.

    ``r20_plan_intent_count`` is the number of positive/negative pairs per
    family.  Every accepted row is checked before it is emitted, and the
    normalized task text is kept disjoint from the hand-written eval corpus.
    """
    pairs = int(getattr(args, "r20_plan_intent_count", 40))
    if pairs < 1:
        raise ValueError("r20 plan-intent pair count must be positive")
    rng = random.Random(f"{args.seed}:r20-plan-intent")
    places = [
        ("poi:gate", "大门", "gate", "entrance"),
        ("poi:pad", "起降坪", "landing pad", "pad"),
        ("poi:tower", "水塔", "water tower", "tower"),
        ("poi:shed", "仓库", "shed", "warehouse"),
    ]
    zones = [("zone:lot", "停车场", "parking lot"), ("zone:field", "场地", "field"),
             ("zone:yard", "院子", "yard")]
    fake_places = [("Raven Pier", "Raven码头"), ("North Annex", "北侧附楼"),
                   ("Quartz Point", "Quartz观测点"), ("Cedar Wharf", "Cedar码头")]
    rows = []

    def refs_for(ref, zh, en, alias):
        # The slash-separated alias is the same ref_labels representation used
        # by mission packages; the task deliberately uses the alias term.
        return (f"{ref} ({zh} / {en} / {alias})",) + tuple(
            f"{z[0]} ({z[1]} / {z[2]})" for z in zones
        )

    def add_pair(index, family, positive, negative, expected_code=None):
        for polarity, (text, plan, refs, capabilities, valid) in enumerate((positive, negative)):
            normalized = normalize_task(text)
            role = "positive" if polarity == 0 else "paired_negative"
            if normalized in held_out:
                return False
            if valid:
                validate_mission_plan(plan, capabilities=frozenset(capabilities))
            findings = check_plan_intent(plan, text, refs)
            cap_findings = capability_violations(
                plan, capabilities,
                airframe="airframe:fixed_wing" if "airframe:fixed_wing" in capabilities else None,
            )
            codes = {finding.code for finding in findings} | {finding["code"] for finding in cap_findings}
            _, _, unknown_refs = check_plan_refs(plan, refs, substitute=False)
            if unknown_refs:
                codes.add("ungrounded_place")
            if valid and codes:
                raise ValueError(f"r20 positive {family} violates contract: {text}: {sorted(codes)}")
            if not valid and expected_code not in codes:
                raise ValueError(f"r20 negative {family} not caught: {text}: {sorted(codes)}")
            rejected_candidate = None
            if not valid:
                rejected_candidate = plan
                plan = {"plan_version": 1, "goal": f"Reject: {text[:160]}",
                        "steps": [H("s1")], "report": ["rejected"]}
            rows.append({
                "sample_id": f"plan-cx-r20-{family}-{index:04d}-{polarity}",
                "kind": "mission_plan", "world": "synthetic_plan_cx_r20",
                "family": family, "task_text": text, "refs": list(refs),
                "pair_role": role,
                "capabilities": list(capabilities), "expected_valid": valid,
                "expected_findings": [] if valid else [expected_code],
                "prompt": plan_prompt(text, refs=refs, prompt_variant=args.prompt_variant,
                                      capabilities=frozenset(capabilities)),
                "assistant_target": plan, "_key": normalized,
            })
            if rejected_candidate is not None:
                rows[-1]["rejected_candidate"] = rejected_candidate
                if args.negative_target_mode == "rejected_candidate":
                    rows[-1]["assistant_target"] = rejected_candidate
                    rows[-1]["negative_target_mode"] = args.negative_target_mode
                    assert_no_unknown_place_substitution(rows[-1])
        return True

    for index in range(pairs):
        ref, zh, en, alias = places[index % len(places)]
        zone, zone_zh, zone_en = zones[index % len(zones)]
        fake_en, fake_zh = fake_places[index % len(fake_places)]
        use_zh = index % 2 == 0
        # Return-home wording has independent Chinese and English variants.
        return_pos = (
            (f"到{zh}核查入口，{rng.choice(['然后返航', '再回到起飞点', '完成后回家'])}。",
             {"plan_version":1,"goal":"return","steps":[S("s1","goto",{"target_ref":ref},"s2","s2"),H("s2")],"report":[]},
             refs_for(ref, zh, en, alias), (), True)
            if use_zh else
            (f"Inspect the {en}, then {rng.choice(['return home', 'head back to the launch point', 'come back to base'])}.",
             {"plan_version":1,"goal":"return","steps":[S("s1","goto",{"target_ref":ref},"s2","s2"),H("s2")],"report":[]},
             refs_for(ref, zh, en, alias), (), True)
        )
        return_neg = (
            (f"到{zh}查看，不用返航，{rng.choice(['就地降落', '原地落地'])}。",
             {"plan_version":1,"goal":"land","steps":[S("s1","goto",{"target_ref":ref},"s2","s2"),L("s2")],"report":[]}, refs_for(ref, zh, en, alias), (), True)
            if use_zh else
            (f"Go to the {en}; do not return home, {rng.choice(['land here', 'touch down on site'])}.",
             {"plan_version":1,"goal":"land","steps":[S("s1","goto",{"target_ref":ref},"s2","s2"),L("s2")],"report":[]}, refs_for(ref, zh, en, alias), (), True)
        )
        add_pair(index, "return_intent", return_pos, return_neg)

        unknown_ref = f"poi:{re.sub(r'\s+', ' ', fake_en.strip().lower())}"
        known_named_ref = f"poi:{re.sub(r'\s+', '_', fake_en.strip().lower())}"
        unknown_plan = {"plan_version":1,"goal":"unknown","steps":[S("s1","goto",{"target_ref":unknown_ref},"s2","s2"),H("s2")],"report":[]}
        zh_suffix = ["看看，再返航。", "查看一下后返回起飞点。", "前往确认情况，然后回家。", "过去查看并返航。"][index % 4]
        en_suffix = ["and take a look, then return home.", "to check it and return to the launch point.", "to inspect it before heading back.", "for a look, then come back to base."][index % 4]
        unknown_text = (f"去{fake_zh}{zh_suffix}" if use_zh else f"Go to {fake_en} {en_suffix}")
        known_named_plan = {"plan_version":1,"goal":"return","steps":[S("s1","goto",{"target_ref":known_named_ref},"s2","s2"),H("s2")],"report":[]}
        named_refs = (f"{known_named_ref} ({fake_zh} / {fake_en})",)
        unknown_pos = (unknown_text, known_named_plan, named_refs, (), True)
        unknown_neg = (unknown_text, unknown_plan, refs_for(ref, zh, en, alias), (), False)
        add_pair(index, "unknown_place", unknown_pos, unknown_neg, "ungrounded_place")

        deliver_plan = {"plan_version":1,"goal":"deliver","steps":[S("s1","deliver",{"target_ref":ref,"payload_ref":"payload:parcel"},"s2","s2"),H("s2")],"report":[]}
        deliver_ok = (f"把包裹投到{zh}，然后返航。" if use_zh else f"Drop the parcel at the {en}, then return home.")
        deliver_bad = deliver_ok
        deliver_positive = (deliver_ok, deliver_plan, refs_for(ref, zh, en, alias), ("payload_drop",), True)
        deliver_negative = (deliver_bad, deliver_plan, refs_for(ref, zh, en, alias), (), False)
        add_pair(index, "missing_payload_drop", deliver_positive, deliver_negative, "missing_capability")

        hover_plan = {"plan_version":1,"goal":"hover","steps":[S("s1","hover",{"region":zone,"duration_s":60},"s2","s2"),H("s2")],"report":[]}
        fixed_safe = (f"在{zone_zh}上空悬停一分钟。" if use_zh else f"Hover over the {zone_en} for one minute.")
        fixed_bad = fixed_safe
        fixed_safe_plan = {"plan_version":1,"goal":"fixed wing transit","steps":[S("s1","goto",{"target_ref":zone},"s2","s2"),H("s2")],"report":[]}
        fixed_positive = (fixed_safe, hover_plan, refs_for(ref, zh, en, alias) + (f"{zone} ({zone_zh} / {zone_en})",), ("hover",), True)
        fixed_negative = (fixed_bad, hover_plan, refs_for(ref, zh, en, alias), ("airframe:fixed_wing",), False)
        add_pair(index, "fixed_wing_constraint", fixed_positive, fixed_negative, "missing_capability")

        land_plan = {"plan_version":1,"goal":"fixed wing landing","steps":[S("s1","land",{"target_ref":ref},None,None)],"report":[]}
        land_text = (f"在{zh}定点降落。" if use_zh else f"Land precisely on the {en}.")
        land_positive = (land_text, land_plan, refs_for(ref, zh, en, alias), ("landing_on_target",), True)
        land_bad = (land_text, land_plan, refs_for(ref, zh, en, alias), ("airframe:fixed_wing",), False)
        add_pair(index, "fixed_wing_target_land", land_positive, land_bad, "missing_capability")

        alias_text = unknown_text
        alias_plan = {"plan_version":1,"goal":"alias","steps":[S("s1","goto",{"target_ref":known_named_ref},"s2","s2"),H("s2")],"report":[]}
        alias_pair = (unknown_text, alias_plan, named_refs, (), True)
        alias_unknown_plan = {"plan_version":1,"goal":"alias","steps":[S("s1","goto",{"target_ref":unknown_ref},"s2","s2"),H("s2")],"report":[]}
        alias_bad = (alias_text, alias_unknown_plan, refs_for(ref, zh, en, alias), (), False)
        add_pair(index, "ref_alias", alias_pair, alias_bad, "ungrounded_place")

        query = "red car" if index % 2 else "bicycle"
        query_zh = "红色轿车" if query == "red car" else "自行车"
        count_plan = {"plan_version":1,"goal":"count","steps":[S("s1","count",{"region":zone,"query":query,"max_count":10},"s2","s2"),H("s2")],"report":[]}
        query_good = (f"数{zone_zh}里的{query_zh}，然后返航。" if use_zh else f"Count {query}s in the {zone_en} and return home.", count_plan, refs_for(ref, zh, en, alias) + (f"{zone} ({zone_zh} / {zone_en})",), (), True)
        normalized = normalize_task(query_good[0])
        if normalized not in held_out:
            rows.append({"sample_id": f"plan-cx-r20-query_grounding-{index:04d}-0", "kind":"mission_plan", "world":"synthetic_plan_cx_r20", "family":"query_grounding", "task_text":query_good[0], "refs":list(query_good[2]), "pair_role":"positive", "capabilities":[], "expected_valid":True, "expected_findings":[], "prompt":plan_prompt(query_good[0], refs=query_good[2], prompt_variant=args.prompt_variant), "assistant_target":query_good[1], "_key":normalized})

    for row in rows:
        row.pop("_key", None)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--replay-planner-train", type=Path, default=None,
        help="prepend mission_plan rows from a prior planner training split",
    )
    parser.add_argument("--rows", type=int, default=1200)
    parser.add_argument("--val-rows", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261015)
    parser.add_argument(
        "--prompt-variant", choices=PLAN_PROMPT_VARIANTS, default="default",
        help="experimental shorter training prompt (default preserves the trained prompt)",
    )
    # v3 alone drops the inspect/monitor/survey/home/land templates; v4 mixes it into v2.
    parser.add_argument("--version", choices=("v2", "v3", "v4", "v5"), default="v4")
    parser.add_argument(
        "--require-skills",
        nargs="*",
        choices=sorted(SKILLS),
        default=None,
        help="required training skills (default: all except deliver for v2/v4/v5, none for v3; "
        "pass without values to disable)",
    )
    parser.add_argument("--r20-intent-pairs", action="store_true",
                        help="append offline r20 return-home/unknown-place intent pairs")
    parser.add_argument("--r20-plan-intent", action="store_true",
                        help="emit paired r20 intent, grounding, alias, and capability audit rows")
    parser.add_argument(
        "--r20-plan-intent-count", type=int, default=40,
        help="positive/negative pairs per r20 intent family (default: 40)",
    )
    parser.add_argument(
        "--r20-plan-intent-mix", action="store_true",
        help="append the scalable r20 rows to the requested v4/v5 normal rows",
    )
    parser.add_argument(
        "--negative-target-mode", choices=("safe_rejection", "rejected_candidate"),
        default="safe_rejection",
        help="target format for expected-invalid rows; r22 uses rejected_candidate",
    )
    parser.add_argument(
        "--r23-recipe", action="store_true",
        help="add a v3-style coverage slice and faithful deliver plans; default output is unchanged",
    )
    parser.add_argument("--r23-v3-count", type=int, default=600,
                        help="number of additional v3-style positive rows for --r23-recipe")
    parser.add_argument("--r23-deliver-count", type=int, default=80,
                        help="number of faithful deliver rows for --r23-recipe")
    parser.add_argument("--r24-recipe", action="store_true",
                        help="extend r23 with grounded-ref, unknown-place, and local/home rows")
    parser.add_argument("--r24-reference-count", type=int, default=80,
                        help="same-name ref-kind contrast rows")
    parser.add_argument("--r24-unknown-count", type=int, default=80,
                        help="faithful unresolved-place rows")
    parser.add_argument("--r24-context-count", type=int, default=80,
                        help="local landing and home/launch return rows without target_ref")
    parser.add_argument("--r25-recipe", action="store_true",
                        help="extend r24 with missing-payload and negated-query contrasts")
    parser.add_argument("--r25-missing-payload-count", type=int, default=40,
                        help="payload-capable/missing-capability contrast pairs (default: 40)")
    parser.add_argument("--r25-negated-query-count", type=int, default=40,
                        help="affirmative/negated-query contrast pairs (default: 40)")
    parser.add_argument(
        "--eval-tasks",
        type=Path,
        nargs="+",
        action="extend",
        default=None,
        help="held-out task files (repeatable; default: all configs/onboard/plan-eval-tasks*.json)",
    )
    args = parser.parse_args(argv)
    if args.rows < 0 or args.val_rows < 0:
        parser.error("row counts must be non-negative")
    if args.r23_v3_count < 0 or args.r23_deliver_count < 0:
        parser.error("r23 counts must be non-negative")
    if any(value < 0 for value in (args.r24_reference_count, args.r24_unknown_count,
                                   args.r24_context_count)):
        parser.error("r24 counts must be non-negative")
    if args.r25_missing_payload_count < 0 or args.r25_negated_query_count < 0:
        parser.error("r25 counts must be non-negative")
    if args.r25_recipe:
        args.r24_recipe = True
    eval_paths = args.eval_tasks
    if eval_paths is None:
        eval_paths = sorted((ROOT / "configs/onboard").glob("plan-eval-tasks*.json"))
    elif args.version == "v5":
        # Extra exclusions cannot opt v5 out of the repository's held-out sets.
        eval_paths = sorted(set(eval_paths) | set(
            (ROOT / "configs/onboard").glob("plan-eval-tasks*.json")
        ))
    held_out = {
        normalize_task(task["text"])
        for path in eval_paths
        for task in json.loads(path.read_text(encoding="utf-8"))["tasks"]
    }
    labels = {r[0]: f"{r[1]} / {r[2]}" for r in ZONES + POIS + ROUTES}
    rng = random.Random(args.seed)
    rows, seen = [], set()
    replay_count = 0
    replay_ids: set[str] = set()
    if args.replay_planner_train:
        replay = load_replay_rows(args.replay_planner_train, {normalize_task(x) for x in held_out})
        rows.extend(replay)
        seen.update(normalize_task(row["task_text"]) for row in replay)
        replay_ids = {str(row.get("sample_id")) for row in replay}
        replay_count = len(replay)
        args.rows += replay_count
    if args.version == "v5":
        rows = v5_rows(args, held_out, labels)
    if args.r20_intent_pairs:
        # This mode is an explicit offline audit corpus, so keep every pair in
        # the requested output instead of mixing it into the normal backbone.
        rows = r20_intent_rows(args, held_out, labels)
        args.rows, args.val_rows = len(rows), 0
    if args.r20_plan_intent:
        special_rows = r20_plan_intent_rows(args, held_out, labels)
        if not args.r20_plan_intent_mix:
            rows = special_rows
            args.rows, args.val_rows = len(rows), 0
        else:
            rows = special_rows + rows
            args.rows += len(special_rows)
    if args.r23_recipe or args.r24_recipe:
        # Keep the legacy v4/replay bytes intact and append a separately tagged
        # v3-style slice.  Deliver rows are the faithful validator-gated plans,
        # so the model learns to emit deliver before capability rejection.
        r23_rows = []
        r23_rng = random.Random(f"{args.seed}:r23")
        sequence_counts = Counter()
        quotas = {name: args.r23_v3_count // len(R23_SEQUENCE_QUOTAS)
                  + int(i < args.r23_v3_count % len(R23_SEQUENCE_QUOTAS))
                  for i, name in enumerate(R23_SEQUENCE_QUOTAS)}
        for sequence, target_count in quotas.items():
            while sequence_counts[sequence] < target_count:
                text, steps, report = r23_target_case(r23_rng, sequence)
                text = " ".join(text.split())
                steps, refs = localise_places(r23_rng, steps, text, labels)
                key = normalize_task(text)
                if key in held_out or key in seen:
                    continue
                plan = {"plan_version": 1, "goal": text[:160], "steps": steps, "report": report}
                validate_mission_plan(plan)
                # Some phrasings omit the query/region the target carries; the trainer's
                # planner contract rejects those rows, so regenerate instead.
                if check_plan_intent(plan, text, tuple(refs)):
                    continue
                seen.add(key)
                sequence_counts[sequence] += 1
                r23_rows.append({"sample_id": f"plan-r23-v3-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]}",
                                 "kind": "mission_plan", "world": "synthetic_plan_r23",
                                 "family": "r23_v3_coverage", "r23_sequence": sequence,
                                 "task_text": text, "refs": list(refs),
                                 "prompt": plan_prompt(text, refs=refs, prompt_variant=args.prompt_variant,
                                                       capabilities=frozenset()),
                                 "assistant_target": plan})
        deliver_index = 0
        while len([r for r in r23_rows if r.get("family") == "r23_deliver_faithful"]) < args.r23_deliver_count:
            text, steps, refs, capabilities = r23_deliver_case(r23_rng, deliver_index)
            deliver_index += 1
            text = " ".join(text.split())
            key = normalize_task(text)
            if key in held_out or key in seen:
                continue
            plan = {"plan_version": 1, "goal": text[:160], "steps": steps, "report": ["delivery"]}
            validate_mission_plan(plan, capabilities=frozenset(capabilities))
            seen.add(key)
            r23_rows.append({"sample_id": f"plan-r23-deliver-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]}",
                             "kind": "mission_plan", "world": "synthetic_plan_r23",
                             "family": "r23_deliver_faithful", "task_text": text,
                             "refs": refs, "capabilities": list(capabilities),
                             "prompt": plan_prompt(text, refs=refs, prompt_variant=args.prompt_variant,
                                                   capabilities=frozenset(capabilities)),
                             "assistant_target": plan})
        rows.extend(r23_rows)
        args.rows += len(r23_rows)
    if args.r24_recipe:
        r24_rows = r24_reference_rows(args, held_out, seen)
        rows.extend(r24_rows)
        args.rows += len(r24_rows)
    if args.r25_recipe:
        new_rows = r25_rows(args, held_out, seen)
        rows.extend(new_rows)
        args.rows += len(new_rows)
    while len(rows) < args.rows + args.val_rows:
        if args.version == "v4":
            generator = mission_v3 if rng.random() < 1 / 3 else mission
        else:
            generator = mission if args.version == "v2" else mission_v3
        zh, en, steps, report = generator(rng)
        if rng.random() < 0.5:
            # Chinese numerals as often as digits.
            zh = re.sub(r"\d+", lambda m: zh_number(int(m.group())), zh)
        text = zh if rng.random() < 0.5 else en
        text = " ".join(text.split())
        steps, refs = localise_places(rng, steps, text, labels)
        task_key = normalize_task(text)
        if task_key in held_out or task_key in seen:
            continue
        seen.add(task_key)
        plan = {"plan_version": 1, "goal": en.rstrip(".")[:160], "steps": steps, "report": report}
        validate_mission_plan(plan)
        sample_id = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        rows.append(
            {
                "sample_id": f"plan-{sample_id}",
                "kind": "mission_plan",
                "world": "synthetic_plan",
                "task_text": text,
                "prompt": plan_prompt(text, refs=refs, prompt_variant=args.prompt_variant,
                                      capabilities=frozenset()),
                "assistant_target": plan,
            }
        )
    if args.r23_recipe or args.r24_recipe:
        # Keep legacy v4 output byte-identical; clean stacked connectives only
        # for rows emitted by the opt-in r23 recipe.
        def clean_r23_text(value):
            text = (str(value).replace("最后然后", "最后")
                    .replace("then then", "then")
                    .replace("and then then", "and then"))
            # Place labels already carry their article ("the gate").
            return DOUBLED_ARTICLE.sub(r"\1", text)
        for row in rows:
            # Replay rows stay verbatim; only rows this recipe generated are cleaned.
            if row.get("family") not in {"r23_v3_coverage", "r23_deliver_faithful",
                                          "r24_reference_type", "r24_unknown_place",
                                          "r24_context_land", "r24_context_return",
                                          "r25_missing_payload", "r25_payload_capable",
                                          "r25_negated_query"}:
                continue
            if isinstance(row.get("task_text"), str):
                old = row["task_text"]
                row["task_text"] = clean_r23_text(old)
                if isinstance(row.get("prompt"), str):
                    row["prompt"] = row["prompt"].replace(old, row["task_text"])
                if isinstance(row.get("assistant_target"), dict):
                    row["assistant_target"]["goal"] = clean_r23_text(row["assistant_target"].get("goal", ""))
    if args.r20_plan_intent and args.r20_plan_intent_mix:
        for row in rows:
            # Preserve replay rows byte-for-byte; only tag newly generated rows.
            if "family" not in row and str(row.get("sample_id")) not in replay_ids:
                row["family"] = "v4_backbone"
    splits = {"train": rows[: args.rows], "val": rows[args.rows :]}
    manifest = {
        "version": args.version,
        "seed": args.seed,
        "rows": {name: len(part) for name, part in splits.items()},
        "skill_counts": {},
        "first_skill_counts": {},
        "final_skill_counts": {},
    }
    if args.r23_recipe or args.r24_recipe:
        manifest["r23_recipe"] = {
            "v3_coverage_requested": args.r23_v3_count,
            "deliver_requested": args.r23_deliver_count,
            "rows_added": sum(1 for row in rows if row.get("family") in {"r23_v3_coverage", "r23_deliver_faithful"}),
        }
    if args.r24_recipe:
        manifest["r24_recipe"] = {
            "reference_requested": args.r24_reference_count,
            "unknown_requested": args.r24_unknown_count,
            "context_requested": args.r24_context_count,
            "rows_added": sum(1 for row in rows if str(row.get("family", "")).startswith("r24_")),
        }
    if args.r25_recipe:
        manifest["r25_recipe"] = {
            "missing_payload_pairs_requested": args.r25_missing_payload_count,
            "negated_query_pairs_requested": args.r25_negated_query_count,
            "rows_added": sum(1 for row in rows if str(row.get("family", "")).startswith("r25_")),
        }
    if replay_count:
        manifest["replay_planner_train_rows"] = replay_count
        manifest["replay_planner_train_path"] = str(args.replay_planner_train)
    if args.prompt_variant != "default":
        manifest["prompt_variant"] = args.prompt_variant
    if args.version == "v5":
        manifest["excluded_prompt_examples"] = (
            plan_prompt("audit", prompt_variant=args.prompt_variant).count("Task:")
            - v5_plan_prompt("audit", (), args.prompt_variant,
                             {normalize_task(text) for text in held_out}).count("Task:")
        )
        manifest["family_counts"] = {
            name: {family: counts[family] for family in sorted(("v4_backbone", *V5_FAMILIES))}
            for name, part in splits.items()
            for counts in [Counter(row["family"] for row in part)]
        }
    if args.r20_plan_intent:
        manifest["r20_plan_intent_pairs_per_family"] = args.r20_plan_intent_count
        manifest["family_counts"] = {
            name: dict(sorted(Counter(row.get("family", "r14_replay") for row in part).items()))
            for name, part in splits.items()
        }
    if args.negative_target_mode == "rejected_candidate":
        checked = sum(row.get("expected_valid") is False for row in splits["train"])
        for row in splits["train"]:
            assert_no_unknown_place_substitution(row)
        manifest["negative_target_safety"] = {
            "mode": args.negative_target_mode,
            "negative_rows_checked": checked,
            "known_ref_substitution_rows": 0,
        }
    for name, part in splits.items():
        sequences = [[step["skill"] for step in row["assistant_target"]["steps"]] for row in part]
        counts = {
            "skill_counts": Counter(skill for sequence in sequences for skill in sequence),
            "first_skill_counts": Counter(sequence[0] for sequence in sequences),
            "final_skill_counts": Counter(sequence[-1] for sequence in sequences),
        }
        for key, counter in counts.items():
            manifest[key][name] = {skill: counter[skill] for skill in sorted(SKILLS)}
    if args.r23_recipe or args.r24_recipe:
        args.output.mkdir(parents=True, exist_ok=True)
        family_counts = {name: Counter(row.get("family", "r14_replay") for row in part)
                         for name, part in splits.items()}
        language_counts = {name: Counter(
            "zh" if any("\u4e00" <= char <= "\u9fff" for char in row["task_text"]) else "en"
            for row in part) for name, part in splits.items()}
        stats = [f"#{' r25' if args.r25_recipe else ' r24' if args.r24_recipe else ' r23'} planner dataset statistics", "",
                 f"- Version: {args.version}; seed: {args.seed}",
                 f"- Rows: train {len(splits['train'])}, val {len(splits['val'])}",
                 "", "| Split | zh | en |", "|---|---:|---:|"]
        for name in ("train", "val"):
            stats.append(f"| {name} | {language_counts[name]['zh']} | {language_counts[name]['en']} |")
        stats += ["", "| Train family | Rows |", "|---|---:|"]
        stats += [f"| {family} | {count} |" for family, count in sorted(family_counts["train"].items())]
        stats += ["", "| Train skill | Steps |", "|---|---:|"]
        stats += [f"| {skill} | {count} |" for skill, count in sorted(manifest["skill_counts"]["train"].items())]
        sequence_counts = Counter(
            row.get("r23_sequence", " -> ".join(step["skill"] for step in row["assistant_target"]["steps"]))
            for row in splits["train"] if row.get("family") == "r23_v3_coverage"
        )
        stats += ["", "| r23 coverage sequence | Rows |", "|---|---:|"]
        stats += [f"| {sequence} | {sequence_counts[sequence]} |"
                  for sequence in sorted(sequence_counts)]
        stats += ["", f"Faithful r23 deliver rows: {family_counts['train']['r23_deliver_faithful']}.",
                  "These targets retain the `deliver` action; capability rejection is performed by the validator.", ""]
        if args.r24_recipe:
            stats += ["", "| r24 family | Rows |", "|---|---:|"]
            stats += [f"| {family} | {count} |" for family, count in sorted(family_counts["train"].items())
                      if str(family).startswith("r24_")]
            stats += ["", "r24 reference rows use only supplied ref kinds; unknown-place rows retain an unresolved candidate ref; context rows omit target_ref.", ""]
        if args.r25_recipe:
            stats += ["", "| r25 family | Rows |", "|---|---:|"]
            stats += [f"| {family} | {count} |" for family, count in sorted(family_counts["train"].items())
                      if str(family).startswith("r25_")]
            stats += ["", "r25 missing-payload negatives retain deliver for validator rejection; capable counterparts remain valid. Negated query negatives retain the forbidden query candidate for ungrounded_query rejection.", ""]
        (args.output / "r23-data-stats.md").write_text("\n".join(stats), encoding="utf-8")
    required = args.require_skills
    if required is None:
        required = sorted(set(SKILLS) - {"deliver"}) if args.version in {"v2", "v4", "v5"} else []
    missing = sorted(
        skill for skill in set(required) if manifest["skill_counts"]["train"][skill] == 0
    )
    if missing:
        parser.error("missing required skills in training split: " + ", ".join(missing))
    args.output.mkdir(parents=True, exist_ok=True)
    for name, part in splits.items():
        with (args.output / f"{name}.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
            for row in part:
                handle.write(
                    json.dumps({**row, "split": name}, ensure_ascii=False, sort_keys=True) + "\n"
                )
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\r\n"
    )
    print(json.dumps({k: len(v) for k, v in splits.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
