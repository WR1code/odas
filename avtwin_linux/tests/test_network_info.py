from __future__ import annotations

from avtwin_linux.network_info import (
    format_network_status, parse_ipv4_addresses, parse_route_source,
)


def test_parses_linux_ipv4_addresses() -> None:
    output = """\
2: enp5s0    inet 192.168.1.5/24 brd 192.168.1.255 scope global enp5s0
3: wlp4s0    inet 192.168.1.199/24 brd 192.168.1.255 scope global wlp4s0
"""
    assert parse_ipv4_addresses(output) == [
        ("enp5s0", "192.168.1.5"), ("wlp4s0", "192.168.1.199"),
    ]


def test_parses_route_selected_linux_source_ip() -> None:
    output = "192.168.1.101 dev wlp4s0 src 192.168.1.199 uid 1000\n    cache\n"
    assert parse_route_source(output) == ("192.168.1.199", "wlp4s0")


def test_status_distinguishes_android_route_from_other_interfaces() -> None:
    text = format_network_status({
        "remote_host": "192.168.1.101",
        "source_ip": "192.168.1.199",
        "source_interface": "wlp4s0",
        "addresses": [("enp5s0", "192.168.1.5"), ("wlp4s0", "192.168.1.199")],
    })
    assert "Android通信本机IP：192.168.1.199 (wlp4s0)" in text
    assert "enp5s0=192.168.1.5" in text
