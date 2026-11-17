"""Optional bridge to the Sirius runtime checkout or installed package."""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace

from .events import EventJournal
from .inputs import FrameSample, current_frame

EVENT_NAMES = {
    "sirius.query.parsed": "query.parsed",
    "sirius.vega.detected": "vega.detected",
    "sirius.vlm.reviewed": "vlm.reviewed",
    "sirius.mizar.located": "mizar.located",
    "sirius.tracking.updated": "tracking.updated",
    "sirius.published": "published",
    "sirius.runtime.degraded": "runtime.degraded",
    "sirius.runtime.failed": "runtime.failed",
    "sirius.adapter.fallback": "adapter.fallback",
}
COMPONENTS = ("altair", "vega", "vlm", "mizar", "tracking", "publish_polaris")


class SiriusUnavailable(RuntimeError):
    """Sirius source is not available in this service environment."""


class RuntimeEventSink:
    def __init__(self, journal: EventJournal) -> None:
        self.journal = journal
        self._local = threading.local()
        self._lock = threading.Lock()
        self._latency_ms: dict[str, float] = {}
        self._sequence = 0

    def bind(self) -> None:
        now = time.perf_counter()
        self._local.started = now
        self._local.last = now

    def unbind(self) -> None:
        self._local.started = None
        self._local.last = None

    def emit(self, name: str, payload: Mapping[str, object]) -> object:
        now = time.perf_counter()
        component = name.removeprefix("sirius.").split(".", 1)[0]
        component = {"query": "altair", "published": "publish_polaris"}.get(component, component)
        previous = getattr(self._local, "last", None) or getattr(self._local, "started", None)
        if component in COMPONENTS and previous is not None:
            with self._lock:
                self._latency_ms[component] = max(0.0, (now - previous) * 1000.0)
        self._local.last = now
        event_name = EVENT_NAMES.get(name)
        if event_name is not None:
            self.journal.publish(event_name, payload)
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        return SimpleNamespace(sequence=sequence, name=name, payload=dict(payload), timestamp_s=now)

    def latencies(self) -> dict[str, float | None]:
        with self._lock:
            return {name: self._latency_ms.get(name) for name in COMPONENTS}


class SiriusRuntimeBridge:
    """Loads Sirius without copying its runtime into the Multi-Detect package."""

    def __init__(
        self,
        *,
        source: str | None,
        components: Mapping[str, object],
        adapters: Mapping[str, object],
        journal: EventJournal,
        publisher: object,
    ) -> None:
        if source:
            self._extend_package_path(source)
        try:
            runtime_module = importlib.import_module("multidetect.sirius_runtime")
            adapters_module = importlib.import_module("multidetect.sirius_runtime_adapters")
            contract_module = importlib.import_module("multidetect.sirius_spica_contract")
        except (ImportError, ModuleNotFoundError) as exc:
            raise SiriusUnavailable("Sirius runtime modules are not importable") from exc

        self.runtime_module = runtime_module
        self.adapters_module = adapters_module
        self.contract_module = contract_module
        self.event_sink = RuntimeEventSink(journal)
        self._runtime_lock = threading.RLock()
        self.adapters = self._build_adapters(adapters, publisher)
        runtime_components = runtime_module.RuntimeComponents(
            **{name: bool(components.get(name, False)) for name in COMPONENTS},
            publish_altair=False,
        )
        runtime_config = runtime_module.SiriusRuntimeConfig(
            components=runtime_components,
            publisher_destinations=("polaris",),
        )
        self.runtime = runtime_module.SiriusRuntime(
            runtime_config,
            parser=runtime_module.HeuristicAltairParser(),
            detector=self.adapters["vega"],
            reviewer=runtime_module.StubVlmReviewer(),
            locator=self.adapters["mizar"],
            tracker=runtime_module.StubTracker(),
            publisher=publisher,
            events=self.event_sink,
        )
        self.publisher = publisher
        self._component_objects = {
            "altair": self.runtime.parser,
            "vega": self.runtime.detector,
            "vlm": self.runtime.reviewer,
            "mizar": self.runtime.locator,
            "tracking": self.runtime.tracker,
        }

    @staticmethod
    def _extend_package_path(source: str) -> None:
        import multidetect

        root = Path(source).expanduser().resolve()
        package_dir = root / "multidetect" if root.name == "src" else root / "src" / "multidetect"
        if not package_dir.is_dir():
            raise SiriusUnavailable(f"Sirius source path has no src/multidetect package: {root}")
        package_paths = getattr(multidetect, "__path__", None)
        if package_paths is None:
            raise SiriusUnavailable("multidetect is not a package")
        if str(package_dir) not in package_paths:
            package_paths.append(str(package_dir))
        importlib.invalidate_caches()

    @staticmethod
    def _load_adapter(spec: object) -> object | None:
        if not isinstance(spec, str) or not spec:
            return None
        module_name, separator, attribute = spec.partition(":")
        if not separator:
            raise ValueError("adapter spec must use module:attribute")
        value: object = importlib.import_module(module_name)
        for part in attribute.split("."):
            value = getattr(value, part)
        return value() if inspect.isclass(value) else value

    def _build_adapters(self, config: Mapping[str, object], publisher: object) -> dict[str, object]:
        runtime = self.runtime_module
        adapter_module = self.adapters_module
        vega_delegate = self._load_adapter(config.get("vega"))
        if vega_delegate is None:
            vega = runtime.StubVegaDetector()
        else:
            vega = adapter_module.VegaDetectorAdapter(
                detector=vega_delegate,
                fallback=runtime.StubVegaDetector(),
                events=self.event_sink,
            )

        mizar_delegate = self._load_adapter(config.get("mizar"))
        if mizar_delegate is None:
            mizar = runtime.StubMizarLocator()
        else:
            locate = getattr(mizar_delegate, "locate", mizar_delegate)

            def locate_with_context(
                query: object,
                detections: Sequence[object],
                outputs: Sequence[str],
            ) -> object:
                sample = current_frame()
                if not callable(locate):
                    raise TypeError("configured Mizar adapter is not callable")
                parameters = inspect.signature(locate).parameters
                kwargs: dict[str, object] = {}
                accepts_kwargs = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
                if "context" in parameters or accepts_kwargs:
                    kwargs["context"] = sample
                if "telemetry" in parameters or accepts_kwargs:
                    kwargs["telemetry"] = {} if sample is None else dict(sample.telemetry)
                if "frame_sample" in parameters:
                    kwargs["frame_sample"] = sample
                return locate(query, detections, outputs, **kwargs)

            mizar = adapter_module.MizarLocatorAdapter(
                locate_fn=locate_with_context,
                fallback=runtime.StubMizarLocator(),
                events=self.event_sink,
            )
        return {"vega": vega, "mizar": mizar, "publisher": publisher}

    def query_structured(
        self,
        request: Mapping[str, object],
        *,
        frame: FrameSample,
    ) -> tuple[dict[str, object], object]:
        self.contract_module.validate_spica_request(request)
        query_mapping = request["query"]
        query = self.contract_module.spica_query_to_internal(query_mapping)
        watch_value = query_mapping.get("watch", False)
        watch = (
            watch_value.get("enabled", False)
            if isinstance(watch_value, Mapping)
            else bool(watch_value)
        )
        with self._runtime_lock:
            result = self.runtime.query_target(
                query,
                outputs=query_mapping.get("outputs"),
                watch=watch,
                frame=frame,
            )
            response = self._project(
                result,
                query,
                request.get("request_id"),
                query_mapping.get("outputs"),
            )
            return response, result

    def query_prompt(
        self,
        prompt: str,
        *,
        request_id: str,
        outputs: Sequence[str] | None,
        watch: bool,
        frame: FrameSample,
    ) -> tuple[dict[str, object], object]:
        with self._runtime_lock:
            result = self.runtime.query_target(prompt, outputs=outputs, watch=watch, frame=frame)
            query = result.query
            if query is None:
                query = self.runtime_module.TargetQuery.from_mapping(
                    {
                        "schema_version": "1.0",
                        "attributes": [{"name": "raw_prompt", "op": "eq", "value": prompt}],
                    }
                )
            return self._project(result, query, request_id, outputs), result

    def _project(
        self,
        result: object,
        query: object,
        request_id: object,
        outputs: object,
    ) -> dict[str, object]:
        response = self.adapters_module.runtime_result_to_spica_response(
            result,
            query,
            request_id=request_id if isinstance(request_id, str) else None,
            latency_ms=0.0,
            outputs=(
                outputs
                if isinstance(outputs, Sequence) and not isinstance(outputs, str)
                else None
            ),
            default_outputs=self.runtime.config.default_outputs,
        )
        if getattr(getattr(result, "stage", None), "value", None) == "abstain":
            response["status"] = "unavailable"
            response["data"] = None
        return response

    def set_components(self, changes: Mapping[str, object]) -> dict[str, bool]:
        with self._runtime_lock:
            unknown = set(changes) - set(COMPONENTS)
            if unknown:
                raise ValueError(f"unknown components: {', '.join(sorted(unknown))}")
            current = dataclasses.asdict(self.runtime.config.components)
            for name, value in changes.items():
                if type(value) is not bool:
                    raise ValueError(f"components.{name} must be boolean")
                current[name] = value
            current["publish_altair"] = False
            new_components = self.runtime_module.RuntimeComponents(**current)
            self.runtime.config = dataclasses.replace(
                self.runtime.config,
                components=new_components,
            )
            return {name: bool(getattr(new_components, name)) for name in COMPONENTS}

    def status(self) -> dict[str, object]:
        component_flags = dataclasses.asdict(self.runtime.config.components)
        latencies = self.event_sink.latencies()
        component_rows = {}
        for name in ("altair", "vega", "vlm", "mizar", "tracking"):
            adapter = self._component_objects[name]
            component_rows[name] = {
                "enabled": bool(component_flags.get(name, False)),
                "available": bool(getattr(adapter, "available", True)),
                "stub": (
                    adapter.__class__.__name__.startswith("Stub")
                    or adapter.__class__.__name__ == "HeuristicAltairParser"
                    or getattr(adapter, "available", True) is False
                ),
                "version": "sirius-runtime-api-v1",
                "latency_ms": latencies.get(name),
            }
        component_rows["publish_polaris"] = {
            "enabled": bool(component_flags.get("publish_polaris", False)),
            "available": self.publisher is not None,
            "stub": not bool(getattr(self.publisher, "active", False)),
            "transport_active": bool(getattr(self.publisher, "active", False)),
            "version": "spica-envelope-v1",
            "latency_ms": latencies.get("publish_polaris"),
        }
        return {
            "available": True,
            "version": "sirius-runtime-api-v1",
            "components": component_rows,
            "degradation_order": [item.value for item in self.runtime.config.degradation_order],
        }


def load_sirius_bridge(
    *,
    source: str | None,
    components: Mapping[str, object],
    adapters: Mapping[str, object],
    journal: EventJournal,
    publisher: object,
) -> SiriusRuntimeBridge:
    return SiriusRuntimeBridge(
        source=source,
        components=components,
        adapters=adapters,
        journal=journal,
        publisher=publisher,
    )

