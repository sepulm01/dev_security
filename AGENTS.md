# AGENTS.md

## Setup & environment

- Copy `.env.example` to `.env` before building. `.env` is gitignored.
- Everything runs in Docker Compose (`docker compose up -d --build`).
- Django settings module: `config.settings`. Always set `DJANGO_SETTINGS_MODULE=config.settings` for non-service commands.

## Developer commands (run inside containers)

```bash
# Django manage.py
docker compose exec django-http python manage.py <cmd>

# Run all tests (none exist yet)
docker compose exec django-http python manage.py test

# Run a specific app's tests
docker compose exec django-http python manage.py test devices

# Lint & format
docker compose exec django-http ruff check .
docker compose exec django-http ruff format .

# Generate migrations (then restart to auto-migrate via entrypoint)
docker compose exec django-http python manage.py makemigrations

# Shell
docker compose exec django-http python manage.py shell

# Sync MediaMTX paths
docker compose exec django-http python manage.py sync_mediamtx

# Ensure Celery Beat heartbeat entry
docker compose exec django-http python manage.py ensure_heartbeat

# Logs per service
docker compose logs -f computer-vision
docker compose logs -f celery-worker
docker compose logs -f django-http
```

## Architecture

- **Monorepo with Docker Compose** (`docker-compose.yml`). Project name: `mediamtx-manager`.
- **Django 6.0** with Gunicorn (WSGI, port 8000) and Daphne (ASGI/WebSocket, port 8001), behind nginx on port 80.
- **Nine Django apps**: `devices` (core), `live` (WebSocket bridge), `ptz`, `detections` (face rec), `notifications` (Telegram), `incidents`, `operadores` (sites/guards), `monitoring` (system metrics).
- **Celery** with `DatabaseScheduler` — the orchestrator. Beat schedule is defined in `config/settings.py:CELERY_BEAT_SCHEDULE`.
- **PostgreSQL with pgvector** for face embeddings.
- **Redis** serves triple duty: Celery broker, Channels layer, DeepStream pub/sub cache.
- **MediaMTX** handles RTSP→WebRTC transcoding for browser viewing.
- **DeepStream** (C++, NVIDIA GPU) runs video analytics pipelines against camera streams. Multiple pipeline variants share a common Docker image but different config YAML files.

## Critical conventions

- **Device.username/password are the single source of truth** for ONVIF and RTSP auth. Never use hardcoded creds.
- **Stream URIs are used verbatim.** `Device.stream_uris[profile_token]` is the exact output of `MediaService.get_stream_uri()`. Never modify, split, strip, or reconstruct it. Only allowed transforms: percent-encoding `+` → `%2B` in MediaMTX URLs.
- **Only `Device.default_profile_token`** is used for DeepStream and MediaMTX. Other profiles are stored for reference.
- **DeepStream pipelines are static** — adding/removing cameras or editing analytics requires regenerating all `config*.yml` (per pipeline × per instance) + `config_nvdsanalytics.txt` and restarting the relevant `computer-vision*` container. Use `regenerate_config_and_restart()`. There are 4 pipelines, each with up to 4 instances (16 containers total). Instances without cameras are automatically stopped by the orchestrator via Docker socket. See `django/devices/README.md` for details on MAX_INSTANCES and round-robin distribution.
- **`orchestrate_cameras`** (Celery Beat every 5s, executed by celery-worker) is the unified orchestrator — ONVIF ping, FPS checks, auto-recovery. Lives in `django/devices/tasks.py`. Beat schedule defined in `config/settings.py:CELERY_BEAT_SCHEDULE`.
- **Migrations run automatically** via `docker-entrypoint.sh` with a PostgreSQL advisory lock (`pg_advisory_lock(123456)`). Manual `python manage.py migrate` is not needed normally.
- **Startup sync**: `DevicesConfig.ready()` (in `django/devices/apps.py`) spawns a daemon thread that pings all devices via ONVIF, refreshes stream URIs, syncs MediaMTX paths, regenerates DeepStream config, and triggers a pipeline restart.
- **Generated configs are gitignored**: `computer_vision/config/config*.yml` and `computer_vision/config/config_nvdsanalytics.txt` contain credentials and must never be committed.

## Testing

- No test suite exists. Use `docker compose exec django-http python manage.py test <app>`.
- GPU-dependent features (DeepStream, face rec) cannot be tested in CI without NVIDIA hardware.

## Lint / style

- Ruff with default settings (no `ruff.toml` or `pyproject.toml` config). Run inside the container.
- Django locale: Spanish (es-cl), timezone: America/Santiago.
- Frontend: Tabler CSS framework + p5.js, served from static vendor directory.

## Service map (key containers)

| Service | Role | Port |
|---------|------|------|
| nginx | Reverse proxy | 80 |
| django-http | UI + REST API (Gunicorn) | 8000 (internal) |
| django-asgi | WebSocket (Daphne) | 8001 (internal) |
| celery-beat | Orchestrator scheduler (DatabaseScheduler) | — |
| celery-worker | Executes orchestrator + all async tasks | — |
| redis-event-bridge | Redis → Channels WebSocket forwarder | — |
| notification-bridge | Notification dispatch | — |
| face-receiver | TCP server for face crops + embeddings | 12348 |
| snapshot-receiver | TCP server for camera snapshots | 12349 |
| computer-vision* | DeepStream GPU pipelines | — |
| mediamtx | RTSP/WebRTC media server | 8554, 8889, 9997 |
| event-stream-service | Dahua event HTTP streaming | — |
| postgres | Database (pgvector) | 5432 |
| redis | Cache/broker/channel layer | 6379 |

## Instancia central (VPS) — stsecurity.streetflow.cl

- Desplegada en el VPS (172.235.155.156) con `docker-compose.central.yml` (proyecto `mediamtx-central`, repo en `/opt/dev_security`).
- Servicios: postgres (pgvector), redis, django-http (gunicorn→127.0.0.1:8280), django-asgi (daphne→8281), celery-beat, celery-worker. `CENTRAL_MODE=1` desactiva la sincronización ONVIF de arranque y habilita `central.tasks.central_check`.
- Apps: `central` (Node/NodeMetric/NodeCommand/NodeAlert, API `/central/api/v1/...` con HMAC por nodo, dashboard `/central/`) y `central_node` (agente `manage.py node_agent`, contenedor `node-agent` en cada nodo).
- Registrar un nodo en el VPS: `docker compose -f docker-compose.central.yml exec django-http python manage.py register_node <slug> --name <nombre> --wg-ip <ip>` → imprime TOKEN (una sola vez).
- En cada nodo LAN: contenedor `node-agent` con env `CENTRAL_API_URL=https://stsecurity.streetflow.cl/central`, `NODE_SLUG`, `NODE_TOKEN`; comandos remotos soportados: `get_state`, `sync_config`, `sync_mediamtx`, `restart_pipeline`.
- Rebuild con versión: `GIT_COMMIT=$(git rev-parse --short HEAD) docker compose -f docker-compose.central.yml build` (arg GIT_COMMIT → env NODE_VERSION).
- WireGuard hub-and-spoke existente: VPS `10.10.20.2/24`, nodos `10.10.20.1`, `.4`, `.5`.
