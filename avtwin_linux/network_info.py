from __future__ import annotations

import re
import subprocess
from typing import Any


_ADDRESS_PATTERN = re.compile(r"^\d+:\s+(\S+)\s+inet\s+(\d+(?:\.\d+){3})/\d+")


def parse_ipv4_addresses(output: str) -> list[tuple[str, str]]:
    """Parse `ip -4 -o addr` output into (interface, address) pairs."""
    result: list[tuple[str, str]] = []
    for line in output.splitlines():
        match = _ADDRESS_PATTERN.match(line.strip())
        if match:
            result.append((match.group(1), match.group(2)))
    return result


def parse_route_source(output: str) -> tuple[str | None, str | None]:
    """Return the source IPv4 and interface selected by `ip route get`."""
    tokens = output.replace("\n", " ").split()
    source = tokens[tokens.index("src") + 1] if "src" in tokens else None
    interface = tokens[tokens.index("dev") + 1] if "dev" in tokens else None
    return source, interface


def _ip_output(arguments: list[str]) -> str:
    completed = subprocess.run(
        ["ip", "-4", *arguments], capture_output=True, text=True,
        check=False, timeout=0.8,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "ip 命令执行失败")
    return completed.stdout


def network_snapshot(remote_host: str = "") -> dict[str, Any]:
    addresses = parse_ipv4_addresses(
        _ip_output(["-o", "addr", "show", "up", "scope", "global"]),
    )
    source_ip: str | None = None
    source_interface: str | None = None
    route_error: str | None = None
    if remote_host:
        try:
            source_ip, source_interface = parse_route_source(
                _ip_output(["route", "get", remote_host]),
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            route_error = str(exc)
    return {
        "addresses": addresses,
        "remote_host": remote_host,
        "source_ip": source_ip,
        "source_interface": source_interface,
        "route_error": route_error,
    }


def format_network_status(snapshot: dict[str, Any]) -> str:
    addresses = list(snapshot.get("addresses") or [])
    source_ip = snapshot.get("source_ip")
    source_interface = snapshot.get("source_interface")
    remote_host = snapshot.get("remote_host")
    if source_ip:
        primary = f"Android通信本机IP：{source_ip} ({source_interface}) → {remote_host}"
    elif remote_host:
        primary = f"Android通信本机IP：无法确定到 {remote_host} 的路由"
    else:
        primary = "Android通信本机IP：填写远端 IP 后自动识别"
    visible = "，".join(f"{interface}={address}" for interface, address in addresses)
    return f"{primary}  |  Linux全部IPv4：{visible or '未发现已连接的IPv4网卡'}"
