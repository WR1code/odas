#!/usr/bin/env python3
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import parse_qs, urlparse

from .spatial_calibration import (
    calibrate_point_clouds, read_avpc, save_calibration,
)


class CalibrationService:
    def __init__(self, directory: Path, lidar_map: Path, *, token: str = ""):
        self.directory = directory.resolve()
        self.lidar_map = lidar_map.resolve()
        self.token = token
        self.phone_map = self.directory / "phone_map.avpc"
        self.result = self.directory / "active_transform.json"
        self.directory.mkdir(parents=True, exist_ok=True)

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] | None = None
        if self.result.is_file():
            try:
                result = json.loads(self.result.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                result = None
        return {
            "protocol": "AVTWIN_CALIBRATION_HTTP_V1",
            "lidar_map_ready": self.lidar_map.is_file(),
            "phone_map_ready": self.phone_map.is_file(),
            "calibration": result,
        }

    def authorize(self, header: str | None) -> bool:
        return not self.token or header == f"Bearer {self.token}"

    def lidar_map_payload(self) -> bytes:
        if not self.lidar_map.is_file():
            raise FileNotFoundError("雷达地图尚未采集")
        # Validate the file before exposing it so iOS never receives a stale
        # or partially written AVPC payload.
        read_avpc(self.lidar_map)
        return self.lidar_map.read_bytes()

    def accept_phone_map(self, payload: bytes) -> dict[str, Any]:
        if len(payload) > 200 * 1024 * 1024:
            raise ValueError("手机点云超过 200 MiB 限制")
        with tempfile.NamedTemporaryFile(dir=self.directory, suffix=".avpc", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        try:
            phone_points, phone_metadata = read_avpc(temporary)
            temporary.replace(self.phone_map)
        finally:
            temporary.unlink(missing_ok=True)
        response: dict[str, Any] = {
            "uploaded": True,
            "phone_points": int(phone_points.shape[0]),
            "phone_frame_id": phone_metadata.get("frame_id"),
            "calibrated": False,
        }
        if not self.lidar_map.is_file():
            response["reason"] = "雷达地图尚未采集"
            return response
        lidar_points, lidar_metadata = read_avpc(self.lidar_map)
        result = calibrate_point_clouds(
            phone_points,
            lidar_points,
            source_frame_id=str(phone_metadata.get("frame_id") or "arkit_user_origin_x_forward_y_left_z_up"),
            target_frame_id=str(lidar_metadata.get("frame_id") or "mid360_map"),
        )
        response["calibrated"] = True
        response["result"] = result.to_dict()
        # Preserve failed diagnostics but never activate a transform that did
        # not satisfy overlap, residual, and ambiguity checks.
        save_calibration(self.directory / "latest_result.json", result)
        if result.quality.accepted:
            save_calibration(self.result, result)
        return response


def handler_for(service: CalibrationService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AVTwinCalibration/1"

        def _json(self, status: int, value: dict[str, Any]) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/v1/lidar-map":
                try:
                    payload = service.lidar_map_payload()
                except (OSError, ValueError) as exc:
                    self._json(404, {"error": str(exc)})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.avtwin.point-cloud")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            if path != "/v1/status":
                self._json(404, {"error": "not_found"})
                return
            self._json(200, service.status())

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/v1/phone-map":
                self._json(404, {"error": "not_found"})
                return
            if not service.authorize(self.headers.get("Authorization")):
                self._json(403, {"error": "forbidden"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0:
                    raise ValueError("请求没有点云数据")
                # Parse now so malformed query strings cannot affect filenames.
                parse_qs(parsed.query)
                result = service.accept_phone_map(self.rfile.read(length))
                self._json(200, result)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{self.client_address[0]} - {format % args}", flush=True)

    return Handler


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="接收 iPhone 点云并与 MID-360S 地图自动配准")
    result.add_argument("--host", default="0.0.0.0")
    result.add_argument("--port", type=int, default=5010)
    result.add_argument("--directory", type=Path, default=Path("avtwin_linux/calibration"))
    result.add_argument("--lidar-map", type=Path, default=Path("avtwin_linux/calibration/lidar_map.avpc"))
    result.add_argument("--token", default="", help="可选 Bearer token")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser().error("--port 必须在 1..65535")
    service = CalibrationService(args.directory, args.lidar_map, token=args.token)
    server = ThreadingHTTPServer((args.host, args.port), handler_for(service))
    print(
        f"空间标定服务：http://{args.host}:{args.port} | 雷达地图 {service.lidar_map}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
