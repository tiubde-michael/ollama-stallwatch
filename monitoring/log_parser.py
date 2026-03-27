#!/usr/bin/env python3
"""Ollama-Log-Parser: Folgt Docker-Logs und schreibt Request-Daten in SQLite."""

import sqlite3
import subprocess
import re
import signal
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "monitor.db"
COMPOSE_DIR = Path(__file__).parent.parent  # /srv/Container

# [GIN] 2026/03/27 - 13:24:13 | 200 |          1m6s |       127.0.0.1 | POST     "/api/generate"
GIN_PATTERN = re.compile(
    r'\[GIN\]\s+(\d{4}/\d{2}/\d{2})\s+-\s+(\d{2}:\d{2}:\d{2})\s+\|\s+(\d+)\s+\|'
    r'\s+([^\|]+?)\s+\|\s+([^\|]+?)\s+\|\s+(\w+)\s+"([^"]+)"'
)

# runner.name aus "finished setting up" oder "context for request finished"
RUNNER_PATTERN = re.compile(
    r'msg="(?:finished setting up|context for request finished)"\s+runner\.name=registry\.ollama\.ai/library/([^\s]+)'
)

# msg="completion request" images=0 prompt=76 format=""
COMPLETION_PATTERN = re.compile(
    r'msg="completion request".*?prompt=(\d+)'
)


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ollama_requests (
            timestamp   TEXT NOT NULL,
            client_ip   TEXT,
            method      TEXT,
            endpoint    TEXT,
            status      INTEGER,
            duration_ms REAL,
            model       TEXT,
            prompt_tokens INTEGER
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ollama_req_ts ON ollama_requests(timestamp)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ollama_req_model ON ollama_requests(model)
    """)
    conn.commit()


def parse_duration(dur_str):
    """Parst Ollama-Dauer-Strings: '1m6s', '18.365us', '1.334ms', '2.5s' -> Millisekunden."""
    dur_str = dur_str.strip()
    total_ms = 0.0

    # Minuten
    m = re.search(r'(\d+)m', dur_str)
    if m:
        total_ms += int(m.group(1)) * 60_000

    # Sekunden (mit oder ohne Dezimal)
    s = re.search(r'([\d.]+)s(?!.*[um])', dur_str)
    if not s:
        s = re.search(r'([\d.]+)s$', dur_str)
    if s:
        total_ms += float(s.group(1)) * 1000

    # Millisekunden
    ms = re.search(r'([\d.]+)ms', dur_str)
    if ms:
        total_ms += float(ms.group(1))

    # Mikrosekunden
    us = re.search(r'([\d.]+)[uµ]s', dur_str)
    if us:
        total_ms += float(us.group(1)) / 1000

    return round(total_ms, 3)


def main():
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    init_db(conn)

    running = True
    current_model = None
    current_prompt_tokens = None
    last_request_rowid = None  # Fuer nachtraegliches Model-Update

    def shutdown(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"Log-Parser gestartet (DB: {DB_PATH})")

    # docker compose logs --follow --since 1s --no-color ollama
    proc = subprocess.Popen(
        ["docker", "compose", "-f", str(COMPOSE_DIR / "docker-compose.yml"),
         "logs", "--follow", "--since", "1s", "--no-color", "ollama"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1
    )

    try:
        for line in proc.stdout:
            if not running:
                break

            # Model-Name aus "finished setting up" / "context for request finished"
            runner_match = RUNNER_PATTERN.search(line)
            if runner_match:
                current_model = runner_match.group(1)
                # Nachtraeglich den letzten Request updaten falls model fehlte
                if last_request_rowid:
                    conn.execute(
                        "UPDATE ollama_requests SET model=? WHERE rowid=? AND model IS NULL",
                        (current_model, last_request_rowid)
                    )
                    last_request_rowid = None
                continue

            # Prompt-Tokens aus "completion request" extrahieren
            comp_match = COMPLETION_PATTERN.search(line)
            if comp_match:
                current_prompt_tokens = int(comp_match.group(1))
                continue

            # GIN Request-Zeile parsen
            gin_match = GIN_PATTERN.search(line)
            if gin_match:
                date_str = gin_match.group(1).replace("/", "-")
                time_str = gin_match.group(2)
                timestamp = f"{date_str}T{time_str}Z"
                status = int(gin_match.group(3))
                duration_ms = parse_duration(gin_match.group(4))
                client_ip = gin_match.group(5).strip()
                method = gin_match.group(6)
                endpoint = gin_match.group(7)

                # Nur relevante API-Endpoints loggen (nicht healthchecks)
                if endpoint in ("/", "/api/ps", "/api/version", "/api/tags"):
                    continue

                is_inference = endpoint in ("/api/chat", "/api/generate")
                model = current_model if is_inference else None
                prompt_tokens = current_prompt_tokens if is_inference else None

                cursor = conn.execute(
                    """INSERT INTO ollama_requests
                       (timestamp, client_ip, method, endpoint, status, duration_ms,
                        model, prompt_tokens)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (timestamp, client_ip, method, endpoint, status, duration_ms,
                     model, prompt_tokens)
                )

                # Merke rowid fuer nachtraegliches Model-Update
                if endpoint in ("/api/chat", "/api/generate") and model is None:
                    last_request_rowid = cursor.lastrowid
                else:
                    last_request_rowid = None

                # Reset nach Einfuegen
                if model:
                    current_prompt_tokens = None

    except Exception as e:
        print(f"Fehler: {e}", file=sys.stderr)
    finally:
        proc.terminate()
        proc.wait()
        conn.close()
        print("Log-Parser beendet.")


if __name__ == "__main__":
    main()
