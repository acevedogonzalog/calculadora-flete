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
app/almacen.py       Archivos ordenados por usuario y por viaje en /data/usuarios
app/cuentas.py       Contraseñas, límites de intentos y correo (Brevo)
app/respaldo.py      Respaldos automáticos y restauración de la base
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

## Cuentas de usuario

Cada persona entra con su usuario (o correo) y contraseña, y ve solo sus viajes, facturas y remitos.

- **Crear cuenta**: usuario, correo, contraseña y el código de invitación (`CODIGO_INVITACION`). Si todavía no hay ninguna cuenta, o si es la primera vez que se entra desde ese navegador, la app abre directamente "Crear cuenta".
- **Mantener la sesión iniciada** (tildado por defecto): no vuelve a pedir la contraseña mientras se use la app al menos una vez cada 90 días. Sin tildar, la sesión se cierra al cerrar el navegador (o a las 12 horas).
- **Olvidé mi contraseña**: llega un código de 6 números al correo (vence en 15 minutos, 5 intentos).
- **Mi cuenta**: datos, cambiar contraseña, cerrar sesión en los otros dispositivos, carpeta y espacio usado.
- La primera cuenta que se crea recibe los viajes cargados antes de que existieran las cuentas.

## Variables en Railway

| Variable | Para qué |
|---|---|
| `DATABASE_URL` | Opcional. Base PostgreSQL (`${{Postgres.DATABASE_URL}}`). Sin esta variable, la base es el archivo `/data/viajes.db` dentro del Volume. |
| `CODIGO_INVITACION` | Código que hay que escribir para crear una cuenta. Sin esta variable no se pueden crear cuentas. |
| `BREVO_API_KEY` | Clave de la API de Brevo para mandar los correos (recuperación de contraseña y avisos). |
| `EMAIL_FROM` | Correo remitente, verificado en Brevo. |
| `EMAIL_FROM_NAME` | Opcional. Nombre del remitente (por defecto "Calculadora de Flete"). |
| `SECRET_KEY` | Opcional. Si no está, se genera una sola vez y se guarda en `/data/.secret_key`. |
| `DATA_DIR` | Opcional. Carpeta base de los archivos; por defecto `/data`. |

**Volume**: en Railway, `Ctrl+K → Create Volume`, servicio `calculadora-flete`, mount path `/data`.

Sin Brevo configurado, los códigos de recuperación aparecen en los Deploy Logs (sirve para probar).

## Resguardo de los datos

- **Volume en `/data` (obligatorio)**: ahí quedan la base (`/data/viajes.db` si no usás PostgreSQL), los archivos de cada usuario y los respaldos. Todo sobrevive a los deploys.
- **Respaldos automáticos** en `/data/respaldos/`: al arrancar, cada 5 minutos si hubo cambios y al apagar (Railway apaga el contenedor viejo en cada deploy). Se guardan los últimos 40, en `.json.gz`.
- **Restauración automática**: si al arrancar la base está completamente vacía y hay respaldos, se restaura el último. Nunca pisa datos existentes. También sirve para pasar de SQLite a PostgreSQL: se configura `DATABASE_URL` con una base nueva y los datos se copian solos.
- **Aviso**: si la base o los archivos no están en un lugar permanente, la app muestra un aviso arriba y lo detalla en *Mi cuenta → Resguardo de datos*.
- **Descargar todos mis datos**: en *Mi cuenta*, un .zip con los viajes (`datos.json`), facturas y remitos en sus carpetas.

## Carpetas en el servidor

```
/data/viajes.db                    base de datos (si no se usa PostgreSQL)
/data/respaldos/                   respaldos automáticos de la base
/data/usuarios/0001-juan/
  LEEME.txt  perfil.json
  viajes/2026-09/viaje-00015_2026-09-28_servagrop-hdo-s-a/
      viaje.json                   todos los datos del viaje (respaldo legible)
      factura_00001-00000106.pdf
      remito-01.jpg  remito-01.mini.jpg
  pendientes/                      subidos antes de guardar el viaje
  eliminados/                      viajes eliminados (se mueven acá, no se borran)
```

## Facturas

El lector está pensado para la factura C que genera ARCA (Comprobantes en línea). Toma la primera página (ORIGINAL) y busca los datos por texto. Si la unidad está en kilos, la pasa a toneladas. Si algún dato no aparece, avisa en pantalla para completarlo a mano. No funciona con fotos o escaneos: hay que subir el PDF descargado de ARCA.

## Seguridad

- **Contraseñas** guardadas con scrypt (nunca en texto). Mínimo 8 caracteres, sin claves comunes ni el nombre de usuario.
- **Sesiones** del lado del servidor: en la base solo se guarda el hash del token. Duran 30 días, se pueden cerrar desde "Mi cuenta" y se cierran todas al recuperar la contraseña.
- **Límite de intentos**: 8 fallos por cuenta o 10 por conexión cada 15 minutos; registro y pedidos de código también limitados.
- **Recuperación**: la respuesta es la misma exista o no la cuenta (no revela usuarios). El código se guarda como hash, vence en 15 minutos y se anula después de 5 intentos.
- **Avisos por correo** al crear la cuenta y cada vez que se cambia la contraseña.
- **Aislamiento**: cada consulta filtra por usuario y cada archivo se busca solo dentro de la carpeta del usuario.
- **Pedidos desde otros sitios** (CSRF) rechazados; cookie `HttpOnly`, `SameSite=Lax` y `Secure` en HTTPS.
- **Archivos subidos**: solo PDF e imágenes, con tamaño máximo; imágenes gigantes que buscan agotar la memoria se rechazan.
- **Excel**: los textos que empiezan con `=` `+` `-` `@` se exportan como texto, nunca como fórmula.
- **Cabeceras**: `nosniff`, `X-Frame-Options: DENY`, `Content-Security-Policy`, `Referrer-Policy`; la API no se guarda en caché.

## Tests

```bash
pip install -r requirements-dev.txt
pytest                      # con SQLite
TEST_DATABASE_URL=postgresql://usuario@host:5432/base_de_prueba pytest   # con PostgreSQL
```

Cubren cuentas (registro, ingreso, recuperación, cambio de contraseña, sesiones), aislamiento entre usuarios, carpetas ordenadas, factura, remitos, viajes, resumen y Excel, datos inválidos o extremos, archivos maliciosos, acceso a archivos fuera de la carpeta, inyección de fórmulas, CSRF, cabeceras de seguridad y el JavaScript de la página.
