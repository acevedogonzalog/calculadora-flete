"""Almacenamiento de archivos en disco, ordenado por usuario y por viaje.

    /data/                               (Volume de Railway)
      usuarios/
        0001-juan/
          LEEME.txt                      qué hay en cada carpeta
          perfil.json                    usuario, correo y fecha de alta (sin la contraseña)
          viajes/
            2026-09/
              viaje-00015_2026-09-28_servagrop-hdo-s-a/
                viaje.json               todos los datos del viaje (copia de respaldo)
                factura_00001-00000106.pdf
                remito-01.jpg  remito-01.mini.jpg
                remito-02.pdf
          pendientes/                    archivos subidos antes de tocar "Guardar viaje"
          eliminados/                    viajes eliminados (se mueven acá, no se borran)
      facturas/  remitos/                archivos de la versión anterior (quedan como respaldo)

Si /data no existe (por ejemplo en la compu) se usa ./data. Se puede cambiar con DATA_DIR.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

try:  # fotos HEIC de iPhone
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:  # pragma: no cover
    pass

log = logging.getLogger("uvicorn.error")

# Máximo de píxeles de una foto (una cámara de 108 MP entra). Más que eso se rechaza:
# evita "bombas" de imágenes que al abrirse ocupan gigas de memoria.
Image.MAX_IMAGE_PIXELS = 110_000_000
FOTO_MAX_LADO = 2400
MINI_LADO = 360
RUTA_VALIDA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,299}$")
NOMBRE_VIEJO = re.compile(r"^[A-Za-z0-9._-]{1,140}\.(pdf|jpg)$")


def _base() -> Path:
    if os.environ.get("DATA_DIR"):
        return Path(os.environ["DATA_DIR"])
    return Path("/data") if Path("/data").is_dir() else Path("data")


BASE = _base()
USUARIOS = BASE / "usuarios"
VIEJO_FACTURAS = BASE / "facturas"   # versión anterior (sin usuarios)
VIEJO_REMITOS = BASE / "remitos"

LEEME = """Carpeta de {usuario} — Calculadora de Flete

viajes/AAAA-MM/viaje-NNNNN_fecha_cliente/
    Un viaje por carpeta, agrupados por mes.
    viaje.json        todos los datos del viaje (se actualiza con cada cambio)
    factura_*.pdf     factura subida
    remito-NN.jpg     fotos del remito (y su miniatura .mini.jpg)

pendientes/   archivos subidos que todavía no se guardaron en un viaje
eliminados/   viajes eliminados desde la app (se guardan acá como respaldo)
perfil.json   datos de la cuenta (sin la contraseña)
"""


def iniciar() -> None:
    USUARIOS.mkdir(parents=True, exist_ok=True)
    en_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    if en_railway and not os.path.ismount("/data"):
        log.warning("ATENCIÓN: /data no es un Volume. Los archivos se van a borrar en el próximo deploy. "
                    "Creá un Volume en Railway con mount path /data.")
    log.info("Archivos de los usuarios en %s", USUARIOS.resolve())


def _slug(texto: str, largo: int = 30) -> str:
    t = (texto or "").lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n"), ("ü", "u")):
        t = t.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:largo].strip("-")


def _escribir(destino: Path, contenido: bytes) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_name(destino.name + ".tmp")
    tmp.write_bytes(contenido)
    tmp.replace(destino)  # escritura atómica


def _escribir_json(destino: Path, datos: dict) -> None:
    _escribir(destino, json.dumps(datos, ensure_ascii=False, indent=2, default=str).encode("utf-8"))


# ---------------- Carpeta del usuario ----------------

def raiz(u: dict) -> Path:
    return USUARIOS / u["carpeta"]


def preparar_usuario(u: dict) -> None:
    r = raiz(u)
    for sub in ("viajes", "pendientes", "eliminados"):
        (r / sub).mkdir(parents=True, exist_ok=True)
    if not (r / "LEEME.txt").exists():
        _escribir(r / "LEEME.txt", LEEME.format(usuario=u["usuario"]).encode("utf-8"))
    actualizar_perfil(u)


def actualizar_perfil(u: dict) -> None:
    _escribir_json(raiz(u) / "perfil.json", {
        "id": u["id"], "usuario": u["usuario"], "correo": u["correo"], "creado": u.get("creado"),
        "actualizado": datetime.now().isoformat(timespec="seconds")})


def ruta(u: dict, rel: str | None) -> Path | None:
    """Ruta absoluta de un archivo del usuario, solo si existe y está dentro de su carpeta."""
    if not rel or not RUTA_VALIDA.match(rel) or ".." in rel.split("/") or "//" in rel:
        return None
    base = raiz(u).resolve()
    p = (base / rel).resolve()
    if base not in p.parents or not p.is_file():
        return None
    return p


def uso(u: dict) -> dict:
    """Cantidad y tamaño de los archivos del usuario."""
    total = cant = 0
    for p in raiz(u).rglob("*"):
        if p.is_file() and p.suffix in (".pdf", ".jpg"):
            cant += 1
            total += p.stat().st_size
    return {"archivos": cant, "bytes": total}


# ---------------- Procesar fotos ----------------

class FormatoNoSoportado(ValueError):
    pass


def _jpeg(img: Image.Image, lado: int, calidad: int) -> bytes:
    img = img.copy()
    img.thumbnail((lado, lado))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=calidad, optimize=True, progressive=True)
    return buf.getvalue()


def procesar_foto(contenido: bytes) -> tuple[bytes, bytes]:
    """Endereza la foto según el celular, la reduce a 2400 px y arma una miniatura."""
    try:
        img = Image.open(io.BytesIO(contenido))
        if img.format == "JPEG":
            img.draft("RGB", (FOTO_MAX_LADO, FOTO_MAX_LADO))  # decodifica ya reducida: menos memoria
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as e:
        raise FormatoNoSoportado("El archivo no es una foto ni un PDF.") from e
    return _jpeg(img, FOTO_MAX_LADO, 85), _jpeg(img, MINI_LADO, 75)


# ---------------- Archivos pendientes (antes de guardar el viaje) ----------------

def guardar_pendiente_pdf(u: dict, contenido: bytes) -> str:
    rel = f"pendientes/factura-{hashlib.sha256(contenido).hexdigest()[:16]}.pdf"
    destino = raiz(u) / rel
    if not destino.exists():
        _escribir(destino, contenido)
    return rel


def guardar_pendiente_remito(u: dict, contenido: bytes) -> dict:
    h = "remito-" + hashlib.sha256(contenido).hexdigest()[:16]
    if contenido.startswith(b"%PDF"):
        rel = f"pendientes/{h}.pdf"
        _escribir(raiz(u) / rel, contenido)
        return {"archivo": rel, "miniatura": "", "tipo": "pdf"}
    foto, mini = procesar_foto(contenido)
    _escribir(raiz(u) / f"pendientes/{h}.jpg", foto)
    _escribir(raiz(u) / f"pendientes/{h}.mini.jpg", mini)
    return {"archivo": f"pendientes/{h}.jpg", "miniatura": f"pendientes/{h}.mini.jpg", "tipo": "foto"}


# ---------------- Carpeta de cada viaje ----------------

def carpeta_viaje(v: dict) -> str:
    fecha = (v.get("fecha") or v.get("creado") or datetime.now().isoformat())[:10]
    nombre = f"viaje-{v['id']:05d}_{fecha}"
    if _slug(v.get("cliente")):
        nombre += "_" + _slug(v.get("cliente"))
    return f"viajes/{fecha[:7]}/{nombre}"


def _libre(destino: Path) -> Path:
    """Si el nombre ya existe, agrega -2, -3…"""
    if not destino.exists():
        return destino
    i = 2
    while True:
        cand = destino.with_name(f"{destino.stem}-{i}{destino.suffix}")
        if not cand.exists():
            return cand
        i += 1


def ubicar(u: dict, origen: Path, carpeta_rel: str, nombre: str, mover: bool) -> str:
    """Lleva un archivo a la carpeta del viaje y devuelve su ruta relativa."""
    destino = _libre(raiz(u) / carpeta_rel / nombre)
    destino.parent.mkdir(parents=True, exist_ok=True)
    if mover:
        origen.replace(destino)
    else:
        shutil.copy2(origen, destino)
    return str(destino.relative_to(raiz(u)))


def nombre_factura(v: dict) -> str:
    nro = _slug(v.get("factura_nro"), 20)
    return f"factura_{nro}.pdf" if nro else "factura.pdf"


def siguiente_remito(u: dict, carpeta_rel: str) -> int:
    existentes = [int(m.group(1)) for p in (raiz(u) / carpeta_rel).glob("remito-*")
                  if (m := re.match(r"remito-(\d+)", p.name))]
    return max(existentes, default=0) + 1


def guardar_remito_en_viaje(u: dict, carpeta_rel: str, contenido: bytes) -> dict:
    n = siguiente_remito(u, carpeta_rel)
    base = raiz(u) / carpeta_rel
    if contenido.startswith(b"%PDF"):
        destino = _libre(base / f"remito-{n:02d}.pdf")
        _escribir(destino, contenido)
        return {"archivo": str(destino.relative_to(raiz(u))), "miniatura": "", "tipo": "pdf"}
    foto, mini = procesar_foto(contenido)
    destino = _libre(base / f"remito-{n:02d}.jpg")
    _escribir(destino, foto)
    mini_p = destino.with_name(destino.stem + ".mini.jpg")
    _escribir(mini_p, mini)
    return {"archivo": str(destino.relative_to(raiz(u))), "miniatura": str(mini_p.relative_to(raiz(u))), "tipo": "foto"}


def escribir_ficha(u: dict, v: dict) -> None:
    """viaje.json: copia legible de todos los datos del viaje dentro de su carpeta."""
    if not v.get("carpeta"):
        return
    ficha = {"usuario": u["usuario"], "actualizado": datetime.now().isoformat(timespec="seconds"), **v}
    _escribir_json(raiz(u) / v["carpeta"] / "viaje.json", ficha)


def archivar_eliminado(u: dict, v: dict) -> None:
    """Mueve la carpeta del viaje a eliminados/ con la fecha en que se borró."""
    ficha = {"usuario": u["usuario"], "eliminado": datetime.now().isoformat(timespec="seconds"), **v}
    origen = raiz(u) / v["carpeta"] if v.get("carpeta") else None
    marca = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    nombre = (Path(v["carpeta"]).name if v.get("carpeta") else f"viaje-{v['id']:05d}")
    destino = _libre(raiz(u) / "eliminados" / f"{marca}_{nombre}")
    if origen and origen.is_dir():
        destino.parent.mkdir(parents=True, exist_ok=True)
        origen.replace(destino)
    _escribir_json(destino / "viaje.json", ficha)


# ---------------- Archivos de la versión anterior ----------------

def ruta_vieja(nombre: str | None, tipo: str) -> Path | None:
    if not nombre or not NOMBRE_VIEJO.match(nombre):
        return None
    p = (VIEJO_FACTURAS if tipo == "factura" else VIEJO_REMITOS) / nombre
    return p if p.is_file() else None
