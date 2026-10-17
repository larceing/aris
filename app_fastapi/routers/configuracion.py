# -*- coding: utf-8 -*-
"""Configuracion -- CRUD completo para las 9 pestanas (Empresas, Categorias,
Sistemas, Iniciativas, Dias especiales, Tipos de dia especial, Textos,
Mantis, Backups), copiado de core/views.py (empresa_*/categoria_global_*/
sistema_*/iniciativa_*/dia_especial_*/tipo_dia_especial_*/texto_config_edit/
mantis_config_save/backup_ahora) + core/templates/configuracion.html.

Simplificacion deliberada frente al original: alli las pestanas cambian con
JS sin recargar (todos los paneles llegan renderizados y ocultos). Aqui cada
pestana es un enlace normal a `?tipo=...` y el servidor decide que panel
mandar -- mismo patron de "formulario normal, sin AJAX" que ya se uso en
Notas, y evita tener que mandar los 9 paneles en cada respuesta."""
import hashlib
import json
import os
import secrets
import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.requests import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from .. import auth, calendario_sync_client, config, db, escritorio_servicios, logging_setup, mantis_client, mantis_csv_import
from ..flash import flash
from ..templating import templates

router = APIRouter(prefix="/configuracion", tags=["configuracion"], dependencies=[Depends(auth.requiere_it)])

# Misma paleta que PROJECT_PALETTE en core/models.py (la que usa el selector
# de color en toda esta pantalla).
COLOR_PALETTE = [
    "#2563eb", "#7c3aed", "#db2777", "#ea580c",
    "#16a34a", "#0891b2", "#9333ea", "#b45309",
    "#0f766e", "#c2410c",
]

TITULOS_TAB = {
    "categorias": "Categorías", "sistemas": "Sistemas", "iniciativas": "Iniciativas",
    "dias_especiales": "Días especiales", "tipos_dia_especial": "Tipos de día especial",
    "textos": "Textos", "mantis": "Mantis", "sistema": "Copias de seguridad", "api": "API",
    "calendario_sync": "Calendario externo", "nexus": "Nexus", "conectores": "Conectores",
    "recordatorio": "Recordatorio", "vigilante": "Vigilante de Mantis", "vigia": "Vigía de carpeta",
}

# Como traer el contenido de cada pestana -- un dict {tipo: funcion} en vez
# de un if/elif largo en la vista.
_CARGADORES = {
    "categorias": db.load_categorias_globales,
    "sistemas": db.load_sistemas,
    "iniciativas": db.load_iniciativas,
    "dias_especiales": db.load_dias_especiales,
    "tipos_dia_especial": db.load_tipos_dia_especial,
    "textos": db.load_textos,
    "mantis": db.load_mantis_ajustes,
    "sistema": db.load_backups,
    "api": db.load_api_tokens,
    "calendario_sync": lambda: [],  # panel propio, no usa valores_tab (ver ctx["calendario_sync_config"])
    "nexus": lambda: [],  # panel propio, no usa valores_tab (ver ctx["nexus_acceso"])
    "conectores": db.load_conectores,
    "recordatorio": lambda: db.load_recordatorio_mensajes(real=escritorio_servicios.USAR_BD_REAL),
    "vigilante": lambda: [],  # panel propio (ver ctx["esc_cfg"])
    "vigia": lambda: [],  # panel propio (ver ctx["esc_cfg"])
}


# pestañas que solo existen con su modulo opcional activo
_MODULO_DE_TAB = {"mantis": "mantis", "vigilante": "mantis", "nexus": "nexus", "conectores": "conectores",
                  "recordatorio": "escritorio", "vigia": "escritorio"}


def _redirect(tipo: str) -> RedirectResponse:
    return RedirectResponse(url=f"/configuracion/?tipo={tipo}", status_code=303)


@router.get("/")
def ver_configuracion(request: Request, tipo: str = "empresas", usuario: str = Depends(auth.requiere_login)):
    tab_activo = tipo if tipo in ("empresas", *TITULOS_TAB) else "empresas"
    if tab_activo in _MODULO_DE_TAB and not config.modulo_activo(_MODULO_DE_TAB[tab_activo]):
        tab_activo = "empresas"  # pestaña de un modulo apagado (ver config.py)
    conteos = db.get_conteos_pendientes()
    conteos["sistema"] = db.count_backups()  # el nav llama "sistema" a la pestana de Backups
    conteos["api"] = len(db.load_api_tokens())

    ctx = {
        "empresas": db.load_empresas(), "palette": COLOR_PALETTE,
        "titulos_tab": TITULOS_TAB, "conteos": conteos,
        "valores_tab": _CARGADORES[tab_activo]() if tab_activo != "empresas" else [],
        "tab_activo": tab_activo, "usuario": usuario, "activo": "configuracion",
    }
    if tab_activo == "dias_especiales":
        ctx["empresas_activas"] = [e for e in db.load_empresas() if e["activo"]]
        ctx["tipos_por_categoria"] = db.load_tipos_dia_especial_por_categoria()
    if tab_activo == "textos":
        ctx["textos_categorias"] = sorted({t["categoria"] for t in ctx["valores_tab"]})
    if tab_activo == "mantis":
        ctx["proyectos"] = db.load_proyectos_basico()
        mantis_destino = db.get_ajuste("mantis.proyecto_destino", "")
        ctx["mantis_proyecto_destino"] = mantis_destino
        ctx["mantis_pool_project_id"] = db.get_ajuste("mantis.pool_project_id", "1")
        ctx["mantis_umbral_ambar_horas"] = db.get_ajuste("mantis.umbral_ambar_horas", "24")
        ctx["mantis_umbral_rojo_horas"] = db.get_ajuste("mantis.umbral_rojo_horas", "72")
        ctx["mantis_watcher_pausado"] = db.get_ajuste("mantis.watcher_pausado", "") == "1"
        ctx["mantis_csv_estado_map"] = mantis_csv_import.mantis_csv_estado_map(
            db.get_ajuste("mantis_csv.estado_map", "{}")
        )
        ctx["mantis_destino_columnas"] = db.load_board_columns(mantis_destino) if mantis_destino else []
        try:
            ctx["mantis_usuario_map"] = json.loads(db.get_ajuste("mantis.usuario_map", "{}") or "{}")
        except (ValueError, TypeError):
            ctx["mantis_usuario_map"] = {}
        ctx["usuarios"] = db.load_usuarios()
    if tab_activo == "nexus":
        ctx["nexus_acceso"] = db.get_ajuste("nexus.acceso", "it")
    if tab_activo == "api":
        proyectos = db.load_proyectos_basico()
        nombre_por_id = {p["id"]: p["nombre"] for p in proyectos}
        for t in ctx["valores_tab"]:
            t["proyecto_nombre"] = nombre_por_id.get(t["proyecto_id"])
        ctx["proyectos"] = proyectos
        ctx["api_master_token_configurado"] = bool(os.getenv("API_IMPORT_TOKEN", "").strip())
    if tab_activo in ("recordatorio", "vigilante", "vigia"):
        ctx["esc_cfg"] = escritorio_servicios.config(tab_activo)
        ctx["esc_estado"] = escritorio_servicios.estado(tab_activo)
        ctx["esc_activos"] = escritorio_servicios.servicios_activos()
        ctx["esc_sonidos"] = escritorio_servicios.SONIDOS
        ctx["esc_dias"] = escritorio_servicios.DIAS
        ctx["entorno_test"] = db.entorno_actual() == "test"
        ctx["esc_iconos"] = sorted(p.name for p in escritorio_servicios.media_dir().iterdir() if p.is_file())
    if tab_activo == "recordatorio":
        ctx["esc_variables"] = escritorio_servicios.VARIABLES_RECORDATORIO
        ctx["recordatorio_categorias"] = sorted({m["categoria"] for m in ctx["valores_tab"] if m["categoria"]})
    if tab_activo == "vigilante":
        ctx["esc_variables"] = escritorio_servicios.VARIABLES_VIGILANTE
        ctx["empresas_iconos"] = [
            {**e, "icono": db.get_ajuste(f"vigilante.icono_empresa.{e['empresa_id']}", "", real=escritorio_servicios.USAR_BD_REAL)}
            for e in db.load_empresas()
        ]
    if tab_activo == "vigia":
        ctx["esc_variables"] = escritorio_servicios.VARIABLES_VIGIA
        ctx["vigia_usuarios"] = escritorio_servicios.usuarios_activos()
    if tab_activo == "conectores":
        from ..conectores import TIPOS, motor  # modulo opcional: solo se carga si se usa
        ctx["conector_tipos"] = TIPOS
        ctx["conector_proyectos"] = {p["id"]: p["nombre"] for p in db.load_proyectos_basico()}
        ctx["conector_usuarios"] = db.load_usuarios()
        editar, nuevo = request.query_params.get("editar", ""), request.query_params.get("nuevo", "")
        existente = db.get_conector(editar) if editar else None
        if existente and existente["tipo"] in TIPOS:
            ctx["conector_form"] = {**existente, "config": motor.config_guardada(existente)}
        elif nuevo in TIPOS:
            ctx["conector_form"] = {"conector_id": "", "tipo": nuevo, "nombre": TIPOS[nuevo].nombre, "config": {},
                                    "proyecto_id": "general", "estado_inicial": "", "asignar_a": None, "intervalo_min": 0}
    if tab_activo == "calendario_sync":
        ctx["calendario_sync_config"] = db.get_calendario_sync_config(usuario)
    return templates.TemplateResponse(request, "configuracion.html", ctx)


# ── empresas ─────────────────────────────────────────────────────────────────

@router.post("/empresa/nueva/")
def crear_empresa(
    nombre: str = Form(""), nombre_fiscal: str = Form(""), color: str = Form(""),
    mantis_project_id: str = Form(""), dominio_email: str = Form(""),
    usuario: str = Depends(auth.requiere_login),
):
    nombre = nombre.strip()
    if nombre:
        db.crear_empresa(
            empresa_id=str(uuid.uuid4())[:8], nombre=nombre,
            nombre_fiscal=nombre_fiscal.strip(), color=color or COLOR_PALETTE[0],
            mantis_project_id=mantis_project_id.strip(), dominio_email=dominio_email.strip(),
        )
    return _redirect("empresas")


@router.post("/empresa/{empresa_id}/editar/")
def editar_empresa(
    empresa_id: str, nombre: str = Form(None), nombre_fiscal: str = Form(None),
    color: str = Form(None), mantis_project_id: str = Form(None), dominio_email: str = Form(None),
    usuario: str = Depends(auth.requiere_login),
):
    actual = next((e for e in db.load_empresas() if e["empresa_id"] == empresa_id), None)
    if actual:
        db.editar_empresa(
            empresa_id=empresa_id,
            nombre=(nombre.strip() if nombre is not None else actual["nombre"]),
            nombre_fiscal=(nombre_fiscal.strip() if nombre_fiscal is not None else actual["nombre_fiscal"]),
            color=(color if color is not None else actual["color"]),
            mantis_project_id=(mantis_project_id.strip() if mantis_project_id is not None else actual["mantis_project_id"]),
            dominio_email=(dominio_email.strip() if dominio_email is not None else actual["dominio_email"]),
        )
    return _redirect("empresas")


@router.post("/empresa/{empresa_id}/toggle/")
def toggle_empresa(empresa_id: str, usuario: str = Depends(auth.requiere_login)):
    db.toggle_empresa(empresa_id)
    return _redirect("empresas")


# ── categorias globales ──────────────────────────────────────────────────────

@router.post("/categoria/nueva/")
def crear_categoria(nombre: str = Form(""), color: str = Form(""), usuario: str = Depends(auth.requiere_login)):
    nombre = nombre.strip()
    if nombre:
        db.crear_categoria(str(uuid.uuid4())[:8], nombre, color or COLOR_PALETTE[0])
    return _redirect("categorias")


@router.post("/categoria/{categoria_id}/editar/")
def editar_categoria(
    categoria_id: str, nombre: str = Form(None), color: str = Form(None),
    usuario: str = Depends(auth.requiere_login),
):
    actual = next((c for c in db.load_categorias_globales() if c["id"] == categoria_id), None)
    if actual:
        db.editar_categoria(
            categoria_id,
            nombre=(nombre.strip() if nombre is not None else actual["nombre"]),
            color=(color if color is not None else actual["color"]),
        )
    return _redirect("categorias")


@router.post("/categoria/{categoria_id}/toggle/")
def toggle_categoria(categoria_id: str, usuario: str = Depends(auth.requiere_login)):
    db.toggle_categoria(categoria_id)
    return _redirect("categorias")


# ── sistemas ─────────────────────────────────────────────────────────────────

@router.post("/sistema/nuevo/")
def crear_sistema(
    nombre: str = Form(""), empresa_id: str = Form(""), color: str = Form(""),
    usuario: str = Depends(auth.requiere_login),
):
    nombre = nombre.strip()
    if nombre:
        db.crear_sistema(str(uuid.uuid4())[:8], nombre, empresa_id.strip(), color or COLOR_PALETTE[0])
    return _redirect("sistemas")


@router.post("/sistema/{sistema_id}/editar/")
def editar_sistema(
    sistema_id: str, nombre: str = Form(None), empresa_id: str = Form(None),
    color: str = Form(None), usuario: str = Depends(auth.requiere_login),
):
    actual = next((s for s in db.load_sistemas() if s["id"] == sistema_id), None)
    if actual:
        db.editar_sistema(
            sistema_id,
            nombre=(nombre.strip() if nombre is not None else actual["nombre"]),
            empresa_id=(empresa_id.strip() if empresa_id is not None else ""),
            color=(color if color is not None else actual["color"]),
        )
    return _redirect("sistemas")


@router.post("/sistema/{sistema_id}/toggle/")
def toggle_sistema(sistema_id: str, usuario: str = Depends(auth.requiere_login)):
    db.toggle_sistema(sistema_id)
    return _redirect("sistemas")


# ── iniciativas ──────────────────────────────────────────────────────────────

@router.post("/iniciativa/nueva/")
def crear_iniciativa(
    nombre: str = Form(""), responsable: str = Form(""), color: str = Form(""),
    usuario: str = Depends(auth.requiere_login),
):
    nombre = nombre.strip()
    if nombre:
        db.crear_iniciativa(str(uuid.uuid4())[:8], nombre, responsable.strip(), color or COLOR_PALETTE[0])
    return _redirect("iniciativas")


@router.post("/iniciativa/{iniciativa_id}/editar/")
def editar_iniciativa(
    iniciativa_id: str, nombre: str = Form(None), responsable: str = Form(None),
    color: str = Form(None), usuario: str = Depends(auth.requiere_login),
):
    actual = next((i for i in db.load_iniciativas() if i["id"] == iniciativa_id), None)
    if actual:
        db.editar_iniciativa(
            iniciativa_id,
            nombre=(nombre.strip() if nombre is not None else actual["nombre"]),
            responsable=(responsable.strip() if responsable is not None else actual["responsable"]),
            color=(color if color is not None else actual["color"]),
        )
    return _redirect("iniciativas")


@router.post("/iniciativa/{iniciativa_id}/toggle/")
def toggle_iniciativa(iniciativa_id: str, usuario: str = Depends(auth.requiere_login)):
    db.toggle_iniciativa(iniciativa_id)
    return _redirect("iniciativas")


# ── tipos de dia especial ────────────────────────────────────────────────────

@router.post("/tipo-dia-especial/nuevo/")
def crear_tipo_dia_especial(
    nombre: str = Form(""), categoria: str = Form(""), color: str = Form(""),
    urgente: str = Form("false"), cuenta_para_sla: str = Form("false"),
    usuario: str = Depends(auth.requiere_login),
):
    nombre = nombre.strip()
    if nombre and categoria in db.CATEGORIAS_TIPO_DIA_ESPECIAL:
        db.crear_tipo_dia_especial(
            str(uuid.uuid4())[:8], nombre, categoria, color or COLOR_PALETTE[0],
            urgente == "true", cuenta_para_sla == "true",
        )
    return _redirect("tipos_dia_especial")


@router.post("/tipo-dia-especial/{tipo_id}/editar/")
def editar_tipo_dia_especial(
    tipo_id: str, nombre: str = Form(None), categoria: str = Form(None), color: str = Form(None),
    urgente: str = Form("false"), cuenta_para_sla: str = Form("false"),
    usuario: str = Depends(auth.requiere_login),
):
    actual = next((t for t in db.load_tipos_dia_especial() if t["id"] == tipo_id), None)
    if actual:
        nueva_categoria = categoria if categoria in db.CATEGORIAS_TIPO_DIA_ESPECIAL else actual["categoria"]
        db.editar_tipo_dia_especial(
            tipo_id,
            nombre=(nombre.strip() if nombre is not None else actual["nombre"]),
            categoria=nueva_categoria, color=(color if color is not None else actual["color"]),
            urgente=(urgente == "true"), cuenta_para_sla=(cuenta_para_sla == "true"),
        )
    return _redirect("tipos_dia_especial")


@router.post("/tipo-dia-especial/{tipo_id}/toggle/")
def toggle_tipo_dia_especial(tipo_id: str, usuario: str = Depends(auth.requiere_login)):
    db.toggle_tipo_dia_especial(tipo_id)
    return _redirect("tipos_dia_especial")


# ── dias especiales ──────────────────────────────────────────────────────────

@router.post("/dia-especial/nuevo/")
def crear_dia_especial(
    tipo: str = Form(""), fecha_inicio: str = Form(""), fecha_fin: str = Form(""),
    nota: str = Form(""), color: str = Form(""), usuario: str = Depends(auth.requiere_login),
):
    fecha_inicio = fecha_inicio.strip()
    if tipo and fecha_inicio:
        db.crear_dia_especial(str(uuid.uuid4())[:8], tipo, fecha_inicio, fecha_fin.strip(), nota.strip(), color)
    return _redirect("dias_especiales")


@router.post("/dia-especial/{dia_id}/editar/")
def editar_dia_especial(
    dia_id: str, tipo: str = Form(""), fecha_inicio: str = Form(""), fecha_fin: str = Form(""),
    nota: str = Form(None), color: str = Form(""), usuario: str = Depends(auth.requiere_login),
):
    db.editar_dia_especial(dia_id, tipo, fecha_inicio, fecha_fin, nota, color)
    return _redirect("dias_especiales")


@router.post("/dia-especial/{dia_id}/eliminar/")
def eliminar_dia_especial(dia_id: str, usuario: str = Depends(auth.requiere_login)):
    db.eliminar_dia_especial(dia_id)
    return _redirect("dias_especiales")


# ── textos de interfaz ───────────────────────────────────────────────────────

@router.post("/texto/{clave}/editar/")
def editar_texto(clave: str, valor: str = Form(""), usuario: str = Depends(auth.requiere_login)):
    db.editar_texto(clave, valor)
    return _redirect("textos")


# ── mantis (ajustes_app) ─────────────────────────────────────────────────────

@router.post("/mantis/guardar/")
def guardar_mantis(
    request: Request,
    proyecto_destino: str = Form(""), pool_project_id: str = Form("1"),
    umbral_ambar_horas: str = Form("24"), umbral_rojo_horas: str = Form("72"),
    usuario: str = Depends(auth.requiere_login),
):
    db.set_ajuste("mantis.proyecto_destino", proyecto_destino.strip())
    db.set_ajuste("mantis.pool_project_id", pool_project_id.strip() or "1")
    db.set_ajuste("mantis.umbral_ambar_horas", umbral_ambar_horas.strip() or "24")
    db.set_ajuste("mantis.umbral_rojo_horas", umbral_rojo_horas.strip() or "72")
    flash(request, "success", "Proyecto destino de Mantis actualizado.")
    return _redirect("mantis")


@router.post("/mantis/vigilante/")
def mantis_watcher_toggle(request: Request, usuario: str = Depends(auth.requiere_login)):
    """Pausa/reanuda scripts/mantis_watcher.py -- NO mata ningun proceso
    desde aqui (ni desde Django ni desde esta v2), esta ruta solo escribe un
    ajuste en ajustes_app (compartido con la app real) que el propio script
    comprueba antes de llamar a Mantis en cada ciclo. Igual que
    core/views.py::mantis_watcher_toggle -- efecto en el siguiente ciclo del
    watcher (hasta 5 min), no instantaneo."""
    pausado_actual = escritorio_servicios.config("vigilante")["activo"] != "1"
    escritorio_servicios.set_activo("vigilante", pausado_actual)
    if pausado_actual:
        flash(request, "success", "Vigilante de Mantis reanudado.")
    else:
        flash(request, "success", "Vigilante de Mantis pausado: no hará ninguna llamada a Mantis hasta que lo reanudes.")
    return _redirect("mantis")


@router.post("/mantis/csv-estado-map/")
async def mantis_csv_estado_map_save(request: Request, usuario: str = Depends(auth.requiere_login)):
    """Guarda el diccionario estado-de-Mantis -> columna-del-tablero usado
    por mantis_csv_import.import_tasks_from_mantis_csv (ajustes_app, clave
    'mantis_csv.estado_map', JSON) -- igual que
    core/views.py::mantis_csv_estado_map_save. El formulario manda filas
    paralelas estado[]/columna[] (ver configuracion.html); una fila con
    cualquiera de los dos campos vacio se descarta. Usa request.form()
    directamente (no Form(...) con nombre fijo) porque son listas paralelas
    de longitud variable, no campos sueltos."""
    form = await request.form()
    estados = form.getlist("estado[]")
    columnas = form.getlist("columna[]")
    mapa = {}
    for estado, columna in zip(estados, columnas):
        estado, columna = str(estado).strip(), str(columna).strip()
        if estado and columna:
            mapa[estado] = columna
    db.set_ajuste("mantis_csv.estado_map", json.dumps(mapa, ensure_ascii=False))
    flash(request, "success", "Mapeo de estados de Mantis guardado.")
    return _redirect("mantis")


@router.post("/mantis/usuario-map/")
async def mantis_usuario_map_save(request: Request, usuario: str = Depends(auth.requiere_login)):
    """Guarda el diccionario usuario/login-de-Mantis -> user_id local usado
    por routers/mantis.py::usuario_map() para asignar una incidencia
    importada cuyo handler no sea 'yo' -- mismo patron de filas paralelas
    que mantis_csv_estado_map_save (mantis_usuario[]/user_id[])."""
    form = await request.form()
    mantis_usuarios = form.getlist("mantis_usuario[]")
    user_ids = form.getlist("user_id[]")
    mapa = {}
    for mantis_usuario, user_id in zip(mantis_usuarios, user_ids):
        mantis_usuario, user_id = str(mantis_usuario).strip(), str(user_id).strip()
        if mantis_usuario and user_id:
            mapa[mantis_usuario] = user_id
    db.set_ajuste("mantis.usuario_map", json.dumps(mapa, ensure_ascii=False))
    flash(request, "success", "Mapeo de usuarios de Mantis guardado.")
    return _redirect("mantis")


# ── nexus (ajustes_app) ──────────────────────────────────────────────────────

@router.post("/nexus/guardar/")
def guardar_nexus(request: Request, acceso: str = Form("it"), usuario: str = Depends(auth.requiere_login)):
    """Quien puede VER la pestaña Nexus y buscar -- ver
    routers/nexus.py::_requiere_acceso_nexus. El modo dev y las credenciales
    cifradas siguen siendo solo IT pase lo que pase aqui, este ajuste solo
    afecta a la busqueda/preview normal."""
    db.set_ajuste("nexus.acceso", acceso if acceso in ("todos", "it") else "it")
    flash(request, "success", "Acceso a Nexus actualizado.")
    return _redirect("nexus")


@router.post("/mantis/csv-importar/")
async def mantis_csv_importar(request: Request, usuario: str = Depends(auth.requiere_login), csv_mantis: UploadFile = File(None)):
    """Importa de una sola vez el .csv que exporta la propia Mantis (no la
    plantilla de Excel de la app) al proyecto configurado en Configuracion
    -> Mantis -- pensado para volcar historico completo (cientos de
    incidencias), algo que la API REST no ofrece en un unico fichero
    descargable. Igual que core/views.py::mantis_csv_importar, incluidos
    los mismos 4 mensajes flash (proyecto sin configurar / sin archivo /
    proyecto borrado / resultado final con avisos)."""
    proyecto_id = db.get_ajuste("mantis.proyecto_destino", "")
    if not proyecto_id:
        flash(request, "error", "configura el proyecto destino en Configuración → Mantis")
        return _redirect("mantis")
    if not csv_mantis or not csv_mantis.filename:
        flash(request, "error", "No se ha seleccionado ningún archivo.")
        return _redirect("mantis")
    if not db.get_proyecto(proyecto_id):
        flash(request, "error", "el proyecto destino configurado ya no existe")
        return _redirect("mantis")

    iniciativa_id = db.find_or_crear_iniciativa_mantis()
    columnas = db.load_board_columns(proyecto_id)
    default_status = next((c["nombre"] for c in columnas if not c["es_done"]), "Pendiente")
    done_column_names = {c["nombre"] for c in columnas if c["es_done"]}
    estado_map = mantis_csv_import.mantis_csv_estado_map(db.get_ajuste("mantis_csv.estado_map", "{}"))
    base_url = mantis_client._base_url() if os.getenv("MANTIS_BASE_URL", "").strip() else ""

    try:
        nuevas, avisos = mantis_csv_import.import_tasks_from_mantis_csv(
            csv_mantis.file, csv_mantis.filename or "", csv_mantis.size or 0,
            [c["nombre"] for c in db.load_categorias_globales()],
            db.load_empresas(), estado_map, default_status, iniciativa_id,
            done_column_names, db.tareas_mantis_ids_importadas(), base_url,
        )
        for t in nuevas:
            db.crear_tarea(
                str(uuid.uuid4())[:8], proyecto_id, t["category"], t["title"], t["status"], t["priority"],
                t["description"], t["next_step"], t["fecha_inicio"], t["fecha_fin"],
                empresa_id=t["empresa_id"], iniciativa_id=t["iniciativa_id"],
                mantis_id=t["mantis_id"], cerrada_en=t["cerrada_en"],
            )
        if nuevas:
            flash(request, "success", f"{len(nuevas)} tareas importadas desde el CSV de Mantis.")
        else:
            flash(request, "warning", "No se importó ninguna tarea nueva (¿ya estaban todas importadas?).")
        if avisos:
            texto = "; ".join(avisos[:8])
            if len(avisos) > 8:
                texto += f" (+{len(avisos) - 8} avisos más)"
            flash(request, "warning", f"{len(avisos)} filas con avisos: {texto}")
    except ValueError as e:
        logging_setup.errores.error(f"{usuario} | POST /configuracion/mantis/csv-importar/ -> {e}")
        flash(request, "error", f"No se pudo importar el CSV: {e}")

    return _redirect("mantis")


# ── backups ──────────────────────────────────────────────────────────────────

@router.post("/backup/nuevo/")
def crear_backup(request: Request, usuario: str = Depends(auth.requiere_login)):
    ok, mensaje = db.crear_backup()
    flash(request, "success" if ok else "error", mensaje)
    if not ok:
        logging_setup.errores.error(f"{usuario} | POST /configuracion/backup/nuevo/ -> {mensaje}")
    return _redirect("sistema")


# ── api_tokens (Configuracion -> API) ───────────────────────────────────────
# Igual que core/views.py::api_token_new/api_token_delete. El valor en claro
# del token solo se genera y se muestra AQUI, en la propia respuesta de esta
# ruta -- se guarda solo su hash (ver db.crear_api_token), no hay forma de
# volver a verlo despues, solo revocarlo y crear uno nuevo. Sin sistema de
# mensajes flash todavia (ver README), asi que en vez de redirigir con el
# token en la URL (se veria en el historial del navegador) esta ruta
# devuelve directamente una pagina de confirmacion dedicada.

@router.post("/api/token/nuevo/")
def api_token_nuevo(request: Request, nombre: str = Form(""), proyecto_id: str = Form(""), usuario: str = Depends(auth.requiere_login)):
    nombre = nombre.strip() or "Sin nombre"
    proyecto_id = proyecto_id.strip() or None
    if proyecto_id and not db.get_proyecto(proyecto_id):
        flash(request, "error", "el proyecto elegido ya no existe")
        return _redirect("api")

    token_plano = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token_plano.encode()).hexdigest()
    db.crear_api_token(str(uuid.uuid4())[:8], nombre, token_hash, proyecto_id, user_id=db.get_user_id(usuario))
    return templates.TemplateResponse(request, "api_token_creado.html", {
        "usuario": usuario, "activo": "configuracion",
        "nombre": nombre, "token_plano": token_plano,
        "api_url": str(request.base_url).rstrip("/") + "/api",
    })


@router.post("/api/token/{token_id}/eliminar/")
def api_token_eliminar(request: Request, token_id: str, usuario: str = Depends(auth.requiere_login)):
    db.eliminar_api_token(token_id)
    flash(request, "success", "Token revocado.")
    return _redirect("api")


# ── calendario externo (feed ICS -> reuniones_calendario) ───────────────────
# Igual que core/views.py::calendario_sync_config_save/calendario_sync_ahora.
# Solo guarda la URL del feed aqui; la sincronizacion en si vive en
# core/calendario_sync_client.py (reutilizado directo, sin nada de Django),
# disparada a mano con "Sincronizar ahora" -- nada automatico en segundo plano.

@router.post("/calendario-sync/guardar/")
def calendario_sync_config_save(request: Request, ics_url: str = Form(""), usuario: str = Depends(auth.requiere_login)):
    db.save_calendario_sync_config(usuario, ics_url.strip())
    flash(request, "success", "Configuración de sincronización del calendario actualizada.")
    return _redirect("calendario_sync")


@router.post("/calendario-sync/sincronizar/")
def calendario_sync_ahora(usuario: str = Depends(auth.requiere_login)):
    cfg = db.get_calendario_sync_config(usuario)
    if not cfg or not cfg["ics_url"]:
        return JSONResponse({"ok": False, "error": "Configura primero la URL del feed ICS."}, status_code=400)
    try:
        eventos = calendario_sync_client.fetch_eventos(cfg["ics_url"])
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    db.upsert_reuniones_calendario(usuario, eventos)
    return JSONResponse({"ok": True, "total": len(eventos)})


# ── avisos de escritorio: Recordatorio + Vigilante de Mantis ─────────────────
#
# Antes eran dos procesos aparte (APP_RECORDATORIO y scripts/mantis_watcher.py),
# ahora son hilos de la propia app -- ver escritorio_servicios.py. Todo lo de
# aqui va siempre a la BD real (escritorio_servicios.USAR_BD_REAL), igual que
# los propios servicios.

_ICONO_EXT = {".png", ".jpg", ".jpeg", ".ico", ".bmp", ".gif"}
_ICONO_MAX_BYTES = 2 * 1024 * 1024


def _real() -> bool:
    return escritorio_servicios.USAR_BD_REAL


def _hora_valida(valor: str, defecto: str) -> str:
    valor = (valor or "").strip()
    try:
        h, m = valor.split(":")
        if 0 <= int(h) <= 23 and 0 <= int(m) <= 59:
            return f"{int(h):02d}:{int(m):02d}"
    except ValueError:
        pass
    return defecto


async def _guardar_comun(request: Request, seccion: str):
    """Campos que comparten los dos paneles (intervalo, textos, sonido,
    horario...). Devuelve el form por si la seccion necesita mas campos."""
    form = await request.form()
    defectos = escritorio_servicios.DEFAULTS[seccion]
    try:
        intervalo = max(1, min(24 * 60, int(str(form.get("intervalo_min", "")).strip())))
    except ValueError:
        intervalo = int(defectos["intervalo_min"])
    sonido = str(form.get("sonido", "default"))
    valores = {
        "intervalo_min": intervalo,
        "app_id": str(form.get("app_id", "")).strip()[:80] or defectos["app_id"],
        "titulo": str(form.get("titulo", "")).strip()[:120] or defectos["titulo"],
        "duracion": "long" if form.get("duracion") == "long" else "short",
        "sonido": sonido if sonido in escritorio_servicios.SONIDOS else "default",
        "solo_horario": "1" if form.get("solo_horario") else "",
        "hora_inicio": _hora_valida(str(form.get("hora_inicio", "")), defectos["hora_inicio"]),
        "hora_fin": _hora_valida(str(form.get("hora_fin", "")), defectos["hora_fin"]),
        "dias": ",".join(d for d in form.getlist("dias") if d in "1234567") or defectos["dias"],
    }
    escritorio_servicios.guardar_config(seccion, valores)
    return form


def _guardar_icono(upload: UploadFile, nombre_base: str) -> str:
    """Guarda la imagen en media/escritorio/ con un nombre propio (nunca el
    del usuario) y devuelve ese nombre, o lanza ValueError."""
    ext = os.path.splitext(upload.filename or "")[1].lower()
    if ext not in _ICONO_EXT:
        raise ValueError("formato no admitido (usa PNG, JPG, ICO, BMP o GIF)")
    data = upload.file.read(_ICONO_MAX_BYTES + 1)
    if len(data) > _ICONO_MAX_BYTES:
        raise ValueError("la imagen pesa más de 2 MB")
    nombre = f"{nombre_base}_{uuid.uuid4().hex[:6]}{ext}"
    (escritorio_servicios.media_dir() / nombre).write_bytes(data)
    return nombre


@router.get("/escritorio/icono/{nombre}")
def escritorio_icono(nombre: str, usuario: str = Depends(auth.requiere_login)):
    ruta = escritorio_servicios.ruta_icono(nombre)
    if not ruta:
        return Response(status_code=404)
    return FileResponse(ruta)


@router.post("/recordatorio/activo/")
def recordatorio_toggle(request: Request, usuario: str = Depends(auth.requiere_login)):
    activo = escritorio_servicios.config("recordatorio")["activo"] == "1"
    escritorio_servicios.set_activo("recordatorio", not activo)
    flash(request, "success", "Recordatorio pausado." if activo else "Recordatorio activado.")
    return _redirect("recordatorio")


@router.post("/vigilante/activo/")
def vigilante_toggle(request: Request, usuario: str = Depends(auth.requiere_login)):
    activo = escritorio_servicios.config("vigilante")["activo"] == "1"
    escritorio_servicios.set_activo("vigilante", not activo)
    flash(request, "success", "Vigilante de Mantis pausado: no hará ninguna llamada a Mantis hasta que lo reanudes."
          if activo else "Vigilante de Mantis reanudado.")
    return _redirect("vigilante")


@router.post("/recordatorio/probar/")
def recordatorio_probar(request: Request, usuario: str = Depends(auth.requiere_login)):
    try:
        flash(request, "success", f"Aviso de prueba: {escritorio_servicios.enviar_recordatorio(forzar=True)}")
    except Exception as e:  # winotify / Windows puede fallar por muchas cosas -- que se vea en pantalla
        flash(request, "error", f"No se pudo mostrar el aviso: {e}")
    return _redirect("recordatorio")


@router.post("/vigilante/probar/")
def vigilante_probar(request: Request, empresa_id: str = Form(""), usuario: str = Depends(auth.requiere_login)):
    try:
        flash(request, "success", f"Aviso de prueba: {escritorio_servicios.probar_vigilante(empresa_id)}")
    except Exception as e:
        flash(request, "error", f"No se pudo mostrar el aviso: {e}")
    return _redirect("vigilante")


@router.post("/recordatorio/guardar/")
async def recordatorio_guardar(request: Request, usuario: str = Depends(auth.requiere_login)):
    form = await _guardar_comun(request, "recordatorio")
    orden = str(form.get("orden", "aleatorio"))
    escritorio_servicios.guardar_config("recordatorio", {
        "orden": orden if orden in ("aleatorio", "rotacion", "orden") else "aleatorio",
        "saltar_festivos": "1" if form.get("saltar_festivos") else "",
    })
    flash(request, "success", "Recordatorio guardado. Se aplica en la siguiente vuelta, sin reiniciar nada.")
    return _redirect("recordatorio")


@router.post("/recordatorio/icono/")
def recordatorio_icono(request: Request, icono: UploadFile = File(None), existente: str = Form(""),
                       usuario: str = Depends(auth.requiere_login)):
    try:
        if icono and icono.filename:
            nombre = _guardar_icono(icono, "recordatorio")
        else:
            nombre = existente if escritorio_servicios.ruta_icono(existente) else ""
        escritorio_servicios.guardar_config("recordatorio", {"icono": nombre})
        flash(request, "success", "Icono del Recordatorio actualizado." if nombre else "Recordatorio sin icono.")
    except ValueError as e:
        flash(request, "error", str(e))
    return _redirect("recordatorio")


@router.post("/recordatorio/mensaje/nuevo/")
def recordatorio_mensaje_nuevo(request: Request, texto: str = Form(""), categoria: str = Form(""),
                               usuario: str = Depends(auth.requiere_login)):
    """Una linea = un mensaje: se puede pegar una lista entera de golpe."""
    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    for linea in lineas:
        db.crear_recordatorio_mensaje(linea[:500], categoria.strip()[:50], real=_real())
    if lineas:
        flash(request, "success", f"{len(lineas)} mensaje(s) añadido(s).")
    return _redirect("recordatorio")


@router.post("/recordatorio/mensaje/{mensaje_id}/editar/")
def recordatorio_mensaje_editar(request: Request, mensaje_id: int, texto: str = Form(""), categoria: str = Form(""),
                                usuario: str = Depends(auth.requiere_login)):
    if texto.strip():
        db.editar_recordatorio_mensaje(mensaje_id, texto.strip()[:500], categoria.strip()[:50], real=_real())
        flash(request, "success", "Mensaje actualizado.")
    return _redirect("recordatorio")


@router.post("/recordatorio/mensaje/{mensaje_id}/toggle/")
def recordatorio_mensaje_toggle(mensaje_id: int, usuario: str = Depends(auth.requiere_login)):
    db.toggle_recordatorio_mensaje(mensaje_id, real=_real())
    return _redirect("recordatorio")


@router.post("/recordatorio/mensaje/{mensaje_id}/eliminar/")
def recordatorio_mensaje_eliminar(request: Request, mensaje_id: int, usuario: str = Depends(auth.requiere_login)):
    db.eliminar_recordatorio_mensaje(mensaje_id, real=_real())
    flash(request, "success", "Mensaje eliminado.")
    return _redirect("recordatorio")


@router.post("/vigilante/guardar/")
async def vigilante_guardar(request: Request, usuario: str = Depends(auth.requiere_login)):
    form = await _guardar_comun(request, "vigilante")
    escritorio_servicios.guardar_config("vigilante", {
        "formato": str(form.get("formato", "")).strip()[:500] or escritorio_servicios.DEFAULTS["vigilante"]["formato"],
        "abrir_al_pulsar": "1" if form.get("abrir_al_pulsar") else "",
    })
    flash(request, "success", "Vigilante de Mantis guardado. Se aplica en la siguiente revisión, sin reiniciar nada.")
    return _redirect("vigilante")


@router.post("/vigilante/revisar/")
def vigilante_revisar(request: Request, usuario: str = Depends(auth.requiere_login)):
    try:
        flash(request, "success", f"Revisión hecha: {escritorio_servicios.revisar_mantis()}")
    except Exception as e:
        flash(request, "error", f"No se pudo revisar Mantis: {e}")
    return _redirect("vigilante")


@router.post("/vigilante/icono/")
def vigilante_icono(request: Request, destino: str = Form(""), icono: UploadFile = File(None),
                    existente: str = Form(""), usuario: str = Depends(auth.requiere_login)):
    """destino: 'pool' (sin triar), 'defecto' o el id de una empresa."""
    if destino in ("pool", "defecto"):
        clave = f"vigilante.icono_{destino}"
    elif destino in {e["empresa_id"] for e in db.load_empresas()}:
        clave = f"vigilante.icono_empresa.{destino}"
    else:
        return _redirect("vigilante")
    try:
        if icono and icono.filename:
            nombre = _guardar_icono(icono, f"vigilante_{destino}")
        else:
            nombre = existente if escritorio_servicios.ruta_icono(existente) else ""
        db.set_ajuste(clave, nombre, real=_real())
        flash(request, "success", "Icono actualizado." if nombre else "Icono quitado.")
    except ValueError as e:
        flash(request, "error", str(e))
    return _redirect("vigilante")


# ── Vigia de carpeta (carpeta configurable) ──────────────────────────────────────────

@router.post("/vigia/activo/")
def vigia_toggle(request: Request, usuario: str = Depends(auth.requiere_login)):
    activo = escritorio_servicios.config("vigia")["activo"] == "1"
    escritorio_servicios.set_activo("vigia", not activo)
    flash(request, "success", "Vigía de carpeta pausado." if activo else "Vigía de carpeta activado.")
    return _redirect("vigia")


@router.post("/vigia/probar/")
def vigia_probar(request: Request, usuario: str = Depends(auth.requiere_login)):
    try:
        flash(request, "success", f"Aviso de prueba: {escritorio_servicios.probar_vigia()}")
    except Exception as e:
        flash(request, "error", f"No se pudo enviar el aviso: {e}")
    return _redirect("vigia")


@router.post("/vigia/revisar/")
def vigia_revisar(request: Request, usuario: str = Depends(auth.requiere_login)):
    try:
        flash(request, "success", f"Revisión hecha: {escritorio_servicios.revisar_vigia()}")
    except Exception as e:
        flash(request, "error", f"No se pudo revisar la carpeta vigilada: {e}")
    return _redirect("vigia")


@router.post("/vigia/guardar/")
async def vigia_guardar(request: Request, usuario: str = Depends(auth.requiere_login)):
    form = await _guardar_comun(request, "vigia")
    defectos = escritorio_servicios.DEFAULTS["vigia"]
    try:
        minutos = max(1, min(24 * 60, int(str(form.get("minutos_atasco", "")).strip())))
    except ValueError:
        minutos = int(defectos["minutos_atasco"])
    destinatario = str(form.get("destinatario", "")).strip()
    escritorio_servicios.guardar_config("vigia", {
        "destinatario": destinatario if destinatario in escritorio_servicios.usuarios_activos() else "",
        "carpeta": str(form.get("carpeta", "")).strip()[:250] or defectos["carpeta"],
        "carpetas_entrada": str(form.get("carpetas_entrada", "")).strip()[:250] or defectos["carpetas_entrada"],
        "carpeta_fallidos": str(form.get("carpeta_fallidos", "")).strip()[:100] or defectos["carpeta_fallidos"],
        "minutos_atasco": minutos,
        "toast": "1" if form.get("toast") else "",
    })
    flash(request, "success", "Vigía de carpeta guardado. Se aplica en la siguiente revisión, sin reiniciar nada.")
    return _redirect("vigia")


@router.post("/vigia/icono/")
def vigia_icono(request: Request, icono: UploadFile = File(None), existente: str = Form(""),
              usuario: str = Depends(auth.requiere_login)):
    try:
        if icono and icono.filename:
            nombre = _guardar_icono(icono, "vigia")
        else:
            nombre = existente if escritorio_servicios.ruta_icono(existente) else ""
        escritorio_servicios.guardar_config("vigia", {"icono": nombre})
        flash(request, "success", "Icono del Vigía de carpeta actualizado." if nombre else "Vigía de carpeta sin icono.")
    except ValueError as e:
        flash(request, "error", str(e))
    return _redirect("vigia")
