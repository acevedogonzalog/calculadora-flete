"""Tests con un servidor real en otro proceso: cookies, rutas protegidas y registro cerrado."""
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

RAIZ = Path(__file__).resolve().parent.parent


def _puerto_libre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _levantar(**extra):
    tmp = Path(tempfile.mkdtemp(prefix="cf-srv-"))
    port = _puerto_libre()
    env = {**os.environ, "PORT": str(port), "SQLITE_PATH": str(tmp / "a.db"), "DATA_DIR": str(tmp / "data"), **extra}
    for k in ("DATABASE_URL", "BREVO_API_KEY"):
        env.pop(k, None)
    if "CODIGO_INVITACION" not in extra:
        env.pop("CODIGO_INVITACION", None)
    p = subprocess.Popen([sys.executable, "main.py"], cwd=RAIZ, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            httpx.get(base + "/api/estado", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.2)
    return p, base


@pytest.fixture(scope="module")
def srv():
    p, base = _levantar(CODIGO_INVITACION="abc-123")
    yield base
    p.terminate()
    p.wait(5)


RUTAS_PROTEGIDAS = [
    ("GET", "/api/viajes"), ("POST", "/api/viajes"), ("GET", "/api/viajes/1"), ("DELETE", "/api/viajes/1"),
    ("GET", "/api/viajes/excel"), ("GET", "/api/viajes/1/excel"), ("GET", "/api/viajes/1/factura"),
    ("POST", "/api/viajes/1/factura"), ("POST", "/api/viajes/1/remitos"), ("POST", "/api/factura"),
    ("POST", "/api/remitos"), ("DELETE", "/api/remitos/1"), ("GET", "/api/archivos/viajes/x.jpg"),
    ("GET", "/api/resumen"), ("GET", "/api/resumen/excel?mes=2026-09"), ("GET", "/api/cuenta"),
    ("POST", "/api/cuenta/clave"), ("POST", "/api/cuenta/cerrar-sesiones"),
]


@pytest.mark.parametrize("metodo,ruta", RUTAS_PROTEGIDAS)
def test_sin_sesion_todo_da_401(srv, metodo, ruta):
    assert httpx.request(metodo, srv + ruta).status_code in (401, 422)
    if metodo == "GET":
        assert httpx.get(srv + ruta).status_code == 401


def test_cookie_segura(srv):
    with httpx.Client(base_url=srv) as c:
        r = c.post("/api/registro", json={"usuario": "gabi", "correo": "gabi@x.com",
                                          "clave": "Cisterna-Gasoil-3", "codigo_invitacion": "abc-123"})
        assert r.status_code == 201
        ck = r.headers["set-cookie"].lower()
        assert "httponly" in ck and "samesite=lax" in ck and "max-age=" in ck
        assert c.get("/api/viajes").status_code == 200
    # detrás de Railway (HTTPS) la cookie además es Secure
    r = httpx.post(srv + "/api/login", json={"usuario": "gabi", "clave": "Cisterna-Gasoil-3"},
                   headers={"x-forwarded-proto": "https"})
    assert "secure" in r.headers["set-cookie"].lower()


def test_cookie_falsificada(srv):
    for valor in ("x", "0" * 64, "a" * 43):
        assert httpx.get(srv + "/api/viajes", cookies={"cf_sesion": valor}).status_code == 401


def test_registro_cerrado_sin_codigo_configurado():
    p, base = _levantar()
    try:
        assert httpx.get(base + "/api/estado").json()["registro_abierto"] is False
        r = httpx.post(base + "/api/registro", json={"usuario": "x1x", "correo": "x@x.com",
                                                     "clave": "Cualquiera-99", "codigo_invitacion": ""})
        assert r.status_code == 403
    finally:
        p.terminate()
        p.wait(5)


def test_instalacion_nueva_sin_cuentas():
    p, base = _levantar(CODIGO_INVITACION="xyz")
    try:
        e = httpx.get(base + "/api/estado").json()
        assert e["hay_usuarios"] is False and e["registro_abierto"] is True
    finally:
        p.terminate()
        p.wait(5)
