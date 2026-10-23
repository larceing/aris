# -*- coding: utf-8 -*-
"""Conexion a la misma base de datos que la app Django real -- PyMySQL
directo (mismo patron que core/db_store.py, pero sin arrastrar Django).
Lee las mismas variables DB_* del .env y el mismo interruptor real/test que
ya usa la app principal (data/entorno_db.txt), asi este proyecto ve siempre
la misma configuracion y los mismos datos que la app de verdad -- nunca una
copia separada."""
import contextvars
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pymysql
import pymysql.cursors

from . import logging_setup

_ENTORNO_FILE = Path(__file__).parent.parent / "data" / "entorno_db.txt"


class _CursorConLog(pymysql.cursors.Cursor):
    """Mismo cursor de PyMySQL de siempre, solo que registra cada sentencia
    en logs/queries.log antes de ejecutarla -- unico punto de cambio para
    loguear TODAS las queries de este archivo, sin tocar cada funcion."""

    def execute(self, query, args=None):
        try:
            # stacklevel=2: que el log apunte a la funcion de db.py que pidio
            # la query (ej. "crear_nota"), no a esta funcion execute() en si.
            logging_setup.queries.info(f"{query} | args={args!r}", stacklevel=2)
        except Exception:
            pass
        return super().execute(query, args)


def _entorno_actual() -> str:
    if _ENTORNO_FILE.exists():
        valor = _ENTORNO_FILE.read_text().strip()
        if valor in ("real", "test"):
            return valor
    return "real"


def entorno_actual() -> str:
    """Igual que _entorno_actual() pero publica -- para el desplegable de
    usuario, que necesita mostrar/cambiar el entorno, no solo conectarse a el."""
    return _entorno_actual()


def set_entorno(valor: str) -> None:
    """Mismo archivo que usa la app real (data/entorno_db.txt) -- cambiarlo
    aqui cambia tambien el entorno de la app Django, es un interruptor
    compartido a proposito, no uno independiente por app."""
    if valor not in ("real", "test"):
        raise ValueError(f"entorno invalido: {valor!r}")
    _ENTORNO_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ENTORNO_FILE.write_text(valor)


def connect(real: bool = False):
    """real=True ignora el toggle real/test y va SIEMPRE a la BD real -- solo
    para los servicios de escritorio en segundo plano (escritorio_servicios.py:
    Recordatorio y Vigilante de Mantis), que avisan de cosas de verdad da
    igual en que entorno este trabajando la web en ese momento."""
    entorno = "real" if real else _entorno_actual()
    dbname = os.getenv("DB_NAME") if entorno == "real" else os.getenv("DB_NAME_TEST")
    return pymysql.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=dbname,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=_CursorConLog,
    )


def _nn(s) -> str:
    return s or ""


def _fmt_datetime(dt) -> str:
    if dt is None:
        return ""
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M")
    return str(dt)


def _fmt_date(d) -> str:
    if d is None:
        return ""
    return d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ── mapeo Mantis <-> Empresa (usado por routers/mantis.py) ─────────────────

def get_mantis_config():
    """(pool_project_id, empresa_project_ids) -- leidos en vivo de
    ajustes_app/empresas, igual que hace la app real (core/views.py::_mantis_config)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT valor FROM ajustes_app WHERE clave=%s", ("mantis.pool_project_id",))
            row = cur.fetchone()
            pool_id = int(row[0]) if row and row[0] else 1

            cur.execute("SELECT mantis_project_id FROM empresas WHERE mantis_project_id IS NOT NULL")
            empresa_project_ids = [r[0] for r in cur.fetchall()]
        return pool_id, empresa_project_ids
    finally:
        conn.close()


# ── notas (usado por routers/notas.py) ──────────────────────────────────────
# Mismo patron que core/db_store.py::load_notas/save_notas: sin FK entrante,
# CRUD real por fila (mas simple que el vaciar-y-reinsertar del original,
# ver conversacion -- el resultado final es identico porque orden ya se
# calcula igual, max(existente)+1, y no hay reordenacion masiva en ningun sitio).

def load_notas() -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, titulo, contenido, color, orden, reunion_id, agenda_fecha, creada_en, actualizada_en "
                "FROM notas ORDER BY orden, creada_en DESC"
            )
            cols = ["nota_id", "titulo", "contenido", "color", "orden", "reunion_id", "agenda_fecha",
                    "creada_en", "actualizada_en"]
            return [
                dict(zip(cols, [r[0], r[1], _nn(r[2]), r[3], r[4], _nn(r[5]), _fmt_date(r[6]),
                                 _fmt_datetime(r[7]), _fmt_datetime(r[8])]))
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def crear_nota(nota_id: str, titulo: str, contenido: str, color: str, reunion_id: str = "",
               agenda_fecha: str = "") -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(orden), 0) FROM notas")
            siguiente_orden = cur.fetchone()[0] + 1
            now = now_text()
            cur.execute(
                "INSERT INTO notas (id, titulo, contenido, color, orden, reunion_id, agenda_fecha, "
                "creada_en, actualizada_en) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (nota_id, titulo, contenido, color, siguiente_orden, reunion_id or None,
                 agenda_fecha or None, now, now),
            )
            _nexus_crear_nodo(cur, "nota", nota_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def editar_nota(nota_id: str, titulo: str, contenido: str, color: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE notas SET titulo=%s, contenido=%s, color=%s, actualizada_en=%s WHERE id=%s",
                (titulo, contenido, color, now_text(), nota_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def eliminar_nota(nota_id: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            _nexus_eliminar_nodo(cur, "nota", nota_id)
            cur.execute("DELETE FROM notas WHERE id=%s", (nota_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── empresas (usado por routers/configuracion.py) ───────────────────────────
# Mismo patron que core/db_store.py::load_empresas/save_empresas, pero por
# fila (igual simplificacion que en notas): el original vacia-y-reinserta la
# tabla entera en cada guardado, aqui un UPDATE/INSERT/borrado puntual llega
# al mismo estado final sin ese vaivan.

def load_empresas() -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, nombre, nombre_fiscal, color, orden, activo, mantis_project_id, dominio_email "
                "FROM empresas ORDER BY orden"
            )
            cols = ["empresa_id", "nombre", "nombre_fiscal", "color", "orden", "activo",
                     "mantis_project_id", "dominio_email"]
            return [
                dict(zip(cols, [r[0], r[1], _nn(r[2]), r[3], r[4], bool(r[5]),
                                 str(r[6]) if r[6] is not None else "", _nn(r[7])]))
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def crear_empresa(empresa_id: str, nombre: str, nombre_fiscal: str, color: str, mantis_project_id: str,
                   dominio_email: str = "") -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(orden), 0) FROM empresas")
            siguiente_orden = cur.fetchone()[0] + 1
            cur.execute(
                "INSERT INTO empresas (id, nombre, nombre_fiscal, color, orden, activo, mantis_project_id, dominio_email) "
                "VALUES (%s,%s,%s,%s,%s,TRUE,%s,%s)",
                (empresa_id, nombre, nombre_fiscal, color, siguiente_orden,
                 int(mantis_project_id) if mantis_project_id else None, dominio_email.strip().lower()),
            )
            _nexus_crear_nodo(cur, "empresa", empresa_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def editar_empresa(empresa_id: str, nombre: str, nombre_fiscal: str, color: str, mantis_project_id: str,
                    dominio_email: str = None) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            if dominio_email is None:
                cur.execute("SELECT dominio_email FROM empresas WHERE id=%s", (empresa_id,))
                row = cur.fetchone()
                dominio_email = row[0] if row else ""
            cur.execute(
                "UPDATE empresas SET nombre=%s, nombre_fiscal=%s, color=%s, mantis_project_id=%s, dominio_email=%s WHERE id=%s",
                (nombre, nombre_fiscal, color, int(mantis_project_id) if mantis_project_id else None,
                 dominio_email.strip().lower(), empresa_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_conteos_pendientes() -> dict:
    """Numero de filas real en las tablas de las pestanas de Configuracion
    que aqui todavia son solo un marcador visual -- para que el nav muestre
    el mismo contador que la app real (ej. `{{ categorias|length }}`) en vez
    de dejarlo en blanco, sin tener que copiar el CRUD de cada una todavia.
    'categorias' cuenta solo las globales (proyecto_id IS NULL), igual que
    core/views.py::global_categories; el resto cuenta todas las filas (la
    app real las lista todas, activas e inactivas, en su tabla)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            conteos = {}
            cur.execute("SELECT COUNT(*) FROM categorias WHERE proyecto_id IS NULL")
            conteos["categorias"] = cur.fetchone()[0]
            for clave, tabla in [
                ("sistemas", "sistemas"), ("iniciativas", "iniciativas"),
                ("dias_especiales", "dias_especiales"), ("tipos_dia_especial", "tipos_dia_especial"),
                ("textos", "plantilla_config"),
            ]:
                cur.execute(f"SELECT COUNT(*) FROM {tabla}")
                conteos[clave] = cur.fetchone()[0]
        return conteos
    finally:
        conn.close()


def count_backups() -> int:
    backups_dir = Path(__file__).parent.parent / "data" / "backups_bd"
    return len(list(backups_dir.glob("*.sql"))) if backups_dir.exists() else 0


# ── contenido real de las pestanas todavia sin CRUD propio ──────────────────
# Solo lectura -- se listan los valores tal cual estan en la BD para que la
# pestana "pendiente" no se quede vacia, aunque el formulario para editarlos
# todavia no exista aqui (eso llega cuando se copie esa pestana de verdad).

def load_categorias_globales() -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, nombre, color, activo FROM categorias "
                "WHERE proyecto_id IS NULL ORDER BY orden"
            )
            return [{"id": r[0], "nombre": r[1], "color": r[2], "activo": bool(r[3])} for r in cur.fetchall()]
    finally:
        conn.close()


def load_sistemas() -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.id, s.nombre, s.color, s.activo, s.empresa_id, e.nombre "
                "FROM sistemas s LEFT JOIN empresas e ON e.id = s.empresa_id ORDER BY s.orden"
            )
            return [{"id": r[0], "nombre": r[1], "color": r[2], "activo": bool(r[3]),
                      "empresa_id": _nn(r[4]), "empresa": _nn(r[5])}
                    for r in cur.fetchall()]
    finally:
        conn.close()


def load_iniciativas() -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, nombre, responsable, color, activo FROM iniciativas ORDER BY nombre")
            return [{"id": r[0], "nombre": r[1], "responsable": _nn(r[2]), "color": r[3], "activo": bool(r[4])}
                    for r in cur.fetchall()]
    finally:
        conn.close()


def load_tipos_dia_especial() -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, nombre, categoria, color, activo, urgente, cuenta_para_sla "
                "FROM tipos_dia_especial ORDER BY categoria, orden"
            )
            return [{"id": r[0], "nombre": r[1], "categoria": r[2], "color": r[3], "activo": bool(r[4]),
                      "urgente": bool(r[5]), "cuenta_para_sla": bool(r[6])} for r in cur.fetchall()]
    finally:
        conn.close()


def load_dias_especiales() -> list:
    """Resuelve tipo_id/empresa_id a un nombre igual que
    core/views.py::resolve_dia_especial (una Sede ligada a Empresa muestra
    nombre/color de la Empresa, no texto duplicado)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT d.id, d.tipo_id, d.empresa_id, d.fecha_inicio, d.fecha_fin, d.nota, d.color, "
                "t.nombre, e.nombre "
                "FROM dias_especiales d "
                "LEFT JOIN tipos_dia_especial t ON t.id = d.tipo_id "
                "LEFT JOIN empresas e ON e.id = d.empresa_id "
                "ORDER BY d.fecha_inicio"
            )
            resultado = []
            for r in cur.fetchall():
                nombre = ("Sede " + r[8]) if r[8] else (r[7] or "?")
                resultado.append({
                    "dia_id": r[0], "tipo_id": _nn(r[1]), "empresa_id": _nn(r[2]),
                    "nombre": nombre, "fecha_inicio": _fmt_date(r[3]), "fecha_fin": _fmt_date(r[4]),
                    "nota": _nn(r[5]), "color": r[6],
                })
            return resultado
    finally:
        conn.close()


def load_textos() -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT clave, valor, categoria FROM plantilla_config ORDER BY categoria, clave")
            return [{"clave": r[0], "valor": r[1], "categoria": r[2]} for r in cur.fetchall()]
    finally:
        conn.close()


def load_mantis_ajustes() -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT clave, valor FROM ajustes_app WHERE clave LIKE 'mantis.%%' ORDER BY clave")
            return [{"clave": r[0], "valor": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()


def load_backups() -> list:
    backups_dir = Path(__file__).parent.parent / "data" / "backups_bd"
    if not backups_dir.exists():
        return []
    archivos = sorted(backups_dir.glob("*.sql"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {"nombre": f.name, "size_kb": round(f.stat().st_size / 1024, 1),
         "fecha": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
        for f in archivos
    ]


def toggle_empresa(empresa_id: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE empresas SET activo = NOT activo WHERE id=%s", (empresa_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── categorias globales (proyecto_id IS NULL) ───────────────────────────────
# Mismo patron que core/views.py::categoria_global_new/edit/toggle.

def crear_categoria(categoria_id: str, nombre: str, color: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(orden), 0) FROM categorias WHERE proyecto_id IS NULL")
            siguiente_orden = cur.fetchone()[0] + 1
            cur.execute(
                "INSERT INTO categorias (id, proyecto_id, nombre, color, orden, activo) "
                "VALUES (%s,NULL,%s,%s,%s,TRUE)",
                (categoria_id, nombre, color, siguiente_orden),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def editar_categoria(categoria_id: str, nombre: str, color: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE categorias SET nombre=%s, color=%s WHERE id=%s", (nombre, color, categoria_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def toggle_categoria(categoria_id: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE categorias SET activo = NOT activo WHERE id=%s", (categoria_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── sistemas ─────────────────────────────────────────────────────────────────

def crear_sistema(sistema_id: str, nombre: str, empresa_id: str, color: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(orden), 0) FROM sistemas")
            siguiente_orden = cur.fetchone()[0] + 1
            cur.execute(
                "INSERT INTO sistemas (id, nombre, empresa_id, color, orden, activo) "
                "VALUES (%s,%s,%s,%s,%s,TRUE)",
                (sistema_id, nombre, empresa_id or None, color, siguiente_orden),
            )
            _nexus_crear_nodo(cur, "sistema", sistema_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def editar_sistema(sistema_id: str, nombre: str, empresa_id: str, color: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sistemas SET nombre=%s, empresa_id=%s, color=%s WHERE id=%s",
                (nombre, empresa_id or None, color, sistema_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def toggle_sistema(sistema_id: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE sistemas SET activo = NOT activo WHERE id=%s", (sistema_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── iniciativas ──────────────────────────────────────────────────────────────

def crear_iniciativa(iniciativa_id: str, nombre: str, responsable: str, color: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO iniciativas (id, nombre, responsable, color, activo) VALUES (%s,%s,%s,%s,TRUE)",
                (iniciativa_id, nombre, responsable, color),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def editar_iniciativa(iniciativa_id: str, nombre: str, responsable: str, color: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE iniciativas SET nombre=%s, responsable=%s, color=%s WHERE id=%s",
                (nombre, responsable, color, iniciativa_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def toggle_iniciativa(iniciativa_id: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE iniciativas SET activo = NOT activo WHERE id=%s", (iniciativa_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── tipos de dia especial ────────────────────────────────────────────────────

CATEGORIAS_TIPO_DIA_ESPECIAL = ["sede", "anotacion", "banner"]


def crear_tipo_dia_especial(tipo_id: str, nombre: str, categoria: str, color: str, urgente: bool, cuenta_para_sla: bool) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(orden), 0) FROM tipos_dia_especial WHERE categoria=%s", (categoria,))
            siguiente_orden = cur.fetchone()[0] + 1
            cur.execute(
                "INSERT INTO tipos_dia_especial (id, nombre, categoria, color, orden, activo, urgente, cuenta_para_sla) "
                "VALUES (%s,%s,%s,%s,%s,TRUE,%s,%s)",
                (tipo_id, nombre, categoria, color, siguiente_orden, urgente, cuenta_para_sla),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def editar_tipo_dia_especial(tipo_id: str, nombre: str, categoria: str, color: str, urgente: bool, cuenta_para_sla: bool) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tipos_dia_especial SET nombre=%s, categoria=%s, color=%s, urgente=%s, cuenta_para_sla=%s WHERE id=%s",
                (nombre, categoria, color, urgente, cuenta_para_sla, tipo_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def toggle_tipo_dia_especial(tipo_id: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE tipos_dia_especial SET activo = NOT activo WHERE id=%s", (tipo_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_tipos_dia_especial_por_categoria() -> dict:
    """{'sede': [...], 'anotacion': [...], 'banner': [...]} de tipos activos --
    para poblar el <select> del modal de Dia especial, igual que
    tipos_sede/tipos_anotacion/tipos_banner en core/views.py::configuracion."""
    tipos = [t for t in load_tipos_dia_especial() if t["activo"]]
    return {cat: [t for t in tipos if t["categoria"] == cat] for cat in CATEGORIAS_TIPO_DIA_ESPECIAL}


# ── dias especiales ──────────────────────────────────────────────────────────

def _parse_dia_especial_tipo(valor: str):
    """Mismo formato que core/views.py::_parse_dia_especial_tipo: el <select>
    codifica el origen como 'empresa:<id>' (Sede ligada a una Empresa real) o
    'tipo:<id>' (resto, incluidas las Sede manuales tipo Teletrabajo)."""
    if valor.startswith("empresa:"):
        return "", valor[len("empresa:"):]
    if valor.startswith("tipo:"):
        return valor[len("tipo:"):], ""
    return "", ""


def _color_dia_especial_default(tipo_id: str, empresa_id: str) -> str:
    conn = connect()
    try:
        with conn.cursor() as cur:
            if empresa_id:
                cur.execute("SELECT color FROM empresas WHERE id=%s", (empresa_id,))
            elif tipo_id:
                cur.execute("SELECT color FROM tipos_dia_especial WHERE id=%s", (tipo_id,))
            else:
                return "#6b7280"
            row = cur.fetchone()
            return row[0] if row else "#6b7280"
    finally:
        conn.close()


def crear_dia_especial(dia_id: str, tipo_valor: str, fecha_inicio: str, fecha_fin: str, nota: str, color: str) -> None:
    tipo_id, empresa_id = _parse_dia_especial_tipo(tipo_valor)
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO dias_especiales (id, tipo_id, empresa_id, fecha_inicio, fecha_fin, nota, color) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (dia_id, tipo_id or None, empresa_id or None, fecha_inicio,
                 fecha_fin or fecha_inicio, nota, color or _color_dia_especial_default(tipo_id, empresa_id)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def editar_dia_especial(dia_id: str, tipo_valor: str, fecha_inicio: str, fecha_fin: str, nota: str, color: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT tipo_id, empresa_id, fecha_inicio, fecha_fin, nota, color FROM dias_especiales WHERE id=%s", (dia_id,))
            actual = cur.fetchone()
            if not actual:
                return
            if tipo_valor:
                tipo_id, empresa_id = _parse_dia_especial_tipo(tipo_valor)
            else:
                tipo_id, empresa_id = _nn(actual[0]), _nn(actual[1])
            nueva_fecha_inicio = fecha_inicio.strip() or _fmt_date(actual[2])
            nueva_fecha_fin = fecha_fin.strip() or nueva_fecha_inicio
            cur.execute(
                "UPDATE dias_especiales SET tipo_id=%s, empresa_id=%s, fecha_inicio=%s, fecha_fin=%s, nota=%s, color=%s WHERE id=%s",
                (tipo_id or None, empresa_id or None, nueva_fecha_inicio, nueva_fecha_fin,
                 nota if nota is not None else _nn(actual[4]), color or actual[5], dia_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def eliminar_dia_especial(dia_id: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM dias_especiales WHERE id=%s", (dia_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── textos de interfaz (plantilla_config) ───────────────────────────────────

def editar_texto(clave: str, valor: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE plantilla_config SET valor=%s WHERE clave=%s", (valor, clave))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── ajustes_app (usado por el panel Mantis) ─────────────────────────────────

def get_ajuste(clave: str, default=None, real: bool = False):
    conn = connect(real)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT valor FROM ajustes_app WHERE clave=%s", (clave,))
            row = cur.fetchone()
            return row[0] if row else default
    finally:
        conn.close()


def set_ajuste(clave: str, valor: str, real: bool = False) -> None:
    conn = connect(real)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ajustes_app (clave, valor) VALUES (%s,%s) ON DUPLICATE KEY UPDATE valor=VALUES(valor)",
                (clave, valor),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_proyectos_basico() -> list:
    """(id, nombre, tipo, estado) de todos los proyectos -- para el <select>
    de "proyecto destino" del panel Mantis (que solo usa id/nombre) y para
    GET /api/dimensiones/ (que ademas necesita tipo/estado)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cond, params = _ver_por_espacio("espacio_id")
            cur.execute(f"SELECT id, nombre, tipo, estado FROM proyectos WHERE {cond} ORDER BY orden", params or None)
            return [{"id": r[0], "nombre": r[1], "tipo": r[2], "estado": r[3]} for r in cur.fetchall()]
    finally:
        conn.close()


# ── backups (mismo patron que core/views.py::hacer_backup_ahora, sin Django) ─

BACKUPS_MAX = 15


def _find_mysqldump() -> str | None:
    import shutil
    desde_env = os.getenv("MYSQLDUMP_PATH")
    if desde_env and Path(desde_env).exists():
        return desde_env
    en_path = shutil.which("mysqldump") or shutil.which("mariadb-dump")
    if en_path:
        return en_path
    for base in (Path("C:/Program Files"), Path("C:/Program Files (x86)")):
        if not base.exists():
            continue
        for patron in ("MariaDB*/bin/mysqldump.exe", "MariaDB*/bin/mariadb-dump.exe", "MySQL*/bin/mysqldump.exe"):
            encontrados = sorted(base.glob(patron))
            if encontrados:
                return str(encontrados[-1])
    return None


def crear_backup() -> tuple[bool, str]:
    """Vuelca la BD real (nunca la de pruebas, da igual el entorno activo de
    esta v2) a un .sql en data/backups_bd/ -- misma carpeta y mismo formato
    de nombre que usa la app Django real, asi ambas comparten el listado."""
    import subprocess

    mysqldump = _find_mysqldump()
    if not mysqldump:
        return False, "No se encontró mysqldump. Define MYSQLDUMP_PATH en app_fastapi/.env con la ruta al ejecutable."

    dbname = os.getenv("DB_NAME")
    backups_dir = Path(__file__).parent.parent / "data" / "backups_bd"
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destino = backups_dir / f"{dbname}_{timestamp}.sql"

    comando = [
        mysqldump, f"-h{os.getenv('DB_HOST', '127.0.0.1')}", f"-P{os.getenv('DB_PORT', '3306')}",
        f"-u{os.getenv('DB_USER')}", "--single-transaction", "--routines", dbname,
    ]
    env = os.environ.copy()
    env["MYSQL_PWD"] = os.getenv("DB_PASSWORD", "")
    try:
        resultado = subprocess.run(comando, capture_output=True, timeout=120, env=env)
        if resultado.returncode != 0 or not resultado.stdout:
            error = resultado.stderr.decode("utf-8", errors="replace")[:300]
            return False, f"mysqldump falló: {error}"
        destino.write_bytes(resultado.stdout)
    except Exception as e:
        return False, f"No se pudo ejecutar mysqldump: {e}"

    for viejo in sorted(backups_dir.glob("*.sql"), key=lambda p: p.stat().st_mtime, reverse=True)[BACKUPS_MAX:]:
        viejo.unlink(missing_ok=True)

    return True, f"Backup creado: {destino.name}"


# ── calendario: tareas + dias especiales resueltos ──────────────────────────
# Igual que core/views.py::calendar_view + _build_day_cell, pero leyendo
# tareas/columnas_tablero por SQL directo en vez de por el patron de
# "cargar todo el proyecto en memoria y regrabarlo entero" que usa
# core/db_store.py -- aqui basta con lo que la vista de Calendario necesita
# (crear/editar una tarea suelta), no hace falta el Kanban/Scrum completo
# para eso. Ver README para la lista de campos que se dejan fuera (subtareas,
# tiempo estimado/real, sprint, empresa/sistema/iniciativa, anclada) -- el
# propio calendar.html de la app real ya usa un modal reducido sin esos 3
# ultimos campos; aqui se recorta un poco mas.

DEFAULT_BOARD_COLUMNS = [
    ("Pendiente", "#f97316", False),
    ("En curso", "#2563eb", False),
    ("En espera", "#ca8a04", False),
    ("Hecha", "#16a34a", True),
]


def load_proyectos_calendario() -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cond, params = _ver_por_espacio("espacio_id")
            cur.execute(f"SELECT id, nombre, color, tipo FROM proyectos WHERE activo=TRUE AND {cond} ORDER BY orden",
                        params or None)
            return [{"id": r[0], "nombre": r[1], "color": r[2], "tipo": r[3]} for r in cur.fetchall()]
    finally:
        conn.close()


def done_status_names_by_project() -> dict:
    """{proyecto_id: {nombres de columnas es_done}} de TODOS los proyectos
    en una sola consulta -- igual que
    core/views.py::done_status_names_by_project(), usado por Calendario
    para el marcador "urgente" sin lanzar una query por cada proyecto
    distinto que aparezca en el mes."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT proyecto_id, nombre FROM columnas_tablero WHERE es_done=TRUE")
            result: dict = {}
            for proyecto_id, nombre in cur.fetchall():
                result.setdefault(proyecto_id, set()).add(nombre)
            return result
    finally:
        conn.close()


def load_home_overview() -> list:
    """Tareas de TODOS los proyectos activos (kanban + scrum) con lo que
    necesita el dashboard de inicio (main.py::inicio) para calcular vencidas
    y de prioridad alta -- una sola consulta con JOIN a proyectos, en vez de
    una query por proyecto."""
    cond, params = _ver_por_espacio("p.espacio_id")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.proyecto_id, p.nombre, p.color, t.titulo, t.estado, "
                "t.prioridad, t.fecha_fin, t.asignado_a, p.espacio_id "
                "FROM tareas t JOIN proyectos p ON p.id = t.proyecto_id "
                f"WHERE t.borrada=0 AND p.activo=TRUE AND {cond}",
                params or None,
            )
            return [
                {
                    "task_id": r[0], "proyecto_id": r[1], "proyecto_nombre": r[2], "proyecto_color": r[3],
                    "title": r[4], "status": r[5], "priority": r[6], "fecha_fin": _fmt_date(r[7]),
                    "asignado_a": r[8], "espacio_id": r[9] or "",
                }
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


# ── espacios (workspaces): fase 1 del multiusuario, solo estructura + ──────
# pertenencia -- ver CLAUDE.md. Quien ve que: visibilidad.py y _ver_* mas abajo.

def get_user_id(username: str):
    """La sesion solo guarda el username (ver auth.py::iniciar_sesion); esto
    resuelve el id de auth_user que necesitan espacios/espacio_usuarios."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM auth_user WHERE username=%s", (username,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def load_usuarios() -> list:
    """Para el desplegable de 'añadir miembro' en Configuración -> Espacios."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, first_name, last_name, email FROM auth_user "
                "WHERE is_active=1 ORDER BY username"
            )
            return [
                {"id": r[0], "username": r[1], "nombre_completo": (f"{r[2]} {r[3]}".strip() or r[1]), "email": r[4]}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def get_or_create_espacio_personal(user_id: int) -> dict:
    """Puerto de get_or_create_personal_board() (CS Manager,
    app/services/boards.py): un usuario tiene como mucho un espacio personal
    -- se valida aqui, en Python, no con una constraint (ver comentario en
    schema_app_tareas.sql sobre el indice unico parcial que MariaDB no
    soporta de forma portable)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, nombre, color FROM espacios WHERE owner_id=%s AND es_personal=TRUE",
                (user_id,),
            )
            row = cur.fetchone()
            if row:
                return {"id": row[0], "nombre": row[1], "color": row[2]}

            espacio_id = str(uuid.uuid4())[:8]
            cur.execute(
                "INSERT INTO espacios (id, nombre, es_personal, owner_id) VALUES (%s, 'Personal', TRUE, %s)",
                (espacio_id, user_id),
            )
            cur.execute(
                "INSERT INTO espacio_usuarios (espacio_id, user_id, rol) VALUES (%s, %s, 'admin')",
                (espacio_id, user_id),
            )
        conn.commit()
        return {"id": espacio_id, "nombre": "Personal", "color": "#6b7280"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_espacios() -> list:
    cond, params = _ver_espacio("e.id")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT e.id, e.nombre, e.color, e.orden, e.activo, e.es_personal, "
                "(SELECT COUNT(*) FROM espacio_usuarios eu WHERE eu.espacio_id = e.id) AS n_miembros, "
                "(SELECT COUNT(*) FROM proyectos p WHERE p.espacio_id = e.id) AS n_proyectos "
                f"FROM espacios e WHERE {cond} ORDER BY e.es_personal DESC, e.orden, e.nombre",
                params or None,
            )
            return [
                {
                    "id": r[0], "nombre": r[1], "color": r[2], "orden": r[3], "activo": bool(r[4]),
                    "es_personal": bool(r[5]), "n_miembros": r[6], "n_proyectos": r[7],
                }
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def get_espacio(espacio_id: str):
    """Para la pagina de detalle de un espacio (routers/proyectos.py) --
    load_espacios() trae todos, esto uno solo por id."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, nombre, color, es_personal FROM espacios WHERE id=%s",
                (espacio_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {"id": row[0], "nombre": row[1], "color": row[2], "es_personal": bool(row[3])}
    finally:
        conn.close()


def contar_tareas_por_proyecto(proyecto_ids: list) -> dict:
    """{proyecto_id: nº de tareas no borradas} -- para el "mini resumen" de
    cada proyecto en la pagina de detalle de un espacio."""
    if not proyecto_ids:
        return {}
    conn = connect()
    try:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(proyecto_ids))
            cur.execute(
                f"SELECT proyecto_id, COUNT(*) FROM tareas WHERE proyecto_id IN ({placeholders}) AND borrada=0 "
                "GROUP BY proyecto_id",
                proyecto_ids,
            )
            return dict(cur.fetchall())
    finally:
        conn.close()


def crear_espacio(espacio_id: str, nombre: str, color: str, creado_por_user_id: int = None) -> None:
    """Quien crea un espacio compartido entra como su admin (rol='admin')
    -- antes de la columna 'rol' no habia ningun "dueno" de un espacio
    compartido, cualquier logueado podia editarlo/desactivarlo."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(orden), 0) FROM espacios")
            orden = cur.fetchone()[0] + 1
            cur.execute(
                "INSERT INTO espacios (id, nombre, color, orden) VALUES (%s, %s, %s, %s)",
                (espacio_id, nombre, color, orden),
            )
            if creado_por_user_id is not None:
                cur.execute(
                    "INSERT INTO espacio_usuarios (espacio_id, user_id, rol) VALUES (%s, %s, 'admin')",
                    (espacio_id, creado_por_user_id),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def editar_espacio(espacio_id: str, nombre: str, color: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE espacios SET nombre=%s, color=%s WHERE id=%s", (nombre, color, espacio_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def toggle_espacio(espacio_id: str) -> bool:
    """Igual que toggle_empresa, pero rechaza el toggle si es un espacio
    personal (siempre tiene que existir, igual que Board.can_delete en CS
    Manager para los suyos). Devuelve False si se rechazo, True si se aplico."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT es_personal FROM espacios WHERE id=%s", (espacio_id,))
            row = cur.fetchone()
            if not row or row[0]:
                return False
            cur.execute("UPDATE espacios SET activo = NOT activo WHERE id=%s", (espacio_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_miembros_espacio(espacio_id: str) -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT u.id, u.username, u.first_name, u.last_name, eu.rol FROM espacio_usuarios eu "
                "JOIN auth_user u ON u.id = eu.user_id WHERE eu.espacio_id=%s ORDER BY u.username",
                (espacio_id,),
            )
            return [
                {"id": r[0], "username": r[1], "nombre_completo": (f"{r[2]} {r[3]}".strip() or r[1]), "rol": r[4]}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def agregar_miembro_espacio(espacio_id: str, user_id: int, rol: str = "editor") -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO espacio_usuarios (espacio_id, user_id, rol) VALUES (%s, %s, %s)",
                (espacio_id, user_id, rol),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def quitar_miembro_espacio(espacio_id: str, user_id: int) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM espacio_usuarios WHERE espacio_id=%s AND user_id=%s",
                (espacio_id, user_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def marcar_it(username: str, es_it: bool) -> None:
    """auth_user.is_staff = si la persona es IT (lo llama auth.iniciar_sesion)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE auth_user SET is_staff=%s WHERE username=%s AND is_staff<>%s",
                        (1 if es_it else 0, username, 1 if es_it else 0))
        conn.commit()
    finally:
        conn.close()


# ── quien ve que: cada espacio solo lo ven sus miembros ──────────────────
# visibilidad.py (dependencia global de main.py) fija aqui, en cada peticion,
# los espacios que puede ver quien la hace. None = lo ve todo (IT, o codigo
# que no viene de una peticion: tests, scripts, la API con llave). Los
# listados de abajo lo aplican solos, asi ninguna pagina nueva se lo salta.
# Un proyecto sin espacio (legado) lo sigue viendo todo el mundo.
_visor: contextvars.ContextVar = contextvars.ContextVar("aris_visor", default=None)


def fijar_visor(espacio_ids) -> None:
    _visor.set(None if espacio_ids is None else frozenset(espacio_ids))


def visor_actual():
    return _visor.get()


def espacios_de_usuario(user_id: int) -> set:
    """Espacios de los que es miembro, mas su espacio personal."""
    if user_id is None:
        return set()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT espacio_id FROM espacio_usuarios WHERE user_id=%s "
                "UNION SELECT id FROM espacios WHERE owner_id=%s",
                (user_id, user_id),
            )
            return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def _lista(v) -> tuple[str, list]:
    v = sorted(v)
    return ",".join(["%s"] * len(v)), v


def _ver_espacio(col: str) -> tuple[str, list]:
    """Condicion SQL para filas de espacios (col = id del espacio)."""
    v = _visor.get()
    if v is None:
        return "TRUE", []
    if not v:
        return "FALSE", []
    ph, params = _lista(v)
    return f"{col} IN ({ph})", params


def _ver_por_espacio(col: str) -> tuple[str, list]:
    """Condicion SQL para filas de proyectos (col = su espacio_id)."""
    v = _visor.get()
    if v is None:
        return "TRUE", []
    if not v:
        return f"{col} IS NULL", []
    ph, params = _lista(v)
    return f"({col} IS NULL OR {col} IN ({ph}))", params


def _ver_proyecto(col: str) -> tuple[str, list]:
    """Condicion SQL para filas que cuelgan de un proyecto (col = proyecto_id)."""
    if _visor.get() is None:
        return "TRUE", []
    cond, params = _ver_por_espacio("espacio_id")
    return f"{col} IN (SELECT id FROM proyectos WHERE {cond})", params


def puede_ver_espacio(espacio_id) -> bool:
    v = _visor.get()
    return v is None or not espacio_id or espacio_id in v


def espacio_de(tipo: str, valor: str):
    """(existe, espacio_id) de lo que nombra un parametro de ruta -- para
    visibilidad.py. tipo es una clave fija de _ESPACIO_DE, nunca del cliente."""
    sql = _ESPACIO_DE[tipo]
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (valor,))
            row = cur.fetchone()
            return (False, None) if not row else (True, row[0])
    finally:
        conn.close()


_DE_PROYECTO = "SELECT p.espacio_id FROM {t} x JOIN proyectos p ON p.id = x.proyecto_id WHERE x.id=%s"
_ESPACIO_DE = {
    "proyecto": "SELECT espacio_id FROM proyectos WHERE id=%s",
    "espacio": "SELECT id FROM espacios WHERE id=%s",
    "tarea": _DE_PROYECTO.format(t="tareas"),
    "columna": _DE_PROYECTO.format(t="columnas_tablero"),
    "documento": _DE_PROYECTO.format(t="documentos"),
    "sprint": _DE_PROYECTO.format(t="sprints"),
    "hito": _DE_PROYECTO.format(t="hitos"),
    "wiki": _DE_PROYECTO.format(t="wiki_paginas"),
}


def es_it_usuario(user_id: int) -> bool:
    if user_id is None:
        return False
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT is_staff FROM auth_user WHERE id=%s", (user_id,))
            row = cur.fetchone()
            return bool(row and row[0])
    finally:
        conn.close()


def rol_en_espacio(espacio_id: str, user_id: int):
    """None si no es miembro, o 'admin'/'editor'/'lector' si lo es."""
    if user_id is None:
        return None
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rol FROM espacio_usuarios WHERE espacio_id=%s AND user_id=%s",
                (espacio_id, user_id),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def cambiar_rol_miembro_espacio(espacio_id: str, user_id: int, rol: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE espacio_usuarios SET rol=%s WHERE espacio_id=%s AND user_id=%s",
                (rol, espacio_id, user_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def puede_administrar_espacio(espacio_id: str, user_id: int) -> bool:
    return rol_en_espacio(espacio_id, user_id) == "admin"


def puede_editar_espacio(espacio_id: str, user_id: int) -> bool:
    return rol_en_espacio(espacio_id, user_id) in ("admin", "editor")


# Los 5 interruptores de permisos granulares de un Editor dentro de un
# espacio (Admin siempre los salta; Lector nunca puede nada de esto de por
# si) -- ver CLAUDE.md. Lista fija para poder validar contra ella cualquier
# clave que llegue de un formulario, nunca interpolar un nombre de columna
# que venga del cliente sin pasar por aqui.
PERMISOS_ESPACIO = [
    "editores_mueven_tareas_ajenas", "editores_eliminan_tareas_ajenas",
    "editores_gestionan_columnas", "editores_editan_apariencia",
    "editores_gestionan_documentos_wiki",
]


def get_permisos_espacio(espacio_id: str) -> dict:
    """Los 5 interruptores de un espacio -- todo True (permisivo, igual que
    el comportamiento de siempre) si el espacio no tiene fila por lo que
    sea, para no bloquear nada por error."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(PERMISOS_ESPACIO)} FROM espacios WHERE id=%s",
                (espacio_id,),
            )
            row = cur.fetchone()
            if not row:
                return {p: True for p in PERMISOS_ESPACIO}
            return {p: bool(v) for p, v in zip(PERMISOS_ESPACIO, row)}
    finally:
        conn.close()


def editar_permisos_espacio(espacio_id: str, **flags) -> None:
    """flags: subconjunto (o todas) de PERMISOS_ESPACIO -> bool. Cualquier
    clave que no este en PERMISOS_ESPACIO se ignora (nunca se interpola un
    nombre de columna que no este en la lista fija de arriba)."""
    claves = [k for k in flags if k in PERMISOS_ESPACIO]
    if not claves:
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            set_sql = ", ".join(f"{k}=%s" for k in claves)
            cur.execute(
                f"UPDATE espacios SET {set_sql} WHERE id=%s",
                [bool(flags[k]) for k in claves] + [espacio_id],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def puede_actuar_en_proyecto(proyecto_id: str, user_id: int, permiso: str, asignado_a=None) -> bool:
    """Punto unico de permiso para tarea/columna/documento/wiki dentro de un
    proyecto YA ABIERTO -- distinto de puede_editar_espacio, que es para
    crear/editar/borrar el PROYECTO en si. Sin espacio asignado -> True
    (proyecto legado, mismo criterio que _puede_editar_espacio_de en
    tablero.py). Admin del espacio -> True siempre. Lector o no-miembro ->
    False (salvo IT, que siempre puede). Editor -> True si la tarea es suya (asignado_a == user_id,
    cuando aplica), si no, el valor del interruptor `permiso`."""
    proyecto = get_proyecto(proyecto_id)
    if not proyecto or not proyecto["espacio_id"]:
        return True
    rol = rol_en_espacio(proyecto["espacio_id"], user_id)
    # IT administra la app entera: actua en todos los espacios, sea o no miembro
    if rol == "admin" or es_it_usuario(user_id):
        return True
    if rol != "editor":
        return False
    if asignado_a is not None and user_id == asignado_a:
        return True
    return get_permisos_espacio(proyecto["espacio_id"]).get(permiso, True)


def puede_escribir_en_proyecto(proyecto_id: str, user_id: int) -> bool:
    """Si puede cambiar algo en el proyecto (para pintar el tablero con o
    sin botones): sin espacio, admin o editor del espacio, o IT. Lector o no
    miembro -> False. Cada accion se sigue comprobando con
    puede_actuar_en_proyecto."""
    proyecto = get_proyecto(proyecto_id)
    if not proyecto or not proyecto["espacio_id"]:
        return True
    return rol_en_espacio(proyecto["espacio_id"], user_id) in ("admin", "editor") or es_it_usuario(user_id)


def get_columna_proyecto_id(col_id: str):
    """proyecto_id de una columna del tablero, o None -- para poder
    comprobar permisos ANTES de renombrar/mover/eliminar una columna (las
    funciones de columnas de mas abajo ya hacen esta misma consulta por su
    cuenta, pero no devuelven el proyecto_id al router)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT proyecto_id FROM columnas_tablero WHERE id=%s", (col_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def load_proyectos_por_espacio(espacio_id: str) -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, nombre, color, tipo FROM proyectos WHERE espacio_id=%s ORDER BY orden",
                (espacio_id if puede_ver_espacio(espacio_id) else "",),
            )
            return [{"id": r[0], "nombre": r[1], "color": r[2], "tipo": r[3]} for r in cur.fetchall()]
    finally:
        conn.close()


def load_espacios_activos() -> list:
    """Para el <select> de espacio en el modal de proyecto (_base.html) --
    global de Jinja2 (ver templating.py), igual motivo que proyectos_sidebar."""
    cond, params = _ver_espacio("id")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, nombre, es_personal FROM espacios WHERE activo=TRUE AND {cond} "
                "ORDER BY es_personal DESC, orden, nombre",
                params or None,
            )
            return [{"id": r[0], "nombre": r[1], "es_personal": bool(r[2])} for r in cur.fetchall()]
    finally:
        conn.close()


def load_board_columns(proyecto_id: str) -> list:
    """Columnas del proyecto -- si no tiene ninguna todavia, crea las 4 por
    defecto y las persiste, igual que core/db_store.py::load_board_columns
    (lazy-create la primera vez que se pide un proyecto sin columnas)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, nombre, orden, es_done, color, max_tareas FROM columnas_tablero "
                "WHERE proyecto_id=%s ORDER BY orden", (proyecto_id,),
            )
            filas = cur.fetchall()
            if filas:
                return [{"id": r[0], "nombre": r[1], "orden": r[2], "es_done": bool(r[3]), "color": r[4],
                          "max_tareas": r[5]} for r in filas]
            columnas = []
            for i, (nombre, color, es_done) in enumerate(DEFAULT_BOARD_COLUMNS):
                col_id = str(uuid.uuid4())[:8]
                cur.execute(
                    "INSERT INTO columnas_tablero (id, proyecto_id, nombre, orden, es_done, color) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (col_id, proyecto_id, nombre, i, es_done, color),
                )
                columnas.append({"id": col_id, "nombre": nombre, "orden": i, "es_done": es_done, "color": color,
                                  "max_tareas": 0})
        conn.commit()
        return columnas
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def is_done_column(proyecto_id: str, nombre_estado: str) -> bool:
    return any(c["nombre"] == nombre_estado and c["es_done"] for c in load_board_columns(proyecto_id))


def bloquea_cierre_por_subtareas(proyecto_id: str, nuevo_status: str, subtareas_raw) -> bool:
    """Igual que core/views.py::_bloquea_cierre_por_subtareas: True si el
    proyecto exige subtareas completas para cerrar (requiere_subtareas_completas)
    y nuevo_status es una columna es_done pero subtareas_raw (el JSON de
    subtareas -- YA con los cambios del formulario aplicados, si los hay, no
    necesariamente lo que hay en la BD) tiene alguna sin marcar. Sin
    subtareas (o sin el flag activado) no bloquea."""
    if not is_done_column(proyecto_id, nuevo_status):
        return False
    proyecto = get_proyecto(proyecto_id)
    if not proyecto or not proyecto.get("requiere_subtareas_completas"):
        return False
    if not subtareas_raw:
        return False
    try:
        subs = json.loads(subtareas_raw)
    except Exception:
        return False
    return any(not (s.get("d") or s.get("checked")) for s in subs)


def tarea_estado_actual(task_id: str):
    """(proyecto_id, estado, subtareas_raw, asignado_a) de una tarea no
    borrada tal y como esta en la BD ahora mismo, o (None, None, None, None)
    si no existe -- usado por editar_tarea/reordenar_tarea/borrar_tarea
    (router) para saber el proyecto/estado/asignado ANTES de este cambio
    (para el "solo si cambia de columna" de _bloquea_cierre_por_subtareas, y
    para puede_actuar_en_proyecto) sin fiarse de nada que mande el cliente."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT proyecto_id, estado, subtareas, asignado_a FROM tareas WHERE id=%s AND borrada=0",
                (task_id,),
            )
            row = cur.fetchone()
            return row if row else (None, None, None, None)
    finally:
        conn.close()


def load_categorias_para_proyecto(proyecto_id: str) -> list:
    """Nombres de categoria para el selector del modal de tarea: globales
    (proyecto_id IS NULL) + las propias de ese proyecto, activas."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT nombre FROM categorias WHERE activo=TRUE "
                "AND (proyecto_id IS NULL OR proyecto_id=%s) ORDER BY nombre", (proyecto_id,),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _resolve_categoria_id(cur, proyecto_id: str, nombre: str):
    """Mismo criterio que TaskStoreDB.save() en core/db_store.py: primero
    categoria propia del proyecto con ese nombre, si no una global con ese
    nombre; si no hay ninguna, NULL (la tarea queda sin categoria)."""
    if not nombre:
        return None
    cur.execute("SELECT id FROM categorias WHERE proyecto_id=%s AND nombre=%s", (proyecto_id, nombre))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT id FROM categorias WHERE proyecto_id IS NULL AND nombre=%s", (nombre,))
    row = cur.fetchone()
    return row[0] if row else None


def load_tasks_by_due_date(date_from: str, date_to: str, proyecto_id: str = "", categoria: str = "") -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            sql = (
                "SELECT t.id, t.proyecto_id, c.nombre, t.titulo, t.estado, t.descripcion, "
                "t.proximo_paso, t.prioridad, t.fecha_inicio, t.fecha_fin, t.anclada, "
                "t.sprint_id, t.subtareas, t.tiempo_estimado, t.tiempo_real "
                "FROM tareas t LEFT JOIN categorias c ON c.id = t.categoria_id "
                "WHERE t.borrada = 0 AND t.fecha_fin BETWEEN %s AND %s"
            )
            params = [date_from, date_to]
            cond, extra = _ver_proyecto("t.proyecto_id")
            sql += f" AND {cond}"
            params += extra
            if proyecto_id:
                sql += " AND t.proyecto_id=%s"
                params.append(proyecto_id)
            if categoria:
                sql += " AND c.nombre=%s"
                params.append(categoria)
            cur.execute(sql, params)
            cols = ["task_id", "proyecto_id", "category", "title", "status", "description",
                     "next_step", "priority", "fecha_inicio", "fecha_fin", "anclada",
                     "sprint_id", "subtareas_raw", "tiempo_estimado", "tiempo_real"]
            return [
                dict(zip(cols, [r[0], r[1], _nn(r[2]), r[3], r[4], _nn(r[5]), _nn(r[6]), r[7],
                                 _fmt_date(r[8]), _fmt_date(r[9]), bool(r[10]),
                                 _nn(r[11]), _nn(r[12]), r[13], r[14]]))
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def load_task(task_id: str):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.proyecto_id, c.nombre, t.titulo, t.estado, t.descripcion, "
                "t.proximo_paso, t.prioridad, t.fecha_inicio, t.fecha_fin, t.anclada "
                "FROM tareas t LEFT JOIN categorias c ON c.id = t.categoria_id WHERE t.id=%s AND t.borrada=0",
                (task_id,),
            )
            r = cur.fetchone()
            if not r:
                return None
            cols = ["task_id", "proyecto_id", "category", "title", "status", "description",
                     "next_step", "priority", "fecha_inicio", "fecha_fin", "anclada"]
            return dict(zip(cols, [r[0], r[1], _nn(r[2]), r[3], r[4], _nn(r[5]), _nn(r[6]), r[7],
                                     _fmt_date(r[8]), _fmt_date(r[9]), bool(r[10])]))
    finally:
        conn.close()


def get_tarea_by_id(task_id: str):
    """Una tarea por id, sin conocer su proyecto de antemano y SIN filtrar
    por borrada -- para el panel de detalle lateral (GET /tarea/<id>/detalle/),
    que puede abrirse desde el buscador global con una tarea de cualquier
    proyecto. Igual que core/db_store.py::get_task_by_id +
    core/views.py::task_detail (que resuelve proyecto/sprint aparte, no aqui)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.proyecto_id, c.nombre, t.titulo, t.estado, t.prioridad, t.descripcion, "
                "t.proximo_paso, t.creada_en, t.actualizada_en, t.borrada, t.fecha_inicio, t.fecha_fin, "
                "t.subtareas, t.tiempo_estimado, t.tiempo_real, t.empresa_id, t.sistema_id, t.iniciativa_id, "
                "t.anclada, t.sprint_id "
                "FROM tareas t LEFT JOIN categorias c ON c.id = t.categoria_id WHERE t.id=%s",
                (task_id,),
            )
            r = cur.fetchone()
            if not r:
                return None
            return {
                "task_id": r[0], "proyecto_id": r[1], "category": _nn(r[2]), "title": r[3], "status": r[4],
                "priority": r[5], "description": _nn(r[6]), "next_step": _nn(r[7]),
                "creada_en": _fmt_datetime(r[8]), "actualizada_en": _fmt_datetime(r[9]), "deleted": bool(r[10]),
                "fecha_inicio": _fmt_date(r[11]), "fecha_fin": _fmt_date(r[12]), "subtareas_raw": _nn(r[13]),
                # tiempo_estimado/tiempo_real son DECIMAL en MariaDB -> PyMySQL
                # los devuelve como decimal.Decimal, que json.dumps no sabe
                # serializar (a diferencia de los demas usos de estas mismas
                # columnas en este archivo, que solo se pintan en Jinja, nunca
                # en un JSONResponse) -- se convierten aqui, en el unico sitio
                # que las manda como JSON.
                "tiempo_estimado": float(r[14]) if r[14] is not None else None,
                "tiempo_real": float(r[15]) if r[15] is not None else None,
                "empresa_id": _nn(r[16]), "sistema_id": _nn(r[17]), "iniciativa_id": _nn(r[18]),
                "anclada": bool(r[19]), "sprint_id": _nn(r[20]),
            }
    finally:
        conn.close()


def load_dias_especiales_resueltos() -> list:
    """fecha_inicio/fecha_fin/nombre/categoria/color/urgente/nota de cada dia
    especial -- para construir las celdas del Calendario (sede/aviso/
    anotacion), igual que core/views.py::resolve_dia_especial."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT d.fecha_inicio, d.fecha_fin, d.nota, d.color, d.empresa_id, "
                "t.nombre, t.categoria, t.urgente, e.nombre "
                "FROM dias_especiales d "
                "LEFT JOIN tipos_dia_especial t ON t.id = d.tipo_id "
                "LEFT JOIN empresas e ON e.id = d.empresa_id"
            )
            resultado = []
            for r in cur.fetchall():
                if r[4]:
                    nombre, categoria, urgente = (("Sede " + r[8]) if r[8] else "Sede"), "sede", False
                else:
                    nombre, categoria, urgente = (r[5] or "?"), (r[6] or "anotacion"), bool(r[7])
                resultado.append({
                    "fecha_inicio": _fmt_date(r[0]), "fecha_fin": _fmt_date(r[1]), "nota": _nn(r[2]),
                    "color": r[3], "nombre": nombre, "categoria": categoria, "urgente": urgente,
                })
            return resultado
    finally:
        conn.close()


def search_tasks(query: str) -> list:
    """Buscador global de la cabecera -- igual que
    core/db_store.py::TaskStoreDB.search_tasks: tareas no borradas cuyo
    titulo/descripcion/proximo paso/categoria contengan `query` (LIKE en
    SQL, sin limite de resultados, % y _ escapados para que no se
    interpreten como comodines)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            cond, extra = _ver_proyecto("t.proyecto_id")
            cur.execute(
                "SELECT t.id, t.titulo, t.estado, t.proyecto_id, p.nombre, p.color "
                "FROM tareas t LEFT JOIN categorias c ON c.id = t.categoria_id "
                "LEFT JOIN proyectos p ON p.id = t.proyecto_id "
                "WHERE t.borrada = 0 AND (t.titulo LIKE %s OR t.descripcion LIKE %s "
                f"OR t.proximo_paso LIKE %s OR c.nombre LIKE %s) AND {cond}",
                (like, like, like, like, *extra),
            )
            return [
                {
                    "task_id": r[0], "title": r[1], "status": r[2], "project_id": r[3],
                    "project_name": r[4] or "?", "project_color": r[5] or "#2563eb",
                }
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def search_wiki_pages(query: str) -> list:
    """Paginas de la Wiki propia (wiki_paginas) cuyo titulo o contenido
    contengan `query` -- mismo patron LIKE que search_tasks, usado por
    Nexus para buscar tambien aqui: antes solo miraba el catalogo externo
    confluence_local (que tiene su propia tabla paginas_wiki, un snapshot
    ajeno) y las tareas de esta app, nunca la Wiki real de cada proyecto.
    El LIKE sobre `contenido` matchea el JSON crudo de EditorJS (incluye
    algo de marcado interno de los bloques) -- suficiente para encontrar la
    pagina, no pretende ser un extracto limpio del texto."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            cond, extra = _ver_proyecto("w.proyecto_id")
            cur.execute(
                "SELECT w.id, w.titulo, w.icono, w.proyecto_id, p.nombre, p.color "
                "FROM wiki_paginas w LEFT JOIN proyectos p ON p.id = w.proyecto_id "
                f"WHERE (w.titulo LIKE %s OR w.contenido LIKE %s) AND {cond}",
                (like, like, *extra),
            )
            return [
                {
                    "page_id": r[0], "title": r[1], "icon": r[2] or "📄", "project_id": r[3],
                    "project_name": r[4] or "?", "project_color": r[5] or "#2563eb",
                }
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def load_avisos_activos() -> dict:
    """{"alarmas": [...], "avisos": [...]} -- dias especiales de categoria
    'banner' activos HOY, separados por urgente/no-urgente -- igual que
    core/views.py::base_context's avisos_activos/alarmas_activas, mostrado
    en todas las paginas (ver _base.html), no solo en Calendario."""
    hoy = datetime.now().strftime("%Y-%m-%d")
    alarmas, avisos = [], []
    for d in load_dias_especiales_resueltos():
        if d["categoria"] != "banner" or not (d["fecha_inicio"] <= hoy <= d["fecha_fin"]):
            continue
        entry = {"nombre": d["nombre"], "nota": d["nota"], "color": d["color"],
                  "fecha_inicio": d["fecha_inicio"], "fecha_fin": d["fecha_fin"]}
        (alarmas if d["urgente"] else avisos).append(entry)
    return {"alarmas": alarmas, "avisos": avisos}


def crear_tarea(task_id: str, proyecto_id: str, category: str, title: str, status: str, priority: str,
                 description: str, next_step: str, fecha_inicio: str, fecha_fin: str,
                 empresa_id: str = "", iniciativa_id: str = "", sistema_id: str = "",
                 anclada: bool = False, subtareas: str = "", tiempo_estimado=None, tiempo_real=None,
                 sprint_id: str = "", mantis_id=None, generada_por_ia: bool = False, reunion_id: str = "",
                 cerrada_en: str = "", asignado_a: int = None) -> None:
    """cerrada_en (opcional): solo lo usa la importacion masiva del CSV de
    Mantis (mantis_csv_import.py) para incidencias que YA llegan resueltas/
    cerradas con una fecha de cierre real del propio export -- el resto de
    quien crea tareas (Backlog, Excel, importar incidencias una a una) no lo
    pasa nunca, y la tarea nace sin cerrar como siempre (NULL), igual que
    antes de anadir este parametro."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            categoria_id = _resolve_categoria_id(cur, proyecto_id, category)
            cur.execute(
                "SELECT COALESCE(MAX(orden), 0) FROM tareas WHERE proyecto_id=%s AND estado=%s AND borrada=0",
                (proyecto_id, status),
            )
            orden = cur.fetchone()[0] + 1
            now = now_text()
            cur.execute(
                "INSERT INTO tareas (id, proyecto_id, categoria_id, titulo, estado, descripcion, proximo_paso, "
                "prioridad, orden, borrada, fecha_inicio, fecha_fin, empresa_id, iniciativa_id, sistema_id, "
                "anclada, subtareas, tiempo_estimado, tiempo_real, sprint_id, mantis_id, generada_por_ia, "
                "reunion_id, cerrada_en, asignado_a, creada_en, actualizada_en) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (task_id, proyecto_id, categoria_id, title, status, description, next_step, priority, orden,
                 fecha_inicio or None, fecha_fin or None, empresa_id or None, iniciativa_id or None,
                 sistema_id or None, anclada, subtareas or None, tiempo_estimado or None, tiempo_real or None,
                 sprint_id or None, mantis_id, generada_por_ia, reunion_id or None, cerrada_en or None,
                 asignado_a, now, now),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def duplicar_tarea(task_id: str) -> str:
    """Igual que core/views.py::task_duplicate: copia una tarea dentro del
    MISMO proyecto -- titulo con "(copia)" al final, subtareas y tiempo
    estimado copiados, tiempo real/fechas/sprint vacios. empresa_id/
    sistema_id/iniciativa_id/mantis_id NO se copian (el original tampoco lo
    hace). El estado se fija al literal 'Pendiente' -- igual quirk heredado
    que el original (no busca la primera columna no-done del proyecto, a
    diferencia de duplicar_proyecto): si el proyecto no tiene una columna
    llamada exactamente asi, la copia nace en un estado sin columna visible
    en el tablero, igual que le pasaria al original. Devuelve el id de la
    tarea nueva, o "" si la original no existe/esta borrada."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.proyecto_id, c.nombre, t.titulo, t.descripcion, t.proximo_paso, "
                "t.prioridad, t.subtareas, t.tiempo_estimado "
                "FROM tareas t LEFT JOIN categorias c ON c.id = t.categoria_id "
                "WHERE t.id=%s AND t.borrada=0", (task_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return ""
    proyecto_id, categoria, titulo, descripcion, proximo_paso, prioridad, subtareas, tiempo_estimado = row
    nuevo_id = str(uuid.uuid4())[:8]
    crear_tarea(
        nuevo_id, proyecto_id, _nn(categoria), titulo + " (copia)", "Pendiente", prioridad,
        _nn(descripcion), _nn(proximo_paso), "", "",
        subtareas=_nn(subtareas), tiempo_estimado=tiempo_estimado,
    )
    return nuevo_id


def editar_tarea(task_id: str, proyecto_id: str, category: str, title: str, status: str, priority: str,
                  description: str, next_step: str, fecha_inicio: str, fecha_fin: str,
                  empresa_id: str = None, iniciativa_id: str = None, sistema_id: str = None,
                  anclada: bool = None, subtareas: str = None, tiempo_estimado=None, tiempo_real=None,
                  sprint_id: str = None, asignado_a=Ellipsis) -> None:
    """asignado_a: Ellipsis (valor por defecto) significa "no tocar" (igual
    que el resto de campos opcionales usan None para eso) -- pero None SI es
    un valor valido aqui ("sin asignar", elegido a proposito en el modal), asi
    que no se puede reusar None como centinela como con empresa_id/etc."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT estado, iniciada_en, cerrada_en, empresa_id, iniciativa_id, sistema_id, anclada, "
                "subtareas, tiempo_estimado, tiempo_real, sprint_id FROM tareas WHERE id=%s", (task_id,),
            )
            actual = cur.fetchone()
            if not actual:
                return
            old_status, iniciada_en, cerrada_en = actual[0], actual[1], actual[2]
            categoria_id = _resolve_categoria_id(cur, proyecto_id, category)
            empresa_final = (empresa_id or None) if empresa_id is not None else actual[3]
            iniciativa_final = (iniciativa_id or None) if iniciativa_id is not None else actual[4]
            sistema_final = (sistema_id or None) if sistema_id is not None else actual[5]
            anclada_final = anclada if anclada is not None else bool(actual[6])
            # columna JSON NULL: una cadena vacia no es JSON valido (viola el
            # CHECK constraint que MariaDB pone solas a las columnas JSON) --
            # "sin subtareas" se guarda como NULL, no como "".
            subtareas_final = (subtareas or None) if subtareas is not None else actual[7]
            tiempo_estimado_final = tiempo_estimado if tiempo_estimado is not None else actual[8]
            tiempo_real_final = tiempo_real if tiempo_real is not None else actual[9]
            sprint_final = (sprint_id or None) if sprint_id is not None else actual[10]

            extra_sql, extra_params = "", []
            if status != old_status:
                cur.execute(
                    "SELECT COALESCE(MAX(orden), 0) FROM tareas WHERE proyecto_id=%s AND estado=%s AND borrada=0",
                    (proyecto_id, status),
                )
                extra_sql += ", orden=%s"
                extra_params.append(cur.fetchone()[0] + 1)
                if not iniciada_en:
                    extra_sql += ", iniciada_en=%s"
                    extra_params.append(now_text())
                if is_done_column(proyecto_id, status):
                    if not cerrada_en:
                        extra_sql += ", cerrada_en=%s"
                        extra_params.append(now_text())
                else:
                    extra_sql += ", cerrada_en=NULL"

            if asignado_a is not Ellipsis:
                extra_sql += ", asignado_a=%s"
                extra_params.append(asignado_a)

            cur.execute(
                "UPDATE tareas SET titulo=%s, proyecto_id=%s, categoria_id=%s, estado=%s, prioridad=%s, "
                "descripcion=%s, proximo_paso=%s, fecha_inicio=%s, fecha_fin=%s, empresa_id=%s, iniciativa_id=%s, "
                f"sistema_id=%s, anclada=%s, subtareas=%s, tiempo_estimado=%s, tiempo_real=%s, sprint_id=%s, actualizada_en=%s{extra_sql} "
                "WHERE id=%s",
                [title, proyecto_id, categoria_id, status, priority, description, next_step,
                 fecha_inicio or None, fecha_fin or None, empresa_final, iniciativa_final, sistema_final,
                 anclada_final, subtareas_final, tiempo_estimado_final, tiempo_real_final, sprint_final,
                 now_text()] + extra_params + [task_id],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── importar incidencias de Mantis como tareas ──────────────────────────────
# Igual que core/views.py::mantis_importar_tareas/_mantis_ids_ya_importadas.

_MANTIS_ID_EN_DESCRIPCION = re.compile(r"Mantis #(\d+)")


def tareas_mantis_ids_importadas() -> set:
    """Ids de incidencias de Mantis que ya tienen una tarea (no borrada) EN
    CUALQUIER PROYECTO -- antes esto solo miraba el proyecto destino
    configurado en ese momento, asi que cambiar ese ajuste (o mover a mano
    una tarea importada a otro proyecto) dejaba reimportar la misma
    incidencia sin avisar. Se reconocen por tareas.mantis_id (columna real,
    la que deja crear_tarea() en las tareas nuevas) o, para tareas
    importadas antes de que existiera esa columna, por la marca
    "Mantis #<id>" que ya deja el pie de la descripcion -- union de las dos
    señales para no perder proteccion sobre datos antiguos."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            ids = set()
            cur.execute("SELECT mantis_id FROM tareas WHERE borrada=0 AND mantis_id IS NOT NULL")
            for (mid,) in cur.fetchall():
                ids.add(mid)
            cur.execute(
                "SELECT descripcion FROM tareas WHERE borrada=0 AND mantis_id IS NULL AND descripcion LIKE '%%Mantis #%%'"
            )
            for (desc,) in cur.fetchall():
                m = _MANTIS_ID_EN_DESCRIPCION.search(desc or "")
                if m:
                    ids.add(int(m.group(1)))
            return ids
    finally:
        conn.close()


def find_or_crear_iniciativa_mantis() -> str:
    """Iniciativa transversal "Mantis" (marca que la tarea viene de ahi) --
    se crea la primera vez que hace falta, igual que en la app real."""
    for i in load_iniciativas():
        if i["nombre"].strip().lower() == "mantis":
            return i["id"]
    iniciativa_id = str(uuid.uuid4())[:8]
    crear_iniciativa(iniciativa_id, "Mantis", "", "#0891b2")
    return iniciativa_id


# ── SLA: horas laborables sobre tareas reales + historico local de Mantis ───
# Igual que core/views.py::sla_dashboard/sla_export/festivos_dates/sla_hours/
# fmt_hours + core/db_store.py::load_tasks_for_sla/load_distinct_task_categories/
# load_mantis_historico/upsert_mantis_historico. mantis_open_hours/
# mantis_iso_a_naive tambien viven en core/views.py pero se reimplementan
# aqui (son puro calculo de fechas, sin nada de Django detras).

def fmt_hours(h):
    if h is None:
        return "—"
    if h < 1:
        return f"{int(h * 60)} min"
    if h < 48:
        return f"{h:.1f} h"
    return f"{h / 24:.1f} días"


def sla_hours(t1_str: str, t2_str: str, festivos: set = None):
    try:
        t1 = datetime.strptime(t1_str.strip(), "%Y-%m-%d %H:%M")
        t2 = datetime.strptime(t2_str.strip(), "%Y-%m-%d %H:%M")
    except Exception:
        return None
    if festivos is None:
        return max(0.0, round((t2 - t1).total_seconds() / 3600, 1))
    total = 0.0
    dia = t1.date()
    while dia <= t2.date():
        if dia.weekday() < 5 and dia not in festivos:
            inicio_dia = datetime.combine(dia, datetime.min.time())
            fin_dia = inicio_dia + timedelta(days=1)
            total += (min(t2, fin_dia) - max(t1, inicio_dia)).total_seconds() / 3600
        dia += timedelta(days=1)
    return max(0.0, round(total, 1))


def festivos_dates() -> set:
    """Mismo criterio que core/views.py::festivos_dates: dias_especiales
    cuyo tipo tiene 'cuenta_para_sla' marcado (Configuracion -> Calendario),
    no un tipo fijo por id."""
    tipos_sla = {t["id"] for t in load_tipos_dia_especial() if t["cuenta_para_sla"]}
    festivos = set()
    for d in load_dias_especiales():
        if d["tipo_id"] not in tipos_sla:
            continue
        try:
            ini = datetime.strptime(d["fecha_inicio"], "%Y-%m-%d").date()
            fin = datetime.strptime(d["fecha_fin"], "%Y-%m-%d").date()
        except Exception:
            continue
        dia = ini
        while dia <= fin:
            festivos.add(dia)
            dia += timedelta(days=1)
    return festivos


def load_tasks_for_sla(date_from: str, date_to: str, proyecto_id: str = "", categoria: str = "") -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            sql = (
                "SELECT t.id, t.proyecto_id, c.nombre, t.titulo, t.estado, t.prioridad, "
                "t.creada_en, t.iniciada_en, t.cerrada_en, t.fecha_fin "
                "FROM tareas t LEFT JOIN categorias c ON c.id = t.categoria_id "
                "WHERE t.borrada = 0 AND t.estado != 'OUTCAST' AND DATE(t.creada_en) BETWEEN %s AND %s"
            )
            params = [date_from, date_to]
            cond, extra = _ver_proyecto("t.proyecto_id")
            sql += f" AND {cond}"
            params += extra
            if proyecto_id:
                sql += " AND t.proyecto_id=%s"
                params.append(proyecto_id)
            if categoria:
                sql += " AND c.nombre=%s"
                params.append(categoria)
            cur.execute(sql, params)
            cols = ["task_id", "proyecto_id", "category", "title", "status", "priority",
                     "created_at", "started_at", "closed_at", "fecha_fin"]
            return [
                dict(zip(cols, [r[0], r[1], _nn(r[2]), r[3], r[4], r[5],
                                 _fmt_datetime(r[6]), _fmt_datetime(r[7]), _fmt_datetime(r[8]), _fmt_date(r[9])]))
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def load_distinct_task_categories() -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT c.nombre FROM tareas t JOIN categorias c ON c.id = t.categoria_id "
                "WHERE t.borrada=0 AND c.nombre IS NOT NULL AND c.nombre != '' ORDER BY c.nombre"
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def mantis_open_hours(creada_iso: str):
    try:
        creada = datetime.fromisoformat(creada_iso.strip())
        ahora = datetime.now(creada.tzinfo) if creada.tzinfo else datetime.now()
        return max(0.0, round((ahora - creada).total_seconds() / 3600, 1))
    except Exception:
        return None


def mantis_iso_a_naive(iso_str: str):
    try:
        return datetime.fromisoformat(iso_str.strip()).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def load_mantis_historico() -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT mantis_id, proyecto, categoria, prioridad, resumen, "
                "estado_name, estado_label, creada, actualizada, resuelta_en, ultima_sync "
                "FROM mantis_incidencias_historico ORDER BY creada"
            )
            cols = ["mantis_id", "proyecto", "categoria", "prioridad", "resumen",
                     "estado_name", "estado_label", "creada", "actualizada", "resuelta_en", "ultima_sync"]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def upsert_mantis_historico(rows: list, sync_ts: str) -> None:
    if not rows:
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO mantis_incidencias_historico "
                "(mantis_id, proyecto, categoria, prioridad, resumen, estado_name, "
                " estado_label, creada, actualizada, resuelta_en, ultima_sync) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE "
                "proyecto=VALUES(proyecto), categoria=VALUES(categoria), prioridad=VALUES(prioridad), "
                "resumen=VALUES(resumen), estado_name=VALUES(estado_name), estado_label=VALUES(estado_label), "
                "actualizada=VALUES(actualizada), "
                "resuelta_en=IF(resuelta_en IS NULL, VALUES(resuelta_en), resuelta_en), "
                "ultima_sync=VALUES(ultima_sync)",
                [(r["mantis_id"], r["proyecto"], r["categoria"], r["prioridad"], r["resumen"],
                  r["estado_name"], r["estado_label"], r["creada"], r["actualizada"],
                  r["resuelta_en"], sync_ts) for r in rows],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_mantis_origen_cache(mantis_ids: list) -> dict:
    """{mantis_id: {"es_ia":bool,"es_correo":bool}} de las incidencias ya
    clasificadas antes -- igual que core/db_store.py::get_mantis_origen_cache.
    El origen de una incidencia no cambia una vez creada, asi que evita
    volver a llamar a mantis_client.clasificar_origen() (una llamada a
    Mantis por incidencia) para lo que ya se sabe."""
    if not mantis_ids:
        return {}
    conn = connect()
    try:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(mantis_ids))
            cur.execute(
                f"SELECT mantis_id, es_ia, es_correo FROM mantis_origen_cache WHERE mantis_id IN ({placeholders})",
                mantis_ids,
            )
            return {r[0]: {"es_ia": bool(r[1]), "es_correo": bool(r[2])} for r in cur.fetchall()}
    finally:
        conn.close()


def upsert_mantis_origen_cache(rows: list) -> None:
    """rows: [{"mantis_id","proyecto_id","creada","es_ia","es_correo",
    "categoria"?,"tecnico"?,"proyecto_nombre"?}, ...] -- las 5 primeras
    claves son iguales que core/db_store.py::upsert_mantis_origen_cache (esa
    parte SI se comparte con Django, misma tabla). categoria/tecnico/
    proyecto_nombre son 3 columnas nullable propias de esta v2, solo
    informativas: son una FOTO del momento de clasificar (se quedan viejas si
    la incidencia se mueve o reasigna), asi que el desglose ya NO las lee --
    proyecto/tecnico/categoria actuales salen de mantis_espejo (ver
    get_mantis_espejo), y de esta tabla solo se usa es_ia/es_correo."""
    if not rows:
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO mantis_origen_cache "
                "(mantis_id, proyecto_id, creada, es_ia, es_correo, clasificado_en, categoria, tecnico, proyecto_nombre) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE es_ia=VALUES(es_ia), es_correo=VALUES(es_correo), "
                "clasificado_en=VALUES(clasificado_en), categoria=VALUES(categoria), "
                "tecnico=VALUES(tecnico), proyecto_nombre=VALUES(proyecto_nombre)",
                [(r["mantis_id"], r["proyecto_id"], r["creada"], r["es_ia"], r["es_correo"], now_text(),
                  r.get("categoria"), r.get("tecnico"), r.get("proyecto_nombre"))
                 for r in rows],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Espejo local de Mantis para el desglose de origen del SLA (tabla
# mantis_espejo): proyecto/tecnico/categoria/fecha de TODAS las incidencias,
# puesto al dia en cada calculo con mantis_client.fetch_actualizadas_desde()
# (solo lo que cambio desde la ultima vez). Sustituye al antiguo "rango ya
# cubierto hoy" (ajustes mantis.origen_sync.<id>), que servia datos viejos:
# no veia incidencias creadas despues del primer calculo del dia, ni las que
# se movian de proyecto o se reasignaban despues de cachearlas.

def get_mantis_espejo_marca() -> str:
    """updated_at (ISO de Mantis, con zona) mas reciente ya volcado al
    espejo, o "" si el espejo esta vacio (toca listado completo)."""
    return get_ajuste("mantis.espejo_actualizado_hasta", "")


def set_mantis_espejo_marca(iso: str) -> None:
    set_ajuste("mantis.espejo_actualizado_hasta", iso)


def upsert_mantis_espejo(rows: list) -> None:
    """rows: [{"mantis_id","proyecto_id","proyecto_nombre","creada",
    "actualizada","tecnico","categoria"}, ...] -- creada/actualizada ya en
    formato naive 'YYYY-MM-DD HH:MM' (ver mantis_iso_a_naive)."""
    if not rows:
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO mantis_espejo "
                "(mantis_id, proyecto_id, proyecto_nombre, creada, actualizada, tecnico, categoria) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE proyecto_id=VALUES(proyecto_id), "
                "proyecto_nombre=VALUES(proyecto_nombre), creada=VALUES(creada), "
                "actualizada=VALUES(actualizada), tecnico=VALUES(tecnico), categoria=VALUES(categoria)",
                [(r["mantis_id"], r["proyecto_id"], r["proyecto_nombre"], r["creada"], r["actualizada"],
                  r["tecnico"], r["categoria"]) for r in rows],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_mantis_espejo(desde: str, hasta: str) -> list:
    """Incidencias del espejo creadas en [desde,hasta] (fechas 'YYYY-MM-DD'),
    de cualquier proyecto -- el llamador decide cuales cuentan."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT mantis_id, proyecto_id, proyecto_nombre, creada, tecnico, categoria "
                "FROM mantis_espejo WHERE DATE(creada) BETWEEN %s AND %s ORDER BY creada",
                (desde, hasta),
            )
            cols = ["mantis_id", "proyecto_id", "proyecto_nombre", "creada", "tecnico", "categoria"]
            return [
                dict(zip(cols, [r[0], r[1], r[2] or "", r[3].strftime("%Y-%m-%d %H:%M") if r[3] else "",
                                 r[4] or "SIN ASIGNAR", r[5] or ""]))
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


# ── tableros globales Kanban/Scrum (paginas /kanban/ y /scrum/) ─────────────
# Igual que core/views.py::board_kanban_global/board_scrum_global/
# _render_global_board/_global_board_columns -- version de solo lectura
# (sin drag-and-drop, sin crear/editar tarea) que junta las tareas de TODOS
# los proyectos de un tipo en un unico tablero. El tablero por-proyecto
# editable (con columnas propias, arrastrar tarjetas, etc.) es una pagina
# aparte, mas grande, que queda pendiente -- ver README.

def load_proyectos_por_tipo(tipo: str) -> list:
    cond, params = _ver_por_espacio("espacio_id")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, nombre, color FROM proyectos WHERE tipo=%s AND activo=TRUE AND {cond} ORDER BY orden",
                (tipo, *params),
            )
            return [{"id": r[0], "nombre": r[1], "color": r[2]} for r in cur.fetchall()]
    finally:
        conn.close()


def board_columns_union(proyecto_ids: list) -> list:
    """Union de las columnas reales de varios proyectos (por nombre, la
    primera que se ve gana) -- para que una columna personalizada de un
    proyecto no desaparezca del tablero global."""
    vistas = {}
    for pid in proyecto_ids:
        for c in load_board_columns(pid):
            if c["nombre"] not in vistas:
                vistas[c["nombre"]] = c
    return sorted(vistas.values(), key=lambda c: (c["es_done"], c["orden"]))


def load_tasks_por_proyectos(proyecto_ids: list, borrada: bool, query: str = "") -> list:
    if not proyecto_ids:
        return []
    conn = connect()
    try:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(proyecto_ids))
            sql = (
                "SELECT t.id, t.proyecto_id, c.nombre, t.titulo, t.estado, t.descripcion, "
                "t.proximo_paso, t.prioridad, t.orden "
                "FROM tareas t LEFT JOIN categorias c ON c.id = t.categoria_id "
                f"WHERE t.proyecto_id IN ({placeholders}) AND t.borrada=%s"
            )
            params = list(proyecto_ids) + [borrada]
            cond, extra = _ver_proyecto("t.proyecto_id")
            sql += f" AND {cond}"
            params += extra
            if query:
                like = f"%{query}%"
                sql += " AND (t.titulo LIKE %s OR t.descripcion LIKE %s OR t.proximo_paso LIKE %s OR c.nombre LIKE %s)"
                params += [like, like, like, like]
            cur.execute(sql, params)
            cols = ["task_id", "proyecto_id", "category", "title", "status", "description",
                     "next_step", "priority", "orden"]
            return [
                dict(zip(cols, [r[0], r[1], _nn(r[2]), r[3], r[4], _nn(r[5]), _nn(r[6]), r[7], r[8]]))
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


# ── proyectos (usado por Wiki y por el sidebar) ─────────────────────────────

def get_proyecto(project_id: str):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT p.id, p.nombre, p.descripcion, p.color, p.responsable, p.estado, p.tipo, "
                "p.fecha_inicio, p.fecha_fin, p.requiere_subtareas_completas, p.espacio_id, e.nombre "
                "FROM proyectos p LEFT JOIN espacios e ON e.id = p.espacio_id WHERE p.id=%s", (project_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0], "nombre": row[1], "descripcion": _nn(row[2]), "color": row[3],
                "responsable": _nn(row[4]), "estado": row[5], "tipo": row[6],
                "fecha_inicio": _fmt_date(row[7]), "fecha_fin": _fmt_date(row[8]),
                "requiere_subtareas_completas": bool(row[9]), "espacio_id": _nn(row[10]),
                # LEFT JOIN a espacios -- para el breadcrumb "Espacios > X > proyecto"
                # de las paginas de proyecto (tablero.html y demas), sin gastar una
                # consulta aparte por pagina (ver CLAUDE.md).
                "espacio_nombre": _nn(row[11]),
            }
    finally:
        conn.close()


def load_proyectos_sidebar() -> list:
    """Igual que las listas kanban/scrum del sidebar de la app real -- se
    usa como global de Jinja2 (ver templating.py) para no tener que meterla
    a mano en el contexto de cada router, es la misma lista en todas las
    paginas."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cond, params = _ver_por_espacio("espacio_id")
            cur.execute(f"SELECT id, nombre, color, tipo FROM proyectos WHERE activo=TRUE AND {cond} ORDER BY orden",
                        params or None)
            return [{"id": r[0], "nombre": r[1], "color": r[2], "tipo": r[3]} for r in cur.fetchall()]
    finally:
        conn.close()


def crear_proyecto(project_id: str, nombre: str, descripcion: str, responsable: str, color: str,
                    estado: str, tipo: str, fecha_inicio: str, fecha_fin: str,
                    requiere_subtareas_completas: bool = False, espacio_id: str = "") -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(orden), 0) FROM proyectos")
            orden = cur.fetchone()[0] + 1
            cur.execute(
                "INSERT INTO proyectos (id, nombre, descripcion, responsable, color, estado, tipo, "
                "fecha_inicio, fecha_fin, orden, activo, requiere_subtareas_completas, espacio_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s)",
                (project_id, nombre, descripcion, responsable, color, estado, tipo,
                 fecha_inicio or None, fecha_fin or None, orden, requiere_subtareas_completas,
                 espacio_id or None),
            )
            _nexus_crear_nodo(cur, "proyecto", project_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def editar_proyecto(project_id: str, nombre: str, descripcion: str, responsable: str, color: str,
                     estado: str, tipo: str, fecha_inicio: str, fecha_fin: str,
                     requiere_subtareas_completas: bool = None, espacio_id: str = None) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            if requiere_subtareas_completas is None:
                cur.execute("SELECT requiere_subtareas_completas FROM proyectos WHERE id=%s", (project_id,))
                row = cur.fetchone()
                requiere_subtareas_completas = bool(row[0]) if row else False
            if espacio_id is None:
                cur.execute("SELECT espacio_id FROM proyectos WHERE id=%s", (project_id,))
                row = cur.fetchone()
                espacio_id = row[0] if row else None
            cur.execute(
                "UPDATE proyectos SET nombre=%s, descripcion=%s, responsable=%s, color=%s, estado=%s, "
                "tipo=%s, fecha_inicio=%s, fecha_fin=%s, requiere_subtareas_completas=%s, espacio_id=%s "
                "WHERE id=%s",
                (nombre, descripcion, responsable, color, estado, tipo,
                 fecha_inicio or None, fecha_fin or None, requiere_subtareas_completas,
                 espacio_id or None, project_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def eliminar_proyecto(project_id: str) -> bool:
    """Borra un proyecto entero -- igual que core/views.py::project_delete.
    'general' es intocable (False sin hacer nada, igual que el original).
    Las tareas del proyecto NO se borran: se reasignan a 'general' (con
    estado='OUTCAST' si el nombre de su columna no existe entre las
    columnas de 'general', para que no queden invisibles del todo en
    ningun tablero -- mismo tratamiento que al borrar una columna). La
    Wiki tampoco se borra, mismo criterio: un proyecto es solo una
    agrupacion de tareas, no el dueño de la informacion -- sus paginas se
    reasignan a 'general' igual que las tareas, EN VEZ de dejar que el
    ON DELETE CASCADE hacia proyectos(id) se las lleve por delante (asi era
    antes: borrar un proyecto borraba sin avisar toda su documentacion).
    Hitos y sprints propios si se borran explicitamente (no tienen sentido
    fuera de su proyecto); documentos propios tambien (fichero fisico
    primero, fila despues). columnas_tablero/api_tokens del proyecto
    desaparecen por el ON DELETE CASCADE que ya tiene el esquema hacia
    proyectos(id) al borrar esa fila -- esos si son propios de CADA
    proyecto (columnas de SU tablero), no informacion que deba sobrevivir."""
    if project_id == "general":
        return False
    general_cols = {c["nombre"] for c in load_board_columns("general")}
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, estado FROM tareas WHERE proyecto_id=%s", (project_id,))
            for task_id, estado in cur.fetchall():
                nuevo_estado = estado if estado in general_cols else "OUTCAST"
                cur.execute(
                    "UPDATE tareas SET proyecto_id='general', estado=%s WHERE id=%s",
                    (nuevo_estado, task_id),
                )
            cur.execute("UPDATE wiki_paginas SET proyecto_id='general' WHERE proyecto_id=%s", (project_id,))
            cur.execute("DELETE FROM hitos WHERE proyecto_id=%s", (project_id,))
            cur.execute("DELETE FROM sprints WHERE proyecto_id=%s", (project_id,))
            cur.execute("SELECT filename FROM documentos WHERE proyecto_id=%s", (project_id,))
            archivos = [r[0] for r in cur.fetchall()]
            cur.execute("DELETE FROM documentos WHERE proyecto_id=%s", (project_id,))
            _nexus_eliminar_nodo(cur, "proyecto", project_id)
            cur.execute("DELETE FROM proyectos WHERE id=%s", (project_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    for filename in archivos:
        p = media_dir() / filename
        if p.exists():
            p.unlink()
    return True


def duplicar_proyecto(project_id: str, nombre: str, copiar_tareas: str, fechas: str,
                       copiar_hitos: bool, crear_sprint_flag: bool) -> str:
    """Igual que core/views.py::project_duplicate (POST): crea un proyecto
    nuevo a partir de uno existente. copiar_tareas: 'pendientes'/'todas'/
    'ninguna' (las copiadas siempre nacen en estado 'Pendiente', igual que el
    original -- no se intenta mapear la columna vieja a una nueva). fechas:
    'limpiar'/'mantener' -- aunque se elija 'mantener', una fecha ya vencida
    siempre se limpia (no tiene sentido arrastrar un vencimiento pasado a un
    proyecto que se acaba de crear). Ni empresa/sistema/iniciativa/subtareas/
    tiempo/anclada/mantis_id se copian -- son detalles de ESA tarea concreta,
    no algo que tenga sentido duplicar sin mas. Devuelve el id del proyecto
    nuevo, o "" si el original no existe."""
    original = get_proyecto(project_id)
    if not original:
        return ""
    nuevo_id = str(uuid.uuid4())[:8]
    crear_proyecto(nuevo_id, nombre, original["descripcion"], original["responsable"],
                   original["color"], "Activo", original["tipo"], "", "",
                   requiere_subtareas_completas=original["requiere_subtareas_completas"])

    nuevo_sprint_id = ""
    if crear_sprint_flag and original["tipo"] == "scrum":
        hoy = datetime.now().strftime("%Y-%m-%d")
        fin = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
        nuevo_sprint_id = crear_sprint(nuevo_id, "Sprint 1", hoy, fin, "activo", "")

    if copiar_tareas != "ninguna":
        orig_tasks = load_tasks_para_tablero(project_id, borrada=False)
        if copiar_tareas == "pendientes":
            done_names = done_status_names_by_project().get(project_id, {"Hecha"})
            orig_tasks = [t for t in orig_tasks if t["status"] not in done_names]
        hoy = datetime.now().strftime("%Y-%m-%d")
        for t in orig_tasks:
            fi = t["fecha_inicio"] if fechas == "mantener" else ""
            ff = t["fecha_fin"] if fechas == "mantener" else ""
            if fi and fi < hoy:
                fi = ""
            if ff and ff < hoy:
                ff = ""
            crear_tarea(
                str(uuid.uuid4())[:8], nuevo_id, t["category"], t["title"], "Pendiente",
                t["priority"], t["description"], t["next_step"], fi, ff,
                sprint_id=nuevo_sprint_id,
            )

    if copiar_hitos:
        for m in load_hitos_por_proyecto(project_id):
            crear_hito(nuevo_id, m["nombre"], "" if fechas == "limpiar" else m["fecha"], False)

    return nuevo_id


# ── tablero por proyecto (columnas + tarjetas, editable) ────────────────────
# Igual que core/views.py::board/_render_board/column_new/column_rename/
# column_move/column_delete/task_reorder. La resolucion/filtrado por sprint
# de los proyectos 'scrum' vive en el router (tablero.py::ver_tablero), igual
# que en el original -- ver mas abajo la seccion "sprints (Scrum)".

def load_tasks_para_tablero(proyecto_id: str, borrada: bool) -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, c.nombre, t.titulo, t.estado, t.prioridad, t.orden, t.descripcion, t.proximo_paso, "
                "t.fecha_inicio, t.fecha_fin, t.sprint_id, t.empresa_id, t.sistema_id, t.iniciativa_id, t.anclada, "
                "t.subtareas, t.tiempo_estimado, t.tiempo_real, t.creada_en, t.actualizada_en, t.mantis_id, "
                "t.generada_por_ia, t.asignado_a, u.username, u.first_name, u.last_name "
                "FROM tareas t LEFT JOIN categorias c ON c.id = t.categoria_id "
                "LEFT JOIN auth_user u ON u.id = t.asignado_a "
                "WHERE t.proyecto_id=%s AND t.borrada=%s ORDER BY t.orden, t.titulo",
                (proyecto_id, borrada),
            )
            resultado = []
            for r in cur.fetchall():
                subt_count = subt_done = 0
                if r[15]:
                    try:
                        subs = json.loads(r[15])
                        subt_count = len(subs)
                        subt_done = sum(1 for s in subs if s.get("d") or s.get("checked"))
                    except Exception:
                        pass
                resultado.append({
                    "task_id": r[0], "category": _nn(r[1]), "title": r[2], "status": r[3], "priority": r[4],
                    "orden": r[5], "description": _nn(r[6]), "next_step": _nn(r[7]),
                    "fecha_inicio": _fmt_date(r[8]), "fecha_fin": _fmt_date(r[9]), "sprint_id": _nn(r[10]),
                    "empresa_id": _nn(r[11]), "sistema_id": _nn(r[12]), "iniciativa_id": _nn(r[13]),
                    "anclada": bool(r[14]), "subtareas_raw": _nn(r[15]),
                    "subtareas_count": subt_count, "subtareas_done": subt_done,
                    "tiempo_estimado": r[16], "tiempo_real": r[17],
                    "creada_en": _fmt_datetime(r[18]), "actualizada_en": _fmt_datetime(r[19]),
                    "mantis_id": r[20], "generada_por_ia": bool(r[21]),
                    "asignado_a": r[22],
                    "asignado_a_nombre": (f"{r[24]} {r[25]}".strip() or r[23]) if r[22] else "",
                })
            return resultado
    finally:
        conn.close()


def load_tareas_vivo_wiki(proyecto_id: str, solo_pendientes: bool = True) -> list:
    """Tareas de UN proyecto para el bloque "vivo" de la Wiki (ver
    wiki_store.render_content, tipo 'tareas_vivas') -- la Wiki no copia
    datos de las tareas, los consulta en el momento de renderizar la
    pagina, para no quedar desactualizada. Version minima de
    load_tasks_para_tablero (solo id/titulo/estado, lo que se pinta en la
    lista), no su detalle completo."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, titulo, estado FROM tareas WHERE proyecto_id=%s AND borrada=FALSE "
                "ORDER BY orden, titulo",
                (proyecto_id,),
            )
            tareas = [{"task_id": r[0], "title": r[1], "status": r[2]} for r in cur.fetchall()]
            if solo_pendientes:
                cur.execute(
                    "SELECT nombre FROM columnas_tablero WHERE proyecto_id=%s AND es_done=TRUE", (proyecto_id,)
                )
                done_names = {r[0] for r in cur.fetchall()}
                tareas = [t for t in tareas if t["status"] not in done_names]
            return tareas
    finally:
        conn.close()


def toggle_borrada_tarea(task_id: str, borrada: bool) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE tareas SET borrada=%s, actualizada_en=%s WHERE id=%s", (borrada, now_text(), task_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def borrar_tareas_bulk(task_ids: list) -> int:
    """Borrado logico en bloque -- igual que core/views.py::task_bulk_delete
    (ids inexistentes se ignoran en silencio), pero en una sola query en vez
    de recorrer los ids en Python. **Diferencia deliberada**: devuelve el
    numero de filas realmente afectadas (`cur.rowcount`); el original
    devuelve `len(ids)` tal cual (el tamano del set recibido, sobre-reporta
    si se mandan ids que ya no existen) -- la UI de ningun lado de esta v2
    ni de la real llega a mostrar ese numero, asi que es un detalle sin
    efecto visible, solo mas honesto."""
    if not task_ids:
        return 0
    conn = connect()
    try:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(task_ids))
            cur.execute(
                f"UPDATE tareas SET borrada=1, actualizada_en=%s WHERE id IN ({placeholders})",
                [now_text()] + list(task_ids),
            )
            afectadas = cur.rowcount
        conn.commit()
        return afectadas
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reordenar_tarea(task_id: str, nuevo_status: str, before_id: str) -> bool:
    """Igual que core/views.py::task_reorder: reordena la columna destino
    completa (1..N) con la tarea insertada en su nueva posicion, y actualiza
    estado/iniciada_en/cerrada_en si cambio de columna."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT proyecto_id, estado, iniciada_en, cerrada_en FROM tareas WHERE id=%s AND borrada=0", (task_id,))
            row = cur.fetchone()
            if not row:
                return False
            proyecto_id, old_status, iniciada_en, cerrada_en = row

            cur.execute("SELECT 1 FROM columnas_tablero WHERE proyecto_id=%s AND nombre=%s", (proyecto_id, nuevo_status))
            if not cur.fetchone():
                return False

            cur.execute(
                "SELECT id FROM tareas WHERE proyecto_id=%s AND estado=%s AND borrada=0 AND id != %s ORDER BY orden",
                (proyecto_id, nuevo_status, task_id),
            )
            ids = [r[0] for r in cur.fetchall()]
            if before_id and before_id in ids:
                ids.insert(ids.index(before_id), task_id)
            else:
                ids.append(task_id)
            for i, tid in enumerate(ids, start=1):
                cur.execute("UPDATE tareas SET orden=%s WHERE id=%s", (i, tid))

            extra_sql, extra_params = "", []
            if nuevo_status != old_status:
                if not iniciada_en:
                    extra_sql += ", iniciada_en=%s"
                    extra_params.append(now_text())
                if is_done_column(proyecto_id, nuevo_status):
                    if not cerrada_en:
                        extra_sql += ", cerrada_en=%s"
                        extra_params.append(now_text())
                else:
                    extra_sql += ", cerrada_en=NULL"
            cur.execute(
                f"UPDATE tareas SET estado=%s, actualizada_en=%s{extra_sql} WHERE id=%s",
                [nuevo_status, now_text()] + extra_params + [task_id],
            )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def crear_columna(proyecto_id: str, nombre: str, color: str, es_done: bool) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(orden), 0) FROM columnas_tablero WHERE proyecto_id=%s", (proyecto_id,))
            orden = cur.fetchone()[0] + 1
            cur.execute(
                "INSERT INTO columnas_tablero (id, proyecto_id, nombre, orden, es_done, color) VALUES (%s,%s,%s,%s,%s,%s)",
                (str(uuid.uuid4())[:8], proyecto_id, nombre, orden, es_done, color),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def renombrar_columna(col_id: str, nombre: str, color: str, es_done: bool, max_tareas: int) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT proyecto_id, nombre FROM columnas_tablero WHERE id=%s", (col_id,))
            row = cur.fetchone()
            if not row:
                return
            proyecto_id, nombre_anterior = row
            cur.execute(
                "UPDATE columnas_tablero SET nombre=%s, color=%s, es_done=%s, max_tareas=%s WHERE id=%s",
                (nombre, color, es_done, max_tareas, col_id),
            )
            if nombre != nombre_anterior:
                # las tareas que estaban en esa columna la siguen (por nombre,
                # igual que la app real) para no dejarlas huerfanas.
                cur.execute(
                    "UPDATE tareas SET estado=%s WHERE proyecto_id=%s AND estado=%s",
                    (nombre, proyecto_id, nombre_anterior),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mover_columna(col_id: str, direction: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT proyecto_id, orden FROM columnas_tablero WHERE id=%s", (col_id,))
            row = cur.fetchone()
            if not row:
                return
            proyecto_id, orden = row
            cur.execute(
                "SELECT id, orden FROM columnas_tablero WHERE proyecto_id=%s ORDER BY orden", (proyecto_id,),
            )
            columnas = cur.fetchall()
            idx = next((i for i, c in enumerate(columnas) if c[0] == col_id), None)
            if idx is None:
                return
            vecino_idx = idx - 1 if direction == "left" else idx + 1
            if vecino_idx < 0 or vecino_idx >= len(columnas):
                return
            vecino_id, vecino_orden = columnas[vecino_idx]
            cur.execute("UPDATE columnas_tablero SET orden=%s WHERE id=%s", (vecino_orden, col_id))
            cur.execute("UPDATE columnas_tablero SET orden=%s WHERE id=%s", (orden, vecino_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def eliminar_columna(col_id: str) -> None:
    """Las tareas que estaban en esa columna pasan al estado sentinela
    'OUTCAST' (no se borran, pero dejan de verse en ninguna columna real) --
    igual que core/views.py::column_delete."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT proyecto_id, nombre FROM columnas_tablero WHERE id=%s", (col_id,))
            row = cur.fetchone()
            if row:
                proyecto_id, nombre = row
                cur.execute(
                    "UPDATE tareas SET estado='OUTCAST' WHERE proyecto_id=%s AND estado=%s", (proyecto_id, nombre),
                )
            cur.execute("DELETE FROM columnas_tablero WHERE id=%s", (col_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── sprints (Scrum) ──────────────────────────────────────────────────────────
# Igual que core/views.py::project_sprints/sprint_new/sprint_edit/sprint_close/
# sprint_add_task/sprint_add_tasks_bulk/sprint_remove_task +
# core/models.py::Sprint/SPRINT_STATUSES/SPRINT_STATUS_COLORS.

SPRINT_STATUSES = ["planificado", "activo", "cerrado"]
SPRINT_STATUS_COLORS = {"planificado": "#6b7280", "activo": "#2563eb", "cerrado": "#16a34a"}


def load_sprints(proyecto_id: str) -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, nombre, numero, fecha_inicio, fecha_fin, estado, objetivo "
                "FROM sprints WHERE proyecto_id=%s ORDER BY numero DESC",
                (proyecto_id,),
            )
            return [
                {
                    "sprint_id": r[0], "nombre": r[1], "numero": r[2],
                    "fecha_inicio": _fmt_date(r[3]), "fecha_fin": _fmt_date(r[4]),
                    "estado": r[5], "objetivo": _nn(r[6]),
                    "estado_color": SPRINT_STATUS_COLORS.get(r[5], "#6b7280"),
                    "duracion_dias": (r[4] - r[3]).days if r[3] and r[4] else 0,
                }
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def get_sprint(sprint_id: str):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, proyecto_id, nombre, numero, fecha_inicio, fecha_fin, estado, objetivo "
                "FROM sprints WHERE id=%s",
                (sprint_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "sprint_id": row[0], "proyecto_id": row[1], "nombre": row[2], "numero": row[3],
                "fecha_inicio": _fmt_date(row[4]), "fecha_fin": _fmt_date(row[5]),
                "estado": row[6], "objetivo": _nn(row[7]),
            }
    finally:
        conn.close()


def sprint_counts_por_proyecto(proyecto_id: str, sprint_ids: list) -> dict:
    """{sprint_id: {total,pendiente,en_curso,hecha}} -- mismo criterio mixto
    (hecha = dinamico segun columnas es_done del proyecto, pendiente/en_curso
    = nombres de estado literales) que core/views.py::project_sprints, con el
    mismo aviso: si se renombran esas dos columnas el conteo de
    pendiente/en_curso deja de cuadrar (no afecta a 'hecha', que si es
    dinamico)."""
    if not sprint_ids:
        return {}
    done_names = {c["nombre"] for c in load_board_columns(proyecto_id) if c["es_done"]}
    counts = {sid: {"total": 0, "pendiente": 0, "en_curso": 0, "hecha": 0} for sid in sprint_ids}
    conn = connect()
    try:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(sprint_ids))
            cur.execute(
                f"SELECT sprint_id, estado FROM tareas WHERE sprint_id IN ({placeholders}) AND borrada=0",
                sprint_ids,
            )
            for sprint_id, estado in cur.fetchall():
                c = counts[sprint_id]
                c["total"] += 1
                if estado == "Pendiente":
                    c["pendiente"] += 1
                elif estado == "En curso":
                    c["en_curso"] += 1
                elif estado in done_names:
                    c["hecha"] += 1
    finally:
        conn.close()
    return counts


def crear_sprint(proyecto_id: str, nombre: str, fecha_inicio: str, fecha_fin: str, estado: str, objetivo: str) -> str:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(numero), 0) FROM sprints WHERE proyecto_id=%s", (proyecto_id,))
            numero = cur.fetchone()[0] + 1
            sprint_id = str(uuid.uuid4())[:8]
            cur.execute(
                "INSERT INTO sprints (id, proyecto_id, nombre, numero, fecha_inicio, fecha_fin, estado, objetivo) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (sprint_id, proyecto_id, nombre or f"Sprint {numero}", numero,
                 fecha_inicio or None, fecha_fin or None, estado or "planificado", objetivo),
            )
        conn.commit()
        return sprint_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def editar_sprint(sprint_id: str, nombre: str, fecha_inicio: str, fecha_fin: str, estado: str, objetivo: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sprints SET nombre=%s, fecha_inicio=%s, fecha_fin=%s, estado=%s, objetivo=%s WHERE id=%s",
                (nombre, fecha_inicio or None, fecha_fin or None, estado, objetivo, sprint_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cerrar_sprint(sprint_id: str):
    """Cierra el sprint y crea el siguiente automaticamente, arrastrando las
    tareas pendientes -- igual que core/views.py::sprint_close, incluida su
    lista fija (no dinamica) de estados que se arrastran ('Pendiente',
    'En curso', 'En espera': si el proyecto ha renombrado esas columnas las
    tareas que queden en la columna renombrada NO se arrastran -- es una
    inconsistencia heredada del original, no un bug introducido aqui).
    Devuelve el id del sprint nuevo, o None si sprint_id no existe."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT proyecto_id, numero, fecha_inicio, fecha_fin FROM sprints WHERE id=%s", (sprint_id,))
            row = cur.fetchone()
            if not row:
                return None
            proyecto_id, numero, fecha_inicio, fecha_fin = row
            cur.execute("UPDATE sprints SET estado='cerrado' WHERE id=%s", (sprint_id,))

            duracion = (fecha_fin - fecha_inicio).days if fecha_inicio and fecha_fin else 0
            duracion = duracion or 14
            hoy = datetime.now().date()
            fin = hoy + timedelta(days=duracion)
            numero_nuevo = numero + 1
            nuevo_id = str(uuid.uuid4())[:8]
            cur.execute(
                "INSERT INTO sprints (id, proyecto_id, nombre, numero, fecha_inicio, fecha_fin, estado, objetivo) "
                "VALUES (%s,%s,%s,%s,%s,%s,'activo','')",
                (nuevo_id, proyecto_id, f"Sprint {numero_nuevo}", numero_nuevo, hoy, fin),
            )

            cur.execute(
                "SELECT id, fecha_fin FROM tareas WHERE sprint_id=%s AND borrada=0 "
                "AND estado IN ('Pendiente','En curso','En espera')",
                (sprint_id,),
            )
            hoy_str = hoy.isoformat()
            for task_id, tarea_fecha_fin in cur.fetchall():
                if tarea_fecha_fin and tarea_fecha_fin.isoformat() < hoy_str:
                    cur.execute(
                        "UPDATE tareas SET sprint_id=%s, fecha_inicio=NULL, fecha_fin=NULL, actualizada_en=%s WHERE id=%s",
                        (nuevo_id, now_text(), task_id),
                    )
                else:
                    cur.execute(
                        "UPDATE tareas SET sprint_id=%s, actualizada_en=%s WHERE id=%s",
                        (nuevo_id, now_text(), task_id),
                    )
        conn.commit()
        return nuevo_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_tareas_sin_sprint(proyecto_id: str) -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.titulo, t.estado, c.nombre FROM tareas t LEFT JOIN categorias c ON c.id = t.categoria_id "
                "WHERE t.proyecto_id=%s AND t.borrada=0 AND t.sprint_id IS NULL ORDER BY t.orden, t.titulo",
                (proyecto_id,),
            )
            return [{"task_id": r[0], "title": r[1], "status": r[2], "category": _nn(r[3])} for r in cur.fetchall()]
    finally:
        conn.close()


def sprint_add_task(sprint_id: str, task_id: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE tareas SET sprint_id=%s, actualizada_en=%s WHERE id=%s", (sprint_id, now_text(), task_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sprint_add_tasks_bulk(sprint_id: str, task_ids: list) -> int:
    if not task_ids:
        return 0
    conn = connect()
    try:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(task_ids))
            cur.execute(
                f"UPDATE tareas SET sprint_id=%s, actualizada_en=%s WHERE id IN ({placeholders})",
                [sprint_id, now_text()] + task_ids,
            )
            added = cur.rowcount
        conn.commit()
        return added
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sprint_remove_task(sprint_id: str, task_id: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tareas SET sprint_id=NULL, actualizada_en=%s WHERE id=%s AND sprint_id=%s",
                (now_text(), task_id, sprint_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── whiteboards (pizarras tipo Paint, reeditables, globales) ────────────────
# Igual que core/db_store.py::TaskStoreDB.load_whiteboards/get_whiteboard/
# create_whiteboard/update_whiteboard/delete_whiteboard -- CRUD por fila (no
# borrar-y-reinsertar), misma tabla `whiteboards` compartida con la app real.

def load_whiteboards() -> list:
    """Para la galeria: sin 'contenido' (puede ser JSON grande), solo lo que
    hace falta para pintar cada tarjeta. reunion_id/agenda_fecha se usan
    para filtrar 'whiteboards_disponibles' en /reunion/<id>/ y
    /agenda/<fecha>/ (solo pizarras sueltas, sin vincular a ninguno)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, titulo, thumbnail, version_hash, reunion_id, agenda_fecha, creada_en, actualizada_en "
                "FROM whiteboards ORDER BY actualizada_en DESC"
            )
            return [
                {
                    "whiteboard_id": r[0], "titulo": r[1], "thumbnail": _nn(r[2]),
                    "version_hash": _nn(r[3]), "reunion_id": _nn(r[4]), "agenda_fecha": _fmt_date(r[5]),
                    "creada_en": _fmt_datetime(r[6]), "actualizada_en": _fmt_datetime(r[7]),
                }
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def get_whiteboard(whiteboard_id: str):
    """Para el editor: aqui si hace falta 'contenido' completo."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, titulo, contenido, thumbnail, version_hash, reunion_id, agenda_fecha, "
                "creada_en, actualizada_en FROM whiteboards WHERE id=%s",
                (whiteboard_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "whiteboard_id": row[0], "titulo": row[1], "contenido": _nn(row[2]),
                "thumbnail": _nn(row[3]), "version_hash": _nn(row[4]), "reunion_id": _nn(row[5]),
                "agenda_fecha": _fmt_date(row[6]),
                "creada_en": _fmt_datetime(row[7]), "actualizada_en": _fmt_datetime(row[8]),
            }
    finally:
        conn.close()


def create_whiteboard(titulo: str, contenido: str, thumbnail: str, version_hash: str, reunion_id: str = "",
                       agenda_fecha: str = "") -> str:
    conn = connect()
    try:
        whiteboard_id = str(uuid.uuid4())[:8]
        now = now_text()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO whiteboards (id, titulo, contenido, thumbnail, version_hash, reunion_id, "
                "agenda_fecha, creada_en, actualizada_en) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (whiteboard_id, titulo, contenido, thumbnail, version_hash, reunion_id or None,
                 agenda_fecha or None, now, now),
            )
            _nexus_crear_nodo(cur, "whiteboard", whiteboard_id)
        conn.commit()
        return whiteboard_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_whiteboard_by_reunion(reunion_id: str):
    """Pizarra vinculada a una reunion (a lo sumo una) -- para el hub
    /reunion/<id>/, igual que core/db_store.py::get_whiteboard_by_reunion."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, titulo, thumbnail, version_hash, reunion_id, creada_en, actualizada_en "
                "FROM whiteboards WHERE reunion_id=%s",
                (reunion_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "whiteboard_id": row[0], "titulo": row[1], "thumbnail": _nn(row[2]),
                "version_hash": _nn(row[3]), "reunion_id": _nn(row[4]),
                "creada_en": _fmt_datetime(row[5]), "actualizada_en": _fmt_datetime(row[6]),
            }
    finally:
        conn.close()


def get_whiteboard_by_agenda_fecha(fecha: str):
    """Pizarra vinculada a un dia de la Agenda (a lo sumo una) -- para el
    hub /agenda/<fecha>/, mismo patron que get_whiteboard_by_reunion."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, titulo, thumbnail, version_hash, agenda_fecha, creada_en, actualizada_en "
                "FROM whiteboards WHERE agenda_fecha=%s",
                (fecha,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "whiteboard_id": row[0], "titulo": row[1], "thumbnail": _nn(row[2]),
                "version_hash": _nn(row[3]), "agenda_fecha": _fmt_date(row[4]),
                "creada_en": _fmt_datetime(row[5]), "actualizada_en": _fmt_datetime(row[6]),
            }
    finally:
        conn.close()


def set_whiteboard_reunion(whiteboard_id: str, reunion_id: str) -> None:
    """Vincula (o desvincula, con reunion_id='') una pizarra ya existente a
    una reunion -- para "usar una pizarra ya creada" desde el hub de la
    reunion, sin pasar por whiteboard_save."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE whiteboards SET reunion_id=%s WHERE id=%s", (reunion_id or None, whiteboard_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_whiteboard_agenda_fecha(whiteboard_id: str, fecha: str) -> None:
    """Vincula (o desvincula, con fecha='') una pizarra ya existente a un
    dia de la Agenda -- mismo patron que set_whiteboard_reunion."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE whiteboards SET agenda_fecha=%s WHERE id=%s", (fecha or None, whiteboard_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_whiteboard(whiteboard_id: str, titulo: str, contenido: str, thumbnail: str, version_hash: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE whiteboards SET titulo=%s, contenido=%s, thumbnail=%s, version_hash=%s, actualizada_en=%s "
                "WHERE id=%s",
                (titulo, contenido, thumbnail, version_hash, now_text(), whiteboard_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── calendario_sync_config + reuniones_calendario (feed ICS -> hub /reunion/<id>/) ──
# Igual que core/db_store.py: config por usuario (solo la URL del feed ICS,
# la sincronizacion en si vive en core/calendario_sync_client.py, reutilizado
# directo -- ver routers/reunion.py) + las reuniones ya sincronizadas.

def get_calendario_sync_config(usuario: str):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT usuario, ics_url, creado_en, actualizado_en FROM calendario_sync_config WHERE usuario=%s",
                (usuario,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {"usuario": row[0], "ics_url": _nn(row[1]),
                    "creado_en": _fmt_datetime(row[2]), "actualizado_en": _fmt_datetime(row[3])}
    finally:
        conn.close()


def save_calendario_sync_config(usuario: str, ics_url: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT usuario FROM calendario_sync_config WHERE usuario=%s", (usuario,))
            if cur.fetchone():
                cur.execute("UPDATE calendario_sync_config SET ics_url=%s WHERE usuario=%s", (ics_url, usuario))
            else:
                cur.execute(
                    "INSERT INTO calendario_sync_config (usuario, ics_url) VALUES (%s,%s)",
                    (usuario, ics_url),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _reunion_id(usuario: str, uid_ics: str, fecha_inicio) -> str:
    """hash en vez del UID tal cual como PK (los UID de Exchange pueden ser
    muy largos, ver CREATE TABLE) -- incluye fecha_inicio porque
    core.calendario_sync_client expande reuniones recurrentes (RRULE) y
    TODAS las ocurrencias de una misma serie comparten el mismo UID; sin la
    fecha en el hash, cada sincronizacion machacaria unas ocurrencias con
    otras y solo quedaria una fila por serie."""
    return hashlib.sha256(f"{usuario}:{uid_ics}:{fecha_inicio.isoformat()}".encode()).hexdigest()


def upsert_reuniones_calendario(usuario: str, eventos: list) -> None:
    """eventos: dicts con uid/titulo/fecha_inicio/fecha_fin/ubicacion/
    enlace_teams, tal como devuelve core.calendario_sync_client.fetch_eventos."""
    if not eventos:
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO reuniones_calendario "
                "(id, usuario, uid_ics, titulo, fecha_inicio, fecha_fin, ubicacion, enlace_teams) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE "
                "titulo=VALUES(titulo), fecha_inicio=VALUES(fecha_inicio), fecha_fin=VALUES(fecha_fin), "
                "ubicacion=VALUES(ubicacion), enlace_teams=VALUES(enlace_teams), "
                "sincronizada_en=CURRENT_TIMESTAMP",
                [(_reunion_id(usuario, e["uid"], e["fecha_inicio"]), usuario, e["uid"], e["titulo"],
                  e["fecha_inicio"], e["fecha_fin"], e["ubicacion"], e["enlace_teams"])
                 for e in eventos],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_reunion_calendario(reunion_id: str):
    """Una sola reunion por id -- para /reunion/<id>/ (hub de pizarra+tareas)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, usuario, titulo, fecha_inicio, fecha_fin, ubicacion, enlace_teams "
                "FROM reuniones_calendario WHERE id=%s",
                (reunion_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {"reunion_id": row[0], "usuario": row[1], "titulo": row[2],
                    "fecha_inicio": _fmt_datetime(row[3]), "fecha_fin": _fmt_datetime(row[4]),
                    "ubicacion": _nn(row[5]), "enlace_teams": _nn(row[6])}
    finally:
        conn.close()


def load_reuniones_calendario(usuario: str, fecha_inicio: str, fecha_fin: str) -> list:
    """Reuniones del usuario cuyo inicio cae en [fecha_inicio, fecha_fin]
    (strings 'YYYY-MM-DD') -- acotado igual que load_tasks_by_due_date, para
    el Calendario."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, usuario, titulo, fecha_inicio, fecha_fin, ubicacion, enlace_teams "
                "FROM reuniones_calendario "
                "WHERE usuario=%s AND DATE(fecha_inicio) BETWEEN %s AND %s "
                "ORDER BY fecha_inicio",
                (usuario, fecha_inicio, fecha_fin),
            )
            return [
                {"reunion_id": r[0], "usuario": r[1], "titulo": r[2],
                 "fecha_inicio": _fmt_datetime(r[3]), "fecha_fin": _fmt_datetime(r[4]),
                 "ubicacion": _nn(r[5]), "enlace_teams": _nn(r[6])}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def load_tasks_by_reunion(reunion_id: str) -> list:
    """Tareas no borradas etiquetadas a una reunion sincronizada -- solo los
    campos que necesita el mini-listado de /reunion/<id>/ (mas ligero que el
    SELECT completo de tareas que usa el Tablero)."""
    cond, extra = _ver_proyecto("proyecto_id")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, proyecto_id, titulo, estado FROM tareas WHERE borrada = 0 AND reunion_id = %s AND {cond}",
                (reunion_id, *extra),
            )
            return [{"task_id": r[0], "proyecto_id": r[1], "title": r[2], "status": r[3]} for r in cur.fetchall()]
    finally:
        conn.close()


def delete_whiteboard(whiteboard_id: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            _nexus_eliminar_nodo(cur, "whiteboard", whiteboard_id)
            cur.execute("DELETE FROM whiteboards WHERE id=%s", (whiteboard_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── hitos (milestones) + sidebar agrupado por Kanban/Scrum ──────────────────
# Igual que core/db_store.py (hitos como parte del ciclo bulk load/save de
# TaskStoreDB) + core/views.py::milestone_new/milestone_edit/base_context's
# kanban_projects/scrum_projects -- aqui, CRUD por fila (mismo patron que el
# resto de esta v2). Sin borrado individual: el original tampoco lo tiene (un
# hito solo desaparece al borrar el proyecto entero, o -- en el original --
# al usar "duplicar proyecto", que aqui no esta portado todavia).

def load_sidebar_projects() -> list:
    """Proyectos activos agrupados por Espacio (no por tipo) para el sidebar
    -- ver CLAUDE.md, seccion Espacios. Cada proyecto lleva su tipo
    (kanban/scrum) para que la plantilla pinte un icono junto al nombre en
    vez de agrupar por eso, mas sus hitos y (los Scrum) su sprint activo --
    misma idea que core/views.py::base_context's kanban_projects/
    scrum_projects + core/templates/base.html en cuanto a filtrar
    activo=TRUE y ordenar por 'orden'. Un proyecto sin espacio_id cae en un
    grupo "Sin espacio" al final, nunca desaparece del sidebar."""
    cond, params = _ver_por_espacio("espacio_id")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, nombre, color, tipo, requiere_subtareas_completas, espacio_id "
                f"FROM proyectos WHERE activo=TRUE AND {cond} ORDER BY orden",
                params or None,
            )
            proyectos = [
                {"id": r[0], "nombre": r[1], "color": r[2], "tipo": r[3],
                 "requiere_subtareas_completas": bool(r[4]), "espacio_id": _nn(r[5]),
                 "milestones": [], "sprint_activo": None}
                for r in cur.fetchall()
            ]
            if not proyectos:
                return []
            ids = [p["id"] for p in proyectos]
            placeholders = ",".join(["%s"] * len(ids))

            cur.execute(
                f"SELECT id, proyecto_id, nombre, fecha, completado FROM hitos WHERE proyecto_id IN ({placeholders})",
                ids,
            )
            hitos_by_proyecto = {}
            for r in cur.fetchall():
                hitos_by_proyecto.setdefault(r[1], []).append(
                    {"milestone_id": r[0], "nombre": r[2], "fecha": _fmt_date(r[3]), "completado": bool(r[4])}
                )

            cur.execute(
                f"SELECT proyecto_id, id, nombre FROM sprints WHERE proyecto_id IN ({placeholders}) AND estado='activo'",
                ids,
            )
            sprint_activo_by_proyecto = {r[0]: {"sprint_id": r[1], "nombre": r[2]} for r in cur.fetchall()}

            cur.execute(
                "SELECT id, nombre, es_personal FROM espacios WHERE activo=TRUE "
                "ORDER BY es_personal DESC, orden, nombre"
            )
            espacios = [{"id": r[0], "nombre": r[1], "es_personal": bool(r[2])} for r in cur.fetchall()]
    finally:
        conn.close()

    for p in proyectos:
        p["milestones"] = hitos_by_proyecto.get(p["id"], [])
        if p["tipo"] == "scrum":
            p["sprint_activo"] = sprint_activo_by_proyecto.get(p["id"])

    proyectos_por_espacio: dict = {}
    for p in proyectos:
        proyectos_por_espacio.setdefault(p["espacio_id"], []).append(p)

    grupos = []
    for e in espacios:
        ps = proyectos_por_espacio.get(e["id"])
        if ps:
            grupos.append({"id": e["id"], "nombre": e["nombre"], "es_personal": e["es_personal"], "proyectos": ps})
    sin_espacio = proyectos_por_espacio.get(None)
    if sin_espacio:
        grupos.append({"id": None, "nombre": "Sin espacio", "es_personal": False, "proyectos": sin_espacio})
    return grupos


def load_dashboard_data(proyecto_id: str) -> list:
    """Tareas de un proyecto con los campos que necesita Dashboard --
    parecido a load_tasks_para_tablero, pero incluye cerrada_en (para el
    burndown), que el tablero no usa."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.titulo, t.estado, c.nombre, t.prioridad, t.fecha_fin, t.sprint_id, "
                "t.creada_en, t.cerrada_en "
                "FROM tareas t LEFT JOIN categorias c ON c.id = t.categoria_id "
                "WHERE t.proyecto_id=%s AND t.borrada=0",
                (proyecto_id,),
            )
            return [
                {
                    "task_id": r[0], "title": r[1], "status": r[2], "category": _nn(r[3]), "priority": r[4],
                    "fecha_fin": _fmt_date(r[5]), "sprint_id": _nn(r[6]),
                    "creada_en": _fmt_datetime(r[7]), "cerrada_en": _fmt_datetime(r[8]),
                }
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def load_hitos_por_proyecto(proyecto_id: str) -> list:
    """Hitos de UN proyecto (para Resumen) -- distinto de
    load_sidebar_projects(), que trae los de varios proyectos a la vez para
    el sidebar."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, nombre, fecha, completado FROM hitos WHERE proyecto_id=%s",
                (proyecto_id,),
            )
            return [
                {"milestone_id": r[0], "nombre": r[1], "fecha": _fmt_date(r[2]), "completado": bool(r[3])}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def crear_hito(proyecto_id: str, nombre: str, fecha: str, completado: bool) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hitos (id, proyecto_id, nombre, fecha, completado) VALUES (%s,%s,%s,%s,%s)",
                (str(uuid.uuid4())[:8], proyecto_id, nombre, fecha or None, completado),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_hito(milestone_id: str):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, proyecto_id, nombre, fecha, completado FROM hitos WHERE id=%s", (milestone_id,))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "milestone_id": row[0], "proyecto_id": row[1], "nombre": row[2],
                "fecha": _fmt_date(row[3]), "completado": bool(row[4]),
            }
    finally:
        conn.close()


def editar_hito(milestone_id: str, nombre: str, fecha: str, completado: bool) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE hitos SET nombre=%s, fecha=%s, completado=%s WHERE id=%s",
                (nombre, fecha or None, completado, milestone_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── documentos (adjuntos de proyecto/tarea) ─────────────────────────────────
# Igual que core/views.py::project_docs/doc_upload/doc_download/doc_delete +
# core/models.py::Document/FILE_ICONS -- CRUD por fila (mismo patron que el
# resto de esta v2). media_dir() es la MISMA carpeta que usa la app Django
# real (BASE_DIR/media, sin subcarpeta -- a diferencia de las miniaturas de
# Whiteboard, que si viven en su propia subcarpeta media/whiteboards/).

MEDIA_ROOT = Path(__file__).resolve().parent.parent / "media"

FILE_ICONS = {
    "pdf": "📄", "doc": "📝", "docx": "📝",
    "xls": "📊", "xlsx": "📊", "csv": "📊",
    "ppt": "📋", "pptx": "📋",
    "jpg": "🖼", "jpeg": "🖼", "png": "🖼", "gif": "🖼", "webp": "🖼",
    "zip": "🗜", "rar": "🗜", "7z": "🗜",
    "mp4": "🎬", "mov": "🎬", "avi": "🎬",
    "mp3": "🎵", "wav": "🎵",
    "txt": "📃", "md": "📃",
}


def media_dir() -> Path:
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    return MEDIA_ROOT


def _doc_row_to_dict(row) -> dict:
    doc_id, proyecto_id, tarea_id, nombre, filename, size, subido_en, subido_por = row
    ext = nombre.rsplit(".", 1)[-1].lower() if "." in nombre else ""
    if size < 1024:
        size_str = f"{size} B"
    elif size < 1024 * 1024:
        size_str = f"{size / 1024:.1f} KB"
    else:
        size_str = f"{size / (1024 * 1024):.1f} MB"
    return {
        "doc_id": doc_id, "proyecto_id": proyecto_id, "tarea_id": _nn(tarea_id),
        "nombre": nombre, "filename": filename, "size": size, "size_str": size_str,
        "uploaded_at": _fmt_datetime(subido_en), "uploaded_by": _nn(subido_por),
        "icon": FILE_ICONS.get(ext, "📎"),
    }


def load_documentos_proyecto(proyecto_id: str) -> list:
    """Adjuntos a nivel de proyecto (tarea_id NULL) -- los adjuntos por
    tarea se listan aparte (load_documentos_por_tarea), no aqui."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, proyecto_id, tarea_id, nombre, filename, size, subido_en, subido_por "
                "FROM documentos WHERE proyecto_id=%s AND tarea_id IS NULL ORDER BY subido_en DESC",
                (proyecto_id,),
            )
            return [_doc_row_to_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def load_documentos_por_tarea(proyecto_id: str) -> dict:
    """{tarea_id: [documentos]} para el proyecto -- usado por la seccion
    "documentos por tarea" de Documentos (poblada por los adjuntos que se
    suben desde el panel de detalle de tarea, ver routers/tablero.py::
    task_doc_upload)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, proyecto_id, tarea_id, nombre, filename, size, subido_en, subido_por "
                "FROM documentos WHERE proyecto_id=%s AND tarea_id IS NOT NULL ORDER BY subido_en DESC",
                (proyecto_id,),
            )
            agrupado: dict = {}
            for r in cur.fetchall():
                doc = _doc_row_to_dict(r)
                agrupado.setdefault(doc["tarea_id"], []).append(doc)
            return agrupado
    finally:
        conn.close()


def load_documentos_de_tarea(tarea_id: str) -> list:
    """Adjuntos de UNA tarea concreta -- para el panel de detalle lateral
    (GET /tarea/<id>/detalle/), a diferencia de load_documentos_por_tarea()
    que trae los de TODAS las tareas de un proyecto a la vez (para la
    pagina de Documentos)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, proyecto_id, tarea_id, nombre, filename, size, subido_en, subido_por "
                "FROM documentos WHERE tarea_id=%s ORDER BY subido_en DESC",
                (tarea_id,),
            )
            return [_doc_row_to_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_documento(doc_id: str):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, proyecto_id, tarea_id, nombre, filename, size, subido_en, subido_por "
                "FROM documentos WHERE id=%s",
                (doc_id,),
            )
            row = cur.fetchone()
            return _doc_row_to_dict(row) if row else None
    finally:
        conn.close()


def crear_documento(proyecto_id: str, tarea_id: str, nombre: str, filename: str, size: int, subido_por: str) -> str:
    conn = connect()
    try:
        doc_id = str(uuid.uuid4())[:8]
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO documentos (id, proyecto_id, tarea_id, nombre, filename, size, subido_por) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (doc_id, proyecto_id, tarea_id or None, nombre, filename, size, subido_por),
            )
        conn.commit()
        return doc_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def eliminar_documento(doc_id: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM documentos WHERE id=%s", (doc_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── api_tokens (Configuracion -> API) -- claves de la API externa de tareas ──
# Igual que core/db_store.py::load_api_tokens/find_api_token_by_hash/
# crear_api_token/marcar_api_token_usado/eliminar_api_token. Solo se guarda
# el hash (sha256) de cada token, nunca el valor en claro.

def load_api_tokens() -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.nombre, t.token_hash, t.proyecto_id, t.creado_en, t.ultimo_uso_en, t.user_id, "
                "COALESCE(NULLIF(TRIM(CONCAT(u.first_name, ' ', u.last_name)), ''), u.username) "
                "FROM api_tokens t LEFT JOIN auth_user u ON u.id = t.user_id"
            )
            return [
                {"token_id": r[0], "nombre": r[1], "token_hash": r[2], "proyecto_id": r[3],
                 "creado_en": _fmt_datetime(r[4]), "ultimo_uso_en": _fmt_datetime(r[5]),
                 "user_id": r[6], "dueno": r[7] or ""}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def find_api_token_by_hash(token_hash: str):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, nombre, token_hash, proyecto_id, creado_en, ultimo_uso_en, user_id "
                "FROM api_tokens WHERE token_hash=%s", (token_hash,),
            )
            r = cur.fetchone()
            if not r:
                return None
            return {"token_id": r[0], "nombre": r[1], "token_hash": r[2], "proyecto_id": r[3],
                    "creado_en": _fmt_datetime(r[4]), "ultimo_uso_en": _fmt_datetime(r[5]), "user_id": r[6]}
    finally:
        conn.close()


def crear_api_token(token_id: str, nombre: str, token_hash: str, proyecto_id: str, user_id: int = None) -> None:
    """user_id: dueño del token (quien lo crea) -- la API actua como esa persona."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_tokens (id, nombre, token_hash, proyecto_id, user_id) VALUES (%s,%s,%s,%s,%s)",
                (token_id, nombre, token_hash, proyecto_id or None, user_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def marcar_api_token_usado(token_id: str) -> None:
    """Best-effort: no revienta si falla (solo informativo, para poder ver
    en Configuracion si un token sigue en uso antes de revocarlo)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE api_tokens SET ultimo_uso_en=NOW() WHERE id=%s", (token_id,))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def eliminar_api_token(token_id: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM api_tokens WHERE id=%s", (token_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── consultas de tareas para la API externa (GET /api/tareas/, .../<id>/) ────
# Select compartido por load_tareas_api/get_tarea_api -- igual conjunto de
# campos que core/views.py::_task_a_dict_api.

_TAREA_API_SELECT = (
    "SELECT t.id, t.proyecto_id, t.titulo, t.descripcion, t.proximo_paso, c.nombre, "
    "t.estado, t.prioridad, t.borrada, t.creada_en, t.actualizada_en, t.fecha_inicio, t.fecha_fin, "
    "t.iniciada_en, t.cerrada_en, t.sprint_id, t.empresa_id, t.sistema_id, t.iniciativa_id, "
    "t.anclada, t.mantis_id, t.generada_por_ia, t.subtareas, t.asignado_a, ua.username "
    "FROM tareas t LEFT JOIN categorias c ON c.id = t.categoria_id "
    "LEFT JOIN auth_user ua ON ua.id = t.asignado_a"
)


def _tarea_api_dict(r) -> dict:
    subtareas = []
    if r[22]:
        try:
            subtareas = [{"texto": s.get("t", ""), "hecho": bool(s.get("d") or s.get("checked"))}
                         for s in json.loads(r[22])]
        except Exception:
            subtareas = []
    return {
        "id": r[0], "proyecto_id": r[1], "titulo": r[2], "descripcion": _nn(r[3]), "proximo_paso": _nn(r[4]),
        "categoria": _nn(r[5]), "status": r[6], "prioridad": r[7], "borrada": bool(r[8]),
        "creada_en": _fmt_datetime(r[9]), "actualizada_en": _fmt_datetime(r[10]),
        "fecha_inicio": _fmt_date(r[11]), "fecha_fin": _fmt_date(r[12]),
        "iniciada_en": _fmt_datetime(r[13]), "cerrada_en": _fmt_datetime(r[14]),
        "sprint_id": r[15] or None, "empresa_id": r[16] or None, "sistema_id": r[17] or None,
        "iniciativa_id": r[18] or None, "anclada": bool(r[19]), "mantis_id": r[20] or None,
        "generada_por_ia": bool(r[21]), "subtareas": subtareas,
        "asignado_a": r[23], "asignado": r[24] or None,
    }


def load_tareas_api(proyecto_id: str = "", status: str = "", empresa_id: str = "", categoria: str = "",
                     mantis_id=None, solo_mantis: bool = False, incluir_borradas: bool = False,
                     actualizada_desde: str = "", limit: int = 200, offset: int = 0):
    """Igual que core/views.py::api_tareas_lista, pero filtrando en SQL (no
    cargando todas las tareas en Python) -- devuelve (tareas, total)."""
    where, params = [], []
    if proyecto_id:
        where.append("t.proyecto_id=%s"); params.append(proyecto_id)
    if status:
        where.append("t.estado=%s"); params.append(status)
    if empresa_id:
        where.append("t.empresa_id=%s"); params.append(empresa_id)
    if categoria:
        where.append("LOWER(c.nombre)=LOWER(%s)"); params.append(categoria)
    if mantis_id is not None:
        where.append("t.mantis_id=%s"); params.append(mantis_id)
    if solo_mantis:
        where.append("t.mantis_id IS NOT NULL")
    if not incluir_borradas:
        where.append("t.borrada=0")
    if actualizada_desde:
        where.append("t.actualizada_en >= %s"); params.append(actualizada_desde)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM tareas t LEFT JOIN categorias c ON c.id = t.categoria_id{where_sql}",
                params,
            )
            total = cur.fetchone()[0]
            cur.execute(
                f"{_TAREA_API_SELECT}{where_sql} ORDER BY t.actualizada_en DESC LIMIT %s OFFSET %s",
                params + [limit, offset],
            )
            tareas = [_tarea_api_dict(r) for r in cur.fetchall()]
        return tareas, total
    finally:
        conn.close()


def get_tarea_api(task_id: str):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"{_TAREA_API_SELECT} WHERE t.id=%s", (task_id,))
            r = cur.fetchone()
            return _tarea_api_dict(r) if r else None
    finally:
        conn.close()


# ── marcadores (accesos directos personales, ver templating.py::marcadores_activos) ──

def load_marcadores(user_id: int) -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, titulo, url FROM marcadores WHERE user_id=%s ORDER BY orden, creado_en",
                (user_id,),
            )
            return [{"id": r[0], "titulo": r[1], "url": r[2]} for r in cur.fetchall()]
    finally:
        conn.close()


def crear_marcador(marcador_id: str, user_id: int, titulo: str, url: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(orden), 0) FROM marcadores WHERE user_id=%s", (user_id,))
            orden = cur.fetchone()[0] + 1
            cur.execute(
                "INSERT INTO marcadores (id, user_id, titulo, url, orden) VALUES (%s,%s,%s,%s,%s)",
                (marcador_id, user_id, titulo, url, orden),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def eliminar_marcador(marcador_id: str, user_id: int) -> None:
    """user_id en el WHERE, no solo el id -- que nadie pueda borrar el
    marcador de otro adivinando/probando ids."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM marcadores WHERE id=%s AND user_id=%s", (marcador_id, user_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── recordatorio_mensajes (Recordatorio ergonomico, ver escritorio_servicios.py)
#
# Lista de mensajes que antes vivia en APP_RECORDATORIO/data/messages.txt.
# Todas aceptan real=True: el servicio de escritorio y su pantalla de
# Configuracion trabajan siempre sobre la BD real (ver connect()); los tests
# llaman sin real para quedarse en test_app_tareas.

def _recordatorio_row(r) -> dict:
    return {
        "id": r[0], "texto": r[1], "categoria": r[2] or "", "activo": bool(r[3]), "orden": r[4],
        "veces_mostrado": r[5], "ultimo_mostrado": _fmt_datetime(r[6]) if r[6] else "",
    }


def load_recordatorio_mensajes(real: bool = False, solo_activos: bool = False) -> list:
    conn = connect(real)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, texto, categoria, activo, orden, veces_mostrado, ultimo_mostrado "
                "FROM recordatorio_mensajes " + ("WHERE activo=1 " if solo_activos else "") +
                "ORDER BY orden, id"
            )
            return [_recordatorio_row(r) for r in cur.fetchall()]
    finally:
        conn.close()


def crear_recordatorio_mensaje(texto: str, categoria: str = "", real: bool = False) -> int:
    conn = connect(real)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(orden), 0) FROM recordatorio_mensajes")
            orden = cur.fetchone()[0] + 1
            cur.execute(
                "INSERT INTO recordatorio_mensajes (texto, categoria, activo, orden) VALUES (%s,%s,1,%s)",
                (texto, categoria, orden),
            )
            nuevo_id = cur.lastrowid
        conn.commit()
        return nuevo_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def editar_recordatorio_mensaje(mensaje_id: int, texto: str, categoria: str, real: bool = False) -> None:
    conn = connect(real)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE recordatorio_mensajes SET texto=%s, categoria=%s WHERE id=%s",
                        (texto, categoria, mensaje_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def toggle_recordatorio_mensaje(mensaje_id: int, real: bool = False) -> None:
    conn = connect(real)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE recordatorio_mensajes SET activo = NOT activo WHERE id=%s", (mensaje_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def eliminar_recordatorio_mensaje(mensaje_id: int, real: bool = False) -> None:
    conn = connect(real)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM recordatorio_mensajes WHERE id=%s", (mensaje_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def marcar_recordatorio_mostrado(mensaje_id: int, real: bool = False) -> None:
    conn = connect(real)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE recordatorio_mensajes SET veces_mostrado = veces_mostrado + 1, ultimo_mostrado=%s "
                "WHERE id=%s", (now_text(), mensaje_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def hoy_es_festivo(real: bool = False) -> bool:
    """Si hoy cae en un dia especial cuyo tipo cuenta como no laborable
    (tipos_dia_especial.cuenta_para_sla) -- mismo criterio que
    festivos_dates(), pero solo para hoy y con real=, para el Recordatorio."""
    conn = connect(real)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM dias_especiales d JOIN tipos_dia_especial t ON t.id=d.tipo_id "
                "WHERE t.cuenta_para_sla=1 AND CURDATE() BETWEEN d.fecha_inicio AND d.fecha_fin LIMIT 1"
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


# ── Buzon: notas entre personas (fase 1 de mensajeria, ver routers/buzon.py) ─
#
# Inspirado en las Note de CS Manager (otra app interna): una nota va de un
# usuario a otro (o a si mismo = nota personal), puede llevar fecha de
# vencimiento con aviso previo, reenviarse (crea una nota NUEVA, la original
# no se toca) y convertirse en tarea (buzon_nota_tareas guarda el vinculo).
# Nada que ver con la tabla 'notas' (post-its sin usuario de la pagina Notas
# y de la Agenda), que sigue igual.

AVISO_UNIDADES = {"horas": 1, "dias": 24, "semanas": 24 * 7}  # en horas
_HORA_VENCIMIENTO = 9  # una nota que vence el dia X se considera que vence a las 09:00


def _buzon_row(r) -> dict:
    (nid, de_id, para_id, contenido, leida, leida_en, vence_en, aviso_cantidad, aviso_unidad,
     recordatorio_enviado_en, reenviada_de, creada_en, editada_en, de_nombre, para_nombre, n_tareas) = r
    return {
        "id": nid, "de_user_id": de_id, "para_user_id": para_id, "contenido": contenido,
        "leida": bool(leida), "leida_en": _fmt_datetime(leida_en),
        "vence_en": _fmt_date(vence_en), "aviso_cantidad": aviso_cantidad, "aviso_unidad": aviso_unidad,
        "recordatorio_enviado": bool(recordatorio_enviado_en), "reenviada_de": reenviada_de,
        "creada_en": _fmt_datetime(creada_en), "editada_en": _fmt_datetime(editada_en),
        "de_nombre": de_nombre, "para_nombre": para_nombre, "personal": de_id == para_id,
        "n_tareas": n_tareas,
    }


_BUZON_SELECT = (
    "SELECT n.id, n.de_user_id, n.para_user_id, n.contenido, n.leida, n.leida_en, n.vence_en, "
    "n.aviso_cantidad, n.aviso_unidad, n.recordatorio_enviado_en, n.reenviada_de, n.creada_en, n.editada_en, "
    "COALESCE(NULLIF(TRIM(CONCAT(ud.first_name, ' ', ud.last_name)), ''), ud.username), "
    "COALESCE(NULLIF(TRIM(CONCAT(up.first_name, ' ', up.last_name)), ''), up.username), "
    "(SELECT COUNT(*) FROM buzon_nota_tareas bt WHERE bt.nota_id = n.id) "
    "FROM buzon_notas n JOIN auth_user ud ON ud.id = n.de_user_id JOIN auth_user up ON up.id = n.para_user_id "
)


def load_buzon(user_id: int, vista: str, desde: str = "", hasta: str = "", persona_id=None) -> list:
    """vista: 'propias' (de mi para mi), 'recibidas' (de otros para mi) o
    'enviadas' (mias para otros). desde/hasta: YYYY-MM-DD sobre creada_en.
    persona_id: el otro extremo (remitente en recibidas, destinatario en
    enviadas); se ignora en 'propias'."""
    where, params = [], []
    if vista == "propias":
        where.append("n.de_user_id = %s AND n.para_user_id = %s"); params += [user_id, user_id]
    elif vista == "enviadas":
        where.append("n.de_user_id = %s AND n.para_user_id <> %s"); params += [user_id, user_id]
        if persona_id:
            where.append("n.para_user_id = %s"); params.append(persona_id)
    else:
        where.append("n.para_user_id = %s AND n.de_user_id <> %s"); params += [user_id, user_id]
        if persona_id:
            where.append("n.de_user_id = %s"); params.append(persona_id)
    if desde:
        where.append("n.creada_en >= %s"); params.append(desde)
    if hasta:
        where.append("n.creada_en < DATE_ADD(%s, INTERVAL 1 DAY)"); params.append(hasta)
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_BUZON_SELECT + "WHERE " + " AND ".join(where) + " ORDER BY n.creada_en DESC, n.id DESC", params)
            return [_buzon_row(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_buzon_nota(nota_id: int):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_BUZON_SELECT + "WHERE n.id = %s", (nota_id,))
            row = cur.fetchone()
            return _buzon_row(row) if row else None
    finally:
        conn.close()


def crear_buzon_nota(de_user_id: int, para_user_id: int, contenido: str, vence_en: str = "",
                     aviso_cantidad: int = 1, aviso_unidad: str = "dias", reenviada_de=None,
                     real: bool = False) -> int:
    """real=True: los servicios de escritorio (escritorio_servicios.py), que avisan siempre en la BD real."""
    conn = connect(real)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO buzon_notas (de_user_id, para_user_id, contenido, leida, leida_en, vence_en, "
                "aviso_cantidad, aviso_unidad, reenviada_de, creada_en) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                # una nota personal nace leida: no tiene sentido avisarse a uno mismo de que se ha escrito algo
                (de_user_id, para_user_id, contenido, de_user_id == para_user_id,
                 now_text() if de_user_id == para_user_id else None, vence_en or None,
                 aviso_cantidad, aviso_unidad if aviso_unidad in AVISO_UNIDADES else "dias",
                 reenviada_de, now_text()),
            )
            nota_id = cur.lastrowid
        conn.commit()
        return nota_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def editar_buzon_nota(nota_id: int, contenido: str, vence_en: str, aviso_cantidad: int, aviso_unidad: str) -> None:
    """Si cambia el vencimiento o el aviso, el recordatorio vuelve a quedar
    pendiente (recordatorio_enviado_en=NULL) para que salte en la fecha nueva."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE buzon_notas SET contenido=%s, editada_en=%s, "
                "recordatorio_enviado_en = IF(vence_en <=> %s AND aviso_cantidad=%s AND aviso_unidad=%s, "
                "recordatorio_enviado_en, NULL), vence_en=%s, aviso_cantidad=%s, aviso_unidad=%s WHERE id=%s",
                (contenido, now_text(), vence_en or None, aviso_cantidad, aviso_unidad,
                 vence_en or None, aviso_cantidad, aviso_unidad if aviso_unidad in AVISO_UNIDADES else "dias", nota_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def marcar_buzon_leida(nota_id: int, para_user_id: int, leida: bool = True) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE buzon_notas SET leida=%s, leida_en=%s WHERE id=%s AND para_user_id=%s",
                (leida, now_text() if leida else None, nota_id, para_user_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def marcar_buzon_todas_leidas(para_user_id: int) -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE buzon_notas SET leida=TRUE, leida_en=%s WHERE para_user_id=%s AND leida=FALSE",
                        (now_text(), para_user_id))
            n = cur.rowcount
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def eliminar_buzon_nota(nota_id: int) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM buzon_notas WHERE id=%s", (nota_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def contar_buzon_sin_leer(user_id: int) -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM buzon_notas WHERE para_user_id=%s AND leida=FALSE", (user_id,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def vincular_nota_tarea(nota_id: int, tarea_id: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO buzon_nota_tareas (nota_id, tarea_id) VALUES (%s,%s)", (nota_id, tarea_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def desvincular_tareas_de_nota(nota_id: int) -> None:
    """Las tareas siguen existiendo, solo dejan de estar conectadas a la nota."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM buzon_nota_tareas WHERE nota_id=%s", (nota_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_tareas_de_nota(nota_id: int) -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.titulo, t.proyecto_id, t.estado FROM buzon_nota_tareas bt "
                "JOIN tareas t ON t.id = bt.tarea_id WHERE bt.nota_id=%s AND t.borrada=0", (nota_id,),
            )
            return [{"id": r[0], "titulo": r[1], "proyecto_id": r[2], "estado": r[3]} for r in cur.fetchall()]
    finally:
        conn.close()


def load_proyectos_para_crear_tareas(user_id: int) -> list:
    """Proyectos donde este usuario puede crear tareas, agrupables por espacio:
    los que no tienen espacio (legado, igual que _puede_editar_espacio_de en
    tablero.py) y los de espacios donde es Admin o Editor. Cada uno lleva
    'rol' para decidir a quien se puede asignar la tarea."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT p.id, p.nombre, p.espacio_id, COALESCE(e.nombre, 'Sin espacio'), eu.rol "
                "FROM proyectos p LEFT JOIN espacios e ON e.id = p.espacio_id "
                "LEFT JOIN espacio_usuarios eu ON eu.espacio_id = p.espacio_id AND eu.user_id = %s "
                "WHERE p.activo = TRUE AND (p.espacio_id IS NULL OR eu.rol IN ('admin','editor')) "
                "ORDER BY e.es_personal DESC, e.orden, e.nombre, p.orden, p.nombre",
                (user_id,),
            )
            return [{"id": r[0], "nombre": r[1], "espacio_id": r[2], "espacio": r[3], "rol": r[4]}
                    for r in cur.fetchall()]
    finally:
        conn.close()


# ── notificaciones (campana de la cabecera, ver routers/notificaciones.py) ──

def crear_notificacion(user_id: int, tipo: str, texto: str, url: str = "", real: bool = False) -> None:
    if not user_id:
        return
    conn = connect(real)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notificaciones (user_id, tipo, texto, url, creada_en) VALUES (%s,%s,%s,%s,%s)",
                (user_id, tipo, texto[:300], url[:300], now_text()),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _notif_row(r) -> dict:
    return {"id": r[0], "tipo": r[1], "texto": r[2], "url": r[3], "leida": bool(r[4]), "creada_en": _fmt_datetime(r[5])}


def resumen_notificaciones(user_id: int, limite: int = 8) -> dict:
    """Para la campana: cuantas sin leer, la id mas alta (el navegador la
    usa para saber si ha llegado algo nuevo desde la ultima consulta) y las
    ultimas 'limite'. No marca nada como leido."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM notificaciones WHERE user_id=%s AND leida=FALSE", (user_id,))
            sin_leer, ultima_id = cur.fetchone()
            cur.execute(
                "SELECT id, tipo, texto, url, leida, creada_en FROM notificaciones WHERE user_id=%s "
                "ORDER BY id DESC LIMIT %s", (user_id, limite),
            )
            return {"sin_leer": sin_leer, "ultima_id": ultima_id, "ultimas": [_notif_row(r) for r in cur.fetchall()]}
    finally:
        conn.close()


def load_notificaciones(user_id: int, limite: int = 200) -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, tipo, texto, url, leida, creada_en FROM notificaciones WHERE user_id=%s "
                "ORDER BY id DESC LIMIT %s", (user_id, limite),
            )
            return [_notif_row(r) for r in cur.fetchall()]
    finally:
        conn.close()


def marcar_notificaciones_leidas(user_id: int) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE notificaciones SET leida=TRUE WHERE user_id=%s AND leida=FALSE", (user_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def generar_recordatorios_buzon(user_id: int, ahora=None) -> int:
    """Crea la notificacion de recordatorio de cada nota de este usuario cuyo
    momento de aviso (vence_en a las 09:00 menos aviso_cantidad*unidad) ya ha
    llegado y aun no se ha avisado. Se llama al consultar la campana -- sin
    hilo propio en segundo plano, y asi funciona igual en el entorno de
    pruebas. Devuelve cuantos recordatorios ha creado."""
    ahora = ahora or datetime.now()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, contenido, vence_en, aviso_cantidad, aviso_unidad FROM buzon_notas "
                "WHERE para_user_id=%s AND vence_en IS NOT NULL AND recordatorio_enviado_en IS NULL",
                (user_id,),
            )
            vencen = []
            for nid, contenido, vence_en, cantidad, unidad in cur.fetchall():
                momento = datetime.combine(vence_en, datetime.min.time()).replace(hour=_HORA_VENCIMIENTO)
                if ahora >= momento - timedelta(hours=cantidad * AVISO_UNIDADES.get(unidad, 24)):
                    vencen.append((nid, contenido, vence_en))
            for nid, contenido, vence_en in vencen:
                resumen = contenido if len(contenido) <= 80 else contenido[:77] + "..."
                cur.execute(
                    "INSERT INTO notificaciones (user_id, tipo, texto, url, creada_en) VALUES (%s,'recordatorio',%s,%s,%s)",
                    (user_id, f"Vence el {vence_en.strftime('%d/%m/%Y')}: {resumen}", f"/buzon/#nota-{nid}", now_text()),
                )
                cur.execute("UPDATE buzon_notas SET recordatorio_enviado_en=%s WHERE id=%s", (now_text(), nid))
        conn.commit()
        return len(vencen)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── salas y mensajes (fase 2: muro de actividad por espacio, ver muro.py) ───
#
# Tablas chat_salas/chat_mensajes/chat_lecturas. Solo hay salas 'muro', una
# por espacio, creada la primera vez que hace falta (los chats directos y de
# grupo se quitaron: la app no es de chats, ver muro.py).

DESHACER_SEGUNDOS = 30  # ventana para recuperar un mensaje propio borrado


def get_espacio_owner(espacio_id: str):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT owner_id FROM espacios WHERE id=%s", (espacio_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def get_or_create_sala_muro(espacio_id: str) -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM chat_salas WHERE tipo='muro' AND espacio_id=%s", (espacio_id,))
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute("INSERT IGNORE INTO chat_salas (tipo, espacio_id, creada_en) VALUES ('muro', %s, %s)",
                        (espacio_id, now_text()))
            cur.execute("SELECT id FROM chat_salas WHERE tipo='muro' AND espacio_id=%s", (espacio_id,))
            sala_id = cur.fetchone()[0]
        conn.commit()
        return sala_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def crear_chat_mensaje(sala_id: int, user_id, contenido: str, url: str = "", es_sistema: bool = False,
                       proyecto_id: str = None) -> int:
    """proyecto_id: en un muro, de que proyecto del espacio es el mensaje
    (lo usa la pestana "Muro" de cada proyecto); None = del espacio en general."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_mensajes (sala_id, user_id, contenido, url, es_sistema, proyecto_id, creado_en) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (sala_id, user_id, contenido, url[:300], es_sistema, proyecto_id or None, now_text()),
            )
            mensaje_id = cur.lastrowid
        conn.commit()
        return mensaje_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_chat_mensajes(sala_id: int, viewer_id: int, limite: int = 150, proyecto_id: str = None) -> list:
    """Los ultimos 'limite' mensajes, del mas antiguo al mas nuevo. Un mensaje
    borrado desaparece para todos, salvo para su autor durante
    DESHACER_SEGUNDOS (sale como 'borrado' con boton de deshacer).
    proyecto_id: solo los de ese proyecto (pestana Muro del proyecto) -- los
    avisos difundidos salen siempre, son para todo el espacio."""
    filtro, params = "", [sala_id, viewer_id, DESHACER_SEGUNDOS]
    if proyecto_id:
        filtro = "AND (m.proyecto_id=%s OR m.es_aviso=TRUE) "
        params.append(proyecto_id)
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT m.id, m.user_id, m.contenido, m.url, m.es_sistema, m.es_aviso, m.creado_en, m.borrado_en, "
                "COALESCE(NULLIF(TRIM(CONCAT(u.first_name, ' ', u.last_name)), ''), u.username), "
                "TIMESTAMPDIFF(SECOND, m.borrado_en, NOW()), m.proyecto_id, p.nombre, p.color "
                "FROM chat_mensajes m LEFT JOIN auth_user u ON u.id = m.user_id "
                "LEFT JOIN proyectos p ON p.id = m.proyecto_id "
                "WHERE m.sala_id=%s AND (m.caduca_en IS NULL OR m.caduca_en > NOW()) "
                "AND (m.borrado_en IS NULL OR (m.user_id=%s AND m.borrado_en > NOW() - INTERVAL %s SECOND)) "
                + filtro + "ORDER BY m.id DESC LIMIT %s",
                params + [limite],
            )
            filas = cur.fetchall()
    finally:
        conn.close()
    return [{
        "id": r[0], "user_id": r[1], "contenido": r[2], "url": r[3], "es_sistema": bool(r[4]),
        "es_aviso": bool(r[5]), "creado_en": _fmt_datetime(r[6]), "borrado": r[7] is not None,
        "autor": r[8] or "", "deshacer_quedan": max(0, DESHACER_SEGUNDOS - (r[9] or 0)) if r[7] else 0,
        "proyecto_id": r[10] or "", "proyecto": r[11] or "", "proyecto_color": r[12] or "",
    } for r in reversed(filas)]


def get_chat_mensaje(mensaje_id: int):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, sala_id, user_id, es_sistema, borrado_en FROM chat_mensajes WHERE id=%s", (mensaje_id,))
            r = cur.fetchone()
            return {"id": r[0], "sala_id": r[1], "user_id": r[2], "es_sistema": bool(r[3]), "borrado": r[4] is not None} if r else None
    finally:
        conn.close()


def borrar_chat_mensaje(mensaje_id: int, user_id: int) -> bool:
    """Solo mensajes propios y nunca los de sistema (esos quedan siempre) ni
    los avisos difundidos (esos solo los borra IT, ver borrar_aviso_difundido)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_mensajes SET borrado_en=NOW() WHERE id=%s AND user_id=%s AND es_sistema=FALSE "
                "AND es_aviso=FALSE AND borrado_en IS NULL",
                (mensaje_id, user_id),
            )
            ok = cur.rowcount == 1
        conn.commit()
        return ok
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def deshacer_borrado_chat_mensaje(mensaje_id: int, user_id: int) -> bool:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_mensajes SET borrado_en=NULL WHERE id=%s AND user_id=%s "
                "AND borrado_en > NOW() - INTERVAL %s SECOND",
                (mensaje_id, user_id, DESHACER_SEGUNDOS),
            )
            ok = cur.rowcount == 1
        conn.commit()
        return ok
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_chat_lectura(sala_id: int, user_id: int) -> dict:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ultimo_leido_id, silenciada FROM chat_lecturas WHERE sala_id=%s AND user_id=%s", (sala_id, user_id))
            r = cur.fetchone()
            return {"ultimo_leido_id": r[0], "silenciada": bool(r[1])} if r else {"ultimo_leido_id": 0, "silenciada": False}
    finally:
        conn.close()


def marcar_chat_leido(sala_id: int, user_id: int, ultimo_id: int, proyecto_id: str = None) -> None:
    """Abrir el muro del espacio lo marca leido entero, con sus
    avisos de la campana (incluidos los de los muros de sus proyectos).
    Abrir la pestana Muro de UN proyecto solo marca leidos los avisos de ese
    proyecto: el contador del espacio sigue contando lo de los demas."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            if proyecto_id:
                cur.execute(
                    "UPDATE notificaciones SET leida=TRUE WHERE user_id=%s AND tipo='muro' AND leida=FALSE "
                    "AND url=CONCAT('/proyecto/', %s, '/muro/')",
                    (user_id, proyecto_id),
                )
                conn.commit()
                return
            cur.execute(
                "INSERT INTO chat_lecturas (sala_id, user_id, ultimo_leido_id) VALUES (%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE ultimo_leido_id = GREATEST(ultimo_leido_id, VALUES(ultimo_leido_id))",
                (sala_id, user_id, ultimo_id),
            )
            # entrar al muro = sus avisos de la campana quedan leidos
            cur.execute(
                "UPDATE notificaciones n JOIN chat_salas s ON s.id=%s SET n.leida=TRUE "
                "WHERE n.user_id=%s AND n.tipo IN ('muro','aviso') AND n.leida=FALSE "
                "AND (n.url = CONCAT('/muro/?espacio=', s.espacio_id) OR n.url LIKE CONCAT('/espacio/', s.espacio_id, '/muro/%%'))",
                (sala_id, user_id),
            )
            cur.execute(
                "UPDATE notificaciones n JOIN chat_salas s ON s.id=%s JOIN proyectos p ON p.espacio_id = s.espacio_id "
                "SET n.leida=TRUE WHERE n.user_id=%s AND n.tipo='muro' AND n.leida=FALSE "
                "AND n.url = CONCAT('/proyecto/', p.id, '/muro/')",
                (sala_id, user_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_chat_silenciada(sala_id: int, user_id: int, silenciada: bool) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_lecturas (sala_id, user_id, silenciada) VALUES (%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE silenciada=VALUES(silenciada)",
                (sala_id, user_id, silenciada),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def contar_muros_sin_leer(user_id: int) -> dict:
    """{espacio_id: n} mensajes de otras personas (no del sistema) que este
    usuario aun no ha visto en el muro de cada espacio donde es miembro."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.espacio_id, COUNT(m.id) FROM chat_salas s "
                "JOIN espacio_usuarios eu ON eu.espacio_id = s.espacio_id AND eu.user_id = %s "
                "JOIN chat_mensajes m ON m.sala_id = s.id "
                "LEFT JOIN chat_lecturas l ON l.sala_id = s.id AND l.user_id = %s "
                "WHERE s.tipo='muro' AND m.es_sistema=FALSE AND m.borrado_en IS NULL "
                "AND (m.user_id IS NULL OR m.user_id <> %s) AND m.id > COALESCE(l.ultimo_leido_id, 0) "
                "GROUP BY s.espacio_id",
                (user_id, user_id, user_id),
            )
            return {r[0]: r[1] for r in cur.fetchall()}
    finally:
        conn.close()


def miembros_a_avisar_muro(espacio_id: str, sala_id: int, excluir_user_id) -> list:
    """Miembros del espacio que no han silenciado su muro, sin el autor."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT eu.user_id FROM espacio_usuarios eu "
                "LEFT JOIN chat_lecturas l ON l.sala_id=%s AND l.user_id=eu.user_id "
                "WHERE eu.espacio_id=%s AND COALESCE(l.silenciada, FALSE)=FALSE AND eu.user_id <> %s",
                (sala_id, espacio_id, excluir_user_id or 0),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def tiene_notificacion_sin_leer(user_id: int, url: str) -> bool:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM notificaciones WHERE user_id=%s AND url=%s AND leida=FALSE LIMIT 1", (user_id, url))
            return cur.fetchone() is not None
    finally:
        conn.close()


def get_tarea_resumen(task_id: str):
    """(titulo, proyecto_id, estado, asignado_a, borrada) -- para los eventos del muro."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT titulo, proyecto_id, estado, asignado_a, borrada FROM tareas WHERE id=%s", (task_id,))
            r = cur.fetchone()
            return {"titulo": r[0], "proyecto_id": r[1], "estado": r[2], "asignado_a": r[3], "borrada": bool(r[4])} if r else None
    finally:
        conn.close()


# ── avisos difundidos (solo IT) y lista de muros de un usuario ──────────────
#
# La app no es de chats: no hay chats directos ni de grupo (se quitaron el
# 2026-10-14, ver scripts/migrar_unificar_muro.py), la conversacion vive en
# el muro de cada espacio/proyecto y lo de persona a persona en el Buzon.

def get_chat_sala(sala_id: int):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, tipo, espacio_id, nombre FROM chat_salas WHERE id=%s", (sala_id,))
            r = cur.fetchone()
            return {"id": r[0], "tipo": r[1], "espacio_id": r[2], "nombre": r[3]} if r else None
    finally:
        conn.close()


def crear_aviso_difundido(sala_id: int, user_id: int, contenido: str, caduca_en=None) -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_mensajes (sala_id, user_id, contenido, es_aviso, caduca_en, creado_en) "
                "VALUES (%s,%s,%s,TRUE,%s,%s)",
                (sala_id, user_id, contenido, caduca_en, now_text()),
            )
            mensaje_id = cur.lastrowid
        conn.commit()
        return mensaje_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def destinatarios_muro(espacio_id: str) -> list:
    """Quien esta en el muro de un espacio: sus miembros, o su dueño si es personal."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM espacio_usuarios WHERE espacio_id=%s", (espacio_id,))
            ids = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT owner_id FROM espacios WHERE id=%s AND es_personal=TRUE", (espacio_id,))
            row = cur.fetchone()
            if row and row[0]:
                ids.add(row[0])
            return sorted(ids)
    finally:
        conn.close()


def borrar_aviso_difundido(mensaje_id: int) -> bool:
    """Solo para IT (lo comprueba el router): un aviso difundido no se borra
    desde el boton normal de cada uno."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE chat_mensajes SET borrado_en=NOW() WHERE id=%s AND es_aviso=TRUE AND borrado_en IS NULL",
                        (mensaje_id,))
            ok = cur.rowcount == 1
        conn.commit()
        return ok
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def actividad_muros() -> dict:
    """{espacio_id: fecha del ultimo mensaje visible} -- para ordenar la lista de muros."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.espacio_id, MAX(m.creado_en) FROM chat_salas s JOIN chat_mensajes m ON m.sala_id = s.id "
                "WHERE s.tipo='muro' AND m.borrado_en IS NULL AND (m.caduca_en IS NULL OR m.caduca_en > NOW()) "
                "GROUP BY s.espacio_id"
            )
            return {r[0]: _fmt_datetime(r[1]) for r in cur.fetchall()}
    finally:
        conn.close()



def load_actividad_muros(espacio_ids: list, limite: int = 8) -> list:
    """Lo ultimo que ha pasado en los muros de estos espacios (eventos y
    mensajes, sin borrados ni avisos caducados) -- para la portada."""
    if not espacio_ids:
        return []
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT m.id, s.espacio_id, e.nombre, e.color, m.proyecto_id, p.nombre, p.color, m.user_id, "
                "COALESCE(NULLIF(TRIM(CONCAT(u.first_name, ' ', u.last_name)), ''), u.username), "
                "m.contenido, m.url, m.es_sistema, m.es_aviso, m.creado_en "
                "FROM chat_mensajes m JOIN chat_salas s ON s.id = m.sala_id AND s.tipo='muro' "
                "JOIN espacios e ON e.id = s.espacio_id "
                "LEFT JOIN proyectos p ON p.id = m.proyecto_id LEFT JOIN auth_user u ON u.id = m.user_id "
                f"WHERE s.espacio_id IN ({','.join(['%s'] * len(espacio_ids))}) AND m.borrado_en IS NULL "
                "AND (m.caduca_en IS NULL OR m.caduca_en > NOW()) ORDER BY m.id DESC LIMIT %s",
                list(espacio_ids) + [limite],
            )
            return [{
                "id": r[0], "espacio_id": r[1], "espacio": r[2], "espacio_color": r[3], "proyecto_id": r[4] or "",
                "proyecto": r[5] or "", "proyecto_color": r[6] or "", "user_id": r[7], "autor": r[8] or "",
                "contenido": r[9], "url": r[10] or "", "es_sistema": bool(r[11]), "es_aviso": bool(r[12]),
                "creado_en": _fmt_datetime(r[13]),
            } for r in cur.fetchall()]
    finally:
        conn.close()


# ── nexus: relaciones entre cualquier par de entidades (tipo Union-Find) ─────
# Ver scripts/schema_app_tareas.sql (nexus_nodos/_relaciones/_historial). Cada
# entidad tiene un nodo; los nodos relacionados (directa o indirectamente)
# comparten "representante". Cada operacion existe en dos formas: _nexus_*(cur,
# ...) para usarla DENTRO de la transaccion de un crear_*/eliminar_* existente,
# y la publica, que abre su propia conexion.

NEXUS_TIPOS = ("proyecto", "sistema", "wiki_pagina", "nota", "whiteboard", "empresa", "credencial")


def _nexus_tipo(tipo: str) -> str:
    if tipo not in NEXUS_TIPOS:
        raise ValueError(f"Tipo de nexus desconocido: {tipo!r}")
    return tipo


def _nexus_nodo(cur, tipo: str, entidad_id) -> tuple | None:
    """(id, representante) del nodo de esa entidad, o None."""
    cur.execute("SELECT id, representante FROM nexus_nodos WHERE tipo=%s AND entidad_id=%s",
                (_nexus_tipo(tipo), str(entidad_id)))
    return cur.fetchone()


def _nexus_crear_nodo(cur, tipo: str, entidad_id) -> str:
    """Idempotente: si la entidad ya tiene nodo devuelve el suyo (sirve igual
    para el alta normal que para el relleno de filas antiguas)."""
    fila = _nexus_nodo(cur, tipo, entidad_id)
    if fila:
        return fila[0]
    nodo_id = str(uuid.uuid4())[:8]
    cur.execute("INSERT INTO nexus_nodos (id, tipo, entidad_id, representante) VALUES (%s, %s, %s, %s)",
                (nodo_id, tipo, str(entidad_id), nodo_id))
    return nodo_id


def _nexus_historial(cur, nodos, anterior, nuevo, motivo: str):
    cur.execute(
        "INSERT INTO nexus_historial (grupo_nodos, representante_anterior, representante_nuevo, motivo) "
        "VALUES (%s, %s, %s, %s)",
        (",".join(sorted(nodos)), anterior, nuevo, motivo[:255]),
    )


def _nexus_miembros(cur, representante: str) -> list:
    cur.execute("SELECT id FROM nexus_nodos WHERE representante=%s", (representante,))
    return [r[0] for r in cur.fetchall()]


def _nexus_relacionar(cur, a: tuple, b: tuple) -> bool:
    """a, b = (tipo, entidad_id); crea los nodos si faltan. Union por tamaño:
    el grupo pequeño pasa al representante del grande (empate: se queda el
    de `a`). Devuelve True si la arista es nueva."""
    nodo_a, nodo_b = _nexus_crear_nodo(cur, *a), _nexus_crear_nodo(cur, *b)
    if nodo_a == nodo_b:
        return False
    rep_a, rep_b = _nexus_nodo(cur, *a)[1], _nexus_nodo(cur, *b)[1]
    if rep_a != rep_b:
        grupo_a, grupo_b = _nexus_miembros(cur, rep_a), _nexus_miembros(cur, rep_b)
        queda, sale, movidos = (rep_a, rep_b, grupo_b) if len(grupo_a) >= len(grupo_b) else (rep_b, rep_a, grupo_a)
        cur.execute("UPDATE nexus_nodos SET representante=%s WHERE representante=%s", (queda, sale))
        _nexus_historial(cur, movidos, sale, queda, f"union de grupos ({len(grupo_a)}+{len(grupo_b)} nodos)")
    menor, mayor = sorted((nodo_a, nodo_b))
    cur.execute("INSERT IGNORE INTO nexus_relaciones (nodo_a, nodo_b) VALUES (%s, %s)", (menor, mayor))
    return cur.rowcount == 1


def _nexus_eliminar_nodo(cur, tipo: str, entidad_id) -> str | None:
    """Llamar ANTES de borrar la fila real. Si el nodo era representante de
    un grupo con mas miembros, promueve al que mas aristas tiene con el resto
    del grupo (sin contar las del nodo que se va; empate: el mas antiguo) y
    lo registra en nexus_historial. Devuelve el nuevo representante, o None
    si no hacia falta ninguno. Las aristas del nodo se van por el CASCADE."""
    fila = _nexus_nodo(cur, tipo, entidad_id)
    if not fila:
        return None
    nodo_id, rep = fila
    nuevo = None
    if nodo_id == rep:
        resto = [n for n in _nexus_miembros(cur, rep) if n != nodo_id]
        if resto:
            marcas = ",".join(["%s"] * len(resto))
            cur.execute(
                f"SELECT n.id, (SELECT COUNT(*) FROM nexus_relaciones r WHERE "
                f"(r.nodo_a=n.id AND r.nodo_b IN ({marcas})) OR (r.nodo_b=n.id AND r.nodo_a IN ({marcas}))) AS grado "
                f"FROM nexus_nodos n WHERE n.id IN ({marcas}) ORDER BY grado DESC, n.creado_en, n.id LIMIT 1",
                resto * 3,
            )
            nuevo = cur.fetchone()[0]
            cur.execute("UPDATE nexus_nodos SET representante=%s WHERE representante=%s", (nuevo, rep))
            _nexus_historial(cur, resto, rep, nuevo,
                             f"representante eliminado ({tipo} {entidad_id}), promovido el mas conectado")
    cur.execute("DELETE FROM nexus_nodos WHERE id=%s", (nodo_id,))
    return nuevo


def _nexus_tx(fn, *args):
    conn = connect()
    try:
        with conn.cursor() as cur:
            resultado = fn(cur, *args)
        conn.commit()
        return resultado
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def nexus_crear_nodo(tipo: str, entidad_id) -> str:
    return _nexus_tx(_nexus_crear_nodo, tipo, entidad_id)


def nexus_relacionar(entidad_a: tuple, entidad_b: tuple) -> bool:
    return _nexus_tx(_nexus_relacionar, entidad_a, entidad_b)


def nexus_eliminar_nodo(tipo: str, entidad_id) -> str | None:
    return _nexus_tx(_nexus_eliminar_nodo, tipo, entidad_id)


def nexus_representante(tipo: str, entidad_id) -> str | None:
    """id del nodo representante del grupo de la entidad (None si no tiene nodo)."""
    fila = _nexus_tx(_nexus_nodo, tipo, entidad_id)
    return fila[1] if fila else None


def nexus_relacionados(tipo: str, entidad_id) -> list:
    """Todo lo que comparte grupo con la entidad (sin ella misma): lo que la
    pestaña Nexus enseña como "relacionado con esto". directo = hay arista
    explicita entre las dos."""
    def _consulta(cur):
        fila = _nexus_nodo(cur, tipo, entidad_id)
        if not fila:
            return []
        nodo_id, rep = fila
        cur.execute(
            "SELECT n.id, n.tipo, n.entidad_id, n.id = n.representante, "
            "EXISTS(SELECT 1 FROM nexus_relaciones r WHERE (r.nodo_a=n.id AND r.nodo_b=%s) "
            "OR (r.nodo_b=n.id AND r.nodo_a=%s)) "
            "FROM nexus_nodos n WHERE n.representante=%s AND n.id<>%s ORDER BY n.tipo, n.creado_en",
            (nodo_id, nodo_id, rep, nodo_id),
        )
        return [{"nodo_id": r[0], "tipo": r[1], "entidad_id": r[2], "es_representante": bool(r[3]),
                 "directo": bool(r[4])} for r in cur.fetchall()]
    return _nexus_tx(_consulta)


_NEXUS_TABLAS = {"proyecto": "proyectos", "sistema": "sistemas", "wiki_pagina": "wiki_paginas",
                 "nota": "notas", "whiteboard": "whiteboards", "empresa": "empresas",
                 "credencial": "credenciales"}


def nexus_sincronizar(real: bool = False) -> dict:
    """Relleno + limpieza, idempotente: crea el nodo de toda fila que aun no
    lo tenga (filas anteriores a nexus) y quita los nodos cuya fila ya no
    existe (borrada por un CASCADE, p. ej. notas de una reunion), pasando por
    _nexus_eliminar_nodo para que su grupo no se quede sin representante.
    Devuelve {tipo: (creados, quitados)}."""
    conn = connect(real=real)
    resultado = {}
    try:
        with conn.cursor() as cur:
            for tipo, tabla in _NEXUS_TABLAS.items():
                cur.execute(f"SELECT t.id FROM {tabla} t LEFT JOIN nexus_nodos n "
                            f"ON n.tipo=%s AND n.entidad_id=t.id WHERE n.id IS NULL", (tipo,))
                faltan = [r[0] for r in cur.fetchall()]
                for entidad_id in faltan:
                    _nexus_crear_nodo(cur, tipo, entidad_id)
                cur.execute(f"SELECT n.entidad_id FROM nexus_nodos n LEFT JOIN {tabla} t "
                            f"ON t.id=n.entidad_id WHERE n.tipo=%s AND t.id IS NULL", (tipo,))
                sobran = [r[0] for r in cur.fetchall()]
                for entidad_id in sobran:
                    _nexus_eliminar_nodo(cur, tipo, entidad_id)
                resultado[tipo] = (len(faltan), len(sobran))
        conn.commit()
        return resultado
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── conectores de entrada (modulo opcional, ver app_fastapi/conectores/) ─────

_CONECTOR_COLS = ("id, nombre, tipo, config, proyecto_id, estado_inicial, asignar_a, activo, intervalo_min, "
                  "ultima_sync, ultimo_resultado, ultimo_ok")


def _conector_row(r) -> dict:
    return {"conector_id": r[0], "nombre": r[1], "tipo": r[2], "config": r[3], "proyecto_id": r[4] or "",
            "estado_inicial": r[5], "asignar_a": r[6], "activo": bool(r[7]), "intervalo_min": r[8],
            "ultima_sync": _fmt_datetime(r[9]), "ultimo_resultado": r[10],
            "ultimo_ok": None if r[11] is None else bool(r[11])}


def load_conectores() -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_CONECTOR_COLS} FROM conectores ORDER BY nombre")
            return [_conector_row(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_conector(conector_id: str):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_CONECTOR_COLS} FROM conectores WHERE id=%s", (conector_id,))
            r = cur.fetchone()
            return _conector_row(r) if r else None
    finally:
        conn.close()


def guardar_conector(conector_id: str, nombre: str, tipo: str, config: str, proyecto_id: str, estado_inicial: str,
                     asignar_a, intervalo_min: int) -> None:
    """Alta o edicion (mismo id) -- no toca activo ni el estado de la ultima sincronizacion."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conectores (id, nombre, tipo, config, proyecto_id, estado_inicial, asignar_a, intervalo_min) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE nombre=VALUES(nombre), tipo=VALUES(tipo), "
                "config=VALUES(config), proyecto_id=VALUES(proyecto_id), estado_inicial=VALUES(estado_inicial), "
                "asignar_a=VALUES(asignar_a), intervalo_min=VALUES(intervalo_min)",
                (conector_id, nombre, tipo, config, proyecto_id or None, estado_inicial, asignar_a or None, intervalo_min),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def toggle_conector(conector_id: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE conectores SET activo = NOT activo WHERE id=%s", (conector_id,))
        conn.commit()
    finally:
        conn.close()


def eliminar_conector(conector_id: str) -> None:
    """Las tareas ya creadas se quedan; solo se olvida de que salieron de aqui."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM conectores WHERE id=%s", (conector_id,))
        conn.commit()
    finally:
        conn.close()


def conector_ids_importados(conector_id: str) -> set:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id_externo FROM conector_elementos WHERE conector_id=%s", (conector_id,))
            return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def registrar_conector_elemento(conector_id: str, id_externo: str, tarea_id: str) -> bool:
    """False si otro proceso ya lo habia registrado (la PK evita el duplicado)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            n = cur.execute("INSERT IGNORE INTO conector_elementos (conector_id, id_externo, tarea_id) VALUES (%s,%s,%s)",
                            (conector_id, id_externo, tarea_id))
        conn.commit()
        return n == 1
    finally:
        conn.close()


def marcar_sync_conector(conector_id: str, resultado: str, ok: bool) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE conectores SET ultima_sync=NOW(), ultimo_resultado=%s, ultimo_ok=%s WHERE id=%s",
                        (resultado[:500], ok, conector_id))
        conn.commit()
    finally:
        conn.close()


def conectores_pendientes() -> list:
    """Activos, con intervalo, y a los que ya les toca."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_CONECTOR_COLS} FROM conectores WHERE activo AND intervalo_min > 0 AND "
                        "(ultima_sync IS NULL OR ultima_sync <= NOW() - INTERVAL intervalo_min MINUTE)")
            return [_conector_row(r) for r in cur.fetchall()]
    finally:
        conn.close()
