"""Schema and connection helpers. Single source of truth for all collectors."""

import sqlite3
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.toml"


def load_config():
    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)

    def resolve(p):
        path = Path(p)
        return path if path.is_absolute() else (ROOT / path).resolve()

    cfg["db_path"] = resolve(cfg["db_path"])
    cfg["stalls_dir"] = resolve(cfg["stalls_dir"])
    return cfg


CONFIG = load_config()


def connect():
    conn = sqlite3.connect(str(CONFIG["db_path"]), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    init_schema(conn)
    return conn


def init_schema(conn):
    # GPU per-poll metrics (delta-logged)
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gpu_metrics_ts ON gpu_metrics(timestamp)")

    # Ollama HTTP requests parsed from container logs
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ollama_requests (
            timestamp     TEXT NOT NULL,
            client_ip     TEXT,
            method        TEXT,
            endpoint      TEXT,
            status        INTEGER,
            duration_ms   REAL,
            model         TEXT,
            prompt_tokens INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ollama_req_ts ON ollama_requests(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ollama_req_model ON ollama_requests(model)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ollama_req_dur ON ollama_requests(duration_ms)")

    # Host-side process metrics for ollama serve and runner
    conn.execute("""
        CREATE TABLE IF NOT EXISTS system_metrics (
            timestamp     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            proc_role     TEXT NOT NULL,    -- 'serve' or 'runner'
            host_pid      INTEGER NOT NULL,
            cpu_percent   REAL NOT NULL,    -- delta over poll interval, 100% = one core
            rss_mib       INTEGER NOT NULL,
            num_threads   INTEGER NOT NULL,
            host_load1    REAL,
            host_mem_used_mib INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sys_metrics_ts ON system_metrics(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sys_metrics_role ON system_metrics(proc_role)")

    # Detected hangs (loaded VRAM, GPU idle, but ollama serve is hot)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stall_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            start_ts        TEXT NOT NULL,
            end_ts          TEXT,
            gpu_id          INTEGER,
            vram_used_mib   INTEGER,
            ollama_serve_cpu REAL,
            ollama_serve_rss_mib INTEGER,
            model           TEXT,
            stack_path      TEXT,
            request_active  INTEGER,
            notes           TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stall_start ON stall_events(start_ts)")
    conn.commit()


if __name__ == "__main__":
    conn = connect()
    print(f"DB initialized at {CONFIG['db_path']}", file=sys.stderr)
    conn.close()
