# Fase 5 — Frontend MVP

## Objetivo

UI pública del comparador: tabla comparadora con capas de profundidad, calculadora visual en cascada y página de detalle por institución. **Astro SSR + islas React** (decisión D6): las páginas públicas se renderizan en servidor para SEO; la interactividad (filtros, calculadora) vive en islas React con TanStack Query.

## Entregables

- `frontend/` — proyecto Astro con TypeScript:
  - `src/pages/index.astro` — comparador: tabla server-rendered con las tasas vigentes (SEO: el contenido principal llega en el HTML), banderas 🟡🔴 junto al nombre de la institución, fecha de última actualización visible. Isla React `Filtros` (todos los filtros de §7, estado en query params para que las vistas filtradas sean enlazables/indexables).
  - `src/pages/institucion/[slug].astro` — detalle por institución (server-rendered, una URL indexable por institución): productos y tasas, capa de profundidad de §11 (indicadores de salud con su periodo, "cifras a `<mes año>`"), banderas con motivo, cobertura de seguro explicada.
  - `src/pages/calculadora.astro` — isla React `CalculadoraCascada`: inputs de §6 (monto, plazo, instrumento/s), visualización **en cascada, no tabla**: bruto → ISR → inflación → ganancia real, con la narrativa "de $X de ganancia bruta, $Y son impuestos, $Z inflación, $W tuya". Nota fiscal y disclaimer siempre visibles.
  - `src/components/` — `TablaComparador.astro`, `Bandera.astro` (tooltip con motivo y enlace al detalle), `SelloCobertura.astro` (IPAB/PROSOFIPO/soberano/sin cobertura — §4.6: "nunca en letra chica"), islas React en `src/components/islands/`.
  - Páginas estáticas: metodología (cómo se calculan TEN/GAT/banderas, umbrales vigentes), acerca de, disclaimer legal (§10: qué NO es la plataforma).
- **BFF**: endpoints server-side de Astro (`src/pages/api/*`) que llaman a la API interna inyectando `X-API-Key` desde variable de entorno del servidor — la key **jamás** llega al navegador. El navegador solo habla con `web`.
- `docker/web/Dockerfile` — build multi-stage (node:20 build → runtime del adapter node de Astro); servicio `web` en compose.
- SEO técnico: metadatos por página, Open Graph, `sitemap.xml`, `robots.txt`, datos estructurados (JSON-LD `FinancialProduct` donde aplique), títulos del tipo "Tasa de CETES hoy vs SOFIPOs — comparador".

## Tareas

1. Scaffold Astro + adapter node + integración React; configurar el proxy dev hacia la API local.
2. Implementar el BFF (cliente de la API con `X-API-Key`) y los tipos TS generados desde el OpenAPI de la fase 4.
3. Construir el comparador SSR + isla de filtros con estado en query params.
4. Construir la calculadora en cascada (isla React + TanStack Query contra el BFF).
5. Construir el detalle de institución y las páginas estáticas.
6. SEO técnico + accesibilidad básica (contraste, navegación por teclado en filtros, `aria` en banderas).
7. Dockerfile y servicio `web` en compose; smoke test del flujo completo navegador → web → api → db.

## Criterios de aceptación

- [x] Lighthouse SEO ≥ 90 y Performance ≥ 80 en `/` e `/institucion/[slug]` (build de producción). Medido en la fase 06: SEO 100 en las tres páginas; rendimiento 95 · 100 · 91 tras autoalojar las fuentes, que venían de Google con un `@import` y costaban 1.5 s de render bloqueado.
- [ ] Con JavaScript deshabilitado, el comparador muestra la tabla completa con tasas y banderas (SSR real).
- [ ] El navegador nunca recibe la `X-API-Key` ni llama a la API interna directamente (verificar en la pestaña Network).
- [ ] Cada dato con tasa muestra su fecha de actualización; cada bandera enlaza a su explicación.
- [ ] La calculadora reproduce visualmente el ejemplo de §6 y muestra la nota fiscal.
- [ ] Las URLs filtradas (`/?plazo=91&seguro=ipab`) son compartibles y renderizan server-side.

## Dependencias

Fase 4 (API con contrato OpenAPI estable).
