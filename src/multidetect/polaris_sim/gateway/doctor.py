"""Read-only LAN, SITL, video and AI readiness checks."""

from __future__ import annotations

import ipaddress
import json
import re
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from typing import Any


def doctor_checks(
    *,
    vehicles: list[dict[str, Any]] | None = None,
    tcp_port: int = 5770,
    udp_port: int = 14550,
    http_port: int = 8780,
    rtsp_port: int = 8554,
    ai_port: int = 8790,
    discovery: dict[str, Any] | None = None,
    host: str = "127.0.0.1",
) -> dict[str, Any]:
    """Collect checks without changing firewall, containers, or system settings."""
    vehicle_rows = vehicles or []
    rows: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str, suggestion: str = "") -> None:
        rows.append({"name": name, "status": status, "detail": detail, "suggestion": suggestion})

    listeners = []
    for port in (tcp_port, http_port):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                listeners.append(port)
        except OSError:
            pass
    for port in (tcp_port, http_port):
        if port in listeners:
            add(f"listen:{port}/tcp", "PASS", f"127.0.0.1:{port} accepts TCP connections")
        else:
            add(f"listen:{port}/tcp", "FAIL", f"no TCP listener detected on port {port}",
                f"Check polaris-sim-gateway and allow TCP {port} on the LAN firewall.")
    udp_listeners = _udp_listener_ports()
    vehicle_ports = [int(row.get("ports", {}).get("telemetry") or 0) for row in vehicle_rows]
    for port in (udp_port, *vehicle_ports):
        if port <= 0:
            continue
        if udp_listeners is None:
            add(f"listen:{port}/udp", "WARN", "could not inspect local UDP listeners", "Check with ss -lunp.")
        elif port in udp_listeners:
            add(f"listen:{port}/udp", "PASS", f"UDP port {port} is bound")
        else:
            add(f"listen:{port}/udp", "FAIL", f"no UDP listener detected on port {port}",
                "Check polaris-sim-gateway and the slot's telemetry socket.")

    _check_firewall(add)
    _check_lan_ips(add)
    if not vehicle_rows:
        add("sitl:vehicles", "WARN", "vehicle status is unavailable", "Start a leased slot 5/6/7 SITL vehicle.")
    for vehicle in vehicle_rows:
        sysid = vehicle.get("sysid", "?")
        heartbeat_age = vehicle.get("last_heartbeat_age_s")
        if heartbeat_age is not None and float(heartbeat_age) < 5.0:
            add(f"sitl:heartbeat:sysid-{sysid}", "PASS", f"heartbeat age {float(heartbeat_age):.2f}s")
        else:
            add(f"sitl:heartbeat:sysid-{sysid}", "FAIL", "no recent PX4 heartbeat", "Check the slot's telemetry port and container logs.")
        fix = vehicle.get("gps_fix_type")
        if fix is not None and int(fix) >= 3:
            add(f"sitl:gps:sysid-{sysid}", "PASS", f"GPS fix type {fix}")
        elif fix is None:
            add(f"sitl:gps:sysid-{sysid}", "WARN", "GPS_RAW_INT has not been observed", "Wait for the SITL estimator and GPS lock.")
        else:
            add(f"sitl:gps:sysid-{sysid}", "FAIL", f"GPS fix type {fix}", "Wait for a 3D fix before flight tests.")
        rtf = vehicle.get("rtf")
        if rtf is None:
            add(f"sitl:clock:sysid-{sysid}", "WARN", "simulation real-time factor is not available yet")
        elif float(rtf) >= 0.5:
            add(f"sitl:clock:sysid-{sysid}", "PASS", f"RTF {float(rtf):.2f}")
        else:
            add(f"sitl:clock:sysid-{sysid}", "WARN", f"low RTF {float(rtf):.2f}", "Reduce rendered scene complexity or close competing workloads.")

    try:
        with socket.create_connection((host, rtsp_port), timeout=0.5):
            add("video:rtsp", "PASS", f"TCP {host}:{rtsp_port} accepts connections")
    except OSError as exc:
        add("video:rtsp", "WARN", f"RTSP port unavailable: {exc}", "Start the video lane and verify /v0/cam.")
    ffprobe = shutil.which("ffprobe")
    if ffprobe and vehicle_rows:
        host_name = (discovery or {}).get("host", host)
        for index, vehicle in enumerate(vehicle_rows):
            suffixes = ("cam", "gimbal") if "gimbal" in str(vehicle.get("airframe", "")) else ("cam",)
            for suffix in suffixes:
                url = f"rtsp://{host_name}:{rtsp_port}/v{index}/{suffix}"
                try:
                    result = subprocess.run(
                        [ffprobe, "-v", "error", "-rtsp_transport", "tcp", "-i", url,
                         "-show_entries", "stream=codec_name", "-of", "json", "-read_intervals", "%+1"],
                        capture_output=True, text=True, timeout=5, check=False,
                    )
                    streams = json.loads(result.stdout or "{}").get("streams", [])
                    if result.returncode == 0 and streams:
                        add(f"video:sysid-{vehicle.get('sysid')}:{suffix}", "PASS", f"ffprobe read {streams[0].get('codec_name', 'stream')}")
                    else:
                        add(f"video:sysid-{vehicle.get('sysid')}:{suffix}", "WARN", result.stderr.strip() or "no readable stream", "Check camera publication and RTSP path.")
                except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                    add(f"video:sysid-{vehicle.get('sysid')}:{suffix}", "WARN", f"ffprobe failed: {exc}", "Check camera publication and RTSP path.")
    else:
        add("video:stream", "WARN", "ffprobe is unavailable or no vehicle is registered", "Install ffmpeg locally and start the video lane to test H.264.")

    try:
        request = urllib.request.Request(f"http://{host}:{ai_port}/health", method="GET")
        with urllib.request.urlopen(request, timeout=1.5) as response:
            add("ai:gateway", "PASS" if response.status == 200 else "WARN", f"HTTP {response.status}")
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        add("ai:gateway", "WARN", f"AI gateway unavailable: {exc}", "Start scripts/polaris_sim/ai.py when provided by the AI lane.")

    statuses = [row["status"] for row in rows]
    overall = "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "PASS"
    return {"status": overall, "checks": rows, "lan_ips": _lan_ip_list(), "discovery": discovery or {}}


def _check_firewall(add) -> None:
    ports = ("5770/tcp", "14550/udp", "8554/tcp", "8780/tcp", "8790/tcp")
    cidrs = _lan_cidrs()
    source = cidrs[0] if cidrs else "<LAN_CIDR>"

    def suggestions(selected: list[str]) -> str:
        commands = []
        for item in selected:
            number, protocol = item.split("/", 1)
            commands.append(f"sudo ufw allow from {source} to any port {number} proto {protocol}")
        return "; ".join(commands)

    try:
        result = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=2, check=False)
        output = (result.stdout + result.stderr).strip()
        if "inactive" in output.lower():
            add("firewall:ufw", "PASS", "ufw is inactive")
            return
        if result.returncode == 0 and "active" in output.lower():
            missing = [port for port in ports if not _ufw_allows_lan(output, port, cidrs)]
            if not missing:
                add("firewall:ufw", "PASS", "ufw allows all required ports from a local LAN subnet")
            else:
                add("firewall:ufw", "WARN", f"ufw lacks LAN-scoped allow rules for {', '.join(missing)}",
                    f"Review then run: {suggestions(missing)}")
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        result = subprocess.run(["iptables", "-S"], capture_output=True, text=True, timeout=2, check=False)
        if result.returncode == 0:
            output = result.stdout
            missing = [port for port in ports if not _iptables_allows_lan(output, port, cidrs)]
            if not missing:
                add("firewall:iptables", "PASS", "iptables allows all required ports from a local LAN subnet")
            else:
                commands = suggestions(missing)
                add("firewall:iptables", "WARN", f"iptables lacks LAN-scoped allow rules for {', '.join(missing)}",
                    f"Review firewall policy; suggested UFW commands: {commands}")
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    commands = suggestions(list(ports))
    add("firewall", "WARN", "could not read ufw or iptables rules", f"Review firewall policy; suggested commands: {commands}")


def _source_in_lan(source: str, cidrs: list[str]) -> bool:
    try:
        source_network = ipaddress.ip_network(source, strict=False)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            lan_network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if source_network.version == lan_network.version and source_network.subnet_of(lan_network):
            return True
    return False


def _source_overlaps_lan(source: str, cidrs: list[str]) -> bool:
    if source.lower().startswith("anywhere"):
        return bool(cidrs)
    try:
        source_network = ipaddress.ip_network(source, strict=False)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            lan_network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if source_network.version == lan_network.version and source_network.overlaps(lan_network):
            return True
    return False


def _ufw_allows_lan(output: str, port_protocol: str, cidrs: list[str]) -> bool:
    for line in output.splitlines():
        match = re.match(r"^\s*(\S+)\s+(ALLOW|DENY|REJECT|LIMIT)(?:\s+IN)?\s+(\S+)", line, re.IGNORECASE)
        if not match or match.group(1) != port_protocol:
            continue
        source = match.group(3)
        if not _source_overlaps_lan(source, cidrs):
            continue
        if match.group(2).upper() != "ALLOW":
            return False
        return _source_in_lan(source, cidrs)
    return False


def _iptables_allows_lan(output: str, port_protocol: str, cidrs: list[str]) -> bool:
    port, protocol = port_protocol.split("/", 1)
    for line in output.splitlines():
        fields = line.split()
        try:
            protocol_value = fields[fields.index("-p") + 1]
            port_value = fields[fields.index("--dport") + 1]
            action_key = "-j" if "-j" in fields else "--jump"
            action = fields[fields.index(action_key) + 1]
            source = fields[fields.index("-s") + 1]
        except (ValueError, IndexError):
            continue
        if protocol_value == protocol and port_value == port and action == "ACCEPT" and _source_in_lan(source, cidrs):
            return True
    return False


def _lan_ip_list() -> list[str]:
    try:
        result = subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True, timeout=2, check=False)
        interfaces = json.loads(result.stdout) if result.returncode == 0 else []
        addresses = []
        for interface in interfaces:
            if not isinstance(interface, dict):
                continue
            for item in interface.get("addr_info", []):
                if isinstance(item, dict):
                    local = item.get("local")
                    if local:
                        addresses.append(str(local))
        return addresses
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []


def _udp_listener_ports() -> set[int] | None:
    try:
        result = subprocess.run(["ss", "-H", "-lun"], capture_output=True, text=True, timeout=2, check=False)
        if result.returncode:
            return None
        ports = set()
        for line in result.stdout.splitlines():
            columns = line.split()
            if len(columns) < 4:
                continue
            match = re.search(r":(\d+)$", columns[3])
            if match:
                ports.add(int(match.group(1)))
        return ports
    except (OSError, subprocess.TimeoutExpired):
        return None


def _lan_cidrs() -> list[str]:
    try:
        result = subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True, timeout=2, check=False)
        interfaces = json.loads(result.stdout) if result.returncode == 0 else []
        networks = set()
        for interface in interfaces:
            if not isinstance(interface, dict):
                continue
            for item in interface.get("addr_info", []):
                if not isinstance(item, dict) or not item.get("local") or item.get("prefixlen") is None:
                    continue
                try:
                    address = ipaddress.ip_interface(f"{item['local']}/{int(item['prefixlen'])}")
                except ValueError:
                    continue
                if address.ip.is_private and not address.ip.is_loopback:
                    networks.add(str(address.network))
        return sorted(networks)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []


def _check_lan_ips(add) -> None:
    addresses = _lan_ip_list()
    private = []
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
            if address.is_private and not address.is_loopback:
                private.append(value)
        except ValueError:
            continue
    if private:
        add("network:lan-ip", "PASS", ", ".join(private))
    else:
        add("network:lan-ip", "WARN", "no private LAN address detected", "Connect the workstation to the same private LAN as Polaris.")


def render_report(report: dict[str, Any]) -> str:
    lines = [f"Doctor: {report['status']}"]
    for row in report["checks"]:
        lines.append(f"{row['status']:4} {row['name']}: {row['detail']}")
        if row.get("suggestion"):
            lines.append(f"     修复建议: {row['suggestion']}")
    return "\n".join(lines)


__all__ = ["doctor_checks", "render_report"]
