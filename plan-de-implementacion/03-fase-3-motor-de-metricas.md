# Fase 3 — Motor de métricas

## Objetivo

Toda la matemática del producto como **funciones puras exhaustivamente testeadas**, sin dependencia de infraestructura: TEN, equivalente GAT, ganancia real post-inflación, conversión UDI→MXN y el motor de banderas. Es la fase con mayor densidad de valor por línea: si estos números están mal, todo lo demás es una UI bonita sobre datos incorrectos.

## Entregables

`src/metrics/` — módulo de funciones puras (entradas y salidas como `Decimal` / modelos pydantic; nada de floats para dinero):

- `fiscal.py` — tratamiento ISR por tipo de instrumento (§4.2 del foundation): retención sobre **capital** (**0.90% anual** en 2026, parametrizada por `parametros_fiscales.tasa_retencion_capital`) para CETES/BONOS/BONDDIA/PRLV/SOFIPOs **y fondos de deuda** — el art. 87 de la LISR remite al régimen de capital del art. 54, no al de ganancia; caso especial UDIBONOS (ajuste inflacionario no gravado hasta vencimiento en ciertos esquemas). Prorrateo sobre base de **360 días**. Expone `retencion_isr(instrumento, monto, tasa, plazo_dias, params) -> Decimal` y la nota fiscal legible que la calculadora debe mostrar (§6).
- `ten.py` — Tasa Efectiva Neta: TNA menos el efecto de la retención aplicable, anualizada por plazo. `ten(tasa_nominal, instrumento, plazo_dias, params) -> Decimal`.
- `gat.py` — equivalente GAT calculado (§4.4) para instrumentos que no la publican: rendimiento anual real después de retenciones y comisiones. Además `gat_inconsistente(gat_publicada, tasa_nominal, umbral_pp) -> bool` para la bandera compuesta de §5.2.
- `real.py` — ganancia real (§4.5): `desglose_cascada(monto, tasa_nominal, instrumento, plazo_dias, inflacion_anual, params) -> DesgloseCascada` con los 5 conceptos: rendimiento bruto, ISR retenido, rendimiento neto, efecto inflación, ganancia real.
- `coverage.py` — cobertura de seguro: constantes en UDIs (IPAB=400_000, PROSOFIPO=25_000), `cobertura_mxn(tipo_seguro, valor_udi) -> Decimal | None` (None = sin límite para SOBERANO, 0 para NINGUNO).
- `flags.py` — motor de reglas de banderas:
  - Individuales (§5.1): IMOR (3%/6%), cobertura de cartera vencida (100%/70%), ICAP (15%/10.5%), NICAP (N2 amarilla, N3/N4 roja), apalancamiento.
  - Compuestas (§5.2): IMOR alto + ICAP bajo + crecimiento agresivo en captación → 🔴 "no recomendable"; tasa muy sobre mercado + IMOR en alerta → 🔴 "red flag"; GAT inconsistente → 🟡.
  - **Prioridad de severidad** (§5.2 nota de diseño): si hay compuesta 🔴, se emite solo esa — nunca compuesta e individual a la vez.
  - Umbrales leídos desde ConfigStore (`effective.umbral_*`), nunca hardcodeados.
  - Firma: `evaluar_banderas(indicadores: IndicadoresInstitucion, contexto_mercado, umbrales) -> list[Bandera]`.

## Tareas

1. Implementar `fiscal.py` primero (todo lo demás lo consume) con la tabla de §4.2 como fuente de verdad; documentar en docstring qué esquema fiscal implementa cada rama y su fecha de referencia.
2. Implementar `ten.py`, `gat.py`, `real.py`, `coverage.py`.
3. Implementar `flags.py` con las reglas individuales, luego las compuestas, luego la resolución de prioridad.
4. Tests (la parte más importante de la fase):
   - **Los ejemplos numéricos del foundation son tests obligatorios**: §4.5 ($100,000 al 7.5% nominal → TEN 6.60% → ganancia neta $6,600; inflación 4.5% → ganancia real $2,100) y la narrativa de §6 ("de $1,000 brutos: $120 impuestos, $600 inflación, $280 real"). Ambos con la retención de 0.90% vigente en 2026.
   - Casos borde fiscales: UDIBONOS, plazos < 1 año anualizados, monto en el límite de retención.
   - Property tests (hypothesis): monotonicidad (más tasa nominal ⇒ más TEN, ceteris paribus; más inflación ⇒ menos ganancia real), la cascada siempre suma (bruto = ISR + inflación + real ± redondeo documentado), banderas nunca emiten compuesta e individual juntas.
   - Matriz de banderas: un caso por celda de las tablas de §5.1 (sano / atención / alerta) y uno por combinación de §5.2.

## Criterios de aceptación

- [ ] Todos los ejemplos numéricos del foundation reproducidos como asserts que pasan.
- [ ] Cobertura de `src/metrics/` ≥ 95%.
- [ ] `flags.py` con los umbrales inyectados (no importa ConfigStore directamente — recibe el objeto de umbrales), de modo que el módulo sigue siendo puro y testeable.
- [ ] mypy strict sin errores en `src/metrics/`.
- [ ] Ninguna función usa `float` para montos o tasas — `Decimal` de punta a punta.

## Dependencias

Fase 2 (enums, modelos de dominio, ConfigStore para los umbrales). No requiere datos reales ni servicios levantados.
