# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A task/project management web app for a team (Kanban + Scrum boards, calendar/agenda, wiki, notes, whiteboard, SLA, person-to-person notes, activity walls, API). This repo is **Odin** (formerly Aris), our own installation: it started as a full-history copy of the company-neutral base (larceing/APP_TAREAS_BASE) and takes its improvements by merging `base/main` (see README). Keep it company-neutral in code all the same: no company data or integrations baked in. Everything that changes between installations lives in `app_fastapi/.env` (branding, login mode, optional modules), and the install comes with demo data. User docs live in `docs/`. Docs and UI text are in Spanish.

## Commands

```bash
# Setup (no Docker)
python -m venv .venv && .venv/bin/pip install -r app_fastapi/requirements.txt
cp app_fastapi/.env.example app_fastapi/.env          # DB_*, SESSION_SECRET_KEY
python scripts/instalar.py                            # creates DBs + schema + admin + demo data (idempotent)

# Run (from the REPO ROOT -- app_fastapi is a package with relative imports)
python -m uvicorn app_fastapi.main:app --port 8000 [--reload --reload-dir app_fastapi]

# Docker (MariaDB included unless COMPOSE_PROFILES is emptied in .env)
cp .env.example .env && docker compose up -d --build

# Tests (write only to DB_NAME_TEST)
python -m unittest app_fastapi.tests -v
python -m unittest app_fastapi.tests.ConectoresSincronizarTests -v

# Admin scripts
python scripts/usuarios.py crear|password|it|activar|listar ...
python scripts/instalar.py --recrear --si [--sin-demo] [--sin-test]   # DROPS and recreates
python scripts/nexus_indexar.py <folder> [--tecnico-auto] [--limpiar]
python scripts/nexus_fichas.py paginas|ficha|enlazar|relacionar|relacionados ...
```

There is no configured linter/formatter — don't invent one.

## Architecture

### No ORM — raw SQL over MariaDB

`app_fastapi/db.py` is the entire data layer: PyMySQL connections and hand-written queries. No SQLAlchemy, ever. New tables go in `scripts/schema_app_tareas.sql` (the single source of truth for a fresh install — `instalar.py` splits it on `;`, so never put a `;` inside a comment or string literal there). For an existing install a schema change also needs a one-off `scripts/migrar_<thing>.py`.

### Installation-level config: `config.py`

`config.py` reads `.env` on every call (never cached): `app_nombre()/app_empresa()/app_logo()/app_tema_css()/app_url_portal()` (branding: name, logo, an extra CSS file loaded after `main.css`, a "back to portal" link; exposed as Jinja globals), `auth_modo()` (`local`|`keycloak`|`sso`), `app_version()` and `modulo_activo(name)` for the optional modules `mantis`, `nexus`, `escritorio`, `conectores` — all **off by default**. A disabled module: its router is not mounted (`main.py`), its menu entries and Configuración tabs are hidden (`modulo_activo()` in templates, `_MODULO_DE_TAB` in `routers/configuracion.py`), and its background threads don't start. Any new optional feature should follow this same pattern. Never hardcode a company name, path, server or person anywhere.

### The real/test entorno toggle

`db.py::entorno_actual()` / `set_entorno()` read/write `data/entorno_db.txt`. Every connection checks it fresh and goes to `DB_NAME` (real) or `DB_NAME_TEST`. Global for the whole app, toggled from the user menu or `POST /entorno/cambiar/`.

### Testing: entorno isolation is mandatory

`tests.py::EntornoAisladoTestCase` forces `entorno='test'` and restores it afterwards. Any test touching data must subclass it and clean up what it creates (unique ids, `TEST_PREFIX = "AUTOTEST_"`). `db.eliminar_proyecto()` does *not* delete a project's tasks (it reassigns them to `general`) — delete them explicitly. `tests.py` turns every `MODULO_*` on before importing `main`. External services (Keycloak, Mantis, connector sources) are always mocked.

### Auth

`auth.py`: `AUTH_MODO=local` checks `auth_user` with Django-compatible `pbkdf2_sha256` hashes (plain `hashlib`; `hash_contrasena()` creates them, `scripts/usuarios.py` manages users); IT = `is_staff`. `AUTH_MODO=keycloak` uses the Direct Access Grant against `KEYCLOAK_ISSUER`; IT = Keycloak group `IT`; first login auto-creates the `auth_user` row. `AUTH_MODO=sso` is the Authorization Code flow (`/login/` redirects to Keycloak, back to `/login/sso/`, `APP_URL_PUBLICA` builds the redirect URI) so one Keycloak login covers every app of the realm. With `KEYCLOAK_GRUPO_APP=Tareas` only `/Tareas` and its subgroups get in and `/Tareas/Admin` is IT for this app; `KEYCLOAK_GRUPO_ADMIN_GLOBAL` (default `IT`) enters and administers every app (`auth.acceso_por_grupos`). `auth_user` has **no column defaults** on purpose (same as Django's) — every INSERT must give all columns. Session = signed cookie (`SessionMiddleware`, 90 days). `requiere_login` / `requiere_it` dependencies.

### CSRF

`csrf.py`, global dependency in `main.py`. Validates POST only; `/api/*` is exempt (`X-API-Key`). Cookie `gestor_csrftoken`, header `X-CSRFToken`, form field via `csrf_field()`.

### Templates

One shared `Jinja2Templates` in `templating.py` — never create a second one. Globals: `icon()`, `csrf_field()`, `get_flashed_messages()`, `entorno_actual()`, `app_nombre()`, `app_logo()`, `modulo_activo()`, `es_it()`, sidebar helpers. Shared chrome CSS in `static/css/main.css`; each page ships its own `<style>` inline — before adding a class name there, check it doesn't collide with `main.css` (a local `.header-view-btn` once broke the whole header). Icons: `icons.py`, inline SVG dict.

### Boards

`columnas_tablero` rows are per project and renameable; a task's `estado` stores the column name (no enum). `load_board_columns()` lazily creates the 4 defaults. "Done" columns: `es_done`.

### Espacios and permissions

Every user has one personal espacio (`get_or_create_espacio_personal`, called on `/`), plus shared espacios with members `admin|editor|lector` and per-espacio editor switches. `db.puede_actuar_en_proyecto()` is the single permission check for actions inside a project. **Visibility is not filtered**: anyone logged in sees every project.

### Acciones — one place per action (`acciones.py`)

Creating/editing/moving/assigning/deleting/restoring a task, writing on a wall and sending a Buzón note each live in one function in `acciones.py`: validate → save → bell notification → wall event. It takes an `Actor` and is used by web routers, the API **and the connectors**. Never write tasks with `db.crear_tarea` directly from a router or the wall and notifications silently stop seeing it. Bulk imports pass `publicar=False` and post one `importar_resumen`.

### Messaging: Buzón, walls, bell — no chats

Buzón (`routers/buzon.py`, `buzon_notas`) = person-to-person notes with due date/reminders, convertible to tasks. Walls (`muro.py`, `routers/muro.py`, `chat_*` tables) = one per espacio, filtered per project; system events published from `acciones.py`; IT "Avisos". Bell (`notificaciones`, polled every 15 s). There are deliberately **no direct/group chats** — don't add them without asking.

### Agenda

`routers/agenda.py` (`/agenda/<fecha>/`): tasks due that day + notes/whiteboard with `agenda_fecha`. Calendar and home page reuse the same `db.py` queries.

### External API (`routers/api_externa.py`)

Built for agents: `GET /api/manual` serves `docs/api/Manual_API_App_Tareas.md` (keep it in sync with the endpoints — it is the agent's only documentation). Tokens have an owner and the API acts as that person.

### Optional module: Conectores (`app_fastapi/conectores/`)

Inbound connectors turn external items into tasks. `base.py` (contract: `Conector.traer(config) -> list[Elemento]`, `Campo` drives the config form), one file per type (`rest_generico.py`, `mantis.py`), registry `TIPOS` in `__init__.py`, `motor.py` does everything else (secret fields AES-GCM-encrypted with `CONECTORES_CLAVE`/`SESSION_SECRET_KEY`, dedupe via `conector_elementos`, `acciones.crear_tarea`, background thread every 60 s, paused in the test entorno). UI: `routers/conectores.py` + `templates/_config_conectores.html`. See `docs/CONECTORES.md`.

### Optional module: Nexus (`app_fastapi/nexus/`, `routers/nexus.py`)

Document search + Union-Find relation graph (`nexus_nodos/_relaciones/_historial`; node hooks inside the `crear_*/eliminar_*` functions; `db.nexus_sincronizar()` repairs orphans) + file catalog (`nexus_carpetas/_archivos`, filled by `scripts/nexus_indexar.py`) + fichas (`wiki_pagina_metadata`) + encrypted credentials. No UI yet to create relations/fichas (`scripts/nexus_fichas.py` meanwhile). See `docs/NEXUS_ESTRUCTURA_DE_GRAFOS.md` and `docs/NEXUS_PLAN_DE_ACCION.md`.

### Optional module: Escritorio (`escritorio_servicios.py`)

Background threads started from `main.py::lifespan`: Recordatorio (break reminders), Vigía de carpeta (generic folder watcher: failed/stuck files, lost access → Buzón note + bell + optional toast; pure diff logic in `evaluar_vigia`) and, with Mantis, the Vigilante. Config re-read from the DB every 15 s. Always the REAL DB (`real=True`); tests set `USAR_BD_REAL = False`. One instance via a file lock (`msvcrt`/`fcntl`); `APP_TAREAS_SIN_ESCRITORIO=1` disables it per process. Toasts (`winotify`) are Windows-only.

### Optional module: Mantis (`mantis_client.py`, `routers/mantis.py`)

Full MantisBT integration with one account from `.env` (`MANTIS_*`): import, move tickets, SLA origin breakdown, Vigilante. `MANTIS_REPORTERO_IA` optionally names a bot account whose tickets are counted apart.

### Deployment by environment (`despliegue/`, `.github/workflows/despliegue.yml`)

DEV/PRO on one server: push to `main` → tests → image `ghcr.io/<repo>:sha-xxxxxxx` → DEV; a published release `vX.Y.Z` → PRO after approval (GitHub environment `pro`); `workflow_dispatch` puts any built version (or `anterior`) in either. `despliegue/servidor/tareas.sh` does the server side (prepare, deploy with health check and auto-rollback, rollback, DB copies, `importar` of an older install, `keycloak` for sso); GitHub reaches it over SSH with a key restricted to `tareas.sh ci`. Its compose file is copied into the image (`Dockerfile`) and extracted per version; each PRO deploy also installs the image's `tareas.sh` (`actualizar_script`). Each environment is the container `tareas-<env>:8000` on the proxy network; what sits in front (a path-based entrada, the portada, a no-terminal panel, own domains) belongs to the server, not to this app — the app only learns its path from `X-Forwarded-Prefix` or `APP_RUTA_BASE` (`ruta_base.py`, never build URLs with the prefix by hand) and its public addresses from `APP_URL_PUBLICA`/`APP_URLS_ALTERNATIVAS` (`auth.url_publica()` picks the one the request came by). Domains, hosts and keys only live in GitHub secrets/variables and on the server. See `docs/DESPLIEGUE.md`.

### Logging

`logging_setup.py` → `app_fastapi/logs/`: `accesos.log`, `cambios.log`, `errores.log`, `queries.log`, `escritorio.log`. Wired once as a middleware in `main.py`.
