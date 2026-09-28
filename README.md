# Calculadora de Flete

Aplicación web para viajes de camión de carga. Funciona en la PC y en el celular.

- **Calculadora**: total de la factura, pago al chofer (porcentaje sobre el total), combustible, otros gastos, ganancia neta e indicadores por tonelada y por km.
- **Carga de factura**: se sube el PDF de la factura electrónica de ARCA y se completan solos la fecha, el número, el cliente, el detalle, el remito, las toneladas y el precio.
- **Viajes**: el botón *Guardar viaje* registra el viaje. En la sección *Viajes* está el historial con totales; al tocar un viaje se ve el detalle y se puede exportar a Excel o eliminar.
- **Resumen mensual**: dentro de *Viajes*, toma los viajes de un mes calendario (del 1 al último día, según la fecha de la factura). Muestra totales, comparación con el mes anterior, una lectura automática del mes y gráficos. Se exporta a Excel con los totales, indicadores, gráficos y la lista de viajes del mes.
- **Factura guardada**: el PDF que se sube queda guardado en `/data/facturas` junto con el viaje. En el detalle del viaje está el botón *Ver factura*; a los viajes sin factura se les puede adjuntar el PDF después.
- **Remitos**: fotos del remito sacadas con el celular (o elegidas de la galería), varias por viaje. Se cargan antes de guardar el viaje o después, desde el detalle. Se guardan en `/data/remitos`, enderezadas y reducidas a 2400 px en JPEG, con miniatura. Acepta JPG, PNG, HEIC (iPhone) y PDF.
- **Exportar**: Excel de un viaje con todo el detalle, Excel del resumen mensual o Excel con todos los viajes.

## Estructura

```
main.py              Arranque del servidor (usa la variable PORT)
app/server.py        API (FastAPI) y página
app/factura.py       Lectura de la factura PDF (pdfplumber)
app/excel.py         Exportación a Excel (openpyxl)
app/resumen.py       Cálculos del resumen mensual
app/almacen.py       Guardado de facturas (/data/facturas) y fotos de remitos (/data/remitos)
app/db.py            Base de datos (PostgreSQL en Railway, SQLite en la compu)
public/index.html    Interfaz
requirements.txt     Librerías de Python
railway.json         Comando de inicio para Railway
```

## Correr en la compu

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
# abrir http://localhost:8000
```

Sin `DATABASE_URL` guarda los viajes en el archivo `viajes.db`.

## Variables en Railway

| Variable | Para qué |
|---|---|
| `DATABASE_URL` | Base PostgreSQL donde se guardan los viajes. Poner `${{Postgres.DATABASE_URL}}`. **Sin esto los viajes se borran en cada despliegue.** |
| `APP_PASSWORD` | Opcional pero recomendado. Si está definida, la app pide esta clave para entrar. |
| `DATA_DIR` | Opcional. Carpeta base de los archivos; por defecto `/data` (facturas en `/data/facturas`, remitos en `/data/remitos`). |

**Volume para las facturas:** en Railway, `Ctrl+K → Create Volume`, elegir el servicio `calculadora-flete` y poner como mount path `/data`. Sin el Volume, las facturas y las fotos de remitos se borran en cada deploy (la app lo avisa en los Deploy Logs).

## Facturas

El lector está pensado para la factura C que genera ARCA (Comprobantes en línea). Toma la primera página (ORIGINAL) y busca los datos por texto. Si la unidad está en kilos, la pasa a toneladas. Si algún dato no aparece, avisa en pantalla para completarlo a mano. No funciona con fotos o escaneos: hay que subir el PDF descargado de ARCA.
