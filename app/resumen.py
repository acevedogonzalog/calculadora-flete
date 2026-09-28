"""Resumen mensual: agrupa los viajes por mes calendario (1 al último día)."""
from __future__ import annotations

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def mes_de(v: dict) -> str:
    """'2026-09'. Usa la fecha de la factura; si no hay, la fecha en que se guardó."""
    return (v.get("fecha") or v.get("creado") or "")[:7]


def nombre_mes(mes: str) -> str:
    anio, m = mes.split("-")
    return f"{MESES[int(m) - 1].capitalize()} {anio}"


def mes_anterior(mes: str) -> str:
    anio, m = map(int, mes.split("-"))
    return f"{anio - 1}-12" if m == 1 else f"{anio}-{m - 1:02d}"


def _div(a, b):
    return round(a / b, 4) if b else None


def totales(lista: list[dict]) -> dict:
    s = lambda k: round(sum((v.get(k) or 0) for v in lista), 2)
    con_km = [v for v in lista if (v.get("km") or 0) > 0]
    km = sum(v["km"] for v in con_km)
    litros_km = sum(v["litros"] for v in con_km)
    t = {
        "viajes": len(lista),
        "toneladas": s("toneladas"),
        "total": s("total"),
        "pago_chofer": s("pago_chofer"),
        "costo_gasoil": s("costo_gasoil"),
        "otros_gastos": s("otros_gastos"),
        "total_gastos": s("total_gastos"),
        "ganancia": s("ganancia"),
        "litros": s("litros"),
        "km": round(km, 2),
        "viajes_con_km": len(con_km),
    }
    t.update({
        "margen": _div(t["ganancia"], t["total"]),
        "pct_chofer": _div(t["pago_chofer"], t["total"]),
        "pct_gasoil": _div(t["costo_gasoil"], t["total"]),
        "pct_otros": _div(t["otros_gastos"], t["total"]),
        "ganancia_por_viaje": _div(t["ganancia"], t["viajes"]),
        "facturado_por_viaje": _div(t["total"], t["viajes"]),
        "ganancia_tn": _div(t["ganancia"], t["toneladas"]),
        "gasoil_tn": _div(t["costo_gasoil"], t["toneladas"]),
        "precio_tn_prom": _div(t["total"], t["toneladas"]),
        "precio_gasoil_prom": _div(t["costo_gasoil"], t["litros"]),
        # Indicadores por km: solo con los viajes que tienen km cargados
        "consumo_100km": _div(litros_km * 100, km),
        "costo_km": _div(sum(v["total_gastos"] for v in con_km), km),
        "ganancia_km": _div(sum(v["ganancia"] for v in con_km), km),
    })
    return t


def resumen(todos: list[dict], mes: str | None) -> dict:
    meses = sorted({mes_de(v) for v in todos if mes_de(v)}, reverse=True)
    if not mes:
        mes = meses[0] if meses else None
    if not mes:
        return {"mes": None, "meses": [], "viajes": [], "totales": totales([]), "anterior": None}
    del_mes = sorted((v for v in todos if mes_de(v) == mes),
                     key=lambda v: (v.get("fecha") or "", v["id"]))
    ant = mes_anterior(mes)
    del_ant = [v for v in todos if mes_de(v) == ant]
    return {
        "mes": mes,
        "nombre": nombre_mes(mes),
        "meses": [{"mes": m, "nombre": nombre_mes(m)} for m in meses],
        "viajes": del_mes,
        "totales": totales(del_mes),
        "anterior": {"mes": ant, "nombre": nombre_mes(ant), "totales": totales(del_ant)} if del_ant else None,
    }
