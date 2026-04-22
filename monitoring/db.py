"""Schema and connection helpers. Single source of truth for all collectors."""

import sqlite3
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.toml"
EXAMPLE_PATH = ROOT / "config.example.toml"


def load_config():
    if not CONFIG_PATH.exists():
        if EXAMPLE_PATH.exists():
            sys.stderr.write(
                f"ERROR: {CONFIG_PATH.name} not found. Copy from template:\n"
                f"  cp {EXAMPLE_PATH} {CONFIG_PATH}\n"
                f"Then edit host_id and any per-host paths.\n"
                f"(install.sh does this automatically on first run.)\n"
            )
        raise SystemExit(2)
    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)

    def resolve(p):
        path = Path(p)
        return path if path.is_absolute() else (ROOT / path).resolve()

    cfg["db_path"] = resolve(cfg["db_path"])
    cfg["stalls_dir"] = resolve(cfg["stalls_dir"])
    cfg["ollama"]["compose_file"] = str(resolve(cfg["ollama"]["compose_file"]))
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
            confidence      TEXT,    -- 'strict' or 'loose'
            notes           TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stall_start ON stall_events(start_ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stall_end ON stall_events(end_ts)")

    # Migration: add columns introduced after initial deploy
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stall_events)").fetchall()}
    if "confidence" not in cols:
        conn.execute("ALTER TABLE stall_events ADD COLUMN confidence TEXT")
    if "mode" not in cols:
        # 'A' = stream-then-stall (GPU was active in recent window before idle)
        # 'B' = silent (GPU never produced output during recent window)
        conn.execute("ALTER TABLE stall_events ADD COLUMN mode TEXT")
    if "max_util_recent" not in cols:
        # Captured at event-open time: max GPU util over the last 60s.
        # Used to derive mode and as raw evidence for post-mortem.
        conn.execute("ALTER TABLE stall_events ADD COLUMN max_util_recent INTEGER")
    conn.commit()


if __name__ == "__main__":
    conn = connect()
    print(f"DB initialized at {CONFIG['db_path']}", file=sys.stderr)
    conn.close()
