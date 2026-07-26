# Handoff: Centinela Financiero — Fase 5 (Frontend MVP)

## Overview
UI pública del comparador de instrumentos de ahorro mexicanos (Fase 5 del plan "Brújula/Centinela Financiero"): **mercado comparador** (tabla/cards con TEN, GAT, cobertura y banderas), **calculadora de combinación de portafolio** con optimizador, **detalle por institución** y **metodología**. Rebrand vigente: **Centinela Financiero** (logo: ojo vigilante sobre pirámide escalonada azteca).

## About the Design Files
Los archivos de este paquete son **referencias de diseño construidas en HTML** (prototipo interactivo), NO código de producción. La tarea es **recrear estas pantallas en el stack objetivo del proyecto**, definido en la fase 5 del plan:

- **Astro SSR + islas React** con TypeScript (decisión D6 — SEO primero).
- Páginas server-rendered: `src/pages/index.astro` (comparador), `src/pages/institucion/[slug].astro`, `src/pages/calculadora.astro`.
- Interactividad (filtros, calculadora/combinador) como **islas React** con TanStack Query en `src/components/islands/`.
- Estado de filtros en **query params** (`/?plazo=91&seguro=ipab`) para URLs enlazables e indexables.
- **BFF**: endpoints server-side de Astro (`src/pages/api/*`) inyectan `X-API-Key`; el navegador nunca habla con la API interna.
- Con JS deshabilitado, la tabla del comparador debe renderizar completa (SSR real).

`Centinela Fase 5 (referencia).dc.html` es el prototipo. Ignora su infraestructura (etiquetas `<x-dc>`, `sc-for`, `sc-if`, `{{ }}`, clase `Component`): el **template equivale al JSX/markup** y la clase lógica documenta **estado, handlers y cálculos** a portar. `ds-industry-styles.css` solo aporta las fuentes/clases base heredadas; los tokens reales del tema oscuro están definidos en el `<style>` del prototipo.

## Fidelity
**High-fidelity (hifi).** Colores, tipografía, espaciados, radios, sombras y copys son finales. Recrear pixel-perfect con los patrones del codebase.

## Design Tokens
Paleta de marca (obligatoria): `#E3F6F5, #A7E0DB, #5FB0C9, #3E6D9C, #2A2F63`.

Tema oscuro (CSS custom properties del prototipo):
- Fondo: `#14163A` + radial `radial-gradient(1200px 500px at 70% -10%, rgba(62,109,156,.35), transparent)`
- Paneles: gradiente `linear-gradient(180deg, #262B5E, #20244F)`; superficie inputs `#1D2150`; dropdown `#232858`
- Línea/borde: `rgba(167,224,219,.14)`; hover de borde `rgba(167,224,219,.5)`
- Texto: `#E3F6F5`; muted `rgba(227,246,245,.62)`; muted-2 `rgba(227,246,245,.42)`
- Semánticos: positivo `#A7E0DB` (up), advertencia `#E7C36A` (warn), negativo/rojo `#F08A8A` (down)
- Acento/CTA: gradiente `linear-gradient(90deg, #5FB0C9, #3E6D9C)`
- Sombras: sm `0 2px 8px rgba(8,10,32,.45)` · md `0 8px 24px rgba(8,10,32,.5)` · lg `0 18px 48px rgba(8,10,32,.6)`
- Radios: cards `16px`, sub-cards/inputs `10–14px`, chips/botones/pills `999px`, checkbox `5px`, avatar círculo
- Tipografía: **Barlow Condensed 600** (encabezados, cifras, labels de sección; `letter-spacing .02–.08em`, uppercase en labels) sobre **Barlow 400** (cuerpo, 13–15px). Google Fonts. Cifras con `font-variant-numeric: tabular-nums`.
- H1: `clamp(30px, 6vw, 46px)`, blanco con la última frase en `#A7E0DB`.
- Iconos: Lucide, stroke 1.5.
- Focus visible: `outline: 2px solid #5FB0C9; outline-offset: 2px` (nunca el ring azul por defecto).

## Responsive (mobile-first)
Breakpoints: base <720 (móvil), `≥720` (tablet), `≥1080` (escritorio), `≥1600` (2K: escala ~1.12×), `≥2400` (4K: escala ~1.55×).
- `<720`: 1 columna; nav inferior fija de 3 tabs (Mercado / Calculadora / Método); píldora flotante de selección sobre la nav; cards en vez de tabla; filtros en grid 2 col; main max 680px, padding-bottom 150px.
- `≥720`: links en navbar (se oculta bottom nav y píldora flotante); cards 2 col; filtros en fila con wrap; layout calculadora 2 col (360px + 1fr); main max 960px.
- `≥1080`: la lista de cards se sustituye por **tabla** (grid de 10 columnas); main max 1220px.
- Hit targets móviles ≥44px.

## Screens / Views

### 1. Mercado (comparador) — `/`
- **Header sticky** (`rgba(16,18,48,.86)` + blur 10px, borde inferior línea): logo (cuadro 32px radio 9px con gradiente acento + SVG del centinela en trazo `#E3F6F5`) + "CENTINELA FINANCIERO" (Barlow Condensed 18px) + links Mercado/Calculadora/Metodología (activo blanco, resto muted). El link Calculadora navega directo, **no** modifica la selección.
- **Hero**: H1 "Mercado de ahorro / rendimiento real." + sub + pill "● Datos al 21 jul 2026" (fondo `rgba(167,224,219,.08)`) + texto de transparencia ("Las instituciones marcadas ◆ son ejemplos ilustrativos").
- **Filtros** (una fila, wrap): Plazo (select: Todos/A la vista/28/91/182/364/365+), **Seguro de depósito** (multiselect dropdown con checkboxes: Soberano/IPAB/PROSOFIPO), **Categoría** (multiselect: Gobierno/Banco digital/SOFIPO), Monto a invertir (texto, formatea miles `es-MX` mientras escribes, filtra `monto_minimo > monto`), Ordenar por (TEN/nominal/GAT/cobertura, desc), toggle pill "Solo sin banderas". Dropdowns: panel absoluto (radio 12, fondo `#232858`, sombra lg), checkbox 16px (marcado: gradiente `#A7E0DB→#5FB0C9`, ✓ `#14163A`); overlay fijo invisible cierra al clic fuera; botón resume selección ("Todos" o "IPAB, PROSOFIPO").
- **Barra "Tu selección"** (visible si hay instrumentos seleccionados; sobrevive a cambios de filtro): fondo `rgba(95,176,201,.08)`, borde `rgba(95,176,201,.25)`, radio 14; chips por instrumento con × (hover rojo), "Limpiar todo" (link subrayado), **"Calcular →"** (pill gradiente, a la derecha) — es el ÚNICO acceso que arma la combinación.
- **Tabla** (≥1080): grid `46px 1.4fr 1.35fr .55fr .75fr .75fr .95fr 1.05fr 1.15fr .7fr` → [+ | Institución | Producto | Plazo | Nominal | TEN | GAT | Cobertura | Banderas | Dato al]. Botón + circular 30px (borde `rgba(167,224,219,.35)`; seleccionado: relleno gradiente y "✓"). Avatar circular 28px con iniciales (fondo de un ciclo de tintes de la paleta, texto `#14163A`). TEN en `#A7E0DB` Barlow Condensed. GAT calculada marca "(equiv.)" en 10px muted. Banderas como pills (amarilla `rgba(231,195,106,.12)`/texto `#E7C36A`; roja `rgba(240,138,138,.12)`/`#F08A8A`) con `title` = motivo. Hover fila `rgba(95,176,201,.08)`. Números alineados a la derecha. Clic en institución → detalle.
- **Cards** (<1080): mismas cifras en grid 3 col (TEN/Nominal/GAT a 25px), banderas, cobertura y fecha.

### 2. Calculadora de combinación — `/calculadora`
Dos columnas (≥720): panel de entrada + resultados.
- **Panel entrada**: Monto total (texto con miles + chips $50k/$250k/$1 M/$5 M), Horizonte (pills 28/91/182/364 días), toggles "Respetar límites de seguro" y "Excluir banderas rojas" (filas pill radio 12; activo: borde/tinte `rgba(167,224,219,.4/.08)` texto `#A7E0DB`), lista de instrumentos (fila radio 12 fondo `rgba(20,22,58,.5)`: cuadrito de color 9px, nombre + subtítulo, input % (0–100 step 0.1) + botón × y "Limpiar"), select "+ Agregar instrumento…", botón "⚡ Optimizar combinación" (pill gradiente, full width, 48px).
- **Reglas de negocio**:
  - Σ% siempre visible; verde `#A7E0DB` si ≈100, ámbar si no. Para el cálculo se normaliza a 100.
  - **Agregar** un instrumento: los % existentes se reescalan proporcionalmente y el nuevo recibe `100/n` (siempre Σ=100, 1 decimal). **Quitar**: se renormaliza el resto.
  - **Optimizador** (greedy, reemplaza la selección actual): elegibles = vista o plazo ≤ horizonte, monto_min ≤ monto, sin bandera roja (si el toggle está activo); ordena por TEN desc; asigna a un producto por institución hasta el **tope de cobertura** de esa institución (IPAB 400,000 UDIs; PROSOFIPO 25,000 UDIs; soberano ∞ absorbe remanente); % con 1 decimal, el residuo de redondeo va al instrumento sin tope.
- **Resultados**: 3 stat-cards (TEN ponderada `#A7E0DB` / Ganancia real / % Protegido — floor, nunca 100% si hay excedente); **barra de asignación** apilada (pill 22px, colores del ciclo `#5FB0C9, #A7E0DB, #7D9BE0, #3E6D9C, #D8E9F5, #8ED0C6, #B9A7E0, #E7C36A`) + leyenda; **cascada** de 4 barras waterfall (pistas 12px radio 999): Rendimiento bruto (`#A7E0DB`, 100%) → ISR retenido (`#E7C36A`, offset derecho) → Efecto inflación (`#8FA0C9`) → Ganancia real (gradiente `#5FB0C9→#A7E0DB`; si negativa `#F08A8A`); caja narrativa ("De $X de ganancia bruta, $Y se van en impuestos, $Z se los come la inflación y $W son realmente tuyos.") fondo `rgba(95,176,201,.1)`; **detalle por instrumento** (monto, %, TEN, real, estado de cobertura: "Cubierto (IPAB)" verde / "Excede cobertura $N" ámbar con icono escudo); Nota fiscal y disclaimer siempre visibles.
- **Fórmulas** (portar como funciones puras; en producción vienen de la API/motor fase 3): `f = plazo/365`; `bruto = monto·tasa%·f`; `ISR = monto·0.50%·f` (retención sobre capital, LIF 2026); `inflación = monto·infl%·f` (default 4.2%); `real = bruto − ISR − inflación`; `TEN = nominal − 0.50`; `GAT equiv ≈ TEN − 0.05`; UDI = $8.56.

### 3. Detalle de institución — `/institucion/[slug]`
"← Volver al mercado"; avatar 46px + nombre + pills categoría/◆ demo; descripción. Dos columnas: (a) banderas activas como tarjetas tintadas con motivo completo, o línea verde "Sin banderas activas"; grid de 4 indicadores (IMOR/ICAP o NICAP/ICOR/Captación: valor 26px, estado con punto de color En rango/Atención/Alerta, descripción, "cifras a may 2026", fuente CNBV); para gobierno: texto "no aplican indicadores CNBV". (b) Tarjeta de **cobertura** destacada (gradiente tinte acento, borde `rgba(95,176,201,.3)`): escudo, tipo (IPAB/PROSOFIPO/Soberano), monto grande en `#A7E0DB` ("$3.42 M" / "$214 mil" / "Sin límite"), explicación en lenguaje claro. Lista de productos con nominal/TEN/GAT/fuente/fecha. CTAs: "+ Agregar a la combinación" (gradiente) y "Ver el mercado" (outline).

### 4. Metodología
4 tarjetas: TEN, Optimizador de combinación, Banderas de salud (umbrales: IMOR ≥3%/≥6%, ICAP <15%/<10.5%, cobertura <100%/<70%, NICAP N2/N3-N4; prioridad de severidad compuesta), Qué NO es Centinela.

## Interactions & Behavior
- Navegación: en producción son rutas reales (el prototipo simula con estado `view`); scroll-to-top al navegar.
- Hovers: filas `rgba(95,176,201,.08)`; CTAs `filter: brightness(1.12)`; chips borde `rgba(167,224,219,.5)`; botones destructivos viran a `#F08A8A`.
- Dropdowns multiselect: abren por botón, cierran al clic fuera; selección no cierra el panel.
- La selección de instrumentos persiste al cambiar filtros (vive fuera del estado de filtros; en producción: query param o store compartido entre islas).
- Accesibilidad: `aria-current="page"` en nav, `aria-label` en botones icónicos, banderas con texto (no solo color), teclado en filtros.

## State Management
- Filtros: `plazo` (single), `seguros[]`, `categorias[]`, `monto`, `orden`, `sinBanderas` — todos en query params.
- Selección/portafolio: `[{productoId, pct}]` (pct 1 decimal, Σ=100).
- Calculadora: `montoTotal`, `horizonteDias`, `respetarSeguro`, `excluirRojas`.
- Datos: la lista de productos/instituciones/indicadores del prototipo es **seed de demostración** (las instituciones ◆ "Ahorra+ Capital" y "Alcancía Fuerte" son ficticias, para ilustrar banderas); en producción vienen de `GET /api/v1/comparador`, `GET /api/v1/instituciones/{id}` y `POST /api/v1/calculadora` vía BFF.

## Assets
- Logo Centinela (SVG inline, trazo 1.5): pirámide escalonada + ojo. Paths: `M3 21h18` · `M5 21v-3h3v-4h3v-4` · `M19 21v-3h-3v-4h-3v-4` · `M11 10h2` · ojo `M12 3.6c2.3 0 3.8 2.2 3.8 2.2S14.3 8 12 8 8.2 5.8 8.2 5.8 9.7 3.6 12 3.6z` + pupila `circle cx12 cy5.8 r.6` (relleno). En header va blanco sobre cuadro 32px radio 9 con gradiente `135deg #5FB0C9→#3E6D9C`.
- Iconos Lucide (stroke 1.5): menú/list, calculadora, libro, escudo, alerta triángulo, chevron, flecha izquierda.
- Fuentes: Barlow y Barlow Condensed (Google Fonts).

## Files
- `Centinela Fase 5 (referencia).dc.html` — prototipo hifi completo (markup + lógica + tokens del tema oscuro en su `<style>`).
- `ds-industry-styles.css` — hoja base heredada (fuentes, clases `.input/.field`); los tokens oscuros del prototipo la sobreescriben.
