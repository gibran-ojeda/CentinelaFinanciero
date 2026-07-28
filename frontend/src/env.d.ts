/// <reference types="astro/client" />

interface ImportMetaEnv {
  /** URL de la API interna. Sin prefijo PUBLIC_: no llega al navegador. */
  readonly API_BASE_URL: string;
  /** Llave de lectura del BFF. Nunca la de admin. */
  readonly API_READ_KEY: string;
  readonly API_TIMEOUT_SECONDS?: string;
  /**
   * Origen público del sitio. Sin prefijo PUBLIC_ a propósito: se lee de
   * `process.env` en tiempo de ejecución, no del bundle. Ver `lib/sitio.ts`.
   */
  readonly SITE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
