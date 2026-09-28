"""Cuentas de usuario: claves, validaciones, límites de intentos y correo.

- Las claves se guardan con scrypt (estándar, lento a propósito para frenar ataques).
- Los códigos de recuperación y los tokens de sesión se guardan como hash: si alguien
  leyera la base de datos, no podría usarlos.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

log = logging.getLogger("uvicorn.error")

# ---------- Clave secreta del servidor ----------

def _clave_servidor() -> bytes:
    """SECRET_KEY si está definida; si no, se genera una y se guarda en /data (sobrevive a los deploys)."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"].encode()
    from .almacen import BASE
    archivo = BASE / ".secret_key"
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        if not archivo.exists():
            archivo.write_text(secrets.token_hex(32))
            os.chmod(archivo, 0o600)
        return archivo.read_text().strip().encode()
    except OSError:
        log.warning("No se pudo guardar la clave secreta en %s: se usa una temporal.", archivo)
        return secrets.token_hex(32).encode()


_SECRETO: bytes | None = None


def secreto() -> bytes:
    global _SECRETO
    if _SECRETO is None:
        _SECRETO = _clave_servidor()
    return _SECRETO


def huella(valor: str) -> str:
    """Hash con la clave del servidor (para tokens de sesión y códigos)."""
    return hmac.new(secreto(), valor.encode(), hashlib.sha256).hexdigest()


# ---------- Contraseñas ----------

N, R, P = 2**14, 8, 1


def hash_clave(clave: str) -> str:
    sal = secrets.token_bytes(16)
    h = hashlib.scrypt(clave.encode("utf-8"), salt=sal, n=N, r=R, p=P, dklen=32)
    return f"scrypt${N}${R}${P}${base64.b64encode(sal).decode()}${base64.b64encode(h).decode()}"


def verificar_clave(clave: str, guardado: str | None) -> bool:
    try:
        _, n, r, p, sal, h = (guardado or "").split("$")
        calc = hashlib.scrypt(clave.encode("utf-8"), salt=base64.b64decode(sal),
                              n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


# Hash de relleno: se verifica igual cuando el usuario no existe, así la demora es la misma
HASH_FALSO = hash_clave(secrets.token_hex(8))

COMUNES = {"12345678", "123456789", "1234567890", "password", "password1", "contraseña", "contrasena",
           "qwerty123", "11111111", "00000000", "abcd1234", "12341234", "iloveyou", "camion123", "flete123"}


def problema_clave(clave: str, usuario: str = "", correo: str = "") -> str | None:
    if len(clave) < 8:
        return "La contraseña tiene que tener al menos 8 caracteres."
    if len(clave) > 128:
        return "La contraseña es demasiado larga (máximo 128 caracteres)."
    if clave.lower() in COMUNES or len(set(clave)) < 4:
        return "Esa contraseña es muy fácil de adivinar. Elegí otra."
    if usuario and usuario.lower() in clave.lower():
        return "La contraseña no puede contener tu nombre de usuario."
    if correo and correo.split("@")[0].lower() in clave.lower() and len(correo.split("@")[0]) >= 4:
        return "La contraseña no puede contener tu correo."
    return None


USUARIO_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,29}$")
CORREO_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def normalizar_usuario(u: str) -> str:
    return (u or "").strip().lower()


def problema_usuario(u: str) -> str | None:
    if not USUARIO_RE.match(u):
        return "El usuario tiene que tener entre 3 y 30 caracteres: letras, números, punto, guion o guion bajo."
    return None


def problema_correo(c: str) -> str | None:
    if len(c) > 200 or not CORREO_RE.match(c):
        return "Ese correo no parece válido."
    return None


def nuevo_token() -> str:
    return secrets.token_urlsafe(32)


def nuevo_codigo() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


# ---------- Límite de intentos ----------

class Limite:
    """Cuenta eventos por clave (IP, usuario, correo) dentro de una ventana de tiempo."""

    def __init__(self, maximo: int, ventana_seg: int):
        self.maximo, self.ventana = maximo, ventana_seg
        self._ev: dict[str, deque] = {}

    def _limpiar(self, clave: str) -> deque:
        q = self._ev.setdefault(clave, deque())
        corte = time.time() - self.ventana
        while q and q[0] < corte:
            q.popleft()
        return q

    def excedido(self, clave: str) -> bool:
        return len(self._limpiar(clave)) >= self.maximo

    def sumar(self, clave: str) -> None:
        self._limpiar(clave).append(time.time())
        if len(self._ev) > 20_000:
            self._ev.clear()

    def reiniciar(self, clave: str) -> None:
        self._ev.pop(clave, None)


LOGIN_IP = Limite(10, 15 * 60)        # 10 fallos por conexión cada 15 min
LOGIN_CUENTA = Limite(8, 15 * 60)     # 8 fallos por cuenta cada 15 min
REGISTRO_IP = Limite(5, 60 * 60)      # 5 cuentas por conexión por hora
RECUPERO_IP = Limite(10, 60 * 60)
RECUPERO_CUENTA = Limite(3, 60 * 60)  # 3 correos de recuperación por cuenta por hora
CODIGO_INTENTOS = 5                   # intentos por código antes de invalidarlo
CODIGO_MINUTOS = 15


# ---------- Correo (Brevo) ----------

def correo_configurado() -> bool:
    return bool(os.environ.get("BREVO_API_KEY") and os.environ.get("EMAIL_FROM"))


def _enviar_brevo(para: str, asunto: str, texto: str, html: str) -> bool:
    cuerpo = {
        "sender": {"email": os.environ["EMAIL_FROM"], "name": os.environ.get("EMAIL_FROM_NAME", "Calculadora de Flete")},
        "to": [{"email": para}],
        "subject": asunto,
        "textContent": texto,
        "htmlContent": html,
    }
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email", data=json.dumps(cuerpo).encode(), method="POST",
        headers={"api-key": os.environ["BREVO_API_KEY"], "content-type": "application/json", "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        log.error("Brevo rechazó el correo (%s): %s", e.code, e.read()[:300])
    except Exception as e:  # red, DNS, etc.
        log.error("No se pudo enviar el correo: %s", e)
    return False


# Reemplazable en los tests
def enviar_correo(para: str, asunto: str, texto: str) -> bool:
    html = "<div style='font-family:Arial,sans-serif;font-size:15px;line-height:1.5;color:#1B2220'>" + \
        "".join(f"<p>{linea}</p>" for linea in texto.split("\n\n")) + "</div>"
    if correo_configurado():
        return _enviar_brevo(para, asunto, texto, html)
    log.warning("Correo NO enviado (falta BREVO_API_KEY o EMAIL_FROM). Para %s — %s:\n%s", para, asunto, texto)
    return False


def enmascarar(correo: str) -> str:
    usuario, _, dominio = correo.partition("@")
    visible = usuario[:2] if len(usuario) > 3 else usuario[:1]
    return f"{visible}{'•' * max(2, len(usuario) - len(visible))}@{dominio}"
