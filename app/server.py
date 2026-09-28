"""Servidor web de la Calculadora de Flete (FastAPI)."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import tempfile
import zipfile
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path as FsPath
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Path, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.background import BackgroundTask
from pydantic import BaseModel, ConfigDict, Field

from . import almacen, cuentas, db, respaldo
from .excel import excel_resumen, excel_todos, excel_viaje
from .factura import leer_factura
from .resumen import resumen

PUBLIC = FsPath(__file__).resolve().parent.parent / "public"
MAX_PDF = 10 * 1024 * 1024     # 10 MB
MAX_FOTO = 25 * 1024 * 1024    # fotos de celular
MAX_PEDIDO = 30 * 1024 * 1024  # tope de cualquier pedido (se corta antes de leerlo)
Id = Annotated[int, Path(ge=1, le=2_147_483_647)]  # rango de un INTEGER de PostgreSQL
COOKIE = "cf_sesion"
log = logging.getLogger("uvicorn.error")


def _invitacion() -> str:
    return os.environ.get("CODIGO_INVITACION", "").strip()


async def _respaldos_periodicos():
    while True:
        await asyncio.sleep(5 * 60)   # solo escribe si algo cambió
        try:
            await asyncio.to_thread(respaldo.hacer_respaldo, "automático")
        except Exception:
            log.exception("Falló el respaldo automático")


@asynccontextmanager
async def _vida(app_):
    db.init_db()
    almacen.iniciar()
    restaurado = respaldo.restaurar_si_vacia()
    if restaurado:
        log.warning("Datos recuperados del respaldo %s", restaurado)
    estado_alm = db.almacenamiento()
    if not estado_alm["base_permanente"]:
        log.warning("ATENCIÓN: la base de datos (%s) NO es permanente: se borra en cada deploy. "
                    "Creá un Volume con mount path /data o configurá DATABASE_URL.", estado_alm["base"])
    else:
        log.info("Base de datos: %s", estado_alm["base"])
    try:
        respaldo.hacer_respaldo("inicio")
    except Exception:
        log.exception("Falló el respaldo al iniciar")
    tarea = asyncio.create_task(_respaldos_periodicos())
    if not _invitacion():
        log.warning("CODIGO_INVITACION no está definida: nadie puede crear cuentas nuevas.")
    if not cuentas.correo_configurado():
        log.warning("Falta BREVO_API_KEY o EMAIL_FROM: los códigos de recuperación se van a mostrar en estos logs "
                    "en lugar de enviarse por correo.")
    if os.environ.get("APP_PASSWORD"):
        log.info("APP_PASSWORD ya no se usa: ahora cada persona entra con su usuario y contraseña. Podés borrarla.")
    yield
    tarea.cancel()
    try:
        respaldo.hacer_respaldo("apagado")   # Railway apaga el contenedor viejo en cada deploy
    except Exception:
        log.exception("Falló el respaldo al apagar")


app = FastAPI(title="Calculadora de Flete", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_vida)

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'self'; "
       "form-action 'self'; frame-ancestors 'none'")


def _host_publico(request: Request) -> str:
    return (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip().lower()


@app.middleware("http")
async def _proteger(request: Request, call_next):
    # 1) Cortar pedidos gigantes antes de leerlos
    largo = request.headers.get("content-length", "")
    if largo.isdigit() and int(largo) > MAX_PEDIDO:
        return JSONResponse({"detail": "El archivo es demasiado grande (máximo 25 MB)."}, status_code=413)
    # 2) Pedidos que modifican datos solo desde la propia página (defensa contra CSRF)
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origen = request.headers.get("origin")
        if origen and urlsplit(origen).netloc.lower() != _host_publico(request):
            return JSONResponse({"detail": "Pedido rechazado: no viene de esta página."}, status_code=403)
    resp = await call_next(request)
    # 3) Cabeceras de seguridad
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Content-Security-Policy"] = CSP
    if request.url.path.startswith("/api/") and "cache-control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-store"
    return resp


CAMPOS = {"toneladas": "Toneladas", "precio_tn": "Precio por tonelada", "pct_chofer": "Porcentaje chofer",
          "precio_gasoil": "Precio del gasoil", "litros": "Litros", "km": "Kilómetros",
          "otros_gastos": "Otros gastos", "fecha": "Fecha", "cliente": "Cliente", "notas": "Notas",
          "usuario": "Usuario", "correo": "Correo", "clave": "Contraseña", "clave_nueva": "Contraseña nueva",
          "codigo": "Código", "codigo_invitacion": "Código de invitación", "actual": "Contraseña actual",
          "nueva": "Contraseña nueva"}


@app.exception_handler(RequestValidationError)
async def _datos_invalidos(request: Request, exc: RequestValidationError):
    """Respuesta clara y sin devolver lo que se envió (un valor como Infinity rompía la respuesta)."""
    campos = [CAMPOS.get(str(e.get("loc", [""])[-1]), str(e.get("loc", [""])[-1])) for e in exc.errors()]
    detalle = "Revisá estos datos: " + ", ".join(dict.fromkeys(campos)) + "." if campos else "Datos inválidos."
    return JSONResponse({"detail": detalle}, status_code=422)


# ======================= Sesión =======================

def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _token_de(request: Request) -> str:
    return request.cookies.get(COOKIE, "")


def usuario_actual(request: Request) -> dict:
    token = _token_de(request)
    u = db.usuario_de_sesion(cuentas.huella(token)) if token else None
    if not u:
        raise HTTPException(401, "Ingresá con tu usuario y contraseña.")
    return u


Usuario = Annotated[dict, Depends(usuario_actual)]


def _abrir_sesion(resp: Response, request: Request, u: dict, recordar: bool = True) -> None:
    token = cuentas.nuevo_token()
    db.crear_sesion(u["id"], cuentas.huella(token), request.headers.get("user-agent", ""), _ip(request), recordar)
    db.marcar_ingreso(u["id"])
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    # Con "Mantener la sesión iniciada" la cookie dura 400 días (el máximo de los navegadores) y el
    # servidor la vence a los 90 días sin uso. Sin tildar: cookie de sesión (se borra al cerrar el navegador).
    resp.set_cookie(COOKIE, token, max_age=400 * 24 * 3600 if recordar else None, httponly=True,
                    samesite="lax", secure=https, path="/")


def _publico(u: dict) -> dict:
    return {"usuario": u["usuario"], "correo": u["correo"]}


def _correo_seguridad(bg: BackgroundTasks, u: dict, asunto: str, texto: str) -> None:
    bg.add_task(cuentas.enviar_correo, u["correo"], asunto, texto)


@app.get("/api/estado")
def estado(request: Request):
    token = _token_de(request)
    u = db.usuario_de_sesion(cuentas.huella(token)) if token else None
    alm = db.almacenamiento()
    return {"autenticado": bool(u), **(_publico(u) if u else {}),
            "registro_abierto": bool(_invitacion()), "correo_activo": cuentas.correo_configurado(),
            "hay_usuarios": db.hay_usuarios(),
            "almacenamiento_ok": alm["base_permanente"] and alm["archivos_permanentes"]}


class RegistroIn(BaseModel):
    usuario: str = Field(max_length=30)
    correo: str = Field(max_length=200)
    clave: str = Field(max_length=128)
    codigo_invitacion: str = Field(max_length=100)
    recordar: bool = True


@app.post("/api/registro", status_code=201)
async def registro(d: RegistroIn, request: Request, bg: BackgroundTasks):
    ip = _ip(request)
    if not _invitacion():
        raise HTTPException(403, "El registro de cuentas nuevas está cerrado.")
    if cuentas.REGISTRO_IP.excedido(ip):
        raise HTTPException(429, "Demasiados intentos. Probá de nuevo en una hora.")
    cuentas.REGISTRO_IP.sumar(ip)
    if not hmac.compare_digest(d.codigo_invitacion.strip().encode(), _invitacion().encode()):
        await asyncio.sleep(0.5)
        raise HTTPException(403, "El código de invitación no es correcto.")
    usuario, correo = cuentas.normalizar_usuario(d.usuario), d.correo.strip().lower()
    for problema in (cuentas.problema_usuario(usuario), cuentas.problema_correo(correo),
                     cuentas.problema_clave(d.clave, usuario, correo)):
        if problema:
            raise HTTPException(400, problema)
    ya_u, ya_c = db.existe(usuario, correo)
    if ya_u:
        raise HTTPException(409, "Ese nombre de usuario ya está en uso. Elegí otro.")
    if ya_c:
        raise HTTPException(409, "Ya hay una cuenta con ese correo. Si es tuya, usá “Olvidé mi contraseña”.")
    primero = db.contar_usuarios() == 0
    clave_hash = await asyncio.to_thread(cuentas.hash_clave, d.clave)
    try:
        u = db.crear_usuario(usuario, correo, clave_hash)
    except Exception:
        raise HTTPException(409, "Ese usuario o correo ya está en uso.")
    almacen.preparar_usuario(u)
    if primero:
        n = await asyncio.to_thread(reclamar_datos_anteriores, u)
        if n:
            log.info("Se asignaron %d viajes anteriores a %s", n, u["usuario"])
    resp = JSONResponse(_publico(u), status_code=201)
    _abrir_sesion(resp, request, u, d.recordar)
    _correo_seguridad(bg, u, "Tu cuenta de Calculadora de Flete",
                      f"Hola {u['usuario']}:\n\nTu cuenta se creó correctamente. Entrás con tu usuario "
                      f"({u['usuario']}) o con este correo.\n\nSi no fuiste vos, respondé este correo.")
    return resp


class LoginIn(BaseModel):
    usuario: str = Field(max_length=200)   # usuario o correo
    clave: str = Field(max_length=200)
    recordar: bool = True


@app.post("/api/login")
async def login(d: LoginIn, request: Request):
    ip, clave_cuenta = _ip(request), d.usuario.strip().lower()
    if cuentas.LOGIN_IP.excedido(ip) or cuentas.LOGIN_CUENTA.excedido(clave_cuenta):
        raise HTTPException(429, "Demasiados intentos. Esperá 15 minutos o usá “Olvidé mi contraseña”.")
    u = db.buscar_usuario(clave_cuenta)
    # Se verifica siempre (con un hash de relleno si el usuario no existe): misma demora en los dos casos
    ok = await asyncio.to_thread(cuentas.verificar_clave, d.clave, u["clave_hash"] if u else cuentas.HASH_FALSO)
    if not (u and ok and u["activo"]):
        cuentas.LOGIN_IP.sumar(ip)
        cuentas.LOGIN_CUENTA.sumar(clave_cuenta)
        raise HTTPException(401, "Usuario o contraseña incorrectos.")
    cuentas.LOGIN_CUENTA.reiniciar(clave_cuenta)
    resp = JSONResponse(_publico(u))
    _abrir_sesion(resp, request, u, d.recordar)
    return resp


@app.post("/api/logout")
def logout(request: Request):
    token = _token_de(request)
    if token:
        db.borrar_sesion(cuentas.huella(token))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


class RecuperarIn(BaseModel):
    usuario: str = Field(max_length=200)


MSJ_RECUPERO = ("Si el dato corresponde a una cuenta, te enviamos un código de 6 números al correo registrado. "
                "Vence en 15 minutos. Revisá también la carpeta de spam.")


@app.post("/api/recuperar")
async def recuperar(d: RecuperarIn, request: Request, bg: BackgroundTasks):
    ip, clave_cuenta = _ip(request), d.usuario.strip().lower()
    if cuentas.RECUPERO_IP.excedido(ip):
        raise HTTPException(429, "Demasiados pedidos. Probá de nuevo en una hora.")
    cuentas.RECUPERO_IP.sumar(ip)
    u = db.buscar_usuario(clave_cuenta)
    # La respuesta es la misma exista o no la cuenta: no revela qué usuarios o correos están registrados
    if u and u["activo"] and not cuentas.RECUPERO_CUENTA.excedido(str(u["id"])):
        cuentas.RECUPERO_CUENTA.sumar(str(u["id"]))
        codigo = cuentas.nuevo_codigo()
        db.crear_codigo(u["id"], cuentas.huella(f"{u['id']}:{codigo}"), cuentas.CODIGO_MINUTOS)
        _correo_seguridad(bg, u, f"{codigo} es tu código para recuperar la contraseña",
                          f"Hola {u['usuario']}:\n\nTu código para crear una contraseña nueva es: {codigo}\n\n"
                          f"Vence en {cuentas.CODIGO_MINUTOS} minutos. Si no lo pediste, ignorá este correo: "
                          "tu contraseña actual sigue funcionando.")
    return {"ok": True, "mensaje": MSJ_RECUPERO}


class ConfirmarIn(BaseModel):
    usuario: str = Field(max_length=200)
    codigo: str = Field(max_length=10)
    clave_nueva: str = Field(max_length=128)


@app.post("/api/recuperar/confirmar")
async def recuperar_confirmar(d: ConfirmarIn, request: Request, bg: BackgroundTasks):
    ip = _ip(request)
    if cuentas.LOGIN_IP.excedido(ip):
        raise HTTPException(429, "Demasiados intentos. Esperá 15 minutos.")
    # Reglas generales de la contraseña antes de usar el código (un error de tipeo no lo gasta).
    # Son las mismas para todos: la respuesta no revela si la cuenta existe.
    # Se usa lo que escribió la persona (no la cuenta real), así la respuesta no revela si existe
    escrito = d.usuario.strip().lower()
    problema = cuentas.problema_clave(d.clave_nueva, escrito if "@" not in escrito else "",
                                      escrito if "@" in escrito else "")
    if problema:
        raise HTTPException(400, problema)
    u = db.buscar_usuario(d.usuario)
    codigo = re.sub(r"\D", "", d.codigo)
    if not u or len(codigo) != 6 or not db.validar_codigo(
            u["id"], cuentas.huella(f"{u['id']}:{codigo}"), cuentas.CODIGO_INTENTOS):
        cuentas.LOGIN_IP.sumar(ip)
        raise HTTPException(400, "El código no es correcto o ya venció. Pedí uno nuevo.")
    problema = cuentas.problema_clave(d.clave_nueva, u["usuario"], u["correo"])
    if problema:
        raise HTTPException(400, problema + " Pedí un código nuevo e intentá otra vez.")
    db.cambiar_clave(u["id"], await asyncio.to_thread(cuentas.hash_clave, d.clave_nueva))
    db.borrar_sesiones(u["id"])  # cierra la sesión en todos los dispositivos
    cuentas.LOGIN_CUENTA.reiniciar(u["usuario"])
    cuentas.LOGIN_CUENTA.reiniciar(u["correo"])
    _correo_seguridad(bg, u, "Se cambió tu contraseña",
                      f"Hola {u['usuario']}:\n\nLa contraseña de tu cuenta se cambió con un código de recuperación. "
                      "Se cerró la sesión en todos los dispositivos.\n\nSi no fuiste vos, pedí un código nuevo "
                      "de inmediato desde “Olvidé mi contraseña”.")
    resp = JSONResponse(_publico(u))
    _abrir_sesion(resp, request, u)
    return resp


# ======================= Mi cuenta =======================

@app.get("/api/cuenta")
def ver_cuenta(u: Usuario):
    return {**_publico(u), "creado": u["creado"], "ultimo_ingreso": u["ultimo_ingreso"],
            "clave_cambiada": u["clave_cambiada"], "sesiones": db.contar_sesiones(u["id"]),
            "carpeta": f"/data/usuarios/{u['carpeta']}", "viajes": len(db.listar(u["id"])), **almacen.uso(u),
            "resguardo": {**db.almacenamiento(), "ultimo_respaldo": respaldo.ultimo()}}


def _armar_zip(u: dict) -> str:
    """Zip con toda la carpeta del usuario y un datos.json con todos sus viajes."""
    fd, destino = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    raiz = almacen.raiz(u)
    viajes = [db.obtener(u["id"], v["id"]) for v in db.listar(u["id"])]
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("datos.json", json.dumps({"usuario": u["usuario"], "correo": u["correo"],
                                             "exportado": date.today().isoformat(), "viajes": viajes},
                                            ensure_ascii=False, indent=2, default=str))
        if raiz.is_dir():
            for p in sorted(raiz.rglob("*")):
                if p.is_file() and not p.name.endswith(".tmp"):
                    z.write(p, p.relative_to(raiz).as_posix())
    return destino


@app.get("/api/cuenta/descargar")
async def descargar_datos(u: Usuario):
    destino = await asyncio.to_thread(_armar_zip, u)
    nombre = f"calculadora-flete_{u['usuario']}_{date.today().isoformat()}.zip"
    return FileResponse(destino, media_type="application/zip", filename=nombre,
                        background=BackgroundTask(os.unlink, destino))


class CambioClaveIn(BaseModel):
    actual: str = Field(max_length=200)
    nueva: str = Field(max_length=128)


@app.post("/api/cuenta/clave")
async def cambiar_clave(d: CambioClaveIn, u: Usuario, request: Request, bg: BackgroundTasks):
    llave = u["usuario"]
    if cuentas.LOGIN_CUENTA.excedido(llave):
        raise HTTPException(429, "Demasiados intentos. Esperá 15 minutos.")
    if not await asyncio.to_thread(cuentas.verificar_clave, d.actual, u["clave_hash"]):
        cuentas.LOGIN_CUENTA.sumar(llave)
        raise HTTPException(400, "La contraseña actual no es correcta.")
    problema = cuentas.problema_clave(d.nueva, u["usuario"], u["correo"])
    if problema:
        raise HTTPException(400, problema)
    db.cambiar_clave(u["id"], await asyncio.to_thread(cuentas.hash_clave, d.nueva))
    cerradas = db.borrar_sesiones(u["id"], excepto=cuentas.huella(_token_de(request)))
    _correo_seguridad(bg, u, "Se cambió tu contraseña",
                      f"Hola {u['usuario']}:\n\nLa contraseña de tu cuenta se cambió desde “Mi cuenta”. "
                      "Si no fuiste vos, usá “Olvidé mi contraseña” para recuperarla.")
    return {"ok": True, "sesiones_cerradas": cerradas}


@app.post("/api/cuenta/cerrar-sesiones")
def cerrar_otras_sesiones(u: Usuario, request: Request):
    return {"ok": True, "sesiones_cerradas": db.borrar_sesiones(u["id"], excepto=cuentas.huella(_token_de(request)))}


# ======================= Factura y remitos (archivos) =======================

async def _leer(archivo: UploadFile, maximo: int, etiqueta: str) -> bytes:
    contenido = await archivo.read(maximo + 1)
    if len(contenido) > maximo:
        raise HTTPException(413, f"{etiqueta} supera los {maximo // (1024 * 1024)} MB.")
    if not contenido:
        raise HTTPException(400, "El archivo está vacío.")
    return contenido


async def _leer_pdf(archivo: UploadFile) -> bytes:
    contenido = await _leer(archivo, MAX_PDF, "El archivo")
    if not contenido.startswith(b"%PDF"):
        raise HTTPException(400, "El archivo no es un PDF. Subí la factura descargada de ARCA.")
    return contenido


def _nombre_original(archivo: UploadFile) -> str:
    return FsPath(archivo.filename or "archivo").name[:200]


@app.post("/api/factura")
async def subir_factura(u: Usuario, archivo: UploadFile = File(...)):
    """Lee los datos de la factura y deja el PDF en pendientes/ hasta que se guarde el viaje."""
    contenido = await _leer_pdf(archivo)
    try:
        # En otro hilo y con tiempo máximo: un PDF raro no puede trabar a los demás
        datos = await asyncio.wait_for(asyncio.to_thread(leer_factura, contenido), timeout=20)
    except Exception:
        datos = {"avisos": ["No se pudieron leer los datos del PDF: cargalos a mano. El archivo igual se guarda."]}
    try:
        datos["factura_archivo"] = await asyncio.to_thread(almacen.guardar_pendiente_pdf, u, contenido)
        datos["factura_nombre"] = _nombre_original(archivo)
    except OSError:
        datos.setdefault("avisos", []).append("No se pudo guardar el PDF en el servidor. Los datos sí se cargaron.")
    return datos


async def _procesar_remito(coro):
    try:
        return await coro
    except almacen.FormatoNoSoportado as e:
        raise HTTPException(400, f"{e} Subí una foto (JPG, PNG, HEIC) o un PDF.")
    except OSError:
        raise HTTPException(500, "No se pudo guardar la foto en el servidor.")


@app.post("/api/remitos")
async def subir_remito(u: Usuario, archivo: UploadFile = File(...)):
    """Guarda una foto de remito en pendientes/ (se lleva a la carpeta del viaje al guardarlo)."""
    contenido = await _leer(archivo, MAX_FOTO, "La foto")
    foto = await _procesar_remito(asyncio.to_thread(almacen.guardar_pendiente_remito, u, contenido))
    return {**foto, "nombre": _nombre_original(archivo)}


@app.get("/api/archivos/{ruta:path}")
def ver_archivo(ruta: str, u: Usuario):
    """Sirve un archivo de la carpeta del usuario (solo PDF y JPG, solo los propios)."""
    p = almacen.ruta(u, ruta)
    if not p or p.suffix not in (".pdf", ".jpg"):
        raise HTTPException(404, "No se encontró el archivo.")
    tipo = "application/pdf" if p.suffix == ".pdf" else "image/jpeg"
    return FileResponse(p, media_type=tipo, headers={
        "Content-Disposition": f'inline; filename="{p.name}"', "Cache-Control": "private, max-age=3600"})


# ======================= Viajes =======================

class RemitoIn(BaseModel):
    archivo: str = Field(max_length=300)
    miniatura: str = Field("", max_length=300)
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
    factura_archivo: str = Field("", max_length=300)
    factura_nombre: str = Field("", max_length=200)
    remitos: list[RemitoIn] = Field(default_factory=list, max_length=30)


def _pendiente(u: dict, rel: str) -> FsPath | None:
    return almacen.ruta(u, rel) if (rel or "").startswith("pendientes/") else None


def _guardar_viaje(u: dict, datos: dict, fotos: list[dict]) -> dict:
    factura_rel = datos.pop("factura_archivo", "")
    factura_nombre = datos.pop("factura_nombre", "")
    vid = db.crear_viaje(u["id"], {**datos, "factura_archivo": "", "factura_nombre": ""})
    v = db.obtener(u["id"], vid)
    carpeta = almacen.carpeta_viaje(v)
    (almacen.raiz(u) / carpeta).mkdir(parents=True, exist_ok=True)
    cambios = {"carpeta": carpeta}
    origen = _pendiente(u, factura_rel)
    if origen:
        cambios["factura_archivo"] = almacen.ubicar(u, origen, carpeta, almacen.nombre_factura(v), mover=True)
        cambios["factura_nombre"] = factura_nombre
    db.actualizar_viaje(u["id"], vid, **cambios)
    for f in fotos:
        origen = _pendiente(u, f["archivo"])
        if not origen:
            continue  # referencias inválidas o de otro usuario se ignoran
        n = almacen.siguiente_remito(u, carpeta)
        archivo = almacen.ubicar(u, origen, carpeta, f"remito-{n:02d}{origen.suffix}", mover=True)
        mini = ""
        mini_origen = _pendiente(u, f.get("miniatura") or "")
        if mini_origen:
            mini = almacen.ubicar(u, mini_origen, carpeta, f"remito-{n:02d}.mini.jpg", mover=True)
        db.agregar_remito(u["id"], vid, {"archivo": archivo, "miniatura": mini,
                                         "tipo": "pdf" if archivo.endswith(".pdf") else "foto",
                                         "nombre": (f.get("nombre") or "")[:200]})
    v = db.obtener(u["id"], vid)
    almacen.escribir_ficha(u, v)
    return v


@app.get("/api/viajes")
def listar_viajes(u: Usuario):
    return db.listar(u["id"])


@app.post("/api/viajes", status_code=201)
async def guardar_viaje(v: ViajeIn, u: Usuario):
    if v.toneladas <= 0 or v.precio_tn <= 0:
        raise HTTPException(400, "Cargá las toneladas y el precio por tonelada antes de guardar.")
    datos = v.model_dump()
    fotos = datos.pop("remitos")
    return await asyncio.to_thread(_guardar_viaje, u, datos, fotos)


@app.get("/api/viajes/excel")
def exportar_todos(u: Usuario):
    return _xlsx(excel_todos(db.listar(u["id"])), f"viajes-{date.today().isoformat()}.xlsx")


def _mes_valido(mes: str | None) -> str | None:
    if mes and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", mes):
        raise HTTPException(400, "Mes inválido. Usá el formato AAAA-MM.")
    return mes


@app.get("/api/resumen")
def ver_resumen(u: Usuario, mes: str | None = None):
    return resumen(db.listar(u["id"]), _mes_valido(mes))


@app.get("/api/resumen/excel")
def exportar_resumen(u: Usuario, mes: str):
    r = resumen(db.listar(u["id"]), _mes_valido(mes))
    if not r["viajes"]:
        raise HTTPException(404, "No hay viajes en ese mes.")
    return _xlsx(excel_resumen(r), f"resumen-{mes}.xlsx")


def _viaje(u: dict, viaje_id: int) -> dict:
    v = db.obtener(u["id"], viaje_id)
    if not v:
        raise HTTPException(404, "Ese viaje no existe.")
    return v


def _asegurar_carpeta(u: dict, v: dict) -> str:
    if not v.get("carpeta"):
        v["carpeta"] = almacen.carpeta_viaje(v)
        db.actualizar_viaje(u["id"], v["id"], carpeta=v["carpeta"])
    (almacen.raiz(u) / v["carpeta"]).mkdir(parents=True, exist_ok=True)
    return v["carpeta"]


@app.get("/api/viajes/{viaje_id}")
def ver_viaje(viaje_id: Id, u: Usuario):
    return _viaje(u, viaje_id)


@app.delete("/api/viajes/{viaje_id}")
def borrar_viaje(viaje_id: Id, u: Usuario):
    v = _viaje(u, viaje_id)
    db.borrar(u["id"], viaje_id)
    try:
        almacen.archivar_eliminado(u, v)   # la carpeta pasa a eliminados/, no se borra
    except OSError:
        log.exception("No se pudo archivar la carpeta del viaje %s", viaje_id)
    return {"ok": True}


@app.get("/api/viajes/{viaje_id}/factura")
def ver_factura(viaje_id: Id, u: Usuario):
    v = _viaje(u, viaje_id)
    p = almacen.ruta(u, v.get("factura_archivo"))
    if not p:
        raise HTTPException(404, "Este viaje no tiene la factura guardada.")
    nombre = re.sub(r"[^A-Za-z0-9._-]+", "-", v.get("factura_nombre") or p.name)
    return FileResponse(p, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{nombre}"'})


@app.post("/api/viajes/{viaje_id}/factura")
async def adjuntar_factura(viaje_id: Id, u: Usuario, archivo: UploadFile = File(...)):
    """Adjunta (o reemplaza) la factura de un viaje. La anterior queda en la carpeta."""
    v = _viaje(u, viaje_id)
    contenido = await _leer_pdf(archivo)
    nombre_original = _nombre_original(archivo)

    def _hacer():
        carpeta = _asegurar_carpeta(u, v)
        tmp = almacen.raiz(u) / almacen.guardar_pendiente_pdf(u, contenido)
        rel = almacen.ubicar(u, tmp, carpeta, almacen.nombre_factura(v), mover=True)
        db.actualizar_viaje(u["id"], viaje_id, factura_archivo=rel, factura_nombre=nombre_original)
        nuevo = db.obtener(u["id"], viaje_id)
        almacen.escribir_ficha(u, nuevo)
        return nuevo
    try:
        return await asyncio.to_thread(_hacer)
    except OSError:
        raise HTTPException(500, "No se pudo guardar el PDF en el servidor.")


@app.post("/api/viajes/{viaje_id}/remitos")
async def agregar_remito(viaje_id: Id, u: Usuario, archivo: UploadFile = File(...)):
    v = _viaje(u, viaje_id)
    contenido = await _leer(archivo, MAX_FOTO, "La foto")
    nombre_original = _nombre_original(archivo)

    def _hacer():
        carpeta = _asegurar_carpeta(u, v)
        foto = almacen.guardar_remito_en_viaje(u, carpeta, contenido)
        db.agregar_remito(u["id"], viaje_id, {**foto, "nombre": nombre_original})
        nuevo = db.obtener(u["id"], viaje_id)
        almacen.escribir_ficha(u, nuevo)
        return nuevo
    return await _procesar_remito(asyncio.to_thread(_hacer))


@app.delete("/api/remitos/{remito_id}")
def quitar_remito(remito_id: Id, u: Usuario):
    vid = db.quitar_remito(u["id"], remito_id)
    if not vid:
        raise HTTPException(404, "Esa foto no existe.")
    v = db.obtener(u["id"], vid)
    if v:
        almacen.escribir_ficha(u, v)
    return {"ok": True}


@app.get("/api/viajes/{viaje_id}/excel")
def exportar_viaje(viaje_id: Id, u: Usuario):
    v = _viaje(u, viaje_id)
    cliente = re.sub(r"[^A-Za-z0-9]+", "-", v.get("cliente") or "").strip("-")[:30]
    nombre = f"viaje-{v['id']}-{v.get('fecha') or ''}-{cliente}".strip("-") + ".xlsx"
    return _xlsx(excel_viaje(v), nombre.replace("--", "-"))


def _xlsx(contenido: bytes, nombre: str) -> Response:
    return Response(contenido, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


# ======================= Datos de la versión anterior =======================

def reclamar_datos_anteriores(u: dict) -> int:
    """Los viajes guardados antes de que existieran las cuentas pasan al primer usuario,
    con sus archivos copiados a la carpeta ordenada (los originales quedan como respaldo)."""
    n = 0
    for v in db.viajes_sin_duenio():
        db.asignar_duenio(u["id"], v["id"])
        v = db.obtener(u["id"], v["id"])
        carpeta = _asegurar_carpeta(u, v)
        viejo = v.get("factura_archivo") or ""
        if viejo and "/" not in viejo:
            p = almacen.ruta_vieja(viejo, "factura")
            rel = almacen.ubicar(u, p, carpeta, almacen.nombre_factura(v), mover=False) if p else ""
            db.actualizar_viaje(u["id"], v["id"], factura_archivo=rel)
        for r in v["remitos"]:
            if "/" in (r["archivo"] or ""):
                continue
            p = almacen.ruta_vieja(r["archivo"], "remito")
            if not p:
                continue
            k = almacen.siguiente_remito(u, carpeta)
            archivo = almacen.ubicar(u, p, carpeta, f"remito-{k:02d}{p.suffix}", mover=False)
            pm = almacen.ruta_vieja(r.get("miniatura") or "", "remito")
            mini = almacen.ubicar(u, pm, carpeta, f"remito-{k:02d}.mini.jpg", mover=False) if pm else ""
            db.actualizar_remito_rutas(r["id"], archivo, mini)
        almacen.escribir_ficha(u, db.obtener(u["id"], v["id"]))
        n += 1
    return n


# ======================= Página =======================

@app.get("/{ruta:path}", include_in_schema=False)
def pagina(ruta: str):
    if ruta.startswith("api/"):
        raise HTTPException(404, "No encontrado.")
    archivo = (PUBLIC / ruta).resolve()
    if ruta and archivo.is_file() and PUBLIC in archivo.parents:
        return FileResponse(archivo)
    return FileResponse(PUBLIC / "index.html", headers={"Cache-Control": "no-cache"})
