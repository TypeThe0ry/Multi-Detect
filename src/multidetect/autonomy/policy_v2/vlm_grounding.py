"""Fail-closed, shadow-only VLM task and attribute grounding."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..contracts import PromptEnvelope
from ..serialization import strict_json_loads
from .contracts import PolicyContractError, TaskSpecV1


class VlmGroundingError(ValueError):
    """Typed fail-closed adapter error."""


class VlmTimeoutError(VlmGroundingError):
    """A model call exceeded its shadow budget."""


class VlmOutputError(VlmGroundingError):
    """A model result was not the strict expected JSON object."""


MATCH_VALUES = frozenset({"true", "false", "unknown"})


@dataclass(frozen=True, slots=True)
class AttributeGroundingResult:
    class_match: str = "unknown"
    color_match: str = "unknown"
    attribute_match: str = "unknown"
    score: float = 0.0
    observed_class: str | None = None
    observed_color: str | None = None
    source: str = "unknown"
    error: str | None = None

    def __post_init__(self) -> None:
        for name in ("class_match", "color_match", "attribute_match"):
            if getattr(self, name) not in MATCH_VALUES:
                raise VlmGroundingError(f"{name} must be true, false, or unknown")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise VlmGroundingError("score must be numeric")
        if not math.isfinite(float(self.score)) or not 0 <= float(self.score) <= 1:
            raise VlmGroundingError("score must be in [0,1]")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "class_match": self.class_match,
            "color_match": self.color_match,
            "attribute_match": self.attribute_match,
            "score": float(self.score),
            "observed_class": self.observed_class,
            "observed_color": self.observed_color,
            "source": self.source,
            "error": self.error,
        }

    def state_fields(self) -> dict[str, Any]:
        """Return only target match fields accepted by the policy state."""
        return {
            "class_match": self.class_match,
            "color_match": self.color_match,
            "attribute_match": self.attribute_match,
            "grounding_score": float(self.score),
        }


@dataclass(frozen=True, slots=True)
class _Box:
    x1: float
    y1: float
    x2: float
    y2: float

    @classmethod
    def parse(cls, value: object, width: int, height: int) -> _Box:
        if isinstance(value, Mapping):
            values = [value.get(k) for k in ("x1", "y1", "x2", "y2")]
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            values = list(value[:4])
        else:
            raise VlmGroundingError("box must be xyxy")
        if len(values) != 4 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) for v in values
        ):
            raise VlmGroundingError("box coordinates must be finite numbers")
        x1, y1, x2, y2 = map(float, values)
        if max(abs(x1), abs(y1), abs(x2), abs(y2)) <= 1:
            x1, x2, y1, y2 = x1 * width, x2 * width, y1 * height, y2 * height
        x1, x2 = sorted((max(0, min(float(width), x1)), max(0, min(float(width), x2))))
        y1, y2 = sorted((max(0, min(float(height), y1)), max(0, min(float(height), y2))))
        if x2 <= x1 or y2 <= y1:
            raise VlmGroundingError("box has no area")
        return cls(x1, y1, x2, y2)

    def key(self) -> tuple[float, float, float, float]:
        return tuple(round(v, 3) for v in (self.x1, self.y1, self.x2, self.y2))


_COLORS = {
    "red": ("red", "\u7ea2", "\u7ea2\u8272"),
    "blue": ("blue", "\u84dd", "\u84dd\u8272"),
    "green": ("green", "\u7eff", "\u7eff\u8272"),
    "yellow": ("yellow", "\u9ec4", "\u9ec4\u8272"),
    "orange": ("orange", "\u6a59", "\u6a59\u8272"),
    "black": ("black", "\u9ed1", "\u9ed1\u8272"),
    "white": ("white", "\u767d", "\u767d\u8272"),
}
_CLASSES = {
    "vehicle": ("vehicle", "car", "\u6c7d\u8f66", "\u8f66\u8f86"),
    "rover": ("rover", "\u63a2\u6d4b\u8f66", "\u6f2b\u6e38\u8f66"),
    "cargo_cart": ("cargo_cart", "cargo cart", "truck", "\u8d27\u8f66", "\u5361\u8f66"),
    "person": ("person", "\u4eba", "\u884c\u4eba"),
}


def _find_unique(text: str, table: Mapping[str, tuple[str, ...]]) -> str | None:
    lowered = text.casefold()
    hits = {
        key: max((len(word) for word in words if word.casefold() in lowered), default=0)
        for key, words in table.items()
    }
    hits = {key: length for key, length in hits.items() if length}
    if not hits:
        return None
    # Explicit alternatives are ambiguous even when one synonym is longer.
    if len(hits) > 1 and (" or " in lowered or "\u6216" in text):
        return None
    best = max(hits.values())
    winners = [key for key, length in hits.items() if length == best]
    return winners[0] if len(winners) == 1 else None


def _radius(text: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(公里|千米|km|米|m)", text, re.IGNORECASE)
    if match:
        return float(match.group(1)) * (
            1000 if match.group(2).casefold() in {"公里", "千米", "km"} else 1
        )
    digits = {
        "一": 1,
        "两": 2,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    match = re.search(r"([一二两三四五六七八九十]+)\s*(公里|千米|米)", text)
    if match and match.group(1) in digits:
        return float(digits[match.group(1)]) * (1000 if match.group(2) in {"公里", "千米"} else 1)
    return None


def deterministic_task_spec(
    text: str, prompt_sha256: str, *, resolver_id: str = "dev-parser"
) -> TaskSpecV1:
    color, target_class = _find_unique(text, _COLORS), _find_unique(text, _CLASSES)
    if target_class is None and color is not None and "\u8f66" in text:
        target_class = "vehicle"
    if " or " in text.casefold() or "\u6216" in text:
        # Alternatives are intentionally left unresolved in the development
        # parser; the VLM may resolve them later, but ambiguity never becomes a
        # confident target constraint.
        color = None
        target_class = None
    unknown: list[str] = []
    if color is None:
        unknown.append("target.color")
    if target_class is None:
        unknown.append("target.class")
    nearest = any(token in text.casefold() for token in ("nearest", "closest", "最近", "最靠近"))
    relative = "nearest" if nearest else "any"
    attrs: tuple[str, ...] = ()
    attr = re.search(
        r"(?:with|\u5e26\u6709|\u5177\u6709)\s+([\w\u4e00-\u9fff-]{2,20})", text, re.IGNORECASE
    )
    if attr:
        attrs = (attr.group(1).strip(),)
    return TaskSpecV1(
        prompt_sha256=prompt_sha256,
        target_class=target_class,
        target_color=color,
        attributes=attrs,
        relative=relative,
        search_radius_m=_radius(text),
        confidence=1.0 if not unknown else 0.0,
        unknown_fields=tuple(unknown),
        resolver_id=resolver_id,
    )


def _strict(raw: object, name: str) -> Mapping[str, Any]:
    try:
        value = (
            strict_json_loads(raw, name=name) if isinstance(raw, (str, bytes, bytearray)) else raw
        )
    except Exception as exc:
        raise VlmOutputError(f"{name} is not strict JSON") from exc
    if not isinstance(value, Mapping):
        raise VlmOutputError(f"{name} must be an object")
    return value


def _timed(fn: Callable[[], Any], timeout_s: float) -> Any:
    if timeout_s <= 0:
        raise VlmGroundingError("timeout_s must be positive")
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout_s)
    except FutureTimeoutError as exc:
        future.cancel()
        raise VlmTimeoutError("VLM timeout") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


class TaskSpecResolver:
    def __init__(
        self,
        vlm: object | None = None,
        *,
        registered_specs: Mapping[str, TaskSpecV1] | None = None,
        prompt_resolver: object | None = None,
        timeout_s: float = 0.75,
        cache_size: int = 128,
        dev_fallback: bool = True,
    ) -> None:
        self.vlm, self.registered_specs, self.prompt_resolver = (
            vlm,
            dict(registered_specs or {}),
            prompt_resolver,
        )
        self.timeout_s, self.cache_size, self.dev_fallback = (
            float(timeout_s),
            max(1, int(cache_size)),
            bool(dev_fallback),
        )
        self._cache: OrderedDict[tuple[str, str], TaskSpecV1] = OrderedDict()

    def resolve(self, prompt: str | PromptEnvelope, *, prompt_id: str | None = None) -> TaskSpecV1:
        if isinstance(prompt, PromptEnvelope):
            text, digest, supplied_id = prompt.text, prompt.canonical_hash, prompt.prompt_id
        elif isinstance(prompt, str) and prompt.strip():
            text, digest, supplied_id = (
                prompt.strip(),
                hashlib.sha256(prompt.strip().encode()).hexdigest(),
                None,
            )
        else:
            raise VlmGroundingError("prompt must be text or PromptEnvelope")
        pid = prompt_id or supplied_id or ""
        key = (digest, pid)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        result = self.registered_specs.get(pid)
        if result is None and pid and self.prompt_resolver is not None:
            # The catalog is consulted first.  PromptTaskResolver normally
            # returns PromptTaskResolution (mission candidates), so it is
            # deliberately accepted only when a caller supplies a TaskSpecV1
            # adapter; arbitrary runtime/waypoint data never crosses here.
            try:
                candidate = self.prompt_resolver.resolve(prompt)
                if isinstance(candidate, TaskSpecV1):
                    result = candidate
                elif hasattr(candidate, "candidates"):
                    # PromptTaskResolution is a catalog binding.  Project only
                    # the non-privileged class constraint into TaskSpecV1;
                    # runtime geometry, coordinates, and waypoints are never
                    # copied into this shadow contract.
                    candidates = tuple(getattr(candidate, "candidates", ()))
                    classes: set[str] = set()
                    for item in candidates:
                        task = getattr(item, "task", None)
                        for value in getattr(task, "target_classes", ()):
                            if isinstance(value, str) and value.strip():
                                classes.add(value.strip())
                    target_class = next(iter(classes)) if len(classes) == 1 else None
                    result = TaskSpecV1(
                        prompt_sha256=digest,
                        target_class=target_class,
                        confidence=1.0 if target_class else 0.0,
                        unknown_fields=() if target_class else ("target.class",),
                        resolver_id=str(getattr(candidate, "resolver_id", "prompt-catalog")),
                    )
            except Exception:
                result = None
        if result is None and self.vlm is not None:
            try:
                call = (
                    self.vlm.resolve_task_spec
                    if hasattr(self.vlm, "resolve_task_spec")
                    else self.vlm
                )
                value = _strict(_timed(lambda: call(text), self.timeout_s), "TaskSpecV1")
                if set(value) - {
                    "target_class",
                    "target_color",
                    "attributes",
                    "relative",
                    "search_radius_m",
                    "record_location",
                    "return_home",
                    "confidence",
                    "unknown_fields",
                }:
                    raise VlmOutputError("unknown TaskSpec fields")
                result = TaskSpecV1(
                    prompt_sha256=digest,
                    target_class=value.get("target_class"),
                    target_color=value.get("target_color"),
                    attributes=tuple(value.get("attributes", ())),
                    relative=value.get("relative", "unknown"),
                    search_radius_m=value.get("search_radius_m"),
                    record_location=value.get("record_location", True),
                    return_home=value.get("return_home", True),
                    confidence=value.get("confidence", 0.0),
                    unknown_fields=tuple(value.get("unknown_fields", ())),
                    resolver_id="vlm-shadow",
                )
            except (VlmGroundingError, PolicyContractError):
                result = None
        if result is None:
            result = (
                deterministic_task_spec(text, digest)
                if self.dev_fallback
                else TaskSpecV1(
                    prompt_sha256=digest,
                    confidence=0,
                    unknown_fields=("resolver",),
                    resolver_id="unknown",
                )
            )
        self._cache[key] = result
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return result


def _hsv(crop: np.ndarray) -> tuple[str | None, float]:
    px = np.asarray(crop, dtype=np.float32)[..., :3] / 255
    mx, mn = px.max(2), px.min(2)
    diff = mx - mn
    sat = np.divide(diff, mx, out=np.zeros_like(diff), where=mx > 1e-6)
    mask = (sat > 0.25) & (mx > 0.12)
    if int(mask.sum()) < max(4, int(mask.size * 0.02)):
        return None, 0.0
    r, g, b = (px[..., i] for i in range(3))
    hue = np.zeros_like(mx)
    nz = diff > 1e-6
    hue[nz & (mx == r)] = ((g - b)[nz & (mx == r)] / diff[nz & (mx == r)]) % 6
    hue[nz & (mx == g)] = ((b - r)[nz & (mx == g)] / diff[nz & (mx == g)]) + 2
    hue[nz & (mx == b)] = ((r - g)[nz & (mx == b)] / diff[nz & (mx == b)]) + 4
    degree = float(np.median((hue * 60)[mask]))
    color = (
        "red"
        if degree < 15 or degree >= 345
        else "orange"
        if degree < 45
        else "yellow"
        if degree < 75
        else "green"
        if degree < 165
        else "blue"
        if degree < 255
        else "red"
    )
    return color, min(1.0, float(mask.mean()) * 2 + float(np.median(sat[mask])))


class AttributeGrounder:
    def __init__(
        self,
        vlm: object | None = None,
        *,
        timeout_s: float = 0.75,
        cache_size: int = 256,
        margin: float = 0.08,
    ) -> None:
        self.vlm, self.timeout_s, self.cache_size, self.margin = (
            vlm,
            float(timeout_s),
            max(1, int(cache_size)),
            max(0, min(0.5, float(margin))),
        )
        self._cache: OrderedDict[tuple[str, tuple[float, ...], str], AttributeGroundingResult] = (
            OrderedDict()
        )

    def ground(
        self,
        rgb: Any,
        boxes: Sequence[object],
        *,
        target_class: str | None = None,
        target_color: str | None = None,
        attributes: Sequence[str] = (),
    ) -> tuple[AttributeGroundingResult, ...]:
        image = np.asarray(rgb)
        if image.ndim != 3 or image.shape[2] < 3:
            raise VlmGroundingError("RGB image must be HxWx3")
        h, w = image.shape[:2]
        frame = hashlib.sha256(np.ascontiguousarray(image).tobytes()).hexdigest()
        prompt = json.dumps(
            {
                "target_class": target_class,
                "target_color": target_color,
                "attributes": list(attributes),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        output = []
        for raw in boxes:
            box = _Box.parse(raw, w, h)
            key = (frame, box.key(), prompt)
            if key in self._cache:
                self._cache.move_to_end(key)
                output.append(self._cache[key])
                continue
            dx, dy = (box.x2 - box.x1) * self.margin, (box.y2 - box.y1) * self.margin
            left, r = int(max(0, box.x1 - dx)), int(min(w, box.x2 + dx))
            t, b = int(max(0, box.y1 - dy)), int(min(h, box.y2 + dy))
            crop = image[t:b, left:r, :3]
            color, score = _hsv(crop)
            expected = _find_unique(target_color or "", _COLORS) if target_color else None
            color_match = (
                "unknown"
                if color is None or expected is None
                else ("true" if color == expected else "false")
            )
            result = AttributeGroundingResult(
                color_match=color_match, score=score, observed_color=color, source="hsv"
            )
            if self.vlm is not None:
                try:
                    call = (
                        self.vlm.ground_attributes
                        if hasattr(self.vlm, "ground_attributes")
                        else self.vlm
                    )
                    value = _strict(
                        _timed(lambda call=call, crop=crop: call(crop, prompt), self.timeout_s),
                        "attribute VLM",
                    )
                    allowed = {
                        "class_match",
                        "color_match",
                        "attribute_match",
                        "score",
                        "observed_class",
                        "observed_color",
                    }
                    required = {"class_match", "color_match", "attribute_match", "score"}
                    if set(value) - allowed or not required <= set(value):
                        raise VlmOutputError("attribute VLM schema mismatch")
                    result = AttributeGroundingResult(
                        class_match=value["class_match"],
                        color_match=value["color_match"],
                        attribute_match=value["attribute_match"],
                        score=value["score"],
                        observed_class=value.get("observed_class"),
                        observed_color=value.get("observed_color"),
                        source="vlm",
                    )
                except (VlmGroundingError, PolicyContractError) as exc:
                    result = AttributeGroundingResult(source="unknown", error=type(exc).__name__)
            self._cache[key] = result
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
            output.append(result)
        return tuple(output)

    __call__ = ground


__all__ = [
    "AttributeGroundingResult",
    "AttributeGrounder",
    "MATCH_VALUES",
    "TaskSpecResolver",
    "VlmGroundingError",
    "VlmOutputError",
    "VlmTimeoutError",
    "deterministic_task_spec",
]
