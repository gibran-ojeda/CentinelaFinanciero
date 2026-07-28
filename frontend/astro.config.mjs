// @ts-check
import node from '@astrojs/node';
import react from '@astrojs/react';
import { defineConfig } from 'astro/config';

// SSR con el adapter de node (decisión D6: SEO primero). El comparador tiene
// que llegar renderizado en el HTML — con JavaScript deshabilitado la tabla
// sigue completa — y sólo los filtros, la selección y el combinador hidratan
// como islas.
export default defineConfig({
  output: 'server',
  adapter: node({ mode: 'standalone' }),
  integrations: [react()],
  // Valor de build, sólo para lo que Astro resuelve internamente. Las URLs que
  // se publican —canónica, sitemap, robots— salen de `lib/sitio.ts`, que lee
  // `SITE_URL` en cada petición: el dominio se conoce al desplegar, no al
  // construir la imagen.
  site: process.env.SITE_URL ?? 'https://centinelafinanciero.lat',
  server: { host: '127.0.0.1', port: 8011 },
  vite: {
    // La API interna y su llave viven en el servidor. Un `import.meta.env`
    // sin prefijo `PUBLIC_` nunca llega al bundle del navegador, que es la
    // garantía de la que depende todo el BFF.
    ssr: { noExternal: ['@nanostores/react'] },
  },
});
