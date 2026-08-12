from __future__ import annotations

import queue
import json
import os
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from .audio_io import (
    AudioDeviceInfo,
    list_audio_devices,
    output_warnings,
    play_safe_output_test,
    recommend_input,
    recommend_output,
)
from .config import SAMPLE_RATE, ControllerConfig
from .controller import Controller
from .continuous import ContinuousController
from .output_paths import validate_output_root


class ControllerGui:
    def __init__(self, root: tk.Tk, defaults: dict[str, Any] | None = None):
        self.root = root
        self.root.title("AV-Twin Linux 声学握手控制器")
        self.root.minsize(1100, 820)
        self.defaults = defaults or {}
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.active_controller: Controller | ContinuousController | None = None
        self.devices: list[AudioDeviceInfo] = []
        self.input_choices: dict[str, AudioDeviceInfo] = {}
        self.output_choices: dict[str, AudioDeviceInfo] = {}
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.preferences_path = config_home / "avtwin-linux" / "gui.json"
        self.preferences = self._load_preferences()
        probe_root = Path(__file__).resolve().parent.parent / "wav"
        default_c1 = probe_root / "c1_mono.wav"
        default_c2 = probe_root / "c2_mono.wav"
        self.levels = np.zeros(8)
        self.waveform = np.zeros((2, 8))
        self.waveform_step = 1
        self.rir_waveform = np.zeros((2, 8))
        self._rir_seen = False
        self._last_audio_event = 0.0
        self.vars = {
            "c1": tk.StringVar(value=str(self.defaults.get("c1") or (default_c1 if default_c1.is_file() else ""))),
            "c2": tk.StringVar(value=str(self.defaults.get("c2") or (default_c2 if default_c2.is_file() else ""))),
            "input": tk.StringVar(),
            "output": tk.StringVar(),
            "output_channel": tk.StringVar(value=str(self.defaults.get("output_channel", 1))),
            "gain": tk.StringVar(value=str(self.defaults.get("playback_gain", 1.0))),
            "udp_port": tk.StringVar(value=str(self.defaults.get("udp_port", 5005))),
            "timeout": tk.StringVar(value=str(self.defaults.get("reply_timeout", 5.0))),
            "c1_threshold": tk.StringVar(value=str(self.defaults.get("c1_threshold", 0.30))),
            "c2_threshold": tk.StringVar(value=str(self.defaults.get("c2_threshold", 0.30))),
            "rir_method": tk.StringVar(value=str(self.defaults.get("rir_method", "deconv"))),
            "capture_mode": tk.StringVar(value=str(self.defaults.get("capture_mode", "single"))),
            "interval": tk.StringVar(value=str(self.defaults.get("interval", 2.0))),
            "max_measurements": tk.StringVar(value=str(self.defaults.get("max_measurements", 0))),
            "max_session_duration": tk.StringVar(value=str(self.defaults.get("max_session_duration", 0.0))),
            "android_host": tk.StringVar(value=str(self.defaults.get("android_host") or "")),
            "overall_policy": tk.StringVar(value=str(self.defaults.get("overall_policy", "strict"))),
            "output_root": tk.StringVar(value=str(Path(
                self.preferences.get("output_root")
                or self.defaults.get("output_root", Path(__file__).parent / "output")
            ).expanduser().resolve())),
            "status": tk.StringVar(value="就绪：先选择 C1、C2、UMA-8 输入和扬声器输出设备"),
            "session_status": tk.StringVar(value="状态 IDLE | measurement 0 | 成功 0 / 失败 0 / 跳过 0"),
        }
        self._build()
        self.refresh_devices()
        self.root.after(80, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        files = ttk.LabelFrame(outer, text="Chirp 音频（主动选择）", padding=10)
        files.pack(fill="x")
        self._file_row(files, 0, "C1 发送模板", "c1")
        self._file_row(files, 1, "C2 返回/RIR 模板", "c2")

        device_frame = ttk.LabelFrame(outer, text="音频设备（输入与输出完全独立）", padding=10)
        device_frame.pack(fill="x", pady=(10, 0))
        ttk.Label(device_frame, text="UMA-8 输入设备").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.input_combo = ttk.Combobox(device_frame, textvariable=self.vars["input"], state="readonly", width=68)
        self.input_combo.grid(row=0, column=1, sticky="ew")
        ttk.Label(device_frame, text="扬声器输出设备").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(7, 0))
        self.output_combo = ttk.Combobox(device_frame, textvariable=self.vars["output"], state="readonly", width=68)
        self.output_combo.grid(row=1, column=1, sticky="ew", pady=(7, 0))
        self.test_output_button = ttk.Button(device_frame, text="测试输出", command=self.test_output)
        self.test_output_button.grid(row=1, column=2, padx=(10, 0), pady=(7, 0))
        ttk.Button(device_frame, text="刷新设备", command=self.refresh_devices).grid(row=0, column=2, padx=(10, 0))
        self.input_combo.bind("<<ComboboxSelected>>", lambda _event: self._save_preferences())
        self.output_combo.bind("<<ComboboxSelected>>", lambda _event: self._save_preferences())
        device_frame.columnconfigure(1, weight=1)

        params = ttk.LabelFrame(outer, text="实验参数", padding=10)
        params.pack(fill="x", pady=(10, 0))
        fields = [
            ("输出声道 (0=左, 1=右)", "output_channel"), ("播放增益", "gain"),
            ("UDP 端口", "udp_port"), ("C2 超时 (s)", "timeout"),
            ("C1 阈值", "c1_threshold"), ("C2 阈值", "c2_threshold"),
            ("自动间隔 (s)", "interval"), ("最大条数 (0=不限)", "max_measurements"),
            ("最大时长 (s, 0=不限)", "max_session_duration"), ("Android IP (可空)", "android_host"),
        ]
        for idx, (label, key) in enumerate(fields):
            col = (idx % 3) * 2
            row = idx // 3
            ttk.Label(params, text=label).grid(row=row, column=col, sticky="w", padx=(0, 5), pady=3)
            ttk.Entry(params, textvariable=self.vars[key], width=12).grid(row=row, column=col + 1, sticky="w", padx=(0, 18))
        ttk.Label(params, text="采集模式").grid(row=4, column=0, sticky="w", pady=3)
        ttk.Combobox(params, textvariable=self.vars["capture_mode"], values=("single", "manual_continuous", "timed_continuous"), state="readonly", width=20).grid(row=4, column=1, sticky="w")
        ttk.Label(params, text="RIR 方法").grid(row=4, column=2, sticky="w", pady=3)
        ttk.Combobox(params, textvariable=self.vars["rir_method"], values=("deconv", "correlation", "correlation_paper"), state="readonly", width=18).grid(row=4, column=3, sticky="w")
        ttk.Label(params, text="PASS 严格程度").grid(row=4, column=4, sticky="w", pady=3)
        ttk.Combobox(params, textvariable=self.vars["overall_policy"], values=("protocol", "rir", "tof", "strict"), state="readonly", width=12).grid(row=4, column=5, sticky="w")
        ttk.Label(params, text="结果保存目录（绝对路径）").grid(row=5, column=0, sticky="w")
        ttk.Entry(params, textvariable=self.vars["output_root"]).grid(row=5, column=1, columnspan=4, sticky="ew")
        ttk.Button(params, text="选择结果保存目录…", command=self._choose_output).grid(row=5, column=5, padx=(6, 0))
        params.columnconfigure(3, weight=1)

        monitor = ttk.LabelFrame(outer, text="实时 8 通道输入电平", padding=8)
        monitor.pack(fill="x", pady=(10, 0))
        self.level_canvases: list[tk.Canvas] = []
        for ch in range(8):
            ttk.Label(monitor, text=f"CH{ch}").grid(row=0, column=ch, padx=4)
            canvas = tk.Canvas(monitor, width=92, height=32, bg="#17202a", highlightthickness=0)
            canvas.grid(row=1, column=ch, padx=3)
            self.level_canvases.append(canvas)
            monitor.columnconfigure(ch, weight=1)
        self.wave_notebook = ttk.Notebook(monitor)
        self.wave_notebook.grid(row=2, column=0, columnspan=8, sticky="ew", pady=(8, 0))
        live_frame = ttk.Frame(self.wave_notebook)
        rir_frame = ttk.Frame(self.wave_notebook)
        self.wave_notebook.add(live_frame, text="实时输入波形")
        self.wave_notebook.add(rir_frame, text="C2 RIR（等待 C2）")
        self.live_figure = Figure(figsize=(10, 2.4), dpi=100)
        self.live_axis = self.live_figure.add_subplot(111)
        self.live_plot = FigureCanvasTkAgg(self.live_figure, master=live_frame)
        self.live_plot.get_tk_widget().configure(height=230)
        self.live_plot.get_tk_widget().pack(fill="both", expand=True)
        self.rir_figure = Figure(figsize=(10, 2.8), dpi=100)
        self.rir_all_axis = self.rir_figure.add_subplot(121)
        self.rir_early_axis = self.rir_figure.add_subplot(122)
        self.rir_plot = FigureCanvasTkAgg(self.rir_figure, master=rir_frame)
        self.rir_plot.get_tk_widget().configure(height=250)
        self.rir_plot.get_tk_widget().pack(fill="both", expand=True)
        self._configure_empty_plots()

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(10, 0))
        self.start_button = ttk.Button(actions, text="开始会话", command=self.start)
        self.start_button.pack(side="left")
        self.capture_button = ttk.Button(actions, text="采集一次", command=self.capture_once, state="disabled")
        self.capture_button.pack(side="left", padx=(8, 0))
        self.pause_button = ttk.Button(actions, text="暂停自动采集", command=self.toggle_pause, state="disabled")
        self.pause_button.pack(side="left", padx=(8, 0))
        self.stop_button = ttk.Button(actions, text="安全停止并保存", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Label(actions, textvariable=self.vars["status"]).pack(side="left", padx=16)
        ttk.Label(outer, textvariable=self.vars["session_status"]).pack(fill="x", pady=(5, 0))

        log_frame = ttk.LabelFrame(outer, text="运行日志与结果摘要", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.log = tk.Text(log_frame, height=14, wrap="word", state="disabled", font=("TkFixedFont", 10))
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _file_row(self, parent: ttk.LabelFrame, row: int, label: str, key: str) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(parent, textvariable=self.vars[key]).grid(row=row, column=1, sticky="ew")
        ttk.Button(parent, text="选择 WAV…", command=lambda: self._choose_wav(key)).grid(row=row, column=2, padx=(8, 0))
        parent.columnconfigure(1, weight=1)

    def _choose_wav(self, key: str) -> None:
        selected = filedialog.askopenfilename(title="选择 chirp WAV", filetypes=(("WAV 音频", "*.wav"), ("所有文件", "*")))
        if selected:
            self.vars[key].set(selected)

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(title="选择实验结果根目录")
        if selected:
            try:
                resolved = validate_output_root(Path(selected), create=True)
            except ValueError as exc:
                messagebox.showerror("结果目录不可用", str(exc))
                return
            self.vars["output_root"].set(str(resolved))
            self._save_preferences()

    def _load_preferences(self) -> dict[str, str]:
        try:
            loaded = json.loads(self.preferences_path.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_preferences(self) -> None:
        input_device = self.input_choices.get(self.vars["input"].get())
        output_device = self.output_choices.get(self.vars["output"].get())
        values = {
            "input_device": input_device.stable_name if input_device else "",
            "output_device": output_device.stable_name if output_device else "",
            "output_root": str(Path(self.vars["output_root"].get()).expanduser().resolve()),
        }
        try:
            self.preferences_path.parent.mkdir(parents=True, exist_ok=True)
            self.preferences_path.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
            self.preferences = values
        except OSError as exc:
            self._append(f"WARNING: 无法保存稳定设备选择：{exc}")

    @staticmethod
    def _label(device: AudioDeviceInfo) -> str:
        if device.alsa_stable_hw:
            alsa = f"{device.alsa_stable_hw} (current {device.alsa_hw})"
        else:
            alsa = device.alsa_hw or "logical route"
        runtime = device.portaudio_index if device.portaudio_index >= 0 else "not used/direct ALSA"
        return (
            f"{device.display_name} | {alsa} | in {device.max_input_channels} / "
            f"out {device.max_output_channels} | experiment 48000 Hz | runtime idx {runtime}"
        )

    @staticmethod
    def _match_choice(
        choices: dict[str, AudioDeviceInfo], selector: Any
    ) -> str | None:
        if selector is None or selector == "":
            return None
        for label, device in choices.items():
            if str(selector).casefold() in {
                device.stable_name.casefold(),
                str(device.portaudio_index).casefold(),
                (device.alsa_stable_hw or "").casefold(),
            }:
                return label
        return None

    def refresh_devices(self) -> None:
        try:
            previous_input = self.input_choices.get(self.vars["input"].get())
            previous_output = self.output_choices.get(self.vars["output"].get())
            self.devices = list_audio_devices()
            input_devices = [item for item in self.devices if item.is_input_candidate]
            output_devices = [item for item in self.devices if item.is_output_candidate]
            preferred_input = recommend_input(self.devices)
            preferred_output = recommend_output(self.devices)
            input_devices.sort(key=lambda item: (item != preferred_input, item.is_virtual, item.display_name))
            output_devices.sort(key=lambda item: (item != preferred_output, item.is_digital_output, item.is_virtual, item.is_uma8, item.display_name))
            self.input_choices = {self._label(item): item for item in input_devices}
            self.output_choices = {self._label(item): item for item in output_devices}
            self.input_combo["values"] = list(self.input_choices)
            self.output_combo["values"] = list(self.output_choices)
            persisted_input = self._match_choice(
                self.input_choices, self.preferences.get("input_device")
            )
            persisted_input_device = self.input_choices.get(persisted_input or "")
            safe_persisted_input = (
                persisted_input_device.stable_name
                if persisted_input_device is not None and persisted_input_device.is_uma8
                else None
            )
            input_selectors = (
                previous_input.stable_name if previous_input else None,
                self.defaults.get("input_device"),
                safe_persisted_input,
                preferred_input.stable_name if preferred_input else None,
            )
            persisted_output = self._match_choice(
                self.output_choices, self.preferences.get("output_device")
            )
            persisted_output_device = self.output_choices.get(persisted_output or "")
            safe_persisted_output = (
                persisted_output_device.stable_name
                if persisted_output_device is not None
                and not persisted_output_device.is_virtual
                and not persisted_output_device.is_digital_output
                and not persisted_output_device.is_uma8
                else None
            )
            output_selectors = (
                previous_output.stable_name if previous_output else None,
                self.defaults.get("output_device"),
                safe_persisted_output,
                preferred_output.stable_name if preferred_output else None,
            )
            input_label = next((found for selector in input_selectors if (found := self._match_choice(self.input_choices, selector))), None)
            output_label = next((found for selector in output_selectors if (found := self._match_choice(self.output_choices, selector))), None)
            self.vars["input"].set(input_label or "")
            self.vars["output"].set(output_label or "")
            self._save_preferences()
            self._append(
                f"设备重扫完成：{len(input_devices)} 个 8ch 输入、{len(output_devices)} 个 2ch 输出；"
                "选择按稳定 ALSA identity 恢复，runtime index 仅供调试"
            )
            if preferred_input:
                self._append(f"推荐输入：{preferred_input.display_name} ({preferred_input.alsa_stable_hw})")
            if preferred_output:
                self._append(f"推荐输出：{preferred_output.display_name} ({preferred_output.alsa_stable_hw})")
            if persisted_output_device is not None and safe_persisted_output is None:
                self._append(
                    f"已忽略旧的不安全输出偏好 {persisted_output_device.display_name}；"
                    "改用 ALC897 Analog 推荐项"
                )
            if persisted_input_device is not None and safe_persisted_input is None:
                self._append(
                    f"已忽略旧的非 UMA-8 输入偏好 {persisted_input_device.display_name}；"
                    "改用 micArray RAW SPK 推荐项"
                )
        except Exception as exc:
            self._append(f"设备枚举失败：{exc}")

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            input_device = self.input_choices[self.vars["input"].get()]
            output_device = self.output_choices[self.vars["output"].get()]
            if input_device.stable_name == output_device.stable_name:
                raise ValueError("输入和输出必须是不同的物理设备；UMA-8 不能同时承担播放")
            mode = self.vars["capture_mode"].get()
            cfg = ControllerConfig(
                c1=Path(self.vars["c1"].get()).expanduser(),
                c2=Path(self.vars["c2"].get()).expanduser(),
                # Stable ALSA identity is resolved to a fresh PortAudio index
                # immediately before opening each experiment.
                input_device=input_device.stable_name,
                output_device=output_device.stable_name,
                output_channel=int(self.vars["output_channel"].get()),
                playback_gain=float(self.vars["gain"].get()),
                udp_port=int(self.vars["udp_port"].get()),
                reply_timeout=float(self.vars["timeout"].get()),
                c1_threshold=float(self.vars["c1_threshold"].get()),
                c2_threshold=float(self.vars["c2_threshold"].get()),
                rir_method=self.vars["rir_method"].get(),
                output_root=Path(self.vars["output_root"].get()).expanduser(),
                capture_mode=mode,
                interval=float(self.vars["interval"].get()),
                max_measurements=int(self.vars["max_measurements"].get()),
                max_session_duration=float(self.vars["max_session_duration"].get()),
                android_host=self.vars["android_host"].get().strip() or None,
                overall_policy=self.vars["overall_policy"].get(),
            )
            cfg.validate()
            self.vars["output_root"].set(str(cfg.output_root))
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        if mode == "timed_continuous" and not messagebox.askyesno(
            "确认自动发声",
            f"将持续录音，并在 3 秒倒计时后自动播放 C1。\n"
            f"相邻实际声学 C1 的目标间隔：{cfg.interval:.3f} s\n"
            f"最大条数：{cfg.max_measurements or '不限'}；最大时长：{cfg.max_session_duration or '不限'} s\n\n"
            "确认扬声器方向和播放增益安全后继续。",
        ):
            return
        self.stop_event.clear()
        self._rir_seen = False
        self.rir_waveform = np.zeros((2, 8))
        self.wave_notebook.tab(1, text="C2 RIR（等待 C2）")
        self._update_rir_plot()
        self._save_preferences()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.capture_button.configure(state="disabled")
        self.pause_button.configure(state="normal" if mode == "timed_continuous" else "disabled")
        self.test_output_button.configure(state="disabled")
        self.vars["status"].set("运行中：请保持设备与环境稳定")

        common = dict(
            notify=lambda text: self.events.put(("log", text)),
            audio_block=self._on_audio_block,
            rir_preview=self._on_rir_preview,
            stop_event=self.stop_event,
        )
        if mode == "single":
            self.active_controller = Controller(cfg, **common)
        else:
            self.active_controller = ContinuousController(
                cfg, status=lambda value: self.events.put(("session_status", value)), **common
            )

        def work() -> None:
            try:
                assert self.active_controller is not None
                directory, result = self.active_controller.run()
                self.events.put(("done", (directory, result)))
            except Exception as exc:
                self.events.put(("error", exc))

        self.worker = threading.Thread(target=work, name="avtwin-controller", daemon=True)
        self.worker.start()

    def test_output(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            device = self.output_choices[self.vars["output"].get()]
        except KeyError:
            messagebox.showerror("输出设备", "请先选择输出设备")
            return
        self._save_preferences()
        self.test_output_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.vars["status"].set("正在测试所选输出：左声道后右声道…")
        for warning in output_warnings(device):
            self._append(warning)

        def work() -> None:
            try:
                play_safe_output_test(
                    device.stable_name,
                    notify=lambda text: self.events.put(("log", text)),
                )
                self.events.put(("test_done", device.display_name))
            except Exception as exc:
                self.events.put(("test_error", exc))

        threading.Thread(target=work, name="avtwin-output-test", daemon=True).start()

    def _on_audio_block(self, block: np.ndarray) -> None:
        now = time.monotonic()
        if now - self._last_audio_event < 0.08:
            return
        self._last_audio_event = now
        step = max(1, block.shape[0] // 400)
        self.events.put(("audio", (np.max(np.abs(block), axis=0), block[::step].copy(), step)))

    def _on_rir_preview(self, rirs: np.ndarray, final: bool) -> None:
        target = 700
        if rirs.shape[0] <= target:
            display = rirs.copy()
        else:
            edges = np.linspace(0, rirs.shape[0], target + 1, dtype=int)
            display = np.zeros((target, rirs.shape[1]), dtype=np.float32)
            for index in range(target):
                section = rirs[edges[index] : edges[index + 1]]
                if not section.size:
                    continue
                peaks = np.argmax(np.abs(section), axis=0)
                display[index] = section[peaks, np.arange(section.shape[1])]
        self.events.put(("rir", (display, final)))

    def stop(self) -> None:
        if isinstance(self.active_controller, ContinuousController):
            self.active_controller.stop()
        else:
            self.stop_event.set()
        self.vars["status"].set("正在安全停止并保存已有数据…")

    def capture_once(self) -> None:
        if isinstance(self.active_controller, ContinuousController):
            self.active_controller.request_capture()

    def toggle_pause(self) -> None:
        if not isinstance(self.active_controller, ContinuousController):
            return
        if self.active_controller.pause_event.is_set():
            self.active_controller.resume()
            self.pause_button.configure(text="暂停自动采集")
        else:
            self.active_controller.pause()
            self.pause_button.configure(text="继续自动采集")

    def _append(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _draw_levels(self) -> None:
        for ch, (canvas, value) in enumerate(zip(self.level_canvases, self.levels)):
            canvas.delete("all")
            width = int(90 * min(1.0, float(value)))
            color = "#e74c3c" if value >= 0.98 else ("#f1c40f" if value >= 0.7 else "#2ecc71")
            canvas.create_rectangle(1, 3, width, 29, fill=color, width=0)
            canvas.create_text(46, 16, text=("inactive" if value < 1e-8 else f"{value:.3f}"), fill="white")

    def _configure_empty_plots(self) -> None:
        self.live_axis.set(
            title="Live UMA-8 input - all channels",
            xlabel="Time in current block (ms)", ylabel="Raw amplitude",
        )
        self.live_axis.grid(True, alpha=0.25)
        for axis, title, limit in (
            (self.rir_all_axis, "C2 RIR - all UMA-8 channels", None),
            (self.rir_early_axis, "C2 RIR - first 50 ms", (0, 50)),
        ):
            axis.set(title=title, xlabel="Time (ms)", ylabel="RIR amplitude")
            if limit:
                axis.set_xlim(*limit)
            axis.grid(True, alpha=0.25)
        self.live_figure.tight_layout()
        self.rir_figure.tight_layout()

    def _update_live_plot(self) -> None:
        axis = self.live_axis
        axis.clear()
        time_ms = np.arange(self.waveform.shape[0]) * self.waveform_step * 1000.0 / SAMPLE_RATE
        for channel in range(min(8, self.waveform.shape[1])):
            axis.plot(time_ms, self.waveform[:, channel], linewidth=0.7, label=f"CH{channel}")
        axis.set(
            title="Live UMA-8 input - all channels (shared amplitude scale)",
            xlabel="Time in current block (ms)", ylabel="Raw amplitude",
        )
        axis.grid(True, alpha=0.25)
        axis.legend(ncol=4, fontsize=7, loc="upper right")
        self.live_figure.tight_layout()
        self.live_plot.draw_idle()

    def _update_rir_plot(self) -> None:
        all_axis, early_axis = self.rir_all_axis, self.rir_early_axis
        all_axis.clear()
        early_axis.clear()
        duration = float(self.defaults.get("rir_duration", 0.5))
        pre_arrival = float(self.defaults.get("rir_pre_arrival", 0.01))
        time_ms = np.linspace(
            -pre_arrival * 1000.0, (duration - pre_arrival) * 1000.0,
            self.rir_waveform.shape[0], endpoint=False,
        )
        for channel in range(min(8, self.rir_waveform.shape[1])):
            all_axis.plot(time_ms, self.rir_waveform[:, channel], linewidth=0.7, label=f"CH{channel}")
            early = time_ms <= 50.0
            early_axis.plot(time_ms[early], self.rir_waveform[early, channel], linewidth=0.8, label=f"CH{channel}")
        all_axis.set(
            title="C2 RIR - all UMA-8 channels (shared physical scale)",
            xlabel="Time relative to C2 arrival (ms)", ylabel="RIR amplitude",
            xlim=(-pre_arrival * 1000.0, max((duration - pre_arrival) * 1000.0, 1.0)),
        )
        early_axis.set(
            title="C2 RIR - pre-arrival and first 50 ms", xlabel="Time relative to C2 arrival (ms)",
            ylabel="RIR amplitude", xlim=(-pre_arrival * 1000.0, 50),
        )
        for axis in (all_axis, early_axis):
            axis.grid(True, alpha=0.25)
            axis.legend(ncol=4, fontsize=7, loc="upper right")
        self.rir_figure.tight_layout()
        self.rir_plot.draw_idle()

    def _poll(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self._append(str(value))
                elif kind == "audio":
                    self.levels = np.asarray(value[0])
                    self.waveform = np.asarray(value[1])
                    self.waveform_step = int(value[2])
                    self._update_live_plot()
                elif kind == "rir":
                    self.rir_waveform = np.asarray(value[0])
                    final = bool(value[1])
                    self._update_rir_plot()
                    self.wave_notebook.tab(1, text="C2 RIR（最终）" if final else "C2 RIR（实时预览）")
                    if not self._rir_seen:
                        self.wave_notebook.select(1)
                        self._rir_seen = True
                elif kind == "session_status":
                    latest = value.get("latest_quality") or {}
                    quality_text = latest.get("overall", "尚无质量结果")
                    countdown = value.get("next_trigger_seconds")
                    countdown_text = "--" if countdown is None else f"{countdown:.1f}s"
                    self.vars["session_status"].set(
                        f"状态 {value['state']} | measurement {value['measurement_id']} | "
                        f"成功 {value['success']} / 失败 {value['failure']} / 跳过 {value['skipped']} | "
                        f"下次 {countdown_text} | 最近质量 {quality_text}"
                    )
                    if self.vars["capture_mode"].get() == "manual_continuous":
                        self.capture_button.configure(
                            state="normal" if value["state"] == "ARMED" and not value["paused"] else "disabled"
                        )
                elif kind == "done":
                    directory, result = value
                    if "quality" in result:
                        overall = result["quality"]["overall"]
                        tof = result["tof"]
                        tof_text = f"距离 {tof['distance_m']:.4f} m" if tof["available"] else "Exact ToF: NOT AVAILABLE"
                    else:
                        overall = f"会话完成：成功 {result['success_count']} / 失败 {result['failure_count']} / 跳过 {result['skipped_count']}"
                        tof_text = "逐轮 ToF 见 measurements；无精确回复时为 NOT AVAILABLE"
                    self.vars["status"].set(f"{overall} — {tof_text} — {directory}")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.capture_button.configure(state="disabled")
                    self.pause_button.configure(state="disabled", text="暂停自动采集")
                    self.test_output_button.configure(state="normal")
                    self.active_controller = None
                elif kind == "error":
                    self._append(f"ERROR: {value}")
                    self.vars["status"].set("运行失败；详见日志")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.capture_button.configure(state="disabled")
                    self.pause_button.configure(state="disabled", text="暂停自动采集")
                    self.test_output_button.configure(state="normal")
                    self.active_controller = None
                    messagebox.showerror("运行失败", str(value))
                elif kind == "test_done":
                    self.vars["status"].set(f"测试输出完成：{value}")
                    self.test_output_button.configure(state="normal")
                    self.start_button.configure(state="normal")
                elif kind == "test_error":
                    self._append(f"测试输出失败：{value}")
                    self.vars["status"].set("测试输出失败；详见日志")
                    self.test_output_button.configure(state="normal")
                    self.start_button.configure(state="normal")
                    messagebox.showerror("测试输出失败", str(value))
        except queue.Empty:
            pass
        self._draw_levels()
        self.root.after(80, self._poll)

    def _close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("正在录音", "是否安全停止录音并退出？已有数据将尽量保存。"):
                return
            if isinstance(self.active_controller, ContinuousController):
                self.active_controller.stop()
            else:
                self.stop_event.set()
            self.vars["status"].set("正在安全停止、分析并保存，请稍候…")
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="disabled")
            self.root.after(100, self._finish_close)
            return
        self.root.destroy()

    def _finish_close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.root.after(100, self._finish_close)
        else:
            self.root.destroy()


def launch_gui(defaults: dict[str, Any] | None = None) -> None:
    root = tk.Tk()
    ControllerGui(root, defaults)
    root.mainloop()
