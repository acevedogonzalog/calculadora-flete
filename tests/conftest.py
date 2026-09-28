"""Configuración de los tests: base de datos y carpeta de archivos temporales.

Las variables se definen ANTES de importar la app, porque la app las lee al importarse.
"""
import io
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="cf-tests-"))
os.environ["SQLITE_PATH"] = str(_TMP / "test.db")
os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["CODIGO_INVITACION"] = "invitacion-de-prueba"
os.environ.pop("BREVO_API_KEY", None)
os.environ.pop("APP_PASSWORD", None)
# Para probar contra PostgreSQL: TEST_DATABASE_URL=postgresql://... pytest
if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
else:
    os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import cuentas, db  # noqa: E402
from app.server import app  # noqa: E402

FACTURA_REAL = Path(os.environ.get("FACTURA_PDF", "/mnt/user-data/uploads/20240020875_011_00001_00000106.pdf"))
INVITACION = "invitacion-de-prueba"
CLAVE_ANA = "Ruta-Nacional-38"
CLAVE_BETO = "Acoplado-Verde-12"

# Correos "enviados" durante los tests (en lugar de mandarlos de verdad)
CORREOS: list[dict] = []


def _correo_falso(para, asunto, texto):
    CORREOS.append({"para": para, "asunto": asunto, "texto": texto})
    return True


cuentas.enviar_correo = _correo_falso


def reiniciar_limites():
    for lim in (cuentas.LOGIN_IP, cuentas.LOGIN_CUENTA, cuentas.REGISTRO_IP, cuentas.RECUPERO_IP, cuentas.RECUPERO_CUENTA):
        lim._ev.clear()


def registrar(c: TestClient, usuario: str, correo: str, clave: str):
    reiniciar_limites()
    r = c.post("/api/registro", json={"usuario": usuario, "correo": correo, "clave": clave,
                                      "codigo_invitacion": INVITACION})
    assert r.status_code == 201, r.text
    return r


@pytest.fixture(scope="session")
def app_lista():
    with TestClient(app) as c:
        # Un viaje de "la versión anterior" (sin dueño): tiene que pasar al primer usuario
        with db.engine.begin() as con:
            con.execute(db.viajes.insert().values(
                creado=datetime.now(timezone.utc), cliente="VIAJE VIEJO", toneladas=10, precio_tn=1000,
                pct_chofer=18, precio_gasoil=2000, litros=100, otros_gastos=0, total=10000, pago_chofer=1800,
                costo_gasoil=200000, total_gastos=201800, ganancia=-191800))
        registrar(c, "ana", "ana@ejemplo.com", CLAVE_ANA)   # primera cuenta
        yield c


@pytest.fixture(scope="session")
def client(app_lista):
    """Sesión de Ana (la primera usuaria)."""
    c = TestClient(app)
    reiniciar_limites()
    r = c.post("/api/login", json={"usuario": "ana", "clave": CLAVE_ANA})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture(scope="session")
def beto(app_lista):
    """Otro usuario, para comprobar que no ve nada de Ana."""
    c = TestClient(app)
    registrar(c, "beto", "beto@ejemplo.com", CLAVE_BETO)
    return c


@pytest.fixture(scope="session")
def factura_pdf() -> bytes:
    if not FACTURA_REAL.exists():
        pytest.skip("No está la factura de ejemplo")
    return FACTURA_REAL.read_bytes()


def jpeg(w=1200, h=900, exif_orient=None) -> bytes:
    from PIL import Image
    img = Image.new("RGB", (w, h), "white")
    buf = io.BytesIO()
    if exif_orient:
        ex = img.getexif()
        ex[0x0112] = exif_orient
        img.save(buf, "JPEG", exif=ex)
    else:
        img.save(buf, "JPEG")
    return buf.getvalue()


VIAJE_OK = {
    "fecha": "2026-09-28", "factura_nro": "00001-00000106", "cliente": "SERVAGROP HDO S. A.",
    "detalle": "FLETE DE MANI", "toneladas": 28, "precio_tn": 54049, "pct_chofer": 18,
    "precio_gasoil": 2300, "litros": 250, "km": 620, "otros_gastos": 0,
}
