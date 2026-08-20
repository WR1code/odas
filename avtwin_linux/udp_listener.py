from __future__ import annotations

from datetime import datetime, timezone
import json
import socket
import threading
import hashlib
from typing import Any, Callable


def validate_mobile_session_command(
    message: dict[str, Any],
    *,
    expected_type: str,
    expected_host: str | None,
    linux_result_port: int,
    mobile_control_port: int,
) -> tuple[bool, str, str, int]:
    """Validate an Android/iOS command that controls the Linux GUI session."""
    source_host = str(message.get("source", "")).rsplit(":", 1)[0]
    reply_port = message.get("mobile_control_port")
    if message.get("type") != expected_type:
        return False, "unexpected_command_type", source_host, mobile_control_port
    if message.get("protocol_version") != 1:
        return False, "unsupported_protocol_version", source_host, mobile_control_port
    if not isinstance(message.get("command_id"), str) or not message["command_id"]:
        return False, "missing_command_id", source_host, mobile_control_port
    if not source_host:
        return False, "missing_source", source_host, mobile_control_port
    if expected_host and source_host != expected_host:
        return False, "source_host_mismatch", source_host, mobile_control_port
    if message.get("linux_result_port") != linux_result_port:
        return False, "linux_result_port_mismatch", source_host, mobile_control_port
    if reply_port != mobile_control_port:
        return False, "mobile_control_port_mismatch", source_host, mobile_control_port
    return True, "accepted", source_host, int(reply_port)


class UdpListener:
    def __init__(self, host: str, port: int, notify: Callable[[str], None] | None = None):
        self.host = host
        self.port = port
        self.notify = notify or (lambda _message: None)
        self.messages: list[dict[str, Any]] = []
        self._raw_lines: list[str] = []
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self._send_lock = threading.Lock()
        self.error: str | None = None

    @property
    def raw_lines(self) -> list[str]:
        return list(self._raw_lines)

    @property
    def is_running(self) -> bool:
        return self.error is None and self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="avtwin-udp", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=1.0)

    def _run(self) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((self.host, self.port))
                sock.settimeout(0.2)
                self._socket = sock
                self._ready.set()
                self.notify(f"UDP 正在监听 {self.host}:{self.port}")
                while not self._stop.is_set():
                    try:
                        payload, source = sock.recvfrom(65535)
                    except socket.timeout:
                        continue
                    received = datetime.now(timezone.utc).isoformat()
                    text = payload.decode("utf-8", errors="replace")
                    envelope: dict[str, Any] = {
                        "received_utc": received,
                        "source": f"{source[0]}:{source[1]}",
                        "raw": text,
                    }
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            envelope.update(parsed)
                            if (
                                parsed.get("protocol") == "AVTWIN_UDP_TEST_V1"
                                and parsed.get("type") == "udp_test_ping"
                            ):
                                reply = {
                                    "protocol": "AVTWIN_UDP_TEST_V1",
                                    "type": "udp_test_reply",
                                    "nonce": parsed.get("nonce"),
                                    "receiver": "linux",
                                }
                                sock.sendto(
                                    json.dumps(reply, separators=(",", ":")).encode(), source,
                                )
                                envelope["automatic_test_reply_sent"] = True
                        else:
                            envelope["parsed"] = parsed
                    except json.JSONDecodeError as exc:
                        envelope["parse_error"] = str(exc)
                    self.messages.append(envelope)
                    self._raw_lines.append(json.dumps(envelope, ensure_ascii=False))
                    label = envelope.get("status") or envelope.get("type") or "unknown"
                    replied = "；已自动回复" if envelope.get("automatic_test_reply_sent") else ""
                    self.notify(f"收到 Android UDP：{label} from {source[0]}:{source[1]}{replied}")
        except OSError as exc:
            self.error = str(exc)
            self._ready.set()
            self.notify(f"WARNING: UDP listener 失败：{exc}")
        finally:
            self._socket = None

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def send_json(self, host: str, port: int, message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self._send_lock:
            bound_socket = self._socket
            if bound_socket is not None:
                bound_socket.sendto(payload, (host, port))
            else:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.sendto(payload, (host, port))
        if message.get("measurement_id") is not None:
            identifier = f"measurement_id={message['measurement_id']}"
        elif message.get("command_id") is not None:
            identifier = f"command_id={message['command_id']}"
        else:
            identifier = "id=none"
        self.notify(
            f"已发送 Android UDP {message.get('type', 'message')}："
            f"{identifier} -> {host}:{port}"
        )

    def send_measurement_quality(
        self, host: str, port: int, *, session_id: str, measurement_id: int,
        quality_pass: bool, quality_overall: str,
        quality_failure_reasons: list[str], tof_available: bool,
        attempts: int = 3,
    ) -> dict[str, Any]:
        """Reliably repeat the idempotent per-measurement quality result to iOS."""
        message: dict[str, Any] = {
            "type": "measurement_quality",
            "protocol_version": 1,
            "session_id": session_id,
            "measurement_id": measurement_id,
            "quality_pass": bool(quality_pass),
            "quality_overall": str(quality_overall),
            "quality_failure_reasons": [str(item) for item in quality_failure_reasons],
            "tof_available": bool(tof_available),
            "receiver": "ios",
        }
        for _attempt in range(max(1, attempts)):
            self.send_json(host, port, message)
        return message


class UdpMeasurementTracker:
    """Associate datagrams once, without leaking late replies into later rounds."""

    def __init__(self, session_id: str, *, compatibility_mode: bool = True):
        self.session_id = session_id
        self.compatibility_mode = compatibility_mode
        self.outstanding: set[int] = set()
        self.completed: set[int] = set()
        self.accepted: dict[int, list[dict[str, Any]]] = {}
        self.arm_acks: dict[int, list[dict[str, Any]]] = {}
        self.events: list[dict[str, Any]] = []
        self._seen: set[str] = set()

    def register(self, measurement_id: int) -> None:
        self.outstanding.add(measurement_id)

    @staticmethod
    def _fingerprint(message: dict[str, Any]) -> str:
        payload = {key: value for key, value in message.items() if key not in {"received_utc", "source", "raw"}}
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
        return hashlib.sha256(encoded).hexdigest()

    def ingest(self, message: dict[str, Any]) -> dict[str, Any]:
        event: dict[str, Any] = {"message": message}
        fingerprint = self._fingerprint(message)
        if message.get("session_id") is None and message.get("measurement_id") is None:
            scope = next(iter(self.outstanding)) if len(self.outstanding) == 1 else "none"
            fingerprint = f"{scope}:{fingerprint}"
        if fingerprint in self._seen:
            event.update(
                status="duplicate", confidence="none",
                measurement_id=message.get("measurement_id"),
            )
            self.events.append(event)
            return event
        self._seen.add(fingerprint)
        message_type = message.get("type")
        if message_type == "arm_ack":
            session = message.get("session_id")
            measurement = message.get("measurement_id")
            if message.get("protocol_version") != 1:
                event.update(status="protocol_version_mismatch", confidence="none")
            elif session != self.session_id:
                event.update(status="session_mismatch", confidence="none")
            elif not isinstance(measurement, int) or measurement not in self.outstanding:
                event.update(status="measurement_id_mismatch", confidence="none")
            else:
                accepted = message.get("accepted") is True
                event.update(
                    status="arm_ack_accepted" if accepted else "arm_ack_rejected",
                    measurement_id=measurement, confidence="high",
                    reason=message.get("reason"),
                )
                self.arm_acks.setdefault(measurement, []).append(message)
            self.events.append(event)
            return event
        if message_type == "reply_timing" and message.get("protocol_version") != 1:
            event.update(status="protocol_version_mismatch", confidence="none")
            self.events.append(event)
            return event
        if message_type not in {None, "reply_timing"}:
            event.update(status="non_reply_message", confidence="none")
            self.events.append(event)
            return event
        session = message.get("session_id")
        measurement = message.get("measurement_id")
        if session is not None or measurement is not None:
            if session != self.session_id:
                event.update(status="session_mismatch", confidence="none")
            elif not isinstance(measurement, int):
                event.update(status="invalid_measurement_id", confidence="none")
            elif measurement in self.completed:
                event.update(status="late", measurement_id=measurement, confidence="none")
            elif measurement not in self.outstanding:
                event.update(status="measurement_id_mismatch", measurement_id=measurement, confidence="none")
            else:
                event.update(status="accepted", measurement_id=measurement, association="explicit_ids", confidence="high")
                self.accepted.setdefault(measurement, []).append(message)
        elif self.compatibility_mode and len(self.outstanding) == 1:
            measurement = next(iter(self.outstanding))
            event.update(status="accepted", measurement_id=measurement,
                         association="single_outstanding_compatibility", confidence="low")
            self.accepted.setdefault(measurement, []).append(message)
        else:
            event.update(status="unassociated", confidence="none")
        self.events.append(event)
        return event

    def messages_for(self, measurement_id: int) -> list[dict[str, Any]]:
        return list(self.accepted.get(measurement_id, []))

    def arm_ack_for(self, measurement_id: int, arm_event_id: str) -> dict[str, Any] | None:
        return next((
            message for message in reversed(self.arm_acks.get(measurement_id, []))
            if message.get("arm_event_id") == arm_event_id
        ), None)

    def association_for(self, measurement_id: int) -> dict[str, Any]:
        events = [event for event in self.events if event.get("measurement_id") == measurement_id]
        accepted = [event for event in events if event["status"] == "accepted"]
        return {
            "method": accepted[-1].get("association") if accepted else "none",
            "confidence": accepted[-1].get("confidence") if accepted else "none",
            "accepted_count": len(accepted),
            "events": [{key: value for key, value in event.items() if key != "message"} for event in events],
        }

    def complete(self, measurement_id: int) -> None:
        self.outstanding.discard(measurement_id)
        self.completed.add(measurement_id)
