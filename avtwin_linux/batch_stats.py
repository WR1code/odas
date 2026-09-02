from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from statistics import mean, pstdev
from typing import Any


FIELDS = (
    "role", "runs", "c1_detection_rate", "c2_detection_rate",
    "false_trigger_rate", "turnaround_mean_ms", "turnaround_std_ms",
    "rir_extraction_success_rate", "tof_valid_rate",
)


def _rate(values: list[bool]) -> float:
    return sum(values) / len(values) if values else 0.0


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def summarize(root: Path) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {"INITIATOR": [], "RESPONDER": []}
    for path in sorted(root.rglob("metadata.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        role = str(item.get("role", "")).upper()
        if role in grouped and item.get("protocol") == "AVTWIN_V1":
            grouped[role].append(item)

    rows: list[dict[str, Any]] = []
    for role, items in grouped.items():
        if not items:
            continue
        c1 = [int(item.get("c1_scores", {}).get("channels_passed", 0)) > 0 for item in items]
        c2 = [int(item.get("c2_scores", {}).get("channels_passed", 0)) > 0 for item in items]
        false_triggers = [
            bool(item.get("realtime_detection", {}).get("c1"))
            and item.get("c1_scores", {}).get("system_sample") is None
            for item in items
        ]
        turnarounds = [
            value * 1000.0 / float(item.get("sample_rate", 48_000))
            for item in items
            if (value := _finite(item.get("turnaround_samples"))) is not None
        ]
        rir = [bool(item.get("remote_rir", {}).get("available")) for item in items]
        tof = [bool(item.get("tof", {}).get("available")) for item in items]
        rows.append({
            "role": role, "runs": len(items),
            "c1_detection_rate": _rate(c1), "c2_detection_rate": _rate(c2),
            "false_trigger_rate": _rate(false_triggers),
            "turnaround_mean_ms": mean(turnarounds) if turnarounds else "",
            "turnaround_std_ms": pstdev(turnarounds) if turnarounds else "",
            "rir_extraction_success_rate": _rate(rir), "tof_valid_rate": _rate(tof),
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汇总 AV-Twin 双角色连续实验 metadata.json")
    parser.add_argument("root", type=Path, help="包含多个会话目录的 output 根目录")
    parser.add_argument("--output", type=Path, help="CSV 输出；默认 <root>/dual_role_stats.csv")
    args = parser.parse_args(argv)
    rows = summarize(args.root)
    output = args.output or args.root / "dual_role_stats.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(output.resolve())
    return 0 if rows else 2


if __name__ == "__main__":
    raise SystemExit(main())
