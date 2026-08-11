"""Start the PCM server first, then ODAS socket client, and collect metrics."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import time


PROJECT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pcm", type=Path, default=PROJECT / "simulation/output/offline/multichannel_s32le.raw")
    parser.add_argument("--port", type=int, default=10000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be in 1..65535")
    output = PROJECT / "simulation/output/online"
    output.mkdir(parents=True, exist_ok=True)
    ready = output / "server.ready"
    ready.unlink(missing_ok=True)
    tracks = output / "tracks.json"
    tracks.unlink(missing_ok=True)
    config_template = (PROJECT / "config/odaslive/uma8_sim_socket.cfg").read_text(encoding="utf-8")
    runtime_config = output / "odas_socket.cfg"
    runtime_config.write_text(
        config_template
        .replace("port = 10000;", f"port = {args.port};", 1)
        .replace("/home/w/project/odas/simulation/output/online/tracks.json", str(tracks.resolve())),
        encoding="utf-8",
    )
    server = subprocess.Popen([
        sys.executable, "-m", "simulation.odas_streamer.tcp_server", str(args.pcm),
        "--port", str(args.port), "--ready-file", str(ready), "--metrics", str(output / "stream_metrics.json")
    ], cwd=PROJECT)
    try:
        for _ in range(100):
            if ready.exists():
                break
            if server.poll() is not None:
                return server.returncode or 1
            time.sleep(0.05)
        else:
            raise RuntimeError("PCM server did not become ready")
        completed = subprocess.run([str(PROJECT / "build/bin/odaslive"), "-s", "-c", str(runtime_config)], cwd=PROJECT)
        return completed.returncode
    finally:
        if server.poll() is None:
            server.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
