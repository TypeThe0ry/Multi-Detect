"""Conservative deterministic checks before language-model mission planning."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from .capability_contract import normalize_capabilities
from .mission_plan import PlanDecision


_PAYLOAD_ACTION = re.compile(
    r"\b(?:deliver|drop|drop off|deploy|release|dispatch|send|transport|put|leave|"
    r"hand over|hand|set down|put down|lower|unload|transfer|"
    r"air[- ]?drop|airdrop)\b|投放|投递|投送|递送|派送|空投|交付|交给|卸下|卸在|留下|放置"
    r"|\bplace\b.{0,48}\b(?:at|on|onto|to)\b"
    r"|(?:把|将|给|向).{0,32}(?:送到|送去|派到|投到|投放到|投递到|交给|交到|放到|放在|放下|"
    r"留在|留下|卸到|卸在|运到|运往|送往|投放|投递|投送|投|空投)"
    , re.IGNORECASE
)
_CARRY_TO = re.compile(r"\b(?:carry|bring|get|take)\b.{0,64}\bto\b", re.I)
_PAYLOAD_OBJECT = re.compile(
    r"\b(?:a|an|the|this|that|my|our|one)?\s*(?:parcel|package|payload|sample|battery|"
    r"battery pack|supply|supplies|medicine|medical kit|emergency kit|tool|tool case|crate|"
    r"box|food|water|radio|beacon|cargo|sensor|equipment|kit)\b"
    r"|(?:包裹|包裹箱|样品|电池|电池包|物资|补给|药品|药箱|医疗包|急救包|工具|工具箱|木箱|箱子|食物|饮用水|水箱|水包|货物|"
    r"载荷|传感器|电台|无线电|标记)"
    , re.IGNORECASE
)
_HOVER = re.compile(
    r"\bhover(?:ing)?\b|\bstay stationary\b|\bhold(?:\s+its)?\s+(?:position|one position)\b|"
    r"\b(?:hold|stay|remain|keep|wait)\b.{0,48}\b"
    r"(?:stationary|in one place|in place|steady|motionless|frozen|suspended)\b|\bstationary\b|"
    r"\b(?:stay|remain|wait)\b.{0,32}\b(?:over|above)\b.{0,24}\b(?:spot|there|it)\b|"
    r"\bwaiting\b.{0,20}\b(?:motionless|stationary|in place)\b|"
    r"\bstation[- ]keep(?:ing)?\b|\bstop midair\b|\bremain directly over\b|悬停|悬住|定点停留|定点守候|"
    r"保持(?:在)?原位|保持(?:在)?一个位置|停住观察|停在空中|原地不动|稳在|保持悬空|暂停在该点|停在.{0,12}上空|"
    r"\bpause\b.{0,24}\b(?:at|over|in)\b",
    re.I,
)
_TARGET_LAND = re.compile(
    r"\b(?:land|touch down|touchdown|set down|put down|bring down)\b.{0,64}\b(?:at|on|onto|on the|at the|inside)\b"
    r"|\bput\b.{0,36}\bdown\b.{0,36}\b(?:at|on|onto)\b"
    r"|\bset\b.{0,36}\bdown\b.{0,36}\b(?:there|at|on|onto)\b"
    r"|\b(?:at|on|onto|inside)\b.{0,64}\b(?:land|touch down|touchdown)\b"
    r"|\b(?:land|touch down|touchdown)\b.{0,48}\b(?:marked|target|designated|specified)\b.{0,24}\b(?:spot|point|area|box|location)\b"
    r"|\b(?:land|touch down|touchdown)\b.{0,48}\b(?:ground|grass|shoreline)\b"
    r"|\bbring\b.{0,40}\bdown\b.{0,24}\b(?:at|on|onto|inside)\b"
    r"|\b(?:touch down|touchdown)\b.{0,24}\b(?:right )?there\b"
    r"|\blanding\b.{0,40}\b(?:in front of|beside|next to|at)\b"
    r"|\b(?:put|set)\s+(?:the\s+)?(?:fixed[- ]wing\s+)?(?:aircraft|plane|drone)\b.{0,24}\b(?:on|onto)\b.{0,24}\b(?:ground|spot|target)\b"
    r"|定点降落|目标点(?:着陆|降落|触地)|(?:降落|着陆|落地|触地|着地|降下|降至|降到|落到).{0,28}(?:到|在|于).{0,24}(?:点|区域|目标|坪|屋顶|大门|水塔|桥|码头|农田|仓库|草地|地面)"
    r"|(?:降落|着陆|落地|触地|着地|降下|降至|降到|落到|落在).{0,36}(?:点|区域|目标|坪|屋顶|大门|水塔|桥|码头|渡口|附楼|农田|仓库|南院|东港|草地|地面|空地|落点|目标框)"
    r"|(?:在|到|于).{0,36}(?:点|区域|目标|坪|屋顶|大门|水塔|桥|码头|渡口|附楼|农田|仓库|南院|东港|那里|该处|草地|地面|空地|落点|目标框).{0,28}(?:降落|着陆|落地|触地|着地|降下|降至|降到|落到|着陆)"
    r"|(?:抵达|到达).{0,24}(?:后|之后).{0,10}(?:原地)?(?:降落|着陆|落地|触地|降下|降至|降到|落到)", re.I
)
_NEGATED_LANDING = re.compile(r"(?:不要|不在|不做|不用|无需|不需要).{0,20}(?:降落|着陆|落地|触地)", re.I)
_UNKNOWN_CUE = re.compile(
    r"non[- ]?existent|not on (?:the )?map|unlisted|not registered|unknown (?:place|location)"
    r"|absent from (?:the )?(?:known places|reference list)|not in (?:the )?(?:known places|reference list)"
    r"|not one of (?:the )?(?:supplied|listed|known) references"
    r"|地图上(?:没有|不存在)|地图外|地图未标注|地图没有标注|地图上没标出|已知地点里没有|没有对应的?(?:poi)?引用|"
    r"引用(?:里|中)?不存在|没有登记|不在(?:任何)?(?:已知地点|引用列表)|不是列表里的|不是给出的任何引用|未登记|未注册|未收录|不存在的|没有这个地点|未知地点", re.I
)
_MOVE_TO = re.compile(
    r"\b(?:go|fly|head|proceed|travel|navigate|land|move|reach)\s+to\s+([^,;.!?。！？]+)"
    r"|\breach\s+(?!home\b)([^,;.!?。！？]+)"
    r"|\bvisit\s+([^,;.!?。！？]+)"
    r"|\b(?:head|move|navigate|proceed|travel|fly|go)\s+(?:toward|for)\s+([^,;.!?。！？]+)"
    r"|\b(?:set|plot)\s+(?:a\s+)?course\s+for\s+([^,;.!?。！？]+)"
    r"|\bmake\s+(?:your|its|the aircraft's)\s+way\s+to\s+([^,;.!?。！？]+)"
    r"|\b(?:take|send|dispatch)\s+(?:the aircraft|the drone|it)\s+to\s+([^,;.!?。！？]+)"
    r"|\b(?:land|touch down)\s+(?:at|on|onto)\s+([^,;.!?。！？]+)"
    r"|(?:去|到|飞到|飞往|前往|往|驶向|赶往|开往|降落在|着陆在|落在)([^，。；！？,;!?]+)", re.I
)
_DIRECT_UNKNOWN = re.compile(
    r"\b(?:reach|visit)\s+(?:the\s+)?([A-Z][\w'-]*(?:\s+[A-Z][\w'-]*)*)"
    r"|\bthe requested\s+([A-Z][\w'-]*(?:\s+[A-Z][\w'-]*)*)"
    r"|(?:目标是|任务要求(?:前往|去|到)|把无人机派到|请(?:把无人机)?(?:派到|去|到))([^，。；！？,;!?]+)", re.I
)
_ZH_PLACE_SUFFIX = re.compile(r"(?:码头|渡口|气象站|泵房|附楼|观景台|营地|瞭望台|仓库|水塔|起降坪|停车场)$")
_NEGATION = re.compile(
    r"\b(?:do not|don't|never|without|not|no request to|no need to|not asked to|not instructed to|"
    r"no\s+(?:station[- ]?keeping|hovering|target landing))\b|"
    r"不要|别|禁止|无需|不用|不需要|不在|不做|不去|不要求|不要真的|未要求",
    re.I,
)
_NON_OPERATIONAL = re.compile(
    r"历史(?:记录|指令)?|旧(?:指令|备注)|只是(?:历史|说明)|仅提到|说明里|作为例子|检查任务是否|"
    r"\b(?:old (?:note|instruction)|archive|archived|checklist mentions?|brief(?:ing)? mentions?|"
    r"as an example|concept|only mentioned|not (?:an?|the) (?:instruction|request))\b",
    re.I,
)
_HOME_LANDING = re.compile(
    r"\b(?:land|touch down|touchdown|set down|put down|bring down)\b.{0,20}\b(?:home|base|launch|runway)\b"
    r"|(?:降落|着陆|落地|触地|着地|降下|降至|降到|落到).{0,10}(?:在|于|到).{0,12}(?:基地|起飞点|起降场)",
    re.I,
)
_PAYLOAD_TARGET = re.compile(
    r"\b(?:to|at|on|onto|there)\b|投到|投放到|投递到|送到|送去|派到|交给|交到|放到|放在|放下|"
    r"留在|卸到|卸在|运到|运往|送往|地面|目标点|指定位置",
    re.I,
)
_AMBIGUOUS_PAYLOAD_ACTION = re.compile(
    r"\b(?:put|leave|hand|set down|put down|lower|unload|transfer|place)\b", re.I
)


def _affirmative(text: str, match: re.Match[str]) -> bool:
    prefix = text[max(0, match.start() - 56):min(len(text), match.start() + 4)]
    return not _NEGATION.search(prefix) and not _NON_OPERATIONAL.search(prefix)


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[^\w\u3400-\u9fff]+", " ", value)
    return " ".join(value.split())


def _known_names(refs: Mapping[str, Any] | Sequence[str] | None) -> set[str]:
    names: set[str] = set()

    def add_label(value: Any) -> None:
        if isinstance(value, str):
            names.add(_normalized(value))
            for alias in re.split(r"[/|;]", value):
                names.add(_normalized(alias))
        elif isinstance(value, Mapping):
            for key in ("name", "label", "labels", "aliases", "alias"):
                if key in value:
                    add_label(value[key])
        elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            for item in value:
                add_label(item)

    if isinstance(refs, Mapping):
        for raw, labels in refs.items():
            value = str(raw)
            names.add(_normalized(value))
            add_label(labels)
    else:
        values = refs or ()
        if isinstance(values, str):
            values = (values,)
        for raw in values:
            value = str(raw)
            base, separator, remainder = value.partition("(")
            names.add(_normalized(value))
            names.add(_normalized(base))
            if separator:
                label_text = remainder.rsplit(")", 1)[0]
                add_label(label_text)
    return {name for name in names if name}


def _decision(code: str, reason: str, *, missing: tuple[str, ...] = (),
              unresolved: tuple[str, ...] = ()) -> PlanDecision:
    return PlanDecision(1, "reject", code, reason, missing, unresolved, None)


def _matches_known(normalized: str, names: set[str]) -> bool:
    if normalized in names:
        return True
    if re.search(r"[\u3400-\u9fff]", normalized):
        if any(name and re.search(r"[\u3400-\u9fff]", name) and name in normalized for name in names):
            return True
        return any(
            name and not re.search(r"[\u3400-\u9fff]", name)
            and re.match(rf"^{re.escape(name)}(?:\s|[\u3400-\u9fff])", normalized)
            for name in names
        )
    # English task wording may continue with an operational clause (for
    # example, "North Annex to check it" or "Cedar Wharf for a look"). Only
    # recognized clause boundaries permit the suffix; "North Annex East" stays
    # a distinct place name.
    return any(
        name and re.match(
            rf"^{re.escape(name)}\s+(?:to|for|and|then|before|after|while|but)\b",
            normalized,
        )
        for name in names
        if not re.search(r"[\u3400-\u9fff]", name)
    )


def _unknown_place(text: str, refs: Mapping[str, Any] | Sequence[str] | None) -> PlanDecision | None:
    names = _known_names(refs)
    cue = _UNKNOWN_CUE.search(text)
    for match in _MOVE_TO.finditer(text):
        if not _affirmative(text, match):
            continue
        candidate = next((part for part in match.groups() if part), "")
        candidate = re.sub(r"\b(?:the|a|an|this|that)\b", " ", candidate, flags=re.I)
        candidate = re.split(
            r"\b(?:and|then|before|after|while|but|do not|return|come back|then return)\b"
            r"|然后|再|并|随后|之后|接着|同时|再回|返航|返回", candidate, maxsplit=1,
            flags=re.I,
        )[0]
        candidate = re.sub(r"(?:取景|查看|检查|拍照|观察|搜索|巡检|核查|inspect|check|photograph|circle)\s*$", "", candidate, flags=re.I)
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
            if not _affirmative(text, match):
                continue
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
    if payload_action and _AMBIGUOUS_PAYLOAD_ACTION.search(payload_action.group(0)):
        payload_window = text[payload_action.start():min(len(text), payload_action.end() + 72)]
        if not _PAYLOAD_TARGET.search(payload_window):
            payload_action = None
    carry_has_object = bool(
        carry_to and any(
            carry_to.start() <= obj.start() < carry_to.end()
            for obj in _PAYLOAD_OBJECT.finditer(text)
        )
    )
    if ("payload_drop" not in available and not _NON_OPERATIONAL.search(text) and
            ((payload_action and _affirmative(text, payload_action)) or
             (carry_has_object and _affirmative(text, carry_to)))
            and _PAYLOAD_OBJECT.search(text)):
        return _decision(
            "missing_capability",
            "The task explicitly requests payload delivery, but payload_drop is unavailable.",
            missing=("payload_drop",),
        )
    hover = _HOVER.search(text)
    hover_negated = bool(re.search(r"\bno\s+station[- ]?keeping\b|\bno\s+hover(?:ing)?\b", text, re.I))
    if (is_fixed_wing and hover and not hover_negated and not _NON_OPERATIONAL.search(text)
            and _affirmative(text, hover)):
        return _decision(
            "missing_capability", "The task explicitly requests hovering, which this fixed-wing airframe cannot perform.",
            missing=("hover",),
        )
    target_land = _TARGET_LAND.search(text)
    if (is_fixed_wing and target_land and not _NON_OPERATIONAL.search(text)
            and _affirmative(text, target_land)
            and not _NEGATED_LANDING.search(text)
            and not _HOME_LANDING.search(text[target_land.start():target_land.start() + 72])):
        return _decision(
            "missing_capability", "The task explicitly requests landing on a target, which this fixed-wing airframe cannot perform.",
            missing=("landing_on_target",),
        )
    return _unknown_place(text, refs)


__all__ = ["preflight_plan_task"]
