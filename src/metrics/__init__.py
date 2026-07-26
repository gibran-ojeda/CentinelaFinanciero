"""Motor de métricas: funciones puras sobre `Decimal`, sin infraestructura.

Aquí vive toda la matemática del producto — TEN, equivalente GAT, ganancia real
post-inflación, cobertura de seguro y el motor de banderas. Ningún módulo de
este paquete importa `core.db`, `core.config_store` ni nada que toque red o
disco: los parámetros y umbrales entran siempre como argumento.

Esa restricción no es purismo. Si estos números están mal, todo lo demás es una
interfaz bonita sobre datos incorrectos, y sólo se pueden verificar de forma
exhaustiva si se pueden llamar sin levantar nada.
"""
