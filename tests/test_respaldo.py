"""Resguardo de datos: base en el Volume, respaldos automáticos, restauración y descarga."""
import gzip
import io
import json
import zipfile

from sqlalchemy import create_engine, func, select

from app import db, respaldo
from conftest import VIAJE_OK


def test_sqlite_va_dentro_de_la_carpeta_de_datos(monkeypatch):
    monkeypatch.delenv("SQLITE_PATH", raising=False)
    monkeypatch.setenv("DATA_DIR", "/volumen")
    assert db.ruta_sqlite() == "/volumen/viajes.db"
    monkeypatch.setenv("SQLITE_PATH", "/otro/lugar.db")
    assert db.ruta_sqlite() == "/otro/lugar.db"


def test_respaldo_se_crea_solo_si_hay_cambios(client, monkeypatch):
    client.post("/api/viajes", json={**VIAJE_OK, "cliente": "PARA RESPALDAR"})
    primero = respaldo.hacer_respaldo("test")
    assert primero and primero.exists()
    assert respaldo.hacer_respaldo("test") is None          # sin cambios: no duplica
    client.post("/api/viajes", json={**VIAJE_OK, "cliente": "OTRO MAS"})
    segundo = respaldo.hacer_respaldo("test")
    assert segundo and segundo != primero
    with gzip.open(segundo, "rt") as f:
        datos = json.load(f)
    assert any(v["cliente"] == "OTRO MAS" for v in datos["tablas"]["viajes"])
    assert "sesiones" not in datos["tablas"]                 # las sesiones no se respaldan
    assert oct(segundo.stat().st_mode)[-3:] == "600"         # solo la app puede leerlo


def test_se_guardan_solo_los_ultimos(client, monkeypatch):
    monkeypatch.setattr(respaldo, "GUARDAR", 3)
    for i in range(5):
        client.post("/api/viajes", json={**VIAJE_OK, "cliente": f"ROTACION {i}"})
        respaldo.hacer_respaldo("test")
    quedan = respaldo.listar()
    assert len(quedan) == 3
    with gzip.open(quedan[-1], "rt") as f:   # el último de la lista es el más nuevo
        assert any(v["cliente"] == "ROTACION 4" for v in json.load(f)["tablas"]["viajes"])


def test_restaurar_en_una_base_nueva(client, tmp_path):
    client.post("/api/viajes", json={**VIAJE_OK, "cliente": "SE TIENE QUE RECUPERAR"})
    archivo = respaldo.hacer_respaldo("test") or respaldo.listar()[-1]
    nueva = create_engine(f"sqlite:///{tmp_path / 'nueva.db'}")
    db.metadata.create_all(nueva)
    assert respaldo.base_vacia(nueva)
    resumen = respaldo.restaurar(archivo, nueva)
    with db.engine.connect() as a, nueva.connect() as b:
        for t in (db.usuarios, db.viajes, db.remitos):
            assert a.execute(select(func.count()).select_from(t)).scalar_one() == \
                b.execute(select(func.count()).select_from(t)).scalar_one() == resumen[t.name]
        ana_a = a.execute(select(db.usuarios.c.clave_hash).where(db.usuarios.c.usuario == "ana")).scalar_one()
        ana_b = b.execute(select(db.usuarios.c.clave_hash).where(db.usuarios.c.usuario == "ana")).scalar_one()
        assert ana_a == ana_b                                  # la contraseña sigue funcionando
        assert b.execute(select(func.count()).select_from(db.viajes).where(
            db.viajes.c.cliente == "SE TIENE QUE RECUPERAR")).scalar_one() == 1


def test_no_restaura_si_la_base_tiene_datos(client):
    respaldo.hacer_respaldo("test")
    assert respaldo.restaurar_si_vacia() is None


def test_cuenta_muestra_el_resguardo(client):
    r = client.get("/api/cuenta").json()["resguardo"]
    assert r["base_permanente"] is True and r["ultimo_respaldo"]["cantidad"] >= 1
    assert client.get("/api/estado").json()["almacenamiento_ok"] is True


def test_descargar_todos_mis_datos(client, factura_pdf, beto):
    f = client.post("/api/factura", files={"archivo": ("f.pdf", factura_pdf, "application/pdf")}).json()
    client.post("/api/viajes", json={**VIAJE_OK, "cliente": "EN EL ZIP", "factura_archivo": f["factura_archivo"]})
    r = client.get("/api/cuenta/descargar")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    nombres = z.namelist()
    datos = json.loads(z.read("datos.json"))
    assert datos["usuario"] == "ana" and any(v["cliente"] == "EN EL ZIP" for v in datos["viajes"])
    assert "LEEME.txt" in nombres and any(n.endswith(".pdf") and "/viaje-" in n for n in nombres)
    # el zip de Beto no tiene nada de Ana
    zb = zipfile.ZipFile(io.BytesIO(beto.get("/api/cuenta/descargar").content))
    assert "EN EL ZIP" not in zb.read("datos.json").decode()
    assert not any(n.endswith(".pdf") for n in zb.namelist())


def test_crea_la_carpeta_de_la_base_si_no_existe(monkeypatch, tmp_path):
    destino = tmp_path / "volumen-nuevo" / "sub"
    monkeypatch.delenv("SQLITE_PATH", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATA_DIR", str(destino))
    assert db._url().endswith("volumen-nuevo/sub/viajes.db") and destino.is_dir()
