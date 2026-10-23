# -*- coding: utf-8 -*-
"""Cada espacio, con sus proyectos, tareas, wiki y muro, solo lo ven sus
miembros (y el dueño de un espacio personal). IT lo ve todo.

Dependencia global de main.py, igual que csrf.proteger:
  1. fija en db.py los espacios que puede ver quien hace la peticion, y los
     listados (sidebar, portada, calendario, buscador...) se filtran solos;
  2. si la ruta nombra algo concreto (proyecto, tarea, columna, documento,
     sprint, hito, pagina de wiki o espacio) de un espacio que no puede ver,
     responde 404, como si no existiera.

Sin sesion (la API con llave, /login, estaticos) no filtra nada: la llave de
la API ya limita a que proyecto llega."""
from fastapi import Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.requests import Request

from . import db

# parametro de ruta -> que es (clave fija de db._ESPACIO_DE)
PARAMETROS = {
    "project_id": "proyecto",
    "proyecto_id": "proyecto",
    "espacio_id": "espacio",
    "task_id": "tarea",
    "col_id": "columna",
    "doc_id": "documento",
    "sprint_id": "sprint",
    "milestone_id": "hito",
    "page_id": "wiki",
}


def _calcular(usuario: str, es_it: bool, path_params: dict):
    """(espacios visibles o None si lo ve todo, True si la ruta nombra algo que no ve)."""
    user_id = db.get_user_id(usuario)
    if es_it or user_id is None or db.es_it_usuario(user_id):
        return None, False
    visibles = db.espacios_de_usuario(user_id)
    for nombre, tipo in PARAMETROS.items():
        valor = path_params.get(nombre)
        if not valor:
            continue
        existe, espacio_id = db.espacio_de(tipo, str(valor))
        if existe and espacio_id and espacio_id not in visibles:
            return visibles, True
    return visibles, False


def sesion(request: Request) -> tuple:
    """(usuario, es_it) de la sesion, o (None, False) sin sesion. Aparte para
    que los tests puedan hacerse pasar por alguien."""
    if "session" not in request.scope:
        return None, False
    return request.session.get("usuario"), bool(request.session.get("es_it"))


async def proteger(request: Request, quien: tuple = Depends(sesion)) -> None:
    usuario, es_it = quien
    if not usuario:
        db.fijar_visor(None)
        return
    # async (no def) a proposito: asi el ContextVar se fija en el contexto de
    # la peticion y la ruta, que corre despues en el threadpool, lo hereda.
    visibles, oculto = await run_in_threadpool(
        _calcular, usuario, es_it, dict(request.path_params)
    )
    db.fijar_visor(visibles)
    if oculto:
        raise HTTPException(status_code=404, detail="No existe o no eres miembro de su espacio.")
