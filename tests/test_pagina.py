"""La página: su JavaScript tiene que compilar sin errores (un error deja la app en blanco)."""
import shutil
import subprocess
from pathlib import Path

import pytest

PAGINA = Path(__file__).resolve().parent.parent / "public" / "index.html"


@pytest.mark.skipif(not shutil.which("node"), reason="Hace falta Node.js para revisar el JavaScript")
def test_javascript_sin_errores_de_sintaxis(tmp_path):
    html = PAGINA.read_text(encoding="utf-8")
    js = html.split("<script>")[1].split("</script>")[0]
    archivo = tmp_path / "pagina.js"
    archivo.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", "--check", str(archivo)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_ids_unicos():
    import re
    html = PAGINA.read_text(encoding="utf-8")
    html = html.split("<script>")[0] + html.split("</script>")[-1]   # solo el HTML, no el código
    ids = re.findall(r'<[^>]*\sid="([^"]+)"', html)
    repetidos = {i for i in ids if ids.count(i) > 1}
    assert not repetidos, repetidos
