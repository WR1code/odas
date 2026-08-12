from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .output_paths import validate_output_root


SAMPLE_RATE = 48_000
CHANNELS = 8


@dataclass(slots=True)
class ControllerConfig:
    c1: Path
    c2: Path
    input_device: int | str | None = None
    output_device: int | str | None = None
    output_channel: int = 1
    playback_gain: float = 1.0
    udp_host: str = "0.0.0.0"
    udp_port: int = 5005
    pre_roll: float = 0.75
    reply_timeout: float = 5.0
    tail: float = 0.75
    c1_threshold: float = 0.30
    c2_threshold: float = 0.30
    rir_method: str = "deconv"
    rir_duration: float = 0.5
    deconv_lambda: float = 1e-4
    speed_of_sound: float = 343.0
    linux_local_reference_correction: float | None = None
    output_root: Path = Path("output")
    capture_mode: str = "single"
    interval: float = 2.0
    max_measurements: int = 0
    max_session_duration: float = 0.0
    startup_countdown: float = 3.0
    android_host: str | None = None
    android_port: int = 5005
    udp_compatibility_mode: bool = True
    overall_policy: str = "strict"
    min_detection_channels: int = 2
    rir_pre_arrival: float = 0.01
    save_rir_npy: bool = True

    def validate(self) -> None:
        if not self.c1.is_file() or not self.c2.is_file():
            raise ValueError("C1 和 C2 都必须是存在的 WAV 文件")
        if self.output_channel < 0:
            raise ValueError("输出声道不能为负数")
        if not 0.0 < self.playback_gain <= 2.0:
            raise ValueError("播放增益必须在 (0, 2] 内")
        if self.udp_port <= 0 or self.udp_port > 65535:
            raise ValueError("UDP 端口必须在 1..65535 内")
        if min(self.pre_roll, self.reply_timeout, self.tail, self.rir_duration) < 0:
            raise ValueError("时间参数不能为负数")
        if not 0.0 < self.c1_threshold <= 1.0 or not 0.0 < self.c2_threshold <= 1.0:
            raise ValueError("检测阈值必须在 (0, 1] 内")
        if self.rir_method not in {"correlation", "correlation_paper", "deconv"}:
            raise ValueError("RIR 方法必须是 correlation 或 deconv")
        if self.capture_mode not in {"single", "manual_continuous", "timed_continuous"}:
            raise ValueError("采集模式必须是 single、manual_continuous 或 timed_continuous")
        if self.interval <= 0:
            raise ValueError("自动采集间隔必须大于 0 秒")
        if self.max_measurements < 0 or self.max_session_duration < 0 or self.startup_countdown < 0:
            raise ValueError("采集数量、会话时长和倒计时不能为负数")
        if self.android_port <= 0 or self.android_port > 65535:
            raise ValueError("Android UDP 端口必须在 1..65535 内")
        if self.overall_policy not in {"protocol", "rir", "tof", "strict"}:
            raise ValueError("PASS 严格程度必须是 protocol、rir、tof 或 strict")
        if self.min_detection_channels < 1 or self.min_detection_channels > CHANNELS:
            raise ValueError(f"有效检测通道数必须在 1..{CHANNELS} 内")
        if self.rir_pre_arrival < 0 or self.rir_pre_arrival >= self.rir_duration:
            raise ValueError("RIR pre-arrival 必须非负且小于 RIR 时长")
        self.output_root = validate_output_root(self.output_root, create=True)
