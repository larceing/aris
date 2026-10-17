# Aris

**Aris** es nuestro gestor de tareas: la instalación propia (DEV y PRO en `/tareas/` del servidor) de la [versión base](https://github.com/larceing/APP_TAREAS_BASE). Este repositorio empezó como copia de la base con todo su historial; para traer sus mejoras:

```bash
git remote add base https://github.com/larceing/APP_TAREAS_BASE.git   # una vez
git fetch base main && git merge base/main
```

`servidor.json` lo da de alta en el servidor (larceing/servidor): su ruta `/tareas/` en la entrada de cada entorno, su tarjeta en Ajustes, sus grupos `/DEV/Tareas` y `/PRO/Tareas` de Keycloak y sus despliegues con `despliegue.yml`.

---


Aplicación web para la gestión del trabajo de un equipo (pensada para IT y soporte, válida para cualquier área). Incluye:

- tableros Kanban y Scrum, con backlog, sprints y Gantt;
- calendario y agenda diaria;
- wiki, notas y pizarras;
- Buzón de notas entre personas, muro de actividad por espacio y campana de avisos;
- informe de SLA y API para integraciones.

Esta es la **versión base**: la misma aplicación que está en producción, sin datos ni integraciones de ninguna empresa concreta, lista para instalar en otra empresa del grupo. Arranca con datos de demostración para poder enseñarla desde el primer minuto.

| Documento | Para quién |
|---|---|
| [docs/MANUAL_DE_USO.md](docs/MANUAL_DE_USO.md) | Personas que usan la app |
| [docs/NORMATIVA_DE_USO.md](docs/NORMATIVA_DE_USO.md) | Todo el mundo (plantilla a adaptar por cada empresa) |
| [docs/ADMINISTRACION.md](docs/ADMINISTRACION.md) | IT: usuarios, copias, actualizaciones, logs |
| [docs/DESPLIEGUE.md](docs/DESPLIEGUE.md) | IT: entornos DEV y PRO con versiones y rollback (GitHub Actions) |
| [docs/CONECTORES.md](docs/CONECTORES.md) | IT: traer datos de otros sistemas como tareas |
| [docs/NEXUS_ESTRUCTURA_DE_GRAFOS.md](docs/NEXUS_ESTRUCTURA_DE_GRAFOS.md) | IT / desarrollo: modelo de relaciones y catálogo |
| [docs/NEXUS_PLAN_DE_ACCION.md](docs/NEXUS_PLAN_DE_ACCION.md) | IT: cómo poner en marcha Nexus en una empresa nueva |
| [docs/api/Manual_API_App_Tareas.md](docs/api/Manual_API_App_Tareas.md) | Integraciones y agentes de IA (también en `GET /api/manual`) |
| [CLAUDE.md](CLAUDE.md) | Desarrollo: arquitectura y reglas del código |

---

## Puesta en marcha con Docker (recomendado)

Requisitos: Docker con Compose v2.24 o superior.

```bash
cp .env.example .env                              # contraseñas de la BD, admin y SESSION_SECRET_KEY
cp app_fastapi/.env.example app_fastapi/.env      # marca, módulos, login (opcional: todo tiene valor por defecto)
docker compose up -d --build
```

Abre `http://localhost:8000` y entra con `INSTALAR_ADMIN` / `INSTALAR_ADMIN_PASSWORD`.

El primer arranque crea las bases de datos, el usuario administrador y, con `INSTALAR_DEMO=1`, los datos de ejemplo. Los arranques siguientes ven que la base ya existe y no la tocan.

**Base de datos.** Por defecto se levanta MariaDB en un contenedor propio (`COMPOSE_PROFILES=bd-docker`). Para usar un servidor MariaDB que ya exista:
1. Deja `COMPOSE_PROFILES` vacío en `.env`.
2. Pon `DB_HOST` y `DB_PORT` de ese servidor.
3. Si el usuario de la app aún no existe, rellena también `DB_ROOT_USER`/`DB_ROOT_PASSWORD`. El instalador lo crea, con permisos solo sobre sus bases de datos.

## Puesta en marcha sin Docker

Requisitos: Python 3.11 y MariaDB 10.6 o superior.

```bash
python -m venv .venv
.venv/bin/pip install -r app_fastapi/requirements.txt          # Windows: .venv\Scripts\pip
cp app_fastapi/.env.example app_fastapi/.env                     # rellena DB_* y SESSION_SECRET_KEY
.venv/bin/python scripts/instalar.py                             # pide la contraseña del admin
.venv/bin/python -m uvicorn app_fastapi.main:app --port 8000     # desde la RAÍZ del repo
```

Opciones del instalador:

| Opción | Qué hace |
|---|---|
| `--sin-demo` | Instala la base vacía, sin datos de ejemplo |
| `--sin-test` | No crea la base de pruebas |
| `--recrear` | **Borra** y vuelve a crear las bases de datos |
| `--admin` / `--password` | Usuario y contraseña del administrador, sin preguntarlos |

## Datos de demostración

| Usuario | Perfil | Contraseña |
|---|---|---|
| `admin` (o el de `INSTALAR_ADMIN`) | IT, administrador | la del instalador |
| `ana.lopez` | IT, Admin del espacio "Equipo" | la misma que el admin |
| `carlos.ruiz` | Usuario, Editor del espacio "Equipo" | la misma que el admin |

La demo trae:
- la empresa ficticia "Empresa Demo", con sus sistemas e iniciativas;
- el proyecto Kanban **Soporte**, con tareas en todas las columnas (una de ellas vencida);
- el proyecto Scrum **Migración de correo**, con un sprint activo;
- mensajes en el muro, notas del Buzón, una página de wiki, notas, días especiales en el calendario y mensajes del Recordatorio.

Para empezar en limpio en producción: `python scripts/instalar.py --recrear --sin-demo`, o `INSTALAR_DEMO=0` antes del primer arranque con Docker.

## Configuración

Todo lo que cambia de una empresa a otra está en `app_fastapi/.env`, sin tocar código. La referencia completa y comentada es [app_fastapi/.env.example](app_fastapi/.env.example).

| Qué | Variables |
|---|---|
| Marca | `APP_NOMBRE`, `APP_EMPRESA`, `APP_LOGO` (deja tu logo en `app_fastapi/static/img/`) |
| Login | `AUTH_MODO=local` (usuarios en la BD) o `keycloak` (SSO, con `KEYCLOAK_*`) |
| Base de datos | `DB_*`; `DB_NAME_TEST` vacío = sin entorno de pruebas |
| Módulos opcionales | `MODULO_MANTIS`, `MODULO_NEXUS`, `MODULO_ESCRITORIO`, `MODULO_CONECTORES` |

### Módulos opcionales

Todos vienen apagados. Un módulo apagado no aparece en el menú ni en Configuración, y sus direcciones devuelven 404.

| Módulo | Qué añade |
|---|---|
| **Conectores** | Trae elementos de otros sistemas (una API REST cualquiera, MantisBT…) y los convierte en tareas. Ver [docs/CONECTORES.md](docs/CONECTORES.md). |
| **Mantis** | Integración completa con MantisBT: importar incidencias, mover tickets, SLA de Mantis y Vigilante de incidencias nuevas. Una cuenta, en el `.env`. |
| **Nexus** | Buscador documental: fichas de sistemas, archivos indexados de carpetas de red, relaciones entre entidades y credenciales cifradas. Ver [docs/NEXUS_PLAN_DE_ACCION.md](docs/NEXUS_PLAN_DE_ACCION.md). |
| **Escritorio** | Servicios en segundo plano:<br>• Recordatorio de pausas activas.<br>• Vigía de carpeta: avisa de archivos atascados o fallidos en una carpeta de integración.<br>• Con Mantis, el Vigilante.<br>Los avisos emergentes solo salen en Windows. En Linux/Docker el Vigía avisa por Buzón y campana. |

## Usuarios

Con `AUTH_MODO=local`, los usuarios se gestionan con `scripts/usuarios.py`:

```bash
python scripts/usuarios.py crear maria --nombre "María" --apellidos "Gil" --email maria@empresa.com [--it]
python scripts/usuarios.py password maria
python scripts/usuarios.py it maria --si          # acceso a Configuración y avisos generales
python scripts/usuarios.py activar maria --no     # dar de baja sin borrar su historial
python scripts/usuarios.py listar
```

Con Docker, añade `docker compose exec app` delante. Con Keycloak, cada persona se da de alta sola la primera vez que entra, y es de IT quien pertenezca al grupo `IT` del realm.

## Estructura

```
app_fastapi/          la aplicación (FastAPI + Jinja2, SQL a mano sobre MariaDB)
  conectores/         módulo de conectores de entrada
  nexus/              módulo Nexus
  routers/            una ruta por pantalla
  templates/ static/  vistas
scripts/              instalar, usuarios, datos de demo, indexador de Nexus, schema SQL
docs/                 manuales
data/ media/          (se crean solos) estado de la app y ficheros subidos
```

## Tests

```bash
python -m unittest app_fastapi.tests -v
```

Los tests escriben en la base de **pruebas** (`DB_NAME_TEST`), nunca en la real, y borran lo que crean. Necesitan `httpx` (ya incluido en `requirements.txt`).
