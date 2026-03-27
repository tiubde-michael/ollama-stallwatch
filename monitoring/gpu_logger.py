#!/usr/bin/env python3
"""GPU-Metriken Delta-Logger: Pollt nvidia-smi alle 10s, schreibt nur bei Aenderung in SQLite."""

import sqlite3
import subprocess
import time
import signal
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "monitor.db"
POLL_INTERVAL = 10  # Sekunden
QUERY_FIELDS = "index,name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw"


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gpu_metrics (
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            gpu_id    INTEGER NOT NULL,
            gpu_name  TEXT NOT NULL,
            vram_used_mib  INTEGER NOT NULL,
            vram_total_mib INTEGER NOT NULL,
            utilization_gpu INTEGER NOT NULL,
            temperature     INTEGER NOT NULL,
            power_draw_w    REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_gpu_metrics_ts ON gpu_metrics(timestamp)
    """)
    conn.commit()


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
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    init_db(conn)

    last_values = {}  # gpu_id -> dict
    running = True

    def shutdown(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"GPU-Logger gestartet (DB: {DB_PATH}, Interval: {POLL_INTERVAL}s)")

    while running:
        try:
            rows = query_nvidia_smi()
            if rows:
                for gpu in rows:
                    gid = gpu["gpu_id"]
                    if has_changed(gpu, last_values.get(gid)):
                        conn.execute(
                            """INSERT INTO gpu_metrics
                               (gpu_id, gpu_name, vram_used_mib, vram_total_mib,
                                utilization_gpu, temperature, power_draw_w)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (gid, gpu["gpu_name"], gpu["vram_used_mib"],
                             gpu["vram_total_mib"], gpu["utilization_gpu"],
                             gpu["temperature"], gpu["power_draw_w"])
                        )
                        last_values[gid] = gpu.copy()
        except Exception as e:
            print(f"Fehler: {e}", file=sys.stderr)

        time.sleep(POLL_INTERVAL)

    conn.close()
    print("GPU-Logger beendet.")


if __name__ == "__main__":
    main()
