"""Infraestructura de LLM: proveedor, control de costo y parseo de respuestas.

Aquí sólo vive la plomería de hablar con un modelo. Lo que se le pide y qué se
hace con lo que contesta es del agente de tasas (`rates_agent`), y toda decisión
sobre publicar o no publicar un dato es determinista y vive fuera de este
paquete — el LLM extrae y estructura, no decide.

Es una versión reducida del módulo homónimo de NA: aquí no hace
falta un router por tiers ni varios proveedores. Un solo proveedor compatible
con la API de OpenAI (DeepSeek), un techo de gasto diario y un parser tolerante.
"""
