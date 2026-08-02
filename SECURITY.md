# Política de seguridad

Centinela Financiero es un proyecto de un solo mantenedor, sin SLA formal de respuesta. Aun así, cualquier reporte de vulnerabilidad se toma en serio.

## Cómo reportar

**No abras un issue público** para vulnerabilidades. En su lugar:

- Usa [GitHub Security Advisories](https://github.com/gibran-ojeda/centinela-financiero/security/advisories/new) para este repositorio, o
- Escribe directamente a **contacto@centinelafinanciero.lat** con el detalle del hallazgo y pasos para reproducirlo.

## Qué esperar

- Confirmación de recepción en un plazo razonable (no hay SLA formal, es un proyecto de un solo mantenedor).
- El sitio es informativo y de solo lectura: no maneja fondos, no pide registro y no guarda datos de visitantes. La API interna no se expone a internet (patrón BFF, ver §14 del foundation). Lo que sí importa y se atiende con prioridad:
  - **Integridad de los datos publicados** — una tasa manipulada o inventada es el daño real que este proyecto puede causar.
  - Acceso no autorizado a los endpoints administrativos de la API.
  - Abuso de las llaves de terceros (Banxico, DeepSeek) configuradas por quien despliega su propia instancia.

## Alcance

Están en alcance tanto la **instancia pública** ([centinelafinanciero.lat](https://centinelafinanciero.lat), operada por el mantenedor) como el código de este repositorio, que cada quien puede desplegar con sus propias credenciales (ver `.env.example`). No hay credenciales de terceros embebidas en el repositorio.
