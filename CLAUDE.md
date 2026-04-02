# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Docker-based deployment of **Open WebUI** (v0.6.43) with **Ollama** (v0.17.6) as a local LLM inference backend. The setup runs on an NVIDIA RTX 3090 Ti (24 GB VRAM) + RTX 5060 Ti (16 GB VRAM) with GPU passthrough.

- **Open WebUI**: Full-stack AI chat/RAG web application (Python/FastAPI backend + TypeScript/SvelteKit frontend)
- **Ollama**: Local LLM inference engine with NVIDIA CUDA support
- **Networking**: Both containers communicate over a custom Docker bridge network (`openllm-net`)

## Common Commands

### Docker Operations

```bash
cd /srv/Container
docker compose up -d                # Start all services
docker compose down                 # Stop all services
docker compose logs -f openwebui    # Follow Open WebUI logs
docker compose logs -f ollama       # Follow Ollama logs
docker compose restart openwebui    # Restart Open WebUI
docker exec -it openwebui bash      # Shell into Open WebUI container
docker exec -it ollama bash         # Shell into Ollama container
```

### Ollama Model Management

```bash
docker exec ollama ollama list          # List installed models
docker exec ollama ollama pull <model>  # Download a model
docker exec ollama ollama rm <model>    # Remove a model
docker exec ollama ollama ps            # Show running models (VRAM usage)
```

### Inside the Open WebUI Container

```bash
# Backend (Python/FastAPI)
cd /app/backend
python -m pytest                        # Run all backend tests
python -m pytest test/apps/webui/routers/test_chats.py  # Run single test file
python -m pytest -k "test_name"         # Run test by name

# Frontend (TypeScript/SvelteKit)
cd /app
npm run dev                             # Dev server with HMR
npm run build                           # Production build
npm run test:frontend                   # Vitest unit tests
npm run lint                            # All lints (frontend + types + backend)
npm run lint:frontend                   # ESLint
npm run lint:types                      # Svelte type checking
npm run lint:backend                    # Pylint
npm run format                          # Prettier (frontend)
npm run format:backend                  # Black (backend)
```

## Architecture

### Container Layout

```
/srv/Container/
├── docker-compose.yml       # Service orchestration (ollama + openwebui)
├── .env                     # Bind IPs, GPU UUID, ports, project name
├── ollama/                  # Ollama data volume (models, history)
├── openwebui/               # Open WebUI data volume (DB, uploads, cache, vector_db)
└── monitoring/              # Lightweight monitoring (GPU + Ollama logs -> SQLite)
    ├── monitor.db           # SQLite DB (gpu_metrics + ollama_requests, 30d retention)
    ├── gpu_logger.py        # nvidia-smi delta-logger (systemd: ollama-gpu-logger)
    ├── log_parser.py        # Ollama docker-log parser (systemd: ollama-log-parser)
    ├── report.py            # CLI reporting tool (summary, models, gpu, clients, busy, status)
    ├── dashboard.py         # HTML-Dashboard-Generator (Chart.js)
    ├── dashboard.html       # Generierte Dashboard-Seite (nicht editieren)
    └── serve.py             # Mini-Webserver fuer Dashboard (systemd: ollama-dashboard, Port 3002)
```

### Open WebUI Internal Structure (inside container at /app)

**Backend** (`/app/backend/open_webui/`):
- `main.py` — FastAPI app entry point, mounts all routers
- `config.py` / `env.py` — Configuration and environment variable handling
- `routers/` — API endpoint handlers (~25 modules: chats, auths, models, files, knowledge, ollama, openai, retrieval, audio, images, etc.)
- `models/` — SQLAlchemy ORM models (~18 modules: users, chats, messages, files, knowledge, tools, functions, etc.)
- `socket/main.py` — Socket.IO WebSocket server for real-time updates
- `utils/middleware.py` — Request/response middleware (handles streaming proxy to LLM backends)
- `retrieval/` — RAG pipeline: document loaders, embedding models, vector store backends, web scrapers
- `utils/tools.py` — Tool/function execution engine
- `utils/mcp/` — Model Context Protocol support
- `internal/db.py` — SQLAlchemy engine setup; supports SQLite (default), PostgreSQL, MySQL
- `migrations/` — Alembic database migrations

**Frontend** (`/app/src/` — SvelteKit):
- Svelte 5 + SvelteKit with Vite
- Tailwind CSS 4, TipTap rich text editor, CodeMirror code editor
- Socket.IO client for real-time chat streaming
- i18next for localization

### Key Architectural Patterns

- **Multi-provider LLM abstraction**: Unified API layer proxying to Ollama, OpenAI, Anthropic, Google, Azure
- **RAG pipeline**: Document ingestion → chunking → Sentence-Transformers embeddings → ChromaDB vector store → BM25 + reranking
- **Plugin system**: User-uploadable Python functions/tools executed in RestrictedPython sandbox
- **Auth**: JWT sessions, OAuth2 (via Authlib), optional LDAP, role-based access control with user groups

## Configuration

Key environment variables (set in `.env` or `docker-compose.yml`):

| Variable | Current Value | Purpose |
|---|---|---|
| `OLLAMA_CONTEXT_LENGTH` | 32768 | Global context window for all Ollama models |
| `OLLAMA_NUM_PARALLEL` | 1 | Concurrent request slots per model |
| `OLLAMA_MAX_LOADED_MODELS` | 2 | Max models in VRAM simultaneously |
| `OLLAMA_DEBUG` | 1 | Detailed request logging (model, tokens, timings) |
| `OLLAMA_GPU_OVERHEAD` | 2147483648 | 2 GiB VRAM reserved for system/driver overhead |
| `OPENWEBUI_PORT` | 3000 | External port mapped to container's 8080 |
| `OLLAMA_BASE_URL` | http://ollama:11434 | Internal Docker network URL for Ollama |

## Monitoring

Lightweight local monitoring via Python scripts + SQLite (no Prometheus/Grafana).

### Web-Dashboard (Port 3002)

```
http://<server-ip>:3002          # 24h (default)
http://<server-ip>:3002/?h=1     # letzte Stunde
http://<server-ip>:3002/?h=24    # letzte 24 Stunden
http://<server-ip>:3002/?h=168   # letzte 7 Tage
http://<server-ip>:3002/?h=720   # letzte 30 Tage
```

Charts: GPU VRAM, GPU Auslastung %, Temperatur/Power, Requests/Stunde, Modell-Verteilung, Prompt-Tokens vs Dauer.
Erreichbar im LAN (192.168.5.0/24) und ueber Tailscale.

### CLI Reports

```bash
python3 /srv/Container/monitoring/report.py summary      # Uebersicht letzte 24h
python3 /srv/Container/monitoring/report.py models        # Modell-Nutzung (7d)
python3 /srv/Container/monitoring/report.py gpu           # GPU-Metriken (24h)
python3 /srv/Container/monitoring/report.py clients       # Client-IPs (7d)
python3 /srv/Container/monitoring/report.py busy          # Busiest Stunden (7d)
python3 /srv/Container/monitoring/report.py status        # DB-Status
# Alle Befehle akzeptieren optionalen Stunden-Parameter: report.py gpu 48
```

### Systemd Services

```bash
sudo systemctl status ollama-gpu-logger    # nvidia-smi alle 10s, delta-only
sudo systemctl status ollama-log-parser    # Folgt Ollama Docker-Logs
sudo systemctl status ollama-dashboard     # Web-Dashboard auf Port 3002
```

### Datenbank-Schema

**`gpu_metrics`**: timestamp, gpu_id, gpu_name, vram_used_mib, vram_total_mib, utilization_gpu, temperature, power_draw_w
**`ollama_requests`**: timestamp, client_ip, method, endpoint, status, duration_ms, model, prompt_tokens

### Details

- **GPU Logger**: Pollt `nvidia-smi` alle 10s, schreibt nur bei Wertaenderung (Delta-Logging)
- **Log Parser**: Folgt `docker compose logs ollama`, extrahiert Modell, Prompt-Tokens, Dauer, Client-IP. Erkennt sowohl offizielle Modelle (`library/`) als auch Community-Modelle (z.B. `alibayram/medgemma`).
- **Retention**: Cronjob loescht Eintraege aelter 30 Tage (`/etc/cron.d/ollama-monitor-retention`)
- **DB**: `/srv/Container/monitoring/monitor.db` (SQLite WAL mode)
- **Dashboard**: Chart.js, wird bei jedem Seitenaufruf live aus SQLite generiert

## Notes

- Comments in `docker-compose.yml` are in German and document deviations from default/previous configurations.
- Image versions are pinned (not `:latest`) for controlled updates.
- The Open WebUI database (SQLite) lives at `/srv/Container/openwebui/webui.db`.
- Vector DB data persists at `/srv/Container/openwebui/vector_db/`.
- `ollama_admin` user has passwordless sudo configured via `/etc/sudoers.d/ollama_admin`.
