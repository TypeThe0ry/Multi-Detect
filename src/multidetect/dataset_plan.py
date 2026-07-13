from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .dataset_audit import source_stem

_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})
_ROLE_PRIORITY = {"train": 1, "val": 2, "test": 3}


@dataclass(frozen=True, slots=True)
class PlannedItem:
    source_id: str
    role: str
    image: Path
    label: Path
    group_id: str


def build_dataset_plan(plan_path: Path, out_dir: Path) -> dict[str, Any]:
    plan_path = plan_path.resolve()
    out_dir = out_dir.resolve()
    raw = json.loads(plan_path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise ValueError("unsupported dataset plan schema version")
    class_names = raw.get("class_names")
    sources = raw.get("sources")
    if not isinstance(class_names, list) or not class_names:
        raise ValueError("dataset plan class_names must be a non-empty array")
    if not isinstance(sources, list) or not sources:
        raise ValueError("dataset plan sources must be a non-empty array")

    project_root = plan_path.parents[2] if plan_path.parent.name == "datasets" else Path.cwd()
    items: list[PlannedItem] = []
    source_reports: dict[str, dict[str, Any]] = {}
    source_train_repeats: dict[str, int] = {}
    seen_source_ids: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("dataset source entries must be objects")
        source_id = str(source.get("id", "")).strip()
        if not source_id or source_id in seen_source_ids:
            raise ValueError("dataset source ids must be non-empty and unique")
        seen_source_ids.add(source_id)
        train_repeat = source.get("train_repeat", 1)
        if (
            isinstance(train_repeat, bool)
            or not isinstance(train_repeat, int)
            or not 1 <= train_repeat <= 10
        ):
            raise ValueError(f"dataset source {source_id} train_repeat must be in [1, 10]")
        source_train_repeats[source_id] = train_repeat
        root = _resolve(project_root, source.get("root"))
        split_specs = source.get("splits")
        if not isinstance(split_specs, list) or not split_specs:
            raise ValueError(f"dataset source {source_id} needs at least one split")
        source_count = 0
        roles: dict[str, int] = defaultdict(int)
        for split in split_specs:
            if not isinstance(split, dict):
                raise ValueError(f"dataset source {source_id} split must be an object")
            role = str(split.get("role", "")).strip()
            if not role:
                raise ValueError(f"dataset source {source_id} split role cannot be empty")
            image_dir = root / str(split.get("images", ""))
            label_dir = root / str(split.get("labels", ""))
            if not image_dir.is_dir() or not label_dir.is_dir():
                raise FileNotFoundError(
                    f"dataset source {source_id} split paths are missing: {image_dir} / {label_dir}"
                )
            images = sorted(
                path
                for path in image_dir.iterdir()
                if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES
            )
            labels = {path.stem: path for path in label_dir.glob("*.txt") if path.is_file()}
            for image in images:
                label = labels.get(image.stem)
                if label is None:
                    raise ValueError(f"missing label for {image}")
                items.append(
                    PlannedItem(
                        source_id=source_id,
                        role=role,
                        image=image.resolve(),
                        label=label.resolve(),
                        group_id=f"{source_id}:{source_stem(image)}",
                    )
                )
                source_count += 1
                roles[role] += 1
        source_reports[source_id] = {
            "root": str(root),
            "license": source.get("license", "unknown"),
            "train_repeat": train_repeat,
            "items": source_count,
            "roles": dict(roles),
        }

    selected: list[PlannedItem] = []
    dropped: list[dict[str, str]] = []
    by_group: dict[str, list[PlannedItem]] = defaultdict(list)
    for item in items:
        by_group[item.group_id].append(item)
    for group_id, variants in by_group.items():
        selected_role = max(
            {item.role for item in variants},
            key=lambda role: (_priority(role), role),
        )
        for item in variants:
            if item.role == selected_role:
                selected.append(item)
            else:
                dropped.append(
                    {
                        "group_id": group_id,
                        "source_id": item.source_id,
                        "image": str(item.image),
                        "dropped_role": item.role,
                        "kept_role": selected_role,
                    }
                )

    by_role: dict[str, list[PlannedItem]] = defaultdict(list)
    for item in selected:
        by_role[item.role].append(item)
    out_dir.mkdir(parents=True, exist_ok=True)
    list_paths: dict[str, Path] = {}
    effective_role_counts: dict[str, int] = {}
    for role, role_items in sorted(by_role.items()):
        path = out_dir / f"{role}.txt"
        lines: list[str] = []
        for item in sorted(role_items, key=_item_key):
            repeat = source_train_repeats[item.source_id] if role == "train" else 1
            lines.extend(f"{item.image.as_posix()}\n" for _ in range(repeat))
        path.write_text(
            "".join(lines),
            encoding="utf-8",
        )
        list_paths[role] = path
        effective_role_counts[role] = len(lines)

    required_roles = {"train", "val", "test"}
    if not required_roles.issubset(list_paths):
        missing = sorted(required_roles - list_paths.keys())
        raise ValueError(f"dataset plan is missing required output role: {missing[0]}")
    _write_yaml(
        out_dir / "data.yaml",
        train=list_paths["train"],
        val=list_paths["val"],
        test=list_paths["test"],
        class_names=class_names,
    )
    external_yamls: dict[str, str] = {}
    for role, path in list_paths.items():
        if not role.startswith("external_"):
            continue
        yaml_path = out_dir / f"{role}.yaml"
        _write_yaml(
            yaml_path,
            train=path,
            val=path,
            test=path,
            class_names=class_names,
        )
        external_yamls[role] = str(yaml_path)

    report = {
        "schema_version": 1,
        "plan": str(plan_path),
        "out_dir": str(out_dir),
        "class_names": class_names,
        "sources": source_reports,
        "output_role_counts": effective_role_counts,
        "output_unique_role_counts": {
            role: len(values) for role, values in sorted(by_role.items())
        },
        "cross_role_source_variants_dropped": len(dropped),
        "dropped_examples": dropped[:50],
        "data_yaml": str(out_dir / "data.yaml"),
        "external_yamls": external_yamls,
        "images_copied": False,
        "labels_rewritten": False,
    }
    (out_dir / "build-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _priority(role: str) -> int:
    return 3 if role.startswith("external_") else _ROLE_PRIORITY.get(role, 0)


def _resolve(project_root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("dataset source root must be a non-empty string")
    path = Path(value)
    return (path if path.is_absolute() else project_root / path).resolve()


def _item_key(item: PlannedItem) -> tuple[str, str]:
    return item.source_id, item.image.as_posix()


def _write_yaml(
    path: Path,
    *,
    train: Path,
    val: Path,
    test: Path,
    class_names: list[object],
) -> None:
    lines = [
        f'train: "{train.as_posix()}"',
        f'val: "{val.as_posix()}"',
        f'test: "{test.as_posix()}"',
        "names:",
    ]
    lines.extend(f"  {index}: {name}" for index, name in enumerate(class_names))
    path.write_text("\n".join((*lines, "")), encoding="utf-8")


__all__ = ["PlannedItem", "build_dataset_plan"]
