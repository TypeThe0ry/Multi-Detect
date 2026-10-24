#!/usr/bin/env python3
"""Generate, validate, sign, and summarize onboard mission packages."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_security import AuthenticationKey, load_key, sign_document
from multidetect.autonomy.plan_intent import check_plan_intent
from multidetect.autonomy.serialization import strict_json_loads


def _pair(text: str, count: int, name: str) -> list[float]:
    try:
        values = [float(part.strip()) for part in text.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} 必须是逗号分隔的数字") from exc
    if len(values) != count:
        raise argparse.ArgumentTypeError(f"{name} 需要 {count} 个值")
    return values


def _named(text: str, kind: str) -> tuple[str, list[str]]:
    parts = text.split(":")
    if len(parts) < 2 or not parts[0].strip():
        raise argparse.ArgumentTypeError(f"{kind} 格式错误")
    return parts[0].strip(), parts[1:]


def build_package(args: argparse.Namespace) -> dict[str, Any]:
    nmin, nmax, emin, emax, amin, amax = args.area
    home = args.home
    zones: dict[str, Any] = {}
    places: dict[str, Any] = {}
    labels: dict[str, str] = {}
    for raw in args.zone:
        name, values = _named(raw, "--zone 名称:北,东,半径[:标签]")
        coords = values[0].split(",") if values else []
        if len(values) not in (1, 2) or len(coords) != 3:
            raise argparse.ArgumentTypeError("--zone 格式：名称:北,东,半径[:标签]")
        north, east, radius = _pair(values[0], 3, "zone")
        ref = f"zone:{name}"
        zones[ref] = {"centre_ne": [north, east], "radius_m": radius,
                      "risk_tag": values[1] if len(values) == 2 else "open_field",
                      "oversight_required": False}
        if len(values) == 2:
            labels[ref] = values[1]
    for raw in args.place:
        name, values = _named(raw, "--place 名称:北,东[:标签]")
        coords = values[0].split(",") if values else []
        if len(values) not in (1, 2) or len(coords) != 2:
            raise argparse.ArgumentTypeError("--place 格式：名称:北,东[:标签]")
        north, east = _pair(values[0], 2, "place")
        ref = f"poi:{name}"
        places[ref] = [north, east]
        if len(values) == 2:
            labels[ref] = values[1]
    document: dict[str, Any] = {
        "mission_id": args.mission_id,
        "task_text": args.prompt,
        "risk_level": args.risk,
        "link_loss_policy": {"action": args.link_loss,
                             **({"timeout_s": args.link_timeout} if args.link_loss in {"continue_then_return", "hold_then_return"} else {})},
        "heartbeat_timeout_s": args.heartbeat_timeout,
        "home": home,
        "places": places,
        "zones": zones,
        "routes": {},
        "ref_labels": labels,
        "operating_area": {"north_min_m": nmin, "north_max_m": nmax,
                            "east_min_m": emin, "east_max_m": emax,
                            "min_altitude_m": amin, "max_altitude_m": amax},
        "budget": {"time_s": args.budget[0], "reserve_time_s": args.budget[1],
                   "energy_wh": args.budget[2], "reserve_energy_wh": args.budget[3]},
        "oversight_steps": [],
    }
    if args.capabilities is not None:
        document["capabilities"] = [v.strip() for v in args.capabilities.split(",") if v.strip()]
    return document


def read_document(path: Path) -> dict[str, Any]:
    value = strict_json_loads(path.read_text(encoding="utf-8"), name="mission package")
    if not isinstance(value, dict):
        raise ValueError("任务包 JSON 顶层必须是对象")
    return value


def validate_document(document: dict[str, Any], *, key: AuthenticationKey | None = None) -> list[str]:
    problems: list[str] = []
    try:
        package = load_mission_package(document, key=key)
    except Exception as exc:
        message = str(exc)
        translations = {
            "missing or unknown fields": "存在缺失字段或未知字段",
            "mission package signature verification failed": "任务包签名无效或内容已被篡改",
            "signed mission package requires an operator key path": "已签名任务包需要提供 operator key 才能校验",
            "mission package must be an object": "任务包顶层必须是对象",
        }
        for source, translated in translations.items():
            message = message.replace(source, translated)
        return [f"任务包格式/安全校验失败：{message}"]
    if package.plan is not None:
        try:
            findings = check_plan_intent(package.plan, document.get("task_text", ""), package.refs)
            problems.extend(f"计划意图：{finding.message}" for finding in findings)
        except Exception as exc:
            problems.append(f"内置计划意图检查失败：{exc}")
    return problems


def _package_key(args: argparse.Namespace) -> AuthenticationKey:
    if args.key_hex:
        try:
            material = bytes.fromhex(args.key_hex)
        except ValueError as exc:
            raise ValueError("--key-hex 必须是十六进制") from exc
        return AuthenticationKey("hmac-sha256", material, "default")
    if args.operator_link:
        link = strict_json_loads(args.operator_link.read_text(encoding="utf-8"), name="operator-link")
        value = link.get("operator_package_key_hex") if isinstance(link, dict) else None
        if not isinstance(value, str) or not value:
            raise ValueError("operator-link.json 缺少 operator_package_key_hex")
        return AuthenticationKey("hmac-sha256", bytes.fromhex(value), "default")
    return load_key(args.operator_key)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def command_new(args: argparse.Namespace) -> int:
    document = build_package(args)
    errors = validate_document(document)
    if errors:
        for error in errors:
            print(f"錯誤：{error}", file=sys.stderr)
        return 2
    rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        _write_json(args.output, document)
    else:
        sys.stdout.write(rendered)
    return 0


def command_validate(args: argparse.Namespace) -> int:
    try:
        document = read_document(args.package)
        key = _package_key(args) if (args.key_hex or args.operator_link or args.operator_key) else None
        errors = validate_document(document, key=key)
    except Exception as exc:
        errors = [f"无法读取任务包：{exc}"]
    if errors:
        print("校验失败：")
        for error in errors:
            print(f"- {error}")
        return 1
    print("校验通过：任务包格式有效，内置计划意图一致。")
    return 0


def command_sign(args: argparse.Namespace) -> int:
    try:
        document = read_document(args.package)
        errors = validate_document(document)
        if errors:
            raise ValueError("；".join(errors))
        key = _package_key(args)
        signed = sign_document(document, key)
        destination = args.package if args.in_place else args.output
        if destination is None:
            destination = args.package.with_name(args.package.stem + ".signed" + args.package.suffix)
        if destination.resolve() == args.package.resolve() and not args.in_place:
            raise ValueError("输出路径等于源文件；覆盖必须显式指定 --in-place")
        _write_json(destination, signed)
    except Exception as exc:
        print(f"签名失败：{exc}", file=sys.stderr)
        return 2
    print(f"已签名：{destination}（{signed['signature_alg']}，key_id={signed['key_id']}）")
    return 0


def command_show(args: argparse.Namespace) -> int:
    try:
        document = read_document(args.package)
        package = load_mission_package(document)
    except Exception as exc:
        print(f"无法读取任务包：{exc}", file=sys.stderr)
        return 2
    budget = document["budget"]
    available_wh = budget["energy_wh"] - budget["reserve_energy_wh"]
    available_time = budget["time_s"] - budget["reserve_time_s"]
    endurance_s = min(available_time, available_wh * 3600 / 500)
    print(f"任务：{package.mission_id}")
    print(f"描述：{document.get('task_text', '（未填写）')}")
    print(f"风险：{document['risk_level']}；失联策略：{document['link_loss_policy']['action']}")
    print(f"Home：北 {document['home'][0]:g} m，东 {document['home'][1]:g} m")
    print(f"地点：{len(document['places'])}；区域：{len(document['zones'])}；路线：{len(document['routes'])}")
    print(f"预算：{budget['time_s']:g}s（预留 {budget['reserve_time_s']:g}s），{budget['energy_wh']:g}Wh（预留 {budget['reserve_energy_wh']:g}Wh）")
    print(f"预算可飞时长估算：约 {endurance_s / 60:.1f} 分钟（min(可用时间, 可用能量×3600/500W)；SIM 保守功率下限，非实机续航标定）")
    signature = "已签名" if "signature" in document or "signature_alg" in document else "未签名"
    print(f"签名：{signature}" + (f"（{document.get('signature_alg', '未知')}，key_id={document.get('key_id', '未知')}）" if signature == "已签名" else ""))
    if package.plan is not None:
        print(f"内置计划：{len(package.plan.steps)} 步")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new", help="从参数生成任务包 JSON")
    new.add_argument("--id", dest="mission_id", default="operator-mission-001")
    new.add_argument("--prompt", default="检查指定场地并返航")
    new.add_argument("--zone", action="append", default=[], metavar="名称:北,东,半径[:标签]")
    new.add_argument("--place", action="append", default=[], metavar="名称:北,东[:标签]")
    new.add_argument("--home", type=lambda s: _pair(s, 2, "--home"), default=[0.0, 0.0])
    new.add_argument("--area", type=lambda s: _pair(s, 6, "--area"), default=[-100, 100, -100, 100, 5, 40], metavar="北最小,北最大,东最小,东最大,高度最小,高度最大")
    new.add_argument("--budget", type=lambda s: _pair(s, 4, "--budget"), default=[600, 180, 120, 30], metavar="任务秒,预留秒,能量Wh,预留Wh")
    new.add_argument("--link-loss", choices=("continue", "continue_then_return", "hold_then_return", "return_now", "land_now"), default="continue")
    new.add_argument("--link-timeout", type=float, default=60)
    new.add_argument("--risk", choices=("low", "medium", "high"), default="low")
    new.add_argument("--heartbeat-timeout", type=float, default=5)
    new.add_argument("--capabilities", help="逗号分隔的包能力声明（只可收紧机载能力）")
    new.add_argument("-o", "--output", type=Path)
    new.set_defaults(func=command_new)
    validate = commands.add_parser("validate", help="校验任务包与内置计划意图")
    validate.add_argument("package", type=Path)
    validate.add_argument("--key-hex")
    validate.add_argument("--operator-link", type=Path)
    validate.add_argument("--operator-key", type=Path)
    validate.set_defaults(func=command_validate)
    sign = commands.add_parser("sign", help="使用 HMAC key 签名并另存文件")
    sign.add_argument("package", type=Path)
    source = sign.add_mutually_exclusive_group(required=True)
    source.add_argument("--key-hex")
    source.add_argument("--operator-link", type=Path)
    source.add_argument("--operator-key", type=Path, help="mission_security 支持的 key 文件")
    sign.add_argument("-o", "--output", type=Path)
    sign.add_argument("--in-place", action="store_true")
    sign.set_defaults(func=command_sign)
    show = commands.add_parser("show", help="显示中文摘要与预算续航估算")
    show.add_argument("package", type=Path)
    show.set_defaults(func=command_show)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, argparse.ArgumentTypeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
