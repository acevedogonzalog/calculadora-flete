"""Almacenamiento de las facturas PDF en disco.

Carpeta por defecto: /data/facturas (en Railway, /data es un Volume, así los
archivos sobreviven a cada deploy). Si /data no existe (por ejemplo en la
compu), se usa ./data/facturas. Se puede cambiar con FACTURAS_DIR.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("uvicorn.error")

NOMBRE_VALIDO = re.compile(r"^[A-Za-z0-9._-]{1,120}\.pdf$")


def _carpeta() -> Path:
    if os.environ.get("FACTURAS_DIR"):
        return Path(os.environ["FACTURAS_DIR"])
    return Path("/data/facturas") if Path("/data").is_dir() else Path("data/facturas")


CARPETA = _carpeta()


def iniciar() -> None:
    CARPETA.mkdir(parents=True, exist_ok=True)
    en_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    if en_railway and not os.path.ismount("/data"):
        log.warning("ATENCIÓN: /data no es un Volume. Las facturas se van a borrar en el próximo deploy. "
                    "Creá un Volume en Railway con mount path /data.")
    log.info("Facturas guardadas en %s", CARPETA.resolve())


def _limpio(texto: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]+", "-", texto or "").strip("-")[:40]


def guardar(contenido: bytes, fecha: str = "", nro: str = "") -> str:
    """Guarda el PDF y devuelve el nombre del archivo. Si ya existe el mismo PDF, no lo duplica."""
    huella = hashlib.sha256(contenido).hexdigest()[:12]
    partes = [p for p in (_limpio(fecha), _limpio(nro), huella) if p]
    nombre = "_".join(partes) + ".pdf"
    destino = CARPETA / nombre
    if not destino.exists():
        tmp = destino.with_suffix(".tmp")
        tmp.write_bytes(contenido)
        tmp.replace(destino)  # escritura atómica
    return nombre


def ruta(nombre: str | None) -> Path | None:
    if not nombre or not NOMBRE_VALIDO.match(nombre):
        return None
    p = CARPETA / nombre
    return p if p.is_file() else None
