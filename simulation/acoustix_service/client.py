"""Launch the official AcoustiX worker in its isolated Conda environment."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

import numpy as np


class AcoustixUnavailable(RuntimeError):
    pass


def compute_rirs(*, scene_xml: Path, config: Path, source_position_m: np.ndarray,
                 receiver_positions_m: np.ndarray, output_npz: Path, seed: int,
                 project_root: Path, conda_env: str = "odas-acoustix",
                 acoustix_root: str | None = None,
                 materials_path: Path | None = None) -> tuple[np.ndarray, dict]:
    root = acoustix_root or os.environ.get("ACOUSTIX_ROOT", str(Path.home() / "src" / "AcoustiX"))
    if not Path(root).joinpath("simu_utils.py").is_file():
        raise AcoustixUnavailable(
            f"official AcoustiX checkout not found at {root}; run scripts/install_acoustix.sh after reviewing its downloads"
        )
    request = {
        "scene_xml": str(scene_xml.resolve()), "config": str(config.resolve()),
        "source_position_m": np.asarray(source_position_m).tolist(),
        "source_orientation": [-1.0, 0.0, 0.0],
        "receiver_positions_m": np.asarray(receiver_positions_m).tolist(),
        "receiver_orientations": np.tile([1.0, 0.0, 0.0], (len(receiver_positions_m), 1)).tolist(),
        "seed": int(seed), "output_npz": str(output_npz.resolve()),
    }
    if materials_path is not None:
        mapped = json.loads(materials_path.read_text(encoding="utf-8"))["materials"]
        frequencies = [125, 250, 500, 1000, 2000, 4000]
        request["material_db"] = {
            item["acoustix_name"]: {
                str(frequency): float(alpha)
                for frequency, alpha in zip(frequencies, item["absorption"])
            }
            for item in mapped.values()
        }
    request_path = output_npz.with_suffix(".request.json")
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
    command = ["conda", "run", "--no-capture-output", "-n", conda_env, "python",
               str(project_root / "simulation/acoustix_service/worker.py"), str(request_path)]
    environment = {**os.environ, "ACOUSTIX_ROOT": str(Path(root).resolve())}
    try:
        completed = subprocess.run(command, text=True, capture_output=True, env=environment, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        raise AcoustixUnavailable(f"AcoustiX worker failed: {detail.strip()}") from error
    status_line = next((line for line in reversed(completed.stdout.splitlines()) if line.startswith("{")), "{}")
    status = json.loads(status_line)
    with np.load(output_npz) as data:
        rirs = np.asarray(data["rirs"], dtype=np.float64)
    return rirs, status
