#!/usr/bin/env python3
"""Discover a Livox MID-360S from its UDP broadcast and build a runtime config."""
from __future__ import annotations

import argparse
import binascii
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import select
import socket
import struct
import sys
import time


DISCOVERY_PORT = 56000
SDK_HEADER_SIZE = 24
MID360S_DEVICE_TYPE = 35


@dataclass(frozen=True)
class Mid360sDevice:
    ip: str
    serial_number: str
    command_port: int


def build_discovery_request(sequence: int) -> bytes:
    """Build the same empty lidar-search command sent by Livox SDK2."""
    packet = bytearray(SDK_HEADER_SIZE)
    struct.pack_into(
        "<BBHIHBB6sHI", packet, 0,
        0xAA, 0, SDK_HEADER_SIZE, sequence & 0xFFFF, 0, 0, 0, b"\0" * 6, 0, 0,
    )
    # SDK2 writes the FastCRC16 result as a little-endian uint16_t.
    struct.pack_into("<H", packet, 18, binascii.crc_hqx(packet[:18], 0xFFFF))
    return bytes(packet)


def parse_detection_packet(packet: bytes, source_ip: str) -> Mid360sDevice | None:
    """Parse the SDK2 search response without accepting unrelated UDP packets."""
    if len(packet) < SDK_HEADER_SIZE + 24 or packet[0] != 0xAA or packet[1] != 0:
        return None
    packet_length = struct.unpack_from("<H", packet, 2)[0]
    command_id = struct.unpack_from("<H", packet, 8)[0]
    if command_id != 0 or packet_length < SDK_HEADER_SIZE + 24 or packet_length > len(packet):
        return None
    payload = packet[SDK_HEADER_SIZE:packet_length]
    if payload[0] != 0 or payload[1] != MID360S_DEVICE_TYPE:
        return None
    advertised_ip = socket.inet_ntoa(payload[18:22])
    if advertised_ip != source_ip:
        return None
    serial_number = payload[2:18].split(b"\0", 1)[0].decode("ascii", errors="replace")
    command_port = struct.unpack_from("<H", payload, 22)[0]
    return Mid360sDevice(advertised_ip, serial_number, command_port)


def discover_mid360s(host_ip: str, timeout_s: float, prefix_length: int = 24) -> Mid360sDevice:
    network = ipaddress.ip_interface(f"{host_ip}/{prefix_length}").network
    deadline = time.monotonic() + timeout_s
    found: dict[str, Mid360sDevice] = {}
    first_found_at: float | None = None
    # MID-360S sends its answer to the limited-broadcast address.  Linux does
    # not deliver that datagram to a socket bound only to the host address, so
    # mirror SDK2 and keep both host and broadcast-bound sockets open.
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as host_socket,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as broadcast_socket,
    ):
        for listener in (host_socket, broadcast_socket):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        host_socket.bind((host_ip, DISCOVERY_PORT))
        broadcast_socket.bind(("255.255.255.255", DISCOVERY_PORT))
        listeners = [host_socket, broadcast_socket]
        sequence = int(time.monotonic_ns() & 0xFFFF)
        next_request_at = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_request_at:
                host_socket.sendto(build_discovery_request(sequence), ("255.255.255.255", DISCOVERY_PORT))
                sequence = (sequence + 1) & 0xFFFF
                next_request_at = now + 1.0
            remaining = min(deadline - now, max(0.0, next_request_at - now))
            if first_found_at is not None:
                remaining = min(remaining, max(0.0, first_found_at + 1.2 - time.monotonic()))
                if remaining <= 0:
                    break
            if remaining <= 0:
                continue
            try:
                readable, _, _ = select.select(listeners, [], [], remaining)
            except InterruptedError:
                continue
            for listener in readable:
                packet, address = listener.recvfrom(2048)
                source_ip = address[0]
                if ipaddress.ip_address(source_ip) not in network:
                    continue
                device = parse_detection_packet(packet, source_ip)
                if device is None:
                    continue
                found[device.ip] = device
                if first_found_at is None:
                    first_found_at = time.monotonic()
    if not found:
        raise TimeoutError(f"{timeout_s:g}s 内未收到 MID-360S 发现广播")
    if len(found) > 1:
        details = ", ".join(f"{item.ip} ({item.serial_number})" for item in found.values())
        raise RuntimeError(f"发现多台 MID-360S，请使用固定配置选择设备：{details}")
    return next(iter(found.values()))


def write_runtime_config(base_path: Path, output_path: Path, lidar_ip: str) -> None:
    with base_path.open(encoding="utf-8") as stream:
        config = json.load(stream)
    config["lidar_configs"][0]["ip"] = lidar_ip
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + f".tmp.{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="自动发现 MID-360S 并生成 Livox 运行时配置")
    result.add_argument("--host-ip", required=True, help="连接雷达的本机 IPv4 地址")
    result.add_argument("--prefix-length", type=int, default=24, help="雷达子网前缀长度")
    result.add_argument("--timeout", type=float, default=6.0, help="等待发现广播的秒数")
    result.add_argument("--base-config", type=Path, required=True, help="基础 Livox JSON 配置")
    result.add_argument("--output-config", type=Path, required=True, help="自动生成的运行时 JSON")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.timeout <= 0:
        parser().error("--timeout 必须大于 0")
    try:
        device = discover_mid360s(args.host_ip, args.timeout, args.prefix_length)
        write_runtime_config(args.base_config, args.output_config, device.ip)
    except (OSError, ValueError, KeyError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"MID-360S 自动发现失败：{exc}", file=sys.stderr)
        return 2
    print(device.ip)
    print(
        f"发现 MID-360S：SN={device.serial_number} IP={device.ip} CMD={device.command_port}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
