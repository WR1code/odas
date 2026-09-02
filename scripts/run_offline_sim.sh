#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ $# -gt 1 ]]; then
    echo "Usage: $0 [clean_mono_speech.wav]" >&2
    exit 2
fi

SPEECH_PATH="${1:-${ODAS_CLEAN_SPEECH:-}}"
if [[ -z "$SPEECH_PATH" ]]; then
    for candidate in /home/w/.vscode/extensions/openai.chatgpt-*/webview/assets/cove.en.*.wav; do
        [[ -f "$candidate" ]] || continue
        if [[ -z "$SPEECH_PATH" || "$candidate" -nt "$SPEECH_PATH" ]]; then
            SPEECH_PATH="$candidate"
        fi
    done
fi
if [[ -z "$SPEECH_PATH" || ! -f "$SPEECH_PATH" ]]; then
    echo "No clean speech WAV found. Set ODAS_CLEAN_SPEECH or pass one WAV path once." >&2
    exit 2
fi

cd "$PROJECT_DIR"
echo "Using clean speech: $SPEECH_PATH"
exec python3 -m simulation.offline_demo --speech "$SPEECH_PATH"
