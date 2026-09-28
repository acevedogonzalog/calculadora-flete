"""Almacenamiento de archivos en disco: facturas PDF y fotos de remitos.

Carpeta base: /data (en Railway es un Volume, así los archivos sobreviven a
cada deploy). Si /data no existe (por ejemplo en la compu) se usa ./data.
  /data/facturas   PDF de las facturas
  /data/remitos    fotos de los remitos (JPEG) y sus miniaturas
Se puede cambiar con DATA_DIR (o FACTURAS_DIR solo para las facturas).
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import re
from datetime import date
from pathlib import Path

from PIL import Image, ImageOps

try:  # fotos HEIC de iPhone
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:  # pragma: no cover
    pass

log = logging.getLogger("uvicorn.error")

NOMBRE_VALIDO = re.compile(r"^[A-Za-z0-9._-]{1,140}\.(pdf|jpg)$")
# Máximo de píxeles de una foto (una cámara de 108 MP entra). Más que eso se rechaza:
# evita "bombas" de imágenes que al abrirse ocupan gigas de memoria.
Image.MAX_IMAGE_PIXELS = 110_000_000
FOTO_MAX_LADO = 2400   # px: suficiente para leer un remito, sin archivos gigantes
MINI_LADO = 360


def _base() -> Path:
    if os.environ.get("DATA_DIR"):
        return Path(os.environ["DATA_DIR"])
    return Path("/data") if Path("/data").is_dir() else Path("data")


BASE = _base()
CARPETA = Path(os.environ["FACTURAS_DIR"]) if os.environ.get("FACTURAS_DIR") else BASE / "facturas"
REMITOS = BASE / "remitos"


def iniciar() -> None:
    CARPETA.mkdir(parents=True, exist_ok=True)
    REMITOS.mkdir(parents=True, exist_ok=True)
    en_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    if en_railway and not os.path.ismount("/data"):
        log.warning("ATENCIÓN: /data no es un Volume. Las facturas y remitos se van a borrar en el "
                    "próximo deploy. Creá un Volume en Railway con mount path /data.")
    log.info("Facturas guardadas en %s", CARPETA.resolve())
    log.info("Remitos guardados en %s", REMITOS.resolve())


def _limpio(texto: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]+", "-", texto or "").strip("-")[:40]


def _escribir(destino: Path, contenido: bytes) -> None:
    if destino.exists():
        return
    tmp = destino.with_name(destino.name + ".tmp")
    tmp.write_bytes(contenido)
    tmp.replace(destino)  # escritura atómica


# ---------- Facturas ----------

def guardar(contenido: bytes, fecha: str = "", nro: str = "") -> str:
    """Guarda el PDF y devuelve el nombre del archivo. Si ya existe el mismo PDF, no lo duplica."""
    huella = hashlib.sha256(contenido).hexdigest()[:12]
    partes = [p for p in (_limpio(fecha), _limpio(nro), huella) if p]
    nombre = "_".join(partes) + ".pdf"
    _escribir(CARPETA / nombre, contenido)
    return nombre


def ruta(nombre: str | None) -> Path | None:
    if not nombre or not NOMBRE_VALIDO.match(nombre) or not nombre.endswith(".pdf"):
        return None
    p = CARPETA / nombre
    return p if p.is_file() else None


# ---------- Remitos ----------

class FormatoNoSoportado(ValueError):
    pass


def _jpeg(img: Image.Image, lado: int, calidad: int) -> bytes:
    img = img.copy()
    img.thumbnail((lado, lado))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=calidad, optimize=True, progressive=True)
    return buf.getvalue()


def guardar_remito(contenido: bytes) -> dict:
    """Guarda una foto (o PDF) de remito. Las fotos se enderezan según la orientación
    del celular, se reducen a 2400 px y se guardan en JPEG, con una miniatura aparte."""
    huella = hashlib.sha256(contenido).hexdigest()[:12]
    base = f"{date.today().isoformat()}_{huella}"
    if contenido.startswith(b"%PDF"):
        nombre = base + ".pdf"
        _escribir(REMITOS / nombre, contenido)
        return {"archivo": nombre, "miniatura": "", "tipo": "pdf"}
    try:
        img = Image.open(io.BytesIO(contenido))
        if img.format == "JPEG":
            img.draft("RGB", (FOTO_MAX_LADO, FOTO_MAX_LADO))  # decodifica ya reducida: mucha menos memoria
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as e:
        raise FormatoNoSoportado("El archivo no es una foto ni un PDF.") from e
    nombre, mini = base + ".jpg", base + ".mini.jpg"
    _escribir(REMITOS / nombre, _jpeg(img, FOTO_MAX_LADO, 85))
    _escribir(REMITOS / mini, _jpeg(img, MINI_LADO, 75))
    return {"archivo": nombre, "miniatura": mini, "tipo": "foto"}


def ruta_remito(nombre: str | None) -> Path | None:
    if not nombre or not NOMBRE_VALIDO.match(nombre):
        return None
    p = REMITOS / nombre
    return p if p.is_file() else None
