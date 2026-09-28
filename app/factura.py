"""Lectura de facturas electrónicas de ARCA (ex AFIP) en PDF.

Las facturas que genera ARCA son PDF con texto (no imágenes), así que alcanza
con extraer el texto de la primera página (el ORIGINAL) y buscar cada dato con
expresiones regulares. Si un dato no aparece, se devuelve vacío y se avisa en
`avisos`, para que el usuario lo complete a mano.
"""
from __future__ import annotations

import io
import re
from typing import Any

import pdfplumber

# Unidades que se interpretan como toneladas o kilos
UNIDADES_TN = {"toneladas", "tonelada", "tn", "tns", "ton", "t"}
UNIDADES_KG = {"kilogramos", "kilogramo", "kg", "kgs", "kilos", "kilo"}

NUM = r"-?[\d.]*\d,\d{2}"  # número en formato argentino: 1.513.372,00 o 1513372,00


def num_ar(texto: str | None) -> float | None:
    """'1.513.372,00' -> 1513372.0"""
    if not texto:
        return None
    try:
        return float(texto.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _buscar(patron: str, texto: str, flags: int = 0) -> str | None:
    m = re.search(patron, texto, flags)
    return m.group(1).strip() if m else None


def extraer_texto(pdf_bytes: bytes) -> str:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        if not pdf.pages:
            return ""
        return pdf.pages[0].extract_text() or ""


def parsear_texto(texto: str) -> dict[str, Any]:
    datos: dict[str, Any] = {}
    avisos: list[str] = []

    pv = _buscar(r"Punto de Venta:\s*(\d+)", texto)
    nro = _buscar(r"Comp\.?\s*Nro:?\s*(\d+)", texto)
    datos["factura_nro"] = f"{pv}-{nro}" if pv and nro else (nro or "")
    datos["tipo"] = _buscar(r"^\s*([ABCEM])\s*$", texto, re.M) or ""

    fecha = _buscar(r"Fecha de Emisi[oó]n:\s*(\d{2}/\d{2}/\d{4})", texto)
    if fecha:
        d, m, a = fecha.split("/")
        datos["fecha"] = f"{a}-{m}-{d}"  # ISO para el campo de fecha
    else:
        datos["fecha"] = ""

    # El primer CUIT es el del emisor; el que está en la misma línea que
    # "Razón Social:" del receptor es el del cliente.
    cuits = re.findall(r"CUIT:\s*(\d{11})", texto)
    datos["emisor_cuit"] = cuits[0] if cuits else ""
    datos["cliente_cuit"] = _buscar(r"CUIT:\s*(\d{11})\s+Apellido y Nombre", texto) or (
        cuits[1] if len(cuits) > 1 else ""
    )
    datos["cliente"] = _buscar(r"Apellido y Nombre / Raz[oó]n Social:\s*(.+)", texto) or ""

    # Línea del ítem: DESCRIPCIÓN  CANTIDAD  UNIDAD  PRECIO  %BONIF  IMP.BONIF  SUBTOTAL
    item = re.search(
        rf"^(?P<desc>.+?)\s+(?P<cant>{NUM})\s+(?P<unidad>[A-Za-zÁÉÍÓÚáéíóú.]+)\s+"
        rf"(?P<precio>{NUM})(?:\s+{NUM}){{0,2}}\s+(?P<subtotal>{NUM})\s*$",
        texto,
        re.M,
    )
    cantidad = precio = None
    if item:
        datos["detalle"] = item.group("desc").strip()
        cantidad = num_ar(item.group("cant"))
        precio = num_ar(item.group("precio"))
        unidad = item.group("unidad").lower().rstrip(".")
        datos["unidad_original"] = unidad
        if unidad in UNIDADES_KG and cantidad is not None and precio is not None:
            cantidad, precio = cantidad / 1000, precio * 1000
            avisos.append("La factura está en kilos: se convirtió a toneladas.")
        elif unidad not in UNIDADES_TN:
            avisos.append(f"Unidad '{unidad}' no reconocida: se usó la cantidad tal cual.")
    else:
        datos["detalle"] = ""
        avisos.append("No se encontró la línea del producto: cargá cantidad y precio a mano.")

    datos["toneladas"] = cantidad
    datos["precio_tn"] = precio
    datos["remito"] = _buscar(r"REMITO\s*N?[°º]?\s*([\d-]+)", texto, re.I) or ""
    datos["importe_total"] = num_ar(_buscar(rf"Importe Total:\s*\$?\s*({NUM})", texto))
    datos["cae"] = _buscar(r"CAE N?[°º]?:\s*(\d+)", texto) or ""

    total = datos["importe_total"]
    if cantidad and precio and total and abs(cantidad * precio - total) > 1:
        avisos.append(
            "Cantidad × precio no coincide con el importe total de la factura "
            "(puede haber varios ítems o bonificaciones). Revisá los valores."
        )
    if not datos["fecha"]:
        avisos.append("No se encontró la fecha de emisión.")
    if not datos["cliente"]:
        avisos.append("No se encontró el cliente.")

    datos["avisos"] = avisos
    return datos


def leer_factura(pdf_bytes: bytes) -> dict[str, Any]:
    texto = extraer_texto(pdf_bytes)
    if not texto.strip():
        return {
            "avisos": [
                "El PDF no tiene texto legible (puede ser una foto o un escaneo). "
                "Descargá la factura original desde ARCA."
            ]
        }
    return parsear_texto(texto)
