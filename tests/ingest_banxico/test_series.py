"""El catálogo tiene que caber en las columnas donde se guarda.

`unidad` es un VARCHAR(40) y ya mordió una vez: la descripción de la base del
INPC medía 47 caracteres y el fallo salió como un error de Postgres a mitad de
la sincronización. Esto lo convierte en un test que no necesita base de datos.
"""

from __future__ import annotations

from domain.orm import SerieEconomica
from ingest_banxico import series as catalogo


def _largo(columna: str) -> int:
    tipo = SerieEconomica.__table__.columns[columna].type
    return int(tipo.length)  # type: ignore[attr-defined]


def test_every_series_fits_in_its_column() -> None:
    for serie in catalogo.CATALOGO:
        assert len(serie.clave) <= _largo("clave_banxico"), serie.clave
        assert len(serie.nombre) <= _largo("nombre"), serie.nombre
        assert len(serie.unidad) <= _largo("unidad"), serie.unidad


def test_the_keys_are_unique() -> None:
    claves = catalogo.claves()
    assert len(claves) == len(set(claves))


def test_the_cetes_tenors_match_products_that_exist() -> None:
    """Sin producto donde aterrizar, el materializador no tendría qué hacer.

    Los cuatro plazos están en `seeds/productos.yaml`; que este test los repita
    es a propósito, porque cambiar uno sin el otro rompería la fase entera.
    """
    assert sorted(catalogo.CETES_POR_PLAZO.values()) == [28, 91, 182, 364]
    assert set(catalogo.CETES_POR_PLAZO) <= set(catalogo.claves())
