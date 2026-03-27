#!/usr/bin/env python3
"""Mini-Webserver fuer das Monitoring-Dashboard. Regeneriert bei jedem Aufruf."""

import http.server
import subprocess
import sys
from pathlib import Path

PORT = 3002
DIR = Path(__file__).parent
DASHBOARD = DIR / "dashboard.py"
HTML = DIR / "dashboard.html"


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIR), **kwargs)

    def do_GET(self):
        # Bei / oder /dashboard.html: Dashboard mit gewuenschtem Zeitraum neu generieren
        path = self.path.split("?")[0]
        if path in ("/", "/dashboard.html"):
            # Zeitraum aus Query-Parameter: ?h=1, ?h=24, ?h=168
            hours = "24"
            if "?" in self.path:
                for param in self.path.split("?")[1].split("&"):
                    if param.startswith("h="):
                        hours = param[2:]

            subprocess.run(
                [sys.executable, str(DASHBOARD), hours],
                capture_output=True, timeout=10
            )
            self.path = "/dashboard.html"

        return super().do_GET()


def main():
    server = http.server.HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"Dashboard-Server laeuft auf http://0.0.0.0:{PORT}")
    print(f"  1h:  http://localhost:{PORT}/?h=1")
    print(f"  24h: http://localhost:{PORT}/?h=24")
    print(f"  7d:  http://localhost:{PORT}/?h=168")
    print(f"  30d: http://localhost:{PORT}/?h=720")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
