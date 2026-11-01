"""Conservative deterministic checks before language-model mission planning."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from .capability_contract import normalize_capabilities
from .mission_plan import PlanDecision


_PAYLOAD_ACTION = re.compile(
    r"\b(?:deliver|drop|deploy|release|dispatch|send|transport)\b|投放|投递|投送|递送"
    r"|(?:把|将|给|向).{0,24}(?:送到|送去|派到|投到|投放到|投递到|投放|投递|投)"
    , re.IGNORECASE
)
_CARRY_TO = re.compile(r"\bcarry\b.{0,64}\bto\b", re.I)
_PAYLOAD_OBJECT = re.compile(
    r"\b(?:a|an|the|this|that|my|our)?\s*(?:parcel|package|payload|sample|battery|"
    r"battery pack|supply|supplies|medicine|medical kit|tool|crate|box|food|water|radio|cargo)\b"
    r"|(?:包裹|包裹箱|样品|电池|电池包|物资|药品|药箱|木箱|医疗包|工具|箱子|食物|水|货物|载荷)"
    , re.IGNORECASE
)
_HOVER = re.compile(r"\bhover(?:ing)?\b|\bstay stationary\b|\bhold position\b|\b(?:stay|remain|keep)\b.{0,24}\b(?:stationary|in one place|in place)\b|\bstationary\b|悬停|定点停留|保持(?:在)?原位|保持(?:在)?一个位置|停住观察", re.I)
_TARGET_LAND = re.compile(
    r"\b(?:land|touch down|set down|put down)\b.{0,48}\b(?:at|on|onto|on the|at the)\b"
    r"|\bput\b.{0,36}\bdown\b.{0,36}\b(?:at|on|onto)\b"
    r"|\bset\b.{0,36}\bdown\b.{0,36}\b(?:there|at|on|onto)\b"
    r"|\b(?:at|on|onto)\b.{0,48}\b(?:land|touch down)\b"
    r"|定点降落|目标点(?:着陆|降落|触地)|(?:降落|着陆|落地|触地).{0,20}(?:到|在|于).{0,18}(?:点|坪|屋顶|大门|水塔|桥|码头|农田|仓库)"
    r"|(?:在|到).{0,24}(?:点|坪|屋顶|大门|水塔|桥|码头|农田|仓库|那里|该处).{0,22}(?:降落|着陆|落地|触地)"
    r"|(?:抵达|到达).{0,18}(?:后|之后).{0,8}(?:原地)?(?:降落|着陆|落地|触地)", re.I
)
_UNKNOWN_CUE = re.compile(
    r"non[- ]?existent|not on (?:the )?map|unlisted|not registered|unknown (?:place|location)"
    r"|absent from (?:the )?(?:known places|reference list)|not in (?:the )?(?:known places|reference list)"
    r"|not one of (?:the )?(?:supplied|listed|known) references"
    r"|地图上(?:没有|不存在)|地图外|地图未标注|地图没有标注|地图上没标出|已知地点里没有|没有对应的?(?:poi)?引用|不在(?:任何)?(?:已知地点|引用列表)|不是列表里的|不是给出的任何引用|未登记|未收录|不存在的|没有这个地点|未知地点", re.I
)
_MOVE_TO = re.compile(
    r"\b(?:go|fly|head|proceed|travel|navigate|land)\s+to\s+([^,;.!?。！？]+)"
    r"|\bvisit\s+([^,;.!?。！？]+)"
    r"|\b(?:take|send|dispatch)\s+(?:the aircraft|the drone|it)\s+to\s+([^,;.!?。！？]+)"
    r"|\b(?:land|touch down)\s+(?:at|on|onto)\s+([^,;.!?。！？]+)"
    r"|(?:去|到|飞到|飞往|前往|降落在|着陆在|落在)([^，。；！？,;!?]+)", re.I
)
_DIRECT_UNKNOWN = re.compile(
    r"\b(?:reach|visit)\s+(?:the\s+)?([A-Z][\w'-]*(?:\s+[A-Z][\w'-]*)*)"
    r"|\bthe requested\s+([A-Z][\w'-]*(?:\s+[A-Z][\w'-]*)*)"
    r"|(?:目标是|任务要求(?:前往|去|到)|把无人机派到|请(?:把无人机)?(?:派到|去|到))([^，。；！？,;!?]+)", re.I
)
_ZH_PLACE_SUFFIX = re.compile(r"(?:码头|渡口|气象站|泵房|附楼|观景台|营地|瞭望台|仓库|水塔|起降坪|停车场)$")
_NEGATION = re.compile(r"\b(?:do not|don't|never|without|not)\b|不要|别|禁止|无需|不用|不需要", re.I)


def _affirmative(text: str, match: re.Match[str]) -> bool:
    return not _NEGATION.search(text[max(0, match.start() - 36):match.start()])


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[^\w\u3400-\u9fff]+", " ", value)
    return " ".join(value.split())


def _known_names(refs: Mapping[str, Any] | Sequence[str] | None) -> set[str]:
    names: set[str] = set()
    if isinstance(refs, Mapping):
        values = refs.keys()
    else:
        values = refs or ()
    for raw in values:
        value = str(raw)
        names.add(_normalized(value))
        names.add(_normalized(value.split("(", 1)[0]))
        for alias_group in re.findall(r"\(([^)]*)\)", value):
            names.update(_normalized(alias) for alias in re.split(r"[/|]", alias_group))
    return {name for name in names if name}


def _decision(code: str, reason: str, *, missing: tuple[str, ...] = (),
              unresolved: tuple[str, ...] = ()) -> PlanDecision:
    return PlanDecision(1, "reject", code, reason, missing, unresolved, None)


def _matches_known(normalized: str, names: set[str]) -> bool:
    if normalized in names:
        return True
    if re.search(r"[\u3400-\u9fff]", normalized):
        return any(name and re.search(r"[\u3400-\u9fff]", name) and name in normalized for name in names)
    return False


def _unknown_place(text: str, refs: Mapping[str, Any] | Sequence[str] | None) -> PlanDecision | None:
    names = _known_names(refs)
    cue = _UNKNOWN_CUE.search(text)
    for match in _MOVE_TO.finditer(text):
        candidate = next((part for part in match.groups() if part), "")
        candidate = re.sub(r"\b(?:the|a|an|this|that)\b", " ", candidate, flags=re.I)
        candidate = re.split(
            r"\b(?:and|then|before|after|while|but|do not|return|come back|then return)\b"
            r"|然后|再|并|随后|之后|接着|同时|再回|返航|返回", candidate, maxsplit=1,
            flags=re.I,
        )[0]
        candidate = re.sub(r"\b(?:there|here|home|base|takeoff point|launch point)\b", " ", candidate, flags=re.I)
        candidate = candidate.strip(" \t\n\r'\"“”‘’()[]")
        normalized = _normalized(candidate)
        if not normalized or normalized in names or (not cue and _matches_known(normalized, names)):
            continue
        if cue:
            return _decision("unknown_place", "Requested location is explicitly described as absent from the known map.", unresolved=(candidate[:96],))
        # Capitalized multiword place names are a narrow, language-neutral signal.
        if re.search(r"\b[A-Z][\w'-]*(?:\s+[A-Z][\w'-]*)+\b", candidate):
            return _decision("unknown_place", "Requested named location is not present in the mission reference list.", unresolved=(candidate[:96],))
        if _ZH_PLACE_SUFFIX.search(candidate) and re.search(r"[\u3400-\u9fff]", candidate):
            return _decision("unknown_place", "Requested named location is not present in the mission reference list.", unresolved=(candidate[:96],))
    if cue:
        for match in _DIRECT_UNKNOWN.finditer(text):
            candidate = next((part for part in match.groups() if part), "")
            candidate = re.split(r"\b(?:which|that|and|then|is|not)\b|，|。|；|,|;", candidate, maxsplit=1, flags=re.I)[0]
            candidate = candidate.strip(" \t\n\r'\"“”‘’()[]")
            normalized = _normalized(candidate)
            if normalized and not _matches_known(normalized, names):
                return _decision("unknown_place", "Requested location is explicitly described as absent from the known map.", unresolved=(candidate[:96],))
    return None


def preflight_plan_task(
    task_text: str,
    refs: Mapping[str, Any] | Sequence[str] | None = None,
    capabilities: Any = (),
    airframe: Any = None,
) -> PlanDecision | None:
    """Return a structured rejection only when text and package facts agree."""
    text = unicodedata.normalize("NFKC", str(task_text or ""))
    available = normalize_capabilities(capabilities)
    is_fixed_wing = airframe in {"fixed_wing", "airframe:fixed_wing"} or "airframe:fixed_wing" in available

    payload_action = _PAYLOAD_ACTION.search(text)
    carry_to = _CARRY_TO.search(text)
    if ("payload_drop" not in available and
            ((payload_action and _affirmative(text, payload_action)) or
             (carry_to and _PAYLOAD_OBJECT.search(text)))
            and _PAYLOAD_OBJECT.search(text)):
        return _decision(
            "missing_capability",
            "The task explicitly requests payload delivery, but payload_drop is unavailable.",
            missing=("payload_drop",),
        )
    hover = _HOVER.search(text)
    if is_fixed_wing and hover and _affirmative(text, hover):
        return _decision(
            "missing_capability", "The task explicitly requests hovering, which this fixed-wing airframe cannot perform.",
            missing=("hover",),
        )
    target_land = _TARGET_LAND.search(text)
    if is_fixed_wing and target_land and _affirmative(text, target_land):
        return _decision(
            "missing_capability", "The task explicitly requests landing on a target, which this fixed-wing airframe cannot perform.",
            missing=("landing_on_target",),
        )
    return _unknown_place(text, refs)


__all__ = ["preflight_plan_task"]
