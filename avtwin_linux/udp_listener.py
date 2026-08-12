from __future__ import annotations

from datetime import datetime, timezone
import json
import socket
import threading
import hashlib
from typing import Any, Callable


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
        self.error: str | None = None

    @property
    def raw_lines(self) -> list[str]:
        return list(self._raw_lines)

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
                        else:
                            envelope["parsed"] = parsed
                    except json.JSONDecodeError as exc:
                        envelope["parse_error"] = str(exc)
                    self.messages.append(envelope)
                    self._raw_lines.append(json.dumps(envelope, ensure_ascii=False))
                    self.notify(f"收到 Android UDP：{envelope.get('status', 'unknown status')}")
        except OSError as exc:
            self.error = str(exc)
            self._ready.set()
            self.notify(f"WARNING: UDP listener 失败：{exc}")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def send_json(self, host: str, port: int, message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(payload, (host, port))
        self.notify(
            f"已发送 Android UDP {message.get('type', 'message')}："
            f"measurement_id={message.get('measurement_id')} -> {host}:{port}"
        )


class UdpMeasurementTracker:
    """Associate datagrams once, without leaking late replies into later rounds."""

    def __init__(self, session_id: str, *, compatibility_mode: bool = True):
        self.session_id = session_id
        self.compatibility_mode = compatibility_mode
        self.outstanding: set[int] = set()
        self.completed: set[int] = set()
        self.accepted: dict[int, list[dict[str, Any]]] = {}
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
            event.update(status="duplicate", confidence="none")
            self.events.append(event)
            return event
        self._seen.add(fingerprint)
        message_type = message.get("type")
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
