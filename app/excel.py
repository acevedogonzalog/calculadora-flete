"""Exportación de viajes a Excel.

Los resultados se escriben como valores (no fórmulas) porque son el registro
de cómo quedó el viaje al guardarlo, y así se ven bien en cualquier visor,
incluido el celular.
"""
from __future__ import annotations

import io
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

F = "Arial"
MONEY = '"$" #,##0.00;[Red]-"$" #,##0.00;"-"'
NUM = '#,##0.00;-#,##0.00;"-"'
PCT = '0.0%;[Red]-0.0%;"-"'
VERDE = "1E6B4F"
thin = Side(style="thin", color="C9CFCB")
BOX = Border(left=thin, right=thin, top=thin, bottom=thin)
F_TIT = Font(name=F, size=15, bold=True, color="FFFFFF")
F_SEC = Font(name=F, size=11, bold=True, color="FFFFFF")
F_LBL = Font(name=F, size=11)
F_VAL = Font(name=F, size=11)
F_BOLD = Font(name=F, size=11, bold=True)
FILL_TIT = PatternFill("solid", fgColor="1B2220")
FILL_SEC = PatternFill("solid", fgColor=VERDE)
FILL_RES = PatternFill("solid", fgColor="E3EFE9")


def _texto_seguro(ws, celda):
    """Un texto que empieza con = + - @ se guarda como texto, nunca como fórmula (inyección de fórmulas)."""
    if isinstance(celda.value, str) and celda.value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        celda.data_type = "s"
    return celda


def _fecha(v):
    return date.fromisoformat(v) if v else None


def _div(a, b):
    return a / b if b else None


def excel_viaje(v: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Viaje"
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 2
    ws.column_dimensions["B"].width = 36
    ws.column_dimensions["C"].width = 30
    fila = 2

    ws.merge_cells(start_row=fila, start_column=2, end_row=fila, end_column=3)
    c = _texto_seguro(ws, ws.cell(fila, 2, f"VIAJE Nº {v['id']} · {v.get('cliente') or 'Sin cliente'}"))
    c.font, c.fill = F_TIT, FILL_TIT
    c.alignment = Alignment(vertical="center", indent=1)
    ws.row_dimensions[fila].height = 28
    fila += 2

    def seccion(titulo):
        nonlocal fila
        ws.merge_cells(start_row=fila, start_column=2, end_row=fila, end_column=3)
        c = ws.cell(fila, 2, titulo)
        c.font, c.fill = F_SEC, FILL_SEC
        c.alignment = Alignment(indent=1)
        fila += 1

    def linea(etq, valor, fmt=None, destacar=False):
        nonlocal fila
        a = ws.cell(fila, 2, etq)
        b = _texto_seguro(ws, ws.cell(fila, 3, valor))
        a.font = F_BOLD if destacar else F_LBL
        b.font = F_BOLD if destacar else F_VAL
        a.border = b.border = BOX
        a.alignment = Alignment(indent=1)
        if fmt:
            b.number_format = fmt
        if isinstance(valor, (int, float)) or fmt:
            b.alignment = Alignment(horizontal="right")
        if destacar:
            a.fill = b.fill = FILL_RES
        fila += 1

    seccion("FACTURA")
    linea("Fecha", _fecha(v.get("fecha")), "dd/mm/yyyy")
    linea("Nº de factura", v.get("factura_nro") or "")
    linea("Cliente", v.get("cliente") or "")
    linea("CUIT cliente", v.get("cliente_cuit") or "")
    linea("Detalle", v.get("detalle") or "")
    linea("Remito", v.get("remito") or "")
    linea("CAE", v.get("cae") or "")
    linea("Factura PDF", "Guardada en la app" if v.get("factura_archivo") else "No cargada")
    linea("Fotos de remito", v.get("remitos_cant") or 0)
    fila += 1

    seccion("DATOS DEL VIAJE")
    linea("Toneladas", v["toneladas"], NUM)
    linea("Precio por tonelada", v["precio_tn"], MONEY)
    linea("Porcentaje chofer", v["pct_chofer"] / 100, PCT)
    linea("Precio gasoil por litro", v["precio_gasoil"], MONEY)
    linea("Litros de gasoil", v["litros"], NUM)
    linea("Kilómetros", v.get("km"), NUM)
    linea("Otros gastos", v["otros_gastos"], MONEY)
    fila += 1

    seccion("RESULTADOS")
    linea("Total factura", v["total"], MONEY, destacar=True)
    linea("Pago al chofer", v["pago_chofer"], MONEY)
    linea("Costo de combustible", v["costo_gasoil"], MONEY)
    linea("Otros gastos", v["otros_gastos"], MONEY)
    linea("Total gastos", v["total_gastos"], MONEY, destacar=True)
    linea("Ganancia neta", v["ganancia"], MONEY, destacar=True)
    fila += 1

    seccion("INDICADORES")
    km = v.get("km") or 0
    linea("Margen de ganancia", _div(v["ganancia"], v["total"]), PCT)
    linea("Combustible sobre la factura", _div(v["costo_gasoil"], v["total"]), PCT)
    linea("Ganancia por tonelada", _div(v["ganancia"], v["toneladas"]), MONEY)
    linea("Gasoil por tonelada", _div(v["costo_gasoil"], v["toneladas"]), MONEY)
    linea("Consumo (L cada 100 km)", _div(v["litros"] * 100, km), NUM)
    linea("Costo por km", _div(v["total_gastos"], km), MONEY)
    linea("Ganancia por km", _div(v["ganancia"], km), MONEY)

    if v.get("notas"):
        fila += 1
        seccion("NOTAS")
        ws.merge_cells(start_row=fila, start_column=2, end_row=fila, end_column=3)
        c = _texto_seguro(ws, ws.cell(fila, 2, v["notas"]))
        c.font = F_VAL
        c.alignment = Alignment(wrap_text=True, vertical="top", indent=1)
        ws.row_dimensions[fila].height = max(30, 15 * (1 + len(v["notas"]) // 60))

    return _guardar(wb)


COLUMNAS = [
    ("Nº", "id", None, 6),
    ("Fecha", "fecha", "dd/mm/yyyy", 12),
    ("Factura", "factura_nro", None, 17),
    ("Cliente", "cliente", None, 26),
    ("Detalle", "detalle", None, 28),
    ("Remito", "remito", None, 16),
    ("Toneladas", "toneladas", NUM, 11),
    ("Precio x tn", "precio_tn", MONEY, 14),
    ("Total factura", "total", MONEY, 16),
    ("% Chofer", "pct_chofer", PCT, 10),
    ("Pago chofer", "pago_chofer", MONEY, 15),
    ("Gasoil $/L", "precio_gasoil", MONEY, 12),
    ("Litros", "litros", NUM, 10),
    ("Costo gasoil", "costo_gasoil", MONEY, 15),
    ("Km", "km", NUM, 10),
    ("Otros gastos", "otros_gastos", MONEY, 14),
    ("Total gastos", "total_gastos", MONEY, 15),
    ("Ganancia neta", "ganancia", MONEY, 16),
    ("Notas", "notas", None, 30),
]
SUMAR = {"toneladas", "total", "pago_chofer", "litros", "costo_gasoil", "km",
         "otros_gastos", "total_gastos", "ganancia"}


def _tabla_viajes(ws, lista: list[dict]) -> int:
    """Escribe la tabla de viajes con fila de totales. Devuelve la fila de totales."""
    ws.freeze_panes = "A2"
    for col, (titulo, _, _, ancho) in enumerate(COLUMNAS, 1):
        c = ws.cell(1, col, titulo)
        c.font, c.fill, c.border = F_SEC, FILL_SEC, BOX
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[c.column_letter].width = ancho
    ws.row_dimensions[1].height = 30

    for r, v in enumerate(lista, 2):
        for col, (_, clave, fmt, _) in enumerate(COLUMNAS, 1):
            val = v.get(clave)
            if clave == "fecha":
                val = _fecha(val)
            elif clave == "pct_chofer":
                val = val / 100
            c = _texto_seguro(ws, ws.cell(r, col, val))
            c.font, c.border = F_VAL, BOX
            if fmt:
                c.number_format = fmt

    tr = len(lista) + 2
    ws.cell(tr, 1, "TOTAL")
    for col, (_, clave, fmt, _) in enumerate(COLUMNAS, 1):
        c = ws.cell(tr, col)
        c.font, c.fill, c.border = F_BOLD, FILL_RES, BOX
        if clave in SUMAR:
            c.value = sum((v.get(clave) or 0) for v in lista)
            c.number_format = fmt
    return tr


def excel_todos(lista: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Viajes"
    _tabla_viajes(ws, lista)
    return _guardar(wb)


COLORES = {"chofer": "3F6FA8", "gasoil": "B5770C", "otros": "8E5BA8", "ganancia": "23855A"}


def excel_resumen(r: dict) -> bytes:
    from openpyxl.chart import BarChart, Reference, Series

    t = r["totales"]
    ant = (r.get("anterior") or {}).get("totales")
    wb = Workbook()
    ws = wb.active
    ws.title = "Resumen"
    ws.sheet_view.showGridLines = False
    for col, w in zip("ABCDE", (2, 34, 20, 20, 14)):
        ws.column_dimensions[col].width = w

    ws.merge_cells("B2:E2")
    c = ws["B2"]
    c.value = f"RESUMEN MENSUAL · {r['nombre'].upper()}"
    c.font, c.fill = F_TIT, FILL_TIT
    c.alignment = Alignment(vertical="center", indent=1)
    ws.row_dimensions[2].height = 28
    ws["B3"] = f"{t['viajes']} viajes · del 1 al último día del mes, según la fecha de la factura"
    ws["B3"].font = Font(name=F, size=9, italic=True, color="66706C")
    fila = 5

    def encabezado(*titulos):
        nonlocal fila
        for i, tit in enumerate(titulos):
            c = ws.cell(fila, 2 + i, tit)
            c.font, c.fill, c.border = F_SEC, FILL_SEC, BOX
            c.alignment = Alignment(horizontal="left" if i == 0 else "center", indent=1 if i == 0 else 0)
        fila += 1

    def linea(etq, valor, fmt, clave=None, destacar=False):
        nonlocal fila
        a = ws.cell(fila, 2, etq)
        b = ws.cell(fila, 3, valor)
        a.alignment = Alignment(indent=1)
        b.number_format = fmt
        celdas = [a, b]
        if ant is not None:
            va = ant.get(clave) if clave else None
            d = ws.cell(fila, 4, va)
            d.number_format = fmt
            e = ws.cell(fila, 5, (valor - va) / abs(va) if (va and valor is not None) else None)
            e.number_format = '+0.0%;[Red]-0.0%;"-"'
            celdas += [d, e]
        for c in celdas:
            c.font = F_BOLD if destacar else F_VAL
            c.border = BOX
            if destacar:
                c.fill = FILL_RES
        fila += 1

    if ant is not None:
        encabezado("TOTALES DEL MES", r["nombre"], r["anterior"]["nombre"], "Variación")
    else:
        encabezado("TOTALES DEL MES", r["nombre"])
    linea("Viajes", t["viajes"], "0", "viajes")
    linea("Toneladas transportadas", t["toneladas"], NUM, "toneladas")
    linea("Total facturado", t["total"], MONEY, "total", True)
    linea("Pago al chofer", t["pago_chofer"], MONEY, "pago_chofer")
    linea("Combustible", t["costo_gasoil"], MONEY, "costo_gasoil")
    linea("Otros gastos", t["otros_gastos"], MONEY, "otros_gastos")
    linea("Total gastos", t["total_gastos"], MONEY, "total_gastos", True)
    linea("Ganancia neta", t["ganancia"], MONEY, "ganancia", True)
    linea("Litros de gasoil", t["litros"], NUM, "litros")
    linea("Kilómetros recorridos", t["km"], NUM, "km")
    fila += 1

    if ant is not None:
        encabezado("INDICADORES", r["nombre"], r["anterior"]["nombre"], "Variación")
    else:
        encabezado("INDICADORES", r["nombre"])
    linea("Margen de ganancia", t["margen"], PCT, "margen")
    linea("Ganancia promedio por viaje", t["ganancia_por_viaje"], MONEY, "ganancia_por_viaje")
    linea("Facturado promedio por viaje", t["facturado_por_viaje"], MONEY, "facturado_por_viaje")
    linea("Precio promedio por tonelada", t["precio_tn_prom"], MONEY, "precio_tn_prom")
    linea("Ganancia por tonelada", t["ganancia_tn"], MONEY, "ganancia_tn")
    linea("Gasoil por tonelada", t["gasoil_tn"], MONEY, "gasoil_tn")
    linea("Precio promedio del gasoil ($/L)", t["precio_gasoil_prom"], MONEY, "precio_gasoil_prom")
    linea("Consumo (L cada 100 km)", t["consumo_100km"], NUM, "consumo_100km")
    linea("Costo por km", t["costo_km"], MONEY, "costo_km")
    linea("Ganancia por km", t["ganancia_km"], MONEY, "ganancia_km")
    if t["viajes_con_km"] < t["viajes"]:
        ws.cell(fila, 2, f"Indicadores por km calculados con {t['viajes_con_km']} de {t['viajes']} viajes (los que tienen km cargados).").font = \
            Font(name=F, size=9, italic=True, color="66706C")
        fila += 1
    fila += 1

    # Distribución del facturado (datos del gráfico)
    encabezado("DISTRIBUCIÓN DEL FACTURADO", "Monto", "% del total")
    dist_ini = fila
    for etq, clave in (("Chofer", "pago_chofer"), ("Combustible", "costo_gasoil"),
                       ("Otros gastos", "otros_gastos"), ("Ganancia neta", "ganancia")):
        a = ws.cell(fila, 2, etq)
        b = ws.cell(fila, 3, t[clave])
        c = ws.cell(fila, 4, t[clave] / t["total"] if t["total"] else None)
        b.number_format, c.number_format = MONEY, PCT
        for x in (a, b, c):
            x.font, x.border = F_VAL, BOX
        a.alignment = Alignment(indent=1)
        fila += 1

    ch = BarChart()
    ch.type = "bar"
    ch.title = "¿A dónde va lo facturado?"
    ch.style = 10
    ch.legend = None
    ch.add_data(Reference(ws, min_col=3, min_row=dist_ini, max_row=dist_ini + 3), titles_from_data=False)
    ch.set_categories(Reference(ws, min_col=2, min_row=dist_ini, max_row=dist_ini + 3))
    from openpyxl.chart.series import DataPoint
    s0 = ch.series[0]
    for i, col in enumerate(COLORES.values()):
        pt = DataPoint(idx=i)
        pt.graphicalProperties.solidFill = col
        pt.graphicalProperties.line.solidFill = col
        s0.dPt.append(pt)
    ch.y_axis.numFmt = '"$" #,##0'
    ch.y_axis.majorGridlines = None
    ch.x_axis.scaling.orientation = "maxMin"
    ch.height, ch.width = 7, 16
    ch.x_axis.delete = False
    ch.y_axis.delete = False
    ws.add_chart(ch, f"B{fila + 1}")

    # Hoja con los viajes del mes + gráfico por viaje
    wv = wb.create_sheet("Viajes del mes")
    lista = r["viajes"]
    tr = _tabla_viajes(wv, lista)
    if lista:
        idx = {clave: i for i, (_, clave, _, _) in enumerate(COLUMNAS, 1)}
        cols = [("Chofer", "pago_chofer", "chofer"), ("Combustible", "costo_gasoil", "gasoil"),
                ("Otros gastos", "otros_gastos", "otros"), ("Ganancia", "ganancia", "ganancia")]
        bc = BarChart()
        bc.type = "col"
        bc.grouping = "stacked"
        bc.overlap = 100
        bc.title = "Facturado por viaje y cómo se reparte"
        bc.style = 10
        for titulo, clave, color in cols:
            ser = Series(Reference(wv, min_col=idx[clave], min_row=2, max_row=tr - 1), title=titulo)
            ser.graphicalProperties.solidFill = COLORES[color]
            ser.graphicalProperties.line.solidFill = COLORES[color]
            bc.series.append(ser)
        bc.set_categories(Reference(wv, min_col=idx["fecha"], min_row=2, max_row=tr - 1))
        bc.x_axis.number_format = "dd/mm"
        bc.y_axis.numFmt = '"$" #,##0'
        bc.x_axis.delete = False
        bc.y_axis.delete = False
        bc.height, bc.width = 9, 22
        bc.legend.position = "b"
        wv.add_chart(bc, f"B{tr + 3}")
    return _guardar(wb)


def _guardar(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
