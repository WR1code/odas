from __future__ import annotations

import queue
import json
import os
from pathlib import Path
import subprocess
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
from .channel_selection import normalize_channels
from .config import SAMPLE_RATE, ControllerConfig
from .controller import Controller
from .continuous import ContinuousController
from .diagnostics import (
    test_probe_detector, test_probe_playback, test_udp_roundtrip,
    test_uma8_recording,
)
from .handshake import Role
from .network_info import format_network_status, network_snapshot
from .output_paths import validate_output_root
from .pose import (
    ManualPoseProvider, PoseProvider, UdpPoseProvider, parse_vector3, transform_offset,
)
from .role_session import HandshakeSession
from .udp_listener import UdpListener


class ControllerGui:
    def __init__(self, root: tk.Tk, defaults: dict[str, Any] | None = None):
        self.root = root
        self.root.title("AV-Twin Linux 声学握手控制器")
        self.root.minsize(1100, 920)
        self.defaults = defaults or {}
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.active_controller: Controller | ContinuousController | HandshakeSession | None = None
        self.live_pose_provider: PoseProvider | None = None
        self.idle_udp_listener: UdpListener | None = None
        self._udp_test_running = False
        self.devices: list[AudioDeviceInfo] = []
        self.input_choices: dict[str, AudioDeviceInfo] = {}
        self.output_choices: dict[str, AudioDeviceInfo] = {}
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.preferences_path = config_home / "avtwin-linux" / "gui.json"
        self.preferences = self._load_preferences()
        selected_channels = set(normalize_channels(self.preferences.get("waveform_channels")))
        self.waveform_channel_vars = [
            tk.BooleanVar(value=channel in selected_channels) for channel in range(8)
        ]
        probe_root = Path(__file__).resolve().parent.parent / "wav"
        default_c1 = probe_root / "c1_mono.wav"
        default_c2 = probe_root / "c2_mono.wav"
        pose_forced_by_launcher = self.defaults.get("pose_source") in {"udp", "manual"}

        def pose_setting(name: str, fallback: Any) -> Any:
            if pose_forced_by_launcher:
                return self.defaults.get(name, fallback)
            return self.preferences.get(name) or self.defaults.get(name, fallback)

        try:
            manual_position = parse_vector3(pose_setting("manual_position", "0,0,0"))
        except ValueError:
            manual_position = (0.0, 0.0, 0.0)

        self.levels = np.zeros(8)
        self.waveform = np.zeros((2, 8))
        self.waveform_step = 1
        self.rir_waveform = np.zeros((2, 8))
        self._rir_seen = False
        self._last_audio_event = 0.0
        self._next_network_refresh = 0.0
        self._network_refresh_running = False
        self._network_snapshot: dict[str, Any] = {}
        self.vars = {
            "c1": tk.StringVar(value=str(self.defaults.get("c1") or (default_c1 if default_c1.is_file() else ""))),
            "c2": tk.StringVar(value=str(self.defaults.get("c2") or (default_c2 if default_c2.is_file() else ""))),
            "input": tk.StringVar(),
            "output": tk.StringVar(),
            "output_channel": tk.StringVar(value=str(self.defaults.get("output_channel", 1))),
            "role": tk.StringVar(value=str(self.defaults.get("role", "initiator"))),
            "debug": tk.BooleanVar(value=bool(self.defaults.get("debug", False))),
            "pose_source": tk.StringVar(value=str(pose_setting("pose_source", "disabled"))),
            "pose_udp_host": tk.StringVar(value=str(pose_setting("pose_udp_host", "0.0.0.0"))),
            "pose_udp_port": tk.StringVar(value=str(pose_setting("pose_udp_port", 5006))),
            "pose_max_age": tk.StringVar(value=str(pose_setting("pose_max_age", 0.25))),
            "manual_x": tk.StringVar(value=str(manual_position[0])),
            "manual_y": tk.StringVar(value=str(manual_position[1])),
            "manual_z": tk.StringVar(value=str(manual_position[2])),
            "speaker_offset": tk.StringVar(value=str(pose_setting("speaker_offset", "0,0,0"))),
            "microphone_offset": tk.StringVar(value=str(pose_setting("microphone_offset", "0,0,0"))),
            "gain": tk.StringVar(value=str(self.defaults.get("playback_gain", 1.0))),
            "udp_port": tk.StringVar(value=str(self.defaults.get("udp_port", 5005))),
            "android_port": tk.StringVar(value=str(
                self.preferences.get("android_port")
                or self.defaults.get("android_port", 5006)
            )),
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
            "pose_status": tk.StringVar(value="等待定位数据或应用手动当前坐标…"),
            "event_pose_status": tk.StringVar(value="本次采集冻结坐标：尚无完成的声学事件"),
            "udp_test_status": tk.StringVar(value="UDP 双向检验：尚未测试"),
            "network_status": tk.StringVar(value="Linux 本机 IPv4：正在读取…"),
        }
        self._build()
        try:
            self._ensure_live_pose_provider()
        except (OSError, RuntimeError, ValueError) as exc:
            self.vars["pose_status"].set(f"位姿接口启动失败：{exc}")
        self._ensure_idle_udp_listener()
        self.refresh_devices()
        self.root.after(250, self._restore_pane_sizes)
        self.root.after(80, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _add_scrollable_module(
        self, title: str, *, padding: int = 8, minsize: int = 60,
    ) -> ttk.Frame:
        """Add one independently scrollable pane and return its content frame."""
        shell = ttk.LabelFrame(self.main_panes, text=title, padding=(4, 2))
        shell.grid_rowconfigure(0, weight=1)
        shell.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(shell, borderwidth=0, highlightthickness=0, takefocus=False)
        horizontal = ttk.Scrollbar(shell, orient="horizontal", command=canvas.xview)
        vertical = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        canvas.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")

        content = ttk.Frame(canvas, padding=padding)
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")

        syncing = False

        def sync_scroll_region(_event: Any = None) -> None:
            nonlocal syncing
            if syncing:
                return
            syncing = True
            try:
                requested_width = content.winfo_reqwidth()
                viewport_width = canvas.winfo_width()
                canvas.itemconfigure(content_window, width=max(requested_width, viewport_width))
                canvas.configure(scrollregion=canvas.bbox("all"))
            finally:
                syncing = False

        content.bind("<Configure>", sync_scroll_region)
        canvas.bind("<Configure>", sync_scroll_region)
        self.main_panes.add(shell, minsize=max(60, minsize), stretch="always")

        if not hasattr(self, "_module_scrollables"):
            self._module_scrollables: list[tuple[tk.Canvas, ttk.Frame]] = []
        self._module_scrollables.append((canvas, content))
        if not hasattr(self, "_module_scroll_syncs"):
            self._module_scroll_syncs: list[Any] = []
        self._module_scroll_syncs.append(sync_scroll_region)
        return content

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text="提示：拖动模块间分隔条调整高度；每个模块底部/右侧滑轨可查看被遮挡内容",
        ).pack(fill="x")
        self.main_panes = tk.PanedWindow(
            outer, orient=tk.VERTICAL, sashwidth=8, sashrelief=tk.RAISED,
            borderwidth=0, showhandle=True,
        )
        self.main_panes.pack(fill="both", expand=True, pady=(4, 0))
        self.main_panes.bind("<ButtonRelease-1>", lambda _event: self._save_preferences())

        files = self._add_scrollable_module("Chirp 音频（主动选择）", padding=10)
        role_frame = self._add_scrollable_module("AV-Twin Role", padding=10)
        role_controls = ttk.Frame(role_frame)
        role_controls.pack(fill="x")
        ttk.Label(role_controls, text="Role").pack(side="left", padx=(0, 8))
        self.role_combo = ttk.Combobox(
            role_controls, textvariable=self.vars["role"], state="readonly", width=64,
            values=(Role.INITIATOR.display_name, Role.RESPONDER.display_name),
        )
        role_value = self.vars["role"].get().lower()
        self.vars["role"].set(Role.RESPONDER.display_name if "responder" in role_value else Role.INITIATOR.display_name)
        self.role_combo.pack(side="left", fill="x", expand=True)
        self.role_flow = ttk.Label(role_frame, text="持续录音 → C1 PLAY → WAIT C2 → C2 remote RIR")
        self.role_flow.pack(anchor="w", padx=(41, 0), pady=(5, 0))
        self.role_combo.bind("<<ComboboxSelected>>", self._role_changed)
        ttk.Checkbutton(role_controls, text="Debug Mode", variable=self.vars["debug"]).pack(side="left", padx=(12, 0))
        self._file_row(files, 0, "C1 probe chirp reference", "c1")
        self._file_row(files, 1, "C2 probe chirp reference", "c2")

        device_frame = self._add_scrollable_module("音频设备（输入与输出完全独立）", padding=10)
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

        pose_frame = self._add_scrollable_module(
            "坐标模式：MID-360S 实时读取 / 手动输入当前坐标", padding=8, minsize=72,
        )
        pose_source_row = ttk.Frame(pose_frame)
        pose_source_row.pack(anchor="w", fill="x")
        ttk.Label(pose_source_row, text="位姿来源").pack(side="left")
        self.pose_source_combo = ttk.Combobox(
            pose_source_row, textvariable=self.vars["pose_source"],
            values=("udp", "manual", "disabled"), state="readonly", width=10,
        )
        self.pose_source_combo.pack(side="left", padx=(5, 14))
        self.pose_source_combo.bind("<<ComboboxSelected>>", self._pose_source_changed)
        ttk.Label(pose_source_row, text="UDP listen").pack(side="left")
        ttk.Entry(pose_source_row, textvariable=self.vars["pose_udp_host"], width=13).pack(side="left", padx=(5, 4))
        ttk.Entry(pose_source_row, textvariable=self.vars["pose_udp_port"], width=7).pack(side="left", padx=(0, 14))
        ttk.Label(pose_source_row, text="最大位姿时差(s)").pack(side="left")
        ttk.Entry(pose_source_row, textvariable=self.vars["pose_max_age"], width=7).pack(side="left", padx=(5, 14))
        self.reset_pose_button = ttk.Button(
            pose_source_row, text="重置零点", command=self.reset_pose_origin,
        )
        self.reset_pose_button.pack(side="left")
        ttk.Label(
            pose_source_row, text="udp=MID-360S 实时坐标；manual=手动当前坐标",
        ).pack(side="left", padx=(12, 0))

        manual_row = ttk.Frame(pose_frame)
        manual_row.pack(anchor="w", fill="x", pady=(6, 0))
        ttk.Label(manual_row, text="手动当前坐标 x,y,z(m)").pack(side="left")
        self.manual_position_entries: list[ttk.Entry] = []
        for key, axis in (("manual_x", "X"), ("manual_y", "Y"), ("manual_z", "Z")):
            box = ttk.Frame(manual_row)
            box.pack(side="left", padx=(5, 4))
            ttk.Label(box, text=axis).pack(side="left")
            entry = ttk.Entry(box, textvariable=self.vars[key], width=9)
            entry.pack(side="left", padx=(2, 0))
            entry.bind("<Return>", lambda _event: self.apply_manual_position())
            self.manual_position_entries.append(entry)
        self.apply_manual_position_button = ttk.Button(
            manual_row, text="应用手动坐标", command=self.apply_manual_position,
        )
        self.apply_manual_position_button.pack(side="left", padx=(6, 14))

        offsets_row = ttk.Frame(pose_frame)
        offsets_row.pack(anchor="w", fill="x", pady=(6, 0))
        ttk.Label(offsets_row, text="雷达/手动点→扬声器 x,y,z(m)").pack(side="left")
        ttk.Entry(offsets_row, textvariable=self.vars["speaker_offset"], width=20).pack(side="left", padx=(5, 14))
        ttk.Label(offsets_row, text="雷达/手动点→UMA-8中心 x,y,z(m)").pack(side="left")
        ttk.Entry(offsets_row, textvariable=self.vars["microphone_offset"], width=20).pack(side="left", padx=(5, 14))
        ttk.Label(
            offsets_row, text="外参均在定位点坐标系；UDP JSON 协议 AVTWIN_POSE_V1",
        ).pack(side="left")

        pose_status_row = ttk.Frame(pose_frame)
        pose_status_row.pack(anchor="w", pady=(6, 0))
        ttk.Label(pose_status_row, text="最新当前坐标：", width=16, anchor="w").pack(side="left")
        ttk.Label(pose_status_row, textvariable=self.vars["pose_status"]).pack(side="left")
        ttk.Label(
            pose_frame, textvariable=self.vars["event_pose_status"],
        ).pack(anchor="w", pady=(6, 0))
        ttk.Label(
            pose_frame,
            text="说明：事件坐标在每次采集的精确 t1/t4（Initiator）或 t2/t3（Responder）冻结；单次写入 metadata.json，连续采集写入该轮 result.json。",
        ).pack(anchor="w", pady=(4, 0))

        params = self._add_scrollable_module("实验参数", padding=10, minsize=72)
        fields = [
            ("输出声道 (LEFT/RIGHT/BOTH)", "output_channel"), ("播放增益", "gain"),
            ("Linux 本机监听/结果端口", "udp_port"), ("Android/远端监听端口", "android_port"),
            ("C2 超时 (s)", "timeout"),
            ("C1 阈值", "c1_threshold"), ("C2 阈值", "c2_threshold"),
            ("自动间隔 (s)", "interval"), ("最大条数 (0=不限)", "max_measurements"),
            ("最大时长 (s, 0=不限)", "max_session_duration"), ("远端 IP (可空)", "android_host"),
        ]
        for idx, (label, key) in enumerate(fields):
            col = (idx % 3) * 2
            row = idx // 3
            ttk.Label(params, text=label).grid(row=row, column=col, sticky="w", padx=(0, 5), pady=3)
            ttk.Entry(params, textvariable=self.vars[key], width=12).grid(row=row, column=col + 1, sticky="w", padx=(0, 18))
        self.vars["output_channel"].set(
            {"0": "LEFT", "1": "RIGHT"}.get(self.vars["output_channel"].get().upper(), self.vars["output_channel"].get().upper())
        )
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
        ttk.Label(
            params, textvariable=self.vars["network_status"], wraplength=1050,
            font=("TkDefaultFont", 10, "bold"),
        ).grid(row=6, column=0, columnspan=6, sticky="w", pady=(8, 0))

        monitor = self._add_scrollable_module("实时 8 通道输入电平与波形", padding=8, minsize=110)
        self.level_canvases: list[tk.Canvas] = []
        for ch in range(8):
            ttk.Label(monitor, text=f"CH{ch}").grid(row=0, column=ch, padx=4)
            canvas = tk.Canvas(monitor, width=92, height=32, bg="#17202a", highlightthickness=0)
            canvas.grid(row=1, column=ch, padx=3)
            self.level_canvases.append(canvas)
            monitor.columnconfigure(ch, weight=1)
        channel_selector = ttk.Frame(monitor)
        channel_selector.grid(row=2, column=0, columnspan=8, sticky="ew", pady=(7, 0))
        ttk.Label(channel_selector, text="波形显示通道：").pack(side="left", padx=(2, 5))
        for channel, variable in enumerate(self.waveform_channel_vars):
            ttk.Checkbutton(
                channel_selector, text=f"CH{channel}", variable=variable,
                command=self._waveform_channels_changed,
            ).pack(side="left", padx=3)
        ttk.Button(
            channel_selector, text="全选", command=lambda: self._set_waveform_channels(True),
        ).pack(side="left", padx=(12, 3))
        ttk.Button(
            channel_selector, text="全不选", command=lambda: self._set_waveform_channels(False),
        ).pack(side="left", padx=3)
        self.wave_notebook = ttk.Notebook(monitor)
        self.wave_notebook.grid(row=3, column=0, columnspan=8, sticky="ew", pady=(8, 0))
        live_frame = ttk.Frame(self.wave_notebook)
        rir_frame = ttk.Frame(self.wave_notebook)
        self.wave_notebook.add(live_frame, text="实时输入波形")
        self.wave_notebook.add(rir_frame, text="C2 remote RIR（等待 C2）")
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

        session_controls = self._add_scrollable_module("声学会话控制", padding=6)
        actions = ttk.Frame(session_controls)
        actions.pack(fill="x")
        self.start_button = ttk.Button(actions, text="开始会话", command=self.start)
        self.start_button.pack(side="left")
        self.capture_button = ttk.Button(actions, text="采集一次", command=self.capture_once, state="disabled")
        self.capture_button.pack(side="left", padx=(8, 0))
        self.pause_button = ttk.Button(actions, text="暂停自动采集", command=self.toggle_pause, state="disabled")
        self.pause_button.pack(side="left", padx=(8, 0))
        self.stop_button = ttk.Button(actions, text="安全停止并保存", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Label(actions, textvariable=self.vars["status"]).pack(side="left", padx=16)
        ttk.Label(session_controls, textvariable=self.vars["session_status"]).pack(fill="x", pady=(5, 0))

        debug_actions = self._add_scrollable_module(
            "Debug Mode — 独立测试（不启动 handshake）", padding=6,
        )
        debug_button_row = ttk.Frame(debug_actions)
        debug_button_row.pack(fill="x")
        for label, command in (
            ("Test C1 Playback", lambda: self.run_diagnostic("play_c1")),
            ("Test C2 Playback", lambda: self.run_diagnostic("play_c2")),
            ("Test C1 Detector", lambda: self.run_diagnostic("detect_c1")),
            ("Test C2 Detector", lambda: self.run_diagnostic("detect_c2")),
            ("Test UMA-8 Recording", lambda: self.run_diagnostic("record")),
        ):
            ttk.Button(debug_button_row, text=label, command=command).pack(side="left", padx=4)
        self.udp_test_button = ttk.Button(
            debug_button_row, text="Test UDP Roundtrip", command=self.test_udp,
        )
        self.udp_test_button.pack(side="left", padx=4)
        ttk.Label(
            debug_actions, textvariable=self.vars["udp_test_status"], wraplength=1050,
        ).pack(fill="x", padx=4, pady=(5, 0))

        log_frame = self._add_scrollable_module("运行日志与结果摘要", padding=6, minsize=80)
        self.log = tk.Text(log_frame, height=14, wrap="word", state="disabled", font=("TkFixedFont", 10))
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _selected_role(self) -> str:
        return "responder" if self.vars["role"].get().startswith("Responder") else "initiator"

    def _update_pose_status(self, value: dict[str, Any]) -> None:
        def position(label: str, key: str) -> str:
            pose = value.get(key)
            if not pose:
                return f"{label}=--"
            xyz = pose["position_m"]
            return f"{label}=({xyz[0]:.3f},{xyz[1]:.3f},{xyz[2]:.3f})m"

        radar = value.get("radar_pose") or {}
        frame = radar.get("frame_id", "--")
        tracking = radar.get("tracking_status", "--")
        timestamp_ns = radar.get("timestamp_ns")
        age = "--" if timestamp_ns is None else f"{max(0.0, (time.monotonic_ns() - int(timestamp_ns)) / 1e6):.0f}ms"
        warning = value.get("pose_warning")
        warning_text = "" if not warning else f" | 警告={warning}"
        self.vars["pose_status"].set(
            f"frame={frame} tracking={tracking} age={age} | " + " | ".join((
                position("Radar", "radar_pose"),
                position("Speaker", "speaker_pose"),
                position("UMA-8", "microphone_pose"),
            )) + warning_text
        )

    def _pose_source_changed(self, _event: Any = None) -> None:
        try:
            self._ensure_live_pose_provider(force_restart=True)
            self._save_preferences()
        except (OSError, RuntimeError, ValueError) as exc:
            self.vars["pose_status"].set(f"位姿接口启动失败：{exc}")

    def _manual_position(self) -> tuple[float, float, float]:
        return parse_vector3((
            self.vars["manual_x"].get(),
            self.vars["manual_y"].get(),
            self.vars["manual_z"].get(),
        ))

    def apply_manual_position(self) -> None:
        try:
            position = self._manual_position()
        except ValueError as exc:
            messagebox.showerror("手动坐标无效", str(exc))
            return
        if self.worker and self.worker.is_alive() and self.vars["pose_source"].get() != "manual":
            messagebox.showwarning("不能切换坐标模式", "请先结束当前采集，再切换到手动坐标模式。")
            return
        if self.vars["pose_source"].get() != "manual":
            self.vars["pose_source"].set("manual")
            self._ensure_live_pose_provider(force_restart=True)
        else:
            provider = self.live_pose_provider
            if isinstance(provider, ManualPoseProvider):
                provider.update(position)
            else:
                self._ensure_live_pose_provider(force_restart=True)
        self._save_preferences()
        self.vars["pose_status"].set(
            f"frame=manual_world tracking=MANUAL | Radar=({position[0]:.3f},{position[1]:.3f},{position[2]:.3f})m"
        )
        self._append(
            f"MANUAL POSITION APPLIED：({position[0]:.3f}, {position[1]:.3f}, {position[2]:.3f}) m"
        )

    def _stop_live_pose_provider(self) -> None:
        provider = self.live_pose_provider
        self.live_pose_provider = None
        if provider is not None:
            provider.stop()

    def reset_pose_origin(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(
                "不能重置零点", "请先结束当前声学采集，避免同一轮事件使用两个坐标原点。",
            )
            return
        provider = self.live_pose_provider
        if provider is None:
            messagebox.showwarning("不能重置零点", "位姿来源尚未启用。")
            return
        if isinstance(provider, ManualPoseProvider):
            for key in ("manual_x", "manual_y", "manual_z"):
                self.vars[key].set("0.0")
            provider.reset_origin()
            self._save_preferences()
            self.vars["pose_status"].set("手动当前坐标已重置为 (0.000,0.000,0.000)m")
            self._append("MANUAL POSITION RESET：当前手动坐标已设为 (0,0,0)")
            return
        provider.reset_origin()
        self.vars["pose_status"].set("零点已重置：等待下一帧作为新的 (0,0,0)…")
        self._append("MID-360S ZERO RESET：下一帧位姿将成为新的相对坐标原点")

    def _ensure_live_pose_provider(self, *, force_restart: bool = False) -> None:
        source = self.vars["pose_source"].get()
        if source == "disabled":
            self._stop_live_pose_provider()
            self.vars["pose_status"].set("位姿来源已禁用")
            return
        if source == "manual":
            position = self._manual_position()
            current = self.live_pose_provider
            if not force_restart and isinstance(current, ManualPoseProvider):
                current.update(position)
            else:
                self._stop_live_pose_provider()
                current = ManualPoseProvider(position)
                current.start()
                self.live_pose_provider = current
            self.vars["pose_status"].set(
                f"手动坐标模式：当前 ({position[0]:.3f},{position[1]:.3f},{position[2]:.3f})m"
            )
            return
        if source != "udp":
            raise ValueError(f"不支持的位姿来源：{source}")
        host = self.vars["pose_udp_host"].get().strip()
        port = int(self.vars["pose_udp_port"].get())
        max_age = float(self.vars["pose_max_age"].get())
        if not host:
            raise ValueError("位姿 UDP 监听地址不能为空")
        if not 1 <= port <= 65535:
            raise ValueError("位姿 UDP 端口必须在 1..65535 内")
        if max_age <= 0:
            raise ValueError("最大位姿时差必须大于 0")
        current = self.live_pose_provider
        if (
            not force_restart and isinstance(current, UdpPoseProvider)
            and current.host == host and current.port == port
        ):
            current.max_age_ns = round(max_age * 1e9)
            return
        self._stop_live_pose_provider()
        provider = UdpPoseProvider(host, port, max_age)
        provider.start()
        self.live_pose_provider = provider
        self.vars["pose_status"].set(f"等待 MID-360S 位姿：udp://{host}:{port}")

    def _update_event_pose_status(self, events: dict[str, Any] | None) -> None:
        if not events:
            return

        def xyz(pose: dict[str, Any] | None) -> str:
            if not pose:
                return "--"
            value = pose["position_m"]
            return f"({value[0]:.3f},{value[1]:.3f},{value[2]:.3f})m"

        parts: list[str] = []
        for name, event in events.items():
            if event.get("available"):
                frame = event.get("radar_pose", {}).get("frame_id", "map")
                parts.append(
                    f"{name}[{frame}] Radar={xyz(event.get('radar_pose'))}, "
                    f"Speaker={xyz(event.get('speaker_pose'))}, UMA-8={xyz(event.get('microphone_pose'))}"
                )
            else:
                parts.append(f"{name}=不可用({event.get('reason', 'unknown')})")
        self.vars["event_pose_status"].set("本次采集冻结坐标：" + "；".join(parts))

    def _role_changed(self, _event: Any = None) -> None:
        responder = self._selected_role() == "responder"
        if responder:
            self.vars["capture_mode"].set("single")
            self.role_flow.configure(text="持续录音 → LISTEN C1 → C1 DETECTED → C2 IMMEDIATE RESPONSE → C1 remote RIR")
            self.wave_notebook.tab(1, text="C1 remote RIR（等待 C1）")
        else:
            self.role_flow.configure(text="持续录音 → C1 PLAY → WAIT C2 → C2 DETECTED → C2 remote RIR")
            self.wave_notebook.tab(1, text="C2 remote RIR（等待 C2）")
        self._configure_empty_plots()
        self._update_rir_plot()

    def _file_row(self, parent: ttk.Frame, row: int, label: str, key: str) -> None:
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
            "pose_source": self.vars["pose_source"].get(),
            "pose_udp_host": self.vars["pose_udp_host"].get(),
            "pose_udp_port": self.vars["pose_udp_port"].get(),
            "pose_max_age": self.vars["pose_max_age"].get(),
            "manual_position": [
                self.vars["manual_x"].get(),
                self.vars["manual_y"].get(),
                self.vars["manual_z"].get(),
            ],
            "speaker_offset": self.vars["speaker_offset"].get(),
            "microphone_offset": self.vars["microphone_offset"].get(),
            "android_port": self.vars["android_port"].get(),
            "waveform_channels": list(self._selected_waveform_channels()),
            "pane_sashes": self._current_pane_sizes(),
        }
        try:
            self.preferences_path.parent.mkdir(parents=True, exist_ok=True)
            self.preferences_path.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
            self.preferences = values
        except OSError as exc:
            self._append(f"WARNING: 无法保存稳定设备选择：{exc}")

    def _current_pane_sizes(self) -> list[int]:
        previous = self.preferences.get("pane_sashes")
        fallback = list(previous) if isinstance(previous, list) else []
        panes = self.main_panes.panes() if hasattr(self, "main_panes") else ()
        if len(panes) < 2 or self.main_panes.winfo_height() < 100:
            return fallback
        try:
            positions = [self.main_panes.sash_coord(index)[1] for index in range(len(panes) - 1)]
        except tk.TclError:
            return fallback
        return positions if all(value > 0 for value in positions) else fallback

    def _restore_pane_sizes(self) -> None:
        positions = self.preferences.get("pane_sashes")
        if not isinstance(positions, list):
            return
        self.root.update_idletasks()
        pane_count = len(self.main_panes.panes())
        height = self.main_panes.winfo_height()
        if pane_count < 2 or height < 100:
            return
        for index, value in enumerate(positions[:pane_count - 1]):
            try:
                y = max(20, min(int(value), height - 20))
                self.main_panes.sash_place(index, 0, y)
            except (TypeError, ValueError, tk.TclError):
                continue

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
            role = self._selected_role()
            if role == "responder" and mode != "single":
                raise ValueError("Responder 使用 single 会话：等待一个 C1 并立即发送一个 C2")
            cfg = ControllerConfig(
                c1=Path(self.vars["c1"].get()).expanduser(),
                c2=Path(self.vars["c2"].get()).expanduser(),
                # Stable ALSA identity is resolved to a fresh PortAudio index
                # immediately before opening each experiment.
                input_device=input_device.stable_name,
                output_device=output_device.stable_name,
                output_channel=self.vars["output_channel"].get(),
                role=role,
                debug=bool(self.vars["debug"].get()),
                pose_source=self.vars["pose_source"].get(),
                pose_udp_host=self.vars["pose_udp_host"].get().strip(),
                pose_udp_port=int(self.vars["pose_udp_port"].get()),
                pose_max_age=float(self.vars["pose_max_age"].get()),
                manual_position_m=self._manual_position(),
                speaker_offset_m=self.vars["speaker_offset"].get(),
                microphone_offset_m=self.vars["microphone_offset"].get(),
                playback_gain=float(self.vars["gain"].get()),
                udp_port=int(self.vars["udp_port"].get()),
                android_port=int(self.vars["android_port"].get()),
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
            self._ensure_live_pose_provider()
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
        # The idle diagnostic responder and the acoustic session intentionally
        # share the configured Linux result port. Hand it over without overlap.
        self._stop_idle_udp_listener()
        self.stop_event.clear()
        self._rir_seen = False
        self.vars["event_pose_status"].set("本次采集冻结坐标：正在等待精确声学事件")
        self.rir_waveform = np.zeros((2, 8))
        probe_label = "C1 remote RIR" if cfg.role == "responder" else "C2 remote RIR"
        self.wave_notebook.tab(1, text=f"{probe_label}（等待）")
        self._update_rir_plot()
        self._save_preferences()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.capture_button.configure(state="disabled")
        self.pause_button.configure(state="normal" if mode == "timed_continuous" else "disabled")
        self.test_output_button.configure(state="disabled")
        self.pose_source_combo.configure(state="disabled")
        self.reset_pose_button.configure(state="disabled")
        self.vars["status"].set("运行中：请保持设备与环境稳定")

        common = dict(
            notify=lambda text: self.events.put(("log", text)),
            audio_block=self._on_audio_block,
            rir_preview=self._on_rir_preview,
            stop_event=self.stop_event,
        )
        if mode == "single":
            self.active_controller = HandshakeSession(
                cfg, status=lambda value: self.events.put(("role_status", value)),
                pose_provider=self.live_pose_provider, **common
            )
        else:
            self.active_controller = ContinuousController(
                cfg, status=lambda value: self.events.put(("session_status", value)),
                pose_provider=self.live_pose_provider, **common
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

    def _stop_idle_udp_listener(self) -> None:
        listener = self.idle_udp_listener
        self.idle_udp_listener = None
        if listener is not None:
            listener.stop()

    def _ensure_idle_udp_listener(self) -> None:
        if (self.worker and self.worker.is_alive()) or self._udp_test_running:
            self._stop_idle_udp_listener()
            return
        try:
            port = int(self.vars["udp_port"].get())
        except ValueError:
            self._stop_idle_udp_listener()
            return
        if not 1 <= port <= 65535:
            self._stop_idle_udp_listener()
            return
        current = self.idle_udp_listener
        if (
            current is not None and current.host == "0.0.0.0"
            and current.port == port and current.is_running
        ):
            return
        self._stop_idle_udp_listener()
        listener = UdpListener(
            "0.0.0.0", port,
            notify=lambda message: self.events.put(("idle_udp", message)),
        )
        listener.start()
        if listener.error:
            self.events.put(("idle_udp_error", listener.error))
            listener.stop()
            return
        self.idle_udp_listener = listener
        if "尚未测试" in self.vars["udp_test_status"].get():
            self.vars["udp_test_status"].set(
                f"Linux 常驻 UDP 测试监听已就绪：所有本机IP:{port}（Android 可直接发起检验）"
            )

    def test_udp(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showerror("UDP 双向检验", "完整握手运行时不能占用同一个本机 UDP 端口")
            return
        try:
            remote_host = self.vars["android_host"].get().strip()
            local_port = int(self.vars["udp_port"].get())
            remote_port = int(self.vars["android_port"].get())
            if not remote_host:
                raise ValueError("请先填写远端 IP")
            if not 0 < local_port <= 65535 or not 0 < remote_port <= 65535:
                raise ValueError("UDP 端口必须在 1..65535 内")
        except ValueError as exc:
            messagebox.showerror("UDP 双向检验参数错误", str(exc))
            return
        self._udp_test_running = True
        self._stop_idle_udp_listener()
        self._save_preferences()
        self.udp_test_button.configure(state="disabled")
        snapshot = network_snapshot(remote_host)
        source_ip = snapshot.get("source_ip") or "路由未确定"
        self.vars["udp_test_status"].set(
            f"测试中：Linux {source_ip}:{local_port} ↔ Android {remote_host}:{remote_port}"
        )

        def work() -> None:
            try:
                result = test_udp_roundtrip(
                    "0.0.0.0", local_port, remote_host, remote_port, timeout=2.0,
                )
                self.events.put(("udp_test_done", result))
            except Exception as exc:
                self.events.put(("udp_test_error", exc))

        threading.Thread(target=work, name="avtwin-udp-roundtrip-test", daemon=True).start()

    def run_diagnostic(self, kind: str) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            input_device = self.input_choices[self.vars["input"].get()]
            output_device = self.output_choices[self.vars["output"].get()]
            output_channel: int | str = self.vars["output_channel"].get().strip().lower()
            if output_channel in {"left", "0"}:
                output_channel = 0
            elif output_channel in {"right", "1"}:
                output_channel = 1
            elif output_channel != "both":
                raise ValueError("输出声道必须是 LEFT、RIGHT 或 BOTH")
            gain = float(self.vars["gain"].get())
        except Exception as exc:
            messagebox.showerror("调试参数错误", str(exc))
            return
        self.vars["status"].set(f"Debug: {kind} 正在运行…")

        def work() -> None:
            try:
                if kind.startswith("play_"):
                    key = "c1" if kind.endswith("c1") else "c2"
                    result = test_probe_playback(
                        Path(self.vars[key].get()).expanduser(), output_device.stable_name,
                        output_channel, gain,
                    )
                elif kind.startswith("detect_"):
                    key = "c1" if kind.endswith("c1") else "c2"
                    result = test_probe_detector(
                        Path(self.vars[key].get()).expanduser(), input_device.stable_name,
                        float(self.vars[f"{key}_threshold"].get()), 2,
                        audio_block=self._on_audio_block,
                    )
                else:
                    _recording, result = test_uma8_recording(
                        input_device.stable_name, audio_block=self._on_audio_block,
                    )
                self.events.put(("diagnostic_done", (kind, result)))
            except Exception as exc:
                self.events.put(("diagnostic_error", (kind, exc)))

        threading.Thread(target=work, name=f"avtwin-debug-{kind}", daemon=True).start()

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
        channels = self._selected_waveform_channels()
        selection = ", ".join(f"CH{channel}" for channel in channels) or "未选择通道"
        self.live_axis.set(
            title=f"Live UMA-8 input - {selection}",
            xlabel="Time in current block (ms)", ylabel="Raw amplitude",
        )
        self.live_axis.grid(True, alpha=0.25)
        probe = "C1 remote RIR" if self._selected_role() == "responder" else "C2 remote RIR"
        for axis, title, limit in (
            (self.rir_all_axis, f"{probe} - {selection}", None),
            (self.rir_early_axis, f"{probe} - first 50 ms", (0, 50)),
        ):
            axis.set(title=title, xlabel="Time (ms)", ylabel="RIR amplitude")
            if limit:
                axis.set_xlim(*limit)
            axis.grid(True, alpha=0.25)
        self.live_figure.tight_layout()
        self.rir_figure.tight_layout()

    def _selected_waveform_channels(self, available: int = 8) -> tuple[int, ...]:
        return tuple(
            channel for channel, variable in enumerate(self.waveform_channel_vars)
            if channel < available and variable.get()
        )

    def _set_waveform_channels(self, selected: bool) -> None:
        for variable in self.waveform_channel_vars:
            variable.set(selected)
        self._waveform_channels_changed()

    def _waveform_channels_changed(self) -> None:
        self._save_preferences()
        self._update_live_plot()
        self._update_rir_plot()

    def _update_live_plot(self) -> None:
        axis = self.live_axis
        axis.clear()
        time_ms = np.arange(self.waveform.shape[0]) * self.waveform_step * 1000.0 / SAMPLE_RATE
        channels = self._selected_waveform_channels(min(8, self.waveform.shape[1]))
        for channel in channels:
            axis.plot(
                time_ms, self.waveform[:, channel], linewidth=0.7,
                color=f"C{channel}", label=f"CH{channel}",
            )
        selection = ", ".join(f"CH{channel}" for channel in channels) or "未选择通道"
        axis.set(
            title=f"Live UMA-8 input - {selection} (shared amplitude scale)",
            xlabel="Time in current block (ms)", ylabel="Raw amplitude",
        )
        axis.grid(True, alpha=0.25)
        if channels:
            axis.legend(ncol=4, fontsize=7, loc="upper right")
        else:
            axis.text(
                0.5, 0.5, "请在上方勾选要显示的通道", transform=axis.transAxes,
                ha="center", va="center",
            )
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
        channels = self._selected_waveform_channels(min(8, self.rir_waveform.shape[1]))
        for channel in channels:
            all_axis.plot(
                time_ms, self.rir_waveform[:, channel], linewidth=0.7,
                color=f"C{channel}", label=f"CH{channel}",
            )
            early = time_ms <= 50.0
            early_axis.plot(
                time_ms[early], self.rir_waveform[early, channel], linewidth=0.8,
                color=f"C{channel}", label=f"CH{channel}",
            )
        chirp = "C1" if self._selected_role() == "responder" else "C2"
        selection = ", ".join(f"CH{channel}" for channel in channels) or "未选择通道"
        all_axis.set(
            title=f"{chirp} remote RIR - {selection} (shared physical scale)",
            xlabel=f"Time relative to {chirp} arrival (ms)", ylabel="RIR amplitude",
            xlim=(-pre_arrival * 1000.0, max((duration - pre_arrival) * 1000.0, 1.0)),
        )
        early_axis.set(
            title=f"{chirp} remote RIR - pre-arrival and first 50 ms", xlabel=f"Time relative to {chirp} arrival (ms)",
            ylabel="RIR amplitude", xlim=(-pre_arrival * 1000.0, 50),
        )
        for axis in (all_axis, early_axis):
            axis.grid(True, alpha=0.25)
            if channels:
                axis.legend(ncol=4, fontsize=7, loc="upper right")
            else:
                axis.text(
                    0.5, 0.5, "请在上方勾选要显示的通道", transform=axis.transAxes,
                    ha="center", va="center",
                )
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
                    probe = "C1 remote RIR" if self._selected_role() == "responder" else "C2 remote RIR"
                    self.wave_notebook.tab(1, text=f"{probe}（最终）" if final else f"{probe}（实时预览）")
                    if not self._rir_seen:
                        self.wave_notebook.select(1)
                        self._rir_seen = True
                elif kind == "session_status":
                    self._update_pose_status(value)
                    self._update_event_pose_status(value.get("latest_spatial_events"))
                    latest = value.get("latest_quality") or {}
                    quality_text = latest.get("overall", "尚无质量结果")
                    countdown = value.get("next_trigger_seconds")
                    countdown_text = "--" if countdown is None else f"{countdown:.1f}s"
                    radar = value.get("radar_pose")
                    radar_text = "radar --"
                    if radar:
                        position = radar["position_m"]
                        radar_text = f"radar ({position[0]:.2f},{position[1]:.2f},{position[2]:.2f})m"
                    self.vars["session_status"].set(
                        f"状态 {value['state']} | measurement {value['measurement_id']} | "
                        f"成功 {value['success']} / 失败 {value['failure']} / 跳过 {value['skipped']} | "
                        f"下次 {countdown_text} | 最近质量 {quality_text} | {radar_text}"
                    )
                    if self.vars["capture_mode"].get() == "manual_continuous":
                        self.capture_button.configure(
                            state="normal" if value["state"] == "ARMED" and not value["paused"] else "disabled"
                        )
                elif kind == "role_status":
                    self._update_pose_status(value)
                    self._update_event_pose_status(value.get("latest_spatial_events"))
                    radar = value.get("radar_pose")
                    radar_text = "radar --"
                    if radar:
                        position = radar["position_m"]
                        radar_text = f"radar ({position[0]:.2f},{position[1]:.2f},{position[2]:.2f})m"
                    self.vars["session_status"].set(
                        f"ROLE {value['role'].upper()} | STATE {value['state']} | session {value['session_id']} | "
                        f"buffer {value['audio_buffer_frames']} | dropped {value['dropped_frames']} | "
                        f"C1 {value['c1_score']:.3f}/{value['c1_threshold']:.2f} | "
                        f"C2 {value['c2_score']:.3f}/{value['c2_threshold']:.2f} | "
                        f"playback {value['playback_status']} | network {value['network_packets']} | {radar_text}"
                    )
                elif kind == "done":
                    directory, result = value
                    self._update_event_pose_status(result.get("local_spatial_events"))
                    if result.get("protocol") == "AVTWIN_V1":
                        overall = result["result"]
                        tof = result["tof"]
                        tof_text = f"距离 {tof['distance_m']:.4f} m" if tof["available"] else "Exact ToF: NOT AVAILABLE"
                    elif "quality" in result:
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
                    self.pose_source_combo.configure(state="readonly")
                    self.reset_pose_button.configure(state="normal")
                    self.active_controller = None
                    self._ensure_idle_udp_listener()
                elif kind == "error":
                    self._append(f"ERROR: {value}")
                    self.vars["status"].set("运行失败；详见日志")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.capture_button.configure(state="disabled")
                    self.pause_button.configure(state="disabled", text="暂停自动采集")
                    self.test_output_button.configure(state="normal")
                    self.pose_source_combo.configure(state="readonly")
                    self.reset_pose_button.configure(state="normal")
                    self.active_controller = None
                    self._ensure_idle_udp_listener()
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
                elif kind == "diagnostic_done":
                    name, result = value
                    self._append(f"DEBUG {name}: {json.dumps(result, ensure_ascii=False, default=str)}")
                    self.vars["status"].set(f"Debug {name} 完成")
                elif kind == "diagnostic_error":
                    name, error = value
                    self._append(f"DEBUG {name} FAILED: {error}")
                    self.vars["status"].set(f"Debug {name} 失败")
                    messagebox.showerror(f"Debug {name} 失败", str(error))
                elif kind == "udp_test_done":
                    self._udp_test_running = False
                    self._append(
                        "UDP ROUNDTRIP: "
                        + json.dumps(value, ensure_ascii=False, default=str)
                    )
                    self.udp_test_button.configure(state="normal")
                    if value["success"]:
                        source_ip = self._network_snapshot.get("source_ip") or "本机IP未知"
                        self.vars["udp_test_status"].set(
                            f"PASS：Linux {source_ip}:{self.vars['udp_port'].get()} ↔ "
                            f"Android {value['remote_endpoint']}，RTT {value['roundtrip_ms']:.2f} ms，"
                            f"reply {value['reply_source']}"
                        )
                    else:
                        source_ip = self._network_snapshot.get("source_ip") or "本机IP未知"
                        self.vars["udp_test_status"].set(
                            f"FAIL：Linux {source_ip}:{self.vars['udp_port'].get()} ↔ "
                            f"Android {value['remote_endpoint']}，2秒内无匹配回复"
                        )
                        messagebox.showwarning(
                            "UDP 双向检验失败",
                            "Linux 已发送 ping，但没有收到 Android 返回的匹配 reply。\n"
                            f"请保持 Android v0.9.2+ 应用打开，并在 Android 中填写 Linux Wi-Fi IP {source_ip}；\n"
                            "确认安卓 ARM 监听端口与这里的远端端口一致。",
                        )
                    self._ensure_idle_udp_listener()
                elif kind == "udp_test_error":
                    self._udp_test_running = False
                    self.udp_test_button.configure(state="normal")
                    self.vars["udp_test_status"].set(f"ERROR：{value}")
                    self._append(f"UDP ROUNDTRIP ERROR: {value}")
                    self._ensure_idle_udp_listener()
                    messagebox.showerror("UDP 双向检验错误", str(value))
                elif kind == "idle_udp":
                    self._append(value)
                    if "已自动回复" in value:
                        self.vars["udp_test_status"].set(f"PASS：Android → Linux → Android；{value}")
                elif kind == "idle_udp_error":
                    self.vars["udp_test_status"].set(f"Linux 常驻 UDP 测试监听失败：{value}")
                    self._append(f"WARNING: Linux 常驻 UDP 测试监听失败：{value}")
                elif kind == "network_status":
                    self._network_refresh_running = False
                    self._network_snapshot = value
                    self.vars["network_status"].set(format_network_status(value))
                elif kind == "network_status_error":
                    self._network_refresh_running = False
                    self.vars["network_status"].set(f"Linux 本机 IPv4：读取失败（{value}）")
        except queue.Empty:
            pass
        now = time.monotonic()
        if now >= self._next_network_refresh and not self._network_refresh_running:
            self._next_network_refresh = now + 2.0
            self._network_refresh_running = True
            remote_host = self.vars["android_host"].get().strip()

            def refresh_network() -> None:
                try:
                    self.events.put(("network_status", network_snapshot(remote_host)))
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    self.events.put(("network_status_error", exc))

            threading.Thread(
                target=refresh_network, name="avtwin-network-status", daemon=True,
            ).start()
            self._ensure_idle_udp_listener()
        controller = self.active_controller
        provider = (
            getattr(controller, "pose_provider", None)
            if controller is not None else self.live_pose_provider
        )
        latest = None if provider is None else provider.latest()
        if latest is not None:
            pose_warning = None
            tracking_status = latest.tracking_status
            metadata = provider.metadata()
            rejection_ns = metadata.get("last_rejection_ns")
            if rejection_ns is not None and time.monotonic_ns() - int(rejection_ns) < 500_000_000:
                tracking_status = "REJECTED"
                pose_warning = metadata.get("last_rejection")
            if controller is not None:
                speaker_offset = controller.config.speaker_offset_m
                microphone_offset = controller.config.microphone_offset_m
            else:
                try:
                    speaker_offset = parse_vector3(self.vars["speaker_offset"].get())
                    microphone_offset = parse_vector3(self.vars["microphone_offset"].get())
                except ValueError:
                    speaker_offset = (0.0, 0.0, 0.0)
                    microphone_offset = (0.0, 0.0, 0.0)
            self._update_pose_status({
                "radar_pose": {
                    "position_m": list(latest.position_m),
                    "orientation_xyzw": list(latest.orientation_xyzw),
                    "timestamp_ns": latest.timestamp_ns,
                    "frame_id": latest.frame_id,
                    "tracking_status": tracking_status,
                },
                "speaker_pose": transform_offset(
                    latest, speaker_offset,
                    child_frame_id="speaker_acoustic_center",
                ),
                "microphone_pose": transform_offset(
                    latest, microphone_offset,
                    child_frame_id="uma8_acoustic_center",
                ),
                "pose_warning": pose_warning,
            })
        # Live status strings can change a content frame's requested width without
        # resizing the canvas window itself. Refresh the scroll extents so the new
        # tail remains reachable and never forces neighboring controls to move.
        for sync_scroll_region in getattr(self, "_module_scroll_syncs", ()):
            sync_scroll_region()
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
        self._save_preferences()
        self._stop_idle_udp_listener()
        self._stop_live_pose_provider()
        self.root.destroy()

    def _finish_close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.root.after(100, self._finish_close)
        else:
            self._save_preferences()
            self._stop_idle_udp_listener()
            self._stop_live_pose_provider()
            self.root.destroy()


def launch_gui(defaults: dict[str, Any] | None = None) -> None:
    root = tk.Tk()
    ControllerGui(root, defaults)
    root.mainloop()
