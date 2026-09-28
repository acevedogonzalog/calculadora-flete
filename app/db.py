"""Base de datos de viajes.

En Railway se usa PostgreSQL (variable DATABASE_URL). En la compu, si no hay
DATABASE_URL, se usa un archivo SQLite local (viajes.db).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import (
    Column, Date, DateTime, Float, Integer, MetaData, String, Table, Text,
    create_engine, delete, insert, select,
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

viajes = Table(
    "viajes", metadata,
    Column("id", Integer, primary_key=True),
    Column("creado", DateTime(timezone=True), nullable=False),
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
    # Resultados (se guardan como quedaron al momento de guardar)
    Column("total", Float, nullable=False, default=0),
    Column("pago_chofer", Float, nullable=False, default=0),
    Column("costo_gasoil", Float, nullable=False, default=0),
    Column("total_gastos", Float, nullable=False, default=0),
    Column("ganancia", Float, nullable=False, default=0),
)


def init_db() -> None:
    metadata.create_all(engine)


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


def _fila(r) -> dict:
    d = dict(r._mapping)
    d["fecha"] = d["fecha"].isoformat() if d.get("fecha") else None
    d["creado"] = d["creado"].isoformat() if d.get("creado") else None
    return d


def crear(datos: dict) -> dict:
    valores = {**datos, **calcular(datos), "creado": datetime.now(timezone.utc)}
    with engine.begin() as con:
        nuevo_id = con.execute(insert(viajes).values(**valores)).inserted_primary_key[0]
    return obtener(nuevo_id)


def listar() -> list[dict]:
    with engine.connect() as con:
        q = select(viajes).order_by(viajes.c.fecha.desc(), viajes.c.id.desc())
        return [_fila(r) for r in con.execute(q)]


def obtener(viaje_id: int) -> dict | None:
    with engine.connect() as con:
        r = con.execute(select(viajes).where(viajes.c.id == viaje_id)).first()
        return _fila(r) if r else None


def borrar(viaje_id: int) -> bool:
    with engine.begin() as con:
        return con.execute(delete(viajes).where(viajes.c.id == viaje_id)).rowcount > 0
