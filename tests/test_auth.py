"""Tests de la clave de acceso (APP_PASSWORD), con un servidor real en otro proceso."""
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
CLAVE = "Camión-2026ñ"   # con acentos a propósito


def _puerto_libre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def srv():
    tmp = Path(tempfile.mkdtemp(prefix="cf-auth-"))
    port = _puerto_libre()
    env = {**os.environ, "APP_PASSWORD": CLAVE, "PORT": str(port),
           "SQLITE_PATH": str(tmp / "a.db"), "DATA_DIR": str(tmp / "data")}
    env.pop("DATABASE_URL", None)
    p = subprocess.Popen([sys.executable, "main.py"], cwd=RAIZ, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            httpx.get(base + "/api/estado", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.2)
    yield base
    p.terminate()
    p.wait(5)


RUTAS_PROTEGIDAS = [
    ("GET", "/api/viajes"), ("POST", "/api/viajes"), ("GET", "/api/viajes/1"), ("DELETE", "/api/viajes/1"),
    ("GET", "/api/viajes/excel"), ("GET", "/api/viajes/1/excel"), ("GET", "/api/viajes/1/factura"),
    ("POST", "/api/viajes/1/factura"), ("POST", "/api/viajes/1/remitos"), ("POST", "/api/factura"),
    ("POST", "/api/remitos"), ("DELETE", "/api/remitos/1"), ("GET", "/api/remitos/archivo/x.jpg"),
    ("GET", "/api/resumen"), ("GET", "/api/resumen/excel?mes=2026-09"),
]


@pytest.mark.parametrize("metodo,ruta", RUTAS_PROTEGIDAS)
def test_sin_sesion_todo_da_401(srv, metodo, ruta):
    assert httpx.request(metodo, srv + ruta).status_code == 401


def test_pagina_y_estado_son_publicos(srv):
    assert httpx.get(srv + "/").status_code == 200
    assert httpx.get(srv + "/api/estado").json() == {"requiere_clave": True, "autenticado": False}


@pytest.mark.parametrize("clave", ["", "x", "camión-2026ñ", "Camión-2026", "ñ" * 50, "' OR 1=1 --"])
def test_clave_incorrecta(srv, clave):
    assert httpx.post(srv + "/api/login", json={"clave": clave}).status_code in (401, 429)


def test_clave_correcta_y_cookie(srv):
    with httpx.Client(base_url=srv) as c:
        r = c.post("/api/login", json={"clave": CLAVE})
        assert r.status_code == 200
        ck = r.headers["set-cookie"].lower()
        assert "httponly" in ck and "samesite=lax" in ck
        assert c.get("/api/viajes").status_code == 200
        assert c.get("/api/estado").json()["autenticado"] is True


def test_cookie_falsificada(srv):
    for valor in ("x", "0" * 64, "1:" + "0" * 64, "9999999999:" + "a" * 64):
        assert httpx.get(srv + "/api/viajes", cookies={"cf_sesion": valor}).status_code == 401


def test_muchos_intentos_fallidos_se_bloquean(srv):
    codigos = [httpx.post(srv + "/api/login", json={"clave": f"mal{i}"}, timeout=10).status_code for i in range(15)]
    assert 429 in codigos, codigos
