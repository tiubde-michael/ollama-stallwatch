# Ollama + Open WebUI with built-in stall monitoring

A reproducible Docker deployment of **[Ollama](https://ollama.com/)** (local LLM
inference) and **[Open WebUI](https://openwebui.com/)** (chat/RAG frontend),
plus a lightweight Python monitoring stack that detects a specific failure
mode where Ollama's main process becomes CPU-bound while the GPU sits idle —
the kind of hang that's only visible live on `nvtop` and otherwise invisible
in logs.

If you're running Ollama on GPU hosts and have ever wondered "why is VRAM
loaded but the GPU isn't doing anything?", this is for you.

## What's in here

```
.
├── docker-compose.yml      Ollama + Open WebUI services with sane defaults
├── .env.example            Template for the per-host environment
├── monitoring/             Python collectors + REST API + dashboard
│   ├── collectors/         GPU, process, log, and stall-detection workers
│   ├── api/                HTTP server (port 3002) with dashboard + REST
│   ├── config.example.toml Template (install.sh creates config.toml from this)
│   └── install.sh          Idempotent, host-detecting install script
├── CLAUDE.md               Developer notes (architecture, common commands)
└── LICENSE                 MIT
```

## Hardware / OS assumptions

- Linux host (tested on Ubuntu 24.04, kernel 6.8).
- Docker + docker compose v2.
- One or more NVIDIA GPUs with the [NVIDIA Container
  Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)
  installed (`runtime: nvidia` in compose). CPU-only mode is not configured
  out of the box.
- Python 3.11+ (for `tomllib`) on the host for the monitoring stack.
- `nvidia-smi`, `gdb`, and `sudo` available on the host (gdb only needed
  for stall-detection stack capture; install.sh degrades gracefully if
  any of these are missing).

## Quick start

```bash
git clone https://github.com/tiubde-michael/ollama-stallwatch.git
cd ollama-stallwatch

# 1. Copy the env template and fill in your values.
cp .env.example .env
$EDITOR .env

# 2. Bring up Ollama + Open WebUI.
docker compose up -d

# 3. Install the monitoring stack (writes systemd units + a small sudoers rule).
sudo monitoring/install.sh

# Open the dashboard:
xdg-open http://localhost:3002/
```

## Monitoring at a glance

The monitoring stack writes everything to a single SQLite file
(`monitoring/monitor.db`, WAL mode) and exposes it both as a
self-contained HTML dashboard (Chart.js) and as a small REST API.

**Dashboard:** `http://<host>:3002/` (override window with `?h=N` hours).

**REST API** (read-only, no auth):

| Endpoint | What it returns |
|---|---|
| `GET /api/health` | `{ok: true, host: ...}` |
| `GET /api/stalls?...` | Stall events (filters below) |
| `GET /api/stalls/<id>/stack` | Plain-text stack capture (gdb + /proc) |
| `GET /api/requests?...` | Filtered request history |
| `GET /api/system?since=ISO` | CPU/RAM time series for `ollama serve` and runner |
| `GET /api/gpu/live` | Fresh `nvidia-smi` snapshot (always current) |
| `GET /api/gpu/series?since=&gpu_id=` | GPU metrics time series (delta-logged) |

`/api/stalls` filters (combinable):
- `since=ISO` — events starting after this timestamp (default: epoch)
- `at=ISO` — events that were active at this timestamp
- `overlapping=START,END` — events overlapping this time range
- `confidence=strict|loose|ghost` — by detection class
- `mode=A|B` — `A` = stream-then-stall (GPU was active), `B` = silent (GPU never produced output)
- `client_ip=<ip>` — best-guess client of the stalling request
- `limit=N` — default 100

`/api/requests` filters: `model`, `since`, `until`, `endpoint`, `client_ip`, `min_duration_ms`, `max_duration_ms`, `status`, `limit`.

**Stall detector** classifies hangs into three confidence levels, run in parallel:

| Confidence | Trigger pattern (per GPU) |
|---|---|
| `strict` | `vram>1GiB AND util<=5% AND power<=50W AND serve_cpu>=50%` for 30s continuous |
| `loose` | same shape with `power<=75W`, satisfied for 80% of the last 30s sliding window |
| `ghost` | `vram>1GiB AND util<=5% AND request_active AND serve_cpu<50%` for 30s — opposite signature: Ollama took the request but neither GPU nor CPU is doing anything |

Each event is also tagged with **`mode`**: `A` if GPU did decode work in the 60s before the stall (recoverable — partial output may be salvageable), `B` if it never did (silent — nothing to recover). On stall open, the detector dumps `/proc/<pid>/task/*/{stack,wchan,stat}` plus a `gdb -batch thread apply all bt` to `monitoring/stalls/<ts>.txt` and links it from the dashboard.

See `CLAUDE.md` for table schemas and per-collector details.

## Security model

`monitoring/install.sh` makes a few changes to your host:

- **Five systemd services** under `/etc/systemd/system/ollama-*.service`
  (gpu logger, log parser, process logger, stall detector, dashboard).
- **One sudoers entry** at `/etc/sudoers.d/ollama-monitor`. It allows
  `root` to run *exactly* these two commands without a password:
  - `gdb -batch -nx -ex 'set pagination off' -ex 'thread apply all bt' -p <pid>`
  - `head -50 /proc/*`

  These are needed by the stall detector to capture diagnostic stacks of
  `ollama serve` from outside the container (no container modification
  required). The sudoers entry is wildcard-restricted to those exact
  invocations — review the file if your threat model is strict.
- **One cron file** at `/etc/cron.d/ollama-monitor-retention` that prunes
  rows and stack files older than 30 days at 03:00 daily.

To uninstall:

```bash
sudo systemctl disable --now ollama-{gpu-logger,log-parser,process-logger,stall-detector,dashboard}.service
sudo rm /etc/systemd/system/ollama-*.service
sudo rm /etc/sudoers.d/ollama-monitor /etc/cron.d/ollama-monitor-retention
sudo systemctl daemon-reload
```

The dashboard binds to `0.0.0.0:3002` by default; restrict via firewall
or change `[api].host` in `monitoring/config.toml` if you don't want it
on the LAN.

## Portability

`install.sh` is host-detecting:
- No `nvidia-smi` → no GPU logger or stall detector services are installed.
- No `docker` → no log parser or process logger services are installed.
- The dashboard always runs.

To deploy on another host: clone, copy `.env.example` → `.env`, edit,
`docker compose up -d`, `sudo monitoring/install.sh`. The install script
will create `monitoring/config.toml` from the template and seed `host_id`
from `hostname -s`.

## License

MIT — see [LICENSE](LICENSE).
