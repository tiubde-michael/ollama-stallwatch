#!/usr/bin/env python3
"""HTTP server: dashboard pages + read-only REST API for cross-host correlation."""

import http.server
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import CONFIG, connect

PORT = CONFIG["api"]["port"]
HOST = CONFIG["api"]["host"]
DIR = Path(__file__).parent
DASHBOARD = DIR / "dashboard.py"
HTML = DIR / "dashboard.html"
STALLS_DIR = CONFIG["stalls_dir"]


def json_response(handler, payload, status=200):
    body = json.dumps(payload, default=str).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler, text, status=200):
    body = text.encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIR), **kwargs)

    def log_message(self, fmt, *args):
        # quiet stdlib access logs; systemd already logs starts/stops
        pass

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        qs = parse_qs(url.query)

        # ===== REST =====
        if path == "/api/health":
            return json_response(self, {"ok": True, "host": CONFIG["host_id"]})

        if path == "/api/stalls":
            since = (qs.get("since", [None])[0]
                     or "1970-01-01T00:00:00Z")
            limit = int(qs.get("limit", ["100"])[0])
            return json_response(self, self._query_stalls(since, limit))

        if path.startswith("/api/stalls/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[4] == "stack":
                return self._serve_stack(int(parts[3]))

        if path == "/api/requests":
            return json_response(self, self._query_requests(qs))

        if path == "/api/system":
            since = qs.get("since", ["1970-01-01T00:00:00Z"])[0]
            return json_response(self, self._query_system(since))

        # ===== HTML dashboard =====
        if path in ("/", "/dashboard.html"):
            hours = qs.get("h", ["24"])[0]
            try:
                int(hours)
            except ValueError:
                hours = "24"
            subprocess.run([sys.executable, str(DASHBOARD), hours],
                           capture_output=True, timeout=15)
            self.path = "/dashboard.html"

        return super().do_GET()

    def _query_stalls(self, since, limit):
        conn = connect()
        rows = conn.execute("""
            SELECT id, start_ts, end_ts, gpu_id, vram_used_mib, ollama_serve_cpu,
                   ollama_serve_rss_mib, model, stack_path, request_active
            FROM stall_events WHERE start_ts > ?
            ORDER BY start_ts DESC LIMIT ?
        """, (since, limit)).fetchall()
        conn.close()
        return {"host": CONFIG["host_id"],
                "items": [{"id": r[0], "start": r[1], "end": r[2], "gpu_id": r[3],
                           "vram_used_mib": r[4], "ollama_serve_cpu": r[5],
                           "ollama_serve_rss_mib": r[6], "model": r[7],
                           "stack_url": (f"/api/stalls/{r[0]}/stack" if r[8] else None),
                           "request_active": r[9]} for r in rows]}

    def _serve_stack(self, stall_id):
        conn = connect()
        row = conn.execute("SELECT stack_path FROM stall_events WHERE id=?",
                           (stall_id,)).fetchone()
        conn.close()
        if not row or not row[0]:
            return text_response(self, "no stack captured for this stall\n", 404)
        path = Path(row[0])
        if not path.is_file():
            return text_response(self, f"stack file missing: {path}\n", 404)
        try:
            return text_response(self, path.read_text())
        except Exception as e:
            return text_response(self, f"read error: {e}\n", 500)

    def _query_requests(self, qs):
        since = qs.get("since", ["1970-01-01T00:00:00Z"])[0]
        until = qs.get("until", [None])[0]
        model = qs.get("model", [None])[0]
        endpoint = qs.get("endpoint", [None])[0]
        client_ip = qs.get("client_ip", [None])[0]
        min_dur = qs.get("min_duration_ms", [None])[0]
        max_dur = qs.get("max_duration_ms", [None])[0]
        status = qs.get("status", [None])[0]
        limit = int(qs.get("limit", ["500"])[0])

        where = ["timestamp > ?"]
        params = [since]
        if until:
            where.append("timestamp < ?"); params.append(until)
        if model:
            where.append("model = ?"); params.append(model)
        if endpoint:
            where.append("endpoint = ?"); params.append(endpoint)
        if client_ip:
            where.append("client_ip = ?"); params.append(client_ip)
        if min_dur:
            where.append("duration_ms >= ?"); params.append(float(min_dur))
        if max_dur:
            where.append("duration_ms <= ?"); params.append(float(max_dur))
        if status:
            where.append("status = ?"); params.append(int(status))
        params.append(limit)

        conn = connect()
        rows = conn.execute(
            "SELECT timestamp, client_ip, method, endpoint, status, duration_ms, "
            "model, prompt_tokens FROM ollama_requests "
            f"WHERE {' AND '.join(where)} ORDER BY timestamp DESC LIMIT ?",
            params
        ).fetchall()
        conn.close()
        return {"host": CONFIG["host_id"],
                "filters": {"since": since, "until": until, "model": model,
                            "endpoint": endpoint, "client_ip": client_ip,
                            "min_duration_ms": min_dur, "max_duration_ms": max_dur,
                            "status": status, "limit": limit},
                "items": [{"timestamp": r[0], "client_ip": r[1], "method": r[2],
                           "endpoint": r[3], "status": r[4], "duration_ms": r[5],
                           "model": r[6], "prompt_tokens": r[7]} for r in rows]}

    def _query_system(self, since):
        conn = connect()
        rows = conn.execute("""
            SELECT timestamp, proc_role, host_pid, cpu_percent, rss_mib,
                   num_threads, host_load1, host_mem_used_mib
            FROM system_metrics WHERE timestamp > ? ORDER BY timestamp
        """, (since,)).fetchall()
        conn.close()
        return {"host": CONFIG["host_id"],
                "items": [{"timestamp": r[0], "proc_role": r[1], "host_pid": r[2],
                           "cpu_percent": r[3], "rss_mib": r[4], "num_threads": r[5],
                           "host_load1": r[6], "host_mem_used_mib": r[7]} for r in rows]}


def main():
    server = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"dashboard+api on http://{HOST}:{PORT}", file=sys.stderr)
    print(f"  /              dashboard (24h, ?h=N to override)", file=sys.stderr)
    print(f"  /api/health    {{ok:true}}", file=sys.stderr)
    print(f"  /api/stalls    list stall_events (?since=ISO&limit=N)", file=sys.stderr)
    print(f"  /api/stalls/<id>/stack   text stack capture", file=sys.stderr)
    print(f"  /api/requests  ollama requests (?model=&min_duration_ms=&since=&...)", file=sys.stderr)
    print(f"  /api/system    process+host metrics (?since=)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
