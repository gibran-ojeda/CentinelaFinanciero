"""Tests de la fusión del `.env` de producción (`scripts/lib/entorno.sh`).

Son los primeros tests de shell del repo, y existen por una razón concreta:
hasta ahora, la primera vez que alguien ejecutaba la lógica del despliegue era
**en producción**. El bug que motivó todo esto —una llave puesta a mano en el
VPS que el despliegue borraba sin decir nada— habría muerto en el primer test.

Las variables se pasan como **variables de shell**, sin `export`, porque así
es exactamente como las emite `deploy.yml`: el script se concatena al mismo
`bash -s` y las lee de ahí. Exportarlas probaría un camino que no existe.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.requires_bash

REPO_ROOT = Path(__file__).resolve().parents[2]
LIBRERIA = REPO_ROOT / "scripts" / "lib" / "entorno.sh"

#: Lo mínimo para que la fusión no aborte por una guarda. Cada test las pisa.
GITHUB_BASE = {
    "POSTGRES_PASSWORD": "clave",
    "API_READ_KEY": "lectura",
    "API_ADMIN_KEY": "admin",
    "SITE_URL": "https://centinelafinanciero.lat",
    "BANXICO_TOKEN": "",
    "DEEPSEEK_API_KEY": "",
    "SCHEDULER_RESEARCH_ENABLED": "",
}


def _correr(
    bash: str,
    destino: Path,
    variables: dict[str, str],
    *,
    previo: str | None = None,
    escribir: bool = True,
    antes: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Fusiona `destino` con `variables` y devuelve el proceso terminado."""
    if previo is not None:
        destino.write_text(previo, encoding="utf-8", newline="\n")
    guion = "\n".join(
        [
            "set -euo pipefail",
            '. "$1"',
            *[f"{clave}={shlex.quote(valor)}" for clave, valor in variables.items()],
            *antes,
            'entorno_fusionar "$2"',
            *(['entorno_escribir "$2"'] if escribir else []),
            "entorno_reportar",
        ]
    )
    return subprocess.run(
        [bash, "-c", guion, "entorno-test", LIBRERIA.as_posix(), destino.as_posix()],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _valores(archivo: Path) -> dict[str, str]:
    """Las claves del archivo, ignorando comentarios y líneas sueltas."""
    pares = {}
    for linea in archivo.read_text(encoding="utf-8").splitlines():
        if linea.startswith("#") or "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        pares[clave] = valor
    return pares


# ─── El incidente ─────────────────────────────────────────────


def test_an_empty_github_value_keeps_the_key_already_in_the_env(bash: str, tmp_path: Path) -> None:
    """El caso que costó días de researcher caído.

    El workflow emite `DEEPSEEK_API_KEY=''` aunque el secreto no exista, así
    que la variable llega **definida y vacía**. Lo que decide es que el valor
    no esté vacío, nunca que la variable esté definida.
    """
    destino = tmp_path / ".env"
    resultado = _correr(
        bash,
        destino,
        GITHUB_BASE,
        previo="DEEPSEEK_API_KEY=sk-puesta-a-mano-en-el-vps\n",
    )

    assert resultado.returncode == 0, resultado.stderr
    assert _valores(destino)["DEEPSEEK_API_KEY"] == "sk-puesta-a-mano-en-el-vps"
    assert "DEEPSEEK_API_KEY" in resultado.stdout
    assert "conservada del .env" in resultado.stdout


def test_a_non_empty_github_value_wins_over_the_env(bash: str, tmp_path: Path) -> None:
    """Una rotación del secreto tiene que llegar a la máquina."""
    destino = tmp_path / ".env"
    resultado = _correr(
        bash,
        destino,
        {**GITHUB_BASE, "DEEPSEEK_API_KEY": "sk-nueva"},
        previo="DEEPSEEK_API_KEY=sk-vieja\n",
    )

    assert resultado.returncode == 0, resultado.stderr
    assert _valores(destino)["DEEPSEEK_API_KEY"] == "sk-nueva"


def test_an_optional_key_absent_everywhere_is_written_empty(bash: str, tmp_path: Path) -> None:
    """El archivo documenta que la variable existe, aunque nadie la haya puesto."""
    destino = tmp_path / ".env"
    resultado = _correr(bash, destino, GITHUB_BASE, previo="")

    assert resultado.returncode == 0, resultado.stderr
    assert _valores(destino)["DEEPSEEK_API_KEY"] == ""
    assert "ausente" in resultado.stdout


# ─── Las clases ───────────────────────────────────────────────


def test_the_switch_falls_back_to_its_default_instead_of_the_env_value(
    bash: str, tmp_path: Path
) -> None:
    """Se conservan credenciales, no interruptores.

    Borrar la variable de repo significa «vuelve al default»; conservarla
    dejaría un `false` viejo apagando el researcher para siempre.
    """
    destino = tmp_path / ".env"
    resultado = _correr(bash, destino, GITHUB_BASE, previo="SCHEDULER_RESEARCH_ENABLED=false\n")

    assert resultado.returncode == 0, resultado.stderr
    assert _valores(destino)["SCHEDULER_RESEARCH_ENABLED"] == "true"
    assert "por defecto" in resultado.stdout


def test_fixed_keys_are_rewritten_when_the_env_diverges(bash: str, tmp_path: Path) -> None:
    """Un POSTGRES_HOST editado a mano es una avería, no una preferencia.

    Se corrige, y el reporte lo dice: pisar una edición manual en silencio
    sería la misma clase de fallo que este trabajo viene a cerrar.
    """
    destino = tmp_path / ".env"
    resultado = _correr(bash, destino, GITHUB_BASE, previo="POSTGRES_HOST=127.0.0.1\n")

    assert resultado.returncode == 0, resultado.stderr
    assert _valores(destino)["POSTGRES_HOST"] == "db"
    assert "fija (cambió)" in resultado.stdout


def test_a_deleted_mandatory_secret_does_not_resurrect_from_the_env(
    bash: str, tmp_path: Path
) -> None:
    """Una rotación que «no tuvo efecto» es peor que un despliegue que falla."""
    destino = tmp_path / ".env"
    resultado = _correr(
        bash,
        destino,
        {**GITHUB_BASE, "POSTGRES_PASSWORD": ""},
        previo="POSTGRES_PASSWORD=la-de-la-maquina\n",
    )

    assert resultado.returncode == 0, resultado.stderr
    assert _valores(destino)["POSTGRES_PASSWORD"] == ""


# ─── Lo que no se toca ────────────────────────────────────────


def test_unknown_keys_comments_and_blank_lines_survive_in_place(bash: str, tmp_path: Path) -> None:
    """En su sitio, no reagrupadas: un comentario movido deja de explicar."""
    destino = tmp_path / ".env"
    previo = (
        "# palanca que puse una noche\n"
        "LLM_COST_DAILY_LIMIT_USD=2.0\n"
        "\n"
        "DEEPSEEK_API_KEY=sk-mia\n"
        "# cola del archivo\n"
    )
    resultado = _correr(bash, destino, GITHUB_BASE, previo=previo)

    assert resultado.returncode == 0, resultado.stderr
    lineas = destino.read_text(encoding="utf-8").splitlines()
    assert lineas[0] == "# palanca que puse una noche"
    assert lineas[1] == "LLM_COST_DAILY_LIMIT_USD=2.0"
    assert lineas[2] == ""
    assert lineas[3] == "DEEPSEEK_API_KEY=sk-mia"
    assert lineas[4] == "# cola del archivo"
    assert "1 conservadas: LLM_COST_DAILY_LIMIT_USD" in resultado.stdout


def test_values_with_equals_spaces_quotes_and_hashes_round_trip(bash: str, tmp_path: Path) -> None:
    """Se escriben en crudo, como el heredoc: entrecomillar cambiaría lo que
    el compose interpreta para valores que hoy ya funcionan."""
    raro = "a=b#c d\"e'f\\g"
    destino = tmp_path / ".env"
    resultado = _correr(bash, destino, {**GITHUB_BASE, "POSTGRES_PASSWORD": raro})

    assert resultado.returncode == 0, resultado.stderr
    assert _valores(destino)["POSTGRES_PASSWORD"] == raro


def test_a_value_with_a_newline_aborts_and_leaves_the_file_untouched(
    bash: str, tmp_path: Path
) -> None:
    """Partiría el .env en dos y la mitad suelta sobreviviría como «ajena»."""
    destino = tmp_path / ".env"
    previo = "SITE_URL=https://previo\n"
    resultado = _correr(
        bash, destino, {**GITHUB_BASE, "POSTGRES_PASSWORD": "linea1\nlinea2"}, previo=previo
    )

    assert resultado.returncode != 0
    assert "salto de línea" in resultado.stderr
    assert destino.read_text(encoding="utf-8") == previo


# ─── Forma del archivo ────────────────────────────────────────


def test_the_first_run_creates_the_whole_template_in_order(bash: str, tmp_path: Path) -> None:
    """En un VPS limpio no hay .env que fusionar: se crea entero."""
    destino = tmp_path / ".env"
    resultado = _correr(bash, destino, GITHUB_BASE)

    assert resultado.returncode == 0, resultado.stderr
    claves = list(_valores(destino))
    assert claves[0] == "ENVIRONMENT"
    assert claves[-1] == "SCHEDULER_RESEARCH_ENABLED"
    assert len(claves) == 16


def test_a_second_run_produces_the_same_bytes(bash: str, tmp_path: Path) -> None:
    """Idempotencia: un despliegue sin cambios no genera ruido en el archivo."""
    destino = tmp_path / ".env"
    _correr(bash, destino, GITHUB_BASE, previo="DEEPSEEK_API_KEY=sk-mia\n")
    primera = destino.read_bytes()
    _correr(bash, destino, GITHUB_BASE)

    assert destino.read_bytes() == primera


def test_duplicate_managed_keys_collapse_into_the_first_one(bash: str, tmp_path: Path) -> None:
    """El compose se queda con la última: dejar dos es que el archivo diga una
    cosa y el contenedor lea otra."""
    destino = tmp_path / ".env"
    resultado = _correr(
        bash,
        destino,
        GITHUB_BASE,
        previo="DEEPSEEK_API_KEY=sk-primera\nDEEPSEEK_API_KEY=sk-segunda\n",
    )

    assert resultado.returncode == 0, resultado.stderr
    texto = destino.read_text(encoding="utf-8")
    assert texto.count("DEEPSEEK_API_KEY=") == 1
    assert _valores(destino)["DEEPSEEK_API_KEY"] == "sk-primera"
    assert "repetida" in resultado.stdout


# ─── Escritura ────────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="los modos POSIX en Windows son ficción")
def test_the_env_ends_up_readable_only_by_its_owner(bash: str, tmp_path: Path) -> None:
    """El VPS se comparte con otro stack: el .env no puede quedar en 644."""
    destino = tmp_path / ".env"
    resultado = _correr(bash, destino, GITHUB_BASE, antes=("umask 022",))

    assert resultado.returncode == 0, resultado.stderr
    assert stat.S_IMODE(destino.stat().st_mode) == 0o600


def test_a_failed_write_leaves_the_previous_file_untouched(bash: str, tmp_path: Path) -> None:
    """Un destino imposible no puede llevarse por delante lo que había."""
    destino = tmp_path / "sin-directorio" / ".env"
    resultado = _correr(bash, destino, GITHUB_BASE)

    assert resultado.returncode != 0
    assert not destino.exists()
    assert list(tmp_path.glob("**/.env.nuevo.*")) == []


def test_the_rewrite_hatch_sets_the_previous_file_aside(bash: str, tmp_path: Path) -> None:
    """La escotilla aparta, no destruye."""
    destino = tmp_path / ".env"
    destino.write_text("DEEPSEEK_API_KEY=sk-mia\nAJENA=1\n", encoding="utf-8", newline="\n")
    resultado = _correr(bash, destino, GITHUB_BASE, antes=('entorno_apartar "$2"',))

    assert resultado.returncode == 0, resultado.stderr
    apartado = tmp_path / ".env.reemplazado"
    assert apartado.exists()
    assert "AJENA=1" in apartado.read_text(encoding="utf-8")
    # El archivo nuevo se creó de cero: ni la llave ni la ajena sobreviven.
    assert _valores(destino)["DEEPSEEK_API_KEY"] == ""
    assert "AJENA" not in _valores(destino)


# ─── La doctrina de no imprimir valores ───────────────────────


def test_the_report_never_prints_a_value(bash: str, tmp_path: Path) -> None:
    """Convierte en aserción la doctrina de gates.sh: los logs de Actions de un
    repositorio público son públicos, y un prefijo de una API key sigue siendo
    material de una API key."""
    señuelos = {
        "POSTGRES_PASSWORD": "VALOR-QUE-NO-DEBE-SALIR-1",
        "API_READ_KEY": "VALOR-QUE-NO-DEBE-SALIR-2",
        "API_ADMIN_KEY": "VALOR-QUE-NO-DEBE-SALIR-3",
        "DEEPSEEK_API_KEY": "VALOR-QUE-NO-DEBE-SALIR-4",
    }
    destino = tmp_path / ".env"
    resultado = _correr(
        bash,
        destino,
        {**GITHUB_BASE, **señuelos},
        previo="BANXICO_TOKEN=VALOR-QUE-NO-DEBE-SALIR-5\n",
    )

    assert resultado.returncode == 0, resultado.stderr
    for valor in [*señuelos.values(), "VALOR-QUE-NO-DEBE-SALIR-5"]:
        assert valor not in resultado.stdout
        assert valor not in resultado.stderr


# ─── Sintaxis de todos los scripts ────────────────────────────


@pytest.mark.parametrize(
    "guion",
    ["lib/entorno.sh", "desplegar.sh", "gates.sh", "respaldar.sh", "restaurar.sh"],
)
def test_the_deploy_scripts_parse(bash: str, guion: str) -> None:
    """La primera comprobación de sintaxis de shell que tiene el repo.

    Hasta ahora, un paréntesis de más en cualquiera de estos se descubría en
    producción: nada los mira ni en pre-commit ni en CI.
    """
    ruta = (REPO_ROOT / "scripts" / guion).as_posix()
    resultado = subprocess.run([bash, "-n", ruta], capture_output=True, text=True, timeout=30)

    assert resultado.returncode == 0, resultado.stderr
