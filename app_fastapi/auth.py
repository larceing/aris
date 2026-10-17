# -*- coding: utf-8 -*-
"""Login con dos modos, elegidos con AUTH_MODO en el .env (ver config.py):

- local (por defecto): usuario/contraseña contra auth_user, con el mismo
  formato de hash que Django (pbkdf2_sha256$iteraciones$sal$hash) -- asi una
  instalacion que venga de Django conserva sus contraseñas. "Es IT" =
  auth_user.is_staff. Los usuarios se crean con scripts/usuarios.py (o los
  crea el instalador: el admin de la demo).
- keycloak: se valida contra el realm de KEYCLOAK_ISSUER con el flujo
  "Direct Access Grant" (grant_type=password): nuestro backend reenvia
  usuario+contraseña al token endpoint y toma un 200 como "credenciales
  correctas", asi login.html no cambia. "Es IT" = grupo "IT" de Keycloak
  (claim "groups"). Quien entra por primera vez se da de alta solo en
  auth_user (ver _asegurar_auth_user). Sin caida a verificacion local: un
  unico origen de verdad para las credenciales.

auth_user es en los dos casos la tabla de la que cuelgan espacios/tareas/etc.

La SESION es propia de la app: cookie firmada (Starlette SessionMiddleware +
itsdangerous, ver main.py); lo unico que persiste en ella es el username y
si es de IT."""
import base64
import hashlib
import hmac
import contextvars
import os
import secrets

import requests
from fastapi import Depends
from fastapi.requests import Request

from . import config, db


class NoAutenticado(Exception):
    """La peticion no trae sesion valida -- un manejador en main.py la
    convierte en una redirección a /login/, no en un 401 crudo."""
    pass


class NoEsIT(Exception):
    """Logueado pero no es de IT (ver docstring del modulo) -- un manejador en
    main.py lo convierte en una redirección a '/' con un aviso, igual que
    NoAutenticado se convierte en redirección a /login/."""
    pass


class SinAccesoApp(Exception):
    """Keycloak acepta a la persona, pero no esta en el grupo de esta app
    (KEYCLOAK_GRUPO_APP) ni es admin global -- login.py enseña un aviso."""
    def __init__(self, usuario=None, grupos=None):
        super().__init__(usuario)
        self.usuario = usuario
        self.grupos = normalizar_grupos(grupos)


def normalizar_grupos(grupos) -> list:
    """Los grupos de Keycloak sin barras de los lados (/PRO/Tareas -> PRO/Tareas)."""
    return sorted({g.strip().strip("/") for g in (grupos or []) if isinstance(g, str) and g.strip().strip("/")})


# grupos del ultimo login con Keycloak en esta peticion (lo guarda
# iniciar_sesion en la sesion, para la portada)
_grupos_login: contextvars.ContextVar = contextvars.ContextVar("grupos_login", default=None)


def acceso_por_grupos(grupos) -> tuple[bool, bool]:
    """(puede_entrar, es_admin) segun los grupos de Keycloak (claim "groups").

    Un grupo por app con subgrupos (el mapper del cliente con "Full group
    path" activado, para que lleguen como /Tareas/Admin):
      /<App>               entra en la app
      /<App>/Admin         administra solo esta app
      /<App>/<Otro>        entra (departamentos y demas)
      KEYCLOAK_GRUPO_ADMIN_GLOBAL (por defecto IT): entra y administra todas.
    Sin KEYCLOAK_GRUPO_APP entra cualquiera del realm y solo el admin global
    administra (lo de siempre)."""
    nombres = {g.strip().strip("/") for g in (grupos or []) if isinstance(g, str)}
    es_global = es_admin_global(nombres)
    app = os.getenv("KEYCLOAK_GRUPO_APP", "").strip().strip("/")
    if not app:
        return True, es_global
    en_app = any(g == app or g.startswith(app + "/") for g in nombres)
    return es_global or en_app, es_global or f"{app}/Admin" in nombres


def es_admin_global(grupos) -> bool:
    """De KEYCLOAK_GRUPO_ADMIN_GLOBAL (por defecto IT): entra y administra
    todas las apps del realm. Ser Admin de una sola app (/Tareas/Admin) no basta."""
    nombres = {g.strip().strip("/") for g in (grupos or []) if isinstance(g, str)}
    admin_global = os.getenv("KEYCLOAK_GRUPO_ADMIN_GLOBAL", "IT").strip().strip("/")
    return bool(admin_global) and admin_global in nombres


def _verificar_keycloak(username: str, password: str):
    """(None, False, False) si Keycloak rechaza las credenciales (o no
    responde), o (username canónico, es_it, admin_global) si son correctas. "es_it" viene del grupo de
    Keycloak "IT" (claim "groups" del userinfo, vía un protocol mapper en el
    cliente) -- deliberadamente NO se guarda en nuestra base de datos, solo
    en la sesion (ver iniciar_sesion): gestionar quien es de IT es cosa
    entera de Keycloak, no una columna/regla fija aqui.
    No se valida la firma del token: la petición va directa de nuestro
    backend al issuer configurado, así que un 200 en el token endpoint ya es
    prueba suficiente de que la contraseña era correcta.
    Lee KEYCLOAK_ISSUER/etc. en cada llamada (no como constante de modulo) --
    mismo motivo que db.py::entorno_actual() nunca cachea: evita depender de
    que .env ya se haya cargado en el momento exacto en que se importa este
    modulo (el orden de imports en tests.py, por ejemplo, no lo garantiza)."""
    issuer = os.getenv("KEYCLOAK_ISSUER")
    if not issuer:
        return None, False, False
    try:
        token_resp = requests.post(
            f"{issuer}/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": os.getenv("KEYCLOAK_CLIENT_ID"),
                "client_secret": os.getenv("KEYCLOAK_CLIENT_SECRET"),
                "username": username,
                "password": password,
                # sin pedirlo explicitamente, el token que devuelve Keycloak
                # no lleva el scope "openid" -- y /userinfo lo exige (403
                # insufficient_scope, "Missing openid scope" si falta).
                "scope": "openid",
            },
            timeout=5,
        )
    except requests.RequestException:
        return None, False, False
    if token_resp.status_code != 200:
        return None, False, False
    try:
        access_token = token_resp.json()["access_token"]
    except (ValueError, KeyError):
        return None, False, False
    return _usuario_de_keycloak(issuer, access_token)


def _usuario_de_keycloak(issuer: str, access_token: str):
    """(username, es_it, admin_global) a partir del access token, via /userinfo -- comun al
    modo keycloak (formulario propio) y al modo sso (pantalla de Keycloak)."""
    try:
        userinfo_resp = requests.get(
            f"{issuer}/protocol/openid-connect/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=5,
        )
    except requests.RequestException:
        return None, False, False
    if userinfo_resp.status_code != 200:
        return None, False, False
    datos = userinfo_resp.json()
    puede, es_it = acceso_por_grupos(datos.get("groups"))
    if not puede:
        raise SinAccesoApp(datos.get("preferred_username"), datos.get("groups"))
    _grupos_login.set(normalizar_grupos(datos.get("groups")))
    return datos.get("preferred_username"), es_it, es_admin_global(datos.get("groups"))


# ── modo sso: inicio de sesion unico en la pantalla de Keycloak ───────────
# Authorization Code Flow: /login/ manda a Keycloak, Keycloak vuelve a
# /login/sso/?code=...&state=..., y aqui se cambia el code por un token
# (peticion directa backend -> issuer, como en el modo keycloak, por eso no
# hace falta validar la firma del token). Si la persona ya entro en otra app
# del mismo realm, Keycloak no vuelve a pedir la contraseña.

def url_publica(host: str = "") -> str:
    """Direccion publica de la app. Si la app tiene mas de una (su dominio
    propio en APP_URL_PUBLICA y la de antes en APP_URLS_ALTERNATIVAS, separadas
    por comas), la del host por el que ha entrado la peticion; si no, la
    principal. Asi el login vuelve siempre al mismo sitio desde el que se fue."""
    from urllib.parse import urlparse
    principal = os.getenv("APP_URL_PUBLICA", "").strip().rstrip("/")
    host = (host or "").strip().lower()
    if host:
        for u in [principal] + os.getenv("APP_URLS_ALTERNATIVAS", "").split(","):
            u = u.strip().rstrip("/")
            if u and urlparse(u).netloc.lower() == host:
                return u
    return principal


def sso_redirect_uri(host: str = "") -> str:
    return url_publica(host) + "/login/sso/"


def sso_url_login(state: str, host: str = "") -> str:
    from urllib.parse import urlencode
    issuer = os.getenv("KEYCLOAK_ISSUER", "")
    return f"{issuer}/protocol/openid-connect/auth?" + urlencode({
        "client_id": os.getenv("KEYCLOAK_CLIENT_ID", ""), "response_type": "code",
        "scope": "openid", "redirect_uri": sso_redirect_uri(host), "state": state,
    })


def sso_completar(code: str, host: str = ""):
    """(None, False, False, "") si Keycloak no acepta el code; (username,
    es_it, admin_global, id_token) si si. El id_token se guarda en la sesion para cerrar la de
    Keycloak sin pantalla de confirmar. Igual que verificar_credenciales, da
    de alta la fila de auth_user."""
    issuer = os.getenv("KEYCLOAK_ISSUER")
    if not issuer or not code:
        return None, False, False, ""
    try:
        token_resp = requests.post(
            f"{issuer}/protocol/openid-connect/token",
            data={
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": sso_redirect_uri(host),
                "client_id": os.getenv("KEYCLOAK_CLIENT_ID"),
                "client_secret": os.getenv("KEYCLOAK_CLIENT_SECRET"),
            },
            timeout=5,
        )
        datos = token_resp.json() if token_resp.status_code == 200 else {}
        access_token = datos["access_token"] if datos else None
        id_token = datos.get("id_token") or ""
    except (requests.RequestException, ValueError, KeyError, AttributeError):
        return None, False, False, ""
    if not access_token:
        return None, False, False, ""
    id_token = id_token if isinstance(id_token, str) else ""
    try:
        usuario, es_it, admin_global = _usuario_de_keycloak(issuer, access_token)
    except SinAccesoApp as e:
        e.id_token = id_token   # para cerrar luego la sesion de Keycloak sin preguntar
        raise
    if usuario:
        _asegurar_auth_user(usuario)
    return usuario, es_it, admin_global, id_token


def sso_url_logout(id_token: str = "", host: str = "", salida: str = "") -> str:
    """Cierra tambien la sesion de Keycloak (si no, la siguiente visita entraria
    sola) y vuelve a SSO_URL_SALIDA (la portada) o a la propia app. Con el
    id_token del login (id_token_hint) Keycloak cierra sin preguntar."""
    from urllib.parse import urlencode
    issuer = os.getenv("KEYCLOAK_ISSUER", "")
    salida = salida or os.getenv("SSO_URL_SALIDA", "").strip() or url_publica(host) + "/"
    params = {"client_id": os.getenv("KEYCLOAK_CLIENT_ID", ""), "post_logout_redirect_uri": salida}
    if id_token:
        params["id_token_hint"] = id_token
    return f"{issuer}/protocol/openid-connect/logout?" + urlencode(params)


def _asegurar_auth_user(username: str) -> None:
    """Aprovisionamiento just-in-time: si Keycloak ya validó a esta persona
    pero todavía no tiene fila en auth_user, se crea sola. 'password' con
    prefijo '!' = convención de Django para "sin contraseña local
    utilizable" (mismo patrón que el usuario de ejemplo de
    la BD de pruebas) -- la contraseña de verdad vive en Keycloak, no aquí."""
    if db.get_user_id(username) is not None:
        return
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            # TODAS las columnas obligatorias, sin fiarse de DEFAULTs: la
            # auth_user real la creo Django y NO tiene ningun valor por
            # defecto (first_name/last_name/email/date_joined...) -- con solo
            # cinco columnas el alta reventaba (1364 "Field 'first_name'
            # doesn't have a default value") la primera vez que entraba
            # alguien nuevo por Keycloak. Los tests no lo veian porque la
            # auth_user de test_app_tareas si tenia DEFAULTs (ya no).
            cur.execute(
                "INSERT INTO auth_user (password, is_superuser, username, first_name, last_name, email, "
                "is_staff, is_active, date_joined) VALUES ('!keycloak', 0, %s, '', '', '', 0, 1, NOW())",
                (username,),
            )
        conn.commit()
    finally:
        conn.close()


# ── modo local: hash compatible con Django (pbkdf2_sha256) ─────────────────

PBKDF2_ITERACIONES = 870000  # las mismas que Django 5.x


def hash_contrasena(password: str, iteraciones: int = PBKDF2_ITERACIONES) -> str:
    sal = secrets.token_urlsafe(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), sal.encode(), iteraciones)
    return f"pbkdf2_sha256${iteraciones}${sal}${base64.b64encode(dk).decode()}"


def _verificar_pbkdf2_sha256(password: str, guardado: str) -> bool:
    """False para cualquier cosa que no sea un hash pbkdf2_sha256 bien
    formado -- incluido '!...' (convención de Django para "sin contraseña
    utilizable", la que llevan los usuarios dados de alta por Keycloak)."""
    try:
        algoritmo, iteraciones, sal, esperado = guardado.split("$", 3)
        if algoritmo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), sal.encode(), int(iteraciones))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(base64.b64encode(dk).decode(), esperado)


def _verificar_local(username: str, password: str):
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT username, password, is_staff, is_active FROM auth_user WHERE username = %s", (username,))
            fila = cur.fetchone()
    finally:
        conn.close()
    if not fila or not fila[3] or not _verificar_pbkdf2_sha256(password, fila[1] or ""):
        return None, False, False
    return fila[0], bool(fila[2]), bool(fila[2])  # en local, is_staff administra todo


def verificar_credenciales(username: str, password: str):
    """(None, False, False) si son invalidas, o (username canónico, es_it,
    admin_global) si son correctas -- usado por routers/login.py."""
    if config.auth_modo() == "local":
        return _verificar_local(username.strip(), password)
    validado, es_it, admin_global = _verificar_keycloak(username, password)
    if not validado:
        return None, False, False
    _asegurar_auth_user(validado)
    return validado, es_it, admin_global


def iniciar_sesion(request: Request, username: str, es_it: bool = False, admin_global: bool = False) -> None:
    request.session["usuario"] = username
    request.session["es_it"] = es_it
    request.session["admin_global"] = bool(admin_global)
    grupos = _grupos_login.get()
    if grupos is not None:
        request.session["grupos"] = grupos
    # con Keycloak, IT lo dice el grupo de cada login: se apunta en auth_user
    # para que los permisos de los espacios (db.puede_actuar_en_proyecto) lo
    # vean. En local is_staff ya es el origen de es_it, asi que no cambia nada.
    try:
        db.marcar_it(username, es_it)
    except Exception:  # noqa: BLE001 -- el login no debe caer por esto
        pass


def cerrar_sesion(request: Request) -> None:
    request.session.clear()


def requiere_login(request: Request) -> str:
    usuario = request.session.get("usuario")
    if not usuario:
        raise NoAutenticado()
    return usuario


def requiere_it(request: Request, usuario: str = Depends(requiere_login)) -> str:
    """Como requiere_login, pero exige ademas ser de IT (ver
    iniciar_sesion/session['es_it']) -- para /configuracion/
    entero: ser Admin de un espacio no da acceso a la configuracion global
    de la app, son dos privilegios sin relacion."""
    if not request.session.get("es_it"):
        raise NoEsIT()
    return usuario

