"""Deterministic checks that a compiled plan does what the task text asks.

The planner (any slow model) can return a plan that is structurally valid and
uses only known references, yet contradicts the task: it lands at the
destination although the task says to come home, or it adds a search for an
object the task never mentions (copied from a training example).  These
checks run on the host after validation and before the plan is installed, so
they hold for every planning model.

Rules (the tables below are the complete vocabulary; see
``docs/onboard-multipurpose-mission-design.md`` for the rationale and the
measured false-block rates):

``return_intent_land``
    The task clearly asks to return home (Chinese/English return phrases, not
    negated) and does not ask to land; then no reachable terminal step may be a
    ``land`` away from home.  A task that asks to land (降落/落地/touch down/
    set down ...) keeps its ``land``.
``ungrounded_query``
    A search/count/inspect/record/follow ``query`` must name something the task
    text mentions: an English word of the query (singular/plural, small synonym
    table) appears in the text, or the query's object word has a known Chinese
    name that appears in the text.  A query whose words are all outside the
    tables is *undecided* and passes, so an unknown object never blocks a plan.
    A validated mission-package preset may authorize its own non-placeholder
    query for the same skill; free-text and model-authored plans still require
    task grounding.
    ``patrol``/``monitor`` must carry a query even when the task names no
    object, so for them the same finding is only a warning when the task does
    ask to patrol/watch (and blocks when the whole step is invented).
``ungrounded_place``
    A goto/land/deliver ``target_ref`` must be a place the task names: the
    ref's name or a label word (English) or a two-character piece / the head
    character of the Chinese label appears in the text, outside a clause that
    only excludes places ("不是水塔", "do not substitute the bridge").  This
    catches an unknown place (Raven Pier) mapped onto another known ref.

The checks only use the task text, never the model; ``repair_plan_intent``
is the deterministic last-attempt repair (land -> return_home, drop the step
with the invented object). An ungrounded place is never repaired.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- text
_CJK = re.compile(r"[㐀-鿿]")
# Clause boundaries: Chinese and English punctuation (a decimal point is not one).
_CLAUSE = re.compile(r"[，。；！？、,;!?\n]|(?<!\d)\.(?!\d)|\bthen\b|\bbut\b")


def _clauses(text: str) -> list[str]:
    return [part for part in _CLAUSE.split(text) if part and part.strip()]


# --------------------------------------------------------------------------- return / land intent
# Chinese phrases that ask the aircraft to come back to its take-off point.
# 起降坪 is a named landing pad (a place), deliberately not a home word.
RETURN_ZH: tuple[str, ...] = (
    r"返航",
    r"回航",
    r"归航",
    r"返场",
    r"撤回",
    r"回家",
    r"(?<!找)(?<!拿)(?<!取)回来",
    r"(?:返回|回到|飞回|退回|回)[^，。；！？、,;!?]{0,6}?(?:起飞|起点|出发点|出发地|放飞|home|HOME|Home|基地)",
)
# English phrases; ``back`` must not continue to a named non-home place.
_HOME_WORDS = (
    r"(?:home|launch|base|take-?off|takeoff|start(?:ing)?\s+point|the\s+start|"
    r"where\s+(?:it|you|we)\s+(?:took|take)\s+off|its\s+(?:launch|take-?off)|"
    r"the\s+launch|the\s+take-?off|the\s+home)"
)
RETURN_EN: tuple[str, ...] = (
    rf"\breturn(?:s|ed|ing)?\s+(?:back\s+)?(?:to\s+)?(?:the\s+)?{_HOME_WORDS}",
    r"\breturn(?:s|ing)?\s*(?:back\s*)?$",
    rf"\b(?:come|comes|coming|head|heads|heading|fly|flies|flying|go|goes|going|get|gets|getting|bring|brings|bringing|send|take|taking|make\s+your\s+way)\b(?:\s+[\w-]+){{0,3}}?\s+back\b(?!\s+(?:to|at|toward|towards)\s+(?!{_HOME_WORDS}))",
    r"\b(?:come|comes|coming|head|heading|fly|flying|go|going|get|getting|bring|bringing|send|take|taking)\b(?:\s+[\w-]+){0,3}?\s+home\b",
    rf"\bback\s+(?:to|at)\s+{_HOME_WORDS}",
    r"\b(?:rtl|rth|return-to-home|return-to-launch)\b",
)
# Explicit landing requests (a landing pad named as a noun is not one).
LAND_ZH: tuple[str, ...] = (
    r"降落",
    r"着陆",
    r"着地",
    r"触地",
    r"落地",
    r"落下",
    r"落(?:在|到|于|回)",
    r"落(?:起降坪|坪)",
    r"(?:就地|原地|收工|收尾|然后|最后|再)落",
)
LAND_EN: tuple[str, ...] = (
    r"\bland(?:s|ed)?\b(?!\s*(?:mark|scape))",
    r"\blanding\b(?!\s+(?:pad|zone|site|area|point|spot|gear|light))",
    r"\btouch(?:es|ed|ing)?\s+down\b",
    r"\b(?:set|sets|setting|put|puts|putting)\b(?:\s+[\w-]+){0,3}?\s+down\b",
)
NEGATION_ZH = re.compile(r"(?:不|别|勿|无需|无须|取消|禁止|免)[^，。；！？、,;!?]{0,4}$")
NEGATION_EN = re.compile(
    r"\b(?:not|don't|dont|do\s+not|never|no\s+need\s+to|without|instead\s+of|rather\s+than|skip|cancel)\b"
    r"(?:\s+[\w'-]+){0,3}\s*$",
    re.I,
)

_RETURN = [re.compile(p) for p in RETURN_ZH] + [re.compile(p, re.I) for p in RETURN_EN]
_LAND = [re.compile(p) for p in LAND_ZH] + [re.compile(p, re.I) for p in LAND_EN]


def _affirmed(patterns: Sequence[re.Pattern[str]], text: str) -> list[str]:
    """Matches that are not negated within their own clause."""
    hits: list[str] = []
    for clause in _clauses(text):
        for pattern in patterns:
            for match in pattern.finditer(clause.strip()):
                before = clause.strip()[: match.start()]
                if NEGATION_ZH.search(before) or NEGATION_EN.search(before):
                    continue
                hits.append(match.group(0))
    return hits


def return_home_intent(task_text: str) -> list[str]:
    """Phrases in which the task asks to come home (empty when none or negated)."""
    return _affirmed(_RETURN, task_text)


def landing_intent(task_text: str) -> list[str]:
    """Phrases in which the task explicitly asks to land (empty when none)."""
    return _affirmed(_LAND, task_text)


# --------------------------------------------------------------------------- query grounding
COLOURS = frozenset(
    "red blue white black orange yellow green purple silver grey gray brown pink "
    "golden gold beige dark light striped".split()
)
_STOPWORDS = frozenset("a an the of in on at with and or to wearing for by near".split())
# Placeholder "objects" a planner writes when the task names none.
PLACEHOLDER_QUERIES = frozenset(
    {
        "none",
        "nothing",
        "object",
        "objects",
        "target",
        "targets",
        "thing",
        "things",
        "unknown",
        "anything",
        "something",
        "null",
        "n/a",
        "na",
        "item",
        "items",
    }
)
_IRREGULAR = {
    "people": "person",
    "persons": "person",
    "men": "man",
    "women": "woman",
    "children": "child",
    "geese": "goose",
    "mice": "mouse",
    "feet": "foot",
    "teeth": "tooth",
    "sheep": "sheep",
    "deer": "deer",
    "fish": "fish",
    "bus": "bus",
    "buses": "bus",
    "glass": "glass",
    "boss": "boss",
}
# Object word -> English words that also name it in a task.
EN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "person": (
        "people",
        "pedestrian",
        "human",
        "someone",
        "somebody",
        "man",
        "woman",
        "individual",
        "worker",
        "runner",
        "cyclist",
        "child",
        "intruder",
        "walker",
        "visitor",
        "hiker",
        "rider",
        "headcount",
    ),
    "pedestrian": ("person", "people", "walker"),
    "car": ("vehicle", "automobile", "sedan", "hatchback", "taxi", "cab"),
    "vehicle": ("car", "truck", "van", "bus", "suv"),
    "bicycle": ("bike", "cycle", "cyclist"),
    "bike": ("bicycle", "cycle", "motorbike", "cyclist"),
    "motorcycle": ("motorbike", "bike"),
    "motorbike": ("motorcycle",),
    "truck": ("lorry", "pickup"),
    "pickup": ("truck",),
    "van": ("minibus", "minivan"),
    "minibus": ("van", "bus"),
    "dog": ("puppy", "canine", "hound"),
    "cone": ("pylon",),
    "dumpster": ("bin", "skip"),
    "bin": ("dumpster", "trash", "garbage"),
    "mailbox": ("postbox",),
    "postbox": ("mailbox",),
    "boat": ("ship", "vessel"),
    "bird": ("gull", "crow", "pigeon", "seagull"),
}
# Object word -> Chinese names (substring match in the task text).
ZH_NAMES: dict[str, tuple[str, ...]] = {
    "person": ("人",),
    "pedestrian": ("行人", "人"),
    "people": ("人",),
    "worker": ("工人", "人员", "人"),
    "man": ("人", "男"),
    "woman": ("人", "女"),
    "child": ("孩", "儿童"),
    "runner": ("跑", "人"),
    "cyclist": ("骑车", "骑行", "人"),
    "car": ("车",),
    "sedan": ("轿车",),
    "vehicle": ("车",),
    "taxi": ("出租车", "的士"),
    "suv": ("SUV", "suv", "越野车"),
    "truck": ("卡车", "货车", "皮卡", "大车", "车"),
    "pickup": ("皮卡",),
    "van": ("面包车", "厢式车", "货车", "车"),
    "bus": ("公交", "大巴", "巴士", "客车", "校车"),
    "minibus": ("中巴", "小巴", "面包车"),
    "ambulance": ("救护车",),
    "fire": ("消防", "火"),
    "motorcycle": ("摩托",),
    "bicycle": ("自行车", "单车", "电瓶车", "电动车", "脚踏车", "骑车"),
    "bike": ("自行车", "单车", "电瓶车", "电动车", "摩托", "骑车"),
    "electric": ("电瓶", "电动"),
    "scooter": ("滑板车", "踏板车", "电瓶车", "电动车"),
    "tractor": ("拖拉机",),
    "forklift": ("叉车",),
    "excavator": ("挖掘机", "挖机"),
    "boat": ("船",),
    "dog": ("狗", "犬"),
    "cat": ("猫",),
    "bird": ("鸟",),
    "cone": ("锥",),
    "traffic": ("交通",),
    "barrel": ("桶",),
    "bucket": ("桶",),
    "drum": ("桶",),
    "barrier": ("墩", "护栏", "隔离"),
    "hydrant": ("消防栓",),
    "dumpster": ("垃圾",),
    "bin": ("垃圾", "桶"),
    "trash": ("垃圾",),
    "garbage": ("垃圾",),
    "rubbish": ("垃圾",),
    "can": ("桶", "罐"),
    "mailbox": ("邮箱", "信箱"),
    "postbox": ("邮筒", "邮箱"),
    "box": ("箱", "盒"),
    "cardboard": ("纸箱", "纸板"),
    "crate": ("箱",),
    "backpack": ("背包", "书包", "包"),
    "bag": ("包", "袋"),
    "suitcase": ("行李箱", "箱"),
    "tent": ("帐篷",),
    "ladder": ("梯",),
    "pallet": ("托盘", "栈板"),
    "antenna": ("天线",),
    "pipe": ("管",),
    "sign": ("标志", "牌"),
    "stop": ("停止", "停车标志"),
    "solar": ("太阳能",),
    "panel": ("板",),
}


def _stem(word: str) -> str:
    word = word.lower()
    if word in _IRREGULAR:
        return _IRREGULAR[word]
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z][a-z0-9'-]*", text.lower())


def _object_words(query: str) -> list[str]:
    return [
        _stem(w) for w in _words(query) if w not in _STOPWORDS and w not in COLOURS and len(w) > 1
    ]


_QUERY_NEGATION_EN = re.compile(
    r"\b(?:do\s+not|don't|dont|never|not\s+(?:a|an|the|invent))\b"
    r"(?:\s+[\w'-]+){0,3}\s*$",
    re.I,
)
_QUERY_NEGATION_ZH = re.compile(r"(?:不要|别|勿|禁止|不是|无需|无须|不准|拒绝)")
# A Chinese negation only scopes over a short verb phrase ("不要凭空检查救护车");
# one earlier in a long clause ("不要停留直接找红车") must not negate the target.
_ZH_NEGATION_WINDOW = 8


def _prefix_negated(clause: str, position: int) -> bool:
    """Whether an explicit negation occurs shortly before this particular mention."""
    prefix = clause[:position]
    return bool(
        _QUERY_NEGATION_EN.search(prefix)
        or _QUERY_NEGATION_ZH.search(prefix[-_ZH_NEGATION_WINDOW:])
    )


def _query_phrase_negation(query: str, task_text: str) -> tuple[bool, bool]:
    """Return (affirmed occurrence, negated occurrence) for the query phrase."""
    phrase = str(query or "").strip().lower()
    if not phrase:
        return False, False
    affirmed = negated = False
    for clause in _clauses(task_text):
        position = clause.lower().find(phrase)
        if position >= 0:
            if _prefix_negated(clause, position):
                negated = True
            else:
                affirmed = True
    return affirmed, negated


def _term_grounded(term: str, task_text: str, *, chinese_names: Sequence[str] = ()) -> bool:
    """Whether a term has an occurrence outside an explicit negation clause."""
    for clause in _clauses(task_text):
        lowered = clause.lower()
        target_stem = _stem(term)
        for match in re.finditer(r"[a-z][a-z0-9'-]*", lowered):
            if _stem(match.group(0)) == target_stem and not _prefix_negated(clause, match.start()):
                return True
        for name in chinese_names:
            position = clause.find(name)
            if position >= 0 and not _prefix_negated(clause, position):
                return True
    return False


_COLOUR_ZH: dict[str, tuple[str, ...]] = {
    "red": ("红", "赤"), "blue": ("蓝",), "white": ("白",),
    "black": ("黑",), "orange": ("橙", "桔"), "yellow": ("黄",),
    "green": ("绿",), "purple": ("紫",), "brown": ("棕", "褐"),
}


def _negated_coloured_query(query: str, task_text: str) -> bool:
    words = _words(query)
    colours = [word for word in words if word in _COLOUR_ZH]
    objects = [word for word in _object_words(query)]
    if not colours or not objects:
        return False
    for clause in _clauses(task_text):
        for colour in colours:
            colour_position = next(
                (clause.find(name) for name in _COLOUR_ZH[colour] if name in clause), -1
            )
            if colour_position < 0:
                continue
            for obj in objects:
                names = ZH_NAMES.get(obj, ())
                position = next(
                    (
                        clause.find(name, colour_position + 1)
                        for name in names
                        if clause.find(name, colour_position + 1) >= 0
                    ),
                    -1,
                )
                if position >= 0 and _prefix_negated(clause, position):
                    return True
    return False


def query_grounding(query: str, task_text: str) -> str:
    """``grounded``, ``ungrounded`` or ``undecided`` for a plan query against the task."""
    query = str(query or "").strip()
    lowered = query.lower()
    if not query:
        return "ungrounded"
    if lowered in PLACEHOLDER_QUERIES:
        return "ungrounded"
    phrase_affirmed, phrase_negated = _query_phrase_negation(query, task_text)
    if phrase_negated and not phrase_affirmed:
        return "ungrounded"
    if _negated_coloured_query(query, task_text):
        return "ungrounded"
    if lowered in task_text.lower():
        return "grounded"
    words = _object_words(query)
    if not words:
        return "undecided"
    for word in words:
        synonyms = (word, *EN_SYNONYMS.get(word, ()))
        if any(
            _term_grounded(_stem(candidate), task_text)
            for candidate in synonyms
        ):
            return "grounded"
    known_zh = False
    for word in words:
        names = ZH_NAMES.get(word)
        if names:
            known_zh = True
            if _term_grounded(word, task_text, chinese_names=names):
                return "grounded"
    if not _CJK.search(task_text):
        # An English task is judged on its own words (with the synonym table).
        return "ungrounded"
    return "ungrounded" if known_zh else "undecided"


# --------------------------------------------------------------------------- watch request
# Words with which a task asks to patrol or watch.  patrol/monitor must carry a
# query, so when the task asks for one but names nothing to look for, the
# planner's placeholder query is tolerated (warning); without such a request
# the whole step is invented and the query finding blocks.
WATCH_ZH = (
    r"巡|监视|监控|观察|盯|守着|看守|留意|注意|留神|警戒|看有没有|看看有没有|"
    r"溜一圈|兜.{0,2}圈|走.{0,2}圈|跑.{0,2}趟"
)
WATCH_EN = (
    r"\b(?:patrol\w*|monitor\w*|watch\w*|observ\w*|guard\w*|surveil\w*|look\s*out|keep\s+an\s+eye|"
    r"circuits?|loops?|rounds?|passes|laps?)\b"
)
_WATCH = (re.compile(WATCH_ZH), re.compile(WATCH_EN, re.I))


def watch_request(task_text: str) -> bool:
    return any(pattern.search(task_text) for pattern in _WATCH)


# --------------------------------------------------------------------------- place grounding
# A clause that names places only to exclude them ("不是水塔", "do not substitute
# the bridge") does not ground them.
PLACE_EXCLUSION = re.compile(
    r"不是|顶替|代替|替代|换成|\bnot\b|n't\b|\bnever\b|instead\s+of|substitut\w*|rather\s+than",
    re.I,
)
_PLACE_STOPWORDS = frozenset({"the", "a", "an", "of", "route", "area", "zone", "place", "point"})
# Built-in references that are not named places.
_BUILTIN_PREFIXES = frozenset({"finding", "track"})

# Common operator vocabulary.  These are deliberately attached to a reference
# whose slug or existing label identifies the same concept; they are never a
# global synonym list.  A term owned by more than one reference is suppressed
# below so it cannot make two places interchangeable.
_PLACE_ALIAS_GROUPS: dict[str, tuple[str, ...]] = {
    "gate": ("门", "大门", "入口", "门口", "entrance", "gate"),
    "entrance": ("门", "大门", "入口", "门口", "entrance", "gate"),
    "pier": ("码头", "pier", "dock"),
    "dock": ("码头", "pier", "dock"),
    "pad": ("停机坪", "起降坪", "landing pad", "pad"),
    "landing": ("停机坪", "起降坪", "landing pad", "pad"),
    "warehouse": ("仓库", "库房", "warehouse", "shed"),
    "shed": ("仓库", "库房", "warehouse", "shed"),
}


def parse_ref_labels(refs: Iterable[str] | Mapping[str, Any] | None) -> dict[str, list[str]]:
    """Parse labelled refs and the legacy slash-separated label convention.

    Mission packages keep ``ref_labels`` values as strings for schema and GCS
    compatibility; ``/`` separates aliases (for example ``"入口 / entrance"``).
    A mapping is also accepted directly for callers that already have the
    package field parsed.
    """
    labels: dict[str, list[str]] = {}
    if isinstance(refs, Mapping):
        for name, value in refs.items():
            if isinstance(value, str):
                labels[str(name)] = [part.strip() for part in value.split("/") if part.strip()]
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                labels[str(name)] = [str(part).strip() for part in value if str(part).strip()]
        return labels
    for ref in refs or ():
        name, _, rest = str(ref).partition(" ")
        rest = rest.strip()
        if rest.startswith("(") and rest.endswith(")"):
            rest = rest[1:-1]
        labels[name] = [part.strip() for part in rest.split("/") if part.strip()]
    return labels


def _builtin_place_aliases(ref: str, labels: Sequence[str]) -> tuple[str, ...]:
    """Return semantic aliases implied by a ref slug or its explicit labels."""
    haystack = " ".join((ref, *labels)).lower()
    aliases: list[str] = []
    for key, values in _PLACE_ALIAS_GROUPS.items():
        if key in ref.partition(":")[2].lower() or key in haystack:
            aliases.extend(values)
    return tuple(dict.fromkeys(aliases))


def _alias_conflicts(refs: Mapping[str, Sequence[str]]) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for ref, labels in refs.items():
        for alias in _builtin_place_aliases(ref, labels):
            key = _stem(alias) if re.fullmatch(r"[a-zA-Z0-9 -]+", alias) else alias.strip().lower()
            owners.setdefault(key, set()).add(ref)
    return {term: refs for term, refs in owners.items() if len(refs) > 1}


def _place_terms(ref: str, labels: Sequence[str]) -> tuple[set[str], set[str]]:
    """(English words, Chinese substrings) that name ``ref`` in a task."""
    english = {
        _stem(w)
        for w in re.split(r"[^a-z0-9]+", ref.partition(":")[2].lower())
        if len(w) > 2 and w not in _PLACE_STOPWORDS
    }
    chinese: set[str] = set()
    for label in labels:
        for run in re.findall(r"[㐀-鿿]+", label):
            # Chinese place nouns are head-final: 门 in 大门 also names 门口.
            chinese.add(run[-1])
            chinese.update(run[i : i + 2] for i in range(len(run) - 1))
        english.update(_stem(w) for w in _words(label) if len(w) > 2 and w not in _PLACE_STOPWORDS)
    return english, chinese


def place_grounded(
    ref: str,
    labels: Sequence[str],
    task_text: str,
    *,
    aliases: Sequence[str] = (),
) -> bool:
    """True when the task names ``ref`` (by its name or a label) outside an exclusion."""
    clauses = [c for c in _clauses(task_text) if not PLACE_EXCLUSION.search(c)]
    body = " ".join(clauses)
    english, chinese = _place_terms(ref, [*labels, *aliases])
    stems = {_stem(w) for w in _words(body)}
    return bool(english & stems) or any(term in body for term in chinese)


# --------------------------------------------------------------------------- plan checks
# ``record`` of whatever is in view (planning data convention, 当前画面).
VIEW_QUERIES = frozenset({"current view", "current scene", "current frame", "view", "scene"})
# Skills whose query names the object the step is about; an invented one is an error.
OBJECT_SKILLS = frozenset({"area_search", "count", "inspect", "record", "follow"})
# Skills that must carry a query even when the task names nothing to watch for.
WATCH_SKILLS = frozenset({"patrol", "monitor"})
# Skills that fly to (or land at) a named place.
PLACE_SKILLS = frozenset({"goto", "land", "deliver"})


@dataclass(frozen=True)
class IntentFinding:
    code: str
    step_id: str
    message: str
    blocking: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "step_id": self.step_id,
            "message": self.message,
            "blocking": self.blocking,
        }


def _get(step: Any, key: str) -> Any:
    if isinstance(step, Mapping):
        return step.get(key)
    return getattr(step, key, None)


def _steps(plan: Any) -> list[Any]:
    steps = plan.get("steps") if isinstance(plan, Mapping) else getattr(plan, "steps", ())
    return [s for s in steps or () if isinstance(s, Mapping) or hasattr(s, "skill")]


def _declared_queries(plan: Any) -> set[tuple[str, str]]:
    """Return non-placeholder queries declared by a trusted preset plan."""
    declared: set[tuple[str, str]] = set()
    for step in reachable_steps(plan):
        skill = str(_get(step, "skill") or "")
        query = str((_get(step, "params") or {}).get("query") or "").strip().lower()
        if skill and query and query not in PLACEHOLDER_QUERIES:
            declared.add((skill, query))
    return declared


def reachable_steps(plan: Any) -> list[Any]:
    """Steps reachable from the first step over on_success/on_fail, in plan order."""
    steps = _steps(plan)
    if not steps:
        return []
    by_id = {str(_get(s, "id")): s for s in steps}
    seen: set[str] = set()
    stack = [str(_get(steps[0], "id"))]
    while stack:
        step_id = stack.pop()
        if step_id in seen or step_id not in by_id:
            continue
        seen.add(step_id)
        for key in ("on_success", "on_fail"):
            nxt = _get(by_id[step_id], key)
            if nxt is not None:
                stack.append(str(nxt))
    return [s for s in steps if str(_get(s, "id")) in seen]


def check_plan_intent(
    plan: Any,
    task_text: str,
    refs: Iterable[str] | Mapping[str, Any] | None = None,
    *,
    declared_plan: Any | None = None,
) -> list[IntentFinding]:
    """Findings where ``plan`` (MissionPlan or plan document) contradicts the task text.

    ``refs`` are the known places offered to the planner (optionally labelled,
    ``"poi:gate (大门 / the gate)"``); without them the place rule is skipped.
    ``declared_plan`` is an optional trusted mission-package preset. Its exact
    non-placeholder query is accepted for the same skill because the package
    declares the object independently of a generic regression sentence.
    """
    findings: list[IntentFinding] = []
    reachable = reachable_steps(plan)
    declared_queries = _declared_queries(declared_plan)
    labels = parse_ref_labels(refs)
    if labels:
        conflicts = _alias_conflicts(labels)
        if conflicts:
            for term, owners in sorted(conflicts.items()):
                findings.append(
                    IntentFinding(
                        "place_alias_conflict",
                        "",
                        f"place alias '{term}' is shared by {', '.join(sorted(owners))}; "
                        "alias disabled",
                        blocking=False,
                    )
                )
        for step in reachable:
            if _get(step, "skill") not in PLACE_SKILLS:
                continue
            ref = (_get(step, "params") or {}).get("target_ref")
            if (
                not isinstance(ref, str)
                or ref in {"home", "zone:all"}
                or ref.partition(":")[0] in _BUILTIN_PREFIXES
                or place_grounded(
                    ref,
                    labels.get(ref, ()),
                    task_text,
                    aliases=[
                        alias
                        for alias in _builtin_place_aliases(ref, labels.get(ref, ()))
                        if (
                            _stem(alias) if re.fullmatch(r"[a-zA-Z0-9 -]+", alias)
                            else alias.strip().lower()
                        )
                        not in conflicts
                    ],
                )
            ):
                continue
            findings.append(
                IntentFinding(
                    "ungrounded_place",
                    str(_get(step, "id")),
                    f"task does not name {ref}: a place not in the known list cannot be "
                    "replaced by another",
                )
            )
    if return_home_intent(task_text) and not landing_intent(task_text):
        for step in reachable:
            params = _get(step, "params") or {}
            if _get(step, "skill") == "land" and params.get("target_ref") != "home":
                findings.append(
                    IntentFinding(
                        "return_intent_land",
                        str(_get(step, "id")),
                        "task asks to return home: end with return_home, not land",
                    )
                )
    for step in reachable:
        skill = _get(step, "skill")
        if skill not in OBJECT_SKILLS and skill not in WATCH_SKILLS:
            continue
        query = (_get(step, "params") or {}).get("query")
        if skill == "record" and str(query).strip().lower() in VIEW_QUERIES:
            continue
        if query is None:
            continue
        query_text = str(query).strip().lower()
        if query_grounding(query_text, task_text) != "ungrounded":
            continue
        if (str(skill), query_text) in declared_queries:
            continue
        findings.append(
            IntentFinding(
                "ungrounded_query",
                str(_get(step, "id")),
                f"query '{query}' is not in the task: drop step {_get(step, 'id')} "
                "or use an object the task names",
                blocking=skill in OBJECT_SKILLS or not watch_request(task_text),
            )
        )
    return findings


def intent_error(findings: Iterable[IntentFinding]) -> str:
    """Short rejection text for the planner's retry prompt (blocking findings only)."""
    messages = []
    for finding in findings:
        if finding.blocking and finding.message not in messages:
            messages.append(finding.message)
    return "task intent: " + "; ".join(messages)


def repair_plan_intent(
    document: Mapping[str, Any], findings: Iterable[IntentFinding]
) -> tuple[dict[str, Any], list[str]]:
    """Deterministic repair for blocking findings: land -> return_home, drop invented steps.

    A dropped step's incoming edges continue to where it would have gone on
    success. The caller validates (and re-checks) the result.
    """
    findings = [f for f in findings if f.blocking]
    steps = [dict(step) for step in document.get("steps", []) if isinstance(step, Mapping)]
    notes: list[str] = []
    to_home = {f.step_id for f in findings if f.code == "return_intent_land"}
    to_drop = {f.step_id for f in findings if f.code == "ungrounded_query"}
    for step in steps:
        if str(step.get("id")) in to_home:
            step["skill"] = "return_home"
            step["params"] = {}
            notes.append(f"{step.get('id')}: land -> return_home (task asks to return home)")
    by_id = {str(step.get("id")): step for step in steps}

    def forward(ref: Any, key: str) -> Any:
        seen: set[str] = set()
        while ref is not None and str(ref) in to_drop and str(ref) not in seen:
            seen.add(str(ref))
            dropped = by_id[str(ref)]
            ref = dropped.get("on_success") if key == "on_success" else dropped.get("on_fail")
            if ref is None:
                ref = dropped.get("on_success")
        return ref

    kept = []
    for step in steps:
        if str(step.get("id")) in to_drop:
            notes.append(
                f"{step.get('id')}: dropped {step.get('skill')} "
                f"(query '{(step.get('params') or {}).get('query')}' not in task)"
            )
            continue
        for key in ("on_success", "on_fail"):
            step[key] = forward(step.get(key), key)
        kept.append(step)
    return {**document, "steps": kept}, notes


__all__ = [
    "COLOURS",
    "EN_SYNONYMS",
    "IntentFinding",
    "LAND_EN",
    "LAND_ZH",
    "OBJECT_SKILLS",
    "PLACEHOLDER_QUERIES",
    "RETURN_EN",
    "RETURN_ZH",
    "VIEW_QUERIES",
    "WATCH_SKILLS",
    "ZH_NAMES",
    "check_plan_intent",
    "intent_error",
    "parse_ref_labels",
    "place_grounded",
    "watch_request",
    "landing_intent",
    "query_grounding",
    "reachable_steps",
    "repair_plan_intent",
    "return_home_intent",
]
