import socket
import struct
import binascii

from avtwin_linux.mid360s_discovery import build_discovery_request, parse_detection_packet


def _packet(ip: str = "192.168.1.116", device_type: int = 35) -> bytes:
    serial = b"ARMCP5F0037716".ljust(16, b"\0")
    payload = bytes((0, device_type)) + serial + socket.inet_aton(ip) + struct.pack("<H", 56100)
    header = bytearray(24)
    header[0] = 0xAA
    struct.pack_into("<H", header, 2, len(header) + len(payload))
    struct.pack_into("<H", header, 8, 0)
    return bytes(header) + payload


def test_parse_mid360s_detection_packet() -> None:
    result = parse_detection_packet(_packet(), "192.168.1.116")
    assert result is not None
    assert result.ip == "192.168.1.116"
    assert result.serial_number == "ARMCP5F0037716"
    assert result.command_port == 56100


def test_rejects_non_mid360s_and_source_ip_mismatch() -> None:
    assert parse_detection_packet(_packet(device_type=9), "192.168.1.116") is None
    assert parse_detection_packet(_packet(), "192.168.1.117") is None


def test_discovery_request_has_sdk2_header_and_crc() -> None:
    packet = build_discovery_request(123)
    assert len(packet) == 24
    assert packet[:2] == b"\xaa\x00"
    assert struct.unpack_from("<H", packet, 2)[0] == 24
    assert struct.unpack_from("<I", packet, 4)[0] == 123
    assert struct.unpack_from("<H", packet, 8)[0] == 0
    assert struct.unpack_from("<H", packet, 18)[0] == binascii.crc_hqx(packet[:18], 0xFFFF)

    wrapped = build_discovery_request(0x12345)
    assert struct.unpack_from("<I", wrapped, 4)[0] == 0x2345
