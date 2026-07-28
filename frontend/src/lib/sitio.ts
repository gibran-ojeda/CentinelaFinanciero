/**
 * URL pública del sitio, resuelta **en cada petición**.
 *
 * La variable se llama `SITE_URL` y no `PUBLIC_SITE_URL` a propósito: Vite
 * sustituye las `PUBLIC_*` durante el build, y este valor no se conoce hasta
 * que arranca el contenedor. Horneada en la imagen, la variable llega vacía al
 * artefacto y el sitio desplegado anuncia como canónica una URL que no es la
 * suya. Es exactamente el mismo motivo por el que `API_READ_KEY` se lee de
 * `process.env` en [api.ts](./api.ts).
 *
 * Sin la variable se cae al origen de la petición. Detrás del proxy eso llega
 * en `http://` plano —el salto Caddy→web no lleva TLS—, así que sirve para
 * desarrollo y no para producción: el despliegue la exige.
 */

function normalizar(crudo: string | undefined): string | null {
  const valor = crudo?.trim();
  if (!valor) return null;
  try {
    // `.origin` descarta cualquier ruta: la base canónica es el host, no una
    // página. Un `SITE_URL=https://centinelafinanciero.lat/` no debe producir
    // canónicas con doble barra.
    return new URL(valor).origin;
  } catch {
    console.error(`[sitio] SITE_URL no es una URL válida y se ignora: ${valor}`);
    return null;
  }
}

/** Origen público del sitio (`https://host`), sin barra final. */
export function sitioPublico(peticion: URL): string {
  return normalizar(process.env.SITE_URL) ?? peticion.origin;
}

/** URL canónica absoluta de la página que se está sirviendo. */
export function urlCanonica(peticion: URL): string {
  return new URL(peticion.pathname, sitioPublico(peticion)).href;
}
