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
            validate_mission_plan(plan)
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
            # Keep each deterministic draw distinct even when a small place pool
            # is cycled; the sortie marker is semantically inert for the checks.
            text = f"{text}（第{index + 1}架次）" if re.search(r"[\u4e00-\u9fff]", text) else f"{text} (sortie {index + 1})"
            normalized = normalize_task(text)
            if normalized in held_out or any(normalized == row["_key"] for row in rows):
                return False
            if valid:
                validate_mission_plan(plan, capabilities=frozenset(capabilities))
            findings = check_plan_intent(plan, text, refs)
            cap_findings = capability_violations(
                plan, capabilities,
                airframe="airframe:fixed_wing" if "airframe:fixed_wing" in capabilities else None,
            )
            codes = {finding.code for finding in findings} | {finding["code"] for finding in cap_findings}
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
                "pair_role": "positive" if polarity == 0 else "paired_negative",
                "capabilities": list(capabilities), "expected_valid": valid,
                "expected_findings": [] if valid else [expected_code],
                "prompt": plan_prompt(text, refs=refs, prompt_variant=args.prompt_variant,
                                      capabilities=frozenset(capabilities)),
                "assistant_target": plan, "_key": normalized,
            })
            if rejected_candidate is not None:
                rows[-1]["rejected_candidate"] = rejected_candidate
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

        unknown_plan = {"plan_version":1,"goal":"unknown","steps":[S("s1","goto",{"target_ref":ref},"s2","s2"),H("s2")],"report":[]}
        unknown_text = (f"去地图没有的{fake_zh}，不要替换成{zh}。" if use_zh else
                        f"Fly to the nonexistent {fake_en}; do not substitute the {en}.")
        unknown_pos = (f"到{zh}核查后返航。", return_pos[1], return_pos[2], (), True)
        unknown_neg = (unknown_text, unknown_plan, refs_for(ref, zh, en, alias), (), False)
        add_pair(index, "unknown_place", unknown_pos, unknown_neg, "ungrounded_place")

        deliver_plan = {"plan_version":1,"goal":"deliver","steps":[S("s1","deliver",{"target_ref":ref,"payload_ref":"payload:parcel"},"s2","s2"),H("s2")],"report":[]}
        deliver_ok = (f"把包裹投放到{zh}后返航。" if use_zh else f"Deliver the parcel to the {en} and return home.")
        deliver_bad = (f"把包裹投放到{zh}，但本机没有载荷投送能力。" if use_zh else f"Deliver the parcel to the {en}; payload drop is unavailable.")
        deliver_positive = (deliver_ok, deliver_plan, refs_for(ref, zh, en, alias), ("payload_drop",), True)
        deliver_negative = (deliver_bad, deliver_plan, refs_for(ref, zh, en, alias), (), False)
        add_pair(index, "missing_payload_drop", deliver_positive, deliver_negative, "missing_capability")

        hover_plan = {"plan_version":1,"goal":"hover","steps":[S("s1","hover",{"region":zone},"s2","s2"),H("s2")],"report":[]}
        fixed_safe = (f"固定翼飞越{zone_zh}后返航。" if use_zh else f"A fixed-wing aircraft flies over the {zone_en} and returns home.")
        fixed_bad = (f"固定翼在{zone_zh}上空悬停。" if use_zh else f"A fixed-wing aircraft should hover over the {zone_en}.")
        fixed_safe_plan = {"plan_version":1,"goal":"fixed wing transit","steps":[S("s1","goto",{"target_ref":zone},"s2","s2"),H("s2")],"report":[]}
        fixed_positive = (fixed_safe, fixed_safe_plan, refs_for(ref, zh, en, alias) + (f"{zone} ({zone_zh} / {zone_en})",), ("airframe:fixed_wing",), True)
        fixed_negative = (fixed_bad, hover_plan, refs_for(ref, zh, en, alias), ("airframe:fixed_wing",), False)
        add_pair(index, "fixed_wing_constraint", fixed_positive, fixed_negative, "missing_capability")

        land_plan = {"plan_version":1,"goal":"fixed wing landing","steps":[S("s1","land",{"target_ref":ref},"s2","s2"),H("s2")],"report":[]}
        land_safe = (f"固定翼飞到{zone_zh}上空后返航。" if use_zh else f"The fixed-wing aircraft flies over the {zone_en} and returns home.", fixed_safe_plan, refs_for(ref, zh, en, alias) + (f"{zone} ({zone_zh} / {zone_en})",), ("airframe:fixed_wing",), True)
        land_bad = (f"固定翼飞到{zh}上方定点降落。" if use_zh else f"A fixed-wing aircraft should land precisely on the {en}.", land_plan, refs_for(ref, zh, en, alias), ("airframe:fixed_wing",), False)
        add_pair(index, "fixed_wing_target_land", land_safe, land_bad, "missing_capability")

        alias_text = (f"到{alias}确认位置，再回基地。" if use_zh else f"Go to the {alias}, verify it, and return to base.")
        alias_plan = {"plan_version":1,"goal":"alias","steps":[S("s1","goto",{"target_ref":ref},"s2","s2"),H("s2")],"report":[]}
        alias_pair = (alias_text, alias_plan, refs_for(ref, zh, en, alias), (), True)
        alias_bad = (f"到{fake_zh}确认位置，再回基地。" if use_zh else f"Go to the {fake_en}, verify it, and return to base.", alias_plan, refs_for(ref, zh, en, alias), (), False)
        add_pair(index, "ref_alias", alias_pair, alias_bad, "ungrounded_place")

        query = "red car" if index % 2 else "bicycle"
        query_zh = "红色轿车" if query == "red car" else "自行车"
        count_plan = {"plan_version":1,"goal":"count","steps":[S("s1","count",{"region":zone,"query":query,"max_count":10},"s2","s2"),H("s2")],"report":[]}
        query_good = (f"数{zone_zh}里的{query_zh}，然后返航。" if use_zh else f"Count {query}s in the {zone_en} and return home.", count_plan, refs_for(ref, zh, en, alias) + (f"{zone} ({zone_zh} / {zone_en})",), (), True)
        query_bad = (f"检查{zh}并返航。" if use_zh else f"Inspect the {en} and return home.", count_plan, refs_for(ref, zh, en, alias) + (f"{zone} ({zone_zh} / {zone_en})",), (), False)
        add_pair(index, "query_grounding", query_good, query_bad, "ungrounded_query")

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
    for name, part in splits.items():
        sequences = [[step["skill"] for step in row["assistant_target"]["steps"]] for row in part]
        counts = {
            "skill_counts": Counter(skill for sequence in sequences for skill in sequence),
            "first_skill_counts": Counter(sequence[0] for sequence in sequences),
            "final_skill_counts": Counter(sequence[-1] for sequence in sequences),
        }
        for key, counter in counts.items():
            manifest[key][name] = {skill: counter[skill] for skill in sorted(SKILLS)}
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
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: len(v) for k, v in splits.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
