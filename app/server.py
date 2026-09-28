"""Servidor web de la Calculadora de Flete (FastAPI)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import almacen, db
from .excel import excel_resumen, excel_todos, excel_viaje
from .factura import leer_factura
from .resumen import resumen

PUBLIC = Path(__file__).resolve().parent.parent / "public"
MAX_PDF = 10 * 1024 * 1024  # 10 MB

# Contraseña opcional: si APP_PASSWORD está definida, la app pide clave.
CLAVE = os.environ.get("APP_PASSWORD", "")
COOKIE = "cf_sesion"


def _token() -> str:
    return hmac.new(CLAVE.encode(), b"calculadora-flete", hashlib.sha256).hexdigest()


def _autenticado(request: Request) -> bool:
    if not CLAVE:
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), _token())


app = FastAPI(title="Calculadora de Flete", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _inicio() -> None:
    db.init_db()
    almacen.iniciar()


@app.middleware("http")
async def _proteger(request: Request, call_next):
    ruta = request.url.path
    libres = ("/api/estado", "/api/login")
    if ruta.startswith("/api/") and ruta not in libres and not _autenticado(request):
        return JSONResponse({"detail": "Ingresá la clave para continuar."}, status_code=401)
    return await call_next(request)


# ---------- Sesión ----------

class Login(BaseModel):
    clave: str


@app.get("/api/estado")
def estado(request: Request):
    return {"requiere_clave": bool(CLAVE), "autenticado": _autenticado(request)}


@app.post("/api/login")
async def login(datos: Login, request: Request):
    if not CLAVE or not hmac.compare_digest(datos.clave, CLAVE):
        await asyncio.sleep(0.8)  # frena intentos repetidos
        raise HTTPException(401, "Clave incorrecta.")
    resp = JSONResponse({"ok": True})
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(COOKIE, _token(), max_age=90 * 24 * 3600, httponly=True,
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
    return Path(archivo.filename or "factura.pdf").name[:200]


@app.post("/api/factura")
async def subir_factura(archivo: UploadFile = File(...)):
    """Lee los datos de la factura y guarda el PDF en /data/facturas."""
    contenido = await _leer_pdf(archivo)
    try:
        datos = leer_factura(contenido)
    except Exception:
        datos = {"avisos": ["No se pudieron leer los datos del PDF: cargalos a mano. El archivo igual se guarda."]}
    try:
        datos["factura_archivo"] = almacen.guardar(contenido, datos.get("fecha", ""), datos.get("factura_nro", ""))
        datos["factura_nombre"] = _nombre_original(archivo)
    except OSError:
        datos.setdefault("avisos", []).append("No se pudo guardar el PDF en el servidor. Los datos sí se cargaron.")
    return datos


# ---------- Viajes ----------

class ViajeIn(BaseModel):
    fecha: date | None = None
    factura_nro: str = Field("", max_length=40)
    cliente: str = Field("", max_length=200)
    cliente_cuit: str = Field("", max_length=20)
    detalle: str = Field("", max_length=300)
    remito: str = Field("", max_length=40)
    cae: str = Field("", max_length=30)
    toneladas: float = Field(ge=0)
    precio_tn: float = Field(ge=0)
    pct_chofer: float = Field(18, ge=0, le=100)
    precio_gasoil: float = Field(ge=0)
    litros: float = Field(ge=0)
    km: float | None = Field(None, ge=0)
    otros_gastos: float = Field(0, ge=0)
    notas: str = Field("", max_length=2000)
    factura_archivo: str = Field("", max_length=160)
    factura_nombre: str = Field("", max_length=200)


@app.get("/api/viajes")
def listar_viajes():
    return db.listar()


@app.post("/api/viajes", status_code=201)
def guardar_viaje(v: ViajeIn):
    if v.toneladas <= 0 or v.precio_tn <= 0:
        raise HTTPException(400, "Cargá las toneladas y el precio por tonelada antes de guardar.")
    datos = v.model_dump()
    if datos["factura_archivo"] and not almacen.ruta(datos["factura_archivo"]):
        datos["factura_archivo"] = datos["factura_nombre"] = ""   # referencia inválida: se ignora
    return db.crear(datos)


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
def ver_viaje(viaje_id: int):
    v = db.obtener(viaje_id)
    if not v:
        raise HTTPException(404, "Ese viaje no existe.")
    return v


@app.delete("/api/viajes/{viaje_id}")
def borrar_viaje(viaje_id: int):
    if not db.borrar(viaje_id):
        raise HTTPException(404, "Ese viaje no existe.")
    return {"ok": True}


@app.get("/api/viajes/{viaje_id}/factura")
def ver_factura(viaje_id: int):
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
async def adjuntar_factura(viaje_id: int, archivo: UploadFile = File(...)):
    """Adjunta (o reemplaza) la factura PDF de un viaje ya guardado."""
    v = db.obtener(viaje_id)
    if not v:
        raise HTTPException(404, "Ese viaje no existe.")
    contenido = await _leer_pdf(archivo)
    try:
        nombre = almacen.guardar(contenido, v.get("fecha") or "", v.get("factura_nro") or "")
    except OSError:
        raise HTTPException(500, "No se pudo guardar el PDF en el servidor.")
    return db.asignar_factura(viaje_id, nombre, _nombre_original(archivo))


@app.get("/api/viajes/{viaje_id}/excel")
def exportar_viaje(viaje_id: int):
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
    archivo = (PUBLIC / ruta).resolve()
    if ruta and archivo.is_file() and PUBLIC in archivo.parents:
        return FileResponse(archivo)
    return FileResponse(PUBLIC / "index.html", headers={"Cache-Control": "no-cache"})
