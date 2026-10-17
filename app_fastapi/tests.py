# -*- coding: utf-8 -*-
"""Tests de app_fastapi -- no existia ninguno hasta ahora (el core/tests.py
de la app Django que este proyecto sustituyo se borro con el resto de
core/). Misma disciplina de entorno que aquel: cualquier test que toque
datos hereda de EntornoAisladoTestCase (fuerza entorno='test', restaura al
terminar, nunca toca 'real'), y limpia lo que crea con el prefijo
TEST_PREFIX.

Ejecutar (desde la raiz del repo, con el venv de app_fastapi):
    app_fastapi\\.venv\\Scripts\\python.exe -m unittest app_fastapi.tests -v
"""
import os
import unittest
import uuid
from unittest.mock import Mock, patch

# los tests cubren tambien los modulos opcionales: se activan todos ANTES de
# importar main (que decide al importarse que routers monta)
for _modulo in ("MANTIS", "NEXUS", "ESCRITORIO", "CONECTORES", "DESPLIEGUES"):
    os.environ[f"MODULO_{_modulo}"] = "1"

from fastapi import HTTPException
from starlette.testclient import TestClient

from . import auth, csrf, db
from .main import app
from .routers import nexus

TEST_PREFIX = "AUTOTEST_"


class EntornoAisladoTestCase(unittest.TestCase):
    """Fuerza entorno='test' para toda la clase y restaura el que hubiera
    antes al terminar, sin condiciones."""

    @classmethod
    def setUpClass(cls):
        cls._entorno_previo = db.entorno_actual()
        db.set_entorno("test")

    @classmethod
    def tearDownClass(cls):
        try:
            # tests que borran filas con DELETE directo (sin eliminar_*)
            # dejarian nodos de nexus huerfanos: se recogen aqui
            db.nexus_sincronizar()
        finally:
            db.set_entorno(cls._entorno_previo)


# ── auth.py: verificacion contra Keycloak (Direct Access Grant) ────────────
# Keycloak se mockea siempre aqui -- estos tests no dependen de que haya un
# servidor Keycloak real levantado en la maquina que los ejecuta.

_KEYCLOAK_ENV = {"AUTH_MODO": "keycloak", "KEYCLOAK_ISSUER": "http://keycloak.test/realms/demo",
                 "KEYCLOAK_CLIENT_ID": "app", "KEYCLOAK_CLIENT_SECRET": "x"}


@patch.dict("os.environ", _KEYCLOAK_ENV)
class AuthTests(unittest.TestCase):
    """Pura -- no toca la BD."""

    @patch("app_fastapi.auth.requests.get")
    @patch("app_fastapi.auth.requests.post")
    def test_credenciales_correctas_devuelve_username(self, mock_post, mock_get):
        mock_post.return_value = Mock(status_code=200, json=lambda: {"access_token": "t"})
        mock_get.return_value = Mock(status_code=200, json=lambda: {"preferred_username": "usuario.demo", "groups": []})
        self.assertEqual(auth._verificar_keycloak("usuario.demo", "clave-correcta"), ("usuario.demo", False, False))

    @patch("app_fastapi.auth.requests.get")
    @patch("app_fastapi.auth.requests.post")
    def test_credenciales_de_alguien_del_grupo_it_marca_es_it(self, mock_post, mock_get):
        mock_post.return_value = Mock(status_code=200, json=lambda: {"access_token": "t"})
        mock_get.return_value = Mock(status_code=200, json=lambda: {"preferred_username": "usuario.demo", "groups": ["IT"]})
        self.assertEqual(auth._verificar_keycloak("usuario.demo", "clave-correcta"), ("usuario.demo", True, True))

    @patch("app_fastapi.auth.requests.post")
    def test_credenciales_incorrectas_no_verifica(self, mock_post):
        mock_post.return_value = Mock(status_code=401, json=lambda: {"error": "invalid_grant"})
        self.assertEqual(auth._verificar_keycloak("usuario.demo", "clave-mala"), (None, False, False))

    @patch("app_fastapi.auth.requests.post", side_effect=auth.requests.RequestException("caido"))
    def test_keycloak_inalcanzable_no_verifica_ni_propaga(self, mock_post):
        self.assertEqual(auth._verificar_keycloak("usuario.demo", "cualquiera"), (None, False, False))

    def test_sin_issuer_configurado_no_verifica(self):
        with patch.dict("os.environ", {"KEYCLOAK_ISSUER": ""}):
            self.assertEqual(auth._verificar_keycloak("usuario.demo", "cualquiera"), (None, False, False))


@patch.dict("os.environ", _KEYCLOAK_ENV)
class AuthAprovisionamientoTests(EntornoAisladoTestCase):
    """Alta automatica en auth_user cuando Keycloak valida a alguien que
    todavia no tiene fila -- mismo patron de limpieza que EspaciosTests."""

    def _borrar_usuario_de_prueba(self, username):
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM auth_user WHERE username=%s", (username,))
            conn.commit()
        finally:
            conn.close()

    @patch("app_fastapi.auth.requests.get")
    @patch("app_fastapi.auth.requests.post")
    def test_usuario_validado_sin_fila_previa_se_crea_solo(self, mock_post, mock_get):
        username = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        mock_post.return_value = Mock(status_code=200, json=lambda: {"access_token": "t"})
        mock_get.return_value = Mock(status_code=200, json=lambda: {"preferred_username": username, "groups": []})
        self.assertIsNone(db.get_user_id(username))
        try:
            resultado, es_it, _ = auth.verificar_credenciales(username, "cualquiera")
            self.assertEqual(resultado, username)
            self.assertFalse(es_it)
            self.assertIsNotNone(db.get_user_id(username))
        finally:
            self._borrar_usuario_de_prueba(username)


# ── auth.py: modo local (hash pbkdf2_sha256 compatible con Django) ──────────

class AuthHashTests(unittest.TestCase):
    """Pura -- sin BD."""

    def test_hash_propio_verifica(self):
        h = auth.hash_contrasena("s3creta-larga", iteraciones=1000)
        self.assertTrue(h.startswith("pbkdf2_sha256$1000$"))
        self.assertTrue(auth._verificar_pbkdf2_sha256("s3creta-larga", h))
        self.assertFalse(auth._verificar_pbkdf2_sha256("otra", h))

    def test_hash_de_django_verifica(self):
        # mismo formato que make_password() de Django (algoritmo$iteraciones$sal$hash_b64),
        # construido aqui con hashlib para no depender de Django
        import base64, hashlib
        dk = base64.b64encode(hashlib.pbkdf2_hmac("sha256", b"demo", b"abc", 1000)).decode()
        self.assertTrue(auth._verificar_pbkdf2_sha256("demo", f"pbkdf2_sha256$1000$abc${dk}"))

    def test_sin_contrasena_utilizable_no_verifica(self):
        for guardado in ("!keycloak", "", "md5$x$y", "pbkdf2_sha256$no-numero$s$h"):
            self.assertFalse(auth._verificar_pbkdf2_sha256("lo-que-sea", guardado))


@patch.dict("os.environ", {"AUTH_MODO": "local"})
class AuthLocalTests(EntornoAisladoTestCase):
    def setUp(self):
        self.username = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO auth_user (password, is_superuser, username, first_name, last_name, email, "
                    "is_staff, is_active, date_joined) VALUES (%s, 0, %s, '', '', '', 1, 1, NOW())",
                    (auth.hash_contrasena("buena-clave", iteraciones=1000), self.username),
                )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM auth_user WHERE username=%s", (self.username,))
            conn.commit()
        finally:
            conn.close()

    def _set_activo(self, activo: bool):
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE auth_user SET is_active=%s WHERE username=%s", (int(activo), self.username))
            conn.commit()
        finally:
            conn.close()

    def test_contrasena_correcta_entra_y_staff_es_it(self):
        self.assertEqual(auth.verificar_credenciales(self.username, "buena-clave"), (self.username, True, True))

    def test_contrasena_incorrecta_no_entra(self):
        self.assertEqual(auth.verificar_credenciales(self.username, "mala"), (None, False, False))

    def test_usuario_inactivo_no_entra(self):
        self._set_activo(False)
        self.assertEqual(auth.verificar_credenciales(self.username, "buena-clave"), (None, False, False))

    @patch("app_fastapi.auth.requests.post")
    def test_modo_local_no_llama_a_keycloak(self, mock_post):
        auth.verificar_credenciales(self.username, "buena-clave")
        mock_post.assert_not_called()


# ── csrf.py: patron doble cookie/sesion ─────────────────────────────────────

class _FakeURL:
    def __init__(self, path):
        self.path = path


class _FakeRequest:
    def __init__(self, method="POST", path="/algo/", headers=None, form_data=None):
        self.method = method
        self.url = _FakeURL(path)
        self.headers = headers or {}
        self.session = {}
        self._form_data = form_data or {}

    async def form(self):
        return self._form_data


class CsrfTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_no_exige_token(self):
        await csrf.proteger(_FakeRequest(method="GET"))  # no debe lanzar

    async def test_post_sin_token_falla(self):
        with self.assertRaises(HTTPException):
            await csrf.proteger(_FakeRequest(method="POST", headers={"content-type": "application/json"}))

    async def test_post_con_token_correcto_en_header_pasa(self):
        req = _FakeRequest(method="POST")
        token = csrf._get_or_create_token(req)
        req.headers = {csrf.CSRF_HEADER_NAME: token, "content-type": "application/json"}
        await csrf.proteger(req)  # no debe lanzar

    async def test_post_con_token_incorrecto_falla(self):
        req = _FakeRequest(method="POST", headers={csrf.CSRF_HEADER_NAME: "token-falso", "content-type": "application/json"})
        with self.assertRaises(HTTPException):
            await csrf.proteger(req)

    async def test_api_exento_de_csrf(self):
        await csrf.proteger(_FakeRequest(method="POST", path="/api/tareas/"))  # exento, no debe lanzar


# ── routers/nexus.py::_requiere_acceso_nexus -- ajuste 'nexus.acceso' ──────

class NexusAccesoTests(EntornoAisladoTestCase):
    """El modo dev y las credenciales son SIEMPRE auth.requiere_it (sin test
    aparte, es el mismo guardian ya probado en ConfiguracionRequiereITTests);
    esto solo prueba el guardian condicional de la busqueda/preview
    normales, que depende del ajuste configurable 'nexus.acceso'."""

    def setUp(self):
        self._ajuste_previo = db.get_ajuste("nexus.acceso", "it")

    def tearDown(self):
        db.set_ajuste("nexus.acceso", self._ajuste_previo)

    def test_por_defecto_it_bloquea_a_quien_no_es_it(self):
        db.set_ajuste("nexus.acceso", "it")
        req = _FakeRequest(method="GET")
        req.session = {"es_it": False}
        with self.assertRaises(auth.NoEsIT):
            nexus._requiere_acceso_nexus(req, "alguien")

    def test_it_entra_pase_lo_que_pase(self):
        db.set_ajuste("nexus.acceso", "it")
        req = _FakeRequest(method="GET")
        req.session = {"es_it": True}
        nexus._requiere_acceso_nexus(req, "alguien")  # no debe lanzar

    def test_todos_deja_entrar_a_quien_no_es_it(self):
        db.set_ajuste("nexus.acceso", "todos")
        req = _FakeRequest(method="GET")
        req.session = {"es_it": False}
        nexus._requiere_acceso_nexus(req, "alguien")  # no debe lanzar


# ── db.py::load_home_overview() -- dashboard de inicio (main.py::inicio) ───

class HomeOverviewTests(EntornoAisladoTestCase):
    def setUp(self):
        self.proyecto_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        self._task_ids = []
        db.crear_proyecto(
            self.proyecto_id, f"{TEST_PREFIX}Proyecto", "", "", "#123456",
            "Activo", "kanban", "", "",
        )

    def tearDown(self):
        # eliminar_proyecto() NO borra las tareas del proyecto -- las
        # reasigna a 'general' (ver db.py::eliminar_proyecto). Hay que
        # borrarlas explicitamente antes, o quedarian ahi para siempre.
        if self._task_ids:
            conn = db.connect()
            try:
                with conn.cursor() as cur:
                    placeholders = ",".join(["%s"] * len(self._task_ids))
                    cur.execute(f"DELETE FROM tareas WHERE id IN ({placeholders})", self._task_ids)
                conn.commit()
            finally:
                conn.close()
        db.eliminar_proyecto(self.proyecto_id)

    def _crear_tarea(self, **kwargs):
        task_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        db.crear_tarea(task_id, self.proyecto_id, "", **kwargs)
        self._task_ids.append(task_id)
        return task_id

    def test_tarea_vencida_aparece(self):
        task_id = self._crear_tarea(title="Tarea vencida", status="Pendiente", priority="Normal",
                                     description="", next_step="", fecha_inicio="", fecha_fin="2000-01-01")
        tasks = db.load_home_overview()
        t = next(t for t in tasks if t["task_id"] == task_id)
        self.assertEqual(t["fecha_fin"], "2000-01-01")
        self.assertEqual(t["proyecto_id"], self.proyecto_id)

    def test_tarea_prioridad_alta_aparece(self):
        task_id = self._crear_tarea(title="Tarea urgente", status="Pendiente", priority="Alta",
                                     description="", next_step="", fecha_inicio="", fecha_fin="")
        tasks = db.load_home_overview()
        t = next(t for t in tasks if t["task_id"] == task_id)
        self.assertEqual(t["priority"], "Alta")

    def test_tarea_hecha_no_cuenta_como_pendiente(self):
        task_id = self._crear_tarea(title="Tarea hecha", status="Hecha", priority="Alta",
                                     description="", next_step="", fecha_inicio="", fecha_fin="2000-01-01")
        tasks = db.load_home_overview()
        done_names = db.done_status_names_by_project()
        pendientes = [t for t in tasks if t["status"] not in done_names.get(t["proyecto_id"], {"Hecha"})]
        self.assertFalse(any(t["task_id"] == task_id for t in pendientes))

    def test_proyecto_borrado_no_aparece(self):
        """load_home_overview() filtra por p.activo=TRUE -- un proyecto que
        no existe (o esta desactivado) no debe aportar tareas."""
        tasks = db.load_home_overview()
        self.assertFalse(any(t["proyecto_id"] == "AUTOTEST_no-existe" for t in tasks))


# ── GET / (main.py::inicio) -- integracion completa via TestClient ─────────

class HomeRouteTests(EntornoAisladoTestCase):
    def setUp(self):
        app.dependency_overrides[auth.requiere_login] = lambda: "test-user"
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        app.dependency_overrides.pop(auth.requiere_login, None)

    def test_inicio_responde_200_autenticado(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)

    def test_inicio_redirige_a_login_sin_sesion(self):
        app.dependency_overrides.pop(auth.requiere_login, None)
        r = self.client.get("/")
        self.assertEqual(r.status_code, 307)
        self.assertTrue(r.headers["location"].startswith("/login/"))

    def test_peticion_de_fondo_sin_sesion_da_401(self):
        # la campana no debe apuntarse como destino del login
        app.dependency_overrides.pop(auth.requiere_login, None)
        r = self.client.get("/notificaciones/resumen", headers={"Accept": "application/json"}, follow_redirects=False)
        self.assertEqual(r.status_code, 401)
        r = self.client.get("/", headers={"Sec-Fetch-Mode": "cors"}, follow_redirects=False)
        self.assertEqual(r.status_code, 401)
        r = self.client.get("/", headers={"Sec-Fetch-Mode": "navigate", "Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(r.status_code, 307)


# ── espacios (workspaces) -- fase 1 del multiusuario, ver CLAUDE.md ─────────

class EspaciosTests(EntornoAisladoTestCase):
    def _crear_usuario_de_prueba(self):
        """No hay todavia gestion de usuarios en la app -- inserta la fila
        de auth_user a mano, igual que hace scripts/init_test_db.py con
        un usuario de ejemplo. password='!...' = convencion de Django para "sin login
        real", no hace falta un hash valido para estos tests."""
        username = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO auth_user (password, is_superuser, username, first_name, last_name, email, "
                    "is_staff, is_active, date_joined) VALUES ('!sin-password-de-pruebas', 0, %s, '', '', '', 0, 1, NOW())",
                    (username,),
                )
                user_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        return user_id

    def _borrar_usuario_de_prueba(self, user_id):
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM auth_user WHERE id=%s", (user_id,))
            conn.commit()
        finally:
            conn.close()

    def test_espacio_personal_se_crea_una_sola_vez(self):
        user_id = self._crear_usuario_de_prueba()
        try:
            primero = db.get_or_create_espacio_personal(user_id)
            segundo = db.get_or_create_espacio_personal(user_id)
            self.assertEqual(primero["id"], segundo["id"])
            espacios = [e for e in db.load_espacios() if e["id"] == primero["id"]]
            self.assertEqual(len(espacios), 1)
            self.assertTrue(espacios[0]["es_personal"])
        finally:
            self._borrar_usuario_de_prueba(user_id)  # ON DELETE CASCADE se lleva el espacio y la membership

    def test_espacio_personal_no_se_puede_desactivar(self):
        user_id = self._crear_usuario_de_prueba()
        try:
            espacio = db.get_or_create_espacio_personal(user_id)
            self.assertFalse(db.toggle_espacio(espacio["id"]))
            activo = next(e["activo"] for e in db.load_espacios() if e["id"] == espacio["id"])
            self.assertTrue(activo)
        finally:
            self._borrar_usuario_de_prueba(user_id)

    def test_espacio_compartido_miembros_y_proyecto(self):
        user_id = self._crear_usuario_de_prueba()
        espacio_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        proyecto_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        db.crear_espacio(espacio_id, f"{TEST_PREFIX}Espacio", "#123456", user_id)
        try:
            db.agregar_miembro_espacio(espacio_id, user_id)
            miembros = db.load_miembros_espacio(espacio_id)
            self.assertEqual(len(miembros), 1)
            self.assertEqual(miembros[0]["id"], user_id)

            db.quitar_miembro_espacio(espacio_id, user_id)
            self.assertEqual(db.load_miembros_espacio(espacio_id), [])

            db.crear_proyecto(proyecto_id, f"{TEST_PREFIX}Proyecto", "", "", "#123456",
                               "Activo", "kanban", "", "", espacio_id=espacio_id)
            proyectos = db.load_proyectos_por_espacio(espacio_id)
            self.assertEqual([p["id"] for p in proyectos], [proyecto_id])

            self.assertTrue(db.toggle_espacio(espacio_id))  # desactivar
            self.assertTrue(db.toggle_espacio(espacio_id))  # reactivar
        finally:
            db.eliminar_proyecto(proyecto_id)
            self._borrar_usuario_de_prueba(user_id)
            conn = db.connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM espacios WHERE id=%s", (espacio_id,))
                conn.commit()
            finally:
                conn.close()

    def test_creador_de_espacio_compartido_es_admin(self):
        user_id = self._crear_usuario_de_prueba()
        espacio_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        db.crear_espacio(espacio_id, f"{TEST_PREFIX}Espacio", "#123456", user_id)
        try:
            self.assertEqual(db.rol_en_espacio(espacio_id, user_id), "admin")
            self.assertTrue(db.puede_administrar_espacio(espacio_id, user_id))
            self.assertTrue(db.puede_editar_espacio(espacio_id, user_id))
        finally:
            self._borrar_espacio_de_prueba(espacio_id)
            self._borrar_usuario_de_prueba(user_id)

    def test_roles_admin_editor_lector(self):
        admin_id = self._crear_usuario_de_prueba()
        editor_id = self._crear_usuario_de_prueba()
        lector_id = self._crear_usuario_de_prueba()
        espacio_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        db.crear_espacio(espacio_id, f"{TEST_PREFIX}Espacio", "#123456", admin_id)
        try:
            db.agregar_miembro_espacio(espacio_id, editor_id, "editor")
            db.agregar_miembro_espacio(espacio_id, lector_id, "lector")

            self.assertTrue(db.puede_administrar_espacio(espacio_id, admin_id))
            self.assertFalse(db.puede_administrar_espacio(espacio_id, editor_id))
            self.assertFalse(db.puede_administrar_espacio(espacio_id, lector_id))

            self.assertTrue(db.puede_editar_espacio(espacio_id, admin_id))
            self.assertTrue(db.puede_editar_espacio(espacio_id, editor_id))
            self.assertFalse(db.puede_editar_espacio(espacio_id, lector_id))

            db.cambiar_rol_miembro_espacio(espacio_id, lector_id, "editor")
            self.assertTrue(db.puede_editar_espacio(espacio_id, lector_id))
        finally:
            self._borrar_espacio_de_prueba(espacio_id)
            self._borrar_usuario_de_prueba(admin_id)
            self._borrar_usuario_de_prueba(editor_id)
            self._borrar_usuario_de_prueba(lector_id)

    def _borrar_espacio_de_prueba(self, espacio_id):
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM espacios WHERE id=%s", (espacio_id,))
            conn.commit()
        finally:
            conn.close()


class EspacioDetallePermisosTests(EntornoAisladoTestCase):
    """Enforcement de rol en las rutas de administracion de un espacio
    (routers/proyectos.py, gestionadas desde /espacio/{id}/) -- un Lector
    no puede editar/gestionar miembros, un Admin si."""

    def setUp(self):
        app.dependency_overrides[csrf.proteger] = lambda: None  # se prueba el permiso, no el CSRF
        self.client = TestClient(app, follow_redirects=False)
        self.admin_id = self._crear_usuario_de_prueba("admin")
        self.lector_id = self._crear_usuario_de_prueba("lector")
        self.espacio_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        db.crear_espacio(self.espacio_id, f"{TEST_PREFIX}Espacio", "#123456", self.admin_id)
        db.agregar_miembro_espacio(self.espacio_id, self.lector_id, "lector")

    def tearDown(self):
        app.dependency_overrides.pop(auth.requiere_login, None)
        app.dependency_overrides.pop(csrf.proteger, None)
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM espacios WHERE id=%s", (self.espacio_id,))
                cur.execute("DELETE FROM auth_user WHERE id IN (%s, %s)", (self.admin_id, self.lector_id))
            conn.commit()
        finally:
            conn.close()

    def _crear_usuario_de_prueba(self, etiqueta):
        username = f"{TEST_PREFIX}{etiqueta}_{uuid.uuid4().hex[:8]}"
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO auth_user (password, is_superuser, username, first_name, last_name, email, "
                    "is_staff, is_active, date_joined) VALUES ('!sin-password-de-pruebas', 0, %s, '', '', '', 0, 1, NOW())",
                    (username,),
                )
                user_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        return user_id

    def _login_como(self, username):
        app.dependency_overrides[auth.requiere_login] = lambda: username

    def test_lector_no_puede_editar_espacio(self):
        lector_username = next(m["username"] for m in db.load_miembros_espacio(self.espacio_id) if m["rol"] == "lector")
        self._login_como(lector_username)
        r = self.client.post(f"/espacio/{self.espacio_id}/editar/",
                              data={"nombre": "Otro nombre"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(db.get_espacio(self.espacio_id)["nombre"], f"{TEST_PREFIX}Espacio")

    def test_admin_si_puede_editar_espacio(self):
        admin_username = next(m["username"] for m in db.load_miembros_espacio(self.espacio_id) if m["rol"] == "admin")
        self._login_como(admin_username)
        r = self.client.post(f"/espacio/{self.espacio_id}/editar/",
                              data={"nombre": "Nombre nuevo"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(db.get_espacio(self.espacio_id)["nombre"], "Nombre nuevo")


class EliminarProyectoDeEspacioTests(EntornoAisladoTestCase):
    """POST /espacio/{id}/proyecto/{id}/eliminar/ -- unico sitio de toda la
    app donde se puede borrar un proyecto (consolidado aqui, ver
    project_config_scope en la memoria de esta sesion): solo un Admin del
    espacio, y solo si 'confirmacion' coincide EXACTO con el nombre del
    proyecto (doble confirmacion -- el JS ya lo impide, esto prueba que el
    servidor tambien lo revalida, por si alguien se salta el JS a mano)."""

    def setUp(self):
        app.dependency_overrides[csrf.proteger] = lambda: None
        self.client = TestClient(app, follow_redirects=False)
        self.admin_id = self._crear_usuario_de_prueba("admin")
        self.lector_id = self._crear_usuario_de_prueba("lector")
        self.espacio_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        self.proyecto_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        self.proyecto_nombre = f"{TEST_PREFIX}Proyecto"
        db.crear_espacio(self.espacio_id, f"{TEST_PREFIX}Espacio", "#123456", self.admin_id)
        db.agregar_miembro_espacio(self.espacio_id, self.lector_id, "lector")
        db.crear_proyecto(self.proyecto_id, self.proyecto_nombre, "", "", "#123456",
                           "Activo", "kanban", "", "", espacio_id=self.espacio_id)

    def tearDown(self):
        app.dependency_overrides.pop(auth.requiere_login, None)
        app.dependency_overrides.pop(csrf.proteger, None)
        db.eliminar_proyecto(self.proyecto_id)  # no-op si ya se borro en el test
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM espacios WHERE id=%s", (self.espacio_id,))
                cur.execute("DELETE FROM auth_user WHERE id IN (%s, %s)", (self.admin_id, self.lector_id))
            conn.commit()
        finally:
            conn.close()

    def _crear_usuario_de_prueba(self, etiqueta):
        username = f"{TEST_PREFIX}{etiqueta}_{uuid.uuid4().hex[:8]}"
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO auth_user (password, is_superuser, username, first_name, last_name, email, "
                    "is_staff, is_active, date_joined) VALUES ('!sin-password-de-pruebas', 0, %s, '', '', '', 0, 1, NOW())",
                    (username,),
                )
                user_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        return user_id

    def _login_como(self, username):
        app.dependency_overrides[auth.requiere_login] = lambda: username

    def _username(self, user_id):
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT username FROM auth_user WHERE id=%s", (user_id,))
                return cur.fetchone()[0]
        finally:
            conn.close()

    def test_lector_no_puede_eliminar(self):
        self._login_como(self._username(self.lector_id))
        r = self.client.post(f"/espacio/{self.espacio_id}/proyecto/{self.proyecto_id}/eliminar/",
                              data={"confirmacion": self.proyecto_nombre})
        self.assertEqual(r.status_code, 303)
        self.assertIsNotNone(db.get_proyecto(self.proyecto_id))

    def test_admin_con_confirmacion_incorrecta_no_elimina(self):
        self._login_como(self._username(self.admin_id))
        r = self.client.post(f"/espacio/{self.espacio_id}/proyecto/{self.proyecto_id}/eliminar/",
                              data={"confirmacion": "nombre que no coincide"})
        self.assertEqual(r.status_code, 303)
        self.assertIsNotNone(db.get_proyecto(self.proyecto_id))

    def test_admin_con_confirmacion_correcta_elimina(self):
        self._login_como(self._username(self.admin_id))
        r = self.client.post(f"/espacio/{self.espacio_id}/proyecto/{self.proyecto_id}/eliminar/",
                              data={"confirmacion": self.proyecto_nombre})
        self.assertEqual(r.status_code, 303)
        self.assertIsNone(db.get_proyecto(self.proyecto_id))


# ── permisos granulares de Editor por espacio (Fase B, ver plan/CLAUDE.md) ──

class PuedeActuarEnProyectoTests(EntornoAisladoTestCase):
    """db.puede_actuar_en_proyecto -- punto unico de permiso para tarea/
    columna/documento/wiki dentro de un proyecto YA abierto (distinto de
    puede_editar_espacio, que es para el proyecto en si)."""

    def setUp(self):
        self.admin_id = self._crear_usuario_de_prueba()
        self.editor_id = self._crear_usuario_de_prueba()
        self.lector_id = self._crear_usuario_de_prueba()
        self.espacio_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        self.proyecto_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        db.crear_espacio(self.espacio_id, f"{TEST_PREFIX}Espacio", "#123456", self.admin_id)
        db.agregar_miembro_espacio(self.espacio_id, self.editor_id, "editor")
        db.agregar_miembro_espacio(self.espacio_id, self.lector_id, "lector")
        db.crear_proyecto(self.proyecto_id, f"{TEST_PREFIX}Proyecto", "", "", "#123456",
                           "Activo", "kanban", "", "", espacio_id=self.espacio_id)

    def tearDown(self):
        db.eliminar_proyecto(self.proyecto_id)
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM espacios WHERE id=%s", (self.espacio_id,))
                cur.execute("DELETE FROM auth_user WHERE id IN (%s, %s, %s)",
                             (self.admin_id, self.editor_id, self.lector_id))
            conn.commit()
        finally:
            conn.close()

    def _crear_usuario_de_prueba(self):
        username = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO auth_user (password, is_superuser, username, first_name, last_name, email, "
                    "is_staff, is_active, date_joined) VALUES ('!sin-password-de-pruebas', 0, %s, '', '', '', 0, 1, NOW())",
                    (username,),
                )
                user_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        return user_id

    def test_proyecto_sin_espacio_siempre_true(self):
        proyecto_legado = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        db.crear_proyecto(proyecto_legado, f"{TEST_PREFIX}Legado", "", "", "#123456", "Activo", "kanban", "", "")
        try:
            self.assertTrue(db.puede_actuar_en_proyecto(proyecto_legado, self.lector_id, "editores_eliminan_tareas_ajenas"))
        finally:
            db.eliminar_proyecto(proyecto_legado)

    def test_admin_siempre_true(self):
        db.editar_permisos_espacio(self.espacio_id, editores_eliminan_tareas_ajenas=False)
        self.assertTrue(db.puede_actuar_en_proyecto(self.proyecto_id, self.admin_id, "editores_eliminan_tareas_ajenas"))

    def test_lector_siempre_false(self):
        self.assertFalse(db.puede_actuar_en_proyecto(self.proyecto_id, self.lector_id, "editores_eliminan_tareas_ajenas"))
        # incluso sobre una tarea "suya" -- un Lector no puede nada de esto
        # de por si, el bypass de "tarea propia" solo aplica a un Editor.
        self.assertFalse(db.puede_actuar_en_proyecto(
            self.proyecto_id, self.lector_id, "editores_eliminan_tareas_ajenas", self.lector_id))

    def test_editor_con_interruptor_on(self):
        self.assertTrue(db.puede_actuar_en_proyecto(self.proyecto_id, self.editor_id, "editores_eliminan_tareas_ajenas"))

    def test_it_puede_aunque_no_sea_miembro(self):
        """Con Keycloak, IT llega en el login (grupo) y se apunta en auth_user:
        tiene que poder actuar en espacios de los que no es miembro."""
        it_id = self._crear_usuario_de_prueba()
        try:
            conn = db.connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT username FROM auth_user WHERE id=%s", (it_id,))
                    username = cur.fetchone()[0]
            finally:
                conn.close()
            self.assertFalse(db.puede_actuar_en_proyecto(self.proyecto_id, it_id, "editores_crean_columnas"))
            db.marcar_it(username, True)
            self.assertTrue(db.puede_actuar_en_proyecto(self.proyecto_id, it_id, "editores_crean_columnas"))
            db.marcar_it(username, False)
            self.assertFalse(db.puede_actuar_en_proyecto(self.proyecto_id, it_id, "editores_crean_columnas"))
        finally:
            conn = db.connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM auth_user WHERE id=%s", (it_id,))
                conn.commit()
            finally:
                conn.close()

    def test_editor_con_interruptor_off_tarea_ajena(self):
        db.editar_permisos_espacio(self.espacio_id, editores_eliminan_tareas_ajenas=False)
        self.assertFalse(db.puede_actuar_en_proyecto(
            self.proyecto_id, self.editor_id, "editores_eliminan_tareas_ajenas", self.admin_id))

    def test_editor_con_interruptor_off_tarea_propia(self):
        db.editar_permisos_espacio(self.espacio_id, editores_eliminan_tareas_ajenas=False)
        self.assertTrue(db.puede_actuar_en_proyecto(
            self.proyecto_id, self.editor_id, "editores_eliminan_tareas_ajenas", self.editor_id))


class TareaPermisosRouterTests(EntornoAisladoTestCase):
    """Enforcement end-to-end (routers/tablero.py::borrar_tarea) del
    interruptor 'editores_eliminan_tareas_ajenas': un Editor sin ese permiso
    no puede borrar la tarea de otro, pero si la suya propia; un Admin
    siempre puede, tenga o no el interruptor activado."""

    def setUp(self):
        app.dependency_overrides[csrf.proteger] = lambda: None  # se prueba el permiso, no el CSRF
        self.client = TestClient(app, follow_redirects=False)
        self.admin_id, self.admin_username = self._crear_usuario_de_prueba("admin")
        self.editor_id, self.editor_username = self._crear_usuario_de_prueba("editor")
        self.espacio_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        self.proyecto_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        db.crear_espacio(self.espacio_id, f"{TEST_PREFIX}Espacio", "#123456", self.admin_id)
        db.agregar_miembro_espacio(self.espacio_id, self.editor_id, "editor")
        db.crear_proyecto(self.proyecto_id, f"{TEST_PREFIX}Proyecto", "", "", "#123456",
                           "Activo", "kanban", "", "", espacio_id=self.espacio_id)
        db.editar_permisos_espacio(self.espacio_id, editores_eliminan_tareas_ajenas=False)
        self._task_ids = []

    def tearDown(self):
        app.dependency_overrides.pop(auth.requiere_login, None)
        app.dependency_overrides.pop(csrf.proteger, None)
        if self._task_ids:
            conn = db.connect()
            try:
                with conn.cursor() as cur:
                    placeholders = ",".join(["%s"] * len(self._task_ids))
                    cur.execute(f"DELETE FROM tareas WHERE id IN ({placeholders})", self._task_ids)
                conn.commit()
            finally:
                conn.close()
        db.eliminar_proyecto(self.proyecto_id)
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM espacios WHERE id=%s", (self.espacio_id,))
                cur.execute("DELETE FROM auth_user WHERE id IN (%s, %s)", (self.admin_id, self.editor_id))
            conn.commit()
        finally:
            conn.close()

    def _crear_usuario_de_prueba(self, etiqueta):
        username = f"{TEST_PREFIX}{etiqueta}_{uuid.uuid4().hex[:8]}"
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO auth_user (password, is_superuser, username, first_name, last_name, email, "
                    "is_staff, is_active, date_joined) VALUES ('!sin-password-de-pruebas', 0, %s, '', '', '', 0, 1, NOW())",
                    (username,),
                )
                user_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        return user_id, username

    def _crear_tarea(self, asignado_a=None):
        task_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        db.crear_tarea(task_id, self.proyecto_id, "", "Tarea", "Pendiente", "Normal", "", "", "", "",
                        asignado_a=asignado_a)
        self._task_ids.append(task_id)
        return task_id

    def _login_como(self, username):
        app.dependency_overrides[auth.requiere_login] = lambda: username

    def _sigue_sin_borrar(self, task_id):
        _, estado, _, _ = db.tarea_estado_actual(task_id)
        return estado is not None

    def test_editor_sin_permiso_no_puede_borrar_tarea_ajena(self):
        task_id = self._crear_tarea(asignado_a=self.admin_id)
        self._login_como(self.editor_username)
        r = self.client.post(f"/tarea/{task_id}/borrar-tablero/", data={"proyecto": self.proyecto_id})
        self.assertEqual(r.status_code, 303)
        self.assertTrue(self._sigue_sin_borrar(task_id))

    def test_editor_sin_permiso_si_puede_borrar_su_propia_tarea(self):
        task_id = self._crear_tarea(asignado_a=self.editor_id)
        self._login_como(self.editor_username)
        r = self.client.post(f"/tarea/{task_id}/borrar-tablero/", data={"proyecto": self.proyecto_id})
        self.assertEqual(r.status_code, 303)
        self.assertFalse(self._sigue_sin_borrar(task_id))

    def test_admin_siempre_puede_borrar(self):
        task_id = self._crear_tarea(asignado_a=self.editor_id)
        self._login_como(self.admin_username)
        r = self.client.post(f"/tarea/{task_id}/borrar-tablero/", data={"proyecto": self.proyecto_id})
        self.assertEqual(r.status_code, 303)
        self.assertFalse(self._sigue_sin_borrar(task_id))


class ConfiguracionRequiereITTests(unittest.TestCase):
    """/configuracion/ entero exige ser de IT
    (auth.requiere_it) -- ser Admin de un espacio no basta."""

    def setUp(self):
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        app.dependency_overrides.pop(auth.requiere_login, None)
        app.dependency_overrides.pop(auth.requiere_it, None)

    def test_sin_grupo_it_redirige_a_inicio(self):
        app.dependency_overrides[auth.requiere_login] = lambda: "test-user"
        r = self.client.get("/configuracion/")
        self.assertEqual(r.status_code, 307)
        self.assertEqual(r.headers["location"], "/")

    def test_con_grupo_it_entra(self):
        # el router exige requiere_it (guardia global), y ver_configuracion
        # ademas pide requiere_login por su cuenta (para tener 'usuario') --
        # hay que sobreescribir ambas.
        app.dependency_overrides[auth.requiere_it] = lambda: "test-user"
        app.dependency_overrides[auth.requiere_login] = lambda: "test-user"
        r = self.client.get("/configuracion/")
        self.assertEqual(r.status_code, 200)


# ── agenda (hub por dia) -- ver CLAUDE.md, seccion Agenda ───────────────────

class AgendaTests(EntornoAisladoTestCase):
    FECHA = "2030-01-15"  # fecha de prueba fija y lejana, no choca con datos reales

    def setUp(self):
        self._task_ids = []
        self._nota_ids = []
        self._whiteboard_ids = []

    def tearDown(self):
        if self._task_ids:
            conn = db.connect()
            try:
                with conn.cursor() as cur:
                    placeholders = ",".join(["%s"] * len(self._task_ids))
                    cur.execute(f"DELETE FROM tareas WHERE id IN ({placeholders})", self._task_ids)
                conn.commit()
            finally:
                conn.close()
        for nota_id in self._nota_ids:
            db.eliminar_nota(nota_id)
        if self._whiteboard_ids:
            conn = db.connect()
            try:
                with conn.cursor() as cur:
                    placeholders = ",".join(["%s"] * len(self._whiteboard_ids))
                    cur.execute(f"DELETE FROM whiteboards WHERE id IN ({placeholders})", self._whiteboard_ids)
                conn.commit()
            finally:
                conn.close()

    def test_tarea_con_fecha_fin_aparece_ese_dia(self):
        task_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        db.crear_tarea(task_id, "general", "", "Tarea de agenda", "Pendiente", "Normal", "", "", "", self.FECHA)
        self._task_ids.append(task_id)
        tareas = db.load_tasks_by_due_date(self.FECHA, self.FECHA)
        self.assertTrue(any(t["task_id"] == task_id for t in tareas))

    def test_nota_anclada_al_dia_aparece(self):
        nota_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        db.crear_nota(nota_id, "Nota de agenda", "", "#fef9c3", agenda_fecha=self.FECHA)
        self._nota_ids.append(nota_id)
        notas = [n for n in db.load_notas() if n["agenda_fecha"] == self.FECHA]
        self.assertTrue(any(n["nota_id"] == nota_id for n in notas))

    def test_pizarra_vinculada_al_dia(self):
        wb_id = db.create_whiteboard(f"{TEST_PREFIX}Pizarra", "[]", "", "abc123")
        self._whiteboard_ids.append(wb_id)
        self.assertIsNone(db.get_whiteboard_by_agenda_fecha(self.FECHA))
        db.set_whiteboard_agenda_fecha(wb_id, self.FECHA)
        wb = db.get_whiteboard_by_agenda_fecha(self.FECHA)
        self.assertIsNotNone(wb)
        self.assertEqual(wb["whiteboard_id"], wb_id)

    def test_ruta_agenda_agrega_tareas_y_notas_del_dia(self):
        task_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        db.crear_tarea(task_id, "general", "", "Tarea visible en agenda", "Pendiente", "Normal",
                        "", "", "", self.FECHA)
        self._task_ids.append(task_id)
        nota_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        db.crear_nota(nota_id, "Nota visible en agenda", "", "#fef9c3", agenda_fecha=self.FECHA)
        self._nota_ids.append(nota_id)

        app.dependency_overrides[auth.requiere_login] = lambda: "test-user"
        try:
            client = TestClient(app, follow_redirects=False)
            r = client.get(f"/agenda/{self.FECHA}/")
            self.assertEqual(r.status_code, 200)
            self.assertIn("Tarea visible en agenda", r.text)
            self.assertIn("Nota visible en agenda", r.text)
        finally:
            app.dependency_overrides.pop(auth.requiere_login, None)


if __name__ == "__main__":
    unittest.main()


# ── avisos de escritorio: Recordatorio + Vigilante de Mantis ───────────────
# (escritorio_servicios.py). USAR_BD_REAL=False para quedarse en
# test_app_tareas, y el toast de Windows se mockea siempre: ningun test
# muestra un aviso de verdad ni llama a la API de Mantis.

import json as _json  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from datetime import datetime as _dt  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from . import escritorio_servicios  # noqa: E402


class EscritorioServiciosTests(EntornoAisladoTestCase):
    AJUSTES = ["recordatorio.activo", "recordatorio.ultimo_ts", "mantis.watcher_pausado", "vigilante.formato", "vigilante.titulo",
               "vigilante.solo_horario", "recordatorio.solo_horario", "recordatorio.estado_ultima",
               "recordatorio.estado_resultado", "vigilante.estado_ultima", "vigilante.estado_resultado"]

    def setUp(self):
        self._real_previo = escritorio_servicios.USAR_BD_REAL
        escritorio_servicios.USAR_BD_REAL = False
        self._ajustes_previos = {k: db.get_ajuste(k) for k in self.AJUSTES}
        self._mensajes = []
        self.toast = patch.object(escritorio_servicios, "mostrar_toast").start()

    def tearDown(self):
        patch.stopall()
        for mid in self._mensajes:
            db.eliminar_recordatorio_mensaje(mid)
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                for clave, valor in self._ajustes_previos.items():
                    if valor is None:
                        cur.execute("DELETE FROM ajustes_app WHERE clave=%s", (clave,))
                    else:
                        cur.execute("UPDATE ajustes_app SET valor=%s WHERE clave=%s", (valor, clave))
            conn.commit()
        finally:
            conn.close()
        escritorio_servicios.USAR_BD_REAL = self._real_previo

    def _mensaje(self, texto="mensaje", categoria=""):
        mid = db.crear_recordatorio_mensaje(f"{TEST_PREFIX}{texto}", categoria)
        self._mensajes.append(mid)
        return mid

    def test_formatear_variable_desconocida_no_rompe(self):
        self.assertEqual(escritorio_servicios.formatear("#{id}\\n{nada}", {"id": 7}), "#7\n{nada}")

    def test_en_horario(self):
        cfg = {"solo_horario": "1", "dias": "1,2,3,4,5", "hora_inicio": "08:00", "hora_fin": "18:00"}
        self.assertTrue(escritorio_servicios.en_horario(cfg, _dt(2030, 1, 14, 9, 0)))    # lunes 9:00
        self.assertFalse(escritorio_servicios.en_horario(cfg, _dt(2030, 1, 14, 18, 0)))  # lunes 18:00
        self.assertFalse(escritorio_servicios.en_horario(cfg, _dt(2030, 1, 19, 10, 0)))  # sabado
        self.assertTrue(escritorio_servicios.en_horario({**cfg, "solo_horario": ""}, _dt(2030, 1, 19, 3, 0)))

    def test_rotacion_elige_el_menos_mostrado(self):
        lista = [
            {"id": 1, "veces_mostrado": 3, "ultimo_mostrado": "2030-01-01 10:00", "orden": 1},
            {"id": 2, "veces_mostrado": 1, "ultimo_mostrado": "2030-01-02 10:00", "orden": 2},
            {"id": 3, "veces_mostrado": 1, "ultimo_mostrado": "2030-01-01 09:00", "orden": 3},
        ]
        with patch.object(db, "load_recordatorio_mensajes", return_value=lista):
            self.assertEqual(escritorio_servicios.elegir_mensaje("rotacion")["id"], 3)
            self.assertEqual(escritorio_servicios.elegir_mensaje("orden")["id"], 2)

    def test_crud_mensajes(self):
        mid = self._mensaje("crud", "Postura")
        db.editar_recordatorio_mensaje(mid, f"{TEST_PREFIX}editado", "Vista")
        db.toggle_recordatorio_mensaje(mid)
        m = next(x for x in db.load_recordatorio_mensajes() if x["id"] == mid)
        self.assertEqual((m["texto"], m["categoria"], m["activo"]), (f"{TEST_PREFIX}editado", "Vista", False))
        self.assertNotIn(mid, [x["id"] for x in db.load_recordatorio_mensajes(solo_activos=True)])

    def test_recordatorio_muestra_y_cuenta(self):
        mid = self._mensaje("muestra", "Postura")
        db.set_ajuste("recordatorio.activo", "1")
        db.set_ajuste("recordatorio.solo_horario", "")
        elegido = next(x for x in db.load_recordatorio_mensajes() if x["id"] == mid)
        with patch.object(escritorio_servicios, "elegir_mensaje", return_value=elegido), \
             patch.object(db, "hoy_es_festivo", return_value=False):
            escritorio_servicios.enviar_recordatorio()
        self.assertEqual(self.toast.call_args.args[2], f"{TEST_PREFIX}muestra")
        m = next(x for x in db.load_recordatorio_mensajes() if x["id"] == mid)
        self.assertEqual(m["veces_mostrado"], 1)

    def test_recordatorio_sobrevive_a_reinicios(self):
        t = escritorio_servicios.toca_ahora
        ultimo_aviso = 10_000.0          # salio un aviso
        intervalo = 15 * 60
        # la app se reinicia 10 min despues: a los 15 min del ultimo aviso toca, aunque la app lleve solo 5
        inicio = ultimo_aviso + 600
        self.assertFalse(t("recordatorio", ultimo_aviso + 14 * 60, inicio, 0.0, intervalo, ultimo_aviso))
        self.assertTrue(t("recordatorio", ultimo_aviso + 15 * 60, inicio, 0.0, intervalo, ultimo_aviso))
        # recien arrancada con un aviso viejo: espera la gracia de arranque, no salta al instante
        inicio = 50_000.0
        self.assertFalse(t("recordatorio", inicio + 30, inicio, 0.0, intervalo, ultimo_aviso))
        self.assertTrue(t("recordatorio", inicio + escritorio_servicios.GRACIA_ARRANQUE, inicio, 0.0, intervalo, ultimo_aviso))
        # fuera de horario (intento sin aviso): no reintenta hasta el siguiente intervalo
        self.assertFalse(t("recordatorio", inicio + 200, inicio, inicio + 150, intervalo, ultimo_aviso))
        # el vigilante revisa enseguida
        self.assertTrue(t("vigilante", inicio, inicio, 0.0, 300))

    def test_recordatorio_pausado_no_muestra(self):
        db.set_ajuste("recordatorio.activo", "")
        self.assertEqual(escritorio_servicios.enviar_recordatorio(), "desactivado")
        self.toast.assert_not_called()

    def test_vigilante_pausado_no_llama_a_mantis(self):
        db.set_ajuste("mantis.watcher_pausado", "1")
        with patch.object(escritorio_servicios.mantis_client, "fetch_pool_issues") as fetch:
            self.assertEqual(escritorio_servicios.revisar_mantis(), "pausado")
        fetch.assert_not_called()

    def test_vigilante_avisa_solo_lo_nuevo_con_formato(self):
        db.set_ajuste("mantis.watcher_pausado", "")
        db.set_ajuste("vigilante.solo_horario", "")
        db.set_ajuste("vigilante.titulo", "Nueva en {empresa}")
        db.set_ajuste("vigilante.formato", "#{id}: {resumen}")
        incidencias = [
            {"id": 1, "proyecto_id": 999, "resumen": "vieja", "url": "", "creada": ""},
            {"id": 2, "proyecto_id": 999, "resumen": "nueva", "url": "", "creada": ""},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            estado = _Path(tmp) / "estado.json"
            estado.write_text("[1]", encoding="utf-8")
            with patch.object(escritorio_servicios, "ESTADO_VIGILANTE", estado), \
                 patch.object(escritorio_servicios, "_empresas_mantis", return_value=(1, [999], {999: {"id": "x", "nombre": "Demo"}})), \
                 patch.object(escritorio_servicios, "_clasificar_y_cachear"), \
                 patch.object(escritorio_servicios.mantis_client, "fetch_pool_issues", return_value=incidencias):
                escritorio_servicios.revisar_mantis()
            self.assertEqual(self.toast.call_count, 1)
            self.assertEqual(self.toast.call_args.args[1:3], ("Nueva en Demo", "#2: nueva"))
            self.assertEqual(sorted(_json.loads(estado.read_text())), [1, 2])


class EscritorioConfiguracionTests(EntornoAisladoTestCase):
    def setUp(self):
        self._real_previo = escritorio_servicios.USAR_BD_REAL
        escritorio_servicios.USAR_BD_REAL = False
        app.dependency_overrides[auth.requiere_it] = lambda: "test-user"
        app.dependency_overrides[auth.requiere_login] = lambda: "test-user"
        app.dependency_overrides[csrf.proteger] = lambda: None
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        for dep in (auth.requiere_it, auth.requiere_login, csrf.proteger):
            app.dependency_overrides.pop(dep, None)
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM recordatorio_mensajes WHERE texto LIKE %s", (f"{TEST_PREFIX}%",))
            conn.commit()
        finally:
            conn.close()
        escritorio_servicios.USAR_BD_REAL = self._real_previo

    def test_paneles_se_ven(self):
        for tipo in ("recordatorio", "vigilante", "mantis", "vigia"):
            r = self.client.get(f"/configuracion/?tipo={tipo}")
            self.assertEqual(r.status_code, 200, tipo)
        self.assertIn("Mensajes (", self.client.get("/configuracion/?tipo=recordatorio").text)
        self.assertIn("CARPETA DE FALLIDOS", self.client.get("/configuracion/?tipo=vigia").text)

    def test_pegar_varios_mensajes_de_golpe(self):
        r = self.client.post("/configuracion/recordatorio/mensaje/nuevo/",
                             data={"texto": f"{TEST_PREFIX}uno\n\n{TEST_PREFIX}dos\n", "categoria": "Vista"})
        self.assertEqual(r.status_code, 303)
        nuevos = [m for m in db.load_recordatorio_mensajes() if m["texto"].startswith(TEST_PREFIX)]
        self.assertEqual(sorted(m["texto"] for m in nuevos), [f"{TEST_PREFIX}dos", f"{TEST_PREFIX}uno"])
        self.assertTrue(all(m["categoria"] == "Vista" for m in nuevos))


# ── Vigia de carpeta (escritorio_servicios.revisar_vigia) ──────────────────────────────
# Carpeta de integracion falsa en un directorio temporal, estado en otro
# fichero temporal, BD de pruebas (USAR_BD_REAL=False) y un usuario de prueba
# propio como destinatario: borrarlo se lleva sus notas y notificaciones.

class VigiaEdiTests(EntornoAisladoTestCase):
    CLAVES = ["activo", "destinatario", "carpeta", "carpetas_entrada", "carpeta_fallidos", "minutos_atasco",
              "solo_horario", "toast", "estado_ultima", "estado_resultado"]

    def setUp(self):
        self._real_previo = escritorio_servicios.USAR_BD_REAL
        escritorio_servicios.USAR_BD_REAL = False
        self._ajustes_previos = {f"vigia.{k}": db.get_ajuste(f"vigia.{k}") for k in self.CLAVES}
        self.toast = patch.object(escritorio_servicios, "mostrar_toast").start()
        self._tmp = tempfile.TemporaryDirectory()
        self.carpeta = _Path(self._tmp.name) / "INTEGRACION"
        (self.carpeta / "Fallidos").mkdir(parents=True)
        (self.carpeta / "temp").mkdir()
        patch.object(escritorio_servicios, "ESTADO_VIGIA", _Path(self._tmp.name) / "estado.json").start()
        self.username = f"{TEST_PREFIX}vigia_{uuid.uuid4().hex[:8]}"
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO auth_user (password, is_superuser, username, first_name, last_name, email, "
                    "is_staff, is_active, date_joined) VALUES ('!sin-password-de-pruebas', 0, %s, '', '', '', 0, 1, NOW())",
                    (self.username,),
                )
                self.user_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        escritorio_servicios.guardar_config("vigia", {
            "activo": "1", "destinatario": self.username, "carpeta": str(self.carpeta), "carpetas_entrada": ".,temp",
            "carpeta_fallidos": "Fallidos", "minutos_atasco": "30", "solo_horario": "", "toast": "1",
        })

    def tearDown(self):
        patch.stopall()
        self._tmp.cleanup()
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM auth_user WHERE id=%s", (self.user_id,))
                for clave, valor in self._ajustes_previos.items():
                    if valor is None:
                        cur.execute("DELETE FROM ajustes_app WHERE clave=%s", (clave,))
                    else:
                        cur.execute("UPDATE ajustes_app SET valor=%s WHERE clave=%s", (valor, clave))
            conn.commit()
        finally:
            conn.close()
        escritorio_servicios.USAR_BD_REAL = self._real_previo

    def _notas(self):
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT contenido FROM buzon_notas WHERE para_user_id=%s ORDER BY id", (self.user_id,))
                notas = [r[0] for r in cur.fetchall()]
                cur.execute("SELECT tipo, texto, url FROM notificaciones WHERE user_id=%s ORDER BY id", (self.user_id,))
                return notas, cur.fetchall()
        finally:
            conn.close()

    def _archivo(self, ruta, minutos_atras=0):
        p = self.carpeta / ruta
        p.write_text("pedido", encoding="utf-8")
        if minutos_atras:
            ts = time.time() - minutos_atras * 60
            os.utime(p, (ts, ts))
        return p

    def test_evaluar_ciclo_completo(self):
        evaluar = escritorio_servicios.evaluar_vigia
        ahora = 1_000_000.0
        # primera vez: foto de lo que hay, sin alarma
        lineas, resumen, estado = evaluar({}, True, {"Fallidos\\a.xml": ahora}, {}, ahora, 30)
        self.assertIn("activado", lineas[0])
        self.assertEqual(resumen, ["vigilancia activada"])
        # nada nuevo: silencio
        self.assertEqual(evaluar(estado, True, {"Fallidos\\a.xml": ahora}, {}, ahora, 30)[0], [])
        # fallido nuevo + uno atascado (35 min) + uno reciente en la cola (no atascado)
        entrada = {"x.xml": ahora - 35 * 60, "y.xml": ahora - 60}
        fallidos = {"Fallidos\\a.xml": ahora, "Fallidos\\b.xml": ahora}
        lineas, resumen, estado = evaluar(estado, True, fallidos, entrada, ahora, 30)
        self.assertEqual(resumen, ["1 fallido(s) nuevo(s)", "1 atascado(s)"])
        self.assertTrue(any("b.xml" in l for l in lineas) and not any("y.xml" in l for l in lineas))
        # se pierde el acceso: un aviso, y el estado de archivos se conserva
        lineas, resumen, estado = evaluar(estado, False, {}, {}, ahora + 60, 30, "\\\\srv\\x")
        self.assertEqual(resumen, ["sin acceso a la carpeta"])
        self.assertEqual(evaluar(estado, False, {}, {}, ahora + 120, 30)[0], [])  # no se repite
        self.assertIn("Fallidos\\b.xml", estado["fallidos"])
        # vuelve el acceso, b sale de Fallidos y x de la cola
        lineas, resumen, estado = evaluar(estado, True, {"Fallidos\\a.xml": ahora}, {}, ahora + 180, 30)
        self.assertEqual(resumen, ["acceso recuperado", "1 fallido(s) resuelto(s)", "1 desatascado(s)"])

    def test_revisar_avisa_por_buzon_y_campana(self):
        self._archivo("Fallidos/viejo.xml")
        self.assertIn("vigilancia activada", escritorio_servicios.revisar_vigia())
        self._archivo("Fallidos/nuevo.xml")
        self._archivo("pedido_atascado.xml", minutos_atras=45)
        self._archivo("temp/recien_llegado.xml")
        resultado = escritorio_servicios.revisar_vigia()
        self.assertIn("1 fallido(s) nuevo(s)", resultado)
        self.assertIn("1 atascado(s)", resultado)
        notas, notificaciones = self._notas()
        self.assertEqual(len(notas), 2)
        self.assertIn("nuevo.xml", notas[1])
        self.assertIn("pedido_atascado.xml", notas[1])
        self.assertNotIn("viejo.xml", notas[1])
        self.assertNotIn("recien_llegado.xml", notas[1])
        self.assertEqual([n[0] for n in notificaciones], ["vigia", "vigia"])
        self.assertTrue(notificaciones[1][2].startswith("/buzon/#nota-"))
        self.assertEqual(self.toast.call_count, 2)
        # otra pasada sin cambios: nada nuevo
        escritorio_servicios.revisar_vigia()
        self.assertEqual(len(self._notas()[0]), 2)

    def test_sin_acceso_avisa_una_vez(self):
        escritorio_servicios.revisar_vigia()
        escritorio_servicios.guardar_config("vigia", {"carpeta": str(self.carpeta / "no_existe")})
        self.assertIn("sin acceso", escritorio_servicios.revisar_vigia())
        escritorio_servicios.revisar_vigia()
        notas, _ = self._notas()
        self.assertEqual(len(notas), 2)
        self.assertIn("No puedo acceder", notas[1])

    def test_sin_destinatario_o_pausado_no_hace_nada(self):
        escritorio_servicios.guardar_config("vigia", {"destinatario": ""})
        self.assertIn("falta el destinatario", escritorio_servicios.revisar_vigia())
        escritorio_servicios.set_activo("vigia", False)
        self.assertEqual(escritorio_servicios.revisar_vigia(), "pausado")
        self.assertEqual(self._notas()[0], [])
        self.toast.assert_not_called()


# ── Buzon (notas entre personas) + campana de notificaciones ───────────────
# Fase 1 de mensajeria (routers/buzon.py, routers/notificaciones.py,
# notificar.py). Dos usuarios de prueba propios (AUTOTEST_...) que se borran
# en tearDown: ON DELETE CASCADE se lleva sus notas y notificaciones.

from datetime import date as _date, timedelta as _timedelta  # noqa: E402

from . import notificar  # noqa: E402


class BuzonTests(EntornoAisladoTestCase):
    def setUp(self):
        sufijo = uuid.uuid4().hex[:6]
        self.u_a, self.u_b = f"{TEST_PREFIX}ana_{sufijo}", f"{TEST_PREFIX}beto_{sufijo}"
        for u in (self.u_a, self.u_b):
            auth._asegurar_auth_user(u)
        self.id_a, self.id_b = db.get_user_id(self.u_a), db.get_user_id(self.u_b)
        self.proyecto_id = f"{TEST_PREFIX}{sufijo}"
        db.crear_proyecto(self.proyecto_id, f"{TEST_PREFIX}buzon", "", "", "#2563eb", "Activo", "kanban", "", "")
        self.client = TestClient(app, follow_redirects=False)
        app.dependency_overrides[csrf.proteger] = lambda: None
        self._como(self.u_a)

    def _como(self, username):
        app.dependency_overrides[auth.requiere_login] = lambda: username

    def tearDown(self):
        for dep in (auth.requiere_login, csrf.proteger):
            app.dependency_overrides.pop(dep, None)
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM tareas WHERE proyecto_id=%s", (self.proyecto_id,))
                cur.execute("DELETE FROM columnas_tablero WHERE proyecto_id=%s", (self.proyecto_id,))
                cur.execute("DELETE FROM proyectos WHERE id=%s", (self.proyecto_id,))
                cur.execute("DELETE FROM auth_user WHERE id IN (%s,%s)", (self.id_a, self.id_b))
            conn.commit()
        finally:
            conn.close()

    def test_enviar_llega_sin_leer_y_avisa(self):
        r = self.client.post("/buzon/enviar/", data={"para_user_id": str(self.id_b), "contenido": "Revisa el albarán 123"})
        self.assertEqual(r.status_code, 303)
        recibidas = db.load_buzon(self.id_b, "recibidas")
        self.assertEqual([n["contenido"] for n in recibidas], ["Revisa el albarán 123"])
        self.assertFalse(recibidas[0]["leida"])
        self.assertEqual(db.contar_buzon_sin_leer(self.id_b), 1)
        self.assertEqual([n["contenido"] for n in db.load_buzon(self.id_a, "enviadas")], ["Revisa el albarán 123"])
        resumen = db.resumen_notificaciones(self.id_b)
        self.assertEqual(resumen["sin_leer"], 1)
        self.assertEqual(resumen["ultimas"][0]["tipo"], "nota")

    def test_nota_personal_nace_leida_y_no_avisa(self):
        self.client.post("/buzon/enviar/", data={"para_user_id": str(self.id_a), "contenido": "Para mí"})
        propias = db.load_buzon(self.id_a, "propias")
        self.assertTrue(propias[0]["personal"] and propias[0]["leida"])
        self.assertEqual(db.load_buzon(self.id_a, "recibidas"), [])
        self.assertEqual(db.resumen_notificaciones(self.id_a)["sin_leer"], 0)

    def test_solo_el_remitente_edita_y_elimina(self):
        nota_id = db.crear_buzon_nota(self.id_a, self.id_b, "original")
        self._como(self.u_b)
        self.assertEqual(self.client.post(f"/buzon/{nota_id}/editar/", data={"contenido": "x"}).status_code, 403)
        self.assertEqual(self.client.post(f"/buzon/{nota_id}/eliminar/").status_code, 403)
        self._como(self.u_a)
        self.client.post(f"/buzon/{nota_id}/editar/", data={"contenido": "editada"})
        self.assertEqual(db.get_buzon_nota(nota_id)["contenido"], "editada")

    def test_solo_el_destinatario_marca_leida(self):
        nota_id = db.crear_buzon_nota(self.id_a, self.id_b, "hola")
        self.client.post(f"/buzon/{nota_id}/leida/")  # como A (remitente): no hace nada
        self.assertFalse(db.get_buzon_nota(nota_id)["leida"])
        self._como(self.u_b)
        self.client.post(f"/buzon/{nota_id}/leida/")
        self.assertTrue(db.get_buzon_nota(nota_id)["leida"])

    def test_nota_ajena_no_se_ve(self):
        nota_id = db.crear_buzon_nota(self.id_a, self.id_a, "privada")
        self._como(self.u_b)
        self.assertEqual(self.client.post(f"/buzon/{nota_id}/reenviar/", data={"para_user_id": str(self.id_b), "contenido": "x"}).status_code, 404)

    def test_convertir_en_tarea_sin_permiso_se_asigna_a_uno_mismo(self):
        nota_id = db.crear_buzon_nota(self.id_b, self.id_a, "Llamar al proveedor")
        r = self.client.post(f"/buzon/{nota_id}/a-tarea/", data={
            "proyecto_id": self.proyecto_id, "estado": "Pendiente", "titulo": "Llamar al proveedor",
            "descripcion": "desde el buzón", "asignado_a": str(self.id_b),
        })
        self.assertEqual(r.status_code, 303)
        tareas = db.load_tareas_de_nota(nota_id)
        self.assertEqual(len(tareas), 1)
        _, _, _, asignado = db.tarea_estado_actual(tareas[0]["id"])
        self.assertEqual(asignado, self.id_a)  # ni IT ni Admin: aunque pidiera B, se la queda A
        self.assertEqual(db.resumen_notificaciones(self.id_b)["sin_leer"], 0)

    def test_reenviar_crea_nota_nueva_y_desvincula(self):
        nota_id = db.crear_buzon_nota(self.id_b, self.id_a, "original")
        task_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        db.crear_tarea(task_id, self.proyecto_id, "", "t", "Pendiente", "Normal", "", "", "", "")
        db.vincular_nota_tarea(nota_id, task_id)
        self.client.post(f"/buzon/{nota_id}/reenviar/", data={"para_user_id": str(self.id_b), "contenido": "mira esto", "desvincular": "1"})
        nueva = db.load_buzon(self.id_a, "enviadas")[0]
        self.assertEqual((nueva["contenido"], nueva["reenviada_de"]), ("mira esto", nota_id))
        self.assertEqual(db.get_buzon_nota(nota_id)["contenido"], "original")
        self.assertEqual(db.load_tareas_de_nota(nota_id), [])

    def test_recordatorio_salta_una_vez(self):
        manana = (_date.today() + _timedelta(days=1)).isoformat()
        db.crear_buzon_nota(self.id_a, self.id_b, "entregar informe", vence_en=manana, aviso_cantidad=2, aviso_unidad="dias")
        db.crear_buzon_nota(self.id_a, self.id_b, "aun no", vence_en=(_date.today() + _timedelta(days=30)).isoformat())
        self.assertEqual(db.generar_recordatorios_buzon(self.id_b), 1)
        self.assertEqual(db.generar_recordatorios_buzon(self.id_b), 0)
        ultimas = db.resumen_notificaciones(self.id_b)["ultimas"]
        self.assertTrue(any(n["tipo"] == "recordatorio" and "entregar informe" in n["texto"] for n in ultimas))

    def test_tarea_asignada_avisa_al_otro_no_a_uno_mismo(self):
        notificar.tarea_asignada(self.id_b, self.id_a, self.u_a, "x1", "Revisar", self.proyecto_id)
        notificar.tarea_asignada(self.id_a, self.id_a, self.u_a, "x2", "Mía", self.proyecto_id)
        self.assertEqual(db.resumen_notificaciones(self.id_b)["sin_leer"], 1)
        self.assertEqual(db.resumen_notificaciones(self.id_a)["sin_leer"], 0)

    def test_abrir_lista_marca_todo_leido(self):
        db.crear_notificacion(self.id_a, "nota", "algo")
        r = self.client.get("/notificaciones/resumen")
        self.assertEqual(r.json()["sin_leer"], 1)
        self.assertEqual(self.client.get("/notificaciones/").status_code, 200)
        self.assertEqual(self.client.get("/notificaciones/resumen").json()["sin_leer"], 0)

    def test_paginas_se_ven(self):
        db.crear_buzon_nota(self.id_b, self.id_a, "con <b>html</b> & cosas")
        for url in ("/buzon/", "/buzon/?vista=enviadas", "/notificaciones/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertIn("con &lt;b&gt;html&lt;/b&gt;", self.client.get("/buzon/").text)


# ── Muro de actividad por espacio (fase 2 de mensajeria) ────────────────────
# muro.py + routers/muro.py. Espacio compartido de prueba con un Admin (A),
# un Editor (B), un Lector (C) y alguien de fuera (D); todo se borra en
# tearDown (el espacio arrastra salas/mensajes/lecturas por ON DELETE CASCADE).

from . import muro as _muro  # noqa: E402


class MuroTests(EntornoAisladoTestCase):
    def setUp(self):
        sufijo = uuid.uuid4().hex[:6]
        self.nombres = {k: f"{TEST_PREFIX}{k}_{sufijo}" for k in "ABCD"}
        for n in self.nombres.values():
            auth._asegurar_auth_user(n)
        self.ids = {k: db.get_user_id(n) for k, n in self.nombres.items()}
        self.espacio_id = f"{TEST_PREFIX}{sufijo}"
        db.crear_espacio(self.espacio_id, f"{TEST_PREFIX}muro", "#0d9488", self.ids["A"])
        db.agregar_miembro_espacio(self.espacio_id, self.ids["B"], "editor")
        db.agregar_miembro_espacio(self.espacio_id, self.ids["C"], "lector")
        self.proyecto_id = f"{TEST_PREFIX}p{sufijo}"
        db.crear_proyecto(self.proyecto_id, f"{TEST_PREFIX}proy", "", "", "#2563eb", "Activo", "kanban", "", "",
                          espacio_id=self.espacio_id)
        self.legado_id = f"{TEST_PREFIX}l{sufijo}"
        db.crear_proyecto(self.legado_id, f"{TEST_PREFIX}legado", "", "", "#2563eb", "Activo", "kanban", "", "")
        self.columnas = [c["nombre"] for c in db.load_board_columns(self.proyecto_id)]
        self.hecha = next(c["nombre"] for c in db.load_board_columns(self.proyecto_id) if c["es_done"])
        self.sala_id = db.get_or_create_sala_muro(self.espacio_id)
        self.client = TestClient(app, follow_redirects=False)
        app.dependency_overrides[csrf.proteger] = lambda: None
        self._como("A")

    def _como(self, k):
        app.dependency_overrides[auth.requiere_login] = lambda: self.nombres[k]

    def tearDown(self):
        for dep in (auth.requiere_login, csrf.proteger):
            app.dependency_overrides.pop(dep, None)
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM tareas WHERE proyecto_id IN (%s,%s)", (self.proyecto_id, self.legado_id))
                cur.execute("DELETE FROM columnas_tablero WHERE proyecto_id IN (%s,%s)", (self.proyecto_id, self.legado_id))
                cur.execute("DELETE FROM proyectos WHERE id IN (%s,%s)", (self.proyecto_id, self.legado_id))
                cur.execute("DELETE FROM espacios WHERE id=%s", (self.espacio_id,))
                cur.execute(f"DELETE FROM auth_user WHERE id IN ({','.join(['%s'] * 4)})", list(self.ids.values()))
            conn.commit()
        finally:
            conn.close()

    def _textos(self, viewer="A"):
        return [m["contenido"] for m in db.load_chat_mensajes(self.sala_id, self.ids[viewer])]

    def _tarea(self, proyecto_id=None, estado=None, asignado=None):
        task_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        db.crear_tarea(task_id, proyecto_id or self.proyecto_id, "", "Revisar albaranes", estado or self.columnas[0],
                       "Normal", "", "", "", "", asignado_a=asignado)
        return task_id

    def test_acceso_por_rol(self):
        espacio = db.get_espacio(self.espacio_id)
        self.assertEqual(_muro.acceso(espacio, self.ids["A"], False), _muro.ESCRIBIR)
        self.assertEqual(_muro.acceso(espacio, self.ids["B"], False), _muro.ESCRIBIR)
        self.assertEqual(_muro.acceso(espacio, self.ids["C"], False), _muro.LEER)
        self.assertIsNone(_muro.acceso(espacio, self.ids["D"], False))
        self.assertEqual(_muro.acceso(espacio, self.ids["D"], True), _muro.ESCRIBIR)  # IT

    def test_tarea_creada_y_asignada_se_publica(self):
        _muro.tarea_creada(self._tarea(asignado=self.ids["B"]), self.ids["A"])
        texto = self._textos()[-1]
        self.assertIn("creó la tarea «Revisar albaranes»", texto)
        self.assertIn("se la asignó a", texto)

    def test_proyecto_sin_espacio_no_publica(self):
        _muro.tarea_creada(self._tarea(proyecto_id=self.legado_id), self.ids["A"])
        self.assertEqual(self._textos(), [])

    def test_empezar_completar_reabrir(self):
        task_id = self._tarea()
        db.reordenar_tarea(task_id, self.columnas[1], "")
        _muro.tarea_cambiada(task_id, self.ids["B"], self.columnas[0], None)
        db.reordenar_tarea(task_id, self.hecha, "")
        _muro.tarea_cambiada(task_id, self.ids["B"], self.columnas[1], None)
        db.reordenar_tarea(task_id, self.columnas[1], "")
        _muro.tarea_cambiada(task_id, self.ids["B"], self.hecha, None)
        textos = self._textos()
        self.assertEqual(len(textos), 3)
        self.assertIn("empezó a trabajar en", textos[0])
        self.assertIn("completó", textos[1])
        self.assertIn("reabrió", textos[2])

    def test_mover_entre_columnas_intermedias_no_publica(self):
        task_id = self._tarea(estado=self.columnas[1])
        db.reordenar_tarea(task_id, self.columnas[2], "")
        _muro.tarea_cambiada(task_id, self.ids["B"], self.columnas[1], None)
        self.assertEqual(self._textos(), [])

    def test_fuera_del_espacio_no_ve_el_muro(self):
        self._como("D")
        self.assertEqual(self.client.get(f"/espacio/{self.espacio_id}/muro/").status_code, 303)
        self.assertEqual(self.client.get(f"/espacio/{self.espacio_id}/muro/mensajes").status_code, 403)

    def test_lector_lee_pero_no_escribe(self):
        self._como("C")
        self.assertEqual(self.client.get(f"/muro/?espacio={self.espacio_id}").status_code, 200)
        r = self.client.post(f"/espacio/{self.espacio_id}/muro/escribir/", json={"contenido": "hola"})
        self.assertEqual(r.status_code, 403)

    def test_escribir_avisa_a_los_demas_una_vez(self):
        self._como("B")
        self.client.post(f"/espacio/{self.espacio_id}/muro/escribir/", json={"contenido": "Mañana cierro el mes"})
        self.client.post(f"/espacio/{self.espacio_id}/muro/escribir/", json={"contenido": "y reviso la integración"})
        for k, esperado in (("A", 1), ("C", 1), ("B", 0)):
            self.assertEqual(db.resumen_notificaciones(self.ids[k])["sin_leer"], esperado, k)
        self.assertEqual(db.contar_muros_sin_leer(self.ids["A"]).get(self.espacio_id), 2)
        # A abre el muro: queda leido y su aviso de la campana tambien
        self._como("A")
        self.client.get(f"/espacio/{self.espacio_id}/muro/mensajes")
        self.assertEqual(db.contar_muros_sin_leer(self.ids["A"]).get(self.espacio_id, 0), 0)
        self.assertEqual(db.resumen_notificaciones(self.ids["A"])["sin_leer"], 0)

    def test_silenciado_no_recibe_avisos(self):
        self._como("C")
        self.assertTrue(self.client.post(f"/espacio/{self.espacio_id}/muro/silenciar/").json()["silenciada"])
        self._como("B")
        self.client.post(f"/espacio/{self.espacio_id}/muro/escribir/", json={"contenido": "aviso"})
        self.assertEqual(db.resumen_notificaciones(self.ids["C"])["sin_leer"], 0)
        self.assertEqual(db.resumen_notificaciones(self.ids["A"])["sin_leer"], 1)

    def test_borrar_y_deshacer_solo_lo_propio(self):
        self._como("B")
        mid = self.client.post(f"/espacio/{self.espacio_id}/muro/escribir/", json={"contenido": "me equivoqué"}).json()["id"]
        self._como("A")
        self.assertEqual(self.client.post(f"/espacio/{self.espacio_id}/muro/mensaje/{mid}/borrar/").status_code, 403)
        self._como("B")
        self.assertTrue(self.client.post(f"/espacio/{self.espacio_id}/muro/mensaje/{mid}/borrar/").json()["ok"])
        self.assertNotIn("me equivoqué", self._textos("A"))          # los demas ya no lo ven
        propio = next(m for m in db.load_chat_mensajes(self.sala_id, self.ids["B"]) if m["id"] == mid)
        self.assertTrue(propio["borrado"] and propio["deshacer_quedan"] > 0)  # el autor ve "deshacer"
        self.assertTrue(self.client.post(f"/espacio/{self.espacio_id}/muro/mensaje/{mid}/deshacer/").json()["ok"])
        self.assertIn("me equivoqué", self._textos("A"))

    def test_mensajes_de_sistema_no_se_borran(self):
        _muro.publicar_en_espacio(self.espacio_id, self.ids["A"], "evento")
        mid = db.load_chat_mensajes(self.sala_id, self.ids["A"])[-1]["id"]
        self.assertEqual(self.client.post(f"/espacio/{self.espacio_id}/muro/mensaje/{mid}/borrar/").status_code, 403)

    def test_cambios_de_miembros_se_publican(self):
        r = self.client.post(f"/espacio/{self.espacio_id}/miembro/{self.ids['C']}/rol/", data={"rol": "editor"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("a Editor", self._textos()[-1])

    def test_pantalla_y_boton_en_el_espacio(self):
        r = self.client.get(f"/espacio/{self.espacio_id}/muro/")  # la ruta vieja lleva a la pagina central
        self.assertEqual((r.status_code, r.headers["location"]), (303, f"/muro/?espacio={self.espacio_id}"))
        self.assertEqual(self.client.get(f"/muro/?espacio={self.espacio_id}").status_code, 200)
        self.assertIn(f"/muro/?espacio={self.espacio_id}", self.client.get(f"/espacio/{self.espacio_id}/").text)


# ── Muro por proyecto: pestana "Muro" = el muro del espacio filtrado ────────
# Reutiliza el montaje de MuroTests (mismo espacio con A admin, B editor,
# C lector, D de fuera, un proyecto en el espacio y otro "legado" sin
# espacio) sin heredar sus tests.

class MuroProyectoTests(EntornoAisladoTestCase):
    setUp = MuroTests.setUp
    tearDown = MuroTests.tearDown
    _como = MuroTests._como
    _tarea = MuroTests._tarea

    def _otro_proyecto(self):
        pid = f"{TEST_PREFIX}o{uuid.uuid4().hex[:6]}"
        db.crear_proyecto(pid, f"{TEST_PREFIX}otro", "", "", "#16a34a", "Activo", "kanban", "", "", espacio_id=self.espacio_id)
        self._extra_proyectos = getattr(self, "_extra_proyectos", []) + [pid]
        self.addCleanup(self._borrar_proyecto, pid)
        return pid

    def _borrar_proyecto(self, pid):
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM tareas WHERE proyecto_id=%s", (pid,))
                cur.execute("DELETE FROM columnas_tablero WHERE proyecto_id=%s", (pid,))
                cur.execute("DELETE FROM proyectos WHERE id=%s", (pid,))
            conn.commit()
        finally:
            conn.close()

    def _textos_proyecto(self, pid):
        return [m["contenido"] for m in self.client.get(f"/proyecto/{pid}/muro/mensajes").json()["mensajes"]]

    def test_pestana_solo_muestra_lo_del_proyecto(self):
        otro = self._otro_proyecto()
        _muro.tarea_creada(self._tarea(), self.ids["A"])
        _muro.tarea_creada(self._tarea(proyecto_id=otro), self.ids["A"])
        textos = self._textos_proyecto(self.proyecto_id)
        self.assertEqual(len(textos), 1)
        mensajes_espacio = self.client.get(f"/espacio/{self.espacio_id}/muro/mensajes").json()["mensajes"]
        self.assertEqual({m["proyecto_id"] for m in mensajes_espacio}, {self.proyecto_id, otro})  # el espacio ve los dos
        self.assertTrue(all(m["proyecto"] for m in mensajes_espacio))  # con el nombre para la etiqueta

    def test_escribir_desde_el_proyecto_avisa_con_enlace_al_proyecto(self):
        self._como("B")
        self.client.post(f"/proyecto/{self.proyecto_id}/muro/escribir/", json={"contenido": "¿Quién mira el albarán?"})
        msg = db.load_chat_mensajes(self.sala_id, self.ids["A"])[-1]
        self.assertEqual(msg["proyecto_id"], self.proyecto_id)
        notif = db.resumen_notificaciones(self.ids["A"])["ultimas"][0]
        self.assertEqual(notif["url"], f"/proyecto/{self.proyecto_id}/muro/")
        self._como("A")
        self.client.get(f"/proyecto/{self.proyecto_id}/muro/mensajes")
        self.assertEqual(db.resumen_notificaciones(self.ids["A"])["sin_leer"], 0)

    def test_desde_el_espacio_se_puede_elegir_proyecto_pero_solo_del_espacio(self):
        self.client.post(f"/espacio/{self.espacio_id}/muro/escribir/", json={"contenido": "para el proyecto", "proyecto_id": self.proyecto_id})
        self.client.post(f"/espacio/{self.espacio_id}/muro/escribir/", json={"contenido": "colado", "proyecto_id": self.legado_id})
        mensajes = {m["contenido"]: m["proyecto_id"] for m in db.load_chat_mensajes(self.sala_id, self.ids["A"])}
        self.assertEqual(mensajes["para el proyecto"], self.proyecto_id)
        self.assertEqual(mensajes["colado"], "")  # proyecto de fuera del espacio: queda como del espacio en general
        self.assertEqual(self._textos_proyecto(self.proyecto_id), ["para el proyecto"])

    def test_aviso_difundido_sale_tambien_en_la_pestana(self):
        db.crear_aviso_difundido(self.sala_id, self.ids["A"], "Reinicio a las 15:00")
        self.assertEqual(self._textos_proyecto(self.proyecto_id), ["Reinicio a las 15:00"])

    def test_proyecto_sin_espacio_no_tiene_muro(self):
        self.assertEqual(self.client.get(f"/proyecto/{self.legado_id}/muro/").status_code, 303)
        self.assertEqual(self.client.get(f"/proyecto/{self.legado_id}/muro/mensajes").status_code, 404)
        self.assertNotIn(f"/proyecto/{self.legado_id}/muro/", self.client.get(f"/proyecto/{self.legado_id}/").text)

    def test_permisos_iguales_que_el_espacio(self):
        self._como("D")
        self.assertEqual(self.client.get(f"/proyecto/{self.proyecto_id}/muro/").status_code, 303)
        self.assertEqual(self.client.get(f"/proyecto/{self.proyecto_id}/muro/mensajes").status_code, 403)
        self._como("C")
        self.assertEqual(self.client.get(f"/proyecto/{self.proyecto_id}/muro/").status_code, 200)
        self.assertEqual(self.client.post(f"/proyecto/{self.proyecto_id}/muro/escribir/", json={"contenido": "x"}).status_code, 403)

    def test_pestana_en_las_paginas_del_proyecto(self):
        self.assertIn(f"/proyecto/{self.proyecto_id}/muro/", self.client.get(f"/proyecto/{self.proyecto_id}/").text)
        r = self.client.get(f"/proyecto/{self.proyecto_id}/muro/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("SalaChat.iniciar", r.text)


# ── Pagina central /muro/ y Aviso de IT (todo al Muro, sin chats) ──────────
# Mismo montaje que MuroTests (A admin, B editor, C lector, D de fuera).

from .routers import muro as _muro_router  # noqa: E402


class MuroCentralTests(EntornoAisladoTestCase):
    setUp = MuroTests.setUp
    tearDown = MuroTests.tearDown
    _como = MuroTests._como

    def _it(self, si=True):
        return patch.object(_muro_router, "_es_it", return_value=si)

    def test_cada_uno_ve_solo_sus_muros(self):
        ids_a = [m["id"] for m in _muro.muros_de(self.ids["A"], False)]
        self.assertIn(self.espacio_id, ids_a)
        self.assertNotIn(self.espacio_id, [m["id"] for m in _muro.muros_de(self.ids["D"], False)])
        otros = [m for m in _muro.muros_de(self.ids["D"], True) if m["id"] == self.espacio_id]  # IT: aparte
        self.assertEqual(len(otros), 1)
        self.assertFalse(otros[0]["propio"])

    def test_ajeno_no_entra_y_vuelve_a_la_lista(self):
        self._como("D")
        r = self.client.get(f"/muro/?espacio={self.espacio_id}")
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/muro/"))
        self.assertEqual(self.client.get("/muro/").status_code, 200)

    def test_vista_de_proyecto_y_contador_de_la_barra_lateral(self):
        self._como("B")
        self.client.post(f"/espacio/{self.espacio_id}/muro/escribir/", json={"contenido": "hola equipo"})
        self._como("A")
        self.assertEqual(self.client.get("/notificaciones/resumen").json()["muros_sin_leer"], 1)
        r = self.client.get(f"/muro/?espacio={self.espacio_id}&proyecto={self.proyecto_id}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"/proyecto/{self.proyecto_id}/muro/", r.text)  # la conversacion usa el muro del proyecto
        self.client.get(f"/espacio/{self.espacio_id}/muro/mensajes")
        self.assertEqual(self.client.get("/notificaciones/resumen").json()["muros_sin_leer"], 0)

    def test_aviso_solo_it_una_notificacion_por_persona(self):
        datos = {"texto": "Reinicio a las 15:00", "espacios": [self.espacio_id], "duracion": "1"}
        with self._it(False):
            self.assertEqual(self.client.post("/muro/aviso/", data=datos).status_code, 403)
        with self._it(True):
            self.assertEqual(self.client.post("/muro/aviso/", data=datos).status_code, 303)
        self.assertEqual([db.resumen_notificaciones(self.ids[k])["sin_leer"] for k in "ABCD"], [0, 1, 1, 0])
        aviso = db.load_chat_mensajes(self.sala_id, self.ids["B"])[-1]
        self.assertTrue(aviso["es_aviso"])
        self.assertEqual(self.client.post(f"/espacio/{self.espacio_id}/muro/mensaje/{aviso['id']}/borrar/").status_code, 403)
        with self._it(False):
            self.assertEqual(self.client.post(f"/muro/aviso/{aviso['id']}/borrar/").status_code, 403)
        with self._it(True):
            self.assertTrue(self.client.post(f"/muro/aviso/{aviso['id']}/borrar/").json()["ok"])
        self.assertFalse(any(m["es_aviso"] for m in db.load_chat_mensajes(self.sala_id, self.ids["B"])))

    def test_aviso_caducado_no_se_ve(self):
        db.crear_aviso_difundido(self.sala_id, self.ids["A"], "viejo", "2000-01-01 00:00:00")
        db.crear_aviso_difundido(self.sala_id, self.ids["A"], "vigente", None)
        self.assertEqual([m["contenido"] for m in db.load_chat_mensajes(self.sala_id, self.ids["A"])], ["vigente"])

    def test_ya_no_hay_chats(self):
        self.assertEqual(self.client.get("/chats/").status_code, 404)
        self.assertIn('href="/muro/"', self.client.get("/muro/").text)


# ── API externa sobre acciones.py: una accion minima arrastra sus efectos ──
# Mismo montaje que MuroTests (espacio con A admin, B editor, C lector, D
# de fuera, un proyecto en el espacio y otro "legado" sin espacio) + tokens
# de API de prueba (con dueño A, sin dueño, y restringido al proyecto).

import hashlib as _hashlib  # noqa: E402


class ApiAccionesTests(EntornoAisladoTestCase):
    setUp_muro = MuroTests.setUp
    tearDown_muro = MuroTests.tearDown
    _textos = MuroTests._textos
    _como = MuroTests._como

    def setUp(self):
        self.setUp_muro()
        app.dependency_overrides.pop(auth.requiere_login, None)  # la API no usa sesion
        self.claves = {}
        for nombre, dueno, proyecto in (("conA", self.ids["A"], None), ("anonimo", None, None),
                                        ("soloProy", self.ids["A"], self.proyecto_id)):
            plano = f"{TEST_PREFIX}{uuid.uuid4().hex}"
            db.crear_api_token(uuid.uuid4().hex[:8], f"{TEST_PREFIX}{nombre}",
                               _hashlib.sha256(plano.encode()).hexdigest(), proyecto, user_id=dueno)
            self.claves[nombre] = plano

    def tearDown(self):
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM api_tokens WHERE nombre LIKE %s", (f"{TEST_PREFIX}%",))
            conn.commit()
        finally:
            conn.close()
        self.tearDown_muro()

    def _api(self, metodo, ruta, token="conA", json_=None):
        return self.client.request(metodo, ruta, json=json_, headers={"X-API-Key": self.claves[token]})

    def _crear(self, token="conA", **campos):
        return self._api("POST", "/api/tareas/", token, {"proyecto_id": self.proyecto_id, "titulo": "Desde la API", **campos})

    def test_sin_clave_401(self):
        self.assertEqual(self.client.post("/api/tareas/", json={}).status_code, 401)

    def test_indice_dice_quien_soy(self):
        r = self._api("GET", "/api/").json()
        self.assertIn("(vía API:", r["actor"])
        self.assertTrue(any(e["ruta"] == "/api/muro/" for e in r["endpoints"]))
        self.assertTrue(self._api("GET", "/api/", "anonimo").json()["actor"].startswith("API · "))

    def test_crear_con_datos_minimos_publica_en_el_muro(self):
        r = self._crear()
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["efectos"], ["tarea_creada", "muro"])
        self.assertIn("(vía API:", self._textos()[-1])
        self.assertIn("creó la tarea «Desde la API»", self._textos()[-1])

    def test_crear_asignada_avisa_al_asignado(self):
        r = self._crear(asignado=self.nombres["B"]).json()
        self.assertIn(f"aviso:{self.nombres['B']}", r["efectos"])
        notif = db.resumen_notificaciones(self.ids["B"])["ultimas"][0]
        self.assertIn("vía API", notif["texto"])
        self.assertEqual(db.get_tarea_api(r["id"])["asignado_a"], self.ids["B"])

    def test_mover_a_hecha_publica_completo(self):
        tid = self._crear().json()["id"]
        r = self._api("POST", f"/api/tareas/{tid}/mover/", json_={"status": self.hecha}).json()
        self.assertEqual(r["efectos"], ["tarea_movida", "muro"])
        self.assertIn("completó", self._textos()[-1])
        self.assertEqual(self._api("POST", f"/api/tareas/{tid}/mover/", json_={"status": "No existe"}).status_code, 400)

    def test_editar_solo_lo_enviado_y_asignar(self):
        tid = self._crear(descripcion="original").json()["id"]
        self._api("POST", f"/api/tareas/{tid}/", json_={"prioridad": "Alta"})
        t = db.get_tarea_api(tid)
        self.assertEqual((t["prioridad"], t["descripcion"]), ("Alta", "original"))
        r = self._api("POST", f"/api/tareas/{tid}/asignar/", json_={"usuario": self.nombres["B"]}).json()
        self.assertIn(f"aviso:{self.nombres['B']}", r["efectos"])
        self.assertIn("asignó «Desde la API»", self._textos()[-1])
        self.assertEqual(self._api("POST", f"/api/tareas/{tid}/", json_={}).status_code, 400)

    def test_eliminar_y_restaurar(self):
        tid = self._crear().json()["id"]
        self.assertEqual(self._api("POST", f"/api/tareas/{tid}/eliminar/").json()["efectos"], ["tarea_eliminada", "muro"])
        self.assertTrue(db.get_tarea_api(tid)["borrada"])
        self._api("POST", f"/api/tareas/{tid}/restaurar/")
        self.assertFalse(db.get_tarea_api(tid)["borrada"])

    def test_importar_publica_un_solo_resumen(self):
        antes = len(self._textos())
        r = self._api("POST", "/api/tareas/importar/", json_={"proyecto_id": self.proyecto_id, "tareas": [
            {"titulo": "Uno"}, {"titulo": "Dos", "asignado": self.nombres["B"]}, {"titulo": "Uno"}]}).json()
        self.assertEqual(r["creadas"], 2)
        self.assertEqual(len(r["avisos"]), 1)  # el titulo repetido
        self.assertIn(f"aviso:{self.nombres['B']}", r["efectos"])
        textos = self._textos()
        self.assertEqual(len(textos), antes + 1)
        self.assertIn("importó 2 tareas", textos[-1])

    def test_muro_y_notas_firmados_por_el_dueno(self):
        r = self._api("POST", "/api/muro/", json_={"proyecto_id": self.proyecto_id, "texto": "Hola desde un script"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self._textos()[-1], "Hola desde un script")
        self.assertEqual(self._api("POST", "/api/muro/", "anonimo", {"proyecto_id": self.proyecto_id, "texto": "x"}).status_code, 403)
        r = self._api("POST", "/api/notas/", json_={"para": self.nombres["B"], "texto": "Revisa esto"}).json()
        self.assertIn(f"aviso:{self.nombres['B']}", r["efectos"])
        self.assertEqual(db.load_buzon(self.ids["B"], "recibidas")[0]["contenido"], "Revisa esto")

    def test_token_restringido(self):
        self.assertEqual(self._api("POST", "/api/tareas/", "soloProy", {"proyecto_id": self.legado_id, "titulo": "x"}).status_code, 403)
        tid = self._api("POST", "/api/tareas/", "conA", {"proyecto_id": self.legado_id, "titulo": "ajena"}).json()["id"]
        self.assertEqual(self._api("POST", f"/api/tareas/{tid}/mover/", "soloProy", {"status": self.hecha}).status_code, 404)
        self.assertEqual(self._api("POST", "/api/notas/", "soloProy", {"texto": "x"}).status_code, 403)

    def test_token_sin_dueno_sigue_creando(self):
        r = self._crear(token="anonimo").json()
        self.assertTrue(r["ok"])
        self.assertTrue(self._textos()[-1].startswith("API · "))

    def test_manual_servido_por_la_api(self):
        r = self._api("GET", "/api/manual")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Flujo de trabajo recomendado", r.text)
        self.assertEqual(self.client.get("/api/manual").status_code, 401)

    def test_subtareas_en_formato_comodo(self):
        tid = self._crear(subtareas=["Paso 1", {"texto": "Paso 2", "hecho": True}]).json()["id"]
        self.assertEqual(db.get_tarea_api(tid)["subtareas"], [{"texto": "Paso 1", "hecho": False}, {"texto": "Paso 2", "hecho": True}])

    def test_simular_no_crea_nada(self):
        antes = len(db.load_tasks_para_tablero(self.proyecto_id, borrada=False))
        r = self._api("POST", "/api/tareas/importar/", json_={"proyecto_id": self.proyecto_id, "simular": True, "tareas": [
            {"titulo": "Plan A", "status": "No existe", "subtareas": ["x", "y"]}, {"titulo": "Plan A"}]}).json()
        self.assertTrue(r["simulacion"])
        self.assertEqual([(t["titulo"], t["subtareas"]) for t in r["se_crearian"]], [("Plan A", 2)])
        self.assertEqual(len(r["avisos"]), 2)  # columna que no existe + titulo repetido dentro del lote
        self.assertEqual(len(db.load_tasks_para_tablero(self.proyecto_id, borrada=False)), antes)
        self.assertEqual(self._textos(), [])

    def test_permisos_del_dueno_se_respetan(self):
        # token cuyo dueño es Lector: puede crear (como en la web) pero no mover tareas ajenas
        plano = f"{TEST_PREFIX}{uuid.uuid4().hex}"
        db.crear_api_token(uuid.uuid4().hex[:8], f"{TEST_PREFIX}lector", _hashlib.sha256(plano.encode()).hexdigest(),
                           None, user_id=self.ids["C"])
        self.claves["lector"] = plano
        tid = self._crear().json()["id"]
        self.assertEqual(self._api("POST", f"/api/tareas/{tid}/mover/", "lector", {"status": self.hecha}).status_code, 403)


# ── nexus: relaciones tipo Union-Find (db.py::nexus_*) ─────────────────────
# Los nodos no tienen FK a la entidad real, asi que bastan entidad_id
# inventados con TEST_PREFIX.

class NexusRelacionesTests(EntornoAisladoTestCase):
    def setUp(self):
        self.sufijo = uuid.uuid4().hex[:6]

    def _e(self, nombre, tipo="sistema"):
        return (tipo, f"{TEST_PREFIX}{self.sufijo}_{nombre}")

    def tearDown(self):
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM nexus_nodos WHERE entidad_id LIKE %s",
                            (f"{TEST_PREFIX}{self.sufijo}_%",))
                ids = [r[0] for r in cur.fetchall()]
                for i in ids:
                    cur.execute("DELETE FROM nexus_historial WHERE FIND_IN_SET(%s, grupo_nodos) "
                                "OR representante_anterior=%s OR representante_nuevo=%s", (i, i, i))
                cur.execute("DELETE FROM nexus_nodos WHERE entidad_id LIKE %s", (f"{TEST_PREFIX}{self.sufijo}_%",))
            conn.commit()
        finally:
            conn.close()

    def _nodo(self, nombre, tipo="sistema"):
        return db.nexus_crear_nodo(*self._e(nombre, tipo))

    def test_crear_nodo_es_su_propio_representante_e_idempotente(self):
        n = self._nodo("a")
        self.assertEqual(db.nexus_representante(*self._e("a")), n)
        self.assertEqual(self._nodo("a"), n)
        self.assertEqual(db.nexus_relacionados(*self._e("a")), [])

    def test_tipo_desconocido_falla(self):
        with self.assertRaises(ValueError):
            db.nexus_crear_nodo("cosa", "x")

    def test_relacionar_une_grupos_por_tamano(self):
        a, b, c = self._nodo("a"), self._nodo("b"), self._nodo("c", "wiki_pagina")
        self.assertTrue(db.nexus_relacionar(self._e("a"), self._e("b")))
        self.assertFalse(db.nexus_relacionar(self._e("b"), self._e("a")))  # misma arista
        # {a,b} (2) + {c} (1): c pasa al representante del grupo grande
        db.nexus_relacionar(self._e("c", "wiki_pagina"), self._e("b"))
        rep = db.nexus_representante(*self._e("a"))
        self.assertEqual(rep, a)
        self.assertEqual(db.nexus_representante(*self._e("c", "wiki_pagina")), rep)
        rel = {r["nodo_id"]: r for r in db.nexus_relacionados(*self._e("c", "wiki_pagina"))}
        self.assertEqual(set(rel), {a, b})
        self.assertTrue(rel[b]["directo"])
        self.assertFalse(rel[a]["directo"])
        self.assertTrue(rel[a]["es_representante"])

    def test_relacionar_crea_nodos_que_faltan(self):
        db.nexus_relacionar(self._e("x"), self._e("y", "nota"))
        self.assertIsNotNone(db.nexus_representante(*self._e("y", "nota")))

    def test_eliminar_representante_promueve_el_mas_conectado_y_lo_registra(self):
        r, a, b, c = (self._nodo(n) for n in ("r", "a", "b", "c"))
        for x in ("a", "b", "c"):
            db.nexus_relacionar(self._e("r"), self._e(x))
        db.nexus_relacionar(self._e("b"), self._e("c"))  # b y c tienen grado 1 entre los que quedan
        db.nexus_relacionar(self._e("c"), self._e("a"))  # ...y c pasa a grado 2
        self.assertEqual(db.nexus_representante(*self._e("a")), r)
        nuevo = db.nexus_eliminar_nodo(*self._e("r"))
        self.assertEqual(nuevo, c)
        for x in ("a", "b", "c"):
            self.assertEqual(db.nexus_representante(*self._e(x)), c)
        self.assertIsNone(db.nexus_representante(*self._e("r")))
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT grupo_nodos, motivo FROM nexus_historial "
                            "WHERE representante_anterior=%s AND representante_nuevo=%s", (r, c))
                fila = cur.fetchone()
                cur.execute("SELECT COUNT(*) FROM nexus_relaciones WHERE nodo_a=%s OR nodo_b=%s", (r, r))
                aristas_r = cur.fetchone()[0]
        finally:
            conn.close()
        self.assertIsNotNone(fila)
        self.assertEqual(set(fila[0].split(",")), {a, b, c})
        self.assertIn("promovido", fila[1])
        self.assertEqual(aristas_r, 0)

    def test_eliminar_empate_promueve_el_mas_antiguo(self):
        r = self._nodo("r")
        a = self._nodo("a")
        self._nodo("b")
        db.nexus_relacionar(self._e("r"), self._e("a"))
        db.nexus_relacionar(self._e("r"), self._e("b"))
        conn = db.connect()
        try:
            with conn.cursor() as cur:  # creado_en tiene resolucion de segundos: forzar el orden
                cur.execute("UPDATE nexus_nodos SET creado_en = creado_en - INTERVAL 1 HOUR WHERE id=%s", (a,))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(db.nexus_eliminar_nodo(*self._e("r")), a)

    def test_eliminar_nodo_no_representante_no_cambia_el_grupo(self):
        r = self._nodo("r")
        db.nexus_relacionar(self._e("r"), self._e("a"))
        self.assertIsNone(db.nexus_eliminar_nodo(*self._e("a")))
        self.assertEqual(db.nexus_representante(*self._e("r")), r)
        self.assertIsNone(db.nexus_eliminar_nodo(*self._e("no_existe")))


class NexusGanchosTests(EntornoAisladoTestCase):
    """crear_*/eliminar_* dan y quitan el nodo; nexus_sincronizar rellena y
    limpia huerfanos. (sincronizar tambien crea los nodos que falten en el
    resto de la BD de test -- es el relleno que esa BD necesita igual.)"""

    def test_nota_crea_y_quita_su_nodo(self):
        nota_id = f"{TEST_PREFIX}{uuid.uuid4().hex[:6]}"
        db.crear_nota(nota_id, "Nota nexus", "", "#fef3c7")
        try:
            self.assertIsNotNone(db.nexus_representante("nota", nota_id))
        finally:
            db.eliminar_nota(nota_id)
        self.assertIsNone(db.nexus_representante("nota", nota_id))

    def test_wiki_crea_y_quita_su_nodo_y_promueve(self):
        from . import wiki_store
        p1 = wiki_store.create_page("general", f"{TEST_PREFIX}nexus 1")
        p2 = wiki_store.create_page("general", f"{TEST_PREFIX}nexus 2")
        nodos = [db.nexus_crear_nodo("wiki_pagina", p["page_id"]) for p in (p1, p2)]
        try:
            db.nexus_relacionar(("wiki_pagina", p1["page_id"]), ("wiki_pagina", p2["page_id"]))
            rep = db.nexus_representante("wiki_pagina", p1["page_id"])
            primero, otro = (p1, p2) if rep == db.nexus_crear_nodo("wiki_pagina", p1["page_id"]) else (p2, p1)
            wiki_store.delete_page(primero["page_id"])
            self.assertEqual(db.nexus_representante("wiki_pagina", otro["page_id"]),
                             db.nexus_crear_nodo("wiki_pagina", otro["page_id"]))
        finally:
            for p in (p1, p2):
                wiki_store.delete_page(p["page_id"])
            conn = db.connect()
            try:
                with conn.cursor() as cur:
                    for n in nodos:
                        cur.execute("DELETE FROM nexus_historial WHERE FIND_IN_SET(%s, grupo_nodos) "
                                    "OR representante_anterior=%s OR representante_nuevo=%s", (n, n, n))
                conn.commit()
            finally:
                conn.close()

    def test_sincronizar_quita_huerfanos(self):
        huerfana = f"{TEST_PREFIX}{uuid.uuid4().hex[:6]}"
        db.nexus_crear_nodo("nota", huerfana)
        resultado = db.nexus_sincronizar()
        self.assertGreaterEqual(resultado["nota"][1], 1)
        self.assertIsNone(db.nexus_representante("nota", huerfana))
        self.assertEqual(db.nexus_sincronizar()["nota"], (0, 0))


# ── Nexus sobre app_tareas (sin confluence_local desde 15/10/2026) ─────────
# Una ficha = pagina de Wiki + wiki_pagina_metadata; archivos en
# nexus_archivos/nexus_carpetas enlazados por nexus_referencias.

from .nexus import search as _nexus_search  # noqa: E402


class NexusBusquedaTests(EntornoAisladoTestCase):
    def setUp(self):
        from . import wiki_store
        conn = db.connect()
        try:
            with conn.cursor() as cur:  # lo que estos tests anaden a historial/log se borra por id en tearDown
                cur.execute("SELECT (SELECT COALESCE(MAX(id), 0) FROM nexus_historial), "
                            "(SELECT COALESCE(MAX(id), 0) FROM nexus_busquedas_log)")
                self.max_historial, self.max_log = cur.fetchone()
        finally:
            conn.close()
        self.sufijo = uuid.uuid4().hex[:6]
        self.titulo = f"{TEST_PREFIX}Ficha {self.sufijo}"
        self.pagina = wiki_store.create_page("general", self.titulo, content={"time": 0, "version": "2.29.0", "blocks": [
            {"type": "paragraph", "data": {"text": "Estado: activo · Rol: tecnico"}},
            {"type": "paragraph", "data": {"text": f"Integracion zorblax{self.sufijo} con el ERP"}},
            {"type": "header", "data": {"text": "Info para desarrolladores", "level": 3}},
            {"type": "paragraph", "data": {"text": "Codigo en <b>repo</b> interno"}}]})["page_id"]
        self.sistema = f"{TEST_PREFIX}{self.sufijo}"
        db.crear_sistema(self.sistema, f"{TEST_PREFIX}Sistema {self.sufijo}", "", "#123456")
        db.nexus_relacionar(("wiki_pagina", self.pagina), ("sistema", self.sistema))
        self.carpeta_id = 900000 + int(self.sufijo, 16) % 90000
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO wiki_pagina_metadata (pagina_id, alias, codigo, tecnologias, estado, rol) "
                            "VALUES (%s, 'Zorb', 'ZB-1', 'Python, MariaDB', 'activo', 'tecnico')", (self.pagina,))
                cur.execute("INSERT INTO nexus_carpetas (id, ruta_completa, ruta_relativa, nombre) VALUES (%s,%s,%s,%s)",
                            (self.carpeta_id, f"C:/x/{self.sufijo}", f"x/{self.sufijo}", self.sufijo))
                cur.execute("INSERT INTO nexus_archivos (id, carpeta_id, ruta_completa, ruta_relativa, nombre, extension) "
                            "VALUES (%s,%s,%s,%s,%s,'.txt')",
                            (self.carpeta_id, self.carpeta_id, f"C:/x/{self.sufijo}/zorblax{self.sufijo}.txt",
                             f"x/{self.sufijo}/zorblax{self.sufijo}.txt", f"zorblax{self.sufijo}.txt"))
                cur.execute("INSERT INTO nexus_referencias (id, wiki_pagina_id, archivo_id) VALUES (%s,%s,%s)",
                            (self.carpeta_id, self.pagina, self.carpeta_id))
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        from . import wiki_store
        for c in _nexus_search.listar_credenciales(self.pagina):
            conn = db.connect()
            try:
                with conn.cursor() as cur:
                    db._nexus_eliminar_nodo(cur, "credencial", c["id"])
                    cur.execute("DELETE FROM credenciales WHERE id=%s", (c["id"],))
                conn.commit()
            finally:
                conn.close()
        wiki_store.delete_page(self.pagina)  # arrastra metadata y referencias (CASCADE)
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM nexus_archivos WHERE id=%s", (self.carpeta_id,))
                cur.execute("DELETE FROM nexus_carpetas WHERE id=%s", (self.carpeta_id,))
                cur.execute("DELETE FROM nexus_nodos WHERE tipo='sistema' AND entidad_id=%s", (self.sistema,))
                cur.execute("DELETE FROM sistemas WHERE id=%s", (self.sistema,))
                cur.execute("DELETE FROM nexus_busquedas_log WHERE id > %s", (self.max_log,))
                cur.execute("DELETE FROM nexus_historial WHERE id > %s", (self.max_historial,))
            conn.commit()
        finally:
            conn.close()

    def test_ficha_por_alias_con_textos_ubicaciones_y_relacionados(self):
        r = _nexus_search.search_catalogo("Zorb")
        ficha = next(p for p in r["proyectos"] if p["id"] == self.pagina)
        self.assertEqual(ficha["nombre"], self.titulo)
        self.assertEqual(ficha["codigo"], "ZB-1")
        self.assertIn(f"zorblax{self.sufijo}", ficha["resumen"])
        self.assertNotIn("Estado:", ficha["resumen"])
        self.assertEqual(ficha["info_desarrollador"], "Codigo en repo interno")
        self.assertEqual(ficha["wiki_url"], f"/proyecto/general/wiki/{self.pagina}/")
        self.assertEqual([u["tipo"] for u in ficha["ubicaciones"]], ["archivo"])
        self.assertIn(f"{TEST_PREFIX}Sistema {self.sufijo}", [x["nombre"] for x in ficha["relacionados"]])

    def test_ficha_por_texto_y_archivo_con_su_ficha(self):
        r = _nexus_search.search_catalogo(f"zorblax{self.sufijo}")
        self.assertIn(self.pagina, [p["id"] for p in r["proyectos"]])
        archivo = next(a for a in r["archivos"] if a["id"] == self.carpeta_id)
        self.assertEqual(archivo["proyecto"], self.titulo)

    def test_tecnologias_y_estadisticas(self):
        techs = {t["tecnologia"]: t for t in _nexus_search.listar_tecnologias()["tecnologias"]}
        self.assertIn(self.pagina, [p["id"] for p in techs["MariaDB"]["proyectos"]])
        self.assertGreaterEqual(_nexus_search.get_stats()["proyectos"], 1)

    def test_credencial_cifrada_colgada_de_la_ficha(self):
        cred = _nexus_search.crear_credencial(self.pagina, "Admin", "root", None, "clave-buena", "s3creto")
        self.assertEqual([c["id"] for c in _nexus_search.listar_credenciales(self.pagina)], [cred])
        self.assertEqual(_nexus_search.search_catalogo("Zorb")["proyectos"][0]["n_credenciales"], 1)
        self.assertFalse(_nexus_search.ver_credencial(cred, "mala")["ok"])
        self.assertEqual(_nexus_search.ver_credencial(cred, "clave-buena")["valor"], "s3creto")
        self.assertFalse(_nexus_search.eliminar_credencial(cred, "mala"))
        self.assertTrue(_nexus_search.eliminar_credencial(cred, "clave-buena"))
        self.assertEqual(_nexus_search.listar_credenciales(self.pagina), [])
        self.assertIsNone(db.nexus_representante("credencial", cred))

    def test_rutas_con_ids_de_texto(self):
        app.dependency_overrides[nexus._requiere_acceso_nexus] = lambda: "usuario.demo"
        app.dependency_overrides[auth.requiere_it] = lambda: "usuario.demo"
        app.dependency_overrides[csrf.proteger] = lambda: None
        try:
            client = TestClient(app)
            datos = client.get("/nexus/search", params={"q": f"zorblax{self.sufijo}"}).json()
            self.assertIn(self.pagina, [p["id"] for p in datos["proyectos"]])
            self.assertNotIn(self.pagina, [w["page_id"] for w in datos["wiki_propia"]])  # no se repite
            r = client.post(f"/nexus/proyecto/{self.pagina}/credenciales",
                            json={"etiqueta": "x", "password": "p", "secreto": "s"})
            cred = r.json()["id"]
            self.assertEqual(client.post(f"/nexus/credencial/{cred}/ver", json={"password": "p"}).json()["valor"], "s")
            self.assertTrue(client.post(f"/nexus/credencial/{cred}/borrar", json={"password": "p"}).json()["ok"])
        finally:
            for dep in (nexus._requiere_acceso_nexus, auth.requiere_it, csrf.proteger):
                app.dependency_overrides.pop(dep, None)


# ── conectores de entrada (modulo opcional, app_fastapi/conectores/) ────────

class ConectoresPurosTests(unittest.TestCase):
    """Sin BD: rutas JSON, prioridad, tipo REST con la red mockeada, cifrado."""

    def test_valor_en_ruta(self):
        from .conectores.base import valor_en_ruta
        dato = {"data": {"items": [{"t": "a"}, {"t": "b"}]}}
        self.assertEqual(valor_en_ruta(dato, "data.items.1.t"), "b")
        self.assertIs(valor_en_ruta(dato, ""), dato)
        self.assertIsNone(valor_en_ruta(dato, "data.nada"))

    def test_prioridad(self):
        from .conectores import motor
        self.assertEqual(motor._prioridad("urgent"), "Alta")
        self.assertEqual(motor._prioridad("Baja"), "Baja")
        self.assertEqual(motor._prioridad("normal"), "Normal")
        self.assertEqual(motor._prioridad(""), "Normal")

    @patch("app_fastapi.conectores.rest_generico.requests.get")
    def test_rest_generico_mapea_campos(self, mock_get):
        from .conectores import TIPOS
        mock_get.return_value = Mock(status_code=200, ok=True, json=lambda: {"res": [
            {"id": 7, "f": {"titulo": "Impresora rota"}, "prio": "high", "vence": "2026-11-02T10:00:00"},
            {"id": 8, "f": {}},  # sin titulo: se ignora
        ]})
        elementos = TIPOS["rest"].traer({"url": "https://x.test/api", "cabecera": "X-API-Key", "token": "k",
                                         "ruta_lista": "res", "campo_id": "id", "campo_titulo": "f.titulo",
                                         "campo_prioridad": "prio", "campo_fecha": "vence",
                                         "plantilla_url": "https://x.test/t/{id}"})
        self.assertEqual(len(elementos), 1)
        e = elementos[0]
        self.assertEqual((e.id_externo, e.titulo, e.fecha_fin, e.url), ("7", "Impresora rota", "2026-11-02", "https://x.test/t/7"))
        self.assertEqual(mock_get.call_args.kwargs["headers"]["X-API-Key"], "k")

    @patch("app_fastapi.conectores.rest_generico.requests.get")
    def test_rest_generico_errores_legibles(self, mock_get):
        from .conectores import TIPOS, ConectorError
        cfg = {"url": "https://x.test", "campo_id": "id", "campo_titulo": "t"}
        mock_get.return_value = Mock(status_code=401, ok=False)
        with self.assertRaisesRegex(ConectorError, "autenticación"):
            TIPOS["rest"].traer(cfg)
        mock_get.return_value = Mock(status_code=200, ok=True, json=lambda: {"no": "lista"})
        with self.assertRaisesRegex(ConectorError, "no hay una lista"):
            TIPOS["rest"].traer(cfg)

    @patch.dict("os.environ", {"CONECTORES_CLAVE": "clave-de-test"})
    def test_secretos_se_guardan_cifrados_y_se_conservan(self):
        from .conectores import motor
        guardado = motor.preparar_config("rest", {"url": "https://x.test", "token": "Bearer secreto"})
        self.assertNotIn("secreto", guardado)
        self.assertEqual(motor.config_en_claro({"config": guardado})["token"], "Bearer secreto")
        # editar sin volver a escribir el token = se conserva el anterior
        import json as _json
        otra = motor.preparar_config("rest", {"url": "https://y.test", "token": ""}, _json.loads(guardado))
        self.assertEqual(motor.config_en_claro({"config": otra})["token"], "Bearer secreto")


@patch.dict("os.environ", {"CONECTORES_CLAVE": "clave-de-test"})
class ConectoresSincronizarTests(EntornoAisladoTestCase):
    def setUp(self):
        from .conectores import motor
        self.motor = motor
        self.conector_id = f"{TEST_PREFIX[:6]}{uuid.uuid4().hex[:8]}"
        db.guardar_conector(self.conector_id, f"{TEST_PREFIX}conector", "rest",
                            motor.preparar_config("rest", {"url": "https://x.test", "campo_id": "id", "campo_titulo": "t"}),
                            "general", "", None, 0)

    def tearDown(self):
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM tareas WHERE id IN (SELECT tarea_id FROM conector_elementos WHERE conector_id=%s)",
                            (self.conector_id,))
                cur.execute("DELETE FROM conectores WHERE id=%s", (self.conector_id,))
            conn.commit()
        finally:
            conn.close()

    def _origen(self, *items):
        return patch("app_fastapi.conectores.rest_generico.requests.get",
                     return_value=Mock(status_code=200, ok=True, json=lambda: list(items)))

    def test_crea_tareas_una_sola_vez(self):
        with self._origen({"id": 1, "t": f"{TEST_PREFIX}uno"}, {"id": 2, "t": f"{TEST_PREFIX}dos"}):
            r = self.motor.sincronizar(self.conector_id)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["nuevas"], 2)
        with self._origen({"id": 1, "t": f"{TEST_PREFIX}uno"}, {"id": 3, "t": f"{TEST_PREFIX}tres"}):
            r = self.motor.sincronizar(self.conector_id)
        self.assertEqual((r["nuevas"], r["ya_importadas"]), (1, 1))
        self.assertEqual(db.conector_ids_importados(self.conector_id), {"1", "2", "3"})
        self.assertTrue(db.get_conector(self.conector_id)["ultimo_ok"])

    def test_fallo_del_origen_queda_guardado_sin_lanzar(self):
        with patch("app_fastapi.conectores.rest_generico.requests.get", return_value=Mock(status_code=500, ok=False)):
            r = self.motor.sincronizar(self.conector_id)
        self.assertFalse(r["ok"])
        c = db.get_conector(self.conector_id)
        self.assertFalse(c["ultimo_ok"])
        self.assertIn("500", c["ultimo_resultado"])

    def test_panel_y_guardado_desde_configuracion(self):
        for dep in (auth.requiere_it, auth.requiere_login):
            app.dependency_overrides[dep] = lambda: "test-user"
        app.dependency_overrides[csrf.proteger] = lambda: None
        try:
            client = TestClient(app, follow_redirects=False)
            self.assertIn(f"{TEST_PREFIX}conector", client.get("/configuracion/?tipo=conectores").text)
            self.assertIn("API REST genérica", client.get("/configuracion/?tipo=conectores&nuevo=rest").text)
            r = client.post("/configuracion/conectores/guardar/", data={
                "tipo": "rest", "conector_id": self.conector_id, "nombre": f"{TEST_PREFIX}renombrado",
                "cfg_url": "https://z.test", "cfg_campo_id": "id", "cfg_campo_titulo": "t",
                "proyecto_id": "general", "intervalo_min": "15"})
            self.assertEqual(r.status_code, 303)
            c = db.get_conector(self.conector_id)
            self.assertEqual((c["nombre"], c["intervalo_min"]), (f"{TEST_PREFIX}renombrado", 15))
        finally:
            for dep in (auth.requiere_it, auth.requiere_login, csrf.proteger):
                app.dependency_overrides.pop(dep, None)


class NexusArchivoSinRolTests(EntornoAisladoTestCase):
    """Un archivo indexado sin rol (lo normal con scripts/nexus_indexar.py)
    tiene que salir en la busqueda normal: rol NULL hacia que el filtro de
    'tecnico sin curar' diera NULL y lo descartara."""

    def setUp(self):
        self.id = 900000 + int(uuid.uuid4().hex[:5], 16) % 90000
        self.nombre = f"qzxw{uuid.uuid4().hex[:6]}.md"
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO nexus_carpetas (id, ruta_completa, ruta_relativa, nombre) VALUES (%s,%s,%s,%s)",
                            (self.id, f"/x/{self.id}", f"x/{self.id}", str(self.id)))
                cur.execute("INSERT INTO nexus_archivos (id, carpeta_id, ruta_completa, ruta_relativa, nombre, extension) "
                            "VALUES (%s,%s,%s,%s,%s,'.md')",
                            (self.id, self.id, f"/x/{self.id}/{self.nombre}", f"x/{self.id}/{self.nombre}", self.nombre))
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM nexus_carpetas WHERE id=%s", (self.id,))  # CASCADE al archivo
            conn.commit()
        finally:
            conn.close()

    def test_sale_en_la_busqueda_normal(self):
        from .nexus import search
        self.assertIn(self.nombre, [a["nombre"] for a in search.search_catalogo(self.nombre[:10])["archivos"]])


class TableroSoloLecturaTests(EntornoAisladoTestCase):
    """Un lector del espacio ve el tablero sin botones que luego no hacen nada."""

    def setUp(self):
        self.espacio = f"{TEST_PREFIX}{uuid.uuid4().hex[:6]}"
        self.proyecto = f"{TEST_PREFIX.lower()}{uuid.uuid4().hex[:6]}"
        self.usuario = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO auth_user (password, is_superuser, username, first_name, last_name, email, "
                            "is_staff, is_active, date_joined) VALUES ('!', 0, %s, '', '', '', 0, 1, NOW())", (self.usuario,))
                cur.execute("INSERT INTO espacios (id, nombre) VALUES (%s, %s)", (self.espacio, self.espacio))
                cur.execute("INSERT INTO espacio_usuarios (espacio_id, user_id, rol) SELECT %s, id, 'lector' FROM auth_user WHERE username=%s",
                            (self.espacio, self.usuario))
            conn.commit()
        finally:
            conn.close()
        db.crear_proyecto(self.proyecto, "Prueba lectura", "", "", "#2563eb", "Activo", "kanban", "", "", espacio_id=self.espacio)
        app.dependency_overrides[auth.requiere_login] = lambda: self.usuario
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        app.dependency_overrides.pop(auth.requiere_login, None)
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM columnas_tablero WHERE proyecto_id=%s", (self.proyecto,))
                cur.execute("DELETE FROM proyectos WHERE id=%s", (self.proyecto,))
                cur.execute("DELETE FROM espacio_usuarios WHERE espacio_id=%s", (self.espacio,))
                cur.execute("DELETE FROM espacios WHERE id=%s", (self.espacio,))
                cur.execute("DELETE FROM auth_user WHERE username=%s", (self.usuario,))
            conn.commit()
        finally:
            conn.close()

    def test_lector_ve_aviso_y_no_botones(self):
        self.assertFalse(db.puede_escribir_en_proyecto(self.proyecto, db.get_user_id(self.usuario)))
        html = self.client.get(f"/proyecto/{self.proyecto}/tablero/").text
        self.assertIn("Solo lectura: puedes ver este tablero", html)
        self.assertIn("var SOLO_LECTURA = true;", html)
        db.cambiar_rol_miembro_espacio(self.espacio, db.get_user_id(self.usuario), "editor")
        html = self.client.get(f"/proyecto/{self.proyecto}/tablero/").text
        self.assertNotIn("Solo lectura: puedes ver este tablero", html)
        self.assertIn("var SOLO_LECTURA = false;", html)


class PortadaYGruposTests(unittest.TestCase):
    """La portada la sirve el servidor: Tareas solo manda alli al entrar y salir."""

    def test_grupos(self):
        with patch.dict("os.environ", {"KEYCLOAK_GRUPO_APP": "Tareas", "KEYCLOAK_GRUPO_ADMIN_GLOBAL": "IT"}):
            self.assertTrue(auth.es_admin_global(["/IT"]))
            self.assertFalse(auth.es_admin_global(["/Tareas/Admin"]))
            self.assertEqual(auth.acceso_por_grupos(["/Tareas/Admin"]), (True, True))

    def test_entrar_y_salir_vuelven_a_la_portada(self):
        client = TestClient(app, follow_redirects=False)
        with patch.dict("os.environ", {"APP_URL_PORTAL": ""}):
            r = client.get("/portada/", headers={"Host": "dev.ejemplo.com", "X-Forwarded-Proto": "https"})
        self.assertEqual(r.headers["location"], "https://dev.ejemplo.com/")
        with patch.dict("os.environ", {"APP_URL_PORTAL": "https://apps.ejemplo.com/"}):
            self.assertEqual(client.get("/portada/").headers["location"], "https://apps.ejemplo.com/")
        with patch.dict("os.environ", {"AUTH_MODO": "local"}):
            self.assertEqual(client.get("/logout/?a=portada").headers["location"], "/portada/")
            self.assertEqual(client.get("/logout/").headers["location"], "/login/")
        with patch.dict("os.environ", dict(_KEYCLOAK_ENV, AUTH_MODO="sso", APP_URL_PUBLICA="https://dev.ejemplo.com/tareas")):
            r = client.get("/logout/?a=portada", headers={"Host": "dev.ejemplo.com"})
        self.assertIn("post_logout_redirect_uri=https%3A%2F%2Fdev.ejemplo.com%2Ftareas%2Fportada%2F", r.headers["location"])

    def test_sin_el_grupo_de_tareas_no_entra_aunque_venga_de_la_portada(self):
        from urllib.parse import parse_qs, urlparse
        client = TestClient(app, follow_redirects=False)
        with patch.dict("os.environ", dict(_KEYCLOAK_ENV, AUTH_MODO="sso", KEYCLOAK_GRUPO_APP="PRO/Tareas")):
            r = client.get("/login/?next=/portada/")
            state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
            with patch("app_fastapi.auth.sso_completar", side_effect=auth.SinAccesoApp("ana", ["/PRO/Lin-AI"])):
                r = client.get(f"/login/sso/?code=c&state={state}")
            self.assertEqual(r.status_code, 200)   # el aviso de sin acceso
            self.assertIn(client.get("/").status_code, (302, 303, 307))

    def test_ajustes_ya_no_es_de_tareas(self):
        client = TestClient(app, follow_redirects=False)
        self.assertEqual(client.get("/ajustes/").status_code, 404)
        self.assertEqual(client.get("/ajustes/portada-entorno/").status_code, 404)

    def test_grupos_del_login_se_guardan(self):
        with patch.dict("os.environ", _KEYCLOAK_ENV), patch("app_fastapi.auth.requests.get") as get:
            get.return_value = Mock(status_code=200, json=lambda: {"preferred_username": "u", "groups": ["/PRO/Tareas", "/PRO/Lin-AI/Editor"]})
            auth._usuario_de_keycloak("http://k", "t")
            sesion = {}
            with patch("app_fastapi.db.marcar_it"):
                auth.iniciar_sesion(Mock(session=sesion), "u")
        self.assertEqual(sesion["grupos"], ["PRO/Lin-AI/Editor", "PRO/Tareas"])


# ── ruta_base.py: servir la app bajo /tareas ────────────────────────────────

class RutaBaseTests(unittest.TestCase):
    """El middleware se prueba aislado (un app minimo)."""

    def setUp(self):
        from fastapi import FastAPI, Request
        from fastapi.responses import HTMLResponse, JSONResponse as _JSON, RedirectResponse as _Redir
        from . import ruta_base

        mini = FastAPI()

        @mini.get("/pagina/")
        def pagina():
            return HTMLResponse('<a href="/">x</a><a href="/notas/">n</a>'
                                "<script>f('/proyecto/' + id + '/tablero/'); a.split('/')</script>"
                                """<script>s.replace(/'/g, "&#39;").replace(/"/g, '')</script>"""
                                '<a href="/tareas/ya/">y</a><a href="https://otra.test/z">e</a>')

        @mini.get("/ir/")
        def ir():
            return _Redir(url="/notas/", status_code=303)

        @mini.get("/datos/")
        def datos():
            return _JSON({"url": "/buzon/", "texto": "/no/tocar", "lista": [{"enlace": "/muro/"}]})

        @mini.get("/ruta/")
        def ruta(request: Request):
            return _JSON({"path": request.url.path})

        self.client = TestClient(ruta_base.RutaBaseMiddleware(mini, prefijo="/tareas"))

    def test_html_prefija_rutas_propias_y_deja_el_resto(self):
        html = self.client.get("/tareas/pagina/").text
        self.assertIn('href="/tareas/"', html)
        self.assertIn('href="/tareas/notas/"', html)
        self.assertIn("f('/tareas/proyecto/' + id + '/tablero/')", html)
        self.assertIn("a.split('/')", html)
        self.assertIn("""s.replace(/'/g, "&#39;").replace(/"/g, '')""", html)
        self.assertIn('href="/tareas/ya/"', html)
        self.assertIn('href="https://otra.test/z"', html)

    def test_redireccion_y_json(self):
        r = self.client.get("/tareas/ir/", follow_redirects=False)
        self.assertEqual(r.headers["location"], "/tareas/notas/")
        self.assertEqual(self.client.get("/tareas/datos/").json(),
                         {"url": "/tareas/buzon/", "texto": "/no/tocar", "lista": [{"enlace": "/tareas/muro/"}]})

    def test_la_app_ve_la_ruta_sin_prefijo(self):
        self.assertEqual(self.client.get("/tareas/ruta/").json(), {"path": "/ruta/"})
        r = self.client.get("/tareas", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (308, "/tareas/"))

    def test_ruta_por_cabecera_del_proxy(self):
        from . import ruta_base
        client = TestClient(ruta_base.RutaBaseMiddleware(self.client.app.app, prefijo=""))
        r = client.get("/tareas/ir/", headers={"X-Forwarded-Prefix": "/tareas"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/tareas/notas/")
        # sin cabecera (su propio dominio): tal cual, en la raiz
        self.assertEqual(client.get("/ir/", follow_redirects=False).headers["location"], "/notas/")
        # cabecera rara: se ignora
        r = client.get("/ir/", headers={"X-Forwarded-Prefix": "/a b<x"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/notas/")


# ── AUTH_MODO=sso: inicio de sesion en la pantalla de Keycloak ──────────────

_SSO_ENV = dict(_KEYCLOAK_ENV, AUTH_MODO="sso", APP_URL_PUBLICA="https://apps.test/tareas")


@patch.dict("os.environ", _SSO_ENV)
class SsoTests(EntornoAisladoTestCase):

    def test_login_manda_a_keycloak_con_state(self):
        client = TestClient(app)
        r = client.get("/login/?next=/notas/", follow_redirects=False)
        self.assertEqual(r.status_code, 307)
        self.assertTrue(r.headers["location"].startswith("http://keycloak.test/realms/demo/protocol/openid-connect/auth?"))
        self.assertIn("redirect_uri=https%3A%2F%2Fapps.test%2Ftareas%2Flogin%2Fsso%2F", r.headers["location"])

    def test_con_dominio_propio_vuelve_por_donde_entro(self):
        # tras "tareas.sh dominio": la principal es la nueva y la de antes sigue valiendo
        with patch.dict("os.environ", {"APP_URL_PUBLICA": "https://tareas.test",
                                       "APP_URLS_ALTERNATIVAS": "https://apps.test/tareas"}):
            r = TestClient(app, base_url="https://apps.test").get("/login/", follow_redirects=False)
            self.assertIn("redirect_uri=https%3A%2F%2Fapps.test%2Ftareas%2Flogin%2Fsso%2F", r.headers["location"])
            r = TestClient(app, base_url="https://tareas.test").get("/login/", follow_redirects=False)
            self.assertIn("redirect_uri=https%3A%2F%2Ftareas.test%2Flogin%2Fsso%2F", r.headers["location"])
            r = TestClient(app, base_url="https://otro.test").get("/login/", follow_redirects=False)
            self.assertIn("redirect_uri=https%3A%2F%2Ftareas.test%2Flogin%2Fsso%2F", r.headers["location"])

    def test_state_distinto_no_inicia_sesion(self):
        client = TestClient(app)
        client.get("/login/", follow_redirects=False)
        with patch("app_fastapi.auth.requests.post") as mock_post:
            r = client.get("/login/sso/?code=abc&state=otro", follow_redirects=False)
        mock_post.assert_not_called()
        self.assertIn("No se pudo completar", r.text)

    @patch("app_fastapi.auth.requests.get")
    @patch("app_fastapi.auth.requests.post")
    def test_vuelta_correcta_inicia_sesion(self, mock_post, mock_get):
        from urllib.parse import parse_qs, urlparse
        usuario = TEST_PREFIX + "sso"
        mock_post.return_value = Mock(status_code=200, json=lambda: {"access_token": "tok", "id_token": "idtok"})
        mock_get.return_value = Mock(status_code=200, json=lambda: {"preferred_username": usuario, "groups": ["IT"]})
        client = TestClient(app)
        try:
            ida = client.get("/login/?next=/notas/", follow_redirects=False).headers["location"]
            state = parse_qs(urlparse(ida).query)["state"][0]
            r = client.get(f"/login/sso/?code=abc&state={state}", follow_redirects=False)
            self.assertEqual((r.status_code, r.headers["location"]), (303, "/notas/"))
            self.assertEqual(mock_post.call_args.kwargs["data"]["grant_type"], "authorization_code")
            self.assertIsNotNone(db.get_user_id(usuario))
            salida = client.get("/logout/", follow_redirects=False).headers["location"]
            self.assertTrue(salida.startswith("http://keycloak.test/realms/demo/protocol/openid-connect/logout?"))
            # con id_token_hint Keycloak cierra sin pantalla de confirmar
            self.assertEqual(parse_qs(urlparse(salida).query)["id_token_hint"], ["idtok"])
        finally:
            conn = db.connect()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM auth_user WHERE username=%s", (usuario,))
            conn.commit()
            conn.close()

    @patch("app_fastapi.auth.requests.get")
    @patch("app_fastapi.auth.requests.post")
    def test_sin_el_grupo_de_la_app_no_entra(self, mock_post, mock_get):
        from urllib.parse import parse_qs, urlparse
        usuario = TEST_PREFIX + "sso_sin"
        mock_post.return_value = Mock(status_code=200, json=lambda: {"access_token": "tok"})
        mock_get.return_value = Mock(status_code=200, json=lambda: {"preferred_username": usuario, "groups": ["/Stock"]})
        client = TestClient(app)
        with patch.dict("os.environ", {"KEYCLOAK_GRUPO_APP": "Tareas"}):
            ida = client.get("/login/", follow_redirects=False).headers["location"]
            state = parse_qs(urlparse(ida).query)["state"][0]
            r = client.get(f"/login/sso/?code=abc&state={state}", follow_redirects=False)
        self.assertIn("no tiene acceso a esta aplicación", r.text)
        self.assertIsNone(db.get_user_id(usuario))
        self.assertIn(client.get("/", follow_redirects=False).status_code, (302, 303, 307))


class AccesoPorGruposTests(unittest.TestCase):

    def test_sin_grupo_de_app_entra_cualquiera(self):
        with patch.dict("os.environ", {"KEYCLOAK_GRUPO_APP": "", "KEYCLOAK_GRUPO_ADMIN_GLOBAL": "IT"}):
            self.assertEqual(auth.acceso_por_grupos([]), (True, False))
            self.assertEqual(auth.acceso_por_grupos(["IT"]), (True, True))

    def test_grupo_de_app_subgrupos_y_admin(self):
        with patch.dict("os.environ", {"KEYCLOAK_GRUPO_APP": "/Tareas", "KEYCLOAK_GRUPO_ADMIN_GLOBAL": "IT"}):
            self.assertEqual(auth.acceso_por_grupos(["/Stock", "/Stock/Admin"]), (False, False))
            self.assertEqual(auth.acceso_por_grupos(["/Tareas"]), (True, False))
            self.assertEqual(auth.acceso_por_grupos(["/Tareas/Compras"]), (True, False))
            self.assertEqual(auth.acceso_por_grupos(["/Tareas/Admin"]), (True, True))
            self.assertEqual(auth.acceso_por_grupos(["/IT"]), (True, True))
            self.assertEqual(auth.acceso_por_grupos(["/TareasOtra"]), (False, False))
