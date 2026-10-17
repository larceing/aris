# -*- coding: utf-8 -*-
"""Servir la app bajo una ruta (p. ej. https://dev.example.com/tareas/)
sin tocar el resto del codigo. La ruta sale de:
- APP_RUTA_BASE=/tareas en el .env (siempre bajo esa ruta), o
- la cabecera X-Forwarded-Prefix que pone el proxy de delante, peticion a
  peticion: asi la misma instancia sirve a la vez en
  https://pro.example.com/tareas/ (entrada comun de varias apps, ver
  docs/DESPLIEGUE.md) y en su propio dominio https://tareas.example.com/.

La app sigue creyendo que vive en "/": este middleware ASGI
1. quita el prefijo de cada peticion entrante (/tareas/notas/ -> /notas/), y
2. se lo pone a todo lo que sale hacia el navegador: cabecera Location, y en
   HTML y en nuestros JS cualquier cadena entre comillas que empiece por
   "/<letra>" ('/notas/', "/static/...", `/proyecto/${id}`); en JSON solo los
   valores de claves tipo url/enlace (nunca el texto que escribe la gente).
Asi tambien salen bien los enlaces guardados en la BD (avisos, muros).

Sin ruta (ni variable ni cabecera) la peticion pasa tal cual.
"""
import json
import os
import re

# JS de terceros que no hay que tocar (sus cadenas no son rutas de la app)
_JS_INTACTOS = ("/static/js/editorjs/", "/static/js/sortable.min.js")
_CLAVES_URL = {"url", "enlace", "link", "href", "redirect", "location", "volver", "next"}

_RUTA_VALIDA = re.compile(r"^(/[A-Za-z0-9_.-]+)+$")


def _normalizar(valor: str) -> str:
    valor = (valor or "").strip().rstrip("/")
    if valor and not valor.startswith("/"):
        valor = "/" + valor
    return valor if _RUTA_VALIDA.match(valor) else ""


def ruta_base() -> str:
    return _normalizar(os.getenv("APP_RUTA_BASE", ""))


def _con_prefijo(prefijo: str, url: str) -> str:
    if url.startswith("/") and not url.startswith("//") and url != prefijo and not url.startswith(prefijo + "/"):
        return prefijo + url
    return url


def reescribir_texto(prefijo: str, texto: str) -> str:
    """Prefija cada cadena que empieza por "/<letra>". Se salta la que va
    detras de un "+" en JS ('/proyecto/' + id + '/tablero/': la segunda es
    la continuacion de una ruta, no el principio), la que ya lo lleva y la
    comilla que es parte de una regex de JS (.replace(/'/g, ...): va justo
    detras de "/" o de "\\")."""
    patron = re.compile(r"""(\+\s*)?(?<![/\\])(["'`])/(?!/)(?=[A-Za-z_#?])""")
    ya = re.compile(re.escape(prefijo.lstrip("/")) + r"""(?:[/"'`?#]|$)""")

    def _sub(m: re.Match) -> str:
        if m.group(1) or ya.match(texto, m.end()):
            return m.group(0)
        return m.group(2) + prefijo + "/"

    texto = patron.sub(_sub, texto)
    # la raiz a secas solo en atributos HTML (en JS '/' suele ser un separador)
    return re.sub(r"""\b(href|action)=(["'])/\2""", lambda m: f"{m.group(1)}={m.group(2)}{prefijo}/{m.group(2)}", texto)


def reescribir_json(prefijo: str, dato):
    if isinstance(dato, dict):
        return {k: (_con_prefijo(prefijo, v) if isinstance(v, str) and k.lower() in _CLAVES_URL
                    else reescribir_json(prefijo, v)) for k, v in dato.items()}
    if isinstance(dato, list):
        return [reescribir_json(prefijo, v) for v in dato]
    return dato


class RutaBaseMiddleware:
    def __init__(self, app, prefijo: str = ""):
        self.app = app
        self.prefijo = prefijo

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        prefijo = self.prefijo
        if not prefijo:
            cabecera = dict(scope.get("headers") or []).get(b"x-forwarded-prefix", b"")
            prefijo = _normalizar(cabecera.decode("latin-1"))
        if not prefijo:
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path == prefijo and scope["type"] == "http":
            destino = prefijo + "/" + (("?" + scope["query_string"].decode()) if scope.get("query_string") else "")
            await send({"type": "http.response.start", "status": 308,
                        "headers": [(b"location", destino.encode())]})
            await send({"type": "http.response.body", "body": b""})
            return
        if path.startswith(prefijo + "/"):
            scope = dict(scope)
            scope["ruta_base"] = prefijo   # la ve la app (request.scope)
            scope["path"] = path[len(prefijo):]
            scope["raw_path"] = scope["path"].encode()
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        ruta = scope["path"]
        estado = {"start": None, "tipo": None, "cuerpo": []}

        async def _send(message):
            if message["type"] == "http.response.start":
                cabeceras = []
                tipo = ""
                for k, v in message.get("headers", []):
                    if k.lower() == b"location":
                        v = _con_prefijo(prefijo, v.decode("latin-1")).encode("latin-1")
                    elif k.lower() == b"content-type":
                        tipo = v.decode("latin-1").lower()
                    cabeceras.append((k, v))
                if "text/html" in tipo:
                    estado["tipo"] = "texto"
                elif "javascript" in tipo and not ruta.startswith(_JS_INTACTOS):
                    estado["tipo"] = "texto"
                elif "application/json" in tipo:
                    estado["tipo"] = "json"
                message = dict(message, headers=cabeceras)
                if estado["tipo"] is None:
                    return await send(message)
                estado["start"] = message
                return
            if message["type"] == "http.response.body" and estado["tipo"] is not None:
                estado["cuerpo"].append(message.get("body", b""))
                if message.get("more_body"):
                    return
                cuerpo = b"".join(estado["cuerpo"])
                cuerpo = _reescribir(prefijo, estado["tipo"], cuerpo)
                inicio = estado["start"]
                cabeceras = [(k, v) for k, v in inicio["headers"] if k.lower() != b"content-length"]
                cabeceras.append((b"content-length", str(len(cuerpo)).encode()))
                await send(dict(inicio, headers=cabeceras))
                return await send({"type": "http.response.body", "body": cuerpo})
            return await send(message)

        return await self.app(scope, receive, _send)


def _reescribir(prefijo: str, tipo: str, cuerpo: bytes) -> bytes:
    try:
        if tipo == "json":
            return json.dumps(reescribir_json(prefijo, json.loads(cuerpo)), ensure_ascii=False).encode("utf-8")
        return reescribir_texto(prefijo, cuerpo.decode("utf-8")).encode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return cuerpo
