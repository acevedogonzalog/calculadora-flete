"""Respaldos automáticos de la base de datos en /data/respaldos.

- Se hace uno al arrancar, cada 5 minutos (solo si algo cambió) y al apagar
  (Railway apaga el contenedor viejo en cada deploy).
- Se guardan los últimos 40, comprimidos (.json.gz), legibles con cualquier programa.
- Si al arrancar la base está completamente vacía y hay un respaldo, se restaura solo.
  Cubre: base recreada, cambio de SQLite a PostgreSQL, o una base que se perdió.
  Nunca pisa datos: si la base tiene algo, no restaura.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, func, select, text

from . import db
from .almacen import BASE

log = logging.getLogger("uvicorn.error")
CARPETA = BASE / "respaldos"
GUARDAR = 40
TABLAS = (db.usuarios, db.viajes, db.remitos)   # sesiones y códigos son temporales: no se respaldan
VERSION = 1


def _serializar(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def volcar(engine=None) -> dict:
    engine = engine or db.engine
    datos = {"version": VERSION, "tablas": {}}
    with engine.connect() as con:
        for t in TABLAS:
            filas = con.execute(select(t).order_by(t.c.id)).mappings().all()
            datos["tablas"][t.name] = [{k: _serializar(v) for k, v in f.items()} for f in filas]
    return datos


def _huella(datos: dict) -> str:
    return hashlib.sha256(json.dumps(datos["tablas"], sort_keys=True, default=str).encode()).hexdigest()


def listar() -> list[Path]:
    if not CARPETA.is_dir():
        return []
    # por fecha real del archivo (el nombre no alcanza: "…-2" quedaría antes que el original)
    return sorted(CARPETA.glob("respaldo_*.json.gz"), key=lambda p: (p.stat().st_mtime_ns, p.name))


def ultimo() -> dict | None:
    archivos = listar()
    if not archivos:
        return None
    p = archivos[-1]
    return {"archivo": p.name, "fecha": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(),
            "cantidad": len(archivos), "bytes": p.stat().st_size}


def hacer_respaldo(motivo: str = "", engine=None) -> Path | None:
    """Escribe un respaldo si los datos cambiaron desde el último. Devuelve el archivo o None."""
    datos = volcar(engine)
    if not any(datos["tablas"].values()):
        return None  # base vacía: no hay nada que resguardar (y no se pisa un respaldo bueno)
    huella = _huella(datos)
    CARPETA.mkdir(parents=True, exist_ok=True)
    marca = CARPETA / ".ultima_huella"
    if marca.exists() and marca.read_text().strip() == huella:
        return None
    datos.update({"creado": datetime.now(timezone.utc).isoformat(), "motivo": motivo,
                  "resumen": {k: len(v) for k, v in datos["tablas"].items()}})
    base = f"respaldo_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
    destino, n = CARPETA / f"{base}.json.gz", 2
    while destino.exists():   # dos respaldos en el mismo segundo no se pisan
        destino, n = CARPETA / f"{base}-{n}.json.gz", n + 1
    tmp = destino.with_name(destino.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)   # contiene los hash de las contraseñas: solo lo lee la app
    tmp.replace(destino)
    marca.write_text(huella)
    for viejo in listar()[:-GUARDAR]:
        viejo.unlink(missing_ok=True)
    log.info("Respaldo guardado: %s (%s) %s", destino.name, motivo, datos["resumen"])
    return destino


def _convertir(col, v):
    if v is None:
        return None
    if isinstance(col.type, DateTime):
        d = datetime.fromisoformat(v)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    if isinstance(col.type, Date):
        return date.fromisoformat(v[:10])
    if isinstance(col.type, Boolean):
        return bool(v)
    if isinstance(col.type, Integer):
        return int(v)
    if isinstance(col.type, Float):
        return float(v)
    return v


def base_vacia(engine=None) -> bool:
    engine = engine or db.engine
    with engine.connect() as con:
        return all(con.execute(select(func.count()).select_from(t)).scalar_one() == 0 for t in TABLAS)


def restaurar(archivo: Path, engine=None) -> dict:
    engine = engine or db.engine
    with gzip.open(archivo, "rt", encoding="utf-8") as f:
        datos = json.load(f)
    resumen = {}
    with engine.begin() as con:
        for t in TABLAS:
            filas = datos["tablas"].get(t.name, [])
            cols = {c.name: c for c in t.columns}
            limpias = [{k: _convertir(cols[k], v) for k, v in fila.items() if k in cols} for fila in filas]
            if limpias:
                con.execute(t.insert(), limpias)
            resumen[t.name] = len(limpias)
            if engine.dialect.name == "postgresql" and limpias:
                # que los próximos ids sigan después de los restaurados
                con.execute(text(f"SELECT setval(pg_get_serial_sequence('{t.name}', 'id'), "
                                 f"(SELECT MAX(id) FROM {t.name}))"))
    return resumen


def restaurar_si_vacia(engine=None) -> dict | None:
    """Solo si la base no tiene NADA y existe un respaldo."""
    archivos = listar()
    if not archivos or not base_vacia(engine):
        return None
    for archivo in reversed(archivos):   # el más nuevo que se pueda leer
        try:
            resumen = restaurar(archivo, engine)
            log.warning("La base estaba vacía: se restauró el respaldo %s %s", archivo.name, resumen)
            return {"archivo": archivo.name, **resumen}
        except Exception:
            log.exception("No se pudo restaurar %s; se prueba con el anterior", archivo.name)
    return None
