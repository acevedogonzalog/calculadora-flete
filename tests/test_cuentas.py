"""Cuentas: registro, ingreso, recuperación por correo, cambio de clave,
aislamiento entre usuarios y orden de las carpetas."""
import json
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app import almacen, db
from app.server import app
from conftest import CLAVE_ANA, CLAVE_BETO, CORREOS, INVITACION, VIAJE_OK, jpeg, registrar, reiniciar_limites


def nuevo():
    return TestClient(app)


def reg(c, usuario, correo, clave="Camion-Azul-2026", inv=INVITACION):
    reiniciar_limites()
    return c.post("/api/registro", json={"usuario": usuario, "correo": correo, "clave": clave, "codigo_invitacion": inv})


# ---------------- Registro ----------------

def test_registro_sin_codigo_o_con_codigo_incorrecto(app_lista):
    assert reg(nuevo(), "carla", "carla@x.com", inv="").status_code == 403
    assert reg(nuevo(), "carla", "carla@x.com", inv="adivinando").status_code == 403


@pytest.mark.parametrize("usuario,correo,clave,esperado", [
    ("ab", "a@x.com", "Camion-Azul-2026", "usuario"),            # usuario corto
    ("con espacio", "a@x.com", "Camion-Azul-2026", "usuario"),
    ("<script>", "a@x.com", "Camion-Azul-2026", "usuario"),
    ("pepe", "no-es-correo", "Camion-Azul-2026", "correo"),
    ("pepe", "pepe@x.com", "corta1", "8 caracteres"),
    ("pepe", "pepe@x.com", "12345678", "fácil"),
    ("pepe", "pepe@x.com", "aaaaaaaaaa", "fácil"),
    ("pepe", "pepe@x.com", "pepe-2026-camion", "usuario"),         # contiene el usuario
])
def test_registro_valida_datos(app_lista, usuario, correo, clave, esperado):
    r = reg(nuevo(), usuario, correo, clave)
    assert r.status_code in (400, 422) and esperado.lower() in r.json()["detail"].lower(), r.json()


def test_registro_no_duplica_usuario_ni_correo(app_lista):
    assert reg(nuevo(), "ANA", "otro@x.com").status_code == 409          # mayúsculas: mismo usuario
    assert reg(nuevo(), "ana2", "ANA@Ejemplo.com").status_code == 409     # mismo correo


def test_clave_guardada_cifrada_y_sesion_como_hash(client):
    with db.engine.connect() as con:
        u = con.execute(select(db.usuarios).where(db.usuarios.c.usuario == "ana")).first()._mapping
        sesiones = [r[0] for r in con.execute(select(db.sesiones.c.id))]
    assert u["clave_hash"].startswith("scrypt$") and CLAVE_ANA not in u["clave_hash"]
    token = client.cookies.get("cf_sesion")
    assert token and token not in sesiones  # en la base solo está el hash del token


# ---------------- Ingreso ----------------

def test_ingreso_con_usuario_o_con_correo(app_lista):
    for dato in ("ana", "ANA@ejemplo.com", "  Ana  "):
        reiniciar_limites()
        assert nuevo().post("/api/login", json={"usuario": dato, "clave": CLAVE_ANA}).status_code == 200, dato


def test_mismo_mensaje_si_el_usuario_no_existe(app_lista):
    reiniciar_limites()
    a = nuevo().post("/api/login", json={"usuario": "ana", "clave": "mal"})
    b = nuevo().post("/api/login", json={"usuario": "nadie-registrado", "clave": "mal"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_bloqueo_por_intentos_fallidos(app_lista):
    reiniciar_limites()
    c = nuevo()
    codigos = [c.post("/api/login", json={"usuario": "ana", "clave": f"mal-{i}"}).status_code for i in range(9)]
    assert codigos[:8] == [401] * 8 and codigos[8] == 429
    # bloqueada aunque ahora ponga la clave correcta
    assert c.post("/api/login", json={"usuario": "ana", "clave": CLAVE_ANA}).status_code == 429
    reiniciar_limites()


def test_cerrar_sesion_invalida_la_cookie(app_lista):
    reiniciar_limites()
    c = nuevo()
    c.post("/api/login", json={"usuario": "ana", "clave": CLAVE_ANA})
    vieja = c.cookies.get("cf_sesion")
    assert c.get("/api/viajes").status_code == 200
    c.post("/api/logout")
    assert nuevo().get("/api/viajes", cookies={"cf_sesion": vieja}).status_code == 401


def test_sesion_vencida(app_lista):
    reiniciar_limites()
    c = nuevo()
    c.post("/api/login", json={"usuario": "ana", "clave": CLAVE_ANA})
    from app import cuentas
    h = cuentas.huella(c.cookies.get("cf_sesion"))
    with db.engine.begin() as con:
        con.execute(update(db.sesiones).where(db.sesiones.c.id == h).values(vence=db.ahora() - timedelta(seconds=1)))
    assert c.get("/api/viajes").status_code == 401


@pytest.mark.parametrize("ruta", ["/api/viajes", "/api/cuenta", "/api/resumen", "/api/viajes/1",
                                  "/api/viajes/excel", "/api/archivos/viajes/x.jpg"])
def test_sin_sesion_no_hay_datos(app_lista, ruta):
    assert nuevo().get(ruta).status_code == 401


# ---------------- Aislamiento entre usuarios ----------------

def test_primer_usuario_hereda_los_viajes_anteriores(client, beto):
    assert any(v["cliente"] == "VIAJE VIEJO" for v in client.get("/api/viajes").json())
    assert not any(v["cliente"] == "VIAJE VIEJO" for v in beto.get("/api/viajes").json())


def test_un_usuario_no_ve_ni_toca_datos_de_otro(client, beto, factura_pdf):
    f = client.post("/api/factura", files={"archivo": ("f.pdf", factura_pdf, "application/pdf")}).json()
    foto = client.post("/api/remitos", files={"archivo": ("a.jpg", jpeg(), "image/jpeg")}).json()
    v = client.post("/api/viajes", json={**VIAJE_OK, "cliente": "SECRETO DE ANA", "factura_archivo": f["factura_archivo"],
                                         "remitos": [foto]}).json()
    vid, rid, archivo = v["id"], v["remitos"][0]["id"], v["remitos"][0]["archivo"]

    assert all(x["cliente"] != "SECRETO DE ANA" for x in beto.get("/api/viajes").json())
    for metodo, ruta in [("GET", f"/api/viajes/{vid}"), ("GET", f"/api/viajes/{vid}/factura"),
                         ("GET", f"/api/viajes/{vid}/excel"), ("DELETE", f"/api/viajes/{vid}"),
                         ("DELETE", f"/api/remitos/{rid}"), ("GET", f"/api/archivos/{archivo}")]:
        assert beto.request(metodo, ruta).status_code == 404, (metodo, ruta)
    assert beto.post(f"/api/viajes/{vid}/remitos", files={"archivo": ("b.jpg", jpeg(), "image/jpeg")}).status_code == 404
    assert beto.post(f"/api/viajes/{vid}/factura", files={"archivo": ("f.pdf", factura_pdf, "application/pdf")}).status_code == 404
    assert "SECRETO" not in beto.get("/api/resumen?mes=2026-09").text
    assert "SECRETO" not in beto.get("/api/viajes/excel").content.decode("latin-1")
    # Beto intenta "robar" los archivos pendientes de Ana usando su ruta: se ignoran
    f2 = client.post("/api/factura", files={"archivo": ("f.pdf", factura_pdf + b" ", "application/pdf")}).json()
    robo = beto.post("/api/viajes", json={**VIAJE_OK, "factura_archivo": f2["factura_archivo"]}).json()
    assert robo["factura_archivo"] == ""
    # todo lo de Ana sigue intacto
    v2 = client.get(f"/api/viajes/{vid}").json()
    assert v2["remitos_cant"] == 1 and client.get(f"/api/viajes/{vid}/factura").status_code == 200


# ---------------- Carpetas ordenadas ----------------

def test_carpetas_ordenadas_por_usuario_y_viaje(client, factura_pdf):
    f = client.post("/api/factura", files={"archivo": ("x.pdf", factura_pdf, "application/pdf")}).json()
    foto = client.post("/api/remitos", files={"archivo": ("a.jpg", jpeg(900, 600), "image/jpeg")}).json()
    v = client.post("/api/viajes", json={**VIAJE_OK, "factura_archivo": f["factura_archivo"], "remitos": [foto]}).json()
    u = db.buscar_usuario("ana")
    raiz = almacen.raiz(u)
    assert raiz.name == "0001-ana" and (raiz / "LEEME.txt").exists()
    perfil = json.loads((raiz / "perfil.json").read_text())
    assert perfil["usuario"] == "ana" and "clave" not in json.dumps(perfil)
    carpeta = raiz / v["carpeta"]
    assert re.fullmatch(r"viajes/2026-09/viaje-\d{5}_2026-09-28_servagrop-hdo-s-a", v["carpeta"])
    nombres = sorted(p.name for p in carpeta.iterdir())
    assert nombres == ["factura_00001-00000106.pdf", "remito-01.jpg", "remito-01.mini.jpg", "viaje.json"]
    ficha = json.loads((carpeta / "viaje.json").read_text())
    assert ficha["id"] == v["id"] and ficha["total"] == 1513372 and len(ficha["remitos"]) == 1
    # agregar otra foto después actualiza la ficha
    client.post(f"/api/viajes/{v['id']}/remitos", files={"archivo": ("b.jpg", jpeg(), "image/jpeg")})
    assert (carpeta / "remito-02.jpg").exists()
    assert len(json.loads((carpeta / "viaje.json").read_text())["remitos"]) == 2
    # eliminar el viaje: la carpeta pasa a eliminados/, nada se pierde
    client.delete(f"/api/viajes/{v['id']}")
    assert not carpeta.exists()
    eliminado = [p for p in (raiz / "eliminados").iterdir() if p.name.endswith(carpeta.name)]
    assert len(eliminado) == 1 and (eliminado[0] / "factura_00001-00000106.pdf").exists()
    assert "eliminado" in json.loads((eliminado[0] / "viaje.json").read_text())


# ---------------- Recuperación por correo ----------------

def _codigo_de(correo):
    msj = [m for m in CORREOS if m["para"] == correo and "código" in m["asunto"]][-1]
    return re.search(r"\b(\d{6})\b", msj["texto"]).group(1)


def test_recuperar_misma_respuesta_exista_o_no(app_lista):
    reiniciar_limites()
    a = nuevo().post("/api/recuperar", json={"usuario": "beto"})
    b = nuevo().post("/api/recuperar", json={"usuario": "no-existe@x.com"})
    assert a.status_code == b.status_code == 200 and a.json() == b.json()


def test_recuperar_contrasenia_completo(beto):
    reiniciar_limites()
    c = nuevo()
    antes = len(CORREOS)
    assert c.post("/api/recuperar", json={"usuario": "beto@ejemplo.com"}).status_code == 200
    assert len(CORREOS) == antes + 1 and CORREOS[-1]["para"] == "beto@ejemplo.com"
    codigo = _codigo_de("beto@ejemplo.com")
    # el código no queda guardado tal cual en la base
    with db.engine.connect() as con:
        assert codigo not in str(con.execute(select(db.codigos)).all())
    # clave nueva débil o con el nombre de usuario: se rechaza sin gastar el código
    assert c.post("/api/recuperar/confirmar", json={"usuario": "beto", "codigo": codigo, "clave_nueva": "123"}).status_code == 400
    assert c.post("/api/recuperar/confirmar", json={"usuario": "beto", "codigo": codigo,
                                                    "clave_nueva": "beto-camion-2027"}).status_code == 400
    r = c.post("/api/recuperar/confirmar", json={"usuario": "beto", "codigo": codigo, "clave_nueva": "Chasis-Nuevo-2027"})
    assert r.status_code == 200
    # la sesión vieja de Beto quedó cerrada, la clave vieja ya no sirve y la nueva sí
    assert beto.get("/api/viajes").status_code == 401
    assert nuevo().post("/api/login", json={"usuario": "beto", "clave": CLAVE_BETO}).status_code == 401
    reiniciar_limites()
    assert beto.post("/api/login", json={"usuario": "beto", "clave": "Chasis-Nuevo-2027"}).status_code == 200
    # el código no se puede usar dos veces
    assert c.post("/api/recuperar/confirmar", json={"usuario": "beto", "codigo": codigo,
                                                    "clave_nueva": "Otra-Clave-Mas-1"}).status_code == 400
    assert any("Se cambió tu contraseña" in m["asunto"] for m in CORREOS if m["para"] == "beto@ejemplo.com")


def test_codigo_se_anula_tras_5_intentos(app_lista):
    c = nuevo()
    registrar(c, "dora", "dora@ejemplo.com", "Semirremolque-44")
    reiniciar_limites()
    c.post("/api/recuperar", json={"usuario": "dora"})
    bueno = _codigo_de("dora@ejemplo.com")
    malo = "000000" if bueno != "000000" else "111111"
    for _ in range(5):
        assert c.post("/api/recuperar/confirmar", json={"usuario": "dora", "codigo": malo,
                                                        "clave_nueva": "Clave-Nueva-Dora-1"}).status_code == 400
    reiniciar_limites()
    assert c.post("/api/recuperar/confirmar", json={"usuario": "dora", "codigo": bueno,
                                                    "clave_nueva": "Clave-Nueva-Dora-1"}).status_code == 400


def test_codigo_vencido(app_lista):
    c = nuevo()
    registrar(c, "eli", "eli@ejemplo.com", "Balanza-Publica-7")
    reiniciar_limites()
    c.post("/api/recuperar", json={"usuario": "eli"})
    codigo = _codigo_de("eli@ejemplo.com")
    with db.engine.begin() as con:
        con.execute(update(db.codigos).values(vence=db.ahora() - timedelta(minutes=1)))
    assert c.post("/api/recuperar/confirmar", json={"usuario": "eli", "codigo": codigo,
                                                    "clave_nueva": "Clave-Nueva-Eli-1"}).status_code == 400


# ---------------- Cambiar contraseña ----------------

def test_cambiar_contrasenia_cierra_otras_sesiones(app_lista):
    reiniciar_limites()
    a, b = nuevo(), nuevo()
    registrar(a, "fede", "fede@ejemplo.com", "Tolva-Granera-55")
    reiniciar_limites()
    b.post("/api/login", json={"usuario": "fede", "clave": "Tolva-Granera-55"})
    assert a.post("/api/cuenta/clave", json={"actual": "mal", "nueva": "Otra-Tolva-66"}).status_code == 400
    r = a.post("/api/cuenta/clave", json={"actual": "Tolva-Granera-55", "nueva": "Otra-Tolva-66"})
    assert r.status_code == 200 and r.json()["sesiones_cerradas"] == 1
    assert a.get("/api/viajes").status_code == 200    # esta sesión sigue
    assert b.get("/api/viajes").status_code == 401    # la otra se cerró
    cuenta = a.get("/api/cuenta").json()
    assert cuenta["usuario"] == "fede" and cuenta["carpeta"].endswith("-fede") and cuenta["sesiones"] == 1


# ---------------- Mantener la sesión iniciada ----------------

def _vence(c):
    from app import cuentas
    h = cuentas.huella(c.cookies.get("cf_sesion"))
    with db.engine.connect() as con:
        v = con.execute(select(db.sesiones.c.vence).where(db.sesiones.c.id == h)).scalar_one()
    return v if v.tzinfo else v.replace(tzinfo=db.ahora().tzinfo)


def test_mantener_sesion_iniciada(app_lista):
    reiniciar_limites()
    c = nuevo()
    r = c.post("/api/login", json={"usuario": "ana", "clave": CLAVE_ANA, "recordar": True})
    assert "max-age=34560000" in r.headers["set-cookie"].lower()          # 400 días en el navegador
    assert timedelta(days=89) < _vence(c) - db.ahora() <= timedelta(days=90)


def test_sin_mantener_sesion(app_lista):
    reiniciar_limites()
    c = nuevo()
    r = c.post("/api/login", json={"usuario": "ana", "clave": CLAVE_ANA, "recordar": False})
    ck = r.headers["set-cookie"].lower()
    assert "max-age" not in ck and "expires" not in ck                     # se borra al cerrar el navegador
    assert _vence(c) - db.ahora() <= timedelta(hours=12)


def test_la_sesion_se_renueva_mientras_se_usa(app_lista):
    reiniciar_limites()
    c = nuevo()
    c.post("/api/login", json={"usuario": "ana", "clave": CLAVE_ANA, "recordar": True})
    from app import cuentas
    h = cuentas.huella(c.cookies.get("cf_sesion"))
    with db.engine.begin() as con:   # como si hubiera entrado hace 80 días por última vez
        con.execute(update(db.sesiones).where(db.sesiones.c.id == h).values(
            vence=db.ahora() + timedelta(days=10), ultima_vez=db.ahora() - timedelta(days=80)))
    assert c.get("/api/viajes").status_code == 200
    assert _vence(c) - db.ahora() > timedelta(days=89)                     # renovada a 90 días


def test_estado_indica_si_hay_cuentas(app_lista):
    assert nuevo().get("/api/estado").json()["hay_usuarios"] is True
