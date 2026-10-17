# -*- coding: utf-8 -*-
"""App FastAPI del Gestor de Tareas -- ver README.md para el contexto
completo (arquitectura, como arrancarla) y CLAUDE.md en la raiz del
proyecto para la vision general."""
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.requests import Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

from . import auth, config, csrf, db, escritorio_servicios, logging_setup, portada, ruta_base  # noqa: E402
from .flash import flash  # noqa: E402
from .routers import agenda, api_externa, buzon, calendario, configuracion, login, mantis, marcadores, muro, nexus, notas, notificaciones, proyectos, reunion, sla, tablero, whiteboard, wiki  # noqa: E402
from .templating import templates  # noqa: E402

# csrf.proteger como dependency GLOBAL (ver csrf.py): corre en toda peticion
# sin tener que anadirla a cada router uno a uno -- genera/mantiene el token
# en la sesion y lo valida en todo POST que no sea /api/ (autenticado con
# X-API-Key, sin sesion de por medio).
@asynccontextmanager
async def lifespan(app):
    """Recordatorio ergonomico + Vigilante de Mantis como hilos en segundo
    plano de la propia app (ver escritorio_servicios.py) -- antes eran dos
    procesos sueltos. TestClient sin 'with' no
    pasa por aqui, asi que los tests no lanzan avisos."""
    if config.modulo_activo("escritorio"):
        escritorio_servicios.arrancar()
    if config.modulo_activo("conectores"):
        from .conectores import motor
        motor.arrancar()
    yield
    escritorio_servicios.parar()
    if config.modulo_activo("conectores"):
        motor.parar()


app = FastAPI(title=config.app_nombre(), dependencies=[Depends(csrf.proteger)], lifespan=lifespan)

# Primeros archivos estaticos de esta v2 (hasta ahora todo el CSS/JS iba
# inline en cada plantilla) -- solo para EditorJS (Wiki), que es un vendor
# bundle demasiado grande para meterlo inline. Copiado literal de
# static/js/editorjs/ de la app real (ver app_fastapi/wiki_store.py).
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# Servir bajo una ruta (/tareas): APP_RUTA_BASE o la cabecera X-Forwarded-Prefix
# del proxy de delante -- ver ruta_base.py. Sin ninguna de las dos no hace nada.
# Va antes que la sesion (por dentro): copia el scope, y la sesion tiene que
# quedar en el de fuera para el middleware de logs.
app.add_middleware(ruta_base.RutaBaseMiddleware, prefijo=ruta_base.ruta_base())


# Sesion propia de esta v2 (cookie firmada, NO la sesion de Django) -- ver
# auth.py. Si no hay SESSION_SECRET_KEY en el .env se genera una al vuelo
# para poder arrancar igualmente, pero entonces todas las sesiones se cierran
# solas en cada reinicio del proceso (la firma deja de coincidir).
_session_secret = os.getenv("SESSION_SECRET_KEY")
if not _session_secret:
    _session_secret = secrets.token_urlsafe(32)
    print("AVISO: SESSION_SECRET_KEY no esta definida en app_fastapi/.env -- "
          "se ha generado una al vuelo, asi que las sesiones no sobreviven a "
          "un reinicio del servidor. Define SESSION_SECRET_KEY (cualquier "
          "cadena larga al azar) para que el login se mantenga entre reinicios.")
app.add_middleware(
    SessionMiddleware, secret_key=_session_secret,
    session_cookie="app_fastapi_session", max_age=60 * 60 * 24 * 90,  # ~3 meses, pedido por el usuario
)


@app.exception_handler(auth.NoAutenticado)
def _sin_sesion(request: Request, exc: auth.NoAutenticado):
    # Una peticion de fondo (la campana, un fetch) sin sesion recibe 401: si
    # se la mandara a /login/ apuntaria su propia URL como destino y pisaria
    # el inicio de sesion que la persona tenga a medias.
    modo = request.headers.get("sec-fetch-mode", "")
    acepta = request.headers.get("accept", "")
    if (modo and modo != "navigate") or ("application/json" in acepta and "text/html" not in acepta):
        return JSONResponse({"error": "sin sesion"}, status_code=401)
    return RedirectResponse(url=f"/login/?next={request.url.path}")


@app.exception_handler(auth.NoEsIT)
def _sin_permiso_it(request: Request, exc: auth.NoEsIT):
    flash(request, "error", "Esta sección es solo para el equipo de IT.")
    return RedirectResponse(url="/")


@app.middleware("http")
async def _logs_de_uso(request: Request, call_next):
    """accesos.log (toda peticion) + cambios.log (solo POST, que en esta app
    es casi siempre una mutacion -- la propia ruta ya dice que accion es) +
    errores.log (excepciones no controladas, ademas de las que cada router
    ya captura por su cuenta) -- ver logging_setup.py."""
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        usuario = request.session.get("usuario", "-") if "session" in request.scope else "-"
        logging_setup.errores.exception(f"{usuario} | {request.method} {request.url.path} -> excepción no controlada")
        raise
    duracion_ms = round((time.perf_counter() - t0) * 1000)
    usuario = request.session.get("usuario", "-") if "session" in request.scope else "-"
    logging_setup.accesos.info(f"{usuario} | {request.method} {request.url.path} -> {response.status_code} ({duracion_ms} ms)")
    if request.method == "POST":
        logging_setup.cambios.info(f"{usuario} | POST {request.url.path} -> {response.status_code}")
    # Aqui SI se conserva la cookie (ver csrf.py::reflejar_cookie): este es
    # el Response final de verdad, a diferencia de fijarla desde la
    # Depends() global (csrf.proteger), que FastAPI descarta en cuanto la
    # ruta devuelve su propio Response (que es siempre, en esta app).
    csrf.reflejar_cookie(request, response)
    return response


app.include_router(login.router)
app.include_router(notas.router)
app.include_router(buzon.router)
app.include_router(notificaciones.router)
app.include_router(muro.router)
app.include_router(muro.router_proyecto)
app.include_router(muro.router_central)
app.include_router(configuracion.router)
app.include_router(calendario.router)
app.include_router(sla.router)
app.include_router(proyectos.router)
app.include_router(wiki.router)
app.include_router(tablero.router)
app.include_router(whiteboard.router)
app.include_router(api_externa.router)
app.include_router(reunion.router)
app.include_router(agenda.router)
app.include_router(marcadores.router)
# modulos opcionales (config.py): apagados no se montan, sus URLs dan 404
if config.modulo_activo("mantis"):
    app.include_router(mantis.router)
if config.modulo_activo("nexus"):
    app.include_router(nexus.router)
if config.modulo_activo("conectores"):
    from .routers import conectores
    app.include_router(conectores.router)


@app.api_route("/version/", methods=["GET", "HEAD"])
def version():
    """Version de la imagen desplegada (APP_VERSION, la pone el Dockerfile):
    la consulta el panel Configuracion -> Despliegues. Sin login. Tambien
    con HEAD, que es lo que usan muchos monitores (UptimeRobot)."""
    return JSONResponse({"version": config.app_version()})



@app.get("/")
def inicio(request: Request, usuario: str = Depends(auth.requiere_login)):
    """Portada "mi dia" (ver portada.py). También el punto donde se garantiza que el
    usuario ya tiene su espacio personal (igual que get_or_create_personal_board
    en CS Manager, llamado desde su vista de inicio) antes de que pueda crear nada."""
    user_id = db.get_user_id(usuario)
    if user_id is not None:
        db.get_or_create_espacio_personal(user_id)
    # "mi dia" -- todo lo que se pinta lo junta portada.py (hoy, mi trabajo,
    # muros, buzon, novedades, avisos de escritorio y proyectos por espacio)
    return templates.TemplateResponse(request, "inicio.html", portada.datos(request, usuario))


@app.get("/buscar/")
def buscar(q: str = "", usuario: str = Depends(auth.requiere_login)):
    """Buscador global de la cabecera -- igual que core/views.py::search,
    llamado por el input de la cabecera (ver _base.html) con debounce de
    300ms. Cada resultado enlaza al tablero del proyecto con
    ?abrir_tarea=<id>, que ese tablero detecta y abre el panel de detalle
    lateral (openDetail(), ver tablero.html) pidiendo la tarea por fetch a
    /tarea/<id>/detalle/ -- funciona aunque la tarjeta no este pintada en la
    vista actual del tablero (otro sprint, backlog, filtrada), igual que en
    la app real."""
    query = q.strip()
    resultados = db.search_tasks(query) if query else []
    return JSONResponse({"results": resultados})


@app.post("/entorno/cambiar/")
def cambiar_entorno(request: Request, usuario: str = Depends(auth.requiere_login)):
    """Mismo interruptor real/pruebas que la app Django (comparten
    data/entorno_db.txt) -- cambiarlo aqui tambien lo cambia alli."""
    nuevo = "test" if db.entorno_actual() == "real" else "real"
    db.set_entorno(nuevo)
    destino = request.headers.get("referer") or "/"
    return RedirectResponse(url=destino, status_code=303)
