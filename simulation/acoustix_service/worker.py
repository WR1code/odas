"""One-request worker that runs only inside the dedicated AcoustiX environment."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    root = Path(os.environ.get("ACOUSTIX_ROOT", "")).expanduser().resolve()
    if not root.joinpath("simu_utils.py").is_file():
        raise SystemExit("ACOUSTIX_ROOT must point to the official AcoustiX checkout")
    os.chdir(root)
    sys.path.insert(0, str(root))
    import numpy as np
    import tensorflow as tf
    from simu_utils import ir_simulation, load_cfg
    import mitsuba as mi

    seed = int(request["seed"])
    gpus = tf.config.list_physical_devices("GPU")
    for gpu in gpus:
        tf.config.experimental.set_memory_growth(gpu, True)
    np.random.seed(seed)
    tf.random.set_seed(seed)
    config = load_cfg(request["config"])
    if request.get("material_db"):
        config["material_db"] = request["material_db"]
    started = time.perf_counter()
    rirs, rx_pos, rx_ori = ir_simulation(
        scene_path=request["scene_xml"],
        rx_pos=np.asarray(request["receiver_positions_m"], dtype=float),
        tx_pos=np.asarray(request["source_position_m"], dtype=float),
        rx_ori=np.asarray(request["receiver_orientations"], dtype=float),
        tx_ori=np.asarray(request["source_orientation"], dtype=float),
        simu_config=config,
    )
    if rirs.shape[0] != len(request["receiver_positions_m"]):
        raise RuntimeError(f"AcoustiX returned {rirs.shape[0]} valid receivers; expected all {len(request['receiver_positions_m'])}")
    output = Path(request["output_npz"])
    output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output, rirs=rirs, receiver_positions_m=rx_pos, receiver_orientations=rx_ori)
    print(json.dumps({"backend": "official_acoustix", "output_npz": str(output),
                      "rir_seconds": time.perf_counter() - started, "shape": list(rirs.shape),
                      "tensorflow_gpus": [gpu.name for gpu in gpus],
                      "mitsuba_variant": mi.variant()}))


if __name__ == "__main__":
    main()
