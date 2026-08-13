from __future__ import annotations

from typing import Any


def normalize_channels(value: Any, channel_count: int = 8) -> tuple[int, ...]:
    """Normalize a saved waveform-channel selection; missing/corrupt means all."""
    if value is None:
        return tuple(range(channel_count))
    if isinstance(value, str):
        if not value.strip():
            return ()
        candidates: Any = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        if not value:
            return ()
        candidates = value
    else:
        return tuple(range(channel_count))
    selected: set[int] = set()
    had_valid_number = False
    for candidate in candidates:
        try:
            channel = int(candidate)
        except (TypeError, ValueError):
            continue
        had_valid_number = True
        if 0 <= channel < channel_count:
            selected.add(channel)
    if not selected and not had_valid_number:
        return tuple(range(channel_count))
    return tuple(sorted(selected))
