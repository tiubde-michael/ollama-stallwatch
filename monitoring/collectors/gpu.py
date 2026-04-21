#!/usr/bin/env python3
"""GPU metrics collector — polls nvidia-smi, writes deltas to gpu_metrics."""

import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from db import CONFIG, connect

POLL_INTERVAL = CONFIG["gpu"]["poll_interval_sec"]
QUERY_FIELDS = "index,name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw"


def query_nvidia_smi():
    result = subprocess.run(
        ["nvidia-smi", f"--query-gpu={QUERY_FIELDS}", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        return None
    rows = []
    for line in result.stdout.strip().split("\n"):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 7:
            continue
        rows.append({
            "gpu_id": int(parts[0]),
            "gpu_name": parts[1],
            "vram_used_mib": int(parts[2]),
            "vram_total_mib": int(parts[3]),
            "utilization_gpu": int(parts[4]),
            "temperature": int(parts[5]),
            "power_draw_w": round(float(parts[6]), 1),
        })
    return rows


def has_changed(current, last):
    if last is None:
        return True
    for key in ("vram_used_mib", "utilization_gpu", "temperature", "power_draw_w"):
        if current[key] != last[key]:
            return True
    return False


def main():
    if shutil.which("nvidia-smi") is None:
        print("gpu collector: nvidia-smi not found, exiting cleanly", file=sys.stderr)
        return 0

    conn = connect()
    last_values = {}
    running = True

    def shutdown(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"gpu collector: poll={POLL_INTERVAL}s db={CONFIG['db_path']}", file=sys.stderr)

    while running:
        try:
            rows = query_nvidia_smi()
            if rows:
                for gpu in rows:
                    gid = gpu["gpu_id"]
                    if has_changed(gpu, last_values.get(gid)):
                        conn.execute(
                            "INSERT INTO gpu_metrics (gpu_id, gpu_name, vram_used_mib, "
                            "vram_total_mib, utilization_gpu, temperature, power_draw_w) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (gid, gpu["gpu_name"], gpu["vram_used_mib"],
                             gpu["vram_total_mib"], gpu["utilization_gpu"],
                             gpu["temperature"], gpu["power_draw_w"])
                        )
                        last_values[gid] = gpu.copy()
        except Exception as e:
            print(f"gpu collector error: {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
