# -*- coding: utf-8 -*-
"""Pantalla de login real (formulario + sesion), copiada de
core/templates/login.html -- sustituye al HTTP Basic del principio (que no
tenia pantalla propia, el navegador sacaba su popup nativo en cada
peticion). La verificacion de usuario/contrasena sigue siendo la misma
(auth.py, contra auth_user); lo unico nuevo aqui es guardar el resultado en
una sesion (cookie firmada, ver main.py) para no volver a pedirlo en cada
peticion."""
import secrets

from fastapi import APIRouter, Form
from fastapi.requests import Request
from fastapi.responses import RedirectResponse

from .. import auth, config
from ..templating import templates

router = APIRouter(tags=["login"])

def _next_seguro(next_url: str) -> str:
    """El destino tras el login viene de la propia URL (?next=...) -- se
    exige que sea una ruta relativa de esta misma app, nunca una URL
    absoluta/externa (evita usarlo como redirect abierto)."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


@router.get("/login/")
def formulario_login(request: Request, next: str = "/"):
    if request.session.get("usuario"):
        return RedirectResponse(url=_next_seguro(next))
    if config.auth_modo() == "sso":
        state = secrets.token_urlsafe(24)
        request.session["sso_state"] = state
        request.session["sso_next"] = _next_seguro(next)
        return RedirectResponse(url=auth.sso_url_login(state, request.headers.get("host", "")))
    return templates.TemplateResponse(request, "login.html", {"error": False, "next": _next_seguro(next)})


@router.post("/login/")
def procesar_login(
    request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/"),
):
    next_url = _next_seguro(next)
    try:
        usuario, es_it, admin_global = auth.verificar_credenciales(username, password)
    except auth.SinAccesoApp:
        return templates.TemplateResponse(request, "login.html", {"error": False, "sin_acceso": True, "next": next_url})
    if not usuario:
        return templates.TemplateResponse(request, "login.html", {"error": True, "next": next_url})
    auth.iniciar_sesion(request, usuario, es_it, admin_global)
    return RedirectResponse(url=next_url, status_code=303)


@router.get("/login/sso/")
def volver_de_sso(request: Request, code: str = "", state: str = "", error: str = ""):
    """Vuelta desde la pantalla de Keycloak (AUTH_MODO=sso, ver auth.py)."""
    esperado = request.session.pop("sso_state", None)
    next_url = request.session.pop("sso_next", "/")
    if error or not esperado or not secrets.compare_digest(state, esperado):
        return templates.TemplateResponse(request, "login.html", {"error": True, "next": next_url, "sso_fallo": True})
    try:
        usuario, es_it, admin_global, id_token = auth.sso_completar(code, request.headers.get("host", ""))
    except auth.SinAccesoApp:
        return templates.TemplateResponse(request, "login.html", {"error": True, "next": next_url, "sso_fallo": True, "sin_acceso": True})
    if not usuario:
        return templates.TemplateResponse(request, "login.html", {"error": True, "next": next_url, "sso_fallo": True})
    auth.iniciar_sesion(request, usuario, es_it, admin_global)
    if id_token:
        request.session["sso_id_token"] = id_token
    return RedirectResponse(url=next_url, status_code=303)


@router.get("/logout/")
def cerrar_sesion(request: Request, a: str = ""):
    """a=portada (el boton Salir de la portada): al acabar vuelve a la
    portada en vez de a esta app."""
    id_token = request.session.get("sso_id_token") or ""
    host = request.headers.get("host", "")
    auth.cerrar_sesion(request)
    if config.auth_modo() == "sso":
        # Keycloak solo vuelve a direcciones de esta app (URL/*): pasa por /portada/
        salida = auth.url_publica(host) + "/portada/" if a == "portada" and auth.url_publica(host) else ""
        return RedirectResponse(url=auth.sso_url_logout(id_token, host, salida))
    return RedirectResponse(url="/portada/" if a == "portada" else "/login/")


@router.get("/portada/")
def a_la_portada(request: Request):
    """Sin login: manda a la portada del entorno (APP_URL_PORTAL, o la raiz
    del dominio por el que se ha entrado). La usan Entrar y Salir de la
    portada para volver alli, no a esta app."""
    destino = config.app_url_portal()
    if not destino:
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
        host = request.headers.get("host", "")
        destino = f"{proto}://{host}/" if host else "/"
    return RedirectResponse(url=destino, status_code=303)
