"""Configuración de los tests: base de datos y carpeta de archivos temporales.

Las variables se definen ANTES de importar la app, porque la app las lee al importarse.
"""
import io
import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="cf-tests-"))
os.environ["SQLITE_PATH"] = str(_TMP / "test.db")
os.environ["DATA_DIR"] = str(_TMP / "data")
# Para probar contra PostgreSQL: TEST_DATABASE_URL=postgresql://... pytest
if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
else:
    os.environ.pop("DATABASE_URL", None)
os.environ.pop("APP_PASSWORD", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.server import app  # noqa: E402

FACTURA_REAL = Path(os.environ.get("FACTURA_PDF", "/mnt/user-data/uploads/20240020875_011_00001_00000106.pdf"))


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


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
