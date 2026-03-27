#!/usr/bin/env python3
"""Erzeugt ein interaktives HTML-Dashboard aus den SQLite-Monitoring-Daten."""

import sqlite3
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

DB_PATH = Path(__file__).parent / "monitor.db"
OUT_PATH = Path(__file__).parent / "dashboard.html"


def query_gpu_timeseries(conn, since):
    """GPU-Metriken als Zeitreihe."""
    rows = conn.execute("""
        SELECT timestamp, gpu_id, gpu_name, vram_used_mib, vram_total_mib,
               utilization_gpu, temperature, power_draw_w
        FROM gpu_metrics WHERE timestamp > ?
        ORDER BY timestamp
    """, (since,)).fetchall()

    gpus = {}
    for ts, gid, name, vram_used, vram_total, util, temp, power in rows:
        if gid not in gpus:
            gpus[gid] = {"name": name, "vram_total": vram_total,
                         "ts": [], "vram": [], "util": [], "temp": [], "power": []}
        gpus[gid]["ts"].append(ts)
        gpus[gid]["vram"].append(vram_used)
        gpus[gid]["util"].append(util)
        gpus[gid]["temp"].append(temp)
        gpus[gid]["power"].append(power)
    return gpus


def query_requests_timeseries(conn, since):
    """Requests aggregiert pro Stunde."""
    rows = conn.execute("""
        SELECT substr(timestamp, 1, 13) as hour,
               COUNT(*) as cnt,
               COUNT(DISTINCT model) as models
        FROM ollama_requests WHERE timestamp > ?
        GROUP BY hour ORDER BY hour
    """, (since,)).fetchall()
    return {"hours": [r[0] for r in rows],
            "counts": [r[1] for r in rows],
            "models": [r[2] for r in rows]}


def query_model_distribution(conn, since):
    """Modell-Verteilung als Pie-Daten."""
    rows = conn.execute("""
        SELECT COALESCE(model, 'unbekannt'), COUNT(*) as cnt
        FROM ollama_requests WHERE timestamp > ? AND endpoint IN ('/api/chat', '/api/generate')
        GROUP BY model ORDER BY cnt DESC LIMIT 10
    """, (since,)).fetchall()
    return {"labels": [r[0] for r in rows], "counts": [r[1] for r in rows]}


def query_prompt_sizes(conn, since):
    """Prompt-Token-Verteilung ueber Zeit."""
    rows = conn.execute("""
        SELECT timestamp, model, prompt_tokens, duration_ms
        FROM ollama_requests
        WHERE timestamp > ? AND prompt_tokens IS NOT NULL
        ORDER BY timestamp
    """, (since,)).fetchall()
    return {"ts": [r[0] for r in rows], "model": [r[1] for r in rows],
            "tokens": [r[2] for r in rows], "duration": [r[3] for r in rows]}


def generate_html(hours):
    if not DB_PATH.exists():
        print(f"Fehler: {DB_PATH} nicht gefunden.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    gpu_data = query_gpu_timeseries(conn, since)
    req_data = query_requests_timeseries(conn, since)
    model_data = query_model_distribution(conn, since)
    prompt_data = query_prompt_sizes(conn, since)
    conn.close()

    label = f"{hours}h" if hours < 48 else f"{hours // 24}d"
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ollama Monitor Dashboard ({label})</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f1117; color: #e0e0e0; padding: 16px; }}
  h1 {{ font-size: 1.3em; margin-bottom: 4px; color: #fff; }}
  .meta {{ font-size: 0.85em; color: #888; margin-bottom: 16px; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .card {{ background: #1a1d27; border-radius: 8px; padding: 16px; }}
  .card h2 {{ font-size: 1em; color: #aaa; margin-bottom: 8px; }}
  .full {{ grid-column: 1 / -1; }}
  canvas {{ max-height: 280px; }}
  .nav {{ margin-bottom: 16px; display: flex; gap: 8px; }}
  .nav a {{ color: #60a5fa; text-decoration: none; padding: 4px 12px;
            border: 1px solid #333; border-radius: 4px; font-size: 0.9em; }}
  .nav a:hover {{ background: #1e293b; }}
  .nav a.active {{ background: #1e40af; border-color: #1e40af; color: #fff; }}
  @media (max-width: 768px) {{ .grid {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<h1>Ollama Monitor Dashboard</h1>
<p class="meta">Zeitraum: {label} | Erzeugt: {generated}</p>
<div class="nav">
  <a href="#" onclick="regen(1)" {{'class="active"' if hours == 1 else ''}}>1h</a>
  <a href="#" onclick="regen(6)">6h</a>
  <a href="#" onclick="regen(24)" {{'class="active"' if hours == 24 else ''}}>24h</a>
  <a href="#" onclick="regen(168)">7d</a>
  <a href="#" onclick="regen(720)">30d</a>
</div>
<div class="grid">
  <div class="card full"><h2>GPU VRAM Auslastung (MiB)</h2><canvas id="vramChart"></canvas></div>
  <div class="card"><h2>GPU Auslastung %</h2><canvas id="utilChart"></canvas></div>
  <div class="card"><h2>GPU Temperatur / Power</h2><canvas id="tempChart"></canvas></div>
  <div class="card"><h2>Requests pro Stunde</h2><canvas id="reqChart"></canvas></div>
  <div class="card"><h2>Modell-Verteilung</h2><canvas id="modelChart"></canvas></div>
  <div class="card full"><h2>Prompt-Groesse & Dauer</h2><canvas id="promptChart"></canvas></div>
</div>
<script>
const COLORS = ['#60a5fa', '#f472b6', '#34d399', '#fbbf24', '#a78bfa', '#fb923c'];
const GPU = {json.dumps(gpu_data, default=str)};
const REQ = {json.dumps(req_data)};
const MODEL = {json.dumps(model_data)};
const PROMPT = {json.dumps(prompt_data)};

Chart.defaults.color = '#888';
Chart.defaults.borderColor = '#2a2d3a';
Chart.defaults.font.size = 11;

function timeAxis() {{
  return {{ type: 'time', time: {{ tooltipFormat: 'dd.MM HH:mm', displayFormats: {{ hour: 'HH:mm', day: 'dd.MM' }} }},
           ticks: {{ maxTicksLimit: 12 }} }};
}}

// VRAM Chart
const vramDs = [];
Object.entries(GPU).forEach(([id, g], i) => {{
  vramDs.push({{
    label: g.name + ' (VRAM)',
    data: g.ts.map((t, j) => ({{ x: t, y: g.vram[j] }})),
    borderColor: COLORS[i], backgroundColor: COLORS[i] + '20',
    fill: true, tension: 0.2, pointRadius: 0
  }});
  vramDs.push({{
    label: g.name + ' (Total)',
    data: g.ts.map(t => ({{ x: t, y: g.vram_total }})),
    borderColor: COLORS[i] + '60', borderDash: [5, 5],
    pointRadius: 0, fill: false
  }});
}});
new Chart('vramChart', {{ type: 'line', data: {{ datasets: vramDs }},
  options: {{ scales: {{ x: timeAxis(), y: {{ beginAtZero: true }} }}, plugins: {{ legend: {{ position: 'bottom' }} }} }} }});

// Utilization Chart
const utilDs = [];
Object.entries(GPU).forEach(([id, g], i) => {{
  utilDs.push({{
    label: g.name,
    data: g.ts.map((t, j) => ({{ x: t, y: g.util[j] }})),
    borderColor: COLORS[i], tension: 0.2, pointRadius: 0
  }});
}});
new Chart('utilChart', {{ type: 'line', data: {{ datasets: utilDs }},
  options: {{ scales: {{ x: timeAxis(), y: {{ beginAtZero: true, max: 100 }} }}, plugins: {{ legend: {{ position: 'bottom' }} }} }} }});

// Temp + Power Chart
const tempDs = [];
Object.entries(GPU).forEach(([id, g], i) => {{
  tempDs.push({{
    label: g.name + ' Temp',
    data: g.ts.map((t, j) => ({{ x: t, y: g.temp[j] }})),
    borderColor: COLORS[i], tension: 0.2, pointRadius: 0, yAxisID: 'y'
  }});
  tempDs.push({{
    label: g.name + ' Power',
    data: g.ts.map((t, j) => ({{ x: t, y: g.power[j] }})),
    borderColor: COLORS[i + 2], borderDash: [3, 3], tension: 0.2, pointRadius: 0, yAxisID: 'y1'
  }});
}});
new Chart('tempChart', {{ type: 'line', data: {{ datasets: tempDs }},
  options: {{ scales: {{ x: timeAxis(),
    y: {{ beginAtZero: true, position: 'left', title: {{ display: true, text: 'Temp C' }} }},
    y1: {{ beginAtZero: true, position: 'right', title: {{ display: true, text: 'Watt' }}, grid: {{ drawOnChartArea: false }} }}
  }}, plugins: {{ legend: {{ position: 'bottom' }} }} }} }});

// Requests Chart
if (REQ.hours.length > 0) {{
  new Chart('reqChart', {{ type: 'bar', data: {{
    labels: REQ.hours.map(h => h.replace('T', ' ')),
    datasets: [{{ label: 'Requests', data: REQ.counts, backgroundColor: '#60a5fa80' }}]
  }}, options: {{ scales: {{ x: {{ ticks: {{ maxTicksLimit: 12 }} }} }},
    plugins: {{ legend: {{ display: false }} }} }} }});
}}

// Model Pie
if (MODEL.labels.length > 0) {{
  new Chart('modelChart', {{ type: 'doughnut', data: {{
    labels: MODEL.labels,
    datasets: [{{ data: MODEL.counts, backgroundColor: COLORS.slice(0, MODEL.labels.length) }}]
  }}, options: {{ plugins: {{ legend: {{ position: 'right' }} }} }} }});
}}

// Prompt Size + Duration Scatter
if (PROMPT.ts.length > 0) {{
  new Chart('promptChart', {{ type: 'scatter', data: {{
    datasets: [{{
      label: 'Prompt Tokens vs Dauer',
      data: PROMPT.ts.map((t, i) => ({{ x: PROMPT.tokens[i], y: PROMPT.duration[i] / 1000 }})),
      backgroundColor: COLORS.map(c => c + '80'),
      pointRadius: 5
    }}]
  }}, options: {{ scales: {{
    x: {{ title: {{ display: true, text: 'Prompt Tokens' }} }},
    y: {{ title: {{ display: true, text: 'Dauer (Sekunden)' }} }}
  }} }} }});
}}

function regen(h) {{
  window.location.href = '/?h=' + h;
}}
</script>
</body>
</html>"""

    OUT_PATH.write_text(html)
    print(f"Dashboard erzeugt: {OUT_PATH}")
    print(f"Oeffnen: http://<server-ip>:3000 (neben WebUI) oder lokal: file://{OUT_PATH}")


def main():
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    generate_html(hours)


if __name__ == "__main__":
    main()
