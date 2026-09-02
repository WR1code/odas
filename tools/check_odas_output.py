#!/usr/bin/env python3
"""短时运行 ODAS 并验证 tracks JSON 输出。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from uma8_visualizer.json_stream import JSONStreamParser  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--odas-bin", type=Path, default=PROJECT_ROOT / "build/bin/odaslive")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config/odaslive/uma8_v2_visualizer.cfg")
    args = parser.parse_args()
    command = ["stdbuf", "-oL", str(args.odas_bin), "-c", str(args.config)]
    try:
        completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=args.seconds, check=False)
        output = completed.stdout
    except subprocess.TimeoutExpired as exc:
        value = exc.stdout or ""
        output = value.decode(errors="replace") if isinstance(value, bytes) else value
    except OSError as exc:
        print(f"无法启动 ODAS：{exc}", file=sys.stderr)
        return 2

    standalone_closes = sum(1 for line in output.splitlines() if line.strip() == "}")
    parsed = JSONStreamParser().feed(output)
    valid_frames = [obj for obj in parsed if "timeStamp" in obj and isinstance(obj.get("src"), list)]
    if not valid_frames and standalone_closes >= 5:
        print("ODAS JSON output appears corrupted.\nInspect src/sink/snk_tracks.c and rebuild ODAS.", file=sys.stderr)
        return 3
    if not valid_frames:
        print("未检测到包含 timeStamp/src 的完整 JSON；请检查声卡、配置和 ODAS 日志。", file=sys.stderr)
        print(output[-1500:], file=sys.stderr)
        return 2
    fields_ok = any(all(key in src for key in ("x", "y", "z", "activity"))
                    for frame in valid_frames for src in frame["src"] if isinstance(src, dict))
    active = any(float(src.get("activity", 0.0)) > 0.05
                 for frame in valid_frames for src in frame["src"] if isinstance(src, dict))
    print(f"完整 JSON 帧：{len(valid_frames)}")
    print(f"x/y/z/activity 字段：{'正常' if fields_ok else '未在轨迹中发现'}")
    print(f"activity > 0.05：{'是' if active else '否（检查期间可能没有声源）'}")
    if not fields_ok:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
