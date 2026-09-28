"""Servidor web de la Calculadora de Flete (FastAPI)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path as FsPath
from typing import Annotated

from fastapi import FastAPI, File, HTTPException, Path, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from . import almacen, db
from .excel import excel_resumen, excel_todos, excel_viaje
from .factura import leer_factura
from .resumen import resumen

PUBLIC = FsPath(__file__).resolve().parent.parent / "public"
MAX_PDF = 10 * 1024 * 1024  # 10 MB
MAX_FOTO = 25 * 1024 * 1024  # fotos de celular
MAX_PEDIDO = 30 * 1024 * 1024  # tope de cualquier pedido (se corta antes de leerlo)
Id = Annotated[int, Path(ge=1, le=2_147_483_647)]  # rango de un INTEGER de PostgreSQL
log = logging.getLogger("uvicorn.error")

# Contraseña opcional: si APP_PASSWORD está definida, la app pide clave.
CLAVE = os.environ.get("APP_PASSWORD", "")
COOKIE = "cf_sesion"


DURACION_SESION = 90 * 24 * 3600


def _firma(vence: str) -> str:
    return hmac.new(CLAVE.encode("utf-8"), f"calculadora-flete:{vence}".encode(), hashlib.sha256).hexdigest()


def _token() -> str:
    """Sesión firmada con la clave y con fecha de vencimiento. Si se cambia la clave, se cierran todas."""
    vence = str(int(time.time()) + DURACION_SESION)
    return f"{vence}:{_firma(vence)}"


def _autenticado(request: Request) -> bool:
    if not CLAVE:
        return True
    vence, _, firma = request.cookies.get(COOKIE, "").partition(":")
    if not vence.isdigit() or int(vence) < time.time():
        return False
    return hmac.compare_digest(firma.encode(), _firma(vence).encode())


@asynccontextmanager
async def _vida(app_):
    db.init_db()
    almacen.iniciar()
    if not CLAVE and (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID")):
        log.warning("ATENCIÓN: APP_PASSWORD no está definida. Cualquiera con la dirección puede ver y borrar viajes.")
    yield


app = FastAPI(title="Calculadora de Flete", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_vida)

CAMPOS = {"toneladas": "Toneladas", "precio_tn": "Precio por tonelada", "pct_chofer": "Porcentaje chofer",
          "precio_gasoil": "Precio del gasoil", "litros": "Litros", "km": "Kilómetros",
          "otros_gastos": "Otros gastos", "fecha": "Fecha", "cliente": "Cliente", "notas": "Notas"}


@app.exception_handler(RequestValidationError)
async def _datos_invalidos(request: Request, exc: RequestValidationError):
    """Respuesta clara y sin devolver lo que se envió (un valor como Infinity rompía la respuesta)."""
    campos = []
    for e in exc.errors():
        nombre = str(e.get("loc", ["", ""])[-1])
        campos.append(CAMPOS.get(nombre, nombre))
    detalle = "Revisá estos datos: " + ", ".join(dict.fromkeys(campos)) + "." if campos else "Datos inválidos."
    return JSONResponse({"detail": detalle}, status_code=422)


CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'self'; "
       "form-action 'self'; frame-ancestors 'none'")


@app.middleware("http")
async def _proteger(request: Request, call_next):
    # 1) Cortar pedidos gigantes antes de leerlos
    largo = request.headers.get("content-length", "")
    if largo.isdigit() and int(largo) > MAX_PEDIDO:
        return JSONResponse({"detail": "El archivo es demasiado grande (máximo 25 MB)."}, status_code=413)
    # 2) Clave
    ruta = request.url.path
    libres = ("/api/estado", "/api/login")
    if ruta.startswith("/api/") and ruta not in libres and not _autenticado(request):
        resp = JSONResponse({"detail": "Ingresá la clave para continuar."}, status_code=401)
    else:
        resp = await call_next(request)
    # 3) Cabeceras de seguridad
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Content-Security-Policy"] = CSP
    if ruta.startswith("/api/") and "cache-control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-store"
    return resp


# Límite de intentos de clave: por dirección IP y en total
_fallos_ip: dict[str, deque] = {}
_fallos_total: deque = deque()
VENTANA, MAX_IP, MAX_TOTAL = 15 * 60, 8, 40


def _bloqueado(ip: str) -> bool:
    ahora = time.time()
    for q in (_fallos_ip.get(ip, deque()), _fallos_total):
        while q and q[0] < ahora - VENTANA:
            q.popleft()
    return len(_fallos_ip.get(ip, ())) >= MAX_IP or len(_fallos_total) >= MAX_TOTAL


def _registrar_fallo(ip: str) -> None:
    ahora = time.time()
    _fallos_ip.setdefault(ip, deque()).append(ahora)
    _fallos_total.append(ahora)
    if len(_fallos_ip) > 10_000:  # no crecer sin límite
        _fallos_ip.clear()


# ---------- Sesión ----------

class Login(BaseModel):
    clave: str = Field(max_length=200)


@app.get("/api/estado")
def estado(request: Request):
    return {"requiere_clave": bool(CLAVE), "autenticado": _autenticado(request)}


@app.post("/api/login")
async def login(datos: Login, request: Request):
    ip = request.client.host if request.client else "?"
    if _bloqueado(ip):
        raise HTTPException(429, "Demasiados intentos. Esperá 15 minutos y probá de nuevo.")
    # Se compara en bytes: así funcionan claves con acentos o ñ
    if not CLAVE or not hmac.compare_digest(datos.clave.encode("utf-8"), CLAVE.encode("utf-8")):
        _registrar_fallo(ip)
        await asyncio.sleep(0.5)
        raise HTTPException(401, "Clave incorrecta.")
    resp = JSONResponse({"ok": True})
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(COOKIE, _token(), max_age=DURACION_SESION, httponly=True,
                    samesite="lax", secure=https)
    return resp


# ---------- Factura ----------

async def _leer_pdf(archivo: UploadFile) -> bytes:
    contenido = await archivo.read(MAX_PDF + 1)
    if len(contenido) > MAX_PDF:
        raise HTTPException(413, "El archivo supera los 10 MB.")
    if not contenido.startswith(b"%PDF"):
        raise HTTPException(400, "El archivo no es un PDF. Subí la factura descargada de ARCA.")
    return contenido


def _nombre_original(archivo: UploadFile) -> str:
    return FsPath(archivo.filename or "factura.pdf").name[:200]


@app.post("/api/factura")
async def subir_factura(archivo: UploadFile = File(...)):
    """Lee los datos de la factura y guarda el PDF en /data/facturas."""
    contenido = await _leer_pdf(archivo)
    try:
        # En otro hilo y con tiempo máximo: un PDF raro no puede trabar al resto de los usuarios
        datos = await asyncio.wait_for(asyncio.to_thread(leer_factura, contenido), timeout=20)
    except Exception:
        datos = {"avisos": ["No se pudieron leer los datos del PDF: cargalos a mano. El archivo igual se guarda."]}
    try:
        datos["factura_archivo"] = await asyncio.to_thread(
            almacen.guardar, contenido, datos.get("fecha", ""), datos.get("factura_nro", ""))
        datos["factura_nombre"] = _nombre_original(archivo)
    except OSError:
        datos.setdefault("avisos", []).append("No se pudo guardar el PDF en el servidor. Los datos sí se cargaron.")
    return datos


# ---------- Viajes ----------

class RemitoIn(BaseModel):
    archivo: str = Field(max_length=160)
    miniatura: str = Field("", max_length=160)
    tipo: str = Field("foto", max_length=10)
    nombre: str = Field("", max_length=200)


class ViajeIn(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)  # rechaza Infinity y NaN

    fecha: date | None = None
    factura_nro: str = Field("", max_length=40)
    cliente: str = Field("", max_length=200)
    cliente_cuit: str = Field("", max_length=20)
    detalle: str = Field("", max_length=300)
    remito: str = Field("", max_length=40)
    cae: str = Field("", max_length=30)
    # Topes amplios (con margen para la inflación) pero finitos
    toneladas: float = Field(ge=0, le=100_000)
    precio_tn: float = Field(ge=0, le=1e10)
    pct_chofer: float = Field(18, ge=0, le=100)
    precio_gasoil: float = Field(ge=0, le=1e8)
    litros: float = Field(ge=0, le=100_000)
    km: float | None = Field(None, ge=0, le=1_000_000)
    otros_gastos: float = Field(0, ge=0, le=1e11)
    notas: str = Field("", max_length=2000)
    factura_archivo: str = Field("", max_length=160)
    factura_nombre: str = Field("", max_length=200)
    remitos: list[RemitoIn] = Field(default_factory=list, max_length=30)


@app.get("/api/viajes")
def listar_viajes():
    return db.listar()


@app.post("/api/viajes", status_code=201)
def guardar_viaje(v: ViajeIn):
    if v.toneladas <= 0 or v.precio_tn <= 0:
        raise HTTPException(400, "Cargá las toneladas y el precio por tonelada antes de guardar.")
    datos = v.model_dump()
    fotos = [f for f in datos.pop("remitos") if almacen.ruta_remito(f["archivo"])]  # solo archivos que existen
    for f in fotos:
        if f["miniatura"] and not almacen.ruta_remito(f["miniatura"]):
            f["miniatura"] = ""
    if datos["factura_archivo"] and not almacen.ruta(datos["factura_archivo"]):
        datos["factura_archivo"] = datos["factura_nombre"] = ""   # referencia inválida: se ignora
    return db.crear(datos, fotos)


@app.get("/api/viajes/excel")
def exportar_todos():
    contenido = excel_todos(db.listar())
    return _xlsx(contenido, f"viajes-{date.today().isoformat()}.xlsx")


# ---------- Resumen mensual ----------

def _mes_valido(mes: str | None) -> str | None:
    if mes and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", mes):
        raise HTTPException(400, "Mes inválido. Usá el formato AAAA-MM.")
    return mes


@app.get("/api/resumen")
def ver_resumen(mes: str | None = None):
    return resumen(db.listar(), _mes_valido(mes))


@app.get("/api/resumen/excel")
def exportar_resumen(mes: str):
    r = resumen(db.listar(), _mes_valido(mes))
    if not r["viajes"]:
        raise HTTPException(404, "No hay viajes en ese mes.")
    return _xlsx(excel_resumen(r), f"resumen-{mes}.xlsx")


@app.get("/api/viajes/{viaje_id}")
def ver_viaje(viaje_id: Id):
    v = db.obtener(viaje_id)
    if not v:
        raise HTTPException(404, "Ese viaje no existe.")
    return v


@app.delete("/api/viajes/{viaje_id}")
def borrar_viaje(viaje_id: Id):
    if not db.borrar(viaje_id):
        raise HTTPException(404, "Ese viaje no existe.")
    return {"ok": True}


@app.get("/api/viajes/{viaje_id}/factura")
def ver_factura(viaje_id: Id):
    v = db.obtener(viaje_id)
    if not v:
        raise HTTPException(404, "Ese viaje no existe.")
    p = almacen.ruta(v.get("factura_archivo"))
    if not p:
        raise HTTPException(404, "Este viaje no tiene la factura guardada.")
    nombre = re.sub(r'[^A-Za-z0-9._-]+', "-", v.get("factura_nombre") or p.name)
    return FileResponse(p, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{nombre}"'})


@app.post("/api/viajes/{viaje_id}/factura")
async def adjuntar_factura(viaje_id: Id, archivo: UploadFile = File(...)):
    """Adjunta (o reemplaza) la factura PDF de un viaje ya guardado."""
    v = db.obtener(viaje_id)
    if not v:
        raise HTTPException(404, "Ese viaje no existe.")
    contenido = await _leer_pdf(archivo)
    try:
        nombre = await asyncio.to_thread(almacen.guardar, contenido, v.get("fecha") or "", v.get("factura_nro") or "")
    except OSError:
        raise HTTPException(500, "No se pudo guardar el PDF en el servidor.")
    return db.asignar_factura(viaje_id, nombre, _nombre_original(archivo))


# ---------- Remitos (fotos) ----------

async def _guardar_foto(archivo: UploadFile) -> dict:
    contenido = await archivo.read(MAX_FOTO + 1)
    if len(contenido) > MAX_FOTO:
        raise HTTPException(413, "La foto supera los 25 MB.")
    if not contenido:
        raise HTTPException(400, "El archivo está vacío.")
    try:
        foto = await asyncio.to_thread(almacen.guardar_remito, contenido)
    except almacen.FormatoNoSoportado as e:
        raise HTTPException(400, f"{e} Subí una foto (JPG, PNG, HEIC) o un PDF.")
    except OSError:
        raise HTTPException(500, "No se pudo guardar la foto en el servidor.")
    foto["nombre"] = _nombre_original(archivo)
    return foto


@app.post("/api/remitos")
async def subir_remito(archivo: UploadFile = File(...)):
    """Guarda una foto de remito antes de guardar el viaje (se asocia al guardar)."""
    return await _guardar_foto(archivo)


@app.post("/api/viajes/{viaje_id}/remitos")
async def agregar_remito(viaje_id: Id, archivo: UploadFile = File(...)):
    if not db.obtener(viaje_id):
        raise HTTPException(404, "Ese viaje no existe.")
    return db.agregar_remito(viaje_id, await _guardar_foto(archivo))


@app.delete("/api/remitos/{remito_id}")
def quitar_remito(remito_id: Id):
    if not db.quitar_remito(remito_id):
        raise HTTPException(404, "Esa foto no existe.")
    return {"ok": True}


@app.get("/api/remitos/archivo/{nombre}")
def ver_remito(nombre: str):
    p = almacen.ruta_remito(nombre)
    if not p:
        raise HTTPException(404, "No se encontró la foto.")
    tipo = "application/pdf" if p.suffix == ".pdf" else "image/jpeg"
    return FileResponse(p, media_type=tipo, headers={
        "Content-Disposition": f'inline; filename="remito-{p.name}"',
        "Cache-Control": "private, max-age=31536000, immutable",  # el nombre cambia si cambia el archivo
    })


@app.get("/api/viajes/{viaje_id}/excel")
def exportar_viaje(viaje_id: Id):
    v = db.obtener(viaje_id)
    if not v:
        raise HTTPException(404, "Ese viaje no existe.")
    cliente = re.sub(r"[^A-Za-z0-9]+", "-", v.get("cliente") or "").strip("-")[:30]
    nombre = f"viaje-{v['id']}-{v.get('fecha') or ''}-{cliente}".strip("-") + ".xlsx"
    return _xlsx(excel_viaje(v), nombre.replace("--", "-"))


def _xlsx(contenido: bytes, nombre: str) -> Response:
    return Response(
        contenido,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


# ---------- Página ----------

@app.get("/{ruta:path}", include_in_schema=False)
def pagina(ruta: str):
    if ruta.startswith("api/"):
        raise HTTPException(404, "No encontrado.")
    archivo = (PUBLIC / ruta).resolve()
    if ruta and archivo.is_file() and PUBLIC in archivo.parents:
        return FileResponse(archivo)
    return FileResponse(PUBLIC / "index.html", headers={"Cache-Control": "no-cache"})
