"""Base de datos.

En Railway se usa PostgreSQL (variable DATABASE_URL). En la compu, si no hay
DATABASE_URL, se usa un archivo SQLite local (viajes.db).

Cada viaje y cada remito pertenece a un usuario: todas las consultas filtran por
usuario_id, así nadie puede ver ni tocar datos de otro.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, Integer, MetaData, String, Table, Text,
    and_, create_engine, delete, func, insert, inspect, select, text, update,
)


def _url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return "sqlite:///" + os.environ.get("SQLITE_PATH", "viajes.db")
    # Railway entrega postgresql://...; SQLAlchemy necesita indicar el driver
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


engine = create_engine(_url(), pool_pre_ping=True)
metadata = MetaData()


def ahora() -> datetime:
    return datetime.now(timezone.utc)


# ---------------- Tablas ----------------

usuarios = Table(
    "usuarios", metadata,
    Column("id", Integer, primary_key=True),
    Column("usuario", String(30), nullable=False, unique=True),
    Column("correo", String(200), nullable=False, unique=True),
    Column("clave_hash", String(200), nullable=False),
    Column("carpeta", String(80), nullable=False, default=""),
    Column("creado", DateTime(timezone=True), nullable=False),
    Column("ultimo_ingreso", DateTime(timezone=True)),
    Column("clave_cambiada", DateTime(timezone=True)),
    Column("activo", Boolean, nullable=False, default=True),
)

sesiones = Table(
    "sesiones", metadata,
    Column("id", String(64), primary_key=True),          # hash del token (el token no se guarda)
    Column("usuario_id", Integer, nullable=False, index=True),
    Column("creada", DateTime(timezone=True), nullable=False),
    Column("vence", DateTime(timezone=True), nullable=False),
    Column("ultima_vez", DateTime(timezone=True), nullable=False),
    Column("agente", String(200), default=""),
    Column("ip", String(64), default=""),
)

codigos = Table(
    "codigos", metadata,
    Column("id", Integer, primary_key=True),
    Column("usuario_id", Integer, nullable=False, index=True),
    Column("codigo_hash", String(64), nullable=False),
    Column("vence", DateTime(timezone=True), nullable=False),
    Column("intentos", Integer, nullable=False, default=0),
    Column("usado", Boolean, nullable=False, default=False),
)

viajes = Table(
    "viajes", metadata,
    Column("id", Integer, primary_key=True),
    Column("usuario_id", Integer, index=True),
    Column("creado", DateTime(timezone=True), nullable=False),
    Column("carpeta", String(200), default=""),             # carpeta del viaje dentro de la del usuario
    # Factura
    Column("fecha", Date),
    Column("factura_nro", String(40), default=""),
    Column("cliente", String(200), default=""),
    Column("cliente_cuit", String(20), default=""),
    Column("detalle", String(300), default=""),
    Column("remito", String(40), default=""),
    Column("cae", String(30), default=""),
    # Parámetros
    Column("toneladas", Float, nullable=False, default=0),
    Column("precio_tn", Float, nullable=False, default=0),
    Column("pct_chofer", Float, nullable=False, default=18),
    Column("precio_gasoil", Float, nullable=False, default=0),
    Column("litros", Float, nullable=False, default=0),
    Column("km", Float),
    Column("otros_gastos", Float, nullable=False, default=0),
    Column("notas", Text, default=""),
    # Factura PDF (ruta dentro de la carpeta del usuario)
    Column("factura_archivo", String(300), default=""),
    Column("factura_nombre", String(200), default=""),
    # Resultados (se guardan como quedaron al momento de guardar)
    Column("total", Float, nullable=False, default=0),
    Column("pago_chofer", Float, nullable=False, default=0),
    Column("costo_gasoil", Float, nullable=False, default=0),
    Column("total_gastos", Float, nullable=False, default=0),
    Column("ganancia", Float, nullable=False, default=0),
)

remitos = Table(
    "remitos", metadata,
    Column("id", Integer, primary_key=True),
    Column("usuario_id", Integer, index=True),
    Column("viaje_id", Integer, nullable=False, index=True),
    Column("archivo", String(300), nullable=False),
    Column("miniatura", String(300), default=""),
    Column("tipo", String(10), default="foto"),
    Column("nombre", String(200), default=""),
    Column("creado", DateTime(timezone=True), nullable=False),
)


def init_db() -> None:
    metadata.create_all(engine)
    _migrar()


def _migrar() -> None:
    """Agrega columnas nuevas a tablas que ya existían y agranda las que quedaron cortas (sin borrar datos)."""
    insp = inspect(engine)
    with engine.begin() as con:
        for tabla in (viajes, remitos):
            actuales = {c["name"]: c for c in insp.get_columns(tabla.name)}
            for col in tabla.columns:
                tipo = col.type.compile(dialect=engine.dialect)
                if col.name not in actuales:
                    con.execute(text(f"ALTER TABLE {tabla.name} ADD COLUMN {col.name} {tipo}"))
                elif (engine.dialect.name == "postgresql" and isinstance(col.type, String)
                      and getattr(actuales[col.name]["type"], "length", None)
                      and actuales[col.name]["type"].length < col.type.length):
                    con.execute(text(f"ALTER TABLE {tabla.name} ALTER COLUMN {col.name} TYPE {tipo}"))


def _fila(r) -> dict:
    d = dict(r._mapping)
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d


# ---------------- Usuarios ----------------

def contar_usuarios() -> int:
    with engine.connect() as con:
        return con.execute(select(func.count()).select_from(usuarios)).scalar_one()


def crear_usuario(usuario: str, correo: str, clave_hash: str) -> dict:
    with engine.begin() as con:
        uid = con.execute(insert(usuarios).values(
            usuario=usuario, correo=correo, clave_hash=clave_hash, creado=ahora(), activo=True,
            carpeta="")).inserted_primary_key[0]
        carpeta = f"{uid:04d}-{usuario}"
        con.execute(update(usuarios).where(usuarios.c.id == uid).values(carpeta=carpeta))
    return obtener_usuario(uid)


def obtener_usuario(uid: int) -> dict | None:
    with engine.connect() as con:
        r = con.execute(select(usuarios).where(usuarios.c.id == uid)).first()
        return _fila(r) if r else None


def buscar_usuario(usuario_o_correo: str) -> dict | None:
    x = (usuario_o_correo or "").strip().lower()
    with engine.connect() as con:
        r = con.execute(select(usuarios).where(
            (func.lower(usuarios.c.usuario) == x) | (func.lower(usuarios.c.correo) == x))).first()
        return _fila(r) if r else None


def existe(usuario: str = "", correo: str = "") -> tuple[bool, bool]:
    with engine.connect() as con:
        u = con.execute(select(usuarios.c.id).where(func.lower(usuarios.c.usuario) == usuario.lower())).first()
        c = con.execute(select(usuarios.c.id).where(func.lower(usuarios.c.correo) == correo.lower())).first()
        return bool(u), bool(c)


def cambiar_clave(uid: int, clave_hash: str) -> None:
    with engine.begin() as con:
        con.execute(update(usuarios).where(usuarios.c.id == uid).values(clave_hash=clave_hash, clave_cambiada=ahora()))


def marcar_ingreso(uid: int) -> None:
    with engine.begin() as con:
        con.execute(update(usuarios).where(usuarios.c.id == uid).values(ultimo_ingreso=ahora()))


# ---------------- Sesiones ----------------

DIAS_SESION = 30


def crear_sesion(uid: int, token_hash: str, agente: str, ip: str) -> datetime:
    vence = ahora() + timedelta(days=DIAS_SESION)
    with engine.begin() as con:
        con.execute(insert(sesiones).values(id=token_hash, usuario_id=uid, creada=ahora(), vence=vence,
                                            ultima_vez=ahora(), agente=agente[:200], ip=ip[:64]))
    return vence


def usuario_de_sesion(token_hash: str) -> dict | None:
    with engine.begin() as con:
        r = con.execute(select(sesiones).where(sesiones.c.id == token_hash)).first()
        if not r:
            return None
        s = r._mapping
        vence = s["vence"] if s["vence"].tzinfo else s["vence"].replace(tzinfo=timezone.utc)
        if vence < ahora():
            con.execute(delete(sesiones).where(sesiones.c.id == token_hash))
            return None
        ultima = s["ultima_vez"] if s["ultima_vez"].tzinfo else s["ultima_vez"].replace(tzinfo=timezone.utc)
        if ahora() - ultima > timedelta(minutes=5):   # no escribir en cada pedido
            con.execute(update(sesiones).where(sesiones.c.id == token_hash).values(ultima_vez=ahora()))
        u = con.execute(select(usuarios).where(and_(usuarios.c.id == s["usuario_id"], usuarios.c.activo))).first()
        return _fila(u) if u else None


def borrar_sesion(token_hash: str) -> None:
    with engine.begin() as con:
        con.execute(delete(sesiones).where(sesiones.c.id == token_hash))


def borrar_sesiones(uid: int, excepto: str | None = None) -> int:
    with engine.begin() as con:
        q = delete(sesiones).where(sesiones.c.usuario_id == uid)
        if excepto:
            q = q.where(sesiones.c.id != excepto)
        return con.execute(q).rowcount


def contar_sesiones(uid: int) -> int:
    with engine.connect() as con:
        return con.execute(select(func.count()).select_from(sesiones).where(
            and_(sesiones.c.usuario_id == uid, sesiones.c.vence > ahora()))).scalar_one()


# ---------------- Códigos de recuperación ----------------

def crear_codigo(uid: int, codigo_hash: str, minutos: int) -> None:
    with engine.begin() as con:
        # un solo código vigente por cuenta
        con.execute(update(codigos).where(codigos.c.usuario_id == uid).values(usado=True))
        con.execute(insert(codigos).values(usuario_id=uid, codigo_hash=codigo_hash,
                                           vence=ahora() + timedelta(minutes=minutos), intentos=0, usado=False))


def validar_codigo(uid: int, codigo_hash: str, max_intentos: int) -> bool:
    """True si el código es correcto y vigente. Cada intento fallido suma; al llegar al máximo se invalida."""
    with engine.begin() as con:
        r = con.execute(select(codigos).where(and_(codigos.c.usuario_id == uid, codigos.c.usado.is_(False)))
                        .order_by(codigos.c.id.desc())).first()
        if not r:
            return False
        c = r._mapping
        vence = c["vence"] if c["vence"].tzinfo else c["vence"].replace(tzinfo=timezone.utc)
        if vence < ahora() or c["intentos"] >= max_intentos:
            con.execute(update(codigos).where(codigos.c.id == c["id"]).values(usado=True))
            return False
        import hmac as _h
        if _h.compare_digest(c["codigo_hash"], codigo_hash):
            con.execute(update(codigos).where(codigos.c.id == c["id"]).values(usado=True))
            return True
        con.execute(update(codigos).where(codigos.c.id == c["id"]).values(intentos=c["intentos"] + 1))
        return False


# ---------------- Viajes (siempre filtrados por usuario) ----------------

def calcular(p: dict) -> dict:
    """Misma cuenta que hace la pantalla. Única fuente de verdad al guardar."""
    total = p["toneladas"] * p["precio_tn"]
    pago_chofer = total * p["pct_chofer"] / 100
    costo_gasoil = p["precio_gasoil"] * p["litros"]
    total_gastos = pago_chofer + costo_gasoil + p["otros_gastos"]
    return {
        "total": round(total, 2),
        "pago_chofer": round(pago_chofer, 2),
        "costo_gasoil": round(costo_gasoil, 2),
        "total_gastos": round(total_gastos, 2),
        "ganancia": round(total - total_gastos, 2),
    }


def crear_viaje(uid: int, datos: dict) -> int:
    valores = {**datos, **calcular(datos), "usuario_id": uid, "creado": ahora()}
    with engine.begin() as con:
        return con.execute(insert(viajes).values(**valores)).inserted_primary_key[0]


def actualizar_viaje(uid: int, viaje_id: int, **valores) -> None:
    with engine.begin() as con:
        con.execute(update(viajes).where(and_(viajes.c.id == viaje_id, viajes.c.usuario_id == uid)).values(**valores))


def listar(uid: int) -> list[dict]:
    with engine.connect() as con:
        q = select(viajes).where(viajes.c.usuario_id == uid).order_by(viajes.c.fecha.desc(), viajes.c.id.desc())
        filas = [_fila(r) for r in con.execute(q)]
        cuentas = dict(con.execute(select(remitos.c.viaje_id, func.count()).where(remitos.c.usuario_id == uid)
                                   .group_by(remitos.c.viaje_id)).all())
    for f in filas:
        f["remitos_cant"] = cuentas.get(f["id"], 0)
    return filas


def obtener(uid: int, viaje_id: int) -> dict | None:
    with engine.connect() as con:
        r = con.execute(select(viajes).where(and_(viajes.c.id == viaje_id, viajes.c.usuario_id == uid))).first()
        if not r:
            return None
        v = _fila(r)
        q = select(remitos).where(and_(remitos.c.viaje_id == viaje_id, remitos.c.usuario_id == uid)).order_by(remitos.c.id)
        v["remitos"] = [_fila(x) for x in con.execute(q)]
        v["remitos_cant"] = len(v["remitos"])
        return v


def agregar_remito(uid: int, viaje_id: int, foto: dict) -> None:
    with engine.begin() as con:
        con.execute(insert(remitos).values(usuario_id=uid, viaje_id=viaje_id, creado=ahora(), **foto))


def quitar_remito(uid: int, remito_id: int) -> int | None:
    """Saca la foto del viaje (el archivo queda en la carpeta). Devuelve el id del viaje."""
    with engine.begin() as con:
        r = con.execute(select(remitos.c.viaje_id).where(
            and_(remitos.c.id == remito_id, remitos.c.usuario_id == uid))).first()
        if not r:
            return None
        con.execute(delete(remitos).where(remitos.c.id == remito_id))
        return r[0]


def borrar(uid: int, viaje_id: int) -> bool:
    with engine.begin() as con:
        n = con.execute(delete(viajes).where(and_(viajes.c.id == viaje_id, viajes.c.usuario_id == uid))).rowcount
        if n:
            con.execute(delete(remitos).where(and_(remitos.c.viaje_id == viaje_id, remitos.c.usuario_id == uid)))
        return n > 0


# ---------------- Datos anteriores a las cuentas ----------------

def viajes_sin_duenio() -> list[dict]:
    with engine.connect() as con:
        filas = [_fila(r) for r in con.execute(select(viajes).where(viajes.c.usuario_id.is_(None)))]
        for v in filas:
            v["remitos"] = [_fila(x) for x in con.execute(
                select(remitos).where(remitos.c.viaje_id == v["id"]).order_by(remitos.c.id))]
        return filas


def asignar_duenio(uid: int, viaje_id: int) -> None:
    with engine.begin() as con:
        con.execute(update(viajes).where(viajes.c.id == viaje_id).values(usuario_id=uid))
        con.execute(update(remitos).where(remitos.c.viaje_id == viaje_id).values(usuario_id=uid))


def actualizar_remito_rutas(remito_id: int, archivo: str, miniatura: str) -> None:
    with engine.begin() as con:
        con.execute(update(remitos).where(remitos.c.id == remito_id).values(archivo=archivo, miniatura=miniatura))
