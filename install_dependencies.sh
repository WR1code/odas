#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/.venv"
PORTAUDIO_ROOT="$PROJECT_DIR/.deps/libportaudio2"
RUNTIME_DIR="$PROJECT_DIR/.runtime"

if [[ ! -r /etc/os-release ]] || ! grep -q '^ID=ubuntu' /etc/os-release; then
    echo "警告：此安装脚本按 Ubuntu 编写，当前系统可能不是 Ubuntu。" >&2
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "错误：找不到 python3。请先安装 Python 3。" >&2
    exit 1
fi

echo "系统组件（如缺失）：sudo apt install python3-tk python3-pip"
echo "正在使用当前 python3：$(command -v python3)"

mkdir -p "$PROJECT_DIR/.deps" "$RUNTIME_DIR/matplotlib"
export PIP_CACHE_DIR="$RUNTIME_DIR/pip-cache"

PORTAUDIO_LIBRARY="$(find "$PORTAUDIO_ROOT" -type f -name 'libportaudio.so.2*' -print -quit 2>/dev/null || true)"
if [[ -z "$PORTAUDIO_LIBRARY" ]] \
    && ! python3 -c 'import ctypes.util, sys; sys.exit(0 if ctypes.util.find_library("portaudio") else 1)'; then
    if ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg-deb >/dev/null 2>&1; then
        echo "错误：系统缺少 PortAudio，且无法使用 apt-get/dpkg-deb 安装项目本地副本。" >&2
        exit 1
    fi

    DOWNLOAD_DIR="$(mktemp -d "$PROJECT_DIR/.deps/portaudio-download.XXXXXX")"
    trap 'rm -rf "$DOWNLOAD_DIR"' EXIT
    echo "系统未安装 PortAudio；正在下载项目本地副本。"
    (
        cd "$DOWNLOAD_DIR"
        apt-get download libportaudio2
    )
    PORTAUDIO_DEB="$(find "$DOWNLOAD_DIR" -maxdepth 1 -type f -name 'libportaudio2_*.deb' -print -quit)"
    if [[ -z "$PORTAUDIO_DEB" ]]; then
        echo "错误：未能下载 libportaudio2 软件包。" >&2
        exit 1
    fi
    rm -rf "$PORTAUDIO_ROOT"
    mkdir -p "$PORTAUDIO_ROOT"
    dpkg-deb -x "$PORTAUDIO_DEB" "$PORTAUDIO_ROOT"
fi

PORTAUDIO_LIBRARY="$(find "$PORTAUDIO_ROOT" -type f -name 'libportaudio.so.2*' -print -quit 2>/dev/null || true)"
if [[ -n "$PORTAUDIO_LIBRARY" ]]; then
    PORTAUDIO_LIB="$(dirname "$PORTAUDIO_LIBRARY")"
    if [[ ! -e "$PORTAUDIO_LIB/libportaudio.so" ]]; then
        ln -s "$(basename "$PORTAUDIO_LIBRARY")" "$PORTAUDIO_LIB/libportaudio.so"
    fi
    export LD_LIBRARY_PATH="$PORTAUDIO_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export LIBRARY_PATH="$PORTAUDIO_LIB${LIBRARY_PATH:+:$LIBRARY_PATH}"
fi

if [[ ! -x "$VENV/bin/python" ]]; then
    echo "正在创建项目 Python 环境：$VENV"
    python3 -m venv --system-site-packages "$VENV"
fi

"$VENV/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"
MPLCONFIGDIR="$RUNTIME_DIR/matplotlib" "$VENV/bin/python" - <<'PY'
import tkinter
import matplotlib
import numpy
import scipy
import sounddevice
print(f"依赖检查成功：matplotlib {matplotlib.__version__}，tkinter 可用")
PY

echo "安装完成。声学握手 GUI 启动命令：./avtwin_linux/run_acoustic_handshake.sh --gui"
