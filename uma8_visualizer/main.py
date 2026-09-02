"""UMA-8 v2 AoA 可视化程序入口。"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from .odas_process import ODASReader
from .track_selector import TrackSelector

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ODAS_ROOT = PROJECT_ROOT
DEFAULT_ODAS_BIN = ODAS_ROOT / "build" / "bin" / "odaslive"
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "odaslive" / "uma8_v2_visualizer.cfg"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="miniDSP UMA-8 v2 实时 AoA 可视化")
    parser.add_argument("--odas-bin", type=Path, default=DEFAULT_ODAS_BIN)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--activity-threshold", type=float, default=0.05)
    parser.add_argument("--track-hold-threshold", type=float, default=0.20)
    parser.add_argument("--no-source-timeout", type=float, default=0.8)
    parser.add_argument("--smoothing-alpha", type=float, default=0.22)
    parser.add_argument("--angle-offset", type=float, default=0.0)
    parser.add_argument("--max-visible-tracks", type=int, default=4)
    parser.add_argument("--no-launch-odas", action="store_true")
    parser.add_argument("--input-file", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 0.0 <= args.activity_threshold <= 1.0 or not 0.0 <= args.track_hold_threshold <= 1.0:
        raise SystemExit("activity 阈值必须在 [0, 1] 内")
    if args.no_source_timeout <= 0 or args.max_visible_tracks < 0:
        raise SystemExit("timeout 必须大于 0，max-visible-tracks 不能为负")

    reader = ODASReader()
    try:
        if args.input_file is not None:
            reader.start_stream(args.input_file.expanduser().open("r", encoding="utf-8", errors="replace"), owned=True)
        elif args.no_launch_odas:
            reader.start_stream(sys.stdin)
        else:
            reader.start_process(args.odas_bin.expanduser().resolve(), args.config.expanduser().resolve())
    except (OSError, ValueError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2

    def shutdown(*_unused: object) -> None:
        reader.stop()

    def handle_signal(signum: int, _frame: object) -> None:
        shutdown()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    try:
        from .visualizer import AoAVisualizer
        visualizer = AoAVisualizer(
            reader, TrackSelector(args.activity_threshold, args.track_hold_threshold),
            no_source_timeout=args.no_source_timeout, smoothing_alpha=args.smoothing_alpha,
            angle_offset=args.angle_offset, max_visible_tracks=args.max_visible_tracks,
            on_close=shutdown,
        )
        visualizer.show()
    except Exception as exc:
        print(f"GUI 运行失败：{exc}", file=sys.stderr)
        return 2
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
