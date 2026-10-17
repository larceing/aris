# -*- coding: utf-8 -*-
"""Configuracion de instalacion: lo que cambia de una empresa a otra sin
tocar codigo. Todo sale del .env (ver .env.example) y se lee en cada
llamada, no como constante de modulo, por el mismo motivo que
db.py::entorno_actual(): no depender del orden en que se importa cada
modulo respecto a load_dotenv().

- Marca: APP_NOMBRE, APP_EMPRESA, APP_LOGO.
- Login: AUTH_MODO = local (por defecto, usuario/contraseña en auth_user),
  keycloak (el formulario de la app valida contra Keycloak) o sso (se entra
  en la pantalla de Keycloak y la sesion vale para todas las apps del realm),
  ver auth.py y routers/login.py.
- Modulos opcionales, apagados por defecto: MODULO_MANTIS, MODULO_NEXUS,
  MODULO_ESCRITORIO, MODULO_CONECTORES. Apagado = no se monta su router, no sale en el menu ni
  en Configuracion, y (escritorio) no arrancan sus hilos.
"""
import os

MODULOS = ("mantis", "nexus", "escritorio", "conectores")

_VERDADERO = {"1", "true", "si", "sí", "yes", "on"}


def app_nombre() -> str:
    return os.getenv("APP_NOMBRE", "").strip() or "Gestor de Tareas"


def app_empresa() -> str:
    return os.getenv("APP_EMPRESA", "").strip() or "Mi Empresa"


def app_logo() -> str:
    return os.getenv("APP_LOGO", "").strip() or "/static/img/logo.svg"


def app_tema_css() -> str:
    """Hoja de estilos extra que se carga tras main.css (colores de la marca de
    esta instalacion; puede ser una URL de otro sitio). Vacia = el aspecto de serie."""
    return os.getenv("APP_TEMA_CSS", "").strip()


def app_url_portal() -> str:
    """Portada desde la que se llega a esta app (boton "Aplicaciones" en la
    cabecera). Vacia = sin boton."""
    return os.getenv("APP_URL_PORTAL", "").strip()


def app_version() -> str:
    """Version desplegada (APP_VERSION, la pone el Dockerfile al construir la
    imagen: v1.2.0 en una release, sha-1a2b3c4 en main). Sin imagen: local."""
    return os.getenv("APP_VERSION", "").strip() or "local"


def auth_modo() -> str:
    modo = os.getenv("AUTH_MODO", "local").strip().lower()
    return modo if modo in ("local", "keycloak", "sso") else "local"


def modulo_activo(nombre: str) -> bool:
    if nombre not in MODULOS:
        raise ValueError(f"módulo desconocido: {nombre}")
    return os.getenv(f"MODULO_{nombre.upper()}", "").strip().lower() in _VERDADERO
