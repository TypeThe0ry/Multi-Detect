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
import math
import random
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
DOUBLED_ARTICLE = re.compile(r"\b([Tt]he) the\b")

from multidetect.autonomy.mission_plan import SKILLS, validate_mission_plan, validate_plan_decision  # noqa: E402
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
IRREGULAR_PLURALS = {"person": "people"}
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


def english_plural(noun: str) -> str:
    """Return a natural plural for the English object labels used in tasks."""
    irregular = IRREGULAR_PLURALS.get(noun.casefold())
    if irregular:
        return irregular
    head, separator, tail = noun.rpartition(" ")
    word = tail if separator else noun
    lowered = word.casefold()
    if lowered.endswith(("s", "x", "z", "ch", "sh")):
        plural = word + "es"
    elif len(word) > 1 and lowered.endswith("y") and lowered[-2] not in "aeiou":
        plural = word[:-1] + "ies"
    else:
        plural = word + "s"
    return f"{head}{separator}{plural}" if separator else plural


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


def r23_target_case(rng: random.Random, sequence: str, *, natural_plural: bool = False):
    """Natural v3 regression coverage with an explicit target sequence."""
    q, qz = rng.choice(OBJECTS)
    q_plural = english_plural(q) if natural_plural else f"{q}s"
    first, firstz, firste = rng.choice(POIS)
    second, secondz, seconde = rng.choice(POIS)
    zone, zonez, zonee = rng.choice(ZONES)
    zone2, zone2z, zone2e = rng.choice(ZONES)
    zone_label = zonee if natural_plural else f"the {zonee}"
    zone2_label = zone2e if natural_plural else f"the {zone2e}"
    route, routez, routee = rng.choice(ROUTES)
    sec = rng.choice([15, 30, 45, 60, 90])
    use_zh = rng.random() < 0.5
    if sequence == "goto_hover_return":
        plan = [S("s1", "goto", {"target_ref": first}, "s2", "s2"),
                S("s2", "hover", {"region": zone, "duration_s": sec}, "s3", "s3"), H("s3")]
        zh = rng.choice([f"先到{firstz}，在{zonez}上空悬停{sec}秒，然后返航",
                         f"飞往{firstz}后稳住{sec}秒，再回到起飞点",
                         f"去{firstz}等{sec}秒，完成后回家"])
        en = rng.choice([
            f"Go to {firste}, hold over {zone_label} for {sec} seconds, then return home.",
            f"Fly to {firste}, hover over {zone_label} for {sec} seconds, and come back to base.",
            f"Reposition to {firste}, wait there for {sec} seconds, then return.",
        ] if natural_plural else [
            f"Go to {firste}, hold over the {zonee} for {sec} seconds, then return home.",
            f"Fly to {firste}, hover for {sec} seconds, and come back to base.",
            f"Reposition to {firste}, wait there for {sec} seconds, then return.",
        ])
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
        zh = rng.choice([
            f"沿{routez}巡逻{loops}圈观察{qz}，结束后去{secondz}，再返航",
            f"巡查{routez}{loops}趟，留意{qz}，随后飞到{secondz}，最后回家",
            f"先巡视{routez}{loops}圈寻找{qz}，再转到{secondz}并回到起飞点",
        ] if natural_plural else [
            f"沿{routez}巡逻{loops}圈观察{qz}，结束后去{secondz}，再返航",
            f"巡查{routez}{loops}趟，随后飞到{secondz}，最后回家",
            f"先巡视{routez}{loops}圈，再转到{secondz}并回到起飞点",
        ])
        en = rng.choice([
            f"Patrol {routee} for {loops} loops watching for {q_plural}, then go to {seconde} and return home.",
            f"Run {loops} circuits of {routee} watching for {q_plural}, reposition to {seconde}, and come back.",
            f"Inspect {routee} {loops} times for {q_plural}, then fly to {seconde} before returning.",
        ] if natural_plural else [
            f"Patrol {routee} for {loops} loops watching for {q}s, then go to {seconde} and return home.",
            f"Run {loops} circuits of {routee}, reposition to {seconde}, and come back.",
            f"Inspect {routee} {loops} times, then fly to {seconde} before returning.",
        ])
    elif sequence == "monitor_monitor_return":
        sec2 = rng.choice([120, 180, 240, 300])
        plan = [S("s1", "monitor", {"region": zone, "query": q, "duration_s": sec}, "s2", "s2"),
                S("s2", "monitor", {"region": zone2, "query": rng.choice(OBJECTS)[0], "duration_s": sec2}, "s3", "s3"), H("s3")]
        q2 = plan[1]["params"]["query"]
        q2z = next(z for e, z in OBJECTS if e == q2)
        zh = rng.choice([f"在{zonez}观察{qz}{sec}秒，再到{zone2z}观察{q2z}{sec2}秒，最后返航",
                         f"先盯着{zonez}里的{qz}，然后监看{zone2z}的{q2z}，完成后回家",
                         f"监视{zonez}{sec}秒，再监视{zone2z}{sec2}秒，带着{q2z}记录返航"])
        q2_plural = english_plural(q2) if natural_plural else f"{q2}s"
        en = rng.choice([
            f"Monitor {zone_label} for {q_plural} for {sec} seconds, then monitor {zone2_label} for {q2_plural} for {sec2} seconds and return home.",
            f"Watch {zone_label} for {q_plural} for {sec} seconds, then observe {zone2_label} for {q2_plural} for {sec2} seconds before coming back.",
            f"Monitor {zone_label} for {q_plural}, then monitor {zone2_label} for {q2_plural} for the requested durations before returning.",
        ] if natural_plural else [
            f"Monitor the {zonee} for {q}s for {sec} seconds, then monitor {zone2e} for {q2}s for {sec2} seconds and return home.",
            f"Watch {zonee} for {sec} seconds, then observe {zone2e} for {sec2} seconds before coming back.",
            f"Monitor {zonee} and then {zone2e} for the requested durations, then return.",
        ])
    elif sequence == "survey_survey_land":
        overlap = rng.choice([0.6, 0.7, 0.8, 0.85])
        plan = [S("s1", "survey", {"region": zone, "overlap": overlap}, "s2", "s2"),
                S("s2", "survey", {"region": zone2, "overlap": overlap}, "s3", "s3"), L("s3")]
        pct = int(overlap * 100)
        zh = rng.choice([f"先测绘{zonez}，再测{zone2z}，两处影像重叠{pct}%，完成后降落",
                         f"给{zonez}和{zone2z}各做一次航测，保持{pct}%重叠，最后落地",
                         f"连续测量{zonez}、{zone2z}，相邻照片重叠{pct}%，结束降落"])
        en = rng.choice([
            f"Survey {zone_label} and then {zone2_label} with {pct}% overlap, then land.",
            f"Map {zone_label} followed by {zone2_label}, keeping {pct}% image overlap before setting down.",
            f"Collect mapping imagery over {zone_label} and {zone2_label} at {pct}% overlap, then land.",
        ] if natural_plural else [
            f"Survey the {zonee} and then the {zone2e} with {pct}% overlap, then land.",
            f"Map {zonee} followed by {zone2e}, keeping {pct}% image overlap before setting down.",
            f"Collect mapping imagery over {zonee} and {zone2e} at {pct}% overlap, then land.",
        ])
    else:
        plan = [S("s1", "record", {"query": q}, "s2", "s2"), H("s2")]
        zh = rng.choice([
            f"把当前视野里的{qz}拍照存档，然后返航",
            f"不用搜索，直接记录眼前的{qz}并回到起飞点",
            f"拍下已经看到的{qz}，保存证据后回家",
        ] if not natural_plural else [
            f"把视野里的{qz}拍照存档后返航",
            f"记录已经看到的{qz}并返回起点",
            f"拍下当前观察到的{qz}，保存证据后回家",
        ])
        en = rng.choice([
            f"Photograph the {q} already in view, then return home; no search is needed.",
            f"Save a still of the {q} currently visible and come back without searching.",
            f"Record the {q} in the current view, then return to launch.",
        ] if not natural_plural else [
            f"Photograph the visible {q} before returning home.",
            f"Save a still of the {q} already in view, then head back to base.",
            f"Record the {q} in the current view and return to launch.",
        ])
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


def r26_rows(args, held_out: set[str], seen: set[str], existing: list[dict]) -> list[dict]:
    """Build balanced bilingual refusal/clarification and valid contrast rows."""
    rows = []
    places = [("poi:gate", "大门", "gate"), ("poi:pad", "起降坪", "landing pad"),
              ("poi:tower", "水塔", "water tower"), ("poi:shed", "仓库", "warehouse"),
              ("poi:bridge", "桥", "bridge"), ("poi:yard", "院子", "yard")]
    payloads = [("包裹", "parcel"), ("药品箱", "medical case"), ("工具箱", "tool case"),
                ("样品盒", "sample case"), ("急救包", "first-aid kit"), ("设备箱", "equipment case"),
                ("物资袋", "supply bag"), ("零件盒", "parts box"), ("水样瓶", "water sample"),
                ("电池组", "battery pack")]
    unknowns = [("青石码头", "Blue Stone Wharf"), ("旧气象站", "Old Weather Station"),
                ("北岸仓库", "North Shore Depot"), ("石拱渡口", "Stone Arch Ferry"),
                ("林场观测点", "Forest Survey Point"), ("月台遗址", "Moon Platform Ruins")]
    queries = [("蓝色月亮车", "blue moon car"), ("紫色水下巴士", "purple underwater bus"),
               ("透明履带飞机", "transparent tracked aircraft"), ("银色火星拖拉机", "silver Mars tractor"),
               ("会飞的黄色潜艇", "flying yellow submarine"), ("绿色冰下卡车", "green under-ice truck")]
    ambiguity = [("那边", "over there"), ("它旁边", "beside it"), ("靠近那里", "near that place"),
                 ("那个位置", "that location"), ("前面那个点", "the point ahead"), ("那一处", "that spot")]
    unsafe = [("禁飞区", "restricted airspace"), ("人群上方", "above a crowd"),
              ("封锁区", "cordoned area"), ("机场跑道", "airport runway"),
              ("高压线内侧", "inside the power-line corridor"), ("临时禁入区", "temporary exclusion zone")]
    templates = {
        "missing_capability": [
            ("请把{payload}送到{place}并完成投放", "Deliver the {payload} at the {place} by dropping it"),
            ("携带{payload}前往{place}，到达后空投", "Carry the {payload} to the {place} and release it there"),
            ("将{payload}投递至{place}，无需返航", "Drop off the {payload} at the {place}; no return is needed"),
            ("到{place}把{payload}放下", "Take the {payload} to the {place} and leave it there"),
            ("把{payload}送往{place}，抵达后释放", "Bring the {payload} to the {place} and release it on arrival"),
            ("在{place}完成{payload}的投放任务", "Complete the {payload} drop mission at the {place}"),
        ],
        "unknown_place": [
            ("前往地图未收录的{place}查看现场", "Inspect the unmapped {place} and return home"),
            ("导航到列表中没有的{place}后返航", "Navigate to the unlisted {place}, then return to base"),
            ("去{place}核实情况，结束后回家", "Check conditions at {place} before heading home"),
            ("请飞抵未标注地点{place}并观察", "Fly to the unmarked site {place} and observe it"),
            ("把航线终点设为未登记的{place}", "Set the route endpoint to the unregistered {place}"),
            ("到没有列出的{place}确认目标后回家", "Visit the unlisted {place}, confirm the target, and go home"),
        ],
        "ungrounded_query": [
            ("在院子里查找{query}，完成后返航", "Search the yard for {query}, then return home"),
            ("确认院区有没有{query}，之后回到起飞点", "Check whether the yard contains {query}, then return to launch"),
            ("搜索作业区中的{query}并报告位置", "Search the operating area for {query} and report its location"),
            ("找出院内的{query}，结束后返航", "Locate {query} in the yard and fly back afterward"),
            ("对院区做一次{query}检索，然后返航", "Run a {query} search in the yard and return afterward"),
            ("在作业区搜寻{query}，报告后回家", "Search the operating area for {query}, report it, and come home"),
        ],
        "ambiguous_task": [
            ("去{place}查看一下，再返航", "Inspect {place} and then return home"),
            ("请到{place}确认情况，完成后回家", "Go to {place} to check the situation, then come back"),
            ("检查{place}并返回起飞点", "Check {place} and return to the launch point"),
            ("飞到{place}观察一遍后返航", "Fly to {place}, take a look, and return to base"),
            ("去{place}处理一下现场再返航", "Go to {place}, handle the inspection, and return"),
            ("把查看任务安排在{place}，完成后回家", "Schedule the inspection at {place}, then come home"),
        ],
        "unsafe_request": [
            ("进入{place}上空搜索车辆，随后返航", "Enter {place} to search for vehicles, then return"),
            ("穿越{place}并拍摄现场，完成后回家", "Cross {place} and photograph the scene before returning"),
            ("飞入{place}检查情况，再返回起飞点", "Fly into {place} to inspect it, then return to launch"),
            ("在{place}内执行巡查并返航", "Patrol within {place} and return home"),
            ("穿入{place}拍摄目标，再飞回基地", "Enter {place} to film the target, then fly back to base"),
            ("沿{place}方向飞入并检查现场", "Fly into {place} and inspect the scene"),
        ],
    }
    decisions = {
        "missing_capability": ("reject", "飞机缺少载荷投放能力。", "Aircraft lacks payload drop capability.", ["payload_drop"], [], None, None),
        "unknown_place": ("clarify", "请求地点未出现在已知引用中。", "The requested place is absent from the known references.", [], [], "你希望改为哪个已知地点？", "Which known place should replace it?"),
        "ungrounded_query": ("reject", "请求的查询类别没有任务依据。", "The requested query has no grounding in the task context.", [], [], None, None),
        "ambiguous_task": ("clarify", "无法唯一确定要检查的位置。", "The location to inspect is not uniquely identified.", [], ["ambiguous_location"], "你希望检查列表中的哪个地点？", "Which listed place should I inspect?"),
        "unsafe_request": ("reject", "请求要求进入受限制区域。", "The request requires entering a restricted area.", [], [], None, None),
    }

    # r26 upgrades legacy negatives emitted by the shared r23/r24 recipes.
    legacy_unknown = [row for row in existing if row.get("family") == "r24_unknown_place"]
    for row in legacy_unknown:
        candidate = row["assistant_target"]
        text = row["task_text"]
        match = re.search(r"poi:([a-z0-9_]+)", candidate["steps"][0]["params"]["target_ref"])
        unresolved = ["poi:" + match.group(1)] if match else []
        zh = bool(re.search(r"[\u4e00-\u9fff]", text))
        row["rejected_candidate"] = candidate
        row["negative_target_mode"] = "rejected_candidate"
        row["reason_code"] = "unknown_place"
        row["assistant_target"] = {
            "plan_version": 1, "decision": "clarify", "reason_code": "unknown_place",
            "reason": decisions["unknown_place"][1 if zh else 2],
            "missing_capabilities": [], "unresolved_refs": unresolved,
            "question": "你希望改为哪个已知地点？" if zh else "Which known place should replace it?",
        }
        row["expected_valid"] = False
        row["expected_findings"] = ["unknown_place"]
        row["prompt"] = plan_prompt(text, refs=tuple(row.get("refs", ())),
            capabilities=frozenset(row.get("capabilities", ())), include_decision_contract=True)
        validate_plan_decision(row["assistant_target"])
        _, _, unknown_refs = check_plan_refs(candidate, tuple(row.get("refs", ())), substitute=False)
        if not unknown_refs:
            raise ValueError(f"r26 legacy unknown-place audit candidate was not rejected: {row['sample_id']}")
    legacy_deliver = [
        row for row in existing
        if row.get("family") == "r23_deliver_faithful"
        and isinstance(row.get("assistant_target"), dict)
        and any(step.get("skill") == "deliver"
                for step in row["assistant_target"].get("steps", ()))
        and "payload_drop" not in set(row.get("capabilities", ()))
    ]
    for row in legacy_deliver:
        candidate = row["assistant_target"]
        text = row["task_text"]
        zh = bool(re.search(r"[\u4e00-\u9fff]", text))
        row["rejected_candidate"] = candidate
        row["negative_target_mode"] = "rejected_candidate"
        row["reason_code"] = "missing_capability"
        row["assistant_target"] = {
            "plan_version": 1, "decision": "reject", "reason_code": "missing_capability",
            "reason": "飞机缺少载荷投放能力。" if zh else "Aircraft lacks payload drop capability.",
            "missing_capabilities": ["payload_drop"], "unresolved_refs": [],
            "question": None,
        }
        row["expected_valid"] = False
        row["expected_findings"] = ["missing_capability"]
        row["capabilities"] = []
        row["prompt"] = plan_prompt(text, refs=tuple(row.get("refs", ())),
            capabilities=frozenset(row.get("capabilities", ())), include_decision_contract=True)
        validate_plan_decision(row["assistant_target"])

    def add_row(code, text, refs, capabilities, target, valid, audit=None, *, positive=False):
        key = normalize_task(text)
        if key in held_out or key in seen:
            return False
        if valid:
            validate_mission_plan(target, capabilities=frozenset(capabilities))
            findings = check_plan_intent(target, text, tuple(refs))
            if findings:
                raise ValueError(f"r26 positive intent failure: {code}: {findings}: {text}")
        else:
            validate_plan_decision(target)
        seen.add(key)
        family = ("r26_payload_capable" if positive and code == "missing_capability"
                  else f"r26p_{code}" if positive else f"r26_{code}")
        row = {"sample_id": f"plan-r26-{code}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]}",
               "kind": "mission_plan", "world": "synthetic_plan_r26", "family": family,
               "pair_role": "positive" if positive else "refusal",
               "reason_code": code,
               "task_text": text, "refs": list(refs), "capabilities": list(capabilities),
               "expected_valid": valid, "expected_findings": [] if valid else [code],
               "prompt": plan_prompt(text, refs=tuple(refs), capabilities=frozenset(capabilities),
                                     include_decision_contract=True),
               "assistant_target": target, "_key": key}
        rows.append(row)
        return True

    def positive_plan(code, text, ref, *, query="vehicle", capability=False):
        if code == "missing_capability":
            place_ref, _zh, _en, _payload_zh, _payload_en = ref
            return {"plan_version": 1, "goal": text[:160], "steps": [
                S("s1", "deliver", {"target_ref": place_ref, "payload_ref": "payload:package"}, "s2", "s2"), H("s2")],
                "report": ["delivery"]}
        place_ref, _zh, _en = ref[:3]
        if code in {"unknown_place", "ambiguous_task"}:
            return {"plan_version": 1, "goal": text[:160], "steps": [
                S("s1", "goto", {"target_ref": place_ref}, "s2", "s2"),
                S("s2", "inspect", {"target_ref": place_ref, "query": query, "views": 1, "standoff_m": 5}, "s3", "s3"), H("s3")],
                "report": ["inspection"]}
        if code == "ungrounded_query":
            return {"plan_version": 1, "goal": text[:160], "steps": [
                S("s1", "area_search", {"region": "zone:yard", "query": query}, "s2", "s2"), H("s2")],
                "report": ["inspection"]}
        return {"plan_version": 1, "goal": text[:160], "steps": [
            S("s1", "area_search", {"region": "zone:yard", "query": query}, "s2", "s2"), H("s2")],
            "report": ["status"]}

    for code, pairs in templates.items():
        inherited_count = (len(legacy_unknown) if code == "unknown_place" else
                           len(legacy_deliver) if code == "missing_capability" else 0)
        requested_total = args.r26_refusal_count
        refusal_count = max(0, requested_total - inherited_count)
        # Cycle finite phrase/place/query combinations and fail once the unique pool is exhausted.
        pool_size = len(pairs) * (len(payloads) if code == "missing_capability" else
                                  len(unknowns) if code == "unknown_place" else
                                  len(queries) if code == "ungrounded_query" else
                                  len(ambiguity) if code == "ambiguous_task" else len(unsafe))
        requested = refusal_count
        if requested > pool_size * 2:
            raise ValueError(f"r26 {code} template pool exhausted after de-duplication; reduce requested count")
        emitted = 0
        for i in range(pool_size * 2):
            if emitted >= refusal_count:
                break
            language_zh = (emitted % 2 == 0)
            choice_set = payloads if code == "missing_capability" else unknowns if code == "unknown_place" else queries if code == "ungrounded_query" else ambiguity if code == "ambiguous_task" else unsafe
            value = choice_set[(i // len(pairs)) % len(choice_set)]
            pair = pairs[i % len(pairs)]
            if code == "missing_capability":
                zh_payload, en_payload = value
                place_ref, place_zh, place_en = places[(i // (len(pairs) * len(payloads))) % len(places)]
                fmt = {"payload": zh_payload if language_zh else en_payload,
                       "place": place_zh if language_zh else place_en}
                refs = (f"{place_ref} ({place_zh} / {place_en})",)
                caps = []
                audit = {"plan_version": 1, "goal": "deliver audit candidate", "steps": [
                    S("s1", "deliver", {"target_ref": place_ref, "payload_ref": "payload:package"}, "s2", "s2"), H("s2")], "report": ["delivery"]}
            elif code == "unknown_place":
                zh_place, en_place = value
                known_ref, known_zh, known_en = places[i % len(places)]
                fake = "poi:" + re.sub(r"[^a-z0-9]+", "_", en_place.casefold()).strip("_")
                fmt = {"place": zh_place if language_zh else en_place}
                refs = (f"{known_ref} ({known_zh} / {known_en})",)
                caps = []
                audit = {"plan_version": 1, "goal": "unknown-place audit candidate", "steps": [S("s1", "goto", {"target_ref": fake}, "s2", "s2"), H("s2")], "report": ["status"]}
            elif code == "ungrounded_query":
                zh_query, en_query = value
                fmt = {"query": zh_query if language_zh else en_query}
                refs = ("zone:yard (院子 / yard)",)
                caps = []
                audit = {"plan_version": 1, "goal": "ungrounded query audit candidate", "steps": [S("s1", "area_search", {"region": "zone:yard", "query": "ambulance"}, "s2", "s2"), H("s2")], "report": ["status"]}
            elif code == "ambiguous_task":
                zh_place, en_place = value
                fmt = {"place": zh_place if language_zh else en_place}
                refs = tuple(f"{r} ({z} / {e})" for r, z, e in places[:2])
                caps = []
                audit = None
            else:
                zh_place, en_place = value
                fmt = {"place": zh_place if language_zh else en_place}
                refs = ("zone:yard (院子 / yard)",)
                caps = []
                audit = None
            text = pair[0 if language_zh else 1].format(**fmt) + ("。" if language_zh else ".")
            decision, reason_zh, reason_en, missing, unresolved, q_zh, q_en = decisions[code]
            reason = reason_zh if language_zh else reason_en
            if code == "unknown_place":
                unresolved = [fake]
            target = {"plan_version": 1, "decision": decision, "reason_code": code, "reason": reason,
                      "missing_capabilities": missing, "unresolved_refs": unresolved,
                      "question": (q_zh if language_zh else q_en) if decision == "clarify" else None}
            added = (add_row(code, text, refs, caps, target, False, audit)
                     if audit is not None else add_row(code, text, refs, caps, target, False))
            if not added:
                continue
            pos_ref = places[i % len(places)]
            positive_queries = [("车辆", "vehicles"), ("行人", "pedestrians"), ("自行车", "bicycles"),
                                ("卡车", "trucks"), ("船只", "boats"), ("工人", "workers"),
                                ("轿车", "cars"), ("公交车", "buses"), ("狗", "dogs"), ("摩托车", "motorcycles")]
            qzh, qen = positive_queries[(i // len(places)) % len(positive_queries)]
            positive_phrases = [
                ("检查", "inspect"), ("查看", "check"), ("搜寻", "search for"),
                ("确认现场有无", "verify whether there are"), ("记录", "record"),
                ("观察", "observe"),
            ]
            verb_zh, verb_en = positive_phrases[(i // (len(places) * len(positive_queries))) % len(positive_phrases)]
            pos_text = (f"到{pos_ref[1]}{verb_zh}{qzh}并返航。" if language_zh else
                        f"Go to the {pos_ref[2]}, {verb_en} {qen}, and return home.")
            if code == "missing_capability":
                payload_zh, payload_en = payloads[(i // len(places)) % len(payloads)]
                pos_text = (f"飞机具备投放能力：把{payload_zh}送到{pos_ref[1]}后返航。" if language_zh else
                            f"With payload drop capability, deliver the {payload_en} to the {pos_ref[2]} and return home.")
            elif code == "ungrounded_query":
                positive_forms = [
                    ("在院子里查找{q}并返航。", "Search the yard for {q}, then return home."),
                    ("对院区中的{q}做一次检查，随后返航。", "Inspect {q} across the yard before returning."),
                    ("确认院子里是否有{q}，然后回到起飞点。", "Check for {q} in the yard and head back to launch."),
                    ("搜索作业区内的{q}，记录结果后返航。", "Search the work area for {q}, record the result, and return."),
                    ("观察院内{q}并向操作员报告，最后返航。", "Observe {q} in the yard, report to the operator, and return."),
                    ("在院区搜寻{q}，完成后飞回基地。", "Look for {q} across the yard, then fly back to base."),
                ]
                pos_text = positive_forms[i % len(positive_forms)][0 if language_zh else 1].format(q=qzh if language_zh else qen)
            elif code == "unsafe_request":
                safe_forms = [
                    ("在安全区域搜索{q}，结束后返航。", "Search for {q} in the safe operating area, then return."),
                    ("只在院子内检查{q}，随后飞回起飞点。", "Inspect {q} within the yard and then return to launch."),
                    ("避开限制区域，在作业区内巡查{q}后返航。", "Avoid restricted areas; patrol for {q} in the work area, then return."),
                    ("在获准区域观察{q}并记录位置，最后回家。", "Observe {q} in the authorized area, record their locations, and return."),
                    ("留在安全边界内查找{q}，完成后返航。", "Stay within the safe boundary while looking for {q}, then return."),
                    ("对院内{q}做常规检查，再回到基地。", "Perform a routine check for {q} in the yard, then return to base."),
                ]
                pos_text = safe_forms[i % len(safe_forms)][0 if language_zh else 1].format(q=qzh if language_zh else qen)
            elif code == "ambiguous_task":
                pos_text = (f"请到{pos_ref[1]}明确检查{qzh}并返航。" if language_zh else
                            f"Inspect {qen} at the {pos_ref[2]} and return home.")
            positive_caps = ["payload_drop"] if code == "missing_capability" else []
            positive = positive_plan(code, pos_text, (pos_ref[0], pos_ref[1], pos_ref[2], "包裹", "parcel"), query=qen)
            if not add_row(code, pos_text, (f"{pos_ref[0]} ({pos_ref[1]} / {pos_ref[2]})", "zone:yard (院子 / yard)"),
                           positive_caps, positive, True, positive=True):
                raise ValueError(f"r26 {code} positive contrast pool exhausted after de-duplication")
            emitted += 1
        if emitted != refusal_count:
            raise ValueError(f"r26 {code} produced {emitted}/{refusal_count} unique refusals; template pool exhausted")
    legacy_positive_forms = [
        ("查看车辆后返航", "Inspect vehicles and return home"),
        ("检查现场车辆，再回到起飞点", "Check the vehicles on site, then return to launch"),
        ("搜索车辆并向操作员报告，随后返航", "Search for vehicles, report to the operator, then return"),
        ("观察车辆情况，完成后回家", "Observe the vehicles and head home when done"),
        ("记录车辆位置并返航", "Record vehicle locations and return to base"),
        ("核实车辆是否在场，然后返回起飞点", "Verify whether vehicles are present, then return to launch"),
        ("巡看车辆后飞回基地", "Look over the vehicles before flying back to base"),
        ("对车辆做现场检查并结束任务", "Inspect the vehicles on site to finish the mission"),
        ("查看有无车辆活动，再返回", "Check for vehicle activity and then return"),
        ("巡查车辆并向地面报告后返航", "Survey the vehicles, report to the ground station, and return"),
        ("搜寻车辆并把发现情况带回", "Search for vehicles and report the findings on return"),
        ("确认车辆位置后结束飞行任务", "Confirm vehicle locations to complete the flight"),
        ("对车辆目标进行目视检查并返航", "Visually inspect vehicle targets and return"),
        ("记录车辆分布情况，再飞回起飞点", "Record vehicle distribution, then fly back to launch"),
        ("在该点观察车辆并结束巡查", "Observe vehicles at the site and conclude the survey"),
        ("清点现场车辆，之后返航", "Count the vehicles on site and then return"),
    ]
    for i in range(len(legacy_unknown)):
        place = places[i % len(places)]
        language_zh = i % 2 == 0
        zh_form, en_form = legacy_positive_forms[(i // len(places)) % len(legacy_positive_forms)]
        text = (f"到{place[1]}{zh_form}。" if language_zh else
                f"At the {place[2]}, {en_form}.")
        refs = (f"{place[0]} ({place[1]} / {place[2]})",)
        target = positive_plan("unknown_place", text, place)
        if not add_row("unknown_place", text, refs, [], target, True, positive=True):
            raise ValueError("r26 unknown-place positive pool exhausted after de-duplication")
    for i, row in enumerate(legacy_deliver):
        candidate = row["rejected_candidate"]
        step = next(step for step in candidate["steps"] if step.get("skill") == "deliver")
        target_ref = step["params"]["target_ref"]
        payload_ref = step["params"]["payload_ref"]
        place = next((p for p in places if p[0] == target_ref), (target_ref, target_ref, target_ref))
        payload = str(payload_ref).split(":", 1)[-1].replace("_", " ")
        zh = bool(re.search(r"[\u4e00-\u9fff]", row["task_text"]))
        zh_forms = [
            "有投放能力时，将{payload}送到{place}并返航",
            "确认载荷系统可用后，把{payload}投放在{place}，随后回家",
            "本机支持载荷投送：携带{payload}前往{place}完成交付",
            "启用投放能力，把{payload}送至{place}再返回起飞点",
            "载荷投放可用，请向{place}交付{payload}后返航",
            "把{payload}运到{place}投放，飞机具备所需能力",
            "在具备投放能力的条件下，将{payload}送到{place}",
            "由支持投放的飞机把{payload}交给{place}并返航",
            "本次飞行允许投放载荷：送{payload}到{place}后返回",
            "载荷设备正常，完成{payload}在{place}的投放任务",
            "前往{place}投放{payload}，当前飞机能力满足要求",
            "确认能够投放后，将{payload}送达{place}并结束任务",
            "当前飞机可以投放载荷，请把{payload}交付到{place}",
            "载荷投送系统可用：将{payload}送到{place}后返回基地",
            "使用本机投放装置，把{payload}送至{place}完成任务",
            "允许使用投放能力，将{payload}交给{place}后返航",
            "投放设备工作正常，前往{place}放下{payload}",
            "执行一次合法的{payload}投放，地点是{place}",
            "飞机的载荷能力满足要求，交付{payload}至{place}",
            "送{payload}到{place}并投放，本机已配备对应能力",
            "确认设备可用后将{payload}空投至{place}",
            "本机支持这一投放任务：把{payload}送到{place}",
            "把{payload}运抵{place}并完成释放，随后回家",
            "载荷舱工作正常，请在{place}完成{payload}交接",
        ]
        en_forms = [
            "With payload drop enabled, deliver the {payload} to {place} and return",
            "The payload system is available; release the {payload} at {place}, then come home",
            "This aircraft supports payload delivery: carry the {payload} to {place}",
            "Use the available drop capability to take the {payload} to {place} and return",
            "Payload release is supported; deliver the {payload} to {place} before returning",
            "Carry the {payload} to {place} for drop-off; the aircraft has the required capability",
            "With a capable aircraft, deliver the {payload} to {place}",
            "Drop the {payload} at {place}; payload delivery is available on this aircraft",
            "Payload release is permitted: take the {payload} to {place} and return",
            "The payload system is ready for a delivery of the {payload} to {place}",
            "Fly to {place} and deliver the {payload}; this aircraft supports payload drop",
            "Complete the {payload} drop-off at {place} with the available capability",
            "The aircraft is cleared for payload release; take the {payload} to {place}",
            "Make a supported delivery of the {payload} to {place}, then return to base",
            "Payload drop is available, so deliver the {payload} at {place}",
            "Take the {payload} to {place}; the aircraft is equipped to release it",
            "Use this aircraft's payload system to deliver the {payload} at {place}",
            "Deliver the {payload} to {place} using the enabled payload release system",
            "The aircraft can drop payloads; complete delivery of the {payload} at {place}",
            "Carry and release the {payload} at {place} with the supported payload system",
            "Fly the {payload} to {place} for a valid payload drop, then return",
            "Complete a permitted release of the {payload} at {place}",
            "Drop off the {payload} at {place}; the required aircraft capability is present",
            "Payload delivery is supported, so take the {payload} to {place}",
            "With release hardware available, deliver the {payload} to {place}",
            "The aircraft has a working drop mechanism for the {payload} delivery to {place}",
            "Deliver the {payload} at {place} and return; payload drop is an available skill",
        ]
        text = (zh_forms[i % len(zh_forms)] if zh else en_forms[i % len(en_forms)]).format(
            payload=payload, place=place[1 if zh else 2]
        )
        text += "。" if zh else "."
        refs = tuple(row.get("refs", ()))
        positive = {**candidate, "goal": text[:160]}
        positive["steps"] = [dict(item) for item in candidate["steps"]]
        positive["steps"][candidate["steps"].index(step)] = {
            **step, "params": {**step["params"], "target_ref": target_ref, "payload_ref": payload_ref}
        }
        if not add_row("missing_capability", text, refs, ["payload_drop"], positive, True, positive=True):
            raise ValueError(f"r26 payload-capable positive pool exhausted at legacy row {i}")
    return rows


def r27_rows(args, held_out: set[str], seen: set[str]) -> list[dict]:
    """Add narrowly grounded refusal pairs and difficult executable positives."""
    rows = []
    places = [("poi:gate", "大门", "gate"), ("poi:pad", "起降坪", "landing pad"),
              ("poi:tower", "水塔", "water tower"), ("poi:shed", "仓库", "warehouse"),
              ("poi:bridge", "桥", "bridge"), ("poi:yard", "院子", "yard")]
    payloads = [
        ("包裹", "parcel", "package"), ("药品箱", "medical case", "medical_kit"),
        ("工具箱", "tool case", "toolkit"), ("样品盒", "sample box", "sample_box"),
        ("急救包", "first-aid kit", "first_aid_kit"), ("设备箱", "equipment case", "equipment_case"),
        ("物资袋", "supply bag", "supply_bag"), ("零件盒", "parts box", "parts_box"),
        ("水样瓶", "water sample", "water_sample"), ("电池组", "battery pack", "battery_pack"),
        ("通信器材", "radio equipment", "radio_equipment"), ("医疗物资", "medical supplies", "medical_supplies"),
        ("传感器", "sensor", "sensor"), ("备用电台", "spare radio", "spare_radio"),
        ("样本罐", "specimen jar", "specimen_jar"), ("救援绳包", "rescue rope kit", "rescue_rope_kit"),
    ]
    unknowns = [("青石码头", "Blue Stone Wharf"), ("旧气象站", "Old Weather Station"),
                ("北岸仓库", "North Shore Depot"), ("石拱渡口", "Stone Arch Ferry"),
                ("林场观测点", "Forest Survey Point"), ("月台遗址", "Moon Platform Ruins")]
    canonical_labels = {ref: f"{zh} / {en}" for ref, zh, en in ZONES + POIS + ROUTES}

    def canonical_refs(steps):
        refs = []
        for step in steps:
            for key in ("region", "target_ref", "route_ref"):
                ref = step.get("params", {}).get(key)
                if ref in canonical_labels and ref not in refs:
                    refs.append(ref)
        return tuple(f"{ref} ({canonical_labels[ref]})" for ref in refs)

    terminal_phrases = {
        "en_return": (
            "then head back to base", "before flying to the launch point", "and finish at the starting point",
            "then make the return leg to base", "ending at the place the flight began", "and come back to launch",
            "before heading home", "then fly back to the departure point", "with a final leg to base",
            "and conclude at the launch site", "before returning to the takeoff point", "then retrace the way home",
            "to finish back at base", "and return to the starting location", "with the aircraft back at launch",
            "then continue home", "and complete the trip at base", "before making the flight back",
            "to end where the sortie started", "and head for the launch site",
            "and fly back to the launch pad", "before taking the return route", "and head back to the launch site",
            "then return to the home point", "and bring the aircraft back to launch", "before returning to the home point",
            "and complete the trip back to base", "then fly back to the pad", "and make the flight back to base",
            "before heading to the takeoff point", "and finish the return to base", "then make for home",
            "and return to the departure pad", "before flying back to the launch area", "and bring the aircraft home",
        ),
        "zh_return": (
            "，再飞回出发地点", "，随后返回起飞位置", "，最后回到基地", "，结束时飞回起点",
            "，并返回最初的起降点", "，收尾时回到出发地", "，然后飞回基地", "，完成后回到起始位置",
            "，最后回到出发点", "，返程至起飞地点", "，再回到基地待命", "，以返航结束这趟任务",
            "，飞回最初的地点", "，随后回到出发位置", "，并飞回起降点", "，最后返回基地",
            "，让飞机回到起点", "，再沿返程回到出发地", "，返航至最初的位置", "，回到起飞处结束",
            "，然后飞回起飞地点", "，返回最初的起降坪", "，并回到出发基地", "，最后飞回起降位置",
            "，再返回最初的地点", "，随后飞向基地降落待命", "，飞回最初的起飞位置", "，并回到起飞处",
            "，返程回到基地位置", "，最后回到起飞场地", "，飞回任务开始的位置", "，回到出发的起降场",
            "，随后返回飞行起点", "，结束返程并回到基地", "，再飞回起降坪", "，将飞机带回起飞地点",
        ),
        "en_land": (
            "and touch down at the end", "before setting the aircraft down", "finishing with a landing",
            "and bring the flight to a close on the ground", "to complete the sortie with a landing",
            "then put the aircraft down", "and end the flight with touchdown", "before coming to rest on the ground",
            "to finish the mission on the ground", "and complete the flight by landing", "ending with touchdown",
            "then settle onto the ground", "to conclude with a landing", "and finish the sortie on the ground",
            "before touching down", "and bring the aircraft down to land", "to end the flight with touchdown",
            "then complete the landing", "and close out the sortie on the ground", "to finish with the aircraft landed",
            "and land at the end of the flight", "then touch down at the landing site", "and finish after touchdown",
            "before landing the aircraft", "and set the drone down to finish", "to end with the aircraft on the ground",
            "then bring the aircraft in to land", "and complete the flight with touchdown", "before the final landing",
            "and finish on the ground after landing", "then land the aircraft safely", "to close the flight with a landing",
            "and touch down to complete the mission", "before ending the sortie on the ground", "then finish with a touchdown",
            "and bring the aircraft to the ground", "to complete the mission after landing",
        ),
        "zh_land": (
            "，随后完成着陆", "，以落地结束飞行", "，最后让飞机着陆", "，并在地面结束任务",
            "，收尾时完成降落", "，然后把飞机降下来", "，最终停在地面", "，结束航段并落地",
            "，完成着陆后结束任务", "，最后在地面收尾", "，以着陆完成本次飞行", "，然后安全落地",
            "，将这趟飞行结束在地面", "，着陆后结束本次任务", "，最后完成落地", "，并以降落收尾",
            "，让飞机在地面停下", "，以落地作为终点", "，随后在地面结束飞行", "，并完成最终着陆",
            "，然后在地面完成任务", "，最后降到地面停稳", "，并在此结束飞行", "，着陆后结束航段",
            "，让无人机落地停下", "，以最终落地结束飞行", "，随后把飞机停在地面", "，降落后结束航程",
            "，并在着陆后收工", "，最后停在地面", "，飞行到此结束并着陆", "，之后完成着陆",
            "，以飞机落地作为结束", "，随后落地结束任务", "，把本次飞行收在地面", "，落地后停止飞行",
        ),
    }
    def vary_terminal(text, steps, index):
        language = "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"
        landing = steps[-1]["skill"] == "land"
        key = f"{language}_{'land' if landing else 'return'}"
        choices = terminal_phrases[key]
        phrase_index = ((index // 10) * 7 if family == "r27_counting"
                        else (index // len(family_sequences)) * 7 + (index % len(family_sequences)))
        phrase = choices[phrase_index % len(choices)]
        if language == "zh":
            pattern = (r"(?:然后|随后|完成后|最后|再|并|让飞机)?(?:返航|回家|回来|返回(?:起飞点|起点|基地|出发点|起降点|最初的起降点)|"
                       r"回到(?:起飞点|起点|基地|出发点|起降点)|让飞机回到(?:起飞点|起点|基地|出发点)|"
                       r"飞回(?:起飞点|起点|基地|出发点|起降点))[。]?$" ) if not landing else (
                       r"(?:然后|随后|完成后|最后|再|并)?(?:降落|落地|着陆)[。]?$" )
            match = re.search(pattern, text)
            return text[:match.start()].rstrip(" ，,。.!?") + phrase + "。" if match else text
        if not landing and re.search(r"(?:no search is needed|without searching)\.?$", text, re.I):
            endings = (
                "with no search beyond what is already in view", "without starting a search", "and no search is required",
                "while leaving the visible target unsearched", "without searching for anything else", "with the target already visible",
                "and skip any further search", "without looking for additional targets", "with no wider search",
                "and leave the current view as is", "without a new search pass", "while keeping the task to the current view",
                "and do not scan another area", "without expanding the search", "with no extra search activity",
                "and avoid searching beyond the current view", "with the existing observation only", "without looking elsewhere",
                "and keep the request limited to what is visible", "with no additional scan",
            )
            text = re.sub(r"(?:;\s*no search is needed|\s+without searching)\.?$", "", text, flags=re.I)
            return text.rstrip(" ,;.") + ", " + endings[phrase_index % len(endings)] + "."
        pattern = (r"(?:,?\s*)(?:then |and |before |to )?(?:return home|return|come back(?: to base| to launch)?|"
                   r"returning|coming back|heading home|head back(?: to base| to the launch point)?|"
                   r"flying back|fly back(?: to the departure point| to base| home)?|"
                   r"return to (?:launch|base|the starting location))\.?$") if not landing else (
                   r"(?:,?\s*)(?:then |and |before |to )?(?:land|set down|setting down|touch down|touching down|"
                   r"set the aircraft down|come to rest on the ground)\.?$")
        match = re.search(pattern, text, re.I)
        if match:
            return text[:match.start()].rstrip(" ,;.") + ", " + phrase + "."
        return text

    def emit(text, refs, capabilities, target, family, *, valid, code=None, candidate=None):
        key = normalize_task(text)
        if key in held_out or key in seen:
            return False
        if valid:
            validate_mission_plan(target, capabilities=frozenset(capabilities))
            findings = check_plan_intent(target, text, tuple(refs))
            if findings:
                raise ValueError(f"r27 positive intent failure: {family}: {findings}: {text}")
        else:
            validate_plan_decision(target)
        seen.add(key)
        prompt = plan_prompt(text, refs=tuple(refs), capabilities=frozenset(capabilities),
                             include_decision_contract=True)
        if family == "r27_missing_capability":
            prompt = prompt.replace("Task: " + text,
                "Available aircraft capabilities: none. Payload drop is unavailable.\nTask: " + text)
        row = {"sample_id": f"plan-r27-{family}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]}",
               "kind": "mission_plan", "world": "synthetic_plan_r27", "family": family,
               "pair_role": "positive" if valid and code else "refusal" if code else None,
               "reason_code": code, "task_text": text, "refs": list(refs),
               "capabilities": list(capabilities), "expected_valid": valid,
               "expected_findings": [] if valid else [code], "prompt": prompt,
               "assistant_target": target, "_key": key}
        if candidate is not None:
            row["rejected_candidate"] = candidate
            row["negative_target_mode"] = "rejected_candidate"
        rows.append(row)
        return True

    def plan(text, steps, report):
        return {"plan_version": 1, "goal": text[:160], "steps": steps, "report": report}

    # Every refusal has a same-frame executable counterpart with only the
    # missing capability or place-list fact changed.
    index = 0
    payload_refusals = 0
    unknown_refusals = 0
    attempts = 0
    while payload_refusals < args.r27_refusal_count or unknown_refusals < args.r27_refusal_count:
        if attempts > args.r27_refusal_count * 20 + 100:
            raise ValueError(
                "r27 refusal pair pool exhausted after holdout filtering "
                f"(missing_capability={payload_refusals}, unknown_place={unknown_refusals})"
            )
        cursor = index + attempts
        place_ref, place_zh, place_en = places[(cursor // 2) % len(places)]
        payload_zh, payload_en, payload_ref = payloads[(cursor // len(places)) % len(payloads)]
        unknown_zh, unknown_en = unknowns[(cursor // 2) % len(unknowns)]
        suffixes = [("北侧", "north side"), ("南侧", "south side"), ("东侧", "east side"),
                    ("西侧", "west side"), ("旧址", "old site"), ("二号点", "site two"),
                    ("山脚点", "foothill site")]
        suffix_zh, suffix_en = suffixes[(cursor // 2) % len(suffixes)]
        unknown_zh, unknown_en = unknown_zh + suffix_zh, unknown_en + " " + suffix_en
        use_zh = cursor % 2 == 0
        if use_zh:
            bad_payload = f"把{payload_zh}送到{place_zh}后返航。"
            good_payload = f"飞机具备投放能力，把{payload_zh}送到{place_zh}后返航。"
            bad_place = f"飞到地图没标出的{unknown_zh}后返航。"
            known_zh_verbs = (f"飞到{place_zh}后返航。", f"前往{place_zh}再回家。",
                              f"到{place_zh}后返回起飞点。", f"请去{place_zh}然后返航。",
                              f"先抵达{place_zh}，完成后回来。", f"直接飞到{place_zh}再返航。",
                              f"现在去{place_zh}，随后回家。", f"到达{place_zh}就返航。",
                              f"把飞机带到{place_zh}后回来。", f"去一趟{place_zh}再回起点。",
                              f"按计划前往{place_zh}，然后返航。", f"抵达{place_zh}即可返回。")
            good_place = known_zh_verbs[(cursor // (2 * len(places))) % len(known_zh_verbs)]
            payload_reason = "飞机没有载荷投放能力。"
            unknown_reason = "请求的地点不在已知地点列表中。"
            unknown_question = f"请问要改去哪个已知地点，例如{place_zh}吗？"
        else:
            bad_payload = f"Deliver the {payload_en} to the {place_en}, then return home."
            good_payload = f"With payload drop capability, deliver the {payload_en} to the {place_en}, then return home."
            bad_place = f"Visit the unlisted {unknown_en}, then return home."
            known_en_verbs = (f"Visit the listed {place_en}, then return home.",
                              f"Go to the listed {place_en} and come back.",
                              f"Fly to the listed {place_en} before heading back to launch.",
                              f"Reposition to the listed {place_en}, then come home.",
                              f"Navigate to the listed {place_en} and return to base.",
                              f"Directly fly to the listed {place_en}, then return.",
                              f"Go to the listed {place_en} now and head home.",
                              f"Reach the listed {place_en}, then fly back.",
                              f"Take the aircraft to the listed {place_en} and come back.",
                              f"Make one trip to the listed {place_en}, then return.",
                              f"Proceed to the listed {place_en} before returning.",
                              f"Once at the listed {place_en}, return to base.")
            good_place = known_en_verbs[(cursor // (2 * len(places))) % len(known_en_verbs)]
            payload_reason = "Payload drop capability is unavailable."
            unknown_reason = "The requested place is not in the known-place list."
            unknown_question = f"Should I use the listed {place_en} instead?"
        refs = (f"{place_ref} ({place_zh} / {place_en})",)
        payload_candidate = plan(bad_payload, [S("s1", "deliver", {
            "target_ref": place_ref, "payload_ref": f"payload:{payload_ref}"}, "s2", "s2"), H("s2")], ["delivery"])
        missing = {"plan_version": 1, "decision": "reject", "reason_code": "missing_capability",
                   "reason": payload_reason, "missing_capabilities": ["payload_drop"],
                   "unresolved_refs": [], "question": None}
        payload_positive = plan(good_payload, [S("s1", "deliver", {
            "target_ref": place_ref, "payload_ref": f"payload:{payload_ref}"}, "s2", "s2"), H("s2")], ["delivery"])
        unknown_ref = "poi:" + re.sub(r"[^a-z0-9]+", "_", unknown_en.casefold()).strip("_")
        unknown_candidate = plan(bad_place, [S("s1", "goto", {"target_ref": unknown_ref}, "s2", "s2"), H("s2")], ["status"])
        unknown_decision = {"plan_version": 1, "decision": "clarify", "reason_code": "unknown_place",
                            "reason": unknown_reason, "missing_capabilities": [],
                            "unresolved_refs": [unknown_ref], "question": unknown_question}
        known_positive = plan(good_place, [S("s1", "goto", {"target_ref": place_ref}, "s2", "s2"),
                                           H("s2")], ["positions"])
        if payload_refusals < args.r27_refusal_count:
            if (emit(bad_payload, refs, (), missing, "r27_missing_capability", valid=False,
                     code="missing_capability", candidate=payload_candidate)
                    and emit(good_payload, refs, ("payload_drop",), payload_positive,
                             "r27_missing_capability_positive", valid=True, code="missing_capability")):
                payload_refusals += 1
        if unknown_refusals < args.r27_refusal_count:
            if (emit(bad_place, refs, (), unknown_decision, "r27_unknown_place", valid=False,
                     code="unknown_place", candidate=unknown_candidate)
                    and emit(good_place, refs, (), known_positive,
                             "r27_unknown_place_positive", valid=True, code="unknown_place")):
                unknown_refusals += 1
        index += 1
        attempts += 1

    # Difficult positives span multi-step, conversational, scoped-negation,
    # local-position, mapping, counting, and monitoring requests.
    rng = random.Random(f"{args.seed}:r27-complex")
    family_sequences = (
        ("r27_multi_step", "patrol_goto_return"),
        ("r27_colloquial", "goto_hover_return"),
        ("r27_negation", "current_view_record"),
        ("r27_mapping", "survey_survey_land"),
        ("r27_monitoring", "monitor_monitor_return"),
    )
    family_openers = {
        "r27_multi_step": (
            ("依照步骤顺序，", "这趟按先后次序完成：", "几项操作依次进行：", "先后安排如下：",
             "按流程逐项执行：", "本次需要连续完成：", "把各项操作接起来：", "任务分步进行：",
             "从第一项开始依序处理：", "这些步骤按顺序衔接：", "先做前项再接后项：", "连贯完成这些操作："),
            ("Following the requested order, ", "In sequence, ", "Work through these actions in order: ",
             "The steps run as follows: ", "Carry out each action in sequence: ", "For this sortie, ",
             "Start with the first action: ", "Handle the steps one by one: ", "Run these actions consecutively: ",
             "Keep the actions in sequence: ", "Move from one step to the next: ", "Complete the sequence: "),
        ),
        "r27_colloquial": (
            ("有空时，", "方便的话，", "这次想请你，", "能否帮忙，", "请顺手，", "劳烦安排：",
             "想麻烦你，", "抽空处理一下：", "能帮我，", "请帮着，", "这趟请你，", "方便时帮忙："),
            ("Could you ", "Can you ", "Please ", "For this flight, ", "On this sortie, ", "Let's ",
             "When ready, ", "I would like you to ", "Go ahead and ", "Take a moment to ",
             "It would help to ", "If convenient, "),
        ),
        "r27_negation": (
            ("不用扩大搜索范围，", "不必再扫查其他区域，", "保持搜索范围有限，", "其他区域无需检查，",
             "不要转去别的区域，", "无需额外搜索，", "只处理眼前目标，", "跳过全域搜索，",
             "不要重新搜索，", "搜索限制在当前视野内，", "不需要继续找目标，", "勿扩大扫描范围，"),
            ("No wider search is needed; ", "Skip any search outside the current view; ",
             "Keep the search scoped to what is visible; ", "Do not scan other areas; ",
             "There is no need to search elsewhere; ", "Avoid a broad search; ",
             "Leave other areas unchecked; ", "Do not expand the search area; ",
             "Search only what is already in view; ", "No additional search is required; ",
             "Keep this to the current view; ", "Do not start a new search; "),
        ),
        "r27_mapping": (
            ("两处都要完成测绘：", "按两个区域分别测绘：", "连续覆盖这两处：", "先后获取两处地图：",
             "这趟安排两片区域的航测：", "两块区域都纳入测绘：", "为这两个区域采集影像：",
             "分区域完成地图采集：", "把两处都测一遍：", "分别覆盖两个测区：", "连做两处航测：",
             "两处地图都要收齐："),
            ("Map both areas: ", "Cover the two regions in turn: ", "Collect imagery over both sites: ",
             "The mapping covers two areas: ", "Survey each region in sequence: ", "For both areas, ",
             "Build coverage over the two sites: ", "Run the two mapping passes: ", "Map the first area, then the second: ",
             "Capture both regions: ", "Include both sites in the survey: ", "Complete the two area surveys: "),
        ),
        "r27_monitoring": (
            ("按时段轮流观察：", "两个区域都要盯一遍：", "分段监视这两处：", "依次留意两片区域：",
             "把观察任务分到两处：", "连续观察两个区域：", "每个时段各看一处：", "先后监看两片区域：",
             "两处都安排观察：", "轮换查看两个区域：", "分别监视这两个地点：", "按顺序跟进两处："),
            ("Watch the two areas in turn: ", "Split the monitoring between both sites: ",
             "Keep an eye on each area: ", "Monitor both regions in sequence: ",
             "The watch covers two locations: ", "For the two monitoring periods, ",
             "Move through both watch areas: ", "Check each region in turn: ",
             "Monitor one area, then the other: ", "Keep both sites under observation: ",
             "Run the two monitoring periods: ", "Cover both locations during the watch: "),
        ),
    }
    index = 0
    attempts = 0
    while index < args.r27_complex_count:
        if attempts > args.r27_complex_count * 30 + 100:
            raise ValueError(
                "r27 complex-positive pool exhausted after holdout filtering "
                f"(accepted={index}, attempts={attempts}, family={family})"
            )
        family, sequence = family_sequences[index % len(family_sequences)]
        if index % 10 == 8:
            n = 2 + ((index // 10) % 8)
            query, query_zh = rng.choice(OBJECTS)
            text = (f"院子里有多少{query_zh}就报多少，数到{n}个就停，然后返航。"
                    if (index // 10) % 2 == 0 else
                    f"Count {english_plural(query)} in the yard up to {n}, report the total, then return home.")
            steps = [S("s1", "count", {"region": "zone:yard", "query": query, "max_count": n}, "s2", "s2"), H("s2")]
            report, family = ["count", "positions"], "r27_counting"
        elif index % 10 == 9:
            spots = (("当前位置", "the current position"), ("此处", "this spot"),
                     ("飞机所在地点", "the aircraft's present location"), ("眼下这个点", "where you are now"),
                     ("脚下", "right here"), ("当前航迹点", "the current waypoint"),
                     ("所在位置", "the present location"), ("现在这个地点", "this location"),
                     ("当前坐标", "the current coordinates"))
            forms = (
                ("在{zh}降落。", "Land at {en}."),
                ("把飞机降在{zh}。", "Set the aircraft down at {en}."),
                ("飞行任务在{zh}结束并着陆。", "End the flight with a landing at {en}."),
                ("不必转场，直接在{zh}落地。", "Skip repositioning and touch down at {en}."),
                ("以{zh}作为终点，随后降落。", "Use {en} as the endpoint and land there."),
            )
            local_texts = tuple((zh.format(zh=spot_zh), en.format(en=spot_en))
                               for spot_zh, spot_en in spots for zh, en in forms)
            local_index = index // 10
            text_zh, text_en = local_texts[(local_index + attempts) % len(local_texts)]
            text = text_zh if (local_index + attempts) % 2 == 0 else text_en
            steps, report, family = [L("s1")], ["status"], "r27_local_position"
        else:
            text, steps, report = r23_target_case(rng, sequence, natural_plural=True)
            zh_text = bool(re.search(r"[\u4e00-\u9fff]", text))
            openers = family_openers[family][0 if zh_text else 1]
            opener = openers[(index // len(family_sequences)) % len(openers)]
            if zh_text:
                text = opener + text
            else:
                text = opener + text[0].lower() + text[1:]
        text = vary_terminal(text, steps, index)
        refs = canonical_refs(steps)
        row_plan = plan(text, steps, report)
        if check_plan_intent(row_plan, text, tuple(refs)):
            attempts += 1
            continue
        if emit(text, refs, (), row_plan, family, valid=True):
            index += 1
        attempts += 1
    return rows


def r28_rows(args, held_out: set[str], seen: set[str]) -> list[dict]:
    """Add stop-at-threshold intent and paired multirotor/fixed-wing contrasts."""
    rows = []
    labels = {ref: f"{zh} / {en}" for ref, zh, en in ZONES + POIS + ROUTES}

    def emit(text, refs, capabilities, target, family, *, valid, reason_code=None):
        key = normalize_task(text)
        if key in held_out or key in seen:
            return False
        if valid:
            validate_mission_plan(target, capabilities=frozenset(capabilities))
            findings = check_plan_intent(target, text, tuple(refs))
            capability = capability_violations(
                target, capabilities,
                airframe="airframe:fixed_wing" if "airframe:fixed_wing" in capabilities else None,
            )
            if findings or capability:
                raise ValueError(f"r28 positive validation failed ({family}): {findings} {capability}: {text}")
        else:
            validate_plan_decision(target)
            if reason_code != "missing_capability":
                raise ValueError(f"r28 unsupported refusal reason for {family}: {reason_code}")
        seen.add(key)
        rows.append({
            "sample_id": f"plan-r28-{family}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]}",
            "kind": "mission_plan", "world": "synthetic_plan_r28", "family": family,
            "pair_role": "positive" if valid else "refusal", "reason_code": reason_code,
            "task_text": text, "refs": list(refs), "capabilities": list(capabilities),
            "expected_valid": valid, "expected_findings": [] if valid else [reason_code],
            "prompt": plan_prompt(text, refs=tuple(refs), capabilities=frozenset(capabilities),
                                  include_decision_contract=True),
            "assistant_target": target,
        })
        return True

    def plan(text, steps, report):
        return {"plan_version": 1, "goal": text[:160], "steps": steps, "report": report}

    def terminal(skill):
        return S("s2", skill, {}, None, None)

    stop_zh = (
        "凑够{n}辆就停手", "数到{n}辆为止", "够{n}辆就收工", "累计到{n}辆就停止计数",
        "达到{n}辆后别再数", "最多数到{n}辆就结束", "数满{n}辆便停下", "看到第{n}辆就停",
    )
    stop_en = (
        "stop once you reach {n}", "count only until you get to {n}", "call it done at {n}",
        "stop counting when you have {n}", "quit once the tally reaches {n}", "count up to {n} and stop",
        "wrap up when you hit {n}", "do not count past {n}",
    )
    land_zh = ("数够{n}辆后就地降落", "达到{n}辆便降落", "数到{n}辆就落地结束", "凑齐{n}辆后着陆")
    land_en = ("land here once you reach {n}", "land after counting {n}", "touch down when the count reaches {n}",
               "stop at {n} and land here")
    object_pairs = [("vehicles", "车辆"), ("cars", "汽车"), ("bicycles", "自行车"),
                    ("motorcycles", "摩托车"), ("people", "行人"), ("trucks", "卡车")]
    zone_pool = [item for item in ZONES if item[0] != "zone:all"]
    route_pool = ROUTES
    index = 0
    stop_emitted = 0
    while stop_emitted < args.r28_stop_count:
        if index > args.r28_stop_count * 100 + 1000:
            raise ValueError(f"r28 stop-threshold template pool exhausted: {stop_emitted}/{args.r28_stop_count}")
        index += 1
        number = (6, 8, 10, 12, 15, 20)[(index // 2) % 6]
        zone, zone_zh, zone_en = zone_pool[(index // 4) % len(zone_pool)]
        object_en, object_zh = object_pairs[(index // 8) % len(object_pairs)]
        use_zh = index % 2 == 0
        land = index % 4 == 0
        mode = index % 3
        route, route_zh, route_en = route_pool[(index // 6) % len(route_pool)]
        if mode == 0 and use_zh:
            ending = (land_zh if land else stop_zh)[(index // 4) % (len(land_zh) if land else len(stop_zh))].format(n=zh_number(number))
            text = f"在{zone_zh}里清点{object_zh}，{ending}。"
        elif mode == 0:
            ending = (land_en if land else stop_en)[(index // 4) % (len(land_en) if land else len(stop_en))].format(n=number)
            text = f"Count {object_en} in {zone_en}; {ending}."
        elif mode == 1 and use_zh:
            ending = (land_zh if land else stop_zh)[(index // 4) % (len(land_zh) if land else len(stop_zh))].format(n=zh_number(number))
            text = f"在{zone_zh}搜索{object_zh}并报数，{ending}。"
        elif mode == 1:
            ending = (land_en if land else stop_en)[(index // 4) % (len(land_en) if land else len(stop_en))].format(n=number)
            text = f"Search {zone_en} for {object_en} and report the tally; {ending}."
        elif use_zh:
            ending = (land_zh if land else stop_zh)[(index // 4) % (len(land_zh) if land else len(stop_zh))].format(n=zh_number(number))
            text = f"沿{route_zh}巡逻并观察{object_zh}，完成{zh_number(number)}圈后停手" + ("并就地降落。" if land else "。")
        else:
            text = f"Patrol {route_en} while watching for {object_en}; stop after completing {number} loops" + (" and land here." if land else ".")
        refs = (f"{zone} ({labels[zone]})",)
        end_skill = "land" if land else "return_home"
        if mode == 0:
            steps = [S("s1", "count", {"region": zone, "query": object_en, "max_count": number}, "s2", "s2"),
                     terminal(end_skill)]
            report = ["count", "positions"]
        elif mode == 1:
            steps = [S("s1", "area_search", {"region": zone, "query": object_en}, "s2", "s3"),
                     S("s2", "count", {"region": zone, "query": object_en, "max_count": number}, "s3", "s3"),
                     S("s3", end_skill, {}, None, None)]
            report = ["findings", "count", "positions"]
        else:
            refs = (f"{route} ({labels[route]})",)
            steps = [S("s1", "patrol", {"route_ref": route, "query": object_en, "loops": number}, "s2", "s2"),
                     terminal(end_skill)]
            report = ["alerts"]
        target = plan(text, steps, report)
        if emit(text, refs, (), target, "r28_stop_threshold", valid=True):
            stop_emitted += 1

    return_zh = ("场地上的电瓶车帮我点一下数，到{n}辆就别再数了。",
                 "数一数院子里的电瓶车，数到{n}辆为止。",
                 "统计停车场里的电瓶车，够{n}辆就收工。",
                 "帮我清点场地中的电瓶车，累计到{n}辆后停止。")
    return_en = ("Count electric scooters in the lot and stop once you reach {n}.",
                 "Tally the electric scooters in the yard; quit when the count gets to {n}.",
                 "Count the electric scooters in the field, up to {n} and no further.",
                 "Report the electric scooters in the lot and stop counting at {n}.")
    return_zones = [("zone:field", "场地", "the field"), ("zone:yard", "院子", "the yard"),
                    ("zone:lot", "停车场", "the lot")]
    index = 0
    return_emitted = 0
    while return_emitted < args.r28_return_count:
        if index > args.r28_return_count * 100 + 1000:
            raise ValueError(f"r28 return-intent template pool exhausted: {return_emitted}/{args.r28_return_count}")
        index += 1
        number = (8, 10, 12, 15, 20)[(index // 2) % 5]
        zone, zone_zh, zone_en = return_zones[(index // 8) % len(return_zones)]
        use_zh = index % 2 == 0
        forms = return_zh if use_zh else return_en
        text = forms[(index // 2) % len(forms)].format(n=zh_number(number) if use_zh else number)
        query = "electric scooters" if not use_zh else "电瓶车"
        target = plan(text, [S("s1", "count", {"region": zone, "query": query,
                                                   "max_count": number}, "s2", "s2"),
                             terminal("return_home")], ["count", "positions"])
        if emit(text, (f"{zone} ({labels[zone]})",), (), target, "r28_return_intent", valid=True):
            return_emitted += 1

    examples = {
        "r28_fixed_wing_hover": {
            "capability": "hover", "skill": "hover", "params": {"duration_s": 60},
            "zh": ("在{zone_zh}上空悬停一分钟观察。", "请在{zone_zh}上方悬停六十秒。",
                   "到{zone_zh}上空定点悬停一分钟。", "在{zone_zh}上空保持悬停六十秒观察。"),
            "en": ("Hover over {zone_en} for one minute and observe.", "Please hover above {zone_en} for sixty seconds.",
                   "Hold a hover over {zone_en} for one minute.", "Stay hovering above {zone_en} for sixty seconds."),
        },
        "r28_fixed_wing_target_land": {
            "capability": "landing_on_target", "skill": "land", "params": {"target_ref": "{poi}"},
            "zh": ("飞到{poi_zh}上方定点降落。", "请在{poi_zh}精确着陆。",
                   "前往{poi_zh}并降落在指定点。", "把飞机降落到{poi_zh}这个目标点。"),
            "en": ("Fly to {poi_en} and land precisely on it.", "Please land exactly at {poi_en}.",
                   "Proceed to {poi_en} and touch down on the marked point.", "Set down on the target at {poi_en}."),
        },
    }
    for family, spec in examples.items():
        made = 0
        pair_index = 0
        while made < args.r28_fixed_wing_count:
            if pair_index > args.r28_fixed_wing_count * 10 + 100:
                raise ValueError(f"r28 fixed-wing template pool exhausted for {family}: {made}/{args.r28_fixed_wing_count}")
            pair_index += 1
            zone, zone_zh, zone_en = zone_pool[(pair_index // 2) % len(zone_pool)]
            poi, poi_zh, poi_en = POIS[(pair_index // 2) % len(POIS)]
            use_zh = pair_index % 2 == 1
            forms = spec["zh"] if use_zh else spec["en"]
            text = forms[(pair_index // 2) % len(forms)].format(
                zone_zh=zone_zh, poi_zh=poi_zh, zone_en=zone_en, poi_en=poi_en)
            region = zone if spec["skill"] == "hover" else None
            params = ({"region": region, **spec["params"]} if region else {"target_ref": poi})
            refs = (f"{zone} ({labels[zone]})",) if region else (f"{poi} ({labels[poi]})",)
            steps = ([S("s1", "goto", {"target_ref": poi}, "s2", "s2"),
                      S("s2", "land", params, None, None)]
                     if spec["skill"] == "land" else
                     [S("s1", "hover", params, "s2", "s2"), terminal("return_home")])
            multi_caps = ("airframe:multirotor", spec["capability"])
            multi_plan = plan(text, steps, [spec["skill"]])
            if not emit(text, refs, multi_caps, multi_plan, family, valid=True):
                continue
            # Add a distinct fixed-wing-context version of the same intent.
            # The task text is identical; aircraft context lives in the prompt capabilities.
            refusal = {"plan_version": 1, "decision": "reject", "reason_code": "missing_capability",
                       "reason": f"This fixed-wing aircraft does not support {spec['capability']}.",
                       "missing_capabilities": [spec["capability"]], "unresolved_refs": [], "question": None}
            # Context is encoded in the prompt so normalize_task remains the same; use a
            # separate seen key for paired aircraft contexts while preserving task text.
            key = normalize_task(text) + "|airframe:fixed_wing"
            if key in held_out or key in seen:
                raise ValueError(f"r28 fixed-wing pair collided: {text}")
            validate_plan_decision(refusal)
            candidate = plan(text, steps, [spec["skill"]])
            capability = capability_violations(candidate, ("airframe:fixed_wing",),
                                               airframe="airframe:fixed_wing")
            if not any(item.get("code") == "missing_capability" for item in capability):
                raise ValueError(f"r28 fixed-wing refusal not supported by capability contract: {text}: {capability}")
            seen.add(key)
            rows.append({
                "sample_id": f"plan-r28-{family}-fixed-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]}",
                "kind": "mission_plan", "world": "synthetic_plan_r28", "family": family,
                "pair_role": "refusal", "reason_code": "missing_capability", "task_text": text,
                "refs": list(refs), "capabilities": ["airframe:fixed_wing"], "expected_valid": False,
                "expected_findings": ["missing_capability"],
                "prompt": plan_prompt(text, refs=refs, capabilities=frozenset(("airframe:fixed_wing",)),
                                      include_decision_contract=True),
                "assistant_target": refusal, "rejected_candidate": candidate,
                "negative_target_mode": "rejected_candidate",
            })
            made += 2
    return rows


def r29_rows(args, held_out: set[str], seen: set[str]) -> list[dict]:
    """Add executable contrasts for query scope, landing, aliases, and return intent."""
    rows = []
    zones = [("zone:lot", "停车场", "the lot"), ("zone:field", "场地", "the field"),
             ("zone:yard", "院子", "the yard"), ("zone:farm", "农田", "the farm")]
    pois = [("poi:gate", "大门", "the gate", "入口", "entrance"),
            ("poi:tower", "水塔", "the tower", "塔楼", "tower"),
            ("poi:shed", "仓库", "the warehouse", "库房", "warehouse"),
            ("poi:bridge", "桥", "the bridge", "桥梁", "bridge")]
    routes = [("route:fence", "围栏路线", "the fence route"),
              ("route:north", "北线", "the north route"),
              ("route:road", "道路路线", "the road route")]
    objects = [("red car", "红色轿车", "ambulance", "救护车"),
               ("bicycle", "自行车", "fire truck", "消防车")]
    ref_aliases = [("poi:gate", "大门", "the gate", "入口", "the entrance"),
                   ("poi:tower", "水塔", "the tower", "塔区", "the tower area"),
                   ("poi:shed", "仓库", "the warehouse", "库房", "the storage building"),
                   ("poi:bridge", "桥", "the bridge", "桥梁", "the bridge span")]
    zh_tail = ("返航", "回到起飞点", "回家", "返场", "飞回基地", "返回出发地",
               "飞回放飞点", "回到基地", "返回起点", "回到出发地", "飞回起飞点", "回基地")
    en_tail = ("then return home", "then head back to the launch point", "then come back to base",
               "then fly back", "then return to launch", "then head home", "then go back to base",
               "then fly home", "then return to the start", "then head back to launch",
               "then come back", "then fly to the launch point")

    def emit(text, refs, target, family, role):
        key = normalize_task(text)
        if key in held_out or key in seen:
            return False
        validate_mission_plan(target)
        findings = check_plan_intent(target, text, tuple(refs))
        if findings:
            raise ValueError(f"r29 intent failure ({family}): {findings}: {text}")
        seen.add(key)
        rows.append({"sample_id": f"plan-r29-{family}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]}",
                     "kind": "mission_plan", "world": "synthetic_plan_r29", "family": family,
                     "pair_role": role, "task_text": text, "refs": list(refs), "capabilities": [],
                     "expected_valid": True, "expected_findings": [],
                     "prompt": plan_prompt(text, refs=refs, capabilities=frozenset(),
                                           prompt_variant=args.prompt_variant),
                     "assistant_target": target})
        return True

    def plan(text, steps, report):
        return {"plan_version": 1, "goal": text[:160], "steps": steps, "report": report}

    specs = (
        ("query_ungrounded", args.r29_query_ungrounded_count),
        ("return_negated_land", args.r29_return_negated_land_count),
        ("query_grounding", args.r29_query_grounding_count),
        ("ref_alias", args.r29_ref_alias_count),
        ("return_intent", args.r29_return_intent_count),
    )
    paired_roles = {
        "query_ungrounded": ("negated_category_excluded", "affirmative_control"),
        "return_negated_land": ("land_not_return", "return_not_land"),
        "query_grounding": ("explicit_query", "alias_query_control"),
        "ref_alias": ("named_alias_reference", "canonical_name_control"),
        "return_intent": ("explicit_return_intent", "no_return_requested"),
    }
    for family, count in specs:
        family_kind = family.removeprefix("r29_")
        index = 0
        attempts = 0
        language_rows = {"zh": 0, "en": 0}
        role_language_rows = Counter()
        while index < count:
            if attempts >= count * 100 + 100:
                raise ValueError(f"r29 template pool exhausted for {family}: {index}/{count}")
            zh = language_rows["zh"] < (count + 1) // 2
            variant = attempts // 4
            positive_role, control_role = paired_roles[family_kind]
            language = "zh" if zh else "en"
            contrast = role_language_rows[(language, control_role)] < role_language_rows[(language, positive_role)]
            if family_kind == "query_ungrounded":
                query, query_zh, excluded, excluded_zh = objects[variant % len(objects)]
                zone, zone_zh, zone_en = zones[(variant // len(objects)) % len(zones)]
                if zh:
                    text = (f"在{zone_zh}{'搜索' if variant % 3 == 0 else '巡查'}{query_zh}，"
                            + (f"并查找{excluded_zh}，{zh_tail[variant % len(zh_tail)]}。" if contrast else
                               f"不要把{excluded_zh}另编成查询，{zh_tail[variant % len(zh_tail)]}。"))
                else:
                    verb = ("Search" if variant % 3 == 0 else "Check")
                    query_text = english_plural(query)
                    excluded_text = english_plural(excluded)
                    excluded_phrase = (excluded_text if excluded_text.endswith("s")
                                       else f"{'an' if excluded_text[0].lower() in 'aeiou' else 'a'} {excluded_text}")
                    text = (f"{verb} {zone_en} for {query_text}" +
                            (f" and {excluded_phrase}; {en_tail[variant % len(en_tail)]}." if contrast else
                             f", but do not add an invented {excluded_text} query; {en_tail[variant % len(en_tail)]}."))
                steps = [S("s1", "area_search", {"region": zone, "query": query},
                           "s2" if contrast else "s3", "s3")]
                if contrast:
                    steps.append(S("s2", "area_search", {"region": zone, "query": excluded}, "s3", "s3"))
                steps.append(H("s3"))
                target = plan(text, steps, ["findings", "positions"])
                role = "affirmative_control" if contrast else "negated_category_excluded"
            elif family_kind == "return_negated_land":
                poi, poi_zh, poi_en, alias_zh, alias_en = ref_aliases[variant % len(ref_aliases)]
                views = 2 + variant % 7
                if contrast:
                    text = (f"去{poi_zh}从{zh_number(views)}个角度核对标志，不要降落，{zh_tail[variant % len(zh_tail)]}。" if zh else
                            f"Go to {poi_en} and inspect the sign from {views} views; do not land, {en_tail[variant % len(en_tail)]}.")
                    terminal = H
                    role = "return_not_land"
                else:
                    text = (f"去{poi_zh}从{zh_number(views)}个角度核对标志，不要返航，任务结束就在当地降落。" if zh else
                            f"Go to {poi_en} and inspect the sign from {views} views; do not return home, land there when done.")
                    terminal = L
                    role = "land_not_return"
                refs = (f"{poi} ({poi_zh} / {poi_en} / {alias_zh} / {alias_en})",)
                query = ("标志" if variant % 2 == 0 else "入口") if zh else "sign"
                target = plan(text, [S("s1", "goto", {"target_ref": poi}, "s2", "s2"),
                                     S("s2", "inspect", {"target_ref": poi, "query": query,
                                                          "standoff_m": 12, "views": views}, "s3", "s3"),
                                     terminal("s3")], ["findings", "views", "positions"])
            elif family_kind == "query_grounding":
                query, query_zh, _excluded, _excluded_zh = objects[variant % len(objects)]
                poi, poi_zh, poi_en, alias_zh, alias_en = ref_aliases[(variant // len(objects)) % len(ref_aliases)]
                if zh:
                    text = (f"到{alias_zh if contrast else poi_zh}附近找{query_zh}，找到后拍照留档，随后回起飞点。"
                            if variant % 3 == 0 else
                            f"在{poi_zh if not contrast else alias_zh}查看{query_zh}并记下位置，{zh_tail[variant % len(zh_tail)]}。")
                else:
                    text = (f"At {alias_en if contrast else poi_en}, find the {query} and save a photo, then return to the launch point."
                            if variant % 3 == 0 else
                            f"Check {alias_en if contrast else poi_en} for {query}, note its position, and {en_tail[variant % len(en_tail)]}.")
                refs = (f"{poi} ({poi_zh} / {poi_en} / {alias_zh} / {alias_en})",)
                target = plan(text, [S("s1", "goto", {"target_ref": poi}, "s2", "s2"),
                                     S("s2", "inspect", {"target_ref": poi, "query": query,
                                                          "standoff_m": 10, "views": 3}, "s3", "s3"),
                                     S("s3", "record", {"query": query}, "s4", "s4"), H("s4")],
                              ["findings", "evidence", "positions"])
                refs = (f"{poi} ({poi_zh} / {poi_en} / {alias_zh} / {alias_en})",)
                role = "alias_query_control" if contrast else "explicit_query"
            elif family_kind == "ref_alias":
                poi, poi_zh, poi_en, alias_zh, alias_en = ref_aliases[variant % len(ref_aliases)]
                views = 2 + variant % 9
                query_zh = "标牌"
                query_en = "sign"
                canonical_en = poi_en.removeprefix("the ")
                alias_place_en = alias_en
                if zh:
                    text = (f"去{poi_zh if contrast else alias_zh}核对{query_zh}，拍{zh_number(views)}个角度后返航。" if variant % 3 == 0
                            else f"到{poi_zh if contrast else alias_zh}附近检查{query_zh}，从{zh_number(views)}个角度拍摄后返航。")
                else:
                    text = (f"Go to {canonical_en if contrast else alias_place_en}, inspect the {query_en} from {views} views, then return home."
                            if variant % 3 == 0 else
                            f"At {canonical_en if contrast else alias_place_en}, check the {query_en} from {views} views and return to base.")
                refs = (f"{poi} ({poi_zh} / {poi_en} / {alias_zh} / {alias_en})",)
                query = query_zh if zh else query_en
                target = plan(text, [S("s1", "goto", {"target_ref": poi}, "s2", "s2"),
                                     S("s2", "inspect", {"target_ref": poi, "query": query,
                                                          "standoff_m": 12, "views": views}, "s3", "s3"),
                                     H("s3")], ["findings", "views", "positions"])
                role = control_role if contrast else positive_role
            else:
                query, query_zh, _excluded, _excluded_zh = objects[variant % len(objects)]
                zone, zone_zh, zone_en = zones[(variant // len(objects)) % len(zones)]
                number = (3, 5, 8, 10, 12)[variant % 5]
                if zh:
                    text = (f"清点{zone_zh}的{query_zh}，数到{zh_number(number)}个就停，{zh_tail[variant % len(zh_tail)]}。"
                            if not contrast else f"清点{zone_zh}的{query_zh}，数到{zh_number(number)}个就停，然后在这里降落。")
                else:
                    text = (f"Count {english_plural(query)} in {zone_en} up to {number}, stop there; {en_tail[variant % len(en_tail)]}."
                            if not contrast else f"Count {english_plural(query)} in {zone_en} up to {number}, then stop and land here.")
                terminal = L if contrast else H
                target = plan(text, [S("s1", "count", {"region": zone, "query": query,
                                                         "max_count": number}, "s2", "s2"),
                                     terminal("s2")], ["count", "positions"])
                role = "explicit_return_intent" if not contrast else "no_return_requested"
            if family_kind in {"query_ungrounded", "return_intent"}:
                refs = (f"{zone} ({zone_zh} / {zone_en})",)
            if emit(text, refs, target, family, role):
                index += 1
                language_rows["zh" if zh else "en"] += 1
                role_language_rows[(language, role)] += 1
            attempts += 1
            # Avoid carrying a reference from the previous family iteration.
            refs = ()
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
    parser.add_argument("--r26-recipe", action="store_true",
                        help="extend r24 with structured reject/clarify decisions; excludes r25 rejected candidates")
    parser.add_argument("--r26-refusal-count", type=int, default=60,
                        help="refusal/clarification rows per reason_code; the recipe adds an equal positive count (default: 60)")
    parser.add_argument("--r27-recipe", action="store_true",
                        help="extend r24 with narrow structured refusals and difficult executable positives")
    parser.add_argument("--r27-refusal-count", type=int, default=40,
                        help="refusal rows per supported reason_code, each paired with one positive (default: 40)")
    parser.add_argument("--r27-complex-count", type=int, default=400,
                        help="difficult executable positives (default: 400)")
    parser.add_argument("--r28-recipe", action="store_true",
                        help="extend r27 with threshold-stop intent and aircraft-capability contrasts")
    parser.add_argument("--r28-stop-count", type=int, default=80,
                        help="count/search/patrol threshold-stop rows (default: 80)")
    parser.add_argument("--r28-fixed-wing-count", type=int, default=30,
                        help="contrast rows per fixed-wing family (default: 30)")
    parser.add_argument("--r28-return-count", type=int, default=30,
                        help="return-home intent rows (default: 30)")
    parser.add_argument("--r29-recipe", action="store_true",
                        help="extend the r24 data recipe with executable planning-error contrasts")
    parser.add_argument("--r29-query-ungrounded-count", type=int, default=72)
    parser.add_argument("--r29-return-negated-land-count", type=int, default=72)
    parser.add_argument("--r29-query-grounding-count", type=int, default=72)
    parser.add_argument("--r29-ref-alias-count", type=int, default=72)
    parser.add_argument("--r29-return-intent-count", type=int, default=72)
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
    if args.r26_refusal_count < 0 or args.r26_refusal_count % 2:
        parser.error("r26 refusal count must be non-negative and even for an equal Chinese/English split")
    if args.r27_refusal_count < 0 or args.r27_complex_count < 0:
        parser.error("r27 counts must be non-negative")
    if any(value < 0 for value in (args.r28_stop_count, args.r28_fixed_wing_count,
                                   args.r28_return_count)):
        parser.error("r28 counts must be non-negative")
    if args.r28_fixed_wing_count % 2:
        parser.error("r28 fixed-wing contrast count must be even for paired aircraft contexts")
    if args.r25_recipe:
        args.r24_recipe = True
    if args.r26_recipe:
        args.r24_recipe = True
    if args.r28_recipe:
        # Must run before the r27 expansion below, which r28 includes.
        args.r27_recipe = True
    if args.r29_recipe:
        # r29 inherits r24's positive/replay distribution only; no r25-r28 refusal recipe.
        args.r23_recipe = True
        args.r24_recipe = True
        args.r24_unknown_count = 0
    if args.r27_recipe:
        args.r23_recipe = True
        args.r24_recipe = True
        # r27 emits its own paired unknown-place decisions at the requested count.
        args.r24_unknown_count = 0
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
    if getattr(args, "r28_recipe", False) or getattr(args, "r29_recipe", False):
        # The 15 return-home probe tasks gate planner replacement too; r28 targets
        # their failure mode, so keep them (and near-duplicates) out of training.
        probe = ROOT / "configs/onboard/plan-probe-tasks-return-home.json"
        held_out |= {
            normalize_task(task["text"])
            for task in json.loads(probe.read_text(encoding="utf-8"))["tasks"]
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
    if args.r25_recipe and not args.r26_recipe:
        new_rows = r25_rows(args, held_out, seen)
        rows.extend(new_rows)
        args.rows += len(new_rows)
    if args.r26_recipe:
        new_rows = r26_rows(args, held_out, seen, rows)
        rows.extend(new_rows)
        args.rows += len(new_rows)
    if args.r27_recipe:
        new_rows = r27_rows(args, held_out, seen)
        rows.extend(new_rows)
        args.rows += len(new_rows)
        # Keep the refusal fraction within the contract even for small custom
        # runs; the filler rows are ordinary executable v4 positives.
        refusal_rows = sum(
            row.get("assistant_target", {}).get("decision") in {"reject", "clarify"}
            for row in rows
        )
        args.rows = max(args.rows, math.ceil(refusal_rows / 0.02))
    if args.r28_recipe:
        new_rows = r28_rows(args, held_out, seen)
        rows.extend(new_rows)
        args.rows += len(new_rows)
        refusal_rows = sum(
            row.get("assistant_target", {}).get("decision") in {"reject", "clarify"}
            for row in rows
        )
        args.rows = max(args.rows, math.ceil(refusal_rows / 0.02))
    if args.r29_recipe:
        new_rows = r29_rows(args, held_out, seen)
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
                                          "r25_negated_query", "r26_missing_capability", "r26_unknown_place",
                                          "r26_ungrounded_query", "r26_ambiguous_task", "r26_unsafe_request",
                                          "r26_payload_capable", "r27_missing_capability",
                                          "r27_missing_capability_positive", "r27_unknown_place",
                                          "r27_unknown_place_positive", "r27_multi_step",
                                          "r27_colloquial", "r27_negation", "r27_mapping",
                                          "r27_monitoring", "r27_counting", "r27_local_position",
                                          "r28_stop_threshold", "r28_fixed_wing_hover",
                                          "r28_fixed_wing_target_land", "r29_query_ungrounded",
                                          "r29_return_negated_land", "r29_query_grounding",
                                          "r29_ref_alias", "r29_return_intent"}:
                continue
            if isinstance(row.get("task_text"), str):
                old = row["task_text"]
                row["task_text"] = clean_r23_text(old)
                if isinstance(row.get("prompt"), str):
                    row["prompt"] = row["prompt"].replace(old, row["task_text"])
                if isinstance(row.get("assistant_target"), dict) and "goal" in row["assistant_target"]:
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
    if args.r26_recipe:
        r26_rows_all = [row for row in rows if row.get("reason_code") in {
            "missing_capability", "unknown_place", "ungrounded_query", "ambiguous_task", "unsafe_request"
        }]
        manifest["r26_recipe"] = {
            "refusal_requested_per_reason_code": args.r26_refusal_count,
            "rows_added": len(r26_rows_all),
            "reason_counts": {
                code: {
                    "refusal": sum(row.get("reason_code") == code and row.get("pair_role") == "refusal" for row in r26_rows_all)
                              + sum(row.get("reason_code") == code and row.get("pair_role") is None and row.get("expected_valid") is False for row in r26_rows_all),
                    "positive": sum(row.get("reason_code") == code and row.get("pair_role") == "positive" for row in r26_rows_all),
                }
                for code in ("missing_capability", "unknown_place", "ungrounded_query", "ambiguous_task", "unsafe_request")
            },
            "structured_targets_only": True,
            "r25_rejected_candidate_rows_included": False,
        }
    if args.r27_recipe:
        r27_rows_all = [row for row in rows if row.get("world") == "synthetic_plan_r27"]
        decisions = [row for row in r27_rows_all
                     if row.get("assistant_target", {}).get("decision") in {"reject", "clarify"}]
        refusals = [row for row in decisions if row["assistant_target"]["decision"] == "reject"
                    or row["assistant_target"]["reason_code"] == "unknown_place"]
        structured_codes = {row.get("reason_code") for row in decisions}
        if structured_codes - {"missing_capability", "unknown_place"}:
            raise ValueError(f"r27 emitted unsupported refusal reasons: {sorted(structured_codes)}")
        r27_refusal_share = len(refusals) / max(1, len(splits["train"]))
        if r27_refusal_share > 0.02:
            raise ValueError(f"r27 structured refusal share {r27_refusal_share:.4%} exceeds 2%")
        if len(decisions) != 2 * args.r27_refusal_count:
            raise ValueError(f"r27 structured refusal rows {len(decisions)} != {2 * args.r27_refusal_count}")
        complex_rows = [row for row in r27_rows_all
                        if row.get("pair_role") is None and row.get("expected_valid") is True]
        if len(complex_rows) < args.r27_complex_count:
            raise ValueError(f"r27 complex positive rows {len(complex_rows)} < {args.r27_complex_count}")
        manifest["r27_recipe"] = {
            "refusal_requested_per_reason_code": args.r27_refusal_count,
            "complex_positive_requested": args.r27_complex_count,
            "structured_refusal_rows": len(refusals),
            "structured_refusal_share_train": r27_refusal_share,
            "reason_counts": {
                code: {
                    "refusal": sum(row.get("reason_code") == code and row.get("assistant_target", {}).get("decision") in {"reject", "clarify"} for row in r27_rows_all),
                    "positive": sum(row.get("reason_code") == code and row.get("pair_role") == "positive" for row in r27_rows_all),
                } for code in ("missing_capability", "unknown_place")
            },
            "complex_families": dict(sorted(Counter(
                row.get("family") for row in complex_rows
            ).items())),
            "excluded_refusal_reasons": {
                "unsafe_request": "安全区域和危险区域由主机规则判断更可靠",
                "ambiguous_task": "缺少必要字段时由主机规则澄清更可靠",
                "ungrounded_query": "查询依据由主机规则检查更可靠",
            },
        }
    if args.r28_recipe:
        r28_rows_all = [row for row in rows if row.get("world") == "synthetic_plan_r28"]
        r28_decisions = [row for row in r28_rows_all
                         if row.get("assistant_target", {}).get("decision") in {"reject", "clarify"}]
        cumulative_decisions = [row for row in rows
                                if row.get("assistant_target", {}).get("decision") in {"reject", "clarify"}]
        r28_refusal_share = len(cumulative_decisions) / max(1, len(splits["train"]))
        if r28_refusal_share > 0.02:
            raise ValueError(f"r28 structured refusal share {r28_refusal_share:.4%} exceeds 2%")
        manifest["r28_recipe"] = {
            "stop_threshold_requested": args.r28_stop_count,
            "fixed_wing_contrast_rows_per_family_requested": args.r28_fixed_wing_count,
            "return_intent_requested": args.r28_return_count,
            "rows_added": len(r28_rows_all),
            "structured_refusal_rows": len(r28_decisions),
            "cumulative_structured_refusal_rows": len(cumulative_decisions),
            "structured_refusal_share_train": r28_refusal_share,
            "family_counts": dict(sorted(Counter(row["family"] for row in r28_rows_all).items())),
        }
    if args.r29_recipe:
        r29_all = [row for row in rows if row.get("world") == "synthetic_plan_r29"]
        manifest["r29_recipe"] = {
            "rows_added": len(r29_all),
            "family_counts": dict(sorted(Counter(row["family"] for row in r29_all).items())),
            "language_counts": dict(sorted(Counter(
                "zh" if re.search(r"[\u3400-\u9fff]", row["task_text"]) else "en"
                for row in r29_all).items())),
            "structured_refusal_rows": sum(
                row.get("assistant_target", {}).get("decision") in {"reject", "clarify"}
                for row in r29_all),
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
        sequences = [[step["skill"] for step in row["assistant_target"].get("steps", ())]
                     for row in part if isinstance(row.get("assistant_target"), dict)
                     and row["assistant_target"].get("steps")]
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
        stats = [f"#{' r28' if args.r28_recipe else ' r27' if args.r27_recipe else ' r26' if args.r26_recipe else ' r25' if args.r25_recipe else ' r24' if args.r24_recipe else ' r23'} planner dataset statistics", "",
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
        if args.r26_recipe:
            stats += ["", "r26 rows supervise structured reject/clarify decisions and exclude r25 rejected-candidate targets.", ""]
        if args.r27_recipe:
            stats += ["", f"Structured refusal rows: {len(decisions)} ({r27_refusal_share:.3%} of train; maximum 2%).",
                      f"Complex executable positives: {len(complex_rows)}.",
                      "", "| r27 family | Rows |", "|---|---:|---:|"]
            stats += [f"| {family} | {count} |" for family, count in sorted(Counter(
                row.get("family") for row in r27_rows_all).items())]
        if args.r28_recipe:
            stats += ["", f"Total structured refusal rows: {len(cumulative_decisions)} ({r28_refusal_share:.3%} of train; maximum 2%).",
                      "", "| r28 family | Rows |", "|---|---:|"]
            stats += [f"| {family} | {count} |"
                      for family, count in manifest["r28_recipe"]["family_counts"].items()]
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
