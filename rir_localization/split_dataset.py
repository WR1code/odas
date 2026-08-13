#!/usr/bin/env python3
from __future__ import annotations

import argparse
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Hashable

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rir_localization.utils import read_csv, write_csv, write_json
from rir_localization.build_dataset import DATASET_FIELDS


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="创建可复现的 RIR train/val/test split")
    result.add_argument("--dataset", required=True, type=Path, help="dataset.csv")
    result.add_argument("--output", required=True, type=Path, help="splits 输出目录")
    result.add_argument("--mode", choices=("sequential", "spatial_block", "session"), default="sequential")
    result.add_argument("--train-ratio", type=float, default=0.7)
    result.add_argument("--val-ratio", type=float, default=0.1)
    result.add_argument("--test-ratio", type=float, default=0.2)
    result.add_argument("--spatial-block-size-m", type=float, default=0.5)
    result.add_argument("--seed", type=int, default=42)
    return result


def _validate_ratios(ratios: tuple[float, float, float]) -> None:
    if any(value < 0 for value in ratios) or not np.isclose(sum(ratios), 1.0, atol=1e-8):
        raise ValueError(f"split ratios 必须非负且总和为 1，实际 {ratios}")


def _counts(total: int, ratios: tuple[float, float, float]) -> list[int]:
    raw = np.asarray(ratios) * total
    result = np.floor(raw).astype(int)
    remainder_order = np.argsort(-(raw - result), kind="stable")
    for index in remainder_order[: total - int(result.sum())]:
        result[index] += 1
    if total >= sum(value > 0 for value in ratios):
        for index, ratio in enumerate(ratios):
            if ratio > 0 and result[index] == 0:
                donor = int(np.argmax(result))
                if result[donor] > 1:
                    result[donor] -= 1; result[index] += 1
    return result.tolist()


def _ordered(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(rows, key=lambda row: (
        row.get("timestamp", ""), row.get("session_id", ""), int(row.get("measurement_id", "0"))
    ))


def _group_split(rows: list[dict[str, str]], group_keys: list[Hashable], ratios: tuple[float, float, float],
                 seed: int) -> list[list[dict[str, str]]]:
    groups: dict[Hashable, list[dict[str, str]]] = defaultdict(list)
    for row, key in zip(rows, group_keys):
        groups[key].append(row)
    keys = list(groups)
    random.Random(seed).shuffle(keys)
    targets = np.asarray(ratios) * len(rows)
    assignments: list[list[dict[str, str]]] = [[], [], []]
    active = [index for index, ratio in enumerate(ratios) if ratio > 0]
    seeded: set[Hashable] = set()
    if len(keys) >= len(active):
        # Seed every requested split with one whole group. Stable random tie-breaking comes from the shuffle.
        ordered_keys = sorted(keys, key=lambda key: len(groups[key]), reverse=True)
        ordered_splits = sorted(active, key=lambda index: ratios[index], reverse=True)
        for key, destination in zip(ordered_keys, ordered_splits):
            assignments[destination].extend(groups[key]); seeded.add(key)
    for key in keys:
        if key in seeded:
            continue
        deficits = targets - np.asarray([len(part) for part in assignments])
        destination = max(active, key=lambda index: (deficits[index], ratios[index]))
        assignments[destination].extend(groups[key])
    return assignments


def create_splits(dataset_csv: Path, output: Path, *, mode: str, ratios: tuple[float, float, float],
                  seed: int, spatial_block_size_m: float) -> dict[str, Any]:
    _validate_ratios(ratios)
    rows = _ordered(read_csv(dataset_csv))
    if not rows:
        raise ValueError(f"dataset 为空：{dataset_csv}")
    if mode == "sequential":
        counts = _counts(len(rows), ratios)
        first, second = counts[0], counts[0] + counts[1]
        parts = [rows[:first], rows[first:second], rows[second:]]
    elif mode == "spatial_block":
        if spatial_block_size_m <= 0:
            raise ValueError("spatial_block_size_m 必须大于 0")
        keys = [(int(np.floor(float(row["rx_x"]) / spatial_block_size_m)),
                 int(np.floor(float(row["rx_y"]) / spatial_block_size_m))) for row in rows]
        parts = _group_split(rows, keys, ratios, seed)
    elif mode == "session":
        parts = _group_split(rows, [row["session_id"] for row in rows], ratios, seed)
    else:
        raise ValueError(f"未知 split mode：{mode}")
    names = ("train", "val", "test")
    output.mkdir(parents=True, exist_ok=True)
    for name, part in zip(names, parts):
        write_csv(output / f"{name}.csv", part, DATASET_FIELDS)
    identities = [{(row["session_id"], row["measurement_id"]) for row in part} for part in parts]
    if any(identities[i] & identities[j] for i in range(3) for j in range(i + 1, 3)):
        raise AssertionError("split 之间出现 measurement 泄漏")
    fig, ax = plt.subplots(figsize=(6.5, 6))
    colors = ("tab:blue", "tab:orange", "tab:green")
    for name, part, color in zip(names, parts, colors):
        if part:
            points = np.asarray([[float(row["rx_x"]), float(row["rx_y"])] for row in part])
            ax.scatter(points[:, 0], points[:, 1], label=f"{name} (n={len(part)})", color=color)
    ax.set(xlabel="x (m)", ylabel="y (m)", title=f"Dataset split: {mode}")
    ax.axis("equal"); ax.grid(alpha=0.3); ax.legend(); fig.tight_layout()
    fig.savefig(output / "split_spatial_map.png", dpi=180); plt.close(fig)
    report = {
        "dataset_csv": str(dataset_csv.resolve()), "mode": mode, "seed": seed,
        "ratios": dict(zip(names, ratios)), "counts": dict(zip(names, map(len, parts))),
        "spatial_block_size_m": spatial_block_size_m if mode == "spatial_block" else None,
        "measurement_overlap": False, "group_overlap": False if mode in {"spatial_block", "session"} else None,
        "warnings": [f"{name} split is empty" for name, part, ratio in zip(names, parts, ratios) if ratio > 0 and not part],
    }
    write_json(output / "split_stats.json", report)
    return report


def main() -> int:
    args = parser().parse_args()
    try:
        report = create_splits(args.dataset, args.output, mode=args.mode,
                               ratios=(args.train_ratio, args.val_ratio, args.test_ratio), seed=args.seed,
                               spatial_block_size_m=args.spatial_block_size_m)
    except Exception as exc:
        print(f"ERROR: split failed: {exc}", file=sys.stderr)
        return 2
    print(f"Split mode: {report['mode']}")
    print(f"Counts: {report['counts']}")
    if report["warnings"]: print(f"Warnings: {report['warnings']}")
    print(f"Output: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
