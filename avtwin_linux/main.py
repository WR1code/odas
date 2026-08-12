#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from avtwin_linux.audio_io import list_devices
    from avtwin_linux.config import ControllerConfig
    from avtwin_linux.controller import Controller
    from avtwin_linux.continuous import ContinuousController
else:
    from .audio_io import list_devices
    from .config import ControllerConfig
    from .controller import Controller
    from .continuous import ContinuousController


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="AV-Twin Linux 双向 acoustic chirp handshake 主控端")
    result.add_argument("--gui", action="store_true", help="打开可视化控制界面")
    result.add_argument("--list-devices", action="store_true", help="列出 PortAudio 输入/输出设备")
    result.add_argument("--c1", type=Path, help="C1 WAV 文件")
    result.add_argument("--c2", type=Path, help="C2 WAV 文件")
    result.add_argument("--input-device", help="稳定设备名（推荐，如 alsa:SPK:0）或当前运行期 index")
    result.add_argument("--output-device", help="稳定设备名（推荐，如 alsa:PCH:0）或当前运行期 index")
    result.add_argument("--output-channel", type=int, default=1, help="扬声器输出声道：0=左，1=右")
    result.add_argument("--playback-gain", type=float, default=1.0, help="C1 播放增益 (0, 2]")
    result.add_argument("--udp-host", default="0.0.0.0")
    result.add_argument("--udp-port", type=int, default=5005)
    result.add_argument("--pre-roll", type=float, default=0.75)
    result.add_argument("--reply-timeout", type=float, default=5.0)
    result.add_argument("--tail", type=float, default=0.75)
    result.add_argument("--c1-threshold", type=float, default=0.30)
    result.add_argument("--c2-threshold", type=float, default=0.30)
    result.add_argument("--rir-method", choices=("deconv", "correlation", "correlation_paper"), default="deconv")
    result.add_argument("--rir-duration", type=float, default=0.5)
    result.add_argument("--deconv-lambda", type=float, default=1e-4)
    result.add_argument("--speed-of-sound", type=float, default=343.0)
    result.add_argument("--linux-local-reference-correction", type=float)
    result.add_argument("--output-root", type=Path, default=Path(__file__).resolve().parent / "output")
    result.add_argument("--capture-mode", choices=("single", "manual_continuous", "timed_continuous"), default="single")
    result.add_argument("--interval", type=float, default=2.0, help="自动轮次间隔，按实际声学 C1 sample 计算")
    result.add_argument("--max-measurements", type=int, default=0, help="最大轮数；0=不限")
    result.add_argument("--max-session-duration", type=float, default=0.0, help="最大会话秒数；0=不限")
    result.add_argument("--startup-countdown", type=float, default=3.0)
    result.add_argument("--android-host", help="Android IP；设置后每轮发送 ARM")
    result.add_argument("--android-port", type=int, default=5005)
    result.add_argument("--overall-policy", choices=("protocol", "rir", "tof", "strict"), default="strict")
    result.add_argument("--min-detection-channels", type=int, default=2)
    result.add_argument("--rir-pre-arrival", type=float, default=0.01)
    return result


def print_devices() -> int:
    print("说明：INDEX 仅在本次运行有效；CLI/持久配置请使用 STABLE NAME。")
    print("实验固定使用 48000 Hz；DEFAULT RATE 只表示设备默认值，不改变实验采样率。\n")
    for item in list_devices():
        print(f"{item['display_name']}")
        print(f"  stable name : {item['stable_name']}")
        print(f"  ALSA        : {item['alsa_stable_hw'] or item['alsa_hw'] or 'logical/virtual route'}")
        print(f"  backend     : {item['backend']} / {item['hostapi']}")
        runtime = item['portaudio_index'] if item['portaudio_index'] >= 0 else 'not used (direct ALSA)'
        print(f"  runtime idx : {runtime}")
        print(
            f"  channels    : in {item['max_input_channels']} / out {item['max_output_channels']} | "
            f"default {item['default_samplerate']:.0f} Hz\n"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.list_devices:
        try:
            return print_devices()
        except Exception as exc:
            print(f"设备枚举失败：{exc}", file=sys.stderr)
            return 2
    if args.gui:
        if __package__ in {None, ""}:
            from avtwin_linux.gui import launch_gui
        else:
            from .gui import launch_gui
        launch_gui(vars(args))
        return 0
    if args.c1 is None or args.c2 is None:
        print("CLI 模式必须指定 --c1 和 --c2；也可使用 --gui 打开界面", file=sys.stderr)
        return 2
    config = ControllerConfig(
        c1=args.c1,
        c2=args.c2,
        input_device=args.input_device,
        output_device=args.output_device,
        output_channel=args.output_channel,
        playback_gain=args.playback_gain,
        udp_host=args.udp_host,
        udp_port=args.udp_port,
        pre_roll=args.pre_roll,
        reply_timeout=args.reply_timeout,
        tail=args.tail,
        c1_threshold=args.c1_threshold,
        c2_threshold=args.c2_threshold,
        rir_method=args.rir_method,
        rir_duration=args.rir_duration,
        deconv_lambda=args.deconv_lambda,
        speed_of_sound=args.speed_of_sound,
        linux_local_reference_correction=args.linux_local_reference_correction,
        output_root=args.output_root,
        capture_mode=args.capture_mode,
        interval=args.interval,
        max_measurements=args.max_measurements,
        max_session_duration=args.max_session_duration,
        startup_countdown=args.startup_countdown,
        android_host=args.android_host,
        android_port=args.android_port,
        overall_policy=args.overall_policy,
        min_detection_channels=args.min_detection_channels,
        rir_pre_arrival=args.rir_pre_arrival,
    )
    try:
        if args.capture_mode == "single":
            _directory, result = Controller(config).run()
            return 0 if result["quality"]["overall"] == "PASS" else 3
        controller = ContinuousController(config)
        if args.capture_mode == "manual_continuous":
            print("手动持续模式：会话启动后，每按一次 Enter 发起一轮；Ctrl+C 安全停止。")

            def read_triggers() -> None:
                while not controller.stop_event.is_set():
                    if sys.stdin.readline() == "":
                        return
                    controller.request_capture()

            threading.Thread(target=read_triggers, name="avtwin-cli-trigger", daemon=True).start()
        _directory, result = controller.run()
    except Exception as exc:
        print(f"AV-Twin 运行失败：{exc}", file=sys.stderr)
        return 2
    return 0 if result["failure_count"] == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
