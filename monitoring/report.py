#!/usr/bin/env python3
"""Ollama-Monitor Auswertung: CLI-Tool fuer SQLite-Monitoring-Daten."""

import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

DB_PATH = Path(__file__).parent / "monitor.db"


def get_conn():
    if not DB_PATH.exists():
        print(f"Fehler: Datenbank {DB_PATH} nicht gefunden.", file=sys.stderr)
        sys.exit(1)
    return sqlite3.connect(str(DB_PATH))


def cmd_summary(hours=24):
    """Uebersicht der letzten N Stunden."""
    conn = get_conn()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"=== Zusammenfassung (letzte {hours}h) ===\n")

    # GPU Peak VRAM
    rows = conn.execute("""
        SELECT gpu_name, MAX(vram_used_mib), vram_total_mib
        FROM gpu_metrics WHERE timestamp > ? GROUP BY gpu_id
    """, (since,)).fetchall()
    if rows:
        print("GPU VRAM Peak:")
        for name, peak, total in rows:
            pct = round(peak / total * 100, 1) if total else 0
            print(f"  {name}: {peak} / {total} MiB ({pct}%)")
    else:
        print("GPU: Keine Daten")

    # GPU Peak Temperatur
    rows = conn.execute("""
        SELECT gpu_name, MAX(temperature), MAX(power_draw_w)
        FROM gpu_metrics WHERE timestamp > ? GROUP BY gpu_id
    """, (since,)).fetchall()
    if rows:
        print("\nGPU Peak Temp / Power:")
        for name, temp, power in rows:
            print(f"  {name}: {temp}C / {power}W")

    # Ollama Requests
    row = conn.execute("""
        SELECT COUNT(*), COUNT(DISTINCT model), COUNT(DISTINCT client_ip)
        FROM ollama_requests WHERE timestamp > ?
    """, (since,)).fetchone()
    if row and row[0] > 0:
        print(f"\nOllama Requests: {row[0]}")
        print(f"Verschiedene Modelle: {row[1]}")
        print(f"Verschiedene Clients: {row[2]}")
    else:
        print("\nOllama: Keine Request-Daten")

    # Top 3 Modelle
    rows = conn.execute("""
        SELECT model, COUNT(*) as cnt FROM ollama_requests
        WHERE timestamp > ? AND model IS NOT NULL
        GROUP BY model ORDER BY cnt DESC LIMIT 3
    """, (since,)).fetchall()
    if rows:
        print("\nTop Modelle:")
        for model, cnt in rows:
            print(f"  {model}: {cnt} Requests")

    conn.close()


def cmd_models(hours=168):
    """Modell-Nutzung der letzten N Stunden (Default: 7 Tage)."""
    conn = get_conn()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    days = hours // 24

    print(f"=== Modell-Nutzung (letzte {days} Tage) ===\n")

    rows = conn.execute("""
        SELECT model, COUNT(*) as cnt,
               ROUND(AVG(duration_ms), 0) as avg_ms,
               ROUND(AVG(prompt_tokens), 0) as avg_prompt
        FROM ollama_requests
        WHERE timestamp > ? AND model IS NOT NULL
        GROUP BY model ORDER BY cnt DESC
    """, (since,)).fetchall()

    if rows:
        print(f"{'Modell':<30} {'Requests':>9} {'Avg Dauer':>12} {'Avg Prompt':>11}")
        print("-" * 65)
        for model, cnt, avg_ms, avg_prompt in rows:
            dur_str = format_duration(avg_ms) if avg_ms else "-"
            prompt_str = str(int(avg_prompt)) if avg_prompt else "-"
            print(f"{model:<30} {cnt:>9} {dur_str:>12} {prompt_str:>11}")
    else:
        print("Keine Daten.")

    conn.close()


def cmd_gpu(hours=24):
    """GPU-Metriken der letzten N Stunden."""
    conn = get_conn()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"=== GPU-Metriken (letzte {hours}h) ===\n")

    rows = conn.execute("""
        SELECT gpu_name,
               MIN(vram_used_mib), ROUND(AVG(vram_used_mib)), MAX(vram_used_mib), vram_total_mib,
               MIN(utilization_gpu), ROUND(AVG(utilization_gpu)), MAX(utilization_gpu),
               MIN(temperature), ROUND(AVG(temperature)), MAX(temperature),
               COUNT(*)
        FROM gpu_metrics WHERE timestamp > ? GROUP BY gpu_id
    """, (since,)).fetchall()

    if rows:
        for r in rows:
            name = r[0]
            print(f"--- {name} ---")
            print(f"  VRAM (MiB):   Min {r[1]:>6}   Avg {int(r[2]):>6}   Max {r[3]:>6}   / {r[4]}")
            print(f"  Auslastung %: Min {r[5]:>6}   Avg {int(r[6]):>6}   Max {r[7]:>6}")
            print(f"  Temperatur C: Min {r[8]:>6}   Avg {int(r[9]):>6}   Max {r[10]:>6}")
            print(f"  Datenpunkte:  {r[11]}")
            print()
    else:
        print("Keine Daten.")

    conn.close()


def cmd_clients(hours=168):
    """Client-IP Nutzung der letzten N Stunden (Default: 7 Tage)."""
    conn = get_conn()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    days = hours // 24

    print(f"=== Client-Zugriffe (letzte {days} Tage) ===\n")

    rows = conn.execute("""
        SELECT client_ip, COUNT(*) as cnt,
               COUNT(DISTINCT model) as models
        FROM ollama_requests
        WHERE timestamp > ?
        GROUP BY client_ip ORDER BY cnt DESC
    """, (since,)).fetchall()

    if rows:
        print(f"{'Client-IP':<20} {'Requests':>9} {'Modelle':>8}")
        print("-" * 40)
        for ip, cnt, models in rows:
            print(f"{ip:<20} {cnt:>9} {models:>8}")
    else:
        print("Keine Daten.")

    conn.close()


def cmd_busy(hours=168):
    """Busiest Stunden der letzten N Stunden (Default: 7 Tage)."""
    conn = get_conn()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    days = hours // 24

    print(f"=== Busiest Stunden (letzte {days} Tage) ===\n")

    rows = conn.execute("""
        SELECT substr(timestamp, 1, 13) as hour, COUNT(*) as cnt
        FROM ollama_requests
        WHERE timestamp > ?
        GROUP BY hour ORDER BY cnt DESC LIMIT 10
    """, (since,)).fetchall()

    if rows:
        print(f"{'Stunde (UTC)':<20} {'Requests':>9}")
        print("-" * 30)
        for hour, cnt in rows:
            print(f"{hour:<20} {cnt:>9}")
    else:
        print("Keine Daten.")

    conn.close()


def cmd_status():
    """Aktueller Status: DB-Groesse, aeltester/neuester Eintrag."""
    if not DB_PATH.exists():
        print("Datenbank existiert noch nicht.")
        return

    conn = get_conn()
    size_mb = round(DB_PATH.stat().st_size / 1024 / 1024, 2)
    print(f"=== Monitor Status ===\n")
    print(f"Datenbank: {DB_PATH} ({size_mb} MB)")

    for table in ("gpu_metrics", "ollama_requests"):
        row = conn.execute(f"""
            SELECT COUNT(*), MIN(timestamp), MAX(timestamp)
            FROM {table}
        """).fetchone()
        if row and row[0] > 0:
            print(f"\n{table}:")
            print(f"  Eintraege: {row[0]}")
            print(f"  Aeltester: {row[1]}")
            print(f"  Neuester:  {row[2]}")
        else:
            print(f"\n{table}: leer")

    conn.close()


def format_duration(ms):
    if ms is None:
        return "-"
    if ms < 1000:
        return f"{ms:.0f}ms"
    elif ms < 60_000:
        return f"{ms / 1000:.1f}s"
    else:
        mins = int(ms // 60_000)
        secs = (ms % 60_000) / 1000
        return f"{mins}m{secs:.0f}s"


def usage():
    print("""Ollama Monitor Report

Verwendung: report.py <befehl> [stunden]

Befehle:
  summary [N]   Uebersicht letzte N Stunden (Default: 24)
  models  [N]   Modell-Nutzung letzte N Stunden (Default: 168 = 7d)
  gpu     [N]   GPU-Metriken letzte N Stunden (Default: 24)
  clients [N]   Client-IPs letzte N Stunden (Default: 168 = 7d)
  busy    [N]   Busiest Stunden letzte N Stunden (Default: 168 = 7d)
  status        DB-Status und Groessen""")


def main():
    if len(sys.argv) < 2:
        usage()
        sys.exit(1)

    cmd = sys.argv[1]
    hours = int(sys.argv[2]) if len(sys.argv) > 2 else None

    commands = {
        "summary": (cmd_summary, 24),
        "models":  (cmd_models, 168),
        "gpu":     (cmd_gpu, 24),
        "clients": (cmd_clients, 168),
        "busy":    (cmd_busy, 168),
        "status":  (cmd_status, None),
    }

    if cmd not in commands:
        usage()
        sys.exit(1)

    func, default_hours = commands[cmd]
    if default_hours is not None:
        func(hours or default_hours)
    else:
        func()


if __name__ == "__main__":
    main()
