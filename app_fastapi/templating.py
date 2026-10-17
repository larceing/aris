# -*- coding: utf-8 -*-
"""Instancia de plantillas Jinja2 UNICA y compartida por todos los routers
-- evitar que cada pagina cree la suya propia (eso duplicaria filtros como
linebreaksbr en vez de definirlos una sola vez)."""
import json
from pathlib import Path

import jinja2
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import config, csrf, db, flash
from .icons import icon

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["linebreaksbr"] = lambda s: Markup("<br>\n".join(str(escape(s)).split("\n")))
templates.env.globals["icon"] = icon
# marca y modulos opcionales de la instalacion (ver config.py / .env)
templates.env.globals["app_nombre"] = config.app_nombre
templates.env.globals["app_empresa"] = config.app_empresa
templates.env.globals["app_logo"] = config.app_logo
templates.env.globals["app_version"] = config.app_version
templates.env.globals["app_tema_css"] = config.app_tema_css
templates.env.globals["app_url_portal"] = config.app_url_portal
templates.env.globals["modulo_activo"] = config.modulo_activo
# entorno_actual() se llama directamente desde _base.html (desplegable de
# usuario + banner "modo pruebas") -- global en vez de tener que meterlo a
# mano en el contexto de cada router, es lo mismo en todas las paginas.
templates.env.globals["entorno_actual"] = db.entorno_actual
# Lista de proyectos para el sidebar (enlaces a su Wiki) -- mismo motivo que
# entorno_actual(): es la misma lista en cualquier pagina, no hace falta que
# cada router la meta a mano en su contexto.
templates.env.globals["proyectos_sidebar"] = db.load_proyectos_sidebar
# Igual que proyectos_sidebar(), pero agrupado Kanban/Scrum + hitos + sprint
# activo -- lo que pinta el sidebar de verdad (ver _base.html).
templates.env.globals["sidebar_projects"] = db.load_sidebar_projects
# Igual que core/views.py::base_context's avisos_activos/alarmas_activas --
# franja de avisos del dia (dias_especiales categoria "banner"), en TODAS
# las paginas, no solo Calendario.
templates.env.globals["avisos_activos"] = db.load_avisos_activos
# Espacios activos para el <select> del modal de proyecto (_base.html) --
# mismo motivo que proyectos_sidebar/sidebar_projects arriba.
templates.env.globals["espacios_activos"] = db.load_espacios_activos


@jinja2.pass_context
def _marcadores_activos(ctx) -> list:
    """Marcadores del usuario logueado, para la seccion del sidebar
    (_base.html) -- pass_context en vez de un Depends() propio porque
    'usuario' ya viaja en el contexto de CADA TemplateResponse (todas las
    rutas lo pasan), sin necesidad de una consulta de sesion aparte."""
    usuario = ctx.get("usuario")
    user_id = db.get_user_id(usuario) if usuario else None
    return db.load_marcadores(user_id) if user_id else []


templates.env.globals["marcadores_activos"] = _marcadores_activos


@jinja2.pass_context
def _csrf_field(ctx) -> Markup:
    """Equivalente a {% csrf_token %} de Django -- {{ csrf_field() }} en
    cualquier <form method="post">. pass_context para leer 'request' del
    propio contexto de la plantilla (Starlette ya lo inyecta siempre en
    TemplateResponse), sin tener que pasarlo a mano en cada llamada."""
    token = csrf.token_actual(ctx["request"])
    return Markup(f'<input type="hidden" name="{csrf.CSRF_FIELD_NAME}" value="{escape(token)}">')


templates.env.globals["csrf_field"] = _csrf_field


@jinja2.pass_context
def _get_flashed_messages(ctx) -> list:
    """Equivalente al 'messages' que Django inyecta solo con el context
    processor de siempre -- aqui hace falta llamarla explicitamente
    ({{ get_flashed_messages() }} en _base.html) porque no hay context
    processors en Jinja2Templates. Se consumen UNA vez (pop), igual que
    Django: el siguiente render ya no los repite."""
    return flash.pop_flashed(ctx["request"])


templates.env.globals["get_flashed_messages"] = _get_flashed_messages


@jinja2.pass_context
def _es_it(ctx) -> bool:
    """Si el usuario logueado es de IT -- en modo keycloak, grupo 'IT' del
    claim 'groups'; en modo local, auth_user.is_staff (ver auth.py) y se guarda solo en la
    sesion, nunca en nuestra base de datos. Usado en _base.html para
    ocultar Mantis/SLA a quien no sea de IT."""
    return bool(ctx["request"].session.get("es_it"))


templates.env.globals["es_it"] = _es_it


@jinja2.pass_context
def _puede_ver_nexus(ctx) -> bool:
    """Para el enlace "Nexus" del sidebar (_base.html) -- oculto a quien no
    tendria acceso de todas formas, mismo criterio que
    routers/nexus.py::_requiere_acceso_nexus (ajuste 'nexus.acceso',
    'it' por defecto)."""
    if not config.modulo_activo("nexus") or not ctx["request"].session.get("usuario"):
        return False
    if db.get_ajuste("nexus.acceso", "it") == "it":
        return bool(ctx["request"].session.get("es_it"))
    return True


templates.env.globals["puede_ver_nexus"] = _puede_ver_nexus

# Mismo patron que el filtro json_script de Django (mantis.html vuelca ahi
# la lista de empresas para que el JS la lea sin tener que pedirla aparte):
# escapa </>&  a \uXXXX para que el JSON pueda vivir dentro de un <script>
# sin riesgo de que un valor con esos caracteres rompa el HTML.
_JSON_SCRIPT_ESCAPES = {ord(">"): "\\u003E", ord("<"): "\\u003C", ord("&"): "\\u0026"}


def _tojson_seguro(value) -> Markup:
    return Markup(json.dumps(value).translate(_JSON_SCRIPT_ESCAPES))


templates.env.filters["tojson_seguro"] = _tojson_seguro
